"""Static publication of validated accounts. Standard library only; no credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ournotes.accounts.index import AccountIndex
from ournotes.accounts.pricing import price_sort_key
from ournotes.accounts.quality import BANDS, LOGO_FILES
from ournotes.paths import ROOT

WEB = ROOT / "web/account-browser"
EXPORT_MARKER = ".ournotes-static-export"
MARKER_CONTENT = "ournotes-static-export-v1\n"


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def public_path(name):
    return bool(
        re.fullmatch(
            r"(?:index\.html|data/current\.json|data/snapshot\.[a-f0-9]{64}\.json|"
            r"static/(?:app|style|data-source)\.[a-f0-9]{64}\.(?:js|css)|"
            r"art/(?:member|support)-[0-9]+\.[a-f0-9]{64}\.(?:png|webp|jpg|jpeg)|"
            r"brand/(?:mygo|mujica|crychic)\.[a-f0-9]{64}\.(?:webp|png))",
            name,
        )
    )


def export_site(accounts_dir, catalog_dir, output, *, allow_empty=False, public_account_limit=None,
                public_catalog=False, reuse_directory=None, asset_cache=None, announcement=None):
    public_catalog = public_catalog or public_account_limit is not None
    if public_account_limit is not None and (
        type(public_account_limit) is not int or public_account_limit <= 0
    ):
        raise ValueError("Public account limit must be a positive integer")
    output = Path(output).absolute()
    if output.resolve() != output or len(output.parts) < 3:
        raise ValueError("Export directory must be an explicit, non-symlink directory")
    # Never allow export deletion to overlap source archives, artwork or project code.
    for source in (
        Path(accounts_dir).resolve(),
        Path(catalog_dir).resolve(),
        WEB,
        ROOT / "ournotes",
        ROOT / "scripts",
    ):
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("Export directory overlaps a source directory")
    marker = output / EXPORT_MARKER
    if output.exists():
        if not output.is_dir() or (
            any(output.iterdir())
            and (not marker.is_file() or marker.is_symlink() or marker.read_text("utf-8") != MARKER_CONTENT)
        ):
            raise ValueError("Refusing to overwrite an unmanaged export directory")
        if any(p.is_symlink() for p in output.rglob("*")):
            raise ValueError("Refusing symlinks in export directory")

    catalog_file = Path(catalog_dir) / "catalog.json"
    catalog_before = catalog_file.read_bytes()
    mapping_file = Path(catalog_dir) / "raw/MasterMemberCard.json"
    mapping_before = mapping_file.read_bytes() if mapping_file.is_file() else None
    index = AccountIndex(accounts_dir, catalog_dir)
    index.refresh()
    snapshot = index.snapshot
    if not snapshot.available or index.signature is None:
        raise ValueError("Account directory unavailable or changed during export; retry")
    if not snapshot.accounts and not allow_empty:
        raise ValueError("No valid accounts; use --allow-empty only to deliberately publish an empty site")

    accounts = snapshot.accounts
    if public_account_limit is not None:
        # Filter before limiting; used accounts must never consume a public slot.
        # Pending first, descending price, ascending index, matching the browser.
        selected = sorted(
            (a for a in accounts if not a["used"]),
            key=price_sort_key,
        )[:public_account_limit]
        accounts = tuple(selected)

    files = {}
    descriptions, next_cache = {}, {}

    def asset(source, prefix):
        stat = source.stat()
        signature = [stat.st_size, stat.st_mtime_ns]
        prior = (asset_cache or {}).get(str(source))
        if prior and prior['stat'] == signature and reuse_directory is not None:
            previous = Path(reuse_directory) / prior['name']
            if previous.is_file() and not previous.is_symlink() and previous.stat().st_size == prior['description']['size']:
                name = prior['name']
                if not public_path(name):
                    raise ValueError('Invalid cached public asset')
                files[name] = previous
                descriptions[name] = prior['description']
                next_cache[str(source)] = prior
                return name
        content = source.read_bytes()
        name = f'{prefix}.{digest(content)}{source.suffix.lower()}'
        files[name] = content
        descriptions[name] = {'sha256': digest(content), 'size': len(content)}
        next_cache[str(source)] = {'stat': signature, 'name': name, 'description': descriptions[name]}
        return name

    cards = []
    for key, card in sorted(index.cards.items()):
        image_path = index.images[key]
        name = asset(image_path, f"art/{card['kind']}-{card['id']}")
        cards.append({**card, "image": name})
    bands = {}
    for key, band in BANDS.items():
        if key not in LOGO_FILES:
            bands[key] = {'name': band['name']}
            continue
        source = WEB / "logos" / LOGO_FILES[key]
        name = asset(source, f"brand/{key}")
        bands[key] = {"name": band["name"], "logo": name}
    payload = {
        "schema_version": 2,
        "cards": cards,
        "bands": bands,
        "accounts": sorted(accounts, key=lambda account: account["index"]),
        "meta": {
            "files": snapshot.files,
            "valid_accounts": sum(not a["used"] for a in accounts),
            "used_accounts": sum(a["used"] for a in accounts),
            "total_valid_accounts": len(accounts),
            "excluded_files": snapshot.excluded,
            "available": True,
        },
    }
    if public_catalog:
        payload["meta"].update(
            public_catalog=True, public_account_limit=public_account_limit, files=len(accounts), excluded_files=0
        )
    from ournotes.accounts.announcement import DEFAULT_ANNOUNCEMENT
    announcement = DEFAULT_ANNOUNCEMENT if announcement is None else announcement
    # Stable across account additions and used changes; changes with website/artwork/content.
    website_version = digest(encoded({'cards': cards, 'bands': bands, 'announcement': announcement,
        'assets': {p.name: digest(p.read_bytes()) for p in WEB.iterdir()
                   if p.suffix in ('.js', '.css', '.html')}}))
    payload['announcement'] = {'content': announcement, 'version': website_version}
    revision = digest(encoded(payload))
    data_name = f"data/snapshot.{revision}.json"
    old_data = output / data_name
    # No-op exports are byte-for-byte stable, including the data timestamp.
    loaded_at = datetime.now(UTC).isoformat()
    if old_data.is_file():
        try:
            previous = json.loads(old_data.read_text("utf-8"))
            previous_payload = {k: v for k, v in previous.items() if k != "revision"}
            old_time = previous_payload["meta"].pop("loaded_at")
            if digest(encoded(previous_payload)) == revision:
                loaded_at = old_time
        except (KeyError, ValueError, TypeError):
            pass
    payload["revision"] = revision
    payload["meta"]["loaded_at"] = loaded_at
    files[data_name] = encoded(payload)
    files["data/current.json"] = encoded({"schema_version": 2, "revision": revision})
    html = (WEB / "index.html").read_text("utf-8")
    if public_catalog:
        html = html.replace("<body>", '<body class="public-catalog">')
    for name, source in (
        ("style.css", "style.css"),
        ("app.js", "app.js"),
        ("data-source.js", "static-source.js"),
    ):
        content = (WEB / source).read_bytes()
        stem, ext = name.rsplit(".", 1)
        asset = f"static/{stem}.{digest(content)}.{ext}"
        files[asset] = content
        html = html.replace("/static/" + name, asset)
    html = html.replace("每 5 秒检查数据更新", "每 5 秒检查已发布数据更新")
    files["index.html"] = html.encode("utf-8")
    mapping_after = mapping_file.read_bytes() if mapping_file.is_file() else None
    if (
        index.scan() != index.signature
        or catalog_file.read_bytes() != catalog_before
        or mapping_after != mapping_before
    ):
        raise ValueError("Source changed during export; retry before publishing")
    if not all(public_path(name) for name in files):
        raise ValueError("Unexpected publication path")

    output.mkdir(parents=True, exist_ok=True)
    marker.write_text(MARKER_CONTENT, "utf-8")
    written = 0
    reused = 0
    # Publish the pointer and page last; only delete within this marked output root.
    for name in sorted(files, key=lambda p: (p in ("data/current.json", "index.html"), p)):
        target = output / name
        content = files[name]
        if isinstance(content, Path):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if os.path.samefile(content, target):
                    reused += 1
                    continue
                target.unlink()
            try:
                os.link(content, target)
            except OSError:
                shutil.copyfile(content, target)
            reused += 1
            continue
        if target.is_file() and target.read_bytes() == files[name]:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(files[name])
        os.replace(temporary, target)
        written += 1
    deleted = 0
    for path in output.rglob("*"):
        if not path.resolve().is_relative_to(output):
            raise ValueError("Export path escaped managed root")
        if path.is_file() and path.name != EXPORT_MARKER and path.relative_to(output).as_posix() not in files:
            path.unlink()
            deleted += 1
    return {
        "output": str(output),
        "revision": revision,
        "accounts": len(accounts),
        "public_account_limit": public_account_limit,
        "excluded": snapshot.excluded,
        "images": len(cards),
        "written": written,
        "deleted": deleted,
        "reused": reused,
        "asset_cache": next_cache,
        "files": {
            name: descriptions[name] if isinstance(content, Path) else {"sha256": digest(content), "size": len(content)}
            for name, content in sorted(files.items())
        },
    }
