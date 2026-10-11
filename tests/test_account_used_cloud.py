import json

import pytest
from test_cloud_integration import archive

pytest_plugins = ["test_cloud_integration"]

from ournotes.accounts.used import preview, set_used
from ournotes.common.files import atomic_json
from ournotes.integrations.cloud_client import CloudClient, CloudError
from ournotes.integrations.cloud_sync import CloudSync


def test_online_used_does_not_update_or_create_local_archives(integration, monkeypatch):
    _, client, _, root, _ = integration
    value = archive(client.allocate('request-1', 'account-1', 'formal'))
    path = root / 'results/archive/accounts' / f"{value['alias_index']} 1 SSR.json"
    atomic_json(path, value)
    sync = CloudSync(root)
    sync.enqueue(value)
    sync.flush(client)
    monkeypatch.setattr(CloudClient, 'from_root', lambda _: client)
    plan = preview(root, value['alias_index'])
    set_used(root, value['alias_index'], plan['token'], used=True)
    assert path.exists() and not json.loads(path.read_text('utf-8'))['used']
    assert client.get_result('account-1')['usage_status'] == 'used'
    with pytest.raises(ValueError, match='不可撤销'):
        set_used(root, value['alias_index'], plan['token'], used=False)
    path.unlink()
    plan = preview(root, value['alias_index'])
    with pytest.raises(ValueError, match='不可撤销'):
        set_used(root, value['alias_index'], plan['token'], used=False)
    assert not path.exists()


def test_offline_does_not_mutate_local_marker(integration, monkeypatch):
    _, client, _, root, _ = integration
    value = archive(client.allocate('request-2', 'account-2', 'formal'))
    path = root / 'results/archive/accounts' / f"{value['alias_index']} 1 SSR.json"
    atomic_json(path, value)
    def unavailable(_):
        raise CloudError('unavailable', 'synthetic offline')
    monkeypatch.setattr(CloudClient, 'from_root', unavailable)
    with pytest.raises(CloudError):
        set_used(root, value['alias_index'], 'unused-token', used=True)
    assert json.loads(path.read_text('utf-8'))['used'] is False




def test_used_lookup_fetches_only_target_result_and_keeps_cloud_only_accounts(integration, monkeypatch):
    _, client, _, root, _ = integration
    values = [archive(client.allocate(f'request-{n}', f'account-{n}', 'formal')) for n in range(2)]
    sync = CloudSync(root)
    for value in values:
        sync.enqueue(value)
    sync.flush(client)
    calls = []
    request = client.request
    def tracked(method, path, body=None):
        calls.append((method, path))
        assert path != '/results', 'A single-account operation must never fetch all results'
        return request(method, path, body)
    monkeypatch.setattr(client, 'request', tracked)
    monkeypatch.setattr(CloudClient, 'from_root', lambda _: client)
    index = values[0]['alias_index']
    plan = preview(root, index)
    set_used(root, index, plan['token'])
    assert calls == [('GET', f'/results/by-index/{index}'), ('GET', '/usage/capability'),
                     ('GET', f'/results/by-index/{index}'), ('POST', '/results/account-0/usage')]
    assert not (root / 'results/archive/accounts').exists()
    assert client.get_result('account-1')['result']['used'] is False


def test_exact_index_api_is_authenticated_shared_and_uses_database_indexes(integration, monkeypatch):
    store, client, _, root, transport = integration
    value = archive(client.allocate('lookup-request', 'lookup-account', 'formal'))
    sync = CloudSync(root)
    sync.enqueue(value)
    sync.flush(client)
    index = value['alias_index']
    other = store.create_node('other-node')
    url = f'/api/cloud/v1/results/by-index/{index}'
    assert transport.get(url).status_code == 403
    def forbidden_full_list(*args):
        raise AssertionError('Exact lookup must not call the all-results query')
    monkeypatch.setattr(store, 'results', forbidden_full_list)
    response = transport.get(url, headers={'Authorization': 'Bearer ' + other['token']})
    assert response.status_code == 200
    assert response.json()['result']['account_id'] == 'lookup-account'
    assert 'results' not in response.json()
    with store.connection() as db:
        plan = db.execute('EXPLAIN QUERY PLAN SELECT r.* FROM allocations a JOIN results r '
                          'ON r.account_id=a.account_id WHERE a.index_no=?', (index,)).fetchall()
    descriptions = [row[3] for row in plan]
    assert sum('SEARCH' in text for text in descriptions) == 2
    assert not any('SCAN' in text for text in descriptions)


