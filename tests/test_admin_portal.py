"""Private email lookup and statistics cannot be accessed through public/node credentials."""
import json

import pytest
from fastapi.testclient import TestClient
from test_account_browser import folders as build_folders

from ournotes.cloud.admin_portal import COOKIE, SESSION_SECONDS, password_record
from ournotes.cloud.api import PREFIX, create_app
from ournotes.cloud.publication import Publisher
from ournotes.cloud.store import Store
from ournotes.common.identity import MAILBOX

PASSWORD = 'synthetic-test-password-only'


@pytest.fixture
def portal(tmp_path):
    _, catalog = build_folders.__wrapped__(tmp_path)
    now = [1000.0]
    store = Store(tmp_path / 'cloud.sqlite3', MAILBOX, clock=lambda: now[0])
    node = store.create_node('node')
    store.high_water({'test_next_index': 8, 'formal_next_index': 1 << 20, 'evidence': 'synthetic namespace'})
    credential = password_record(PASSWORD)
    app = create_app(store, 'synthetic-administrator-token-1234567890', catalog=catalog,
                     portal_credential=credential, publisher=Publisher(store, tmp_path / 'publications', catalog))
    with TestClient(app, base_url='https://testserver') as client:
        yield store, client, now, credential, catalog, node


def login(client, password=PASSWORD, username='Fuyuki'):
    return client.post('/admin/api/login', json={'username': username, 'password': password})


def test_used_statistics_include_booked_without_exposing_private_state(portal):
    from test_cloud_service import result
    store, client, _, _, _, _ = portal
    allocation = store.allocate('node', {'account_id': 'booked-account', 'request_id': 'book-allocation',
                                        'channel': 'formal'})
    store.upload('node', allocation['account_id'], {'event_id': 'book-result', 'version': 1,
                                                   'result': result(allocation)})
    store.usage('node', allocation['account_id'], {'request_id': 'book', 'expected_version': 1,
                                                  'target_status': 'booked'})
    assert login(client).status_code == 200
    response = client.get('/admin/api/statistics?scope=used')
    assert response.status_code == 200 and response.json()['n'] == 1
    assert 'usage_status' not in response.text


def test_announcement_authenticated_edit_conflict_and_persistence(portal, tmp_path):
    store, client, _, _, catalog, _ = portal
    path = '/admin/api/announcement'
    assert client.get(path).status_code == 401
    assert login(client).status_code == 200
    original = client.get(path).json()
    body = {'content': '测试公告 <script>plain text</script>', 'expected_version': original['version']}
    assert client.post(path, json=body, headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert client.post(path, json={**body, 'content': 'x' * 5001}).status_code == 422
    response = client.post(path, json=body)
    assert response.status_code == 200
    assert response.json()['content'] == body['content']
    assert client.post(path, json=body).status_code == 409
    publisher = Publisher(store, tmp_path / 'publications', catalog)
    assert publisher.announcement()['content'] == body['content']
    assert response.json()['publication']['pending'] is False
    from test_cloud_publication import snapshot
    assert snapshot(publisher)['announcement']['content'] == body['content']
    version = client.get(path).json()['version']
    assert client.post(path, json={'content': '', 'expected_version': version}).status_code == 200
    assert snapshot(publisher)['announcement']['content'] == ''


def test_announcement_retry_survives_restart_without_manual_publication(portal, tmp_path, monkeypatch):
    from test_cloud_publication import snapshot
    store, client, _, _, catalog, _ = portal
    assert login(client).status_code == 200
    path = '/admin/api/announcement'
    version = client.get(path).json()['version']

    def interrupted():
        raise OSError('synthetic interrupted publication')

    monkeypatch.setattr(client.app.state.publisher, 'publish_pending', interrupted)
    response = client.post(path, json={'content': '持久化重试公告', 'expected_version': version})
    assert response.status_code == 200 and response.json()['publication']['pending'] is True
    restarted = Publisher(Store(store.path, MAILBOX), tmp_path / 'publications', catalog)
    assert restarted.announcement()['content'] == '持久化重试公告'
    assert restarted.publish_pending()['pending'] is False
    assert snapshot(restarted)['announcement']['content'] == '持久化重试公告'


def test_fixed_account_session_cookie_and_expiry_persist_across_restart(portal):
    store, client, now, credential, catalog, _ = portal
    assert client.get('/admin/api/session').status_code == 401
    assert login(client, 'incorrect').status_code == 401
    assert login(client, username='new-user').status_code == 401
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    for item in ('HttpOnly', 'Secure', 'SameSite=strict', 'Path=/admin', f'Max-Age={SESSION_SECONDS}'):
        assert item in cookie
    token = client.cookies.get(COOKIE)
    with store.connection() as db:
        assert token not in json.dumps([tuple(r) for r in db.execute('SELECT * FROM portal_sessions')])
    restarted = create_app(Store(store.path, MAILBOX, clock=lambda: now[0]),
                           'synthetic-administrator-token-1234567890', catalog=catalog,
                           portal_credential=credential)
    with TestClient(restarted, base_url='https://testserver') as other:
        other.cookies.set(COOKIE, token, path='/admin')
        now[0] += SESSION_SECONDS - 1
        assert other.get('/admin/api/session').status_code == 200
        now[0] += 1
        assert other.get('/admin/api/session').status_code == 401


def test_private_queries_require_cookie_and_logout_revokes_it(portal):
    store, client, _, _, _, node = portal
    for path in ('/admin/api/email?index=8', '/admin/api/statistics?scope=all', '/admin/api/statistics?scope=used'):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={'Authorization': 'Bearer ' + node['token']}).status_code == 401
    assert client.post('/admin/api/register', json={}).status_code in (404, 405)
    assert login(client).status_code == 200
    assert client.get('/admin/api/email?index=8').status_code == 404
    assert client.post('/admin/api/logout', json={}).status_code == 200
    assert client.get('/admin/api/session').status_code == 401
    assert client.get('/admin').status_code == 200
    assert client.get('/admin').headers['cache-control'] == 'no-store'
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM portal_sessions').fetchone()[0] == 0


