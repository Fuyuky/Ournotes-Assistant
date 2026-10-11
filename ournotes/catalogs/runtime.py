"""Versioned BDON snapshots used only by the game runner (not the account website)."""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from ournotes.catalogs.cards import (
    ASSETS,
    RARITIES,
    TABLES,
    _download,
    _rows,
    build_catalog,
    sha256,
)
from ournotes.catalogs.masterdata import download_tables
from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.paths import DATA_ROOT as ROOT

RUNTIME_ROOT = ROOT / 'data/catalog'
EXTRA_TABLES = ('MasterItem', 'MasterGacha', 'MasterGachaProduct', 'MasterGachaLot',
                'MasterGachaPrize', 'MasterGachaView')
POOL_IDENTITIES = {
    'launch-mygo-pickup-20260831': 1,
    'launch-ave-mujica-pickup-20260831': 2,
    'launch-ssr-guaranteed-ticket': 3,
    'launch-ssr-member-guaranteed-ticket': 4,
    'launch-ssr-support-guaranteed-ticket': 5,
    'launch-sr-plus-guaranteed-ticket': 6,
    'our-notes-standard': 7,
}


def pool_key(gacha_id):
    return next((key for key, value in POOL_IDENTITIES.items() if value == gacha_id),
                f'bdon-gacha-{gacha_id}')


def asset_url(key):
    key = key.removesuffix('.png')
    if not key or '..' in key or key.startswith('/') or ':' in key:
        raise ValueError('BDON素材路径无效。')
    return f'{ASSETS}/{key}/{key.rsplit("/", 1)[-1]}.webp'


def current_catalog(root=RUNTIME_ROOT):
    root = Path(root).resolve()
    pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
    folder = (root / pointer['directory'].replace('\\', '/')).resolve()
    if not folder.is_relative_to(root):
        raise ValueError('运行素材目录越界。')
    for name in ('catalog.json', 'pools.json'):
        if sha256(folder / name) != pointer['sha256'][name]:
            raise ValueError(f'运行素材清单校验失败：{name}')
    return folder


def saved_snapshot(data_root, relative):
    """Resolve legacy pinned paths without rewriting account identity or hashes."""
    relative = str(relative).replace('\\', '/')
    if relative.startswith('data/bdon-runtime/snapshots/'):
        relative = 'data/catalog/snapshots/' + relative.removeprefix('data/bdon-runtime/snapshots/')
    root = Path(data_root).resolve()
    folder = (root / relative).resolve()
    if not folder.is_relative_to(root / 'data/catalog/snapshots'):
        raise ValueError('保存的运行素材路径越界。')
    return folder


def compile_pools(tables, cards):
    texts = {x['id']: x for x in tables['MasterText']}
    items = {x['id']: x for x in tables['MasterItem']}
    products = {x['id']: x for x in tables['MasterGachaProduct']}
    card_keys = {c['key'] for c in cards}
    unsupported = {f"{kind}:{row['id']}" for kind, table in
                   (('member', 'MasterMemberCard'), ('support', 'MasterSupportCard'))
                   for row in tables[table] if row['rarity'] not in RARITIES}

    def text(key):
        value = texts[key].get('simplifiedChinese', '').strip()
        if not value or value == key:
            raise ValueError(f'BDON中文名称缺失：{key}')
        return value

    pools = []
    for row in tables['MasterGacha']:
        group_ids = {x['prizeGroupId'] for x in tables['MasterGachaLot']
                     if x['lotGroupId'] == row['lotGroupId']}
        prizes, pickups = [], []
        for prize in tables['MasterGachaPrize']:
            if prize['groupId'] not in group_ids or prize['resourceType'] not in (2, 3):
                continue
            kind = 'member' if prize['resourceType'] == 2 else 'support'
            key = f"{kind}:{prize['resourceId']}"
            if key not in card_keys and key not in unsupported:
                raise ValueError(f'卡池引用缺失卡牌：{key}')
            prizes.append(key)
            if prize.get('pickUpType') == 2:
                pickups.append(key)
        item = items.get(row['gachaTicketItemId'])
        pools.append({
            'key': pool_key(row['id']), 'gacha_id': row['id'], 'name': text(row['nameTextId']),
            'banner_path': row['bannerAssetName'], 'logo_path': row['logoAssetName'],
            'banner': f"pools/{row['id']}-banner.webp", 'logo': f"pools/{row['id']}-logo.webp",
            'ticket_item_id': row['gachaTicketItemId'],
            'ticket_name': text(item['nameTextId']) if item else None,
            'products': [products[row[f'productId{i}']] for i in range(1, 5) if row[f'productId{i}']],
            'start_at': row['startAt'], 'end_at': row['endAt'],
            'card_keys': sorted(set(prizes)), 'source_page': f"https://bdon.moe/gacha/{row['id']}",
            'pickup_card_keys': sorted(set(pickups)),
            'unsupported_card_keys': sorted(set(prizes) & unsupported),
        })
    return pools


