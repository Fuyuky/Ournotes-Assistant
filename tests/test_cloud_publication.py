"""Global manual snapshots and interrupted commit; never publishes outside a temporary directory."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_account_browser import folders as build_folders
from test_cloud_service import ADMIN, MAILBOX, allocate, result, upload

from ournotes.cloud.api import PREFIX, create_app
from ournotes.cloud.publication import Publisher
from ournotes.cloud.store import Conflict, Store


@pytest.fixture
def published(tmp_path):
    _, catalog = build_folders.__wrapped__(tmp_path)
    store = Store(tmp_path / "cloud.sqlite3", MAILBOX)
    node = store.create_node("node1")
    store.create_node("node2")
    store.high_water({"test_next_index": 10, "formal_next_index": (1 << 20) + 100,
                      "evidence": "synthetic verified counters"})
    publisher = Publisher(store, tmp_path / "publications", catalog)
    return store, publisher, node, catalog


def request(revision, request_id="publish1"):
    return {"request_id": request_id, "expected_revision": revision, "limit": 300}


def snapshot(publisher):
    current = json.loads(publisher.resource("data/current.json").read_text("utf-8"))
    return json.loads(publisher.resource(f"data/snapshot.{current['revision']}.json").read_text("utf-8"))


def test_booking_folds_publicly_and_terminal_change_advances_usage_without_new_content(published):
    store, publisher, credential, catalog = published
    a = allocate(store)
    upload(store, a)
    publisher.publish('node1', {'request_id': 'all-booking', 'expected_revision': 1, 'limit': None})
    with TestClient(create_app(store, ADMIN, publisher=publisher, catalog=catalog)) as client:
        headers = {'Authorization': 'Bearer ' + credential['token']}
        url = PREFIX + f"/results/{a['account_id']}/usage"
        booked = client.post(url, headers=headers, json={'request_id': 'booked',
                             'expected_version': 1, 'target_status': 'booked'})
        assert booked.status_code == 200 and not booked.json()['publication']['pending']
        public = snapshot(publisher)
        assert public['accounts'][0]['used'] and 'usage_status' not in json.dumps(public)
        revision = public['revision']
        done = client.post(url, headers=headers, json={'request_id': 'used',
                           'expected_version': 2, 'target_status': 'used'})
        assert done.status_code == 200 and not done.json()['publication']['pending']
        assert snapshot(publisher)['revision'] == revision
        assert publisher.current()['usage_revision'] == store.results()['revision']


def test_booking_cancellation_refreshes_existing_public_account(published):
    store, publisher, _, _ = published
    a = allocate(store)
    upload(store, a)
    store.usage('node1', a['account_id'], {'request_id': 'booked', 'expected_version': 1, 'target_status': 'booked'})
    publisher.publish('node1', {'request_id': 'all-booked', 'expected_revision': 2, 'limit': None})
    assert snapshot(publisher)['accounts'][0]['used']
    store.usage('node1', a['account_id'], {'request_id': 'cancel', 'expected_version': 2, 'target_status': 'available'})
    assert not publisher.publish_usage()['pending']
    assert not snapshot(publisher)['accounts'][0]['used']


def test_announcement_change_is_durably_queued_for_automatic_publication(published):
    store, publisher, _, _ = published
    upload(store, allocate(store))
    publisher.publish('node1', request(1))
    original = snapshot(publisher)['announcement']
    publisher.save_announcement('新公告', publisher.announcement()['version'])
    assert snapshot(publisher)['announcement'] == original
    result = publisher.publish_pending()
    assert not result.get('unchanged')
    assert snapshot(publisher)['announcement']['content'] == '新公告'
    assert snapshot(publisher)['announcement']['version'] != original['version']


def test_pricing_source_changes_invalidate_unchanged_publication(published, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from ournotes.accounts import pricing

    _, publisher, _, _ = published
    before = publisher.build_signature()
    original = Path.stat
    price_path = Path(pricing.__file__)

    def updated_stat(path, *args, **kwargs):
        stat = original(path, *args, **kwargs)
        if path == price_path:
            return SimpleNamespace(st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns+1)
        return stat

    monkeypatch.setattr(Path, 'stat', updated_stat)
    assert publisher.build_signature() != before


def test_full_catalog_usage_updates_without_rebuilding_artwork_or_publishing_new_accounts(published, monkeypatch):
    store, publisher, credential, catalog = published
    a = allocate(store)
    upload(store, a)
    first = publisher.publish('node1', {'request_id': 'all', 'expected_revision': 1, 'limit': None})
    initial_price = snapshot(publisher)['accounts'][0]['price']
    import ournotes.cloud.publication as module
    monkeypatch.setattr(module, 'export_site', lambda *args, **kwargs: pytest.fail('used update rebuilt site'))
    # An unpublished new result must not be silently published by a usage update.
    b = allocate(store, 'node2', 'account-new', 'allocation-new')
    upload(store, b)
    app = create_app(store, ADMIN, publisher=publisher, catalog=catalog)
    with TestClient(app) as client:
        response = client.post(PREFIX + f"/results/{a['account_id']}/used",
                               headers={'Authorization': 'Bearer ' + credential['token']},
                               json={'request_id': 'mark', 'expected_version': 1, 'used': True})
    assert response.status_code == 200 and response.json()['publication']['pending'] is False
    data = snapshot(publisher)
    pointer = json.loads(publisher.resource('data/current.json').read_text('utf-8'))
    assert pointer['schema_version'] == data['schema_version'] == 2
    assert data['accounts'][0]['price'] == initial_price
    assert len(data['accounts']) == 1 and data['accounts'][0]['used'] is True
    assert data['meta']['used_accounts'] == 1 and data['meta']['public_catalog']
    assert publisher.status(store.results()['revision'])['pending']  # New result still unpublished.
    assert publisher.resource(f"data/snapshot.{first['public_revision']}.json").exists()
    # Original request keeps its original receipt after an automatic usage publication.
    assert publisher.publish('node1', {'request_id': 'all', 'expected_revision': 1, 'limit': None}) == first


def test_unchanged_publish_is_fast_and_new_result_reuses_verified_assets(published, monkeypatch):
    store, publisher, _, _ = published
    a = allocate(store)
    upload(store, a)
    publisher.publish('node1', {'request_id': 'all', 'expected_revision': 1, 'limit': None})
    from pathlib import Path
    original = Path.read_bytes
    def no_art_reads(path):
        assert path.suffix not in ('.png', '.webp'), 'Unchanged artwork was reread'
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', no_art_reads)
    response = publisher.publish('node1', {'request_id': 'no-change', 'expected_revision': 1, 'limit': None})
    assert response['unchanged'] and response['changed_files'] == 0
    b = allocate(store, 'node2', 'new', 'new-alloc')
    upload(store, b)
    response = publisher.publish('node1', {'request_id': 'added', 'expected_revision': 2, 'limit': None})
    assert response['accounts'] == 2


def test_usage_publication_failure_keeps_marker_and_retries(published, monkeypatch):
    store, publisher, credential, catalog = published
    a = allocate(store)
    upload(store, a)
    publisher.publish('node1', {'request_id': 'all', 'expected_revision': 1, 'limit': None})
    original = publisher._usage_pointer
    monkeypatch.setattr(publisher, '_usage_pointer', lambda *args: (_ for _ in ()).throw(OSError('failure')))
    app = create_app(store, ADMIN, publisher=publisher, catalog=catalog)
    with TestClient(app) as client:
        response = client.post(PREFIX + f"/results/{a['account_id']}/used",
                               headers={'Authorization': 'Bearer ' + credential['token']},
                               json={'request_id': 'mark', 'expected_version': 1, 'used': True})
    assert response.status_code == 200 and response.json()['publication']['pending'] is True
    assert store.results(a['account_id'])['result']['used'] is True
    assert snapshot(publisher)['accounts'][0]['used'] is False
    monkeypatch.setattr(publisher, '_usage_pointer', original)
    assert publisher.publish_usage()['pending'] is False
    assert snapshot(publisher)['accounts'][0]['used'] is True


def test_private_updates_wait_for_manual_global_publish_and_used(published):
    store, publisher, _, _ = published
    a, b = allocate(store), allocate(store, "node2", "account2", "allocation2")
    upload(store, a)
    upload(store, b)
    assert publisher.current() is None
    with pytest.raises(KeyError):
        publisher.resource("index.html")
    revision = store.results()["revision"]
    first = publisher.publish("node1", request(revision))
    assert publisher.publish("node1", request(revision)) == first
    assert len(snapshot(publisher)["accounts"]) == 2
    public = json.dumps(snapshot(publisher))
    for secret in (a["alias"], b["alias"], "account_id", "node_id", "UID", "owner_use"):
        assert secret not in public
    store.used("node2", a["account_id"], {"request_id": "used1", "expected_version": 1, "used": True})
    assert len(snapshot(publisher)["accounts"]) == 2
    assert publisher.status(store.results()["revision"])["pending"]
    second = publisher.publish("node2", request(store.results()["revision"], "publish2"))
    assert second["accounts"] == 2
    assert {x["index"] for x in snapshot(publisher)["accounts"]} == {a['index'], b['index']}
    assert next(x for x in snapshot(publisher)['accounts'] if x['index'] == a['index'])['used'] is True
    assert second["changed_files"] < len(publisher.current()["files"])
    assert not publisher.status(second["revision"])["pending"]
    publisher.resource(f"data/snapshot.{first['public_revision']}.json")  # old browser still works


def test_legacy_limit_cannot_truncate_the_public_inventory(published):
    store, publisher, _, _ = published
    a, b = allocate(store), allocate(store, 'node2', 'second', 'second-allocation')
    upload(store, a)
    upload(store, b)
    value = publisher.publish('node1', {'request_id': 'legacy-client', 'expected_revision': 2, 'limit': 1})
    assert value['accounts'] == 2 and publisher.current()['all_accounts'] is True


def test_publication_interrupt_leaves_pointer_and_recovers_lost_receipt(published, monkeypatch):
    store, publisher, _, _ = published
    a = allocate(store)
    upload(store, a)
    first = publisher.publish("node1", request(1))
    store.used("node2", a["account_id"], {"request_id": "used", "expected_version": 1, "used": True})
    import ournotes.cloud.publication as module
    replace = module.os.replace

    def fail_pointer(source, destination):
        if destination == publisher.pointer:
            raise OSError("synthetic pointer switch failure")
        replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_pointer)
    with pytest.raises(OSError):
        publisher.publish("node1", request(2, "second"))
    assert publisher.current()["response"] == first
    monkeypatch.setattr(module.os, "replace", replace)
    original = store.save_receipt

    def fail_receipt(*args):
        raise OSError("synthetic crash after pointer fsync")

    monkeypatch.setattr(store, "save_receipt", fail_receipt)
    with pytest.raises(OSError):
        publisher.publish("node1", request(2, "second"))
    switched = publisher.current()["response"]
    monkeypatch.setattr(store, "save_receipt", original)
    assert publisher.publish("node1", request(2, "second")) == switched
    assert switched["accounts"] == 1
    assert snapshot(publisher)['accounts'][0]['used'] is True


def test_revision_or_sha_conflict_cannot_change_publication(published, monkeypatch):
    store, publisher, _, _ = published
    a = allocate(store)
    upload(store, a)
    first = publisher.publish("node1", request(1))
    with pytest.raises(Conflict):
        publisher.publish("node1", request(0, "stale"))
    import ournotes.cloud.publication as module
    export = module.export_site

    def corrupted(*args, **kwargs):
        value = export(*args, **kwargs)
        value["files"]["index.html"]["sha256"] = "0" * 64
        return value

    monkeypatch.setattr(module, "export_site", corrupted)
    store.used("node1", a["account_id"], {"request_id": "force-new-render", "expected_version": 1, "used": True})
    with pytest.raises(Conflict):
        publisher.publish("node1", request(2, "bad-hash"))
    assert publisher.current()["response"] == first
    with pytest.raises(KeyError):
        publisher.resource("../cloud.sqlite3")


def test_catalog_rejects_unknown_result_and_public_routes_are_whitelisted(published):
    store, publisher, node, catalog = published
    app = create_app(store, ADMIN, publisher=publisher, catalog=catalog)
    headers = {"Authorization": "Bearer " + node["token"]}
    a = allocate(store)
    invalid = result(a)
    invalid["cards"][0]["card"]["id"] = 9999
    with TestClient(app) as client:
        response = client.put(PREFIX + "/results/account1", headers=headers,
                              json={"event_id": "invalid", "version": 1, "result": invalid})
        assert response.status_code == 422
        assert client.get("/").status_code == 404
        upload(store, a)
        publisher.publish("node1", request(1))
        assert client.get("/").status_code == 200
        assert client.get("/data/current.json").status_code == 200
        assert client.get("/published.json").status_code == 404
        assert client.get("/cloud.sqlite3").status_code == 404
        status = client.get(PREFIX + "/results", headers=headers).json()["publication"]
        assert status["pending"] is False


def test_version_fsync_failure_cannot_switch_publication(published, monkeypatch):
    store, publisher, _, _ = published
    allocation = allocate(store)
    upload(store, allocation)
    first = publisher.publish("node1", request(1))
    import ournotes.cloud.publication as module

    def fail_before_pointer(*args):
        raise OSError("synthetic filesystem flush failure")

    monkeypatch.setattr(module, "sync_version", fail_before_pointer)
    store.used("node1", allocation["account_id"], {"request_id": "force-flush", "expected_version": 1, "used": True})
    with pytest.raises(OSError):
        publisher.publish("node1", request(2, "cannot-flush"))
    assert publisher.current()["response"] == first


def test_saved_pool_identity_survives_publication(published):
    store,publisher,_,_=published
    a=allocate(store)
    value=result(a)
    value['pool_keys']=['bdon-gacha-12']
    store.upload('node1',a['account_id'],{'event_id':'with-pool','version':1,'result':value})
    publisher.publish('node1',request(1))
    assert snapshot(publisher)['accounts'][0]['pool_keys']==['bdon-gacha-12']