def test_login_origin_rate_limit_and_validation_do_not_echo_password(portal):
    _, client, _, _, _, _ = portal
    assert client.post('/admin/api/login', json={'username': 'admin', 'password': PASSWORD},
                       headers={'Origin': 'https://untrusted.example'}).status_code == 403
    invalid = client.post('/admin/api/login', json={'username': 'admin', 'password': PASSWORD, 'extra': PASSWORD})
    assert invalid.status_code == 422 and PASSWORD not in invalid.text
    for _ in range(10):
        assert login(client, 'incorrect').status_code == 401
    assert login(client).status_code == 429


def test_email_uses_allocations_and_four_tables_use_valid_cloud_results(portal):
    store, client, _, _, _, _ = portal
    values = []
    for n in range(3):
        values.append(store.allocate('node', {'request_id': f'allocate{n}', 'account_id': f'account{n}', 'channel': 'test'}))
    # Duplicate copies count as one character; same ID support is independent.
    for n, holdings in enumerate(([(1, 2)], [(1, 1), (2, 1)])):
        a = values[n]
        cards = [{'card': {'kind': 'member', 'id': i, 'rarity': 'SSR', 'name': 'Synthetic', 'title': 'Card'},
                  'count': count} for i, count in holdings]
        cards.append({'card': {'kind': 'support', 'id': 1, 'rarity': 'SSR', 'name': 'Synthetic', 'title': 'Card'}, 'count': 1})
        result = {'schema_version': 1, 'account_id': a['account_id'], 'index': a['index'], 'email': a['alias'],
                  'cards': cards, 'totals': {'member': {'SSR': 2, 'SR': 0, 'R': 40},
                    'support': {'SSR': 1, 'SR': 0, 'R': 0}}, 'pull_count': 43, 'draw_labels': ['43'], 'used': False}
        store.upload('node', a['account_id'], {'event_id': f'upload{n}', 'version': 1, 'result': result})
    store.used('node', values[1]['account_id'], {'used': True, 'expected_version': 1, 'request_id': 'used1'})
    assert login(client).status_code == 200
    assert client.get('/admin/api/email?index=10').status_code == 404
    assert client.get('/admin/api/email?index=8').json() == {'index': 8, 'email': values[0]['alias']}
    all_rows = client.get('/admin/api/statistics?scope=all').json()
    used_rows = client.get('/admin/api/statistics?scope=used').json()
    assert (all_rows['n'], used_rows['n']) == (2, 1)
    assert len(all_rows['metrics']) == len(used_rows['metrics']) == 4
    metrics = {m['key']: m for m in all_rows['metrics']}
    assert metrics['ssr_total']['max'] == 3
    assert metrics['unique_characters']['max'] == 2
    assert metrics['mygo_characters']['max'] == metrics['mujica_characters']['max'] == 1
    for report in (all_rows, used_rows):
        for metric in report['metrics']:
            assert sum(b['count'] for b in metric['histogram']) == report['n']
        assert 'email' not in json.dumps(report)
    # The session grants no machine administration rights.
    assert client.post(PREFIX + '/admin/nodes', json={'node_id': 'attacker'}).status_code == 403


def test_empty_scope_is_four_empty_tables(portal):
    _, client, _, _, _, _ = portal
    assert login(client).status_code == 200
    value = client.get('/admin/api/statistics?scope=used').json()
    assert value['n'] == 0 and len(value['metrics']) == 4
    assert all(m['max'] is None and m['histogram'] == [] for m in value['metrics'])
