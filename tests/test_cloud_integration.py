"""Real cloud API plus the real client/outbox/OTP adapter, with synthetic offline transports."""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_cloud_service import ADMIN, MAILBOX, FakeMail, message

from ournotes.cloud.api import create_app
from ournotes.cloud.mail import MailRule
from ournotes.cloud.store import Store
from ournotes.integrations.cloud_client import CloudClient, CloudError
from ournotes.integrations.cloud_sync import CloudSync
from ournotes.runtime.mail import RunnerMail


@pytest.fixture
def integration(tmp_path):
    store = Store(tmp_path / "server/cloud.sqlite3", MAILBOX)
    credential = store.create_node("node1")
    store.high_water({"test_next_index": 8, "formal_next_index": 1 << 20,
                      "evidence": "synthetic initial empty namespace"})
    fake = FakeMail()
    rule = MailRule(("login@example.com",), "Verification", r"Code (?P<code>\d{6})")
    app = create_app(store, ADMIN, backend=fake, rule=rule)
    with TestClient(app) as transport:
        client = CloudClient("http://127.0.0.1", credential["token"], transport=transport)
        yield store, client, fake, tmp_path / "client", transport


def archive(allocation):
    return {"schema_version": 1, "status": "complete", "account_id": allocation["account_id"],
            "alias_index": allocation["index"], "alias": allocation["alias"],
            "cards": [{"card": {"id": 1, "kind": "member", "rarity": "SSR", "title": "Synthetic",
                       "name": "Synthetic"}, "count": 1}],
            "totals": {"member": {"SSR": 1, "SR": 0, "R": 42}, "support": {"SSR": 0, "SR": 0, "R": 0}},
            "used": False}


def test_authenticated_transport_refuses_redirect():
    from fastapi import FastAPI
    from fastapi.responses import RedirectResponse
    app = FastAPI()

    @app.get('/api/cloud/v1/redirect-test')
    def redirect():
        return RedirectResponse('https://untrusted.invalid/collect', status_code=307)

    with TestClient(app) as transport, pytest.raises(CloudError) as error:
        CloudClient('http://127.0.0.1', 'synthetic-credential', transport=transport).request('GET', '/redirect-test')
    assert error.value.status == 307


def test_network_transport_installs_no_redirect_handler(monkeypatch):
    from urllib.error import HTTPError

    from ournotes.integrations import cloud_client

    opener = Mock()
    opener.open.side_effect = HTTPError('https://cloud.invalid', 302, 'redirect', {}, None)
    factory = Mock(return_value=opener)
    monkeypatch.setattr(cloud_client, 'build_opener', factory)
    with pytest.raises(CloudError) as error:
        CloudClient('https://cloud.invalid', 'synthetic-credential').health()
    assert error.value.status == 302
    handler = factory.call_args.args[0]
    assert isinstance(handler, cloud_client.NoRedirect)
    assert handler.redirect_request(None, None, 302, '', {}, 'https://other.invalid') is None


def test_client_outbox_restart_after_server_commit_response_loss(integration):
    store, client, _, root, transport = integration
    allocation = client.allocate("request1", "account1", "formal")
    assert client.allocate("request1", "account1", "formal") == allocation
    sync = CloudSync(root)
    sync.enqueue(archive(allocation))

    class LostResponse:
        def request(self, method, path, **kwargs):
            response = transport.request(method, path, **kwargs)
            if method == "POST" and path.endswith("/results") and response.status_code == 200:
                raise CloudError("unavailable", "Synthetic response loss after durable commit")
            return response

    lossy = CloudClient(client.service_url, client.credential, transport=LostResponse())
    assert sync.try_flush(lossy)["pending"] is True
    assert len(store.results()["results"]) == 1
    assert CloudSync(root).flush(client)["uploaded"] == 1
    assert store.results("account1")["version"] == 1
    assert CloudSync(root).flush(client)["uploaded"] == 0


def test_changed_result_returning_to_prior_content_keeps_unique_event(integration):
    store, client, _, root, _ = integration
    value = archive(client.allocate("request1", "account1", "formal"))
    sync = CloudSync(root)
    sync.enqueue(value)
    sync.flush(client)
    value["draw_labels"] = ["updated"]
    sync.enqueue(value)
    sync.flush(client)
    value["draw_labels"] = []
    sync.enqueue(value)
    assert sync.flush(client)["uploaded"] == 1
    assert store.results("account1")["source_version"] == 3


def test_cloud_used_version_wins_over_stale_source_upload(integration):
    _, client, _, root, _ = integration
    value = archive(client.allocate("request1", "account1", "formal"))
    sync = CloudSync(root)
    sync.enqueue(value)
    sync.flush(client)
    changed = client.set_used("account1", True, 1, "used1")
    assert changed["version"] == 2
    assert client.set_used("account1", True, 1, "used1") == changed
    value["draw_labels"] = ["source-change"]
    sync.enqueue(value)
    sync.flush(client)
    assert client.get_result("account1")["result"]["used"] is True
    assert client.list_results()["revision"] == 3
    with pytest.raises(CloudError) as error:
        client.set_used("account1", False, 1, "stale")
    assert error.value.status == 409


def test_runner_mail_uses_original_ticket_and_durable_finish_outbox(integration):
    store, client, fake, root, _ = integration
    allocation = client.allocate("request1", "account1", "formal")
    mail = RunnerMail(root, client=client)
    prepared = mail.prepare(allocation["alias"], "account1")
    assert mail.prepare(allocation["alias"], "account1")["ticket"] == prepared["ticket"]
    fake.messages["candidate"] = message(allocation["alias"], store.clock() + 1)
    assert mail.poll(prepared["ticket"], prepared['expires_at']) == {"status": "received", "code": "123456"}
    assert mail.finish(prepared["ticket"])["uploaded"] == 1
    assert client.poll_mail(prepared["ticket"])["status"] == "done"


