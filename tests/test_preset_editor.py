"""Synthetic cloud CAS and saved local runtime plans; no business operations."""
import copy
import hashlib
import json
import sqlite3

import pytest
from test_launcher_web import setup as launcher_setup

from ournotes.common.files import atomic_json
from ournotes.integrations.cloud_client import CloudError
from ournotes.runtime.presets import canonical_preset, expand_preset

setup_fixture = pytest.fixture(name='setup')(launcher_setup.__wrapped__)


@pytest.fixture
def editor(setup, monkeypatch):
    client, service, root, calls, processes = setup
    preset = canonical_preset({'name': '测试预设', 'description': '说明', 'entries': [
        {'pool_key': 'pickup', 'resource': 'stars', 'pulls': 20},
        {'pool_key': 'standard', 'resource': 'ticket', 'pulls': 10}]})
    other = canonical_preset({'name': '另一个预设', 'entries': [
        {'pool_key': 'standard', 'resource': 'ticket', 'pulls': 1}]})
    snapshot = {'version': 7, 'presets': {'0': preset, '1': other}}
    writes = []
    class Cloud:
        def presets(self):
            return copy.deepcopy(snapshot)
        def save_preset(self, key, value, expected_version):
            if expected_version != snapshot['version']:
                raise CloudError('version_conflict', '共享版本已变化。')
            writes.append(('save', key, expected_version))
            snapshot['presets'][key] = canonical_preset(value)
            snapshot['version'] += 1
            return copy.deepcopy(snapshot)
        def create_preset(self, value, expected_version):
            return self.save_preset('2', value, expected_version)
        def delete_preset(self, key, expected_version):
            if expected_version != snapshot['version']:
                raise CloudError('version_conflict', '共享版本已变化。')
            writes.append(('delete', key, expected_version))
            del snapshot['presets'][key]
            snapshot['version'] += 1
            return copy.deepcopy(snapshot)
    monkeypatch.setattr('ournotes.integrations.cloud_client.CloudClient.from_root', lambda _: Cloud())
    service._presets = None
    service._preset_checked_at = 0
    service._preset_snapshot = None
    folder = root / 'data/catalog/snapshots/test'
    products = [{'drawCount': n, 'itemType': 12, 'price': 200*n} for n in (1, 10)]
    atomic_json(folder / 'pools.json', {'pools': [
        {'key': 'pickup', 'name': '测试UP', 'gacha_id': 1, 'ticket_name': '星钻券', 'products': products,
         'pickup_card_keys': ['member:61', 'support:63'], 'unsupported_card_keys': []},
        {'key': 'standard', 'name': '常驻', 'gacha_id': 2, 'ticket_name': '常驻券', 'products': products,
         'pickup_card_keys': [], 'unsupported_card_keys': []}]})
    atomic_json(folder / 'catalog.json', {'cards': [
        {'key': 'member:61', 'kind': 'member', 'name': '角色', 'rarity': 'SSR'},
        {'key': 'support:63', 'kind': 'support', 'name': '支援', 'rarity': 'SSR'}]})
    atomic_json(root / 'data/catalog/current.json', {
        'directory': 'snapshots/test', 'sha256': {name: hashlib.sha256((folder/name).read_bytes()).hexdigest()
                                               for name in ('pools.json', 'catalog.json')}})
    with sqlite3.connect(root / 'state/runtime.sqlite3') as db:
        run = json.loads(db.execute('SELECT data FROM runs').fetchone()[0])
        run.update(preset='测试预设', status='running')
        db.execute('UPDATE runs SET data=?', (json.dumps(run),))
        db.execute('ALTER TABLE accounts ADD COLUMN data TEXT')
        account = {'id': 'a2', 'run_id': 'r1', 'batch_id': 'b'*32, 'preset': '测试预设', 'alias_index': 2,
                   'phase': 'draws', 'plan': expand_preset(preset), 'game_uid': 'private-uid-sentinel',
                   'alias': 'private-sentinel@example.test'}
        db.execute("UPDATE accounts SET data=? WHERE id='a2'", (json.dumps(account),))
        db.execute('CREATE TABLE draws(account_id TEXT,ordinal INTEGER,status TEXT)')
        db.execute("INSERT INTO draws VALUES('a2',0,'complete')")
    return client, service, root, calls, processes, snapshot, writes


def payload(client):
    data = client.get('/api/launcher/presets').json()
    preset = data['presets'][0]
    return {'key': preset['key'], 'name': preset['name'], 'description': preset['description'],
            'expected_version': data['version'], 'entries': [{k:v for k,v in e.items() if k != 'batches'} for e in preset['entries']]}


