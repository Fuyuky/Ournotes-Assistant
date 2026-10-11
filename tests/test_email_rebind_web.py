from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_launcher_web import setup as web_fixture

from ournotes.control.web import create_app
from ournotes.rebind.flow import Flow
from ournotes.rebind.web import Extension

web_setup = pytest.fixture(name='web_setup')(web_fixture.__wrapped__)


class Stub:
    def status(self):
        return {'task': None, 'running': False}

    def start(self, *args):
        raise AssertionError('No device start permitted in this test')

    def close(self):
        pass

    def observe_used(self, envelopes):
        pass

    def retry_target(self, *args):
        self.target = args
        return {'task': {'state': 'changing_email'}}


def test_formal_extension_mount_and_local_csrf(web_setup):
    original, launcher, root, _, _ = web_setup
    assert original.get('/api/launcher/email-rebind/status').status_code == 200
    assert 'account-rebind-toggle' in original.get('/').text
    extension = Extension(root, service=Stub())
    app = create_app(root, service=launcher, extension=extension)
    client = TestClient(app, base_url='http://127.0.0.1')
    assert client.get('/api/launcher/email-rebind/status').json()['task'] is None
    page = client.get('/').text
    assert '<h1>账号管理</h1>' in page and 'account-rebind-toggle' in page
    assert '>标记已使用</button>' in page and '>标记已拍下</button>' in page and '>账号换绑</button>' in page
    assert client.get('/email-rebind/static/app.js').status_code == 200
    assert client.get('/email-rebind/static/style.css').status_code == 200
    assert client.get('/email-rebind/static/unknown.css').status_code == 404
    body = {'index': 1, 'instance': 0, 'email': 'synthetic@example.com', 'request_id': 'a'*32}
    assert client.post('/api/launcher/email-rebind/start', json=body).status_code == 403
    assert client.post('/api/launcher/email-rebind/start', json=body,
                       headers={'X-Launcher-Token': app.state.token, 'Origin': 'https://evil.test'}).status_code == 403
    body['email'] = 'private-sentinel'
    body['instance'] = 'not-an-integer'
    reply = client.post('/api/launcher/email-rebind/start', json=body, headers={'X-Launcher-Token': app.state.token})
    assert reply.status_code == 422 and 'private-sentinel' not in reply.text

    target = {'task_id': 'a'*32, 'command_id': 'b'*32, 'email': 'replacement@qq.com', 'email_revision': 1}
    assert client.post('/api/launcher/email-rebind/target', json=target).status_code == 403
    assert client.post('/api/launcher/email-rebind/target', json=target,
                       headers={'X-Launcher-Token': app.state.token}).json()['task']['state'] == 'changing_email'
    assert extension.service.target == ('a'*32, 'b'*32, 'replacement@qq.com', 1)
    target['email_revision'] = 0
    assert client.post('/api/launcher/email-rebind/target', json=target,
                       headers={'X-Launcher-Token': app.state.token}).status_code == 422


def test_input_guard_refuses_stale_page():
    taps = []
    flow = Flow.__new__(Flow)
    flow.read = lambda: 'unknown'
    flow.device = SimpleNamespace(tap=lambda *args: taps.append(args))
    with pytest.raises(ValueError):
        flow.tap('confirm_b', 800, 605)
    assert not taps


def test_cloud_unknown_mail_does_not_send_again():
    flow = Flow.__new__(Flow)
    calls = []
    flow.service = SimpleNamespace(check=lambda: None, wait=lambda n: None)
    flow.read = lambda: 'verify_a'
    flow.image = None
    flow.fields = None
    flow.client = SimpleNamespace(request=lambda *args: calls.append(args) or {'status':'uncertain'})
    from unittest.mock import patch
    with patch('ournotes.rebind.flow.field_text', return_value=''), pytest.raises(ValueError):
        flow.mail('ticket', 'done')
    assert len(calls) == 1 and calls[0][1].endswith('/poll')


def test_asset_sources_are_independent_and_not_unvalidated():
    import hashlib
    import json
    root = Path(__file__).resolve().parents[1] / 'ournotes/rebind/assets'
    for spec in json.loads((root / 'manifest.json').read_text('utf-8')):
        assert spec['positive'] and spec['positive'] != spec['source']
        assert spec['positive_score'] >= .89
        assert hashlib.sha256((root / spec['file']).read_bytes()).hexdigest() == spec['crop_sha256']
