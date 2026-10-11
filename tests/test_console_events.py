"""One local state stream, authenticated WS, and no implicit business actions."""
import asyncio
import copy
import json
import sqlite3

import pytest
from starlette.websockets import WebSocketDisconnect
from test_launcher_web import setup as launcher_setup

from ournotes.control.events import StateStream

setup_fixture = pytest.fixture(name='setup')(launcher_setup.__wrapped__)


def test_stream_ignores_read_timestamps_and_only_emits_changed_state():
    value = {'observed_at': 1, 'running': False, 'cloud_sync': {'updated_at': 1}, 'email_rebind': {'running': False, 'task': None}}
    calls = []
    stream = StateStream(lambda: calls.append(True) or copy.deepcopy(value))
    async def run():
        first = await stream.refresh()
        value['observed_at'] = 2
        value['cloud_sync']['updated_at'] = 2
        second = await stream.refresh()
        assert first['stream_revision'] == second['stream_revision']
        assert (await stream.next(second['stream_revision'], timeout=.001))['type'] == 'heartbeat'
        value['email_rebind']['task'] = {'state': 'waiting_b'}
        third = await stream.refresh()
        assert third['stream_revision'] > second['stream_revision']
        event = await stream.next(second['stream_revision'])
        assert event['data']['email_rebind']['task']['state'] == 'waiting_b'
        await stream.close()
    asyncio.run(run())
    assert len(calls) == 3


def test_two_browsers_receive_the_same_changed_runtime_snapshot(setup):
    client, _, root, spawned, _ = setup
    address = 'ws://127.0.0.1:8766/api/launcher/events'
    protocols = ['ournotes', 'session.' + client.app.state.token]
    with client.websocket_connect(address, subprotocols=protocols) as first, client.websocket_connect(address, subprotocols=protocols) as second:
        assert first.accepted_subprotocol == second.accepted_subprotocol == 'ournotes'
        a, b = first.receive_json(), second.receive_json()
        assert a['type'] == b['type'] == 'snapshot'
        assert a['data']['stream_revision'] == b['data']['stream_revision']
        assert 'email_rebind' in a['data'] and 'notifications' in a['data'] and 'clash' in a['data']
        with sqlite3.connect(root/'state/runtime.sqlite3') as db:
            run = json.loads(db.execute("SELECT data FROM runs WHERE id='r1'").fetchone()[0])
            run['cloud_confirmed'] = 2
            db.execute("UPDATE runs SET data=? WHERE id='r1'", (json.dumps(run),))
        updated = client.post('/api/launcher/refresh').json()
        assert updated['lanes'][0]['complete'] == 2
        a, b = first.receive_json(), second.receive_json()
        assert a['data']['stream_revision'] == b['data']['stream_revision']
        assert a['data']['lanes'][0]['complete'] == 2
    assert spawned == []


@pytest.mark.parametrize('protocols,headers', [
    (['ournotes', 'session.wrong'], {}),
    ([], {}),
    (['ournotes'], {}),
    (['valid'], {'origin': 'https://evil.example'}),
    (['valid'], {'host': 'evil.example'})])
def test_websocket_requires_loopback_same_origin_and_session_token(setup, protocols, headers):
    client, _, _, spawned, _ = setup
    if protocols == ['valid']:
        protocols = ['ournotes', 'session.' + client.app.state.token]
    address = 'ws://127.0.0.1:8766/api/launcher/events'
    with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect(address, headers=headers, subprotocols=protocols):
        pass
    assert error.value.code == 4403 and spawned == []


def test_websocket_query_token_is_not_supported(setup):
    client, _, _, _, _ = setup
    address = 'ws://127.0.0.1:8766/api/launcher/events?token=' + client.app.state.token
    with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect(address):
        pass
    assert error.value.code == 4403


def test_machine_api_is_mounted_on_console_with_bearer_and_loopback_guards(setup):
    from fastapi.testclient import TestClient

    from ournotes.control.web import create_app
    _, service, root, spawned, _ = setup
    secret = 'offline-test-session-' + 'x' * 32
    app = create_app(root, service, machine_credential=secret)
    client = TestClient(app, base_url='http://127.0.0.1:8766')
    route = '/agent/api/v1/status'
    assert client.get(route).status_code == 401
    client.headers['Authorization'] = 'Bearer ' + secret
    status = client.get(route)
    assert status.status_code == 200 and status.json()['version'] == service.runtime()['version']
    assert client.get(route, headers={'Host': 'evil.example'}).status_code == 403
    assert client.get(route, headers={'Origin': 'https://evil.example'}).status_code == 403
    for removed in ('accounts', 'diagnostics', 'review'):
        assert client.get('/agent/api/v1/' + removed).status_code == 404
    response = client.post('/agent/api/v1/commands', json={
        'action': 'gmail_check', 'command_id': 'synthetic-command', 'expected_version': service.runtime()['version']})
    assert response.status_code == 422 and spawned == []


def test_old_game_update_can_be_hidden_without_install_or_audit_changes(setup):
    client, service, root, spawned, _ = setup
    value = {'status': 'failed', 'message': '[WinError 32] old file in use', 'request_id': 'a'*32,
             'results': [{'instance': 1, 'status': 'updated', 'version': '1.0.3'}]}
    path = root/'state/game-update.json'
    path.write_text(json.dumps(value), 'utf-8')
    from ournotes.control.game_update import GameUpdate
    service.game_update = GameUpdate(root)
    before = path.read_bytes()
    assert client.get('/api/launcher/status').json()['game_update']['historical']
    assert client.post('/api/launcher/game-update/dismiss', json={}).json()['status'] == 'idle'
    assert path.read_bytes() == before and spawned == []
