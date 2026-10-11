import concurrent.futures
import threading

import pytest

from ournotes.runtime.pools import make_plan
from ournotes.runtime.scheduler import MultiRunner, create_batch
from ournotes.runtime.store import AllocationClosed, RuntimeStore


@pytest.mark.parametrize("total", [1, 2, 5, 17])
def test_concurrent_lanes_never_exceed_shared_total(tmp_path, total):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(i, "MyGO only") for i in range(4)], "test", total)
    barrier = threading.Barrier(4)

    def work(lane):
        store = RuntimeStore(tmp_path)
        indices = []
        try:
            run = store.get_run(lane["run_id"])
            assert run["count"] is None
            barrier.wait(timeout=10)
            while True:
                try:
                    account = store.reserve(run, make_plan(run["preset"]))
                except AllocationClosed:
                    return indices
                indices.append(account["alias_index"])
        finally:
            store.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        indices = [i for result in pool.map(work, batch["lanes"]) for i in result]
    assert len(set(indices)) == len(indices) == total
    assert db.counters()["test"] == 8 + total
    assert db.get_batch()["total_count"] == total
    db.close()


def test_inflight_and_deleted_slots_still_consume_quota_after_reopen(tmp_path):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(0, "MyGO only"), (1, "Mujica only")], "test", 1)
    run = db.get_run(batch["lanes"][0]["run_id"])
    account = db.reserve(run, make_plan(run["preset"]))
    db.close()
    db = RuntimeStore(tmp_path)
    assert db.get_run(run["id"])["active_account"] == account["id"]
    other = db.get_run(batch["lanes"][1]["run_id"])
    with pytest.raises(AllocationClosed):
        db.reserve(other, make_plan(other["preset"]))
    account["status"] = "deleted"
    db.save_account(account)
    with pytest.raises(AllocationClosed):
        db.reserve(other, make_plan(other["preset"]))
    assert db.counters()["test"] == 9
    db.close()


def test_legacy_batches_and_unlimited_batches_keep_semantics(tmp_path):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(0, "MyGO only")], "test", None)
    run = db.get_run(batch["lanes"][0]["run_id"])
    for _ in range(3):
        db.reserve(run, make_plan(run["preset"]))
    assert not db.batch_quota_exhausted(run)
    batch.pop("count_mode")
    batch.pop("total_count")
    run["count"] = 50
    db.save_batch(batch)
    db.save_run(run)
    assert db.get_run(run["id"])["count"] == 50 and not db.batch_quota_exhausted(run)
    db.close()


def test_empty_staggered_lane_does_not_boot_when_shared_quota_is_allocated(tmp_path, monkeypatch):
    import ournotes.runtime.scheduler as module

    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(0, "MyGO only"), (1, "MyGO only")], "test", 1)
    first = db.get_run(batch["lanes"][0]["run_id"])
    db.reserve(first, make_plan(first["preset"]))
    monkeypatch.setattr(module, "RuntimeStore", lambda: RuntimeStore(tmp_path))
    monkeypatch.setattr(module.Runner, "_initialize", lambda *a, **kw: pytest.fail("must not start a device"))
    controller = MultiRunner()
    controller.worker(batch["lanes"][1])
    assert controller.updates.get_nowait()["status"] == "complete"
    assert db.get_run(batch["lanes"][1]["run_id"])["status"] == "complete"
    db.close()