def checksums(root):
    return {name: hashlib.sha256((root/name).read_bytes()).digest()
            for name in ('state/runtime.sqlite3', 'config/presets.json')}


def test_read_shared_presets_and_fixed_active_plan_without_writes(editor):
    client, _, root, calls, _, _, writes = editor
    before = checksums(root)
    data = client.get('/api/launcher/presets').json()
    assert data['editable'] and data['version'] == 7
    assert data['presets'][0]['key'] == '0'
    assert data['presets'][0]['total_pulls'] == 30 and data['presets'][0]['validation_errors'] == []
    assert data['saved']['plans'][0]['matches_preset']
    assert 'private-sentinel' not in json.dumps(data) and 'game_uid' not in json.dumps(data)
    assert checksums(root) == before and not calls and not writes


def test_cloud_edit_during_active_batch_does_not_rewrite_saved_plan(editor):
    client, service, root, calls, _, snapshot, writes = editor
    value = payload(client)
    before = checksums(root)
    value['entries'][0]['pulls'] = 23
    response = client.post('/api/launcher/presets', json=value)
    assert response.status_code == 200
    data = response.json()
    assert data['version'] == 8 and data['presets'][0]['total_pulls'] == 33
    assert not data['saved']['plans'][0]['matches_preset']
    assert writes == [('save', '0', 7)] and snapshot['presets']['0']['total_pulls'] == 33
    assert service.presets[0]['total_pulls'] == 33
    assert checksums(root) == before and not (root/'state/preset-history').exists() and not calls
    assert client.post('/api/launcher/presets', json=value).status_code == 409
    assert len(writes) == 1


def test_create_and_delete_global_preset_by_stable_key(editor):
    client, _, root, calls, _, snapshot, writes = editor
    before = checksums(root)
    value = payload(client)
    value.update(operation='create', name='新增预设')
    value.pop('key')
    response = client.post('/api/launcher/presets', json=value)
    assert response.status_code == 200
    new = next(p for p in response.json()['presets'] if p['name'] == '新增预设')
    assert new['key'] == '2'
    response = client.post('/api/launcher/presets', json={
        'operation': 'delete', 'name': new['name'], 'key': new['key'], 'expected_version': 8})
    assert response.status_code == 200 and response.json()['version'] == 9
    assert new['key'] not in snapshot['presets'] and [w[0] for w in writes] == ['save', 'delete']
    assert checksums(root) == before and not calls


@pytest.mark.parametrize('entry', [
    {'pool_key': 'unknown', 'resource': 'stars', 'pulls': 10},
    {'pool_key': 'pickup', 'resource': 'stars', 'pulls': 0},
    {'pool_key': 'pickup', 'resource': 'stars', 'pulls': True},
    {'pool_key': 'pickup', 'resource': 'paid', 'pulls': 10},
    {'pool_key': 'pickup', 'resource': 'stars', 'pulls': 10, 'until_owned': ['member:999'], 'fallback_pool_key': 'standard'}])
def test_invalid_plans_are_rejected_before_cloud_mutation(editor, entry):
    client, _, root, calls, _, _, writes = editor
    value = payload(client)
    value['entries'] = [entry]
    before = checksums(root)
    assert client.post('/api/launcher/presets', json=value).status_code in (409, 422)
    assert checksums(root) == before and not calls and not writes


def test_duplicate_name_and_session_guards(editor):
    client, _, _, _, _, _, writes = editor
    value = payload(client)
    assert client.post('/api/launcher/presets', json=value, headers={'X-Launcher-Token': 'bad'}).status_code == 403
    assert client.post('/api/launcher/presets', json={**value, 'path': '../config.json'}).status_code == 422
    assert client.post('/api/launcher/presets', json={**value, 'operation': 'create'}).status_code == 409
    assert writes == []


def test_start_requires_current_cloud_version_before_spawning(editor):
    client, _, root, calls, _, snapshot, _ = editor
    with sqlite3.connect(root/'state/runtime.sqlite3') as db:
        db.execute('DELETE FROM batches')
        db.execute('DELETE FROM runs')
    value = {'action': 'start', 'run': {'lanes': [{'instance': 0, 'preset': '测试预设'}],
                                     'count': 1, 'preset_version': 6}}
    assert client.post('/api/launcher/action', json=value).status_code == 409 and calls == []
    value['run']['preset_version'] = snapshot['version']
    assert client.post('/api/launcher/action', json=value).status_code == 200
    assert '--preset-version' in calls[0][0] and calls[0][0][calls[0][0].index('--preset-version')+1] == '7'
