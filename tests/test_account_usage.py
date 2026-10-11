"""Three-state usage, public folding and product isolation with synthetic data."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_account_browser import folders as build_folders
from test_account_browser import record, write
from test_cloud_integration import archive
from test_launcher_web import setup as launcher_fixture

from ournotes.accounts.browser import create_app
from ournotes.accounts.site import export_site
from ournotes.accounts.used import preview, reconcile_pending, set_usage
from ournotes.cloud.store import Store
from ournotes.common.files import atomic_json
from ournotes.common.identity import MAILBOX
from ournotes.integrations.cloud_client import CloudClient, CloudError
from ournotes.integrations.cloud_sync import CloudSync

pytest_plugins = ['test_cloud_integration']
web_setup = pytest.fixture(name='web_setup')(launcher_fixture.__wrapped__)


@pytest.fixture
def usage(integration, monkeypatch):
    store, client, _, root, _ = integration
    value = archive(client.allocate('usage-allocation', 'usage-account', 'formal'))
    value['pull_count'] = 43
    path = root / 'results/archive/accounts' / f"{value['alias_index']} 1 SSR.json"
    atomic_json(path, value)
    sync = CloudSync(root)
    sync.enqueue(value)
    sync.flush(client)
    monkeypatch.setattr(CloudClient, 'from_root', lambda _: client)
    return store, client, root, path, value


def change(usage, target):
    _, _, root, _, value = usage
    return set_usage(root, value['alias_index'], preview(root, value['alias_index'])['token'], target)


def test_book_cancel_terminal_and_old_interface_protection(usage):
    _, client, _, _, _ = usage
    assert change(usage, 'booked')['usage_status'] == 'booked'
    assert client.get_result('usage-account')['usage_status'] == 'booked'
    assert change(usage, 'available')['used'] is False
    change(usage, 'booked')
    final = change(usage, 'used')
    assert client.get_result('usage-account')['usage_status'] == 'used'
    for target in ('booked', 'available'):
        with pytest.raises(ValueError, match='不可撤销'):
            change(usage, target)
        with pytest.raises(CloudError) as failure:
            client.set_usage('usage-account', target, final['version'])
        assert failure.value.status == 409
    with pytest.raises(CloudError) as failure:
        client.set_used('usage-account', False, final['version'])
    assert failure.value.status == 409


def test_booking_receipts_and_retransmission_do_not_finalize(usage):
    store, client, _, _, _ = usage
    request = {'request_id': 'book', 'expected_version': 1, 'target_status': 'booked'}
    booked = store.usage('node1', 'usage-account', request)
    assert store.usage('node1', 'usage-account', request) == booked
    assert client.usage_receipt('usage-account', 'book')['usage_status'] == 'booked'
    result = booked['result']
    result['used'] = False
    store.upload('node1', 'usage-account', {'event_id': 'same-source', 'version': 1, 'result': result})
    result['used'] = True
    store.upload('node1', 'usage-account', {'event_id': 'next-source', 'version': 20, 'result': result})
    assert client.get_result('usage-account')['usage_status'] == 'booked'
    current = client.get_result('usage-account')
    client.set_usage('usage-account', 'available', current['version'])
    state = client.sync_manifest()['states'][0]
    assert state['usage_status'] == 'available' and not state['result']['used']


def test_old_database_migration_preserves_terminal_and_is_repeatable(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE results(account_id TEXT PRIMARY KEY,node_id TEXT,source_version INTEGER, '
                   'version INTEGER,payload TEXT)')
        for i, used in enumerate((False, True)):
            db.execute('INSERT INTO results VALUES (?,?,1,1,?)',
                       (str(i), 'node', json.dumps({'index': i, 'used': used})))
    store = Store(path, MAILBOX)
    assert store.results()['results'] == []  # Incomplete legacy payloads stay private.
    with store.connection() as db:
        assert [r[0] for r in db.execute('SELECT usage_status FROM results ORDER BY account_id')] == ['available', 'used']
        db.execute("UPDATE results SET usage_status='booked',payload=? WHERE account_id='0'",
                   (json.dumps({'index': 0, 'used': True}),))
        db.commit()
    with Store(path, MAILBOX).connection() as db:
        assert db.execute("SELECT usage_status FROM results WHERE account_id='0'").fetchone()[0] == 'booked'


def test_cancel_competes_with_terminal_and_can_never_clear_it(usage):
    _, client, _, _, _ = usage
    booked = change(usage, 'booked')
    def attempt(target):
        try:
            return client.set_usage('usage-account', target, booked['version'])
        except CloudError as error:
            assert error.status == 409
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(attempt, ('available', 'used')))
    current = client.get_result('usage-account')
    if current['usage_status'] != 'used':
        client.set_usage('usage-account', 'used', current['version'])
    with pytest.raises(CloudError):
        client.set_usage('usage-account', 'available', client.get_result('usage-account')['version'])


@pytest.mark.parametrize('target', ['booked', 'used'])
@pytest.mark.parametrize('http_status', [None, 503])
def test_lost_response_confirmed_by_real_state_and_receipt(usage, monkeypatch, target, http_status):
    _, client, root, path, _ = usage
    original = client.set_usage
    def lost(*args):
        original(*args)
        raise CloudError('unavailable', 'synthetic response loss', http_status)
    monkeypatch.setattr(client, 'set_usage', lost)
    assert change(usage, target)['usage_status'] == target
    assert client.get_result('usage-account')['usage_status'] == target
    assert 'usage_status' not in json.loads(path.read_text('utf-8'))
    assert json.loads((root / 'state/account-usage-pending.json').read_text()) == {}


def test_cancel_does_not_clear_unconfirmed_terminal_or_rebind_guard(usage, monkeypatch):
    _, client, root, path, value = usage
    change(usage, 'booked')
    original = client.set_usage
    def uncertain(account, target, *args):
        if target == 'used':
            raise CloudError('unavailable', 'synthetic request uncertainty')
        return original(account, target, *args)
    monkeypatch.setattr(client, 'set_usage', uncertain)
    with pytest.raises(CloudError, match='尚未确认'):
        change(usage, 'used')
    atomic_json(root / 'state/email-rebind-outcomes.json',
                {str(value['alias_index']): {'account_id': value['account_id'], 'state': 'used_pending'}})
    saved = json.loads(path.read_text())
    saved['rebind_used_pending'] = True
    atomic_json(path, saved)
    change(usage, 'available')
    assert client.get_result('usage-account')['usage_status'] == 'available'
    pending = json.loads((root / 'state/account-usage-pending.json').read_text())
    assert any(item['target_status'] == 'used' for item in pending.values())
    monkeypatch.setattr(client, 'set_usage', original)
    reconcile_pending(root, client)
    assert client.get_result('usage-account')['usage_status'] == 'used'
    assert json.loads((root / 'state/account-usage-pending.json').read_text()) == {}
    assert json.loads((root / 'state/email-rebind-outcomes.json').read_text()) == {}


def test_service_unavailable_without_receipt_keeps_terminal_intent(usage, monkeypatch):
    _, client, root, _, _ = usage
    def unavailable(*args):
        raise CloudError('unavailable', 'synthetic service unavailability', 503)
    monkeypatch.setattr(client, 'set_usage', unavailable)
    with pytest.raises(CloudError, match='尚未确认'):
        change(usage, 'used')
    pending = json.loads((root / 'state/account-usage-pending.json').read_text())
    assert any(item['target_status'] == 'used' for item in pending.values())


def test_newer_booked_projection_cannot_confirm_terminal(usage, monkeypatch):
    _, client, _, _, _ = usage
    change(usage, 'booked')
    original = client.set_usage
    def competing(account, target, version, request_id):
        original(account, 'booked', version, 'competing-book')
        raise CloudError('unavailable', 'synthetic response loss')
    monkeypatch.setattr(client, 'set_usage', competing)
    with pytest.raises(CloudError) as error:
        change(usage, 'used')
    assert error.value.status == 409
    assert client.get_result('usage-account')['usage_status'] == 'booked'






def test_missing_protocol_disables_mutations_without_optimistic_write(usage, monkeypatch):
    _, client, _, path, _ = usage
    monkeypatch.setattr(client, 'usage_capability', lambda: {'usage_protocol': 1})
    preview(usage[2], usage[4]['alias_index'])
    before = path.read_bytes()
    with pytest.raises(ValueError, match='升级'):
        change(usage, 'booked')
    assert path.read_bytes() == before


def test_local_status_api_authentication_cloud_only_account_and_terminal_protection(usage, web_setup, monkeypatch):
    _, cloud, _, _, value = usage
    client, _, root, calls, _ = web_setup
    monkeypatch.setattr(CloudClient, 'from_root', lambda _: cloud)
    folder = root / 'results/archive/accounts'
    before = {p.name: p.read_bytes() for p in folder.glob('*.json')}
    body = {'index': value['alias_index']}
    assert client.post('/api/launcher/accounts/usage-preview', json=body,
                       headers={'X-Launcher-Token': 'wrong'}).status_code == 403
    plan = client.post('/api/launcher/accounts/usage-preview', json=body).json()
    assert plan['usage_status'] == 'available'
    target = {**body, 'token': plan['token'], 'target_status': 'booked'}
    reply = client.post('/api/launcher/accounts/set-usage', json=target)
    assert reply.status_code == 200 and reply.json()['usage_status'] == 'booked'
    assert client.post('/api/launcher/accounts/set-usage', json=target).status_code == 409
    assert {p.name: p.read_bytes() for p in folder.glob('*.json')} == before and not calls


def test_card_browser_and_static_export_fold_without_leaking_status(tmp_path):
    accounts, catalog = build_folders.__wrapped__(tmp_path)
    write(accounts, record(1, used=True, usage_status='booked'))
    write(accounts, record(2, used=True, usage_status='used'))
    write(accounts, record(3, used=False, usage_status='available'))
    write(accounts, record(4, used=False, usage_status=None))
    with TestClient(create_app(accounts, catalog)) as client:
        assert client.get('/api/accounts').json()['total'] == 1
        data = client.get('/api/accounts?include_used=true').json()
        assert data['meta']['used_accounts'] == 2
        assert client.get('/api/accounts?index=1').json()['accounts'][0]['used']
        assert 'usage_status' not in json.dumps(data)
    out = tmp_path / 'site'
    exported = export_site(accounts, catalog, out)
    data = json.loads((out / f"data/snapshot.{exported['revision']}.json").read_text())
    assert data['meta']['used_accounts'] == 2
    assert 'usage_status' not in json.dumps(data)


