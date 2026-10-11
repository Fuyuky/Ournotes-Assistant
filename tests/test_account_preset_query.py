"""Cloud-only account details, including immutable historical preset metadata."""
import copy
import hashlib

import pytest
from test_launcher_web import setup as launcher_setup

from ournotes.integrations.cloud_client import CloudError

setup_fixture = pytest.fixture(name='setup')(launcher_setup.__wrapped__)


@pytest.fixture
def account_cloud(setup, monkeypatch):
    calls = []
    item = {'version': 5, 'usage_status': 'booked', 'owner_username': 'another-admin',
            'activity': {'id': 'event-1', 'name': '活动一'},
            'result': {'account_id': 'remote-id', 'index': 3416, 'email': 'owner@example.test',
                       'used': True, 'pull_count': 43, 'pool_keys': ['bdon-gacha-12'],
                       'preset_snapshot': {'name': '历史计划', 'total_pulls': 62,
                                           'entries': [{'pool_key': 'bdon-gacha-12', 'resource': 'stars', 'pulls': 62}]},
                       'cards': [{'card': {'kind': 'member', 'id': 1, 'rarity': 'SSR', 'name': '角色', 'title': '卡面'}, 'count': 2},
                                 {'card': {'kind': 'member', 'id': 1, 'rarity': 'SSR', 'name': '角色', 'title': '卡面'}, 'count': 1},
                                 {'card': {'kind': 'support', 'id': 1, 'rarity': 'SSR', 'name': '支援', 'title': '卡面'}, 'count': 3}]}}
    class Cloud:
        def get_result_by_index(self, index):
            calls.append(index)
            return copy.deepcopy(item)
        def card_art(self, kind, identity):
            return b'cloud-image', 'image/png'
    monkeypatch.setattr('ournotes.integrations.cloud_client.CloudClient.from_root', lambda _: Cloud())
    return *setup, item, calls


def test_query_can_manage_other_admin_without_local_archive_or_runtime_identity(account_cloud):
    client, _, root, spawned, _, _item, calls = account_cloud
    before = hashlib.sha256((root / 'state/runtime.sqlite3').read_bytes()).digest()
    response = client.get('/api/launcher/accounts/3416')
    assert response.status_code == 200
    value = response.json()
    assert value['owner_username'] == 'another-admin' and value['usage_status'] == 'booked'
    assert value['historical_preset'] == {'name': '历史计划', 'planned_pulls': 62, 'status': 'recorded'}
    assert [(c['key'], c['count']) for c in value['cards']] == [('member:1', 3), ('support:1', 3)]
    assert value['ssr_total'] == 6 and value['activity']['name'] == '活动一'
    assert client.get(value['cards'][0]['image']).content == b'cloud-image'
    assert calls == [3416] and not spawned
    assert not list((root / 'results/archive/accounts').glob('3416*'))
    assert hashlib.sha256((root / 'state/runtime.sqlite3').read_bytes()).digest() == before
    assert 'account_id' not in value and 'game_uid' not in value


def test_query_email_uses_cloud_and_never_reconstructs_local_result(account_cloud):
    client, _, root, _, _, _, calls = account_cloud
    response = client.post('/api/launcher/email', json={'index': 3416})
    assert response.json() == {'index': 3416, 'email': 'owner@example.test'}
    assert calls == [3416] and not list((root / 'results/archive/accounts').glob('3416*'))


def test_cloud_failure_does_not_fall_back_to_existing_local_archive(setup, monkeypatch):
    client, _, root, _, _ = setup
    class Cloud:
        def get_result_by_index(self, index):
            raise CloudError('offline', '云端暂不可达。')
    monkeypatch.setattr('ournotes.integrations.cloud_client.CloudClient.from_root', lambda _: Cloud())
    existing = next((root / 'results/archive/accounts').glob('1 *')).read_bytes()
    assert client.get('/api/launcher/accounts/1').status_code == 409
    assert client.post('/api/launcher/email', json={'index': 1}).status_code == 409
    assert next((root / 'results/archive/accounts').glob('1 *')).read_bytes() == existing


@pytest.mark.parametrize('invalid', ['-1', str(2**53), 'true', '1.5'])
def test_invalid_index_rejected_before_cloud_request(account_cloud, invalid):
    client, _, _, _, _, _, calls = account_cloud
    assert client.get('/api/launcher/accounts/' + invalid).status_code == 422
    assert calls == []


def test_wrong_cloud_identity_is_rejected(account_cloud):
    client, _, _, _, _, item, _ = account_cloud
    item['result']['index'] = 1
    assert client.get('/api/launcher/accounts/3416').status_code == 409
