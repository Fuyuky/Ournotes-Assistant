"""Compact SQL pagination preserves complete-result visibility and one-snapshot totals."""
from contextlib import contextmanager

import pytest
from test_admin_portal import login
from test_admin_portal import portal as portal_fixture
from test_cloud_service import result

from ournotes.cloud.api import PREFIX
from ournotes.cloud.store import Store, encoded
from ournotes.common.identity import alias_at


@pytest.fixture
def portal(tmp_path):
    yield from portal_fixture.__wrapped__(tmp_path)


def seed_account(store, suffix, *, preset_key=None, preset_version=4, node='node'):
    allocation = store.allocate(node, {'request_id': f'page-allocate-{suffix}',
                                       'account_id': f'page-account-{suffix}', 'channel': 'formal'})
    value = result(allocation)
    if preset_key is not None:
        value['preset_snapshot'] = {'key': preset_key, 'version': preset_version,
                                    'plan': [{'pool_key': 'bdon-gacha-12', 'resource': 'stars', 'pulls': 10}]}
    store.upload(node, allocation['account_id'], {'event_id': f'page-upload-{suffix}', 'version': 1,
                                                 'result': value})
    return allocation


def add_alice_node(store):
    store.create_node('alice-node')
    with store.transaction() as db:
        db.execute("UPDATE nodes SET owner_username='Alice' WHERE id='alice-node'")


def test_default_twenty_rows_count_offset_order_and_full_index_detail(portal):
    store, client, _, _, _, _ = portal
    allocations = [seed_account(store, str(n), preset_key='0') for n in range(45)]
    store.allocate('node', {'request_id': 'unfinished', 'account_id': 'unfinished', 'channel': 'formal'})
    assert login(client).status_code == 200
    first = client.get('/admin/api/accounts').json()
    assert (first['total'], first['offset'], first['limit']) == (45, 0, 20)
    assert [item['result']['index'] for item in first['accounts']] == [a['index'] for a in allocations[:20]]
    summary = first['accounts'][0]
    assert set(summary['result']) == {'account_id', 'index', 'used'}
    assert set(summary['projection']) == {'ssr_total', 'activity'}
    assert summary['projection']['ssr_total'] == 1
    assert not {'email', 'cards', 'preset_snapshot'} & set(summary['result'])
    assert 'ssr_cards' not in summary
    for offset in (20, 40, 60):
        value = client.get('/admin/api/accounts', params={'offset': offset}).json()
        assert (value['total'], value['offset']) == (45, offset)
        assert [item['result']['index'] for item in value['accounts']] == [a['index'] for a in allocations[offset:offset + 20]]
    assert client.get('/admin/api/accounts', params={'offset': 2**64}).json()['accounts'] == []
    detail = client.get('/admin/api/accounts', params={'index': allocations[0]['index']}).json()
    assert detail['total'] == 1
    complete = detail['accounts'][0]
    assert complete['result']['email'] == allocations[0]['alias']
    assert complete['result']['cards'] and complete['ssr_cards']
    assert complete['result']['preset_snapshot']['key'] == '0'
    assert complete['historical_preset']['id'] == '0'
    assert complete['projection']['ssr_total'] == summary['projection']['ssr_total']


