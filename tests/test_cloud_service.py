"""Offline cloud failure windows; only synthetic data, tokens and mail."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ournotes.cloud.api import PREFIX, create_app
from ournotes.cloud.mail import MailRule, Tickets
from ournotes.cloud.models import Result
from ournotes.cloud.store import Conflict, Forbidden, Store, Unavailable, alias_at

MAILBOX = "abcdefghijklmnopqrstuvwxy@gmail.com"
ADMIN = "synthetic-administrator-credential-1234567890"


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / "cloud.sqlite3", MAILBOX)
    node1, node2 = store.create_node("node1"), store.create_node("node2")
    # Distinct upload owners; all administrators may still query and manage usage globally.
    with store.transaction() as db:
        db.execute("UPDATE nodes SET owner_username='Other' WHERE id='node2'")
    store.high_water({"test_next_index": 10, "formal_next_index": (1 << 20) + 100,
                      "evidence": "synthetic verified retained counters"})
    return store, node1, node2


def allocate(store, node="node1", account="account1", request="allocation1", channel="formal"):
    return store.allocate(node, {"account_id": account, "request_id": request, "channel": channel})


def result(allocation):
    return {"schema_version": 1, "account_id": allocation["account_id"], "index": allocation["index"],
            "email": allocation["alias"], "cards": [{"card": {"kind": "member", "id": 1,
             "rarity": "SSR", "name": "Synthetic", "title": "Card"}, "count": 1}],
            "totals": {"member": {"SSR": 1, "SR": 0, "R": 42}, "support": {"SSR": 0, "SR": 0, "R": 0}},
            "pull_count": 43, "draw_labels": ["43"], "used": False}


def upload(store, allocation, version=1, event="event1"):
    return store.upload(allocation["node_id"], allocation["account_id"],
                        {"event_id": event, "version": version, "result": result(allocation)})


def test_concurrent_permanent_allocations_and_lost_response(service):
    store, _, _ = service
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda n: allocate(store, "node1" if n % 2 else "node2",
                                                  f"account{n}", f"request{n}"), range(24)))
    assert len({item["index"] for item in values}) == 24
    first = values[0]
    restarted = Store(store.path, MAILBOX)
    assert allocate(restarted, first["node_id"], first["account_id"], first["request_id"]) == first
    assert allocate(restarted, account="next", request="next")["index"] > max(x["index"] for x in values)
    with pytest.raises(Conflict):
        allocate(restarted, first["node_id"], "different", first["request_id"])


def test_fsync_reservation_recovers_failed_database_commit(service, monkeypatch):
    store, _, _ = service
    original = store.save_receipt

    def fail(*args):
        raise OSError("synthetic disk failure after durable reservation")

    monkeypatch.setattr(store, "save_receipt", fail)
    with pytest.raises(OSError):
        allocate(store)
    monkeypatch.setattr(store, "save_receipt", original)
    first = allocate(store)
    assert first["index"] == (1 << 20) + 100
    assert allocate(store, account="next", request="next")["index"] == first["index"] + 1


def test_initial_closed_and_backup_restore_stays_closed(tmp_path, service):
    fresh = Store(tmp_path / "fresh.sqlite3", MAILBOX)
    fresh.create_node("node1")
    with pytest.raises(Unavailable):
        allocate(fresh)
    store, _, _ = service
    allocate(store)
    backup = store.backup(tmp_path / "backup.sqlite3")
    restored = Store(backup, MAILBOX)
    with pytest.raises(Unavailable):
        allocate(restored, account="new", request="new")
    with pytest.raises(Conflict):
        restored.high_water({"test_next_index": 10, "formal_next_index": 1 << 20,
                             "evidence": "unsafe older high water"})


def test_namespaces_and_credential_revocation(service):
    store, node1, node2 = service
    assert allocate(store, channel="test")["index"] == 10
    assert allocate(store, "node2", "formal", "formal")["index"] >= 1 << 20
    assert store.authenticate(node1["token"]) == "node1"
    store.revoke_node("node1")
    with pytest.raises(Forbidden):
        store.authenticate(node1["token"])
    assert store.authenticate(node2["token"]) == "node2"
    rotated = store.rotate_node("node1")
    assert store.authenticate(rotated["token"]) == "node1"


def test_merge_nodes_used_cas_old_events_and_source_ownership(service):
    store, _, _ = service
    first = allocate(store)
    second = allocate(store, "node2", "account2", "allocation2")
    a = upload(store, first)
    upload(store, second)
    assert len(store.results()["results"]) == 2
    changed = store.used("node2", "account1", {"request_id": "used1", "expected_version": a["version"], "used": True})
    assert changed["version"] == 2
    assert upload(store, first) == a  # permanent response, no old state applied
    latest = upload(store, first, 2, "event2")
    assert latest["result"]["used"] is True and latest["version"] == 3
    with pytest.raises(Conflict):
        store.used("node1", "account1", {"request_id": "stale", "expected_version": 1, "used": False})
    with pytest.raises(Forbidden):
        store.upload("node2", "account1", {"event_id": "foreign", "version": 3, "result": result(first)})
    with pytest.raises(Conflict):
        upload(store, first, 1, "old-new-id")


def test_success_schema_rejects_runtime_fields_and_incomplete_totals(service):
    value = result(allocate(service[0]))
    for field in ("uid", "UID", "owner_use", "status", "phase", "context", "run", "batch", "screenshot"):
        with pytest.raises(ValueError):
            Result.model_validate({**value, field: "forbidden"})
    assert Result.model_validate(value).totals.member.R == 42
    value["totals"]["member"]["R"] = 41
    with pytest.raises(ValueError):
        Result.model_validate(value)


def test_permanent_receipts_support_real_chinese_titles_and_conflicting_replays(service):
    store, _, _ = service
    allocation = allocate(store)
    value = result(allocation)
    value["cards"][0]["card"].update(name="千石由乃", title="由我主演的赛博之夜")
    request = {"event_id": "中文回执", "version": 1, "result": value}
    first = store.upload("node1", "account1", request)
    assert store.upload("node1", "account1", request) == first
    restarted = Store(store.path, MAILBOX)
    assert restarted.upload("node1", "account1", request) == first
    with pytest.raises(Conflict):
        restarted.upload("node1", "account1", {**request, "version": 2})


def test_historical_import_no_reallocation_and_conflicts(service):
    store, _, _ = service
    historic = {"account_id": "historical", "index": (1 << 20) + 2,
                "alias": alias_at((1 << 20) + 2, MAILBOX)}
    request = {"request_id": "migration1", "node_id": "node1", "channel": "formal", "result": result(historic)}
    a = store.import_historical(request)
    assert store.import_historical(request) == a
    assert store.import_historical({**request, "request_id": "migration2"}) == a
    acknowledgement = store.upload("node1", "historical", {"event_id": "first-local-queue", "version": 1,
                                   "result": request["result"]})
    assert acknowledgement == a and store.results()["revision"] == 1
    assert allocate(store)["index"] == (1 << 20) + 100
    with pytest.raises(Conflict):
        store.import_historical({**request, "request_id": "collision", "node_id": "node2"})


class FakeMail:
    def __init__(self):
        self.messages = {}
        self.fail = False

    def message_ids(self, query):
        if self.fail:
            raise Unavailable("Synthetic proxy unavailable")
        return set(self.messages)

    def message(self, message_id):
        return self.messages[message_id]


def message(alias, now, code="123456", sender="login@example.com"):
    return {"internalDate": str(int(now * 1000)), "payload": {"mimeType": "text/plain", "headers": [
        {"name": "From", "value": sender}, {"name": "To", "value": alias},
        {"name": "Subject", "value": "Verification"}],
        "body": {"data": base64.urlsafe_b64encode(("Code " + code).encode()).decode()}}}


@pytest.fixture
def mail(service):
    store, _, _ = service
    now = [1700000000.0]
    store.clock = lambda: now[0]
    backend = FakeMail()
    rule = MailRule(("login@example.com",), "Verification", r"Code (?P<code>\d{6})")
    return store, Tickets(store, backend, rule), backend, now


def prepare(tickets, allocation, request="ticket1"):
    return tickets.prepare(allocation["node_id"], {"request_id": request, "account_id": allocation["account_id"],
                                                 "alias": allocation["alias"], "timeout": 120})


def test_exact_alias_parallel_tickets_notify_loss_and_repeat_query(mail):
    store, tickets, backend, now = mail
    a, b = allocate(store), allocate(store, "node2", "account2", "allocation2")
    ta, tb = prepare(tickets, a), prepare(tickets, b, "ticket2")
    assert prepare(tickets, a)["ticket_id"] == ta["ticket_id"]
    backend.messages["first"] = message(a["alias"], now[0] + 1)
    backend.messages["second"] = message(b["alias"], now[0] + 1, "654321")
    # Client's sent notification was lost; original prepared ticket still recovers the right code.
    assert tickets.poll("node1", ta["ticket_id"])["code"] == "123456"
    assert tickets.poll("node2", tb["ticket_id"])["code"] == "654321"
    assert tickets.poll("node1", ta["ticket_id"])["code"] == "123456"
    with pytest.raises(Forbidden):
        tickets.poll("node2", ta["ticket_id"])
    assert "code" not in tickets.complete("node1", ta["ticket_id"])
    with store.connection() as db:
        assert db.execute("SELECT code FROM tickets WHERE id=?", (ta["ticket_id"],)).fetchone()[0] is None


def test_large_historical_inbox_does_not_block_new_ticket(mail):
    store, tickets, backend, now = mail
    allocation = allocate(store)
    for number in range(600):
        backend.messages[f"old-{number}"] = message(allocation["alias"], now[0] - 60)
    backend.messages["existing-current-second"] = message(allocation["alias"], now[0])

    def bounded_ids(query):
        cutoff = int(query.rsplit("after:", 1)[1]) * 1000
        ids = {key for key, value in backend.messages.items() if int(value["internalDate"]) >= cutoff}
        if len(ids) >= 500:
            raise Unavailable("Gmail candidate limit reached")
        return ids

    backend.message_ids = bounded_ids
    ticket = prepare(tickets, allocation)
    assert tickets.poll("node1", ticket["ticket_id"])["status"] == "prepared"
    backend.messages["new"] = message(allocation["alias"], now[0] + 1, "654321")
    assert tickets.poll("node1", ticket["ticket_id"])["code"] == "654321"


@pytest.mark.parametrize('reason', ['game_rejected', 'input_misdirected'])
def test_rejected_code_retry_is_once_idempotent_and_excludes_original_message(mail, reason):
    store, tickets, backend, now = mail
    allocation = allocate(store)
    original = prepare(tickets, allocation)
    backend.messages['original'] = message(allocation['alias'], now[0] + 1)
    assert tickets.poll('node1', original['ticket_id'])['status'] == 'ready'
    now[0] += 121  # A saved rejection can be recovered after the old ticket expired.
    request = {'request_id': 'retry-account1', 'reason': reason, 'timeout': 120}
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: tickets.retry('node1', original['ticket_id'], request), range(4)))
    retry = replies[0]
    assert all(reply == retry for reply in replies)
    assert retry['ticket_id'] != original['ticket_id']
    assert tickets.poll('node1', original['ticket_id'])['status'] == 'superseded'
    assert tickets.poll('node1', retry['ticket_id'])['status'] == 'prepared'
    backend.messages['replacement'] = message(allocation['alias'], now[0] + 1, '654321')
    assert tickets.poll('node1', retry['ticket_id'])['code'] == '654321'
    restarted = Tickets(Store(store.path, MAILBOX, clock=store.clock), backend, tickets.rule)
    assert restarted.retry('node1', original['ticket_id'], request)['ticket_id'] == retry['ticket_id']
    with pytest.raises(Conflict):
        restarted.retry('node1', retry['ticket_id'], dict(request, request_id='another-retry'))
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM ticket_retries').fetchone()[0] == 1
        assert db.execute('SELECT code FROM tickets WHERE id=?', (original['ticket_id'],)).fetchone()[0] is None
        assert db.execute('SELECT ticket_id FROM consumed WHERE message_id=?', ('original',)).fetchone()[0] == original['ticket_id']


@pytest.mark.parametrize('state', ['prepared', 'sent', 'expired', 'ambiguous', 'done', 'foreign'])
@pytest.mark.parametrize('reason', ['game_rejected', 'input_misdirected'])
def test_retry_rejects_undelivered_uncertain_completed_or_foreign_tickets(mail, state, reason):
    store, tickets, backend, now = mail
    allocation = allocate(store)
    original = prepare(tickets, allocation)
    if state in ('done', 'foreign'):
        backend.messages['code'] = message(allocation['alias'], now[0] + 1)
        tickets.poll('node1', original['ticket_id'])
    if state == 'sent':
        tickets.sent('node1', original['ticket_id'])
    elif state == 'expired':
        now[0] += 121
    elif state == 'done':
        tickets.complete('node1', original['ticket_id'])
    elif state == 'ambiguous':
        for i in range(2):
            backend.messages[str(i)] = message(allocation['alias'], now[0] + 1)
        tickets.poll('node1', original['ticket_id'])
    with pytest.raises(Forbidden if state == 'foreign' else Conflict):
        tickets.retry('node2' if state == 'foreign' else 'node1', original['ticket_id'],
                      {'request_id': 'retry', 'reason': reason, 'timeout': 120})
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM ticket_retries').fetchone()[0] == 0


def test_baseline_sender_ambiguity_expiry_and_proxy_isolation(mail):
    store, tickets, backend, now = mail
    a = allocate(store)
    backend.messages["old"] = message(a["alias"], now[0])
    ticket = prepare(tickets, a)
    backend.messages["wrong"] = message(a["alias"], now[0] + 1, sender="wrong@example.com")
    assert tickets.poll("node1", ticket["ticket_id"])["status"] == "prepared"
    backend.messages["new1"] = message(a["alias"], now[0] + 1)
    backend.messages["new2"] = message(a["alias"], now[0] + 2)
    assert tickets.poll("node1", ticket["ticket_id"])["status"] == "ambiguous"
    backend.fail = True
    assert allocate(store, "node2", "other", "other")["index"] > a["index"]
    now[0] += 121
    assert tickets.poll("node1", ticket["ticket_id"])["status"] == "ambiguous"
    with pytest.raises(Conflict):
        tickets.retry('node1', ticket['ticket_id'],
                      {'request_id': 'no-ambiguous-retry', 'reason': 'expired', 'timeout': 120})


def test_only_confirmed_expiry_can_retry_once_without_a_delivered_code(mail):
    store, tickets, _backend, now = mail
    allocation = allocate(store)
    original = prepare(tickets, allocation)
    request = {'request_id': 'expired-retry', 'reason': 'expired', 'timeout': 120}
    with pytest.raises(Conflict):
        tickets.retry('node1', original['ticket_id'], request)
    now[0] += 121
    retry = tickets.retry('node1', original['ticket_id'], request)
    assert retry['ticket_id'] != original['ticket_id']
    assert tickets.poll('node1', original['ticket_id'])['status'] == 'superseded'
    now[0] += 121
    with pytest.raises(Conflict):
        tickets.retry('node1', retry['ticket_id'], dict(request, request_id='another-expired-retry'))
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 2


def test_backup_scrubs_ephemeral_codes_and_does_not_resume_tickets(mail, tmp_path):
    import sqlite3
    from contextlib import closing

    store, tickets, backend, now = mail
    allocation = allocate(store)
    ticket = prepare(tickets, allocation)
    backend.messages["candidate"] = message(allocation["alias"], now[0] + 1)
    assert tickets.poll("node1", ticket["ticket_id"])["status"] == "ready"
    backup = store.backup(tmp_path / "backup.sqlite3")
    with closing(sqlite3.connect(backup)) as connection:
        code, state = connection.execute("SELECT code,state FROM tickets WHERE id=?", (ticket["ticket_id"],)).fetchone()
        assert code is None and state == "expired"
        assert connection.execute("SELECT value FROM metadata WHERE key='allocation_enabled'").fetchone()[0] == "false"


def test_api_admin_boundaries_and_sensitive_validation_not_echoed(service):
    store, node1, _ = service
    app = create_app(store, ADMIN)
    with TestClient(app) as client:
        header = {"Authorization": "Bearer " + node1["token"]}
        assert client.get(PREFIX + "/health", headers=header).json()["node_id"] == "node1"
        assert client.get(PREFIX + "/results").status_code == 403
        assert client.post(PREFIX + "/admin/nodes", headers=header, json={"node_id": "forbidden"}).status_code == 403
        response = client.post(PREFIX + "/allocations", headers=header,
                               json={"request_id": "x", "account_id": "x", "token": "SECRET-MUST-NOT-ECHO"})
        assert response.status_code == 422 and "SECRET" not in response.text
        assert response.headers["Cache-Control"] == "no-store"
        admin = {"Authorization": "Bearer " + ADMIN}
        assert client.post(PREFIX + "/admin/nodes", headers=admin, json={"node_id": "new"}).status_code == 200
        allocation = client.post(PREFIX + "/allocations", headers=header,
                                 json={"request_id": "request", "account_id": "account"}).json()
        response = client.put(PREFIX + "/results/account", headers=header,
                              json={"event_id": "event", "version": 1, "result": result(allocation)})
        assert response.status_code == 200
        assert len(client.get(PREFIX + "/results", headers=header).json()["results"]) == 1


def test_proxy_limit_replacement_is_linked_idempotent_and_repeatable(mail):
    store, tickets, _backend, _now = mail
    original = prepare(tickets, allocate(store))
    for i in range(3):
        tickets.sent('node1', original['ticket_id'])
        request = {'request_id': f'limit-{i}', 'reason': 'proxy_limit', 'timeout': 120}
        replacement = tickets.retry('node1', original['ticket_id'], request)
        assert tickets.retry('node1', original['ticket_id'], request)['ticket_id'] == replacement['ticket_id']
        with pytest.raises(Conflict):
            tickets.retry('node1', original['ticket_id'], dict(request, request_id=f'other-{i}'))
        original = replacement
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM ticket_retries').fetchone()[0] == 0