@pytest.mark.parametrize('index', [-1, True, 1.5, '1', 2**53])
def test_usage_rejects_invalid_index_before_any_request(integration, monkeypatch, index):
    _, client, _, root, _ = integration
    monkeypatch.setattr(CloudClient, 'from_root', lambda _: client)
    def forbidden_request(*args):
        raise AssertionError('Invalid index must not make a cloud request')
    monkeypatch.setattr(client, 'request', forbidden_request)
    with pytest.raises(ValueError, match='编号'):
        preview(root, index)
    with pytest.raises(ValueError, match='编号'):
        client.get_result_by_index(index)


def test_index_api_missing_and_invalid_parameters_do_not_echo_private_data(integration):
    _, client, _, _, transport = integration
    headers = {'Authorization': 'Bearer ' + client.credential}
    assert transport.get('/api/cloud/v1/results/by-index/0', headers=headers).status_code == 404
    for invalid in ['-1', '1.5', 'abc', str(2**53)]:
        response = transport.get('/api/cloud/v1/results/by-index/' + invalid, headers=headers)
        assert response.status_code == 422
        assert response.json() == {'error': 'invalid_request'}


def test_usage_confirmation_retains_cloud_cas_when_changed_after_lookup(integration, monkeypatch):
    _, client, _, root, _ = integration
    value = archive(client.allocate('race-request', 'race-account', 'formal'))
    path = root / 'results/archive/accounts' / f"{value['alias_index']} 1 SSR.json"
    atomic_json(path, value)
    sync = CloudSync(root)
    sync.enqueue(value)
    sync.flush(client)
    monkeypatch.setattr(CloudClient, 'from_root', lambda _: client)
    plan = preview(root, value['alias_index'])
    original = client.set_usage
    def changed(account, used, version, request_id=None):
        original(account, 'booked', version, 'competing-marker')
        return original(account, used, version, 'stale-marker')
    monkeypatch.setattr(client, 'set_usage', changed)
    with pytest.raises(CloudError) as error:
        set_used(root, value['alias_index'], plan['token'])
    assert error.value.status == 409
    assert client.get_result('race-account')['usage_status'] == 'booked'
    assert json.loads(path.read_text('utf-8'))['used'] is False


@pytest.fixture
def legacy_used(integration, monkeypatch):
    _, client, _, root, _ = integration
    value = archive(client.allocate('legacy-request', 'legacy-account', 'formal'))
    path = root / 'results/archive/accounts' / f"{value['alias_index']} 1 SSR.json"
    atomic_json(path, value)
    sync = CloudSync(root)
    sync.enqueue(value)
    sync.flush(client)
    calls = []
    request = client.request

    def legacy(method, url, body=None):
        calls.append((method, url))
        assert url != '/results', 'Legacy compatibility must never download all results'
        if url.startswith('/results/by-index/'):
            raise CloudError('http_error', 'Synthetic missing index route', 404)
        return request(method, url, body)

    monkeypatch.setattr(client, 'request', legacy)
    monkeypatch.setattr(CloudClient, 'from_root', lambda _: client)
    return client, root, value, path, calls




@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'unreadable', 'wrong_index', 'bool_index', 'path_id'])
def test_legacy_lookup_rejects_ambiguous_or_bad_local_identity(legacy_used, bad):
    _, root, value, path, calls = legacy_used
    if bad == 'missing':
        path.unlink()
    elif bad == 'duplicate':
        atomic_json(path.with_name(f"{value['alias_index']} duplicate.json"), value)
    elif bad == 'unreadable':
        path.write_text('{', 'utf-8')
    else:
        field, replacement = {'wrong_index': ('alias_index', value['alias_index'] + 1),
                              'bool_index': ('alias_index', True), 'path_id': ('account_id', '../private')}[bad]
        atomic_json(path, {**value, field: replacement})
    with pytest.raises(ValueError):
        preview(root, value['alias_index'])
    assert calls == [('GET', f"/results/by-index/{value['alias_index']}")]




@pytest.mark.parametrize('status', [403, 409, 503])
def test_legacy_fallback_does_not_hide_auth_conflict_or_service_failure(legacy_used, monkeypatch, status):
    client, root, value, _, calls = legacy_used

    def unavailable(_):
        raise CloudError('http_error', 'Synthetic service failure', status)

    monkeypatch.setattr(client, 'get_result_by_index', unavailable)
    with pytest.raises(CloudError) as error:
        preview(root, value['alias_index'])
    assert error.value.status == status and not calls


