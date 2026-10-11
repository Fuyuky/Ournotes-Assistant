"""Resumable BDON artwork catalog built from HK/TW/MO masterdata.

The recognizer uses the resulting local snapshot without network access.
Artwork endpoint/schema reference: StarMoe-org/moenotes commit
14f6680152a6e7e5299047d87f832c682179df82. No upstream application code is vendored.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from ournotes.catalogs.masterdata import download_tables

METADATA = "https://metadata.bdon.moe/master"
ASSETS = "https://assets.bdon.moe/zh-Hans"
TABLES = ("MasterMemberCard", "MasterSupportCard", "MasterText", "MasterCharacter")
RARITIES = {2: "R", 3: "SR", 4: "SSR"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(url: str, path: Path, *, image: bool = False) -> None:
    if path.exists():
        return
    request = urllib.request.Request(url, headers={
        "User-Agent": "OurNotesAssistant/0.1 (personal offline card recognition)",
        "Referer": "https://bdon.moe/",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                data = response.read()
            if image:
                Image.open(io.BytesIO(data)).verify()
            else:
                json.loads(data)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
            return
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(attempt + 1)


def _rows(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data["_allData"]
    return [{key.removeprefix("_"): value for key, value in row.items()} for row in rows]


def build_catalog(directory: Path, *, workers: int = 3) -> dict:
    """Keep existing downloaded files; write catalog only after every asset is valid."""
    directory = Path(directory)
    if not all((directory / 'raw' / f'{table}.json').exists() for table in TABLES):
        download_tables(directory / 'raw', TABLES)
    source_path = directory / 'raw' / 'source.json'
    source = json.loads(source_path.read_text('utf-8')) if source_path.exists() else None
    metadata = (f"https://raw.githubusercontent.com/StarMoe-org/moenotes-masterdata/{source['commit']}/{source['region']}"
                if source else METADATA)
    tables = {name: _rows(directory / "raw" / f"{name}.json") for name in TABLES}
    texts = {row["id"]: row for row in tables["MasterText"]}
    characters = {row["id"]: row for row in tables["MasterCharacter"]}

    def resolve(key: str) -> str:
        row = texts[key]
        for language in ("simplifiedChinese", "traditionalChinese", "japanese", "english", "korean"):
            text = row.get(language, "").strip()
            if text and text != key:
                return text
        raise ValueError(f"卡片文字缺失：{key}")

    cards = []
    for kind, table in (("member", "MasterMemberCard"), ("support", "MasterSupportCard")):
        for row in tables[table]:
            if row["rarity"] not in RARITIES:
                continue
            asset_id = int(row["assetID"])
            member = kind == "member"
            folder = "MemberCard" if member else "SupportCard"
            label = "member_thumbnail" if member else "snap_thumbnail"
            url = f"{ASSETS}/{folder}/{asset_id}/{label}/{label}.webp"
            display_name = resolve(row["nameTextID"])
            canonical_name = resolve(characters[row["characterID"]]["nameTextID"]) if member else display_name
            # Character metadata includes Ave Mujica stage aliases; expose the civilian
            # name used by the requested text format, keeping the original display name.
            canonical_name = canonical_name.split(" / ")[-1]
            cards.append({
                "key": f"{kind}:{row['id']}", "id": row["id"], "kind": kind,
                "asset_id": asset_id, "rarity": RARITIES[row["rarity"]],
                "title": resolve(row["subtitleTextID"] if member else row["descriptionTextID"]),
                "name": canonical_name, "display_name": display_name,
                "image": f"images/{kind}-{asset_id}.webp", "image_url": url,
                "source_page": f"https://bdon.moe/{'cards' if member else 'support-cards'}/{row['id']}",
            })

    def fetch(card: dict) -> dict:
        path = directory / card["image"]
        _download(card["image_url"], path, image=True)
        with Image.open(path) as im:
            card["image_size"] = list(im.size)
        card["sha256"] = sha256(path)
        return card

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as executor:
        cards = list(executor.map(fetch, cards))
    catalog = {
        "schema_version": 1, "retrieved_at": datetime.now(UTC).isoformat(),
        "source": "https://bdon.moe/", "reference_commit": "14f6680152a6e7e5299047d87f832c682179df82",
        "note": "一次性本地快照；成员和留影均包含R/SR/SSR；游戏美术版权归原权利人。",
        "tables": [{"file": f"raw/{table}.json", "url": f"{metadata}/{table}.json",
                    "sha256": sha256(directory / "raw" / f"{table}.json")} for table in TABLES],
        "cards": cards,
    }
    if source:
        catalog['masterdata'] = source
    (directory / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return catalog