def sync_catalog(root=RUNTIME_ROOT, *, workers=3, max_age=3600, seed_raw=None):
    """Refresh metadata; switch the pointer only after the complete snapshot validates.

    A controller pins the returned path. Later updates cannot change its templates.
    seed_raw is for reproducible offline development/tests, never the automatic refresh.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with SecretStore(root).lock('sync'):
        previous = None
        if (root / 'current.json').exists():
            previous = current_catalog(root)
            pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
            if max_age and time.time() - pointer['checked_at'] < max_age:
                return previous
        folder = root / 'snapshots' / uuid.uuid4().hex
        folder.mkdir(parents=True)
        try:
            if not seed_raw:
                download_tables(folder / 'raw', TABLES + EXTRA_TABLES)
            elif (Path(seed_raw) / 'source.json').is_file():
                (folder / 'raw').mkdir(exist_ok=True)
                shutil.copy2(Path(seed_raw) / 'source.json', folder / 'raw' / 'source.json')
            for name in TABLES + EXTRA_TABLES:
                target = folder / 'raw' / f'{name}.json'
                if seed_raw:
                    target.parent.mkdir(exist_ok=True)
                    shutil.copy2(Path(seed_raw) / target.name, target)
            if previous and unchanged_snapshot(previous, folder):
                # A check timestamp is not a new resource version. Keep every pinned
                # path valid and discard only this invocation's unpublished directory.
                temporary = folder.resolve()
                if temporary.parent != (root / 'snapshots').resolve() or temporary == previous.resolve():
                    raise ValueError('临时素材目录越界。')
                shutil.rmtree(temporary)
                atomic_json(root / 'current.json', {**pointer, 'checked_at': time.time()})
                return previous
            # Immutable images already validated by earlier card catalogs can be reused.
            cached = previous or ROOT / 'data/catalog/site'
            if (cached / 'catalog.json').exists():
                old = json.loads((cached / 'catalog.json').read_text(encoding='utf-8'))
                for card in old['cards']:
                    source = (cached / card['image']).resolve()
                    if source.is_relative_to(cached.resolve()) and source.exists() and sha256(source) == card['sha256']:
                        target = folder / card['image']
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
            catalog = build_catalog(folder, workers=workers)
            catalog['snapshot_purpose'] = 'runner-only; isolated from account website assets'
            catalog['metadata_checked_at'] = time.time()
            atomic_json(folder / 'catalog.json', catalog)
            tables = {name: _rows(folder / 'raw' / f'{name}.json') for name in TABLES + EXTRA_TABLES}
            jobs = []
            pools = compile_pools(tables, catalog['cards'])
            for pool in pools:
                if pool['unsupported_card_keys']:
                    continue
                for kind in ('banner', 'logo'):
                    jobs.append({'image': pool[kind], 'url': asset_url(pool[kind + '_path']),
                                 'pool_key': pool['key'], 'role': kind})

            cached_assets = {}
            if previous:
                for name, key in (('pools.json', 'assets'),):
                    for entry in json.loads((previous / name).read_text(encoding='utf-8'))[key]:
                        cached_assets[entry['image']] = entry

            def fetch(entry):
                target = folder / entry['image']
                cached_entry = cached_assets.get(entry['image'])
                source = (previous / entry['image']).resolve() if previous else None
                if (cached_entry and cached_entry['url'] == entry['url'] and source.exists()
                        and source.is_relative_to(previous.resolve()) and sha256(source) == cached_entry['sha256']):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                _download(entry['url'], target, image=True)
                with Image.open(target) as im:
                    entry['image_size'] = list(im.size)
                entry['sha256'] = sha256(target)

            with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as executor:
                list(executor.map(fetch, jobs))
            pool_assets = [entry for entry in jobs if 'pool_key' in entry]
            atomic_json(folder / 'pools.json', {'schema_version': 1, 'pools': pools, 'assets': pool_assets})
            if previous:
                share_unchanged_files(folder, previous)
            atomic_json(root / 'current.json', {
                'schema_version': 1, 'checked_at': time.time(), 'directory': folder.relative_to(root).as_posix(),
                'sha256': {name: sha256(folder / name) for name in ('catalog.json', 'pools.json')},
            })
            return current_catalog(root)
        finally:
            # Only this call's unpublished directory; never remove a pinned version.
            published = None
            if (root / 'current.json').is_file():
                published = json.loads((root / 'current.json').read_text('utf-8')).get('directory')
            if folder.exists() and (published or '').replace('\\', '/') != folder.relative_to(root).as_posix():
                from ournotes.runtime.storage import inside
                inside(root, folder, root / 'snapshots')
                for path in folder.rglob('*'):
                    inside(root, path, folder)
                shutil.rmtree(folder)



def unchanged_snapshot(previous, candidate):
    """Reuse only an intact snapshot with identical upstream metadata."""
    try:
        source = candidate / 'raw' / 'source.json'
        if source.exists():
            old_source = json.loads((previous / 'raw' / 'source.json').read_text('utf-8'))
            new_source = json.loads(source.read_text('utf-8'))
            if any(old_source.get(k) != new_source.get(k) for k in ('region', 'version', 'resource_version')):
                return False
        for name in TABLES + EXTRA_TABLES:
            if sha256(previous / 'raw' / f'{name}.json') != sha256(candidate / 'raw' / f'{name}.json'):
                return False
        entries = json.loads((previous / 'catalog.json').read_text('utf-8'))['cards']
        entries += json.loads((previous / 'pools.json').read_text('utf-8'))['assets']
        for entry in entries:
            path = (previous / entry['image']).resolve()
            if not path.is_relative_to(previous.resolve()) or sha256(path) != entry['sha256']:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def share_unchanged_files(folder, previous):
    """Share immutable bytes only after the new snapshot has finished writing.

    A filesystem without hard links keeps ordinary copies. Neither snapshot's
    contents or manifest hashes change, and future builds write into new files.
    """
    from ournotes.runtime.storage import inside

    boundary = folder.parent.resolve()
    saved = 0
    for target in folder.rglob('*'):
        inside(boundary, target, folder)
        if not target.is_file():
            continue
        source = previous / target.relative_to(folder)
        inside(boundary, source, previous)
        if not source.is_file() or source.stat().st_size != target.stat().st_size:
            continue
        if sha256(source) != sha256(target) or os.path.samefile(source, target):
            continue
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.link')
        try:
            os.link(source, temporary)
            os.replace(temporary, target)
            saved += target.stat().st_size
        except OSError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
    return saved
