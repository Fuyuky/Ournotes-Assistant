"""Server process, proxy privacy and mail-network isolation, all with fake IO."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_cloud_service import MAILBOX, FakeMail, allocate

from deploy.cloud.update_proxy import build_config, update
from ournotes.cloud.mail import GmailAuthorizationRequired, GmailBackend, MailRule, SecretVault, Tickets
from ournotes.cloud.process import process_lease
from ournotes.cloud.store import Conflict, Store, Unavailable


def store(tmp_path):
    value = Store(tmp_path / "cloud.sqlite3", MAILBOX)
    value.create_node("node1")
    value.create_node("node2")
    value.high_water({"test_next_index": 8, "formal_next_index": 1 << 20,
                      "evidence": "synthetic verified initial namespace"})
    return value


def test_one_process_lease_and_os_release(tmp_path):
    path = tmp_path / "cloud.sqlite3"
    with process_lease(path), pytest.raises(Unavailable), process_lease(path):
        pass
    with process_lease(path):
        pass


@pytest.mark.parametrize('marker,category', [
    ('CERTIFICATE_VERIFY_FAILED', 'certificate_verify_failed'),
    ('UNEXPECTED_EOF_WHILE_READING', 'unexpected_eof'),
    ('SSLV3_ALERT_HANDSHAKE_FAILURE', 'handshake_failure'),
    ('WRONG_VERSION_NUMBER', 'wrong_version'),
    ('unrecognized SSL detail', 'other'),
])
@pytest.mark.parametrize('service', ['gmail', 'oauth'])
def test_transport_diagnostic_classifies_tls_without_exposing_secrets(tmp_path, monkeypatch, caplog,
                                                                    marker, category, service):
    from unittest.mock import MagicMock

    import requests

    backend = GmailBackend(SecretVault(tmp_path / 'private/oauth.json'),
                           'http://127.0.0.1:7890', MAILBOX, lambda: 0)
    session = MagicMock()
    session.__enter__.return_value = session
    session.request.side_effect = requests.exceptions.SSLError(
        marker + ' synthetic-secret-token private-mail@example.test')
    monkeypatch.setattr(backend, 'session', lambda: session)
    url = ('https://oauth2.googleapis.com/token' if service == 'oauth' else
           'https://gmail.googleapis.com/gmail/v1/users/me/messages?q=private-mail@example.test')
    with pytest.raises(Unavailable):
        backend.request('GET', url)
    assert f'service={service} tls={category}' in caplog.text
    assert 'synthetic-secret-token' not in caplog.text
    assert 'private-mail@example.test' not in caplog.text
    assert url not in caplog.text


def test_mail_network_blocks_no_sqlite_transaction_or_other_node(tmp_path):
    db = store(tmp_path)
    first = allocate(db)
    started, release = threading.Event(), threading.Event()

    class SlowMail(FakeMail):
        def message_ids(self, query):
            started.set()
            release.wait(timeout=3)
            return set()

    tickets = Tickets(db, SlowMail(), MailRule(("login@example.com",), "Verification", r"(?P<code>\d{6})"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(tickets.prepare, "node1", {"request_id": "ticket", "account_id": "account1",
                              "alias": first["alias"], "timeout": 120})
        assert started.wait(timeout=1)
        try:
            other = allocate(db, "node2", "account2", "allocation2")
            assert not release.is_set()
            assert other["index"] == first["index"] + 1
        finally:
            release.set()
        assert pending.result(timeout=2)["status"] == "prepared"


def test_corrupt_allocation_audit_disables_allocation_but_not_private_queries(tmp_path):
    db = store(tmp_path)
    allocate(db)
    with db.audit_path.open("a", encoding="utf-8") as handle:
        handle.write("{truncated\n")
    restarted = Store(db.path, MAILBOX)
    assert restarted.results()["results"] == []
    with restarted.connection() as connection:
        assert restarted.meta(connection, "allocation_enabled") == "false"
    with pytest.raises(Unavailable):
        allocate(restarted, account="other", request="other")


def test_expired_account_has_no_automatic_second_ticket(tmp_path):
    db = store(tmp_path)
    now = [1700000000.0]
    db.clock = lambda: now[0]
    allocation = allocate(db)
    tickets = Tickets(db, FakeMail(), MailRule(("login@example.com",), "Verification", r"(?P<code>\d{6})"))
    request = {"request_id": "ticket", "account_id": "account1", "alias": allocation["alias"], "timeout": 30}
    first = tickets.prepare("node1", request)
    now[0] += 31
    assert tickets.poll("node1", first["ticket_id"])["status"] == "expired"
    with pytest.raises(Conflict):
        tickets.prepare("node1", {**request, "request_id": "different"})


def test_google_refresh_and_gmail_read_share_explicit_proxy(tmp_path, monkeypatch):
    vault = SecretVault(tmp_path / "private/oauth.json")
    backend = GmailBackend(vault, "http://127.0.0.1:7890", MAILBOX, lambda: 1700000000.0)
    records = []

    class Session:
        def __init__(self):
            self.proxies, self.trust_env = {}, True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            records.append((method, url, self.trust_env, self.proxies.copy(), kwargs))
            response = {"access_token": "synthetic-access-token", "expires_in": 3600} if method == "POST" else {
                "emailAddress": MAILBOX}
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: response)

    import requests
    monkeypatch.setattr(requests, "Session", Session)
    credentials = {"token": "synthetic", "refresh_token": "synthetic-refresh",
                   "token_uri": "https://oauth2.googleapis.com/token", "client_id": "synthetic.apps.googleusercontent.com",
                   "client_secret": "synthetic-client-secret", "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}
    assert backend.import_credentials(credentials) == {"authorized": True}
    assert [record[0] for record in records] == ["POST", "GET"]
    assert all(record[2] is False and record[3]["https"] == "http://127.0.0.1:7890" for record in records)
    assert "synthetic-refresh" in vault.path.read_text("utf-8")
    records.clear()
    saved = vault.load()
    saved["expires_at"] = 0
    vault.save(saved)
    backend.get("profile")
    assert [record[0] for record in records] == ["POST", "GET"]
    with pytest.raises(ValueError):
        GmailBackend(vault, "http://public-proxy.example:7890", MAILBOX, lambda: 0)


def test_invalid_google_grant_is_permanent_and_never_echoes_response(tmp_path, monkeypatch, caplog):
    from unittest.mock import Mock

    import requests

    backend = GmailBackend(SecretVault(tmp_path / 'private/oauth.json'),
                           'http://127.0.0.1:7890', MAILBOX, lambda: 0)
    response = Mock(status_code=400)
    response.json.return_value = {'error': 'invalid_grant', 'error_description': 'private-sentinel'}
    response.raise_for_status.side_effect = requests.HTTPError('private-sentinel', response=response)
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.request.return_value = response
    monkeypatch.setattr(backend, 'session', lambda: session)
    with pytest.raises(GmailAuthorizationRequired) as raised:
        backend.request('POST', 'https://oauth2.googleapis.com/token', data={'refresh_token': 'private-sentinel'})
    assert 'private-sentinel' not in str(raised.value) + caplog.text
    assert 'invalid_grant' in caplog.text



def test_vault_refresh_preserves_owner_before_replacing_credentials(tmp_path, monkeypatch):
    import os

    vault = SecretVault(tmp_path / "private/oauth.json")
    vault.save({"token": "old-synthetic"})
    previous = vault.path.stat()
    owners = []

    def chown(fd, uid, gid):
        assert vault.load() == {"token": "old-synthetic"}
        assert os.fstat(fd).st_size == 0
        owners.append((uid, gid))

    monkeypatch.setattr(os, "fchown", chown, raising=False)
    vault.save({"token": "new-synthetic"})
    assert owners == [(previous.st_uid, previous.st_gid)]
    assert vault.load() == {"token": "new-synthetic"}


def test_vault_owner_failure_keeps_previous_credentials(tmp_path, monkeypatch):
    import os

    vault = SecretVault(tmp_path / "private/oauth.json")
    vault.save({"token": "old-synthetic"})

    def denied(*args):
        raise PermissionError("synthetic denied")

    monkeypatch.setattr(os, "fchown", denied, raising=False)
    with pytest.raises(PermissionError):
        vault.save({"token": "new-synthetic"})
    assert vault.load() == {"token": "old-synthetic"}
    assert not list(vault.path.parent.glob("*.tmp"))


def test_vault_unreadable_credentials_return_unavailable(tmp_path, monkeypatch):
    from pathlib import Path

    vault = SecretVault(tmp_path / "private/oauth.json")
    vault.save({"token": "synthetic"})

    def denied(*args):
        raise PermissionError("private-path-and-secret")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(Unavailable) as error:
        vault.load()
    assert "permissions require maintenance" in str(error.value)
    assert "private-path-and-secret" not in str(error.value)


def subscription():
    return {"mixed-port": 9999, "external-controller": "0.0.0.0:9090", "rules": ["MATCH,DIRECT"],
            "proxies": [{"name": "node", "type": "trojan", "server": "proxy.example", "port": 443,
                         "password": "synthetic-only"}]}


def test_subscription_does_not_inherit_listener_rules_controller_or_skip_tls():
    value = build_config(subscription())
    assert value["mixed-port"] == 7890 and value["bind-address"] == "127.0.0.1"
    assert value["allow-lan"] is False and "external-controller" not in value
    assert value["rules"] == ["MATCH,GOOGLE"]
    group = value["proxy-groups"][0]
    assert group["type"] == "url-test" and group["lazy"] is False
    assert group["url"] == "https://www.gstatic.com/generate_204"
    bad = subscription()
    bad["proxies"][0]["skip-cert-verify"] = True
    with pytest.raises(ValueError):
        build_config(bad)


def test_failed_proxy_validation_preserves_previous_file(tmp_path, monkeypatch):
    import requests

    import deploy.cloud.update_proxy as module
    pytest.importorskip("yaml")
    private = tmp_path / "subscription-url"
    private.write_text("https://subscription.example/synthetic-only", "utf-8")
    output = tmp_path / "config.yaml"
    output.write_text("previous valid config", "utf-8")
    response = SimpleNamespace(raise_for_status=lambda: None,
                               iter_content=lambda size: [json.dumps(subscription()).encode()])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(ValueError):
        update(private, output, "synthetic-mihomo")
    assert output.read_text("utf-8") == "previous valid config"


def test_native_google_credentials_json_import_discards_metadata_and_verifies_mailbox(tmp_path, monkeypatch):
    from unittest.mock import Mock

    credentials_module = pytest.importorskip("google.oauth2.credentials")
    credentials = credentials_module.Credentials(
        token="synthetic-old-token", refresh_token="synthetic-refresh",
        client_id="synthetic.apps.googleusercontent.com", client_secret="synthetic-client-secret",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"])
    value = json.loads(credentials.to_json())
    value.update(universe_domain="googleapis.com", account="untrusted-label@example.test")
    value.pop("token_uri", None)  # Current native serialization omits the default URI.
    vault = SecretVault(tmp_path / "private/oauth.json")
    backend = GmailBackend(vault, "http://127.0.0.1:7890", MAILBOX, lambda: 1700000000.0)
    network = Mock(side_effect=[{"access_token": "synthetic-new-token", "expires_in": 3600},
                               {"emailAddress": MAILBOX}])
    monkeypatch.setattr(backend, "request", network)
    assert backend.import_credentials(value) == {"authorized": True}
    saved = vault.load()
    assert saved["token"] == "synthetic-new-token"
    assert saved["token_uri"] == "https://oauth2.googleapis.com/token"
    assert "account" not in saved and "universe_domain" not in saved
    before = vault.path.read_bytes()
    network.reset_mock()
    with pytest.raises(ValueError):
        backend.import_credentials({**value, "universe_domain": "untrusted.example"})
    assert not network.called and vault.path.read_bytes() == before


@pytest.mark.parametrize("action", ["revoke", "rotate"])
def test_credential_changes_during_mail_network_cannot_claim_or_return_code(tmp_path, action):
    from test_cloud_service import message

    from ournotes.cloud.store import Forbidden

    db = store(tmp_path)
    credential = db.rotate_node("node1")
    identity = db.authenticate(credential["token"])
    allocation = allocate(db)
    backend = FakeMail()
    tickets = Tickets(db, backend, MailRule(("login@example.com",), "Verification", r"Code (?P<code>\d{6})"))
    ticket = tickets.prepare(identity, {"request_id": "ticket", "account_id": "account1",
                                       "alias": allocation["alias"], "timeout": 120})
    backend.messages["candidate"] = message(allocation["alias"], db.clock() + 1)
    started, release = threading.Event(), threading.Event()

    def slow_ids(query):
        started.set()
        release.wait(timeout=3)
        return {"candidate"}

    backend.message_ids = slow_ids
    with ThreadPoolExecutor(max_workers=1) as pool:
        polling = pool.submit(tickets.poll, identity, ticket["ticket_id"])
        assert started.wait(timeout=1)
        try:
            if action == "revoke":
                db.revoke_node("node1")
            else:
                db.rotate_node("node1")
        finally:
            release.set()
        with pytest.raises(Forbidden):
            polling.result(timeout=2)
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM consumed").fetchone()[0] == 0
        assert connection.execute("SELECT code FROM tickets WHERE id=?", (ticket["ticket_id"],)).fetchone()[0] is None
