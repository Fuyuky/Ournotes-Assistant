"""Cloud commit/local crash recovery, exercised through real HTTP and both stores."""
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from ournotes.cloud.api import create_app
from ournotes.cloud.store import Store
from ournotes.common.identity import MAILBOX
from ournotes.integrations.cloud_client import CloudClient, CloudError
from ournotes.runtime.store import RuntimeStore


@pytest.mark.parametrize("failure", ["response", "local_commit"])
def test_cloud_allocation_is_permanent_and_resumes_original_intent(tmp_path, monkeypatch, failure):
    cloud = Store(tmp_path / "cloud.sqlite3", MAILBOX)
    token = cloud.create_node("first")["token"]
    cloud.high_water({"test_next_index": 8, "formal_next_index": 1 << 20,
                      "evidence": "isolated test namespace"})
    with TestClient(create_app(cloud, "synthetic-admin-credential-for-offline-tests")) as transport:
        client = CloudClient("http://127.0.0.1", token, transport=transport)
        root = tmp_path / "client"
        calls = []

        class Allocator:
            def allocate(self, request_id, account_id, channel):
                # Another connection can write while the network call is outstanding.
                with closing(sqlite3.connect(root / "state/runtime.sqlite3", timeout=0)) as db, db:
                    db.execute("CREATE TABLE IF NOT EXISTS independent_writer (id INTEGER)")
                    db.execute("INSERT INTO independent_writer VALUES (1)")
                result = client.allocate(request_id, account_id, channel)
                calls.append(result)
                if failure == "response" and len(calls) == 1:
                    raise CloudError("unavailable", "Synthetic response lost after server commit")
                return result

        local = RuntimeStore(root, allocator=Allocator())
        run = local.create_run("都抽", "test", 1)
        save_run = local.save_run
        if failure == "local_commit":
            def fail(_):
                raise OSError("Synthetic local disk failure")
            monkeypatch.setattr(local, "save_run", fail)
        with pytest.raises((CloudError, OSError)):
            local.reserve(run, [])
        monkeypatch.setattr(local, "save_run", save_run)
        assert local.counters()["test"] == 8
        assert local.db.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
        local.close()

        local = RuntimeStore(root, allocator=Allocator())
        recovered = local.reserve(local.get_run(run["id"]), [])
        assert calls[0] == calls[1]
        assert recovered["id"] == calls[0]["account_id"]
        assert recovered["alias_index"] == 8
        assert local.db.execute("SELECT count(*) FROM allocation_intents").fetchone()[0] == 0
        # The next request consumes the next number; no release or reuse protocol exists.
        assert client.allocate("next-request", "next-account", "test")["index"] == 9
        local.close()
