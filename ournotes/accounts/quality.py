"""SSR character identities, band rosters and collection labels."""

from __future__ import annotations

import json
from pathlib import Path

from ournotes.accounts.pricing import DREAM_PICKUPS

BANDS = {
    "mygo": {"name": "MyGO!!!!!", "characters": frozenset((1, 2, 3, 4, 5))},
    "mujica": {"name": "Ave Mujica", "characters": frozenset((6, 7, 8, 9, 10))},
    "dream": {"name": "梦限大", "characters": frozenset((11, 12, 13, 14, 15))},
    "millsage": {"name": "Millsage", "characters": frozenset((16, 17, 18, 19, 20))},
    "dumb": {"name": "一家 Dumb Rock", "characters": frozenset((21, 22, 23, 24, 25))},
    "crychic": {"name": "CRYCHIC", "characters": frozenset((1, 4, 5, 7, 10))},
}
LOGO_FILES = {"mygo": "mygo.webp", "mujica": "mujica.webp", "crychic": "crychic.png"}


def historical_activity(pool_keys):
    """Only documented, unambiguous historical pool identities authorize a backfill."""
    known = sorted(set(pool_keys or ()) & {'bdon-gacha-10', 'bdon-gacha-12'})
    if len(known) != 1:
        return None
    key = known[0]
    dream = key == 'bdon-gacha-10'
    return {'id': key, 'pool_key': key, 'gacha_id': 10 if dream else 12,
            'name': '梦限大' if dream else 'millsage', 'label': '梦限大 一期' if dream else 'millsage 一期',
            'pickup_card_keys': sorted(DREAM_PICKUPS if dream else
                                       {'member:65', 'member:66', 'support:65', 'support:66'})}


def character_map(catalog_dir):
    raw = Path(catalog_dir) / "raw/MasterMemberCard.json"
    if not raw.is_file():
        return {}
    return {c["_id"]: c["_characterID"] for c in json.loads(raw.read_text("utf-8"))["_allData"]}


def ssr_rosters(catalog, mapping):
    available = {mapping[c['id']] for c in catalog['cards'] if c['kind'] == 'member'
                 and c['rarity'] == 'SSR' and c['id'] in mapping}
    return {key: band['characters'] & available for key, band in BANDS.items()}


def describe(counts, mapping, rosters=None, pool_keys=None, *, activity=None, activity_known=False):
    characters = set()
    for key in counts:
        kind, identifier = key.split(":")
        if kind == "member":
            if int(identifier) not in mapping:
                raise ValueError("missing_character_mapping")
            characters.add(mapping[int(identifier)])
    result = {f"{key}_characters": len(characters & band["characters"]) for key, band in BANDS.items()}
    result['unique_characters'] = len(characters)
    result['dream_pickup_count'] = len(DREAM_PICKUPS.intersection(counts))
    result['millsage_pickup_count'] = len({'member:65', 'member:66', 'support:65', 'support:66'}.intersection(counts))
    activity = activity if activity_known else historical_activity(pool_keys)
    progress = None
    if activity:
        targets = set(activity['pickup_card_keys'])
        total = len(targets)
        held = len(targets.intersection(counts))
        if total:
            progress = {'id': activity['id'], 'label': activity['label'], 'up_count': held, 'up_total': total}
    result['activity'] = progress
    rosters = rosters or {key: band['characters'] for key, band in BANDS.items()}
    result['band_capacity'] = {key: len(rosters.get(key, ())) for key in ('mygo', 'mujica', 'dream', 'millsage', 'dumb')}
    badges = []
    for key, band in BANDS.items():
        held = result[f"{key}_characters"]
        if key in ('dream', 'millsage'):
            expected = 'bdon-gacha-10' if key == 'dream' else 'bdon-gacha-12'
            if progress and progress['id'] == expected and progress['up_count']:
                badges.append(f"{key}-{progress['up_count']}")
        elif key == 'dumb':
            continue
        elif band["characters"].issubset(characters):
            badges.append(f"{key}-full")
        elif key != "crychic" and held in (3, 4):
            badges.append(f"{key}-{held}")
    return {**result, "badges": badges}