def test_owner_preset_alias_unknown_deleted_filters_and_option_counts(portal):
    store, client, _, _, _, _ = portal
    with store.transaction() as db:
        db.executemany('INSERT INTO preset_aliases VALUES(?,1)', [('曾用名称',), ('old-hashed-key',)])
    allocations = [seed_account(store, str(n), preset_key=key)
                   for n, key in enumerate(('0', '1', '曾用名称', 'old-hashed-key', '501', None, 'unmatched'))]
    add_alice_node(store)
    alice = seed_account(store, 'alice', preset_key='1', node='alice-node')
    assert login(client).status_code == 200
    source = client.get('/admin/api/accounts', params={'owner_username': 'Fuyuki', 'preset_id': '1',
                                                      'offset': 1, 'limit': 2}).json()
    assert source['total'] == 3 and len(source['accounts']) == 2
    options = {item['id']: item for item in source['preset_options']}
    assert {key: item['count'] for key, item in options.items()} == {'0': 1, '1': 3, '501': 1, '__unknown__': 2}
    assert options['501']['deleted'] and not options['1']['deleted']
    historical = client.get('/admin/api/accounts', params={'index': allocations[2]['index']}).json()['accounts'][0]
    assert historical['result']['preset_snapshot']['key'] == '曾用名称'
    assert historical['historical_preset'] == {'id': '1', 'name': options['1']['name'], 'deleted': False, 'version': 4}
    unknown = client.get('/admin/api/accounts', params={'owner_username': 'Fuyuki', 'preset_id': '__unknown__'}).json()
    assert unknown['total'] == 2
    assert [a['result']['index'] for a in unknown['accounts']] == [a['index'] for a in allocations[-2:]]
    owner = client.get('/admin/api/accounts', params={'owner_username': 'Alice'}).json()
    assert owner['total'] == 1 and owner['preset_options'][0]['count'] == 1
    assert owner['accounts'][0]['result']['index'] == alice['index']
    assert client.get('/admin/api/accounts', params={'index': alice['index'], 'owner_username': 'Fuyuki'}).json()['total'] == 0
    assert client.get('/admin/api/accounts', params={'index': allocations[0]['index'], 'preset_id': '1'}).json()['total'] == 0
    with store.transaction() as db:
        db.execute('DELETE FROM shared_presets WHERE id=1')
    deleted = client.get('/admin/api/accounts', params={'preset_id': '1'}).json()
    assert deleted['total'] == 4
    assert next(p for p in deleted['preset_options'] if p['id'] == '1')['deleted']
    assert client.get('/admin/api/accounts', params={'owner_username': 'Missing'}).json()['preset_options'] == []


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'offset=-1', 'index=-1', 'preset_id=-1',
                                   'preset_id=01', 'preset_id=old-hashed-key', 'preset_id=10000000000000000'])
def test_invalid_pagination_and_preset_filters_are_rejected(portal, query):
    _, client, _, _, _, _ = portal
    assert login(client).status_code == 200
    assert client.get('/admin/api/accounts?' + query).status_code == 422


def test_account_list_requires_session_and_cannot_use_a_node_token(portal):
    _, client, _, _, _, node = portal
    assert client.get('/admin/api/accounts').status_code == 401
    assert client.get('/admin/api/accounts', headers={'Authorization': 'Bearer ' + node['token']}).status_code == 401


def test_warm_list_reads_only_compact_metadata_and_uses_sql_limit(portal, monkeypatch):
    store, client, _, _, _, _ = portal
    for n in range(65):
        seed_account(store, str(n), preset_key='0')
    assert login(client).status_code == 200
    assert client.get('/admin/api/accounts').json()['total'] == 65
    statements = []
    real_connection = store.connection

    @contextmanager
    def traced_connection():
        with real_connection() as db:
            db.set_trace_callback(statements.append)
            yield db

    def no_full_validation(*args, **kwargs):
        pytest.fail('A warm list must not parse/validate full result payloads')

    monkeypatch.setattr(store, 'connection', traced_connection)
    monkeypatch.setattr(store, 'results', no_full_validation)
    monkeypatch.setattr(store, 'final', no_full_validation)
    monkeypatch.setattr(store._admin_result_validator, 'account', no_full_validation)
    source = client.get('/admin/api/accounts?offset=20').json()
    assert source['total'] == 65 and len(source['accounts']) == 20
    assert any('LIMIT 20 OFFSET 20' in statement for statement in statements)
    assert not any('r.*' in statement or 'r.payload' in statement or 'BEGIN IMMEDIATE' in statement
                   for statement in statements)


