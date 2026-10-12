"""Offline multi-user administration, immutable activity assignment and explicit mail actions."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_account_browser import folders as build_folders
from test_cloud_service import ADMIN, result
from test_email_rebind_cloud import RULE, Mail, message

from ournotes.cloud.administrators import Administrators, password_record
from ournotes.cloud.api import PREFIX, create_app
from ournotes.cloud.mail import Tickets
from ournotes.cloud.publication import Publisher
from ournotes.cloud.store import Forbidden, Store, Unavailable, encoded
from ournotes.common.identity import MAILBOX

PASSWORD = 'synthetic-administrator-password'


@pytest.fixture
def portal(tmp_path):
    _, catalog = build_folders.__wrapped__(tmp_path)
    now = [1_700_000_000.0]
    store = Store(tmp_path / 'service.sqlite3', MAILBOX, clock=lambda: now[0])
    legacy = store.create_node('legacy')
    store.high_water({'test_next_index': 8, 'formal_next_index': 1 << 20, 'evidence': 'synthetic audit'})
    backend, confirmations = Mail(), []
    app = create_app(store, ADMIN, catalog=catalog, backend=backend, rule=RULE,
                     portal_credential=password_record(PASSWORD),
                     manual_confirmer=lambda p: confirmations.append(p) or True)
    with TestClient(app, base_url='https://testserver') as client:
        assert client.post('/admin/api/login', json={'username': 'Fuyuki', 'password': PASSWORD}).status_code == 200
        yield store, app, client, catalog, now, legacy, backend, confirmations


def allocate(store, identity='legacy', suffix='1'):
    return store.allocate(identity, {'request_id': 'allocate-' + suffix, 'account_id': 'account-' + suffix,
                                     'channel': 'formal'})


def upload(store, allocation, identity=None, **changes):
    request = {'event_id': 'upload-' + allocation['account_id'], 'version': 1,
               'result': {**result(allocation), **changes}}
    return store.upload(identity or allocation['node_id'], allocation['account_id'], request)


def bearer(token):
    return {'Authorization': 'Bearer ' + token}


def create_alice(app):
    app.state.administrators.create('Fuyuki', 'Alice', 'synthetic-alice-password')
    return app.state.administrators.credential('Alice')


def login(client, username='Alice', password='synthetic-alice-password'):
    client.cookies.clear()
    return client.post('/admin/api/login', json={'username': username, 'password': password})


def test_fixed_admin_credential_enables_rebind_without_legacy_allowlist(portal):
    _, app, client, _, _, legacy, _, _ = portal
    alice = create_alice(app)
    assert client.get(PREFIX + '/rebind/capability', headers=bearer(alice['token'])).status_code == 200
    assert client.get(PREFIX + '/rebind/capability', headers=bearer(legacy['token'])).status_code == 403
    app.state.administrators.delete('Fuyuki', 'Alice')
    assert client.get(PREFIX + '/rebind/capability', headers=bearer(alice['token'])).status_code in (401, 403)


def test_only_fuyuki_creates_and_deletes_admins_and_deleted_accounts_remain(portal):
    store, app, client, _, _, _, _, _ = portal
    alice = create_alice(app)
    identity = store.authenticate(alice['token'])
    account = allocate(store, identity, 'alice')
    upload(store, account, identity)
    assert login(client).status_code == 200
    assert client.post('/admin/api/administrators', json={'username': 'Eve', 'password': PASSWORD}).status_code == 403
    assert client.delete('/admin/api/administrators/Fuyuki').status_code == 403
    cookie = client.cookies.get('ournotes_admin_session')
    assert login(client, 'Fuyuki', PASSWORD).status_code == 200
    assert client.delete('/admin/api/administrators/Fuyuki').status_code == 403
    assert client.delete('/admin/api/administrators/Alice').status_code == 200
    assert client.get('/admin/api/accounts?owner_username=Alice').json()['total'] == 1
    with pytest.raises(Forbidden):
        store.authenticate(alice['token'])
    with pytest.raises(Forbidden):
        app.state.administrators.session(cookie)
    assert login(client).status_code == 401
    assert client.post('/admin/api/register', json={}).status_code in (404, 405)


def test_all_admins_query_and_manage_complete_accounts_and_filter_upload_owner(portal):
    store, app, client, _, _, _, _, _ = portal
    legacy_account = allocate(store)
    upload(store, legacy_account)
    incomplete = allocate(store, suffix='incomplete')
    alice = create_alice(app)
    identity = store.authenticate(alice['token'])
    alice_account = allocate(store, identity, 'alice')
    upload(store, alice_account, identity)
    headers = bearer(alice['token'])
    values = client.get(PREFIX + '/accounts', headers=headers).json()['accounts']
    assert {a['owner_username'] for a in values} == {'Fuyuki', 'Alice'}
    assert client.get(PREFIX + '/accounts?owner_username=Alice', headers=headers).json()['accounts'][0]['node_id'] == identity
    assert client.get(PREFIX + f'/results/by-index/{incomplete["index"]}', headers=headers).status_code == 404
    assert client.get('/admin/api/email?index=' + str(incomplete['index'])).status_code == 404
    assert client.get('/admin/api/accounts').json()['total'] == 2
    response = client.post(PREFIX + f'/results/{legacy_account["account_id"]}/usage', headers=headers,
                           json={'request_id': 'alice-used', 'expected_version': 1, 'target_status': 'used'})
    assert response.status_code == 200 and response.json()['usage_status'] == 'used'
    assert client.get(PREFIX + '/art/member/1', headers=headers).status_code == 200
    assert client.get(PREFIX + '/art/member/999', headers=headers).status_code == 404


def test_credential_is_encrypted_recoverable_and_never_public(portal):
    store, _, client, _, _, _, _, _ = portal
    first = client.get('/admin/api/credential').json()
    assert first == client.get('/admin/api/credential').json()
    with store.connection() as db:
        dump = '\n'.join(db.iterdump())
    assert first['token'] not in dump
    assert first == Administrators(store).credential('Fuyuki')
    for path in ('/credential', '/admin/api/register', '/api/cloud/v1/admin/credential'):
        response = client.get(path)
        assert first['token'] not in response.text
    backup = store.backup(store.path.with_name('backup.sqlite3'))
    restored = Store(backup, MAILBOX)
    assert Administrators(restored).credential('Fuyuki') == first


def test_missing_encryption_key_fails_closed_instead_of_replacing_credentials(portal):
    store, app, _, _, _, _, _, _ = portal
    key = app.state.administrators.key_path
    saved = key.read_bytes()
    key.unlink()
    try:
        with pytest.raises(Unavailable, match='key missing'):
            Administrators(store)
        assert not key.exists()
    finally:
        key.write_bytes(saved)


def test_reset_revokes_every_old_access_but_recovers_original_allocation_upload_and_ticket(portal):
    store, app, client, _, _, legacy, _, _ = portal
    old_identity = store.authenticate(legacy['token'])
    account = allocate(store, old_identity)
    tickets = Tickets(store, app.state.manual_mail.backend, RULE)
    request = {'request_id': 'ticket-1', 'account_id': account['account_id'], 'alias': account['alias'], 'timeout': 120}
    ticket = tickets.prepare(old_identity, request)
    previous = client.get('/admin/api/credential').json()
    reset = client.post('/admin/api/credential/reset', json={}).json()
    assert reset['generation'] == previous['generation'] + 1
    for token in (legacy['token'], previous['token']):
        assert client.get(PREFIX + '/health', headers=bearer(token)).status_code == 403
    identity = store.authenticate(reset['token'])
    assert store.allocate(identity, {'request_id': account['request_id'], 'account_id': account['account_id'],
                                     'channel': 'formal'}) == account
    assert tickets.prepare(identity, request)['ticket_id'] == ticket['ticket_id']
    assert tickets.get(identity, ticket['ticket_id'])['node_id'] == 'legacy'
    uploaded = upload(store, account, identity)
    assert uploaded['node_id'] == 'legacy' and uploaded['owner_username'] == 'Fuyuki'
    with store.connection() as db:
        assert db.execute("SELECT node_id FROM receipts WHERE scope='upload'").fetchone()[0] == 'legacy'
    alice = create_alice(app)
    with pytest.raises(Forbidden):
        upload(store, account, store.authenticate(alice['token']))


def test_every_business_query_excludes_invalid_and_unfinished_payloads(portal):
    store, _, client, _, _, legacy, _, _ = portal
    complete = allocate(store)
    upload(store, complete)
    bad = allocate(store, suffix='bad')
    invalid = [encoded({**result(bad), 'status': status}) for status in ('partial', 'failed', 'cancelled')]
    invalid.extend([encoded({**result(bad), 'cards': [{'card': {'kind': 'member', 'id': 99999,
                                                             'rarity': 'SSR', 'name': 'Unknown', 'title': ''},
                                                    'count': 1}]}), '{corrupt'])
    for payload in invalid:
        with store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO results(account_id,node_id,source_version,version,payload) '
                       'VALUES(?,?,1,1,?)', (bad['account_id'], 'legacy', payload))
        headers = bearer(legacy['token'])
        assert len(client.get(PREFIX + '/results', headers=headers).json()['results']) == 1
        assert len(client.get(PREFIX + '/accounts', headers=headers).json()['accounts']) == 1
        assert len(client.get(PREFIX + '/sync?inventory=true', headers=headers).json()['accounts']) == 1
        assert not client.post(PREFIX + '/sync/results', headers=headers, json={'account_ids': [bad['account_id']]}).json()['results']
        assert client.get(PREFIX + f'/results/{bad["account_id"]}', headers=headers).status_code == 404
        assert client.get('/admin/api/accounts').json()['total'] == 1
        assert client.get('/admin/api/email?index=' + str(bad['index'])).status_code == 404
        assert client.get('/admin/api/mail?index=' + str(bad['index'])).status_code == 404


def test_websocket_push_contains_only_invalidations_and_reset_closes_old_connection(portal):
    store, _, client, _, _, _, _, _ = portal
    credential = client.get('/admin/api/credential').json()
    with client.websocket_connect(PREFIX + '/events', headers=bearer(credential['token'])) as ws:
        assert ws.receive_json()['type'] == 'ready'
        account = allocate(store)
        upload(store, account)
        event = ws.receive_json()
        assert set(event) == {'id', 'type', 'revision'} and event['type'] == 'results.changed'
        assert 'email' not in json.dumps(event) and 'owner' not in json.dumps(event)
        client.post('/admin/api/credential/reset', json={})
        with pytest.raises(WebSocketDisconnect) as stopped:
            ws.receive_json()
        assert stopped.value.code == 4403
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(PREFIX + '/events'):
        pass


def test_shared_presets_cas_delete_and_saved_run_snapshot_are_independent(portal):
    _, _, client, _, _, legacy, _, _ = portal
    headers = bearer(legacy['token'])
    initial = client.get(PREFIX + '/presets', headers=headers).json()
    preset = {'name': 'Synthetic cloud preset', 'entries': [{'pool_key': 'bdon-gacha-10', 'resource': 'stars', 'pulls': 12}]}
    saved = client.post(PREFIX + '/presets', headers=headers, json={'expected_version': initial['version'], 'preset': preset})
    path = PREFIX + '/presets/' + str(saved.json()['created_id'])
    assert saved.status_code == 200
    snapshot = saved.json()['presets'][str(saved.json()['created_id'])]
    assert snapshot['total_pulls'] == 12 and snapshot['entries'][0]['batches'] == [10, 1, 1]
    assert client.put(path, headers=headers, json={'expected_version': initial['version'], 'preset': preset}).status_code == 409
    assert client.delete(path + '?expected_version=' + str(initial['version']), headers=headers).status_code == 409
    deleted = client.delete(path + '?expected_version=' + str(saved.json()['version']), headers=headers)
    assert deleted.status_code == 200 and str(saved.json()['created_id']) not in deleted.json()['presets']
    assert snapshot['total_pulls'] == 12  # The previously pinned snapshot is not mutated by CRUD.


def test_activity_is_pinned_at_allocation_not_actual_plan_or_later_active_pool(portal, tmp_path):
    store, app, client, catalog, _, _, _, _ = portal
    pools = tmp_path / 'pools'
    pools.mkdir()
    pool = {'key': 'bdon-gacha-10', 'gacha_id': 10, 'name': '梦限大', 'pickup_card_keys': ['member:1', 'support:1', 'member:1']}
    (pools / 'pools.json').write_text(json.dumps({'pools': [pool]}), 'utf-8')
    app.state.activities.import_catalog(pools)
    version = app.state.activities.snapshot()['version']
    assert client.post('/admin/api/activities/active', json={'activity_id': 'bdon-gacha-10', 'expected_version': version}).status_code == 200
    account = allocate(store)
    assert account['activity_id'] == 'bdon-gacha-10'
    version = app.state.activities.snapshot()['version']
    app.state.activities.activate(None, version)
    value = upload(store, account, pool_keys=['bdon-gacha-12'], activity_id='pretend', activity=None)
    assert value['activity_id'] == 'bdon-gacha-10'
    projected = client.get('/admin/api/accounts?index=' + str(account['index'])).json()['accounts'][0]['projection']
    assert projected['activity'] == {'id': 'bdon-gacha-10', 'label': '梦限大 一期', 'up_count': 1, 'up_total': 2}
    inactive = allocate(store, suffix='inactive')
    upload(store, inactive, pool_keys=['bdon-gacha-10'])
    assert client.get('/admin/api/accounts?index=' + str(inactive['index'])).json()['accounts'][0]['projection']['activity'] is None
    publisher = Publisher(store, tmp_path / 'publication', catalog)
    publisher.publish_pending()
    pointer = json.loads(publisher.resource('data/current.json').read_text('utf-8'))
    public = json.loads(publisher.resource('data/snapshot.' + pointer['revision'] + '.json').read_text('utf-8'))
    assert public['accounts'][0]['activity']['up_total'] == 2
    assert all(key not in encoded(public) for key in ('owner_username', account['alias'], 'node_id', 'preset_snapshot'))


def test_historical_activity_backfill_does_not_invent_unknown_plan_or_change_node(tmp_path):
    from ournotes.cloud.activities import Activities

    store = Store(tmp_path / 'history.sqlite3', MAILBOX)
    store.create_node('history-node')
    store.high_water({'test_next_index': 8, 'formal_next_index': 1 << 20, 'evidence': 'synthetic audit'})
    values = []
    for suffix, keys in [('known', ['bdon-gacha-12']), ('unknown', []), ('ambiguous', ['bdon-gacha-10', 'bdon-gacha-12'])]:
        allocation = allocate(store, 'history-node', suffix)
        with store.transaction() as db:
            db.execute('UPDATE allocations SET activity_payload=NULL WHERE account_id=?', (allocation['account_id'],))
        upload(store, allocation, pool_keys=keys)
        values.append(allocation)
    Activities(store)
    assert store.results(values[0]['account_id'])['activity']['label'] == 'millsage 一期'
    assert store.results(values[0]['account_id'])['node_id'] == 'history-node'
    for value in values[1:]:
        assert 'activity_id' not in store.results(value['account_id'])['result']


def test_manual_mail_latest_per_purpose_is_read_only_and_expired_links_stay_disabled(portal):
    store, _, client, _, now, _, backend, confirmations = portal
    allocation = allocate(store)
    upload(store, allocation)
    backend.messages.update({'login-old': message(allocation['alias'], now[0] - 90, 'login'),
                             'login-new': message(allocation['alias'], now[0] - 30, 'login'),
                             'identity': message(allocation['alias'], now[0] - 50),
                             'foreign': message('someone@gmail.com', now[0] - 1),
                             'old-link': message(allocation['alias'], now[0] - 700)})
    response = client.get('/admin/api/mail?index=' + str(allocation['index'])).json()
    assert [v['message_id'] for v in response['messages']] == ['login-new', 'identity']
    assert response['messages'][1]['can_execute'] is True and confirmations == []
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM consumed').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM manual_mail_actions').fetchone()[0] == 0
    del backend.messages['identity']
    old = client.get('/admin/api/mail?index=' + str(allocation['index']) + '&minutes=1440').json()['messages'][1]
    assert old['message_id'] == 'old-link' and old['can_execute'] is False
    assert client.post('/admin/api/mail/' + str(allocation['index']) + '/execute',
                       json={'message_id': 'old-link', 'request_id': 'old-execute'}).status_code == 409
    assert confirmations == []


def test_manual_confirmation_rechecks_latest_identity_mail_before_execution(portal):
    store, _, client, _, now, _, backend, confirmations = portal
    account = allocate(store)
    upload(store, account)
    backend.messages['older'] = message(account['alias'], now[0] - 30)
    assert client.get('/admin/api/mail?index=' + str(account['index'])).json()['messages'][0]['message_id'] == 'older'
    backend.messages['latest'] = message(account['alias'], now[0] - 10)
    path = '/admin/api/mail/' + str(account['index']) + '/execute'
    assert client.post(path, json={'message_id': 'older', 'request_id': 'stale-button'}).status_code == 409
    assert confirmations == []
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_mail_actions').fetchone()[0] == 0
    assert client.post(path, json={'message_id': 'latest', 'request_id': 'latest-button'}).json()['status'] == 'done'
    assert len(confirmations) == 1


def test_manual_confirmation_is_independently_deduplicated_without_using_automatic_tickets(portal):
    store, app, client, _, now, _, backend, confirmations = portal
    account = allocate(store)
    upload(store, account)
    backend.messages['verify'] = message(account['alias'], now[0] - 20)
    # An automatic claim does not interlock or prevent an explicit administrator action.
    with store.transaction() as db:
        db.execute('INSERT INTO consumed VALUES(?,?,?)', ('verify', 'automatic-ticket', now[0]))
        before = list(map(tuple, db.execute('SELECT * FROM consumed')))
    path = '/admin/api/mail/' + str(account['index']) + '/execute'
    assert client.post(path, json={'message_id': 'verify', 'request_id': 'manual1'}).json()['status'] == 'done'
    assert client.post(path, json={'message_id': 'verify', 'request_id': 'manual1'}).json()['replayed'] is True
    assert client.post(path, json={'message_id': 'verify', 'request_id': 'manual2'}).json()['replayed'] is True
    assert len(confirmations) == 1 and store.results(account['account_id'])['usage_status'] == 'available'
    with store.connection() as db:
        assert list(map(tuple, db.execute('SELECT * FROM consumed'))) == before
        assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0] == 0
    assert app.state.manual_mail.read(account['index'])['messages'][0]['can_execute'] is False


def test_concurrent_manual_confirmations_and_unknown_outcome_never_replay(portal):
    store, app, _, _, now, _, backend, confirmations = portal
    account = allocate(store)
    upload(store, account)
    backend.messages['verify'] = message(account['alias'], now[0] - 20)

    def unknown(params):
        confirmations.append(params)
        raise Unavailable('synthetic response loss')

    app.state.manual_mail.confirmer = unknown
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda n: app.state.manual_mail.execute('Fuyuki', account['index'], 'verify',
                                                                         'request-' + str(n)), range(4)))
    assert len(confirmations) == 1 and all(value['status'] == 'uncertain' for value in responses)
    assert app.state.manual_mail.execute('Fuyuki', account['index'], 'verify', 'request-later')['replayed'] is True


def test_website_gmail_authorization_is_health_only_and_machine_maintenance_is_separate(portal):
    _, _, client, _, _, legacy, _, _ = portal
    assert client.get('/admin/api/gmail/health').status_code == 200
    assert client.post('/admin/api/gmail/authorize', json={}).status_code in (404, 405)
    assert client.post('/admin/api/oauth', json={}).status_code in (404, 405)
    assert client.post(PREFIX + '/admin/oauth', json={}).status_code == 403
    assert client.post(PREFIX + '/admin/oauth', headers=bearer(legacy['token']), json={}).status_code == 403


def test_successful_upload_automatically_publishes_and_a_failed_publish_retries_after_restart(portal, tmp_path, monkeypatch):
    from ournotes.cloud import publication

    store, _, _, catalog, _, legacy, _, _ = portal
    publisher = Publisher(store, tmp_path / 'automatic-publication', catalog)
    app = create_app(store, ADMIN, catalog=catalog, publisher=publisher)
    account = allocate(store)
    real_export = publication.export_site

    def unavailable(*args, **kwargs):
        raise OSError('synthetic disk temporarily unavailable')

    monkeypatch.setattr(publication, 'export_site', unavailable)
    with TestClient(app) as transport:
        response = transport.post(PREFIX + '/results', headers=bearer(legacy['token']),
                                  json={'event_id': 'automatic-result', 'version': 1, 'result': result(account)})
    assert response.status_code == 200 and response.json()['publication']['pending'] is True
    assert publisher.current() is None and store.result_by_index(account['index'])['result']['index'] == account['index']
    monkeypatch.setattr(publication, 'export_site', real_export)
    restarted = Publisher(Store(store.path, MAILBOX), publisher.root, catalog)
    assert restarted.publish_pending()['pending'] is False
    assert restarted.current()['response']['accounts'] == 1
    with store.connection() as db:
        assert store.meta(db, 'publication_pending_revision') is None


def test_upload_during_publication_keeps_newer_durable_revision_and_retries_whole_snapshot(portal, tmp_path, monkeypatch):
    from ournotes.cloud import publication

    store, _, _, catalog, _, _, _, _ = portal
    first = allocate(store)
    upload(store, first)
    second = allocate(store, suffix='2')
    publisher = Publisher(store, tmp_path / 'racing-publication', catalog)
    real_export, added = publication.export_site, []

    def concurrent_export(*args, **kwargs):
        exported = real_export(*args, **kwargs)
        if not added:
            upload(store, second)
            added.append(True)
        return exported

    monkeypatch.setattr(publication, 'export_site', concurrent_export)
    from ournotes.cloud.store import Conflict
    with pytest.raises(Conflict):
        publisher.publish_pending()
    assert publisher.current() is None
    with store.connection() as db:
        assert store.meta(db, 'publication_pending_revision') == '2'
    assert publisher.publish_pending()['accounts'] == 2
    assert publisher.current()['response']['revision'] == 2


def test_preset_snapshot_is_private_and_preserves_run_version(portal):
    store, _, client, _, _, _, _, _ = portal
    account = allocate(store)
    plan = [{'pool_key': 'bdon-gacha-12', 'resource': 'stars', 'pulls': 10}]
    snapshot = {'key': 'former-preset', 'version': 7, 'plan': plan}
    upload(store, account, preset_snapshot=snapshot)
    value = client.get('/admin/api/accounts?index=' + str(account['index'])).json()['accounts'][0]
    assert value['result']['preset_snapshot'] == snapshot
    assert 'preset_snapshot' not in value['projection']
