import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ournotes.catalogs.cards import TABLES, _rows
from ournotes.catalogs.runtime import EXTRA_TABLES, asset_url, compile_pools, current_catalog
from ournotes.runtime.draw_strategy import validate_strategy
from ournotes.runtime.pools import PoolCatalog, confirm_identity


def test_unsupported_rarity_only_blocks_plans_using_its_pool():
    folder = current_catalog()
    data = json.loads((folder / 'catalog.json').read_text(encoding='utf-8'))
    tables = {name: _rows(folder / 'raw' / f'{name}.json') for name in TABLES + EXTRA_TABLES}
    tables['MasterMemberCard'].append({'id': 99999, 'rarity': 20})
    future = deepcopy(tables['MasterGacha'][0])
    future.update(id=99999, lotGroupId=99999)
    tables['MasterGacha'].append(future)
    tables['MasterGachaLot'].append({'lotGroupId': 99999, 'prizeGroupId': 99999})
    tables['MasterGachaPrize'].append({'groupId': 99999, 'resourceType': 2, 'resourceId': 99999})
    pools = {p['key']: p for p in compile_pools(tables, data['cards'])}
    catalog = SimpleNamespace(get=pools.get)
    supported = 'launch-mygo-pickup-20260831'
    assert pools['bdon-gacha-99999']['unsupported_card_keys'] == ['member:99999']
    validate_strategy([{'pool_key': supported}], catalog)
    with pytest.raises(ValueError, match='尚不支持'):
        validate_strategy([{'pool_key': 'bdon-gacha-99999'}], catalog)
    with pytest.raises(ValueError, match='尚不支持'):
        validate_strategy([{'pool_key': supported, 'until_owned': pools[supported]['pickup_card_keys'],
                            'fallback_pool_key': 'bdon-gacha-99999'}], catalog)
    with pytest.raises(ValueError, match='不在固定目录'):
        validate_strategy([{'pool_key': 'missing'}], catalog)


def test_new_pool_prizes_resolve_to_new_cards_and_stable_legacy_keys():
    folder = current_catalog()
    data = json.loads((folder / 'catalog.json').read_text(encoding='utf-8'))
    tables = {name: _rows(folder / 'raw' / f'{name}.json') for name in TABLES + EXTRA_TABLES}
    pools = compile_pools(tables, data['cards'])
    new = next(p for p in pools if p['gacha_id'] == 10)
    assert new['key'] == 'bdon-gacha-10'
    assert new['name'] == '由我主演的赛博之夜招募'
    assert {'member:61', 'member:62', 'support:62', 'support:63'} <= set(new['card_keys'])
    # Other new cards can be event rewards; the catalog must include them even
    # though this pool's prize table does not contain them.
    assert {'member:63', 'support:64'} <= {c['key'] for c in data['cards']}
    assert next(p for p in pools if p['gacha_id'] == 1)['key'] == 'launch-mygo-pickup-20260831'
    with pytest.raises(ValueError, match='缺失卡牌'):
        compile_pools(tables, [c for c in data['cards'] if c['key'] != 'member:61'])


def test_names_are_metadata_driven_and_ambiguous_names_are_rejected():
    catalog = PoolCatalog()
    assert confirm_identity('由我主演的赛博之夜招募 消耗2000个星钻', 'bdon-gacha-10', catalog)
    assert not confirm_identity('开服纪念！MyGO!!!!! Pick Up招募', 'bdon-gacha-10', catalog)
    assert not confirm_identity('由我主演的赛博之夜招募 交织的乐章招募', 'bdon-gacha-10', catalog)


def test_hk_october_pool_and_same_number_card_kinds_stay_distinct():
    catalog = PoolCatalog()
    pool = catalog.get('bdon-gacha-12')
    assert pool['name'] == '光之所至，伊始之时招募'
    assert set(pool['pickup_card_keys']) == {'member:65', 'member:66', 'support:65', 'support:66'}
    assert not pool['unsupported_card_keys']
    assert confirm_identity(pool['name'] + ' 消耗2000个星钻', pool['key'], catalog)
    assert not confirm_identity('由我主演的赛博之夜招募', pool['key'], catalog)
    data = json.loads((catalog.directory / 'catalog.json').read_text('utf-8'))
    assert data['masterdata']['region'] == 'hk-tw-mo'
    cards = {c['key']: c for c in data['cards']}
    assert cards['member:65']['title'] == '划破黎明'
    assert cards['support:65']['title'] == '赤橙之中'