@pytest.mark.parametrize('bad', ['partial', 'failed', 'cancelled', 'corrupt', 'unknown-card', 'wrong-identity'])
def test_payload_mutation_invalidates_visibility_count_and_preset_options(portal, bad):
    store, client, _, _, _, _ = portal
    allocation = seed_account(store, 'valid', preset_key='0')
    assert login(client).status_code == 200
    assert client.get('/admin/api/accounts').json()['total'] == 1
    valid = store.results(allocation['account_id'])['result']
    invalid = dict(valid)
    if bad in ('partial', 'failed', 'cancelled'):
        invalid['status'] = bad
    elif bad == 'unknown-card':
        invalid['cards'] = [{'card': {**valid['cards'][0]['card'], 'id': 99999}, 'count': 1}]
    elif bad == 'wrong-identity':
        invalid['account_id'] = 'other'
    payload = '{corrupt' if bad == 'corrupt' else encoded(invalid)
    with store.transaction() as db:
        db.execute('UPDATE results SET payload=? WHERE account_id=?', (payload, allocation['account_id']))
    hidden = client.get('/admin/api/accounts').json()
    assert hidden['total'] == 0 and hidden['accounts'] == hidden['preset_options'] == []
    assert client.get('/admin/api/accounts', params={'index': allocation['index']}).json()['total'] == 0
    with store.transaction() as db:
        db.execute('UPDATE results SET payload=? WHERE account_id=?', (encoded(valid), allocation['account_id']))
    assert client.get('/admin/api/accounts').json()['total'] == 1


def test_upload_usage_and_delete_refresh_only_changed_results(portal, monkeypatch):
    store, client, _, _, _, _ = portal
    first, second = seed_account(store, 'first', preset_key='0'), seed_account(store, 'second', preset_key='1')
    assert login(client).status_code == 200
    assert client.get('/admin/api/accounts').json()['total'] == 2
    validated = []
    original = store._admin_result_validator.account

    def record_validation(value):
        validated.append(value['account_id'])
        return original(value)

    monkeypatch.setattr(store._admin_result_validator, 'account', record_validation)
    value = result(first)
    value['cards'][0]['count'] = value['totals']['member']['SSR'] = 2
    value['pull_count'] = 44
    store.upload('node', first['account_id'], {'event_id': 'changed-result', 'version': 2, 'result': value})
    source = client.get('/admin/api/accounts').json()
    assert source['accounts'][0]['projection']['ssr_total'] == 2 and validated == [first['account_id']]
    assert source['accounts'][0]['version'] == 2
    validated.clear()
    store.usage_admin('Fuyuki', second['account_id'], {'request_id': 'book-second', 'expected_version': 1,
                                                     'target_status': 'booked'})
    source = client.get('/admin/api/accounts').json()
    assert source['accounts'][1]['result']['used'] is True and source['accounts'][1]['usage_status'] == 'booked'
    assert validated == [second['account_id']]
    validated.clear()
    with store.transaction() as db:
        db.execute('DELETE FROM results WHERE account_id=?', (first['account_id'],))
    source = client.get('/admin/api/accounts').json()
    assert source['total'] == 1 and source['accounts'][0]['result']['account_id'] == second['account_id']
    assert validated == []


def test_restart_keeps_validated_cache_and_catalog_change_revalidates(portal, monkeypatch):
    store, client, _, _, _, _ = portal
    allocation = seed_account(store, 'first')
    assert login(client).status_code == 200
    assert client.get('/admin/api/accounts').json()['total'] == 1
    validator = store._admin_result_validator
    validated = []
    original = validator.account

    def record_validation(value):
        validated.append(value['account_id'])
        return original(value)

    monkeypatch.setattr(validator, 'account', record_validation)
    restarted = Store(store.path, store.mailbox)
    restarted.prepare_admin_results(validator)
    assert validated == [] and restarted.admin_result_page()['total'] == 1
    monkeypatch.setitem(validator.definitions, 'member:1', 'SR')
    restarted.prepare_admin_results(validator)
    assert validated == [allocation['account_id']] and restarted.admin_result_page()['total'] == 0


