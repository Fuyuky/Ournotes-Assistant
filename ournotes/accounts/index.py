"""Validated, credential-free public projection of existing account archives."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ournotes.accounts.pricing import price_accounts
from ournotes.accounts.quality import character_map, describe, ssr_rosters
from ournotes.accounts.usage import status as usage_status

LOG = logging.getLogger("account_browser")


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("invalid_integer")
    return value


@dataclass(frozen=True)
class Snapshot:
    accounts: tuple
    files: int
    excluded: int
    loaded_at: str
    available: bool
    revision: int
    # Private join keys for local tools; never included in account projections.
    identities: dict = field(default_factory=dict)


class AccountIndex:
    def __init__(self, accounts_dir, catalog_dir):
        self.directory = Path(accounts_dir).resolve()
        self.catalog_dir = Path(catalog_dir).resolve()
        self.definitions = {}
        self.cards = {}
        self.images = {}
        self.characters = character_map(self.catalog_dir)
        catalog = json.loads((self.catalog_dir / "catalog.json").read_text("utf-8"))
        for card in catalog["cards"]:
            kind, card_id = card["kind"], integer(card["id"], 1)
            if kind not in ("member", "support"):
                raise ValueError("Invalid catalog kind")
            key = f"{kind}:{card_id}"
            if key in self.definitions:
                raise ValueError("Duplicate catalog key")
            self.definitions[key] = card["rarity"]
            if card["rarity"] != "SSR":
                continue
            image = (self.catalog_dir / card["image"]).resolve()
            if (
                not image.is_relative_to(self.catalog_dir / "images")
                or image.suffix.lower() not in (".webp", ".png", ".jpg", ".jpeg")
                or not image.is_file()
            ):
                raise ValueError("Invalid catalog image")
            self.images[key] = image
            self.cards[key] = {
                "key": key,
                "id": card_id,
                "kind": kind,
                "rarity": "SSR",
                "name": card["name"],
                "title": card["title"],
                "image": f"/art/{kind}/{card_id}",
            }
        self.rosters = ssr_rosters(catalog, self.characters)
        self.signature = None
        self.snapshot = Snapshot((), 0, 0, "", False, 0)

    def scan(self):
        if not self.directory.is_dir():
            raise OSError("Archive directory unavailable")
        return tuple(
            sorted(
                (p.name, p.stat().st_mtime_ns, p.stat().st_size)
                for p in self.directory.iterdir()
                if p.suffix.lower() == ".json"
            )
        )

    def account(self, data):
        if type(data.get("used", False)) is not bool:
            raise ValueError("invalid_used_flag")
        current_usage = usage_status(data)
        pull_count = data.get("pull_count")
        if pull_count is not None:
            integer(pull_count, 1)
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported_schema")
        if data.get("status") != "complete":
            raise ValueError("not_complete")
        if integer(data.get("unknown_slots", 0)) != 0 or data.get("skipped"):
            raise ValueError("unresolved_results")
        if not isinstance(data["cards"], list):
            raise TypeError("invalid_cards")
        index = integer(data["alias_index"])
        counts = Counter()
        totals = Counter()
        for item in data["cards"]:
            card, count = item["card"], integer(item["count"], 1)
            key = f"{card['kind']}:{integer(card['id'], 1)}"
            if self.definitions.get(key) != card["rarity"]:
                raise ValueError("unknown_card_or_rarity")
            totals[card["kind"], card["rarity"]] += count
            if card["rarity"] == "SSR":
                counts[key] += count
        for kind in ("member", "support"):
            for rarity in ("SSR", "SR"):
                if integer(data["totals"][kind][rarity]) != totals[kind, rarity]:
                    raise ValueError("inconsistent_totals")
        pool_keys = data.get('pool_keys', [])
        if not isinstance(pool_keys, list) or any(not isinstance(key, str) for key in pool_keys):
            raise ValueError('invalid_pool_keys')
        pool_keys = sorted(set(pool_keys) & {'bdon-gacha-10', 'bdon-gacha-12'})
        # Explicit projection: no alias, UID, filename, context or raw JSON survives.
        member, support = totals["member", "SSR"], totals["support", "SSR"]
        return {
            "index": index,
            "used": current_usage != 'available',
            "pull_count": pull_count,
            "pool_keys": pool_keys,
            "ssr_total": member + support,
            "sr_total": totals["member", "SR"] + totals["support", "SR"],
            **describe(counts, self.characters, self.rosters, pool_keys,
                       activity=data.get('activity'), activity_known='activity_id' in data),
            "cards": tuple(
                {"key": key, "count": counts[key]}
                for key in sorted(counts, key=lambda k: (k.split(":")[0], int(k.split(":")[1])))
            ),
        }

    def refresh(self):
        try:
            signature = self.scan()
        except OSError:
            if self.snapshot.available or not self.snapshot.loaded_at:
                LOG.warning("Archive directory unavailable; clearing published accounts")
                self.publish((), 0, 0, False)
            self.signature = None
            return
        if signature == self.signature:
            return
        parsed, indices = [], Counter()
        for name, _, _ in signature:
            path = self.directory / name
            try:
                if path.is_symlink() or path.resolve().parent != self.directory:
                    raise ValueError("external_archive")
                data = json.loads(path.read_text("utf-8"))
                index = integer(data["alias_index"])
                indices[index] += 1
                parsed.append((name, index, data))
            except (OSError, ValueError, KeyError, TypeError):
                LOG.warning("Excluded archive %r: unreadable or invalid account index", name)
        accounts, identities = [], {}
        for name, index, data in parsed:
            try:
                if indices[index] != 1:
                    raise ValueError("duplicate_index")
                accounts.append(self.account(data))
                if isinstance(data.get('account_id'), str) and data['account_id']:
                    identities[index] = data['account_id']
            except (ValueError, KeyError, TypeError) as error:
                # Only controlled reason codes; do not log card/credential contents.
                reason = str(error) if type(error) is ValueError else "invalid_schema"
                LOG.warning("Excluded archive %r: %s", name, reason)
        try:
            if self.scan() != signature:
                return  # Never publish a mixture of files changed during a refresh.
        except OSError:
            self.signature = None
            self.publish((), 0, 0, False)
            return
        self.signature = signature
        self.publish(tuple(accounts), len(signature), len(signature) - len(accounts), True, identities)

    def publish(self, accounts, files, excluded, available, identities=None):
        self.snapshot = Snapshot(
            price_accounts(accounts),
            files,
            excluded,
            datetime.now(UTC).isoformat(),
            available,
            self.snapshot.revision + 1,
            identities or {},
        )
