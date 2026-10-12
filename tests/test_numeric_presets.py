"""Persistent identities, legacy migration and immutable historical snapshots."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ournotes.cloud.presets import Presets
from ournotes.cloud.store import Conflict, Store, encoded


def preset(name='Example'):
    return {'name': name, 'entries': [{'pool_key': 'example', 'resource': 'stars', 'pulls': 10}]}


def test_zero_primary_key_and_no_reuse_after_delete(tmp_path):
    store = Store(tmp_path / 'cloud.db', 'syntheticpresettestingaccount@gmail.com')
    service = Presets(store, {'presets': []})
    first = service.create(preset(), 1)
    assert first['created_id'] == 0
    assert list(first['presets']) == ['0']
    service.delete('0', first['version'])
    service = Presets(store)
    second = service.create(preset('Second'), service.snapshot()['version'])
    assert second['created_id'] == 1
    with pytest.raises(KeyError):
        service.save('0', preset(), second['version'])
    with store.connection() as db:
        assert next(r for r in db.execute('PRAGMA table_info(shared_presets)') if r['name'] == 'id')['pk'] == 1


def test_legacy_aliases_policy_removed_and_idempotent_migration(tmp_path):
    store = Store(tmp_path / 'cloud.db', 'syntheticpresettestingaccount@gmail.com')
    old = preset('Old policy')
    old['entries'][0].update(until_owned=['member:1'], fallback_pool_key='standard')
    with store.transaction() as db:
        db.execute('CREATE TABLE shared_presets(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('INSERT INTO shared_presets VALUES(?,?)', ('preset-legacy', encoded(old)))
        store.set_meta(db, 'presets_version', 5)
    service = Presets(store)
    value = service.snapshot()
    assert value['version'] == 6 and list(value['presets']) == ['0']
    assert 'until_owned' not in value['presets']['0']['entries'][0]
    assert Presets(store).snapshot() == value
    with store.connection() as db:
        assert dict(db.execute('SELECT key,preset_id FROM preset_aliases').fetchall()) == {
            'preset-legacy': 0, 'Old policy': 0}
    service.delete('0', 6)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM preset_aliases').fetchone()[0] == 2


def test_concurrent_create_keeps_cas_and_counter(tmp_path):
    store = Store(tmp_path / 'cloud.db', 'syntheticpresettestingaccount@gmail.com')
    service = Presets(store, {'presets': []})
    def create(name):
        try:
            return service.create(preset(name), 1)['created_id']
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(create, ['One', 'Two']))
    assert set(outcomes) == {0, 'conflict'}
    assert service.create(preset('Third'), 2)['created_id'] == 1


def test_server_pool_names_and_resource_validation(tmp_path):
    catalog = tmp_path / 'catalog'
    catalog.mkdir()
    (catalog / 'pools.json').write_text(json.dumps({'pools': [{
        'key': 'example', 'name': 'Example pool', 'gacha_id': 1,
        'products': [{'itemType': 12}], 'ticket_name': None}]}))
    service = Presets(Store(tmp_path / 'cloud.db', 'syntheticpresettestingaccount@gmail.com'), {'presets': []}, catalog=catalog)
    assert service.snapshot()['pools'][0]['name'] == 'Example pool'
    value = preset()
    value['entries'][0]['resource'] = 'ticket'
    with pytest.raises(ValueError):
        service.create(value, 1)


def test_ambiguous_legacy_name_and_key_does_not_guess_or_prevent_startup(tmp_path):
    store = Store(tmp_path / 'cloud.db', 'syntheticpresettestingaccount@gmail.com')
    with store.transaction() as db:
        db.execute('CREATE TABLE shared_presets(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('INSERT INTO shared_presets VALUES(?,?)', ('old-a', encoded(preset('old-b'))))
        db.execute('INSERT INTO shared_presets VALUES(?,?)', ('old-b', encoded(preset('0'))))
        store.set_meta(db, 'presets_version', 8)
    service = Presets(store)
    assert list(service.snapshot()['presets']) == ['0', '1']
    with store.connection() as db:
        aliases = dict(db.execute('SELECT key,preset_id FROM preset_aliases').fetchall())
        assert aliases == {'old-a': 0, '0': 1}
        assert store.meta(db, 'presets_numeric_version') == '9'


def test_migration_names_remain_unique_and_numeric_snapshot_version_is_stable(tmp_path):
    store = Store(tmp_path / 'cloud.db', 'syntheticpresettestingaccount@gmail.com')
    old = preset('Example 集齐转池')
    old['entries'][0].update(until_owned=['member:1'], fallback_pool_key='standard')
    service = Presets(store, {'presets': [old, preset('Example 固定招募')]})
    names = [p['name'] for p in service.snapshot()['presets'].values()]
    assert len(names) == len(set(names)) == 2
    service.create(preset('Later'), 1)
    with store.connection() as db:
        assert store.meta(db, 'presets_numeric_version') == '1'