def test_count_rows_usage_options_and_revision_share_a_snapshot_during_upload(portal, monkeypatch):
    store, client, _, _, _, _ = portal
    first = seed_account(store, 'first', preset_key='0')
    second = store.allocate('node', {'request_id': 'second', 'account_id': 'second', 'channel': 'formal'})
    assert login(client).status_code == 200
    before = client.get('/admin/api/accounts').json()
    real_connection, changed = store.connection, []

    class InterleavedConnection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, parameters=()):
            cursor = self.db.execute(sql, parameters)
            if sql.startswith('SELECT COUNT(*) ') and not changed:
                changed.append(True)
                store.usage_admin('Fuyuki', first['account_id'], {'request_id': 'concurrent-used',
                                  'expected_version': 1, 'target_status': 'used'})
                store.upload('node', second['account_id'], {'event_id': 'concurrent-upload', 'version': 1,
                                                            'result': result(second)})
            return cursor

    @contextmanager
    def interleaved_connection():
        with real_connection() as db:
            yield InterleavedConnection(db)

    monkeypatch.setattr(store, 'connection', interleaved_connection)
    during = client.get('/admin/api/accounts').json()
    assert changed and during == before
    monkeypatch.setattr(store, 'connection', real_connection)
    after = client.get('/admin/api/accounts').json()
    assert after['total'] == 2 and after['revision'] == before['revision'] + 2
    assert after['accounts'][0]['result']['used'] is True
    assert sum(item['count'] for item in after['preset_options']) == 2


def test_client_index_lookup_validates_only_the_requested_result(portal, monkeypatch):
    store, client, _, _, _, node = portal
    first, second = seed_account(store, 'first'), seed_account(store, 'second')
    validated = []
    original = store.final

    def record_validation(row):
        validated.append(row['account_id'])
        return original(row)

    monkeypatch.setattr(store, 'final', record_validation)
    response = client.get(PREFIX + '/accounts', params={'index': second['index']},
                          headers={'Authorization': 'Bearer ' + node['token']})
    assert response.status_code == 200
    assert response.json()['accounts'][0]['result']['email'] == second['alias']
    assert validated == [second['account_id']] and first['account_id'] not in validated


def test_historical_import_is_visible_after_transactional_index_refresh(portal):
    store, client, _, _, _, _ = portal
    assert login(client).status_code == 200
    assert client.get('/admin/api/accounts').json()['total'] == 0
    store.high_water({'test_next_index': 100, 'formal_next_index': 1 << 20, 'evidence': 'synthetic history'})
    allocation = {'account_id': 'historical', 'index': 12, 'alias': alias_at(12, store.mailbox)}
    store.import_historical({'request_id': 'historical-import', 'node_id': 'node', 'channel': 'test',
                             'result': result(allocation)})
    source = client.get('/admin/api/accounts').json()
    assert source['total'] == 1 and source['accounts'][0]['result']['index'] == 12


def test_numeric_historical_name_uses_alias_until_numeric_identity_cutover(portal):
    store, client, _, _, _, _ = portal
    with store.transaction() as db:
        store.set_meta(db, 'presets_numeric_version', 5)
        db.execute("INSERT INTO preset_aliases VALUES('0',1)")
    old = seed_account(store, 'old-name', preset_key='0', preset_version=4)
    no_version = seed_account(store, 'no-version', preset_key='0', preset_version=None)
    new = seed_account(store, 'new-id', preset_key='0', preset_version=5)
    no_alias = seed_account(store, 'unmatched-id', preset_key='501', preset_version=4)
    deleted = seed_account(store, 'deleted-id', preset_key='501', preset_version=5)
    assert login(client).status_code == 200
    first = client.get('/admin/api/accounts', params={'preset_id': '1'}).json()
    assert first['total'] == 2
    assert [a['result']['index'] for a in first['accounts']] == [old['index'], no_version['index']]
    assert client.get('/admin/api/accounts', params={'preset_id': '0'}).json()['accounts'][0]['result']['index'] == new['index']
    unknown = client.get('/admin/api/accounts', params={'preset_id': '__unknown__'}).json()
    assert unknown['total'] == 1 and unknown['accounts'][0]['result']['index'] == no_alias['index']
    detail = client.get('/admin/api/accounts', params={'index': old['index']}).json()['accounts'][0]
    assert detail['historical_preset']['id'] == '1' and detail['result']['preset_snapshot']['key'] == '0'
    removed = client.get('/admin/api/accounts', params={'index': deleted['index']}).json()['accounts'][0]
    assert removed['historical_preset']['id'] == '501' and removed['historical_preset']['deleted']