def test_snapshot_pointer_and_asset_paths_fail_closed(tmp_path):
    (tmp_path / 'current.json').write_text(json.dumps({'directory': '../escape'}))
    with pytest.raises(ValueError, match='越界'):
        current_catalog(tmp_path)
    for key in ('../other', '/tmp/asset', 'https://other/asset'):
        with pytest.raises(ValueError):
            asset_url(key)


def test_failed_refresh_keeps_previous_pointer_and_assets(tmp_path, monkeypatch):
    import shutil

    from ournotes.catalogs.cards import sha256
    from ournotes.catalogs.runtime import sync_catalog
    folder = tmp_path / 'snapshots' / 'existing'
    folder.mkdir(parents=True)
    names = ('catalog.json', 'pools.json')
    for name in names:
        shutil.copy2(current_catalog() / name, folder / name)
    pointer = json.dumps({'directory': 'snapshots/existing', 'checked_at': 0,
                          'sha256': {name: sha256(folder / name) for name in names}})
    (tmp_path / 'current.json').write_text(pointer)
    def unavailable(*args, **kwargs):
        raise OSError('offline')
    monkeypatch.setattr('ournotes.catalogs.runtime.download_tables', unavailable)
    with pytest.raises(OSError, match='offline'):
        sync_catalog(tmp_path, max_age=0)
    assert (tmp_path / 'current.json').read_text() == pointer
    assert current_catalog(tmp_path) == folder
    assert list((tmp_path / 'snapshots').iterdir()) == [folder]


def test_identical_refresh_reuses_existing_snapshot(tmp_path):
    from ournotes.catalogs.runtime import sync_catalog
    from ournotes.common.files import atomic_json, file_hash
    previous = tmp_path/'snapshots'/'previous'
    for name in TABLES + EXTRA_TABLES:
        atomic_json(previous/'raw'/f'{name}.json', [])
    atomic_json(previous/'catalog.json', {'cards': []})
    atomic_json(previous/'pools.json', {'assets': []})
    atomic_json(tmp_path/'current.json', {'directory': 'snapshots/previous', 'checked_at': 0,
                'sha256': {n:file_hash(previous/n) for n in ('catalog.json', 'pools.json')}})
    assert sync_catalog(tmp_path, max_age=0, seed_raw=previous/'raw') == previous
    assert [p.name for p in (tmp_path/'snapshots').iterdir()] == ['previous']


def test_snapshot_sharing_preserves_distinct_versions_and_rejects_corrupt_assets(tmp_path):
    import os

    from ournotes.catalogs.runtime import share_unchanged_files, unchanged_snapshot
    from ournotes.common.files import atomic_json, file_hash
    old, new = tmp_path/'old', tmp_path/'new'
    for folder in (old, new):
        for name in TABLES + EXTRA_TABLES:
            atomic_json(folder/'raw'/f'{name}.json', [])
        (folder/'image.webp').write_bytes(b'immutable-image')
        atomic_json(folder/'catalog.json', {'cards': [{'image': 'image.webp', 'sha256': file_hash(folder/'image.webp')}]})
        atomic_json(folder/'pools.json', {'assets': []})
    assert unchanged_snapshot(old, new)
    (old/'image.webp').write_bytes(b'corrupted')
    assert not unchanged_snapshot(old, new)
    (old/'image.webp').write_bytes(b'immutable-image')
    (new/'raw'/f'{TABLES[0]}.json').write_text('[1]', encoding='utf8')
    assert not unchanged_snapshot(old, new)
    assert share_unchanged_files(new, old) > 0
    assert os.path.samefile(old/'image.webp', new/'image.webp')
    assert not os.path.samefile(old/'raw'/f'{TABLES[0]}.json', new/'raw'/f'{TABLES[0]}.json')
    assert (old/'raw'/f'{TABLES[0]}.json').read_text().strip() == '[]'
    assert share_unchanged_files(new, old) == 0