def test_node_admin_boundaries_via_real_client(integration):
    _, client, _, _, _ = integration
    assert client.health()["node_id"] == "node1"
    with pytest.raises(CloudError) as error:
        client.request('POST', '/admin/oauth', {"refresh_token": "synthetic-never-real"})
    assert error.value.status == 403


def test_mail_retry_recovers_lost_response_with_one_new_ticket(integration, monkeypatch):
    store, client, fake, root, _ = integration
    allocation = client.allocate('allocation', 'retry-login-account', 'test')
    mail = RunnerMail(root, client=client)
    original = mail.prepare(allocation['alias'], allocation['account_id'])
    fake.messages['first'] = message(allocation['alias'], store.clock() + 1)
    assert mail.poll(original['ticket'], original['expires_at'])['status'] == 'received'
    request = client.retry_mail
    calls = []
    def lost_response(*args, **kwargs):
        value = request(*args, **kwargs)
        calls.append(value['ticket_id'])
        if len(calls) == 1:
            raise CloudError('unavailable', 'synthetic lost response', 503)
        return value
    monkeypatch.setattr(client, 'retry_mail', lost_response)
    monkeypatch.setattr('ournotes.runtime.mail.time.sleep', lambda _: None)
    retry = mail.retry(original['ticket'], allocation['account_id'], 'game_rejected')
    assert calls == [retry['ticket'], retry['ticket']]
    assert client.poll_mail(original['ticket'])['status'] == 'superseded'
    fake.messages['second'] = message(allocation['alias'], store.clock() + 1, '654321')
    assert mail.poll(retry['ticket'], retry['expires_at']) == {'status': 'received', 'code': '654321'}
    with pytest.raises(CloudError) as error:
        client.retry_mail(retry['ticket'], 'second-retry', 'game_rejected')
    assert error.value.status == 409
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 2


def test_mail_retries_lost_prepare_response_without_creating_another_ticket(integration, monkeypatch):
    store, client, fake, root, transport = integration
    allocation = client.allocate('retry-allocation', 'retry-account', 'test')
    waits = []
    monkeypatch.setattr('ournotes.runtime.mail.time.sleep', waits.append)
    monkeypatch.setattr('ournotes.runtime.mail.random.uniform', lambda low, high: low)

    class FlakyMail:
        failures = 2

        def request(self, method, path, **kwargs):
            response = transport.request(method, path, **kwargs)
            if self.failures:
                self.failures -= 1
                raise CloudError('unavailable', 'response lost after commit')
            return response

    mail = RunnerMail(root, client=CloudClient(client.service_url, client.credential, transport=FlakyMail()))
    prepared = mail.prepare(allocation['alias'], 'retry-account')
    assert waits == [2, 4]
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 1
    fake.messages['candidate'] = message(allocation['alias'], store.clock() + 1)
    assert mail.poll(prepared['ticket'], prepared['expires_at'])['status'] == 'received'


@pytest.mark.parametrize('status,expires_at,calls', [(503, 200, 6), (503, 101, 1), (403, 200, 1)])
def test_mail_retry_is_bounded_by_ticket_deadline_and_error_kind(monkeypatch, status, expires_at, calls):
    monkeypatch.setattr('ournotes.runtime.mail.time.time', lambda: 100)
    wait = Mock()
    monkeypatch.setattr('ournotes.runtime.mail.time.sleep', wait)
    client = Mock()
    client.poll_mail.side_effect = CloudError('http_error', 'synthetic', status)
    mail = RunnerMail(client=client)
    with pytest.raises(CloudError):
        mail.poll('same-ticket', expires_at)
    assert client.poll_mail.call_count == calls
    assert all(call.args == ('same-ticket',) for call in client.poll_mail.call_args_list)
    assert wait.call_count == calls - 1
    client.prepare_mail.assert_not_called()
    client.mail_sent.assert_not_called()


def test_completion_upload_targets_one_account_and_retries_same_receipt(integration, monkeypatch):
    from ournotes.integrations.cloud_sync import CloudClientConfig
    store, client, _, root, _ = integration
    sync = CloudSync(root)
    for i in range(2):
        sync.enqueue(archive(client.allocate(f'completion-{i}', f'completed-{i}', 'formal')))
    monkeypatch.setattr(CloudClientConfig, 'load', lambda root: object())
    monkeypatch.setattr(CloudClient, 'from_root', lambda root: client)
    original = client.upload
    monkeypatch.setattr(client, 'upload', Mock(side_effect=CloudError('unavailable', 'offline')))
    assert sync.upload_completed('completed-1')['pending']
    with sync.database() as db:
        before = tuple(db.execute('SELECT event_id,version FROM outbox WHERE key=?', ('completed-1',)).fetchone())
    monkeypatch.setattr(client, 'upload', original)
    assert sync.upload_completed('completed-1')['results_uploaded'] == 1
    assert len(store.results()['results']) == 1
    with sync.database() as db:
        assert tuple(db.execute('SELECT event_id,version FROM outbox WHERE key=?', ('completed-1',)).fetchone()) == before
        assert db.execute('SELECT pending FROM outbox WHERE key=?', ('completed-0',)).fetchone()[0] == 1
    assert sync.upload_completed('completed-1')['results_uploaded'] == 0
