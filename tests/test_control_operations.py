from concurrent.futures import ThreadPoolExecutor

import pytest

from ournotes.control.operations import OperationStore


def test_repeat_response_loss_restart_and_parameter_conflict(tmp_path):
    calls = []
    store = OperationStore(tmp_path)
    original = store.submit("command-001", "start", {"batch_id": "batch"}, lambda _: calls.append(1) or {"job": "one"})
    assert OperationStore(tmp_path).submit("command-001", "start", {"batch_id": "batch"}, lambda _: calls.append(2)) == original
    assert calls == [1]
    with pytest.raises(ValueError, match="不同参数"):
        store.submit("command-001", "start", {"batch_id": "other"}, lambda _: None)


def test_concurrent_duplicate_forms_single_action(tmp_path):
    store = OperationStore(tmp_path)
    calls = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: store.submit("command-002", "start", {}, lambda _: calls.append(1)), range(4)))
    assert len(calls) == 1
    assert all(reply["command_id"] == "command-002" for reply in replies)


def test_crash_window_never_replays_and_pending_is_bound(tmp_path):
    store = OperationStore(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        store.submit("command-003", "start", {}, lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert store.get("command-003")["status"] == "running"
    assert store.submit("command-003", "start", {}, lambda _: pytest.fail("uncertain command replayed"))["status"] == "running"
    store.submit("command-004", "pause", {"batch_id": "a"}, lambda _: {})
    assert not store.pending("b")
    assert store.pending("a")[0]["command_id"] == "command-004"
    store.mark("command-004", "paused")
    assert not store.pending("a")
    # All handles are closed, so the file can be renamed on Windows.
    store.path.rename(store.path.with_suffix(".closed"))


def test_missing_receipt_does_not_create_database(tmp_path):
    with pytest.raises(ValueError):
        OperationStore(tmp_path).get("missing")
    assert not (tmp_path / "state").exists()


def test_pending_extended_actions_and_run_target(tmp_path):
    store = OperationStore(tmp_path)
    store.submit("command-005", "resume_lanes", {"run_id": "run"}, lambda _: {})
    store.mark("command-005", "accepted")
    assert store.pending("run", actions=("resume_lanes",))[0]["parameters"] == {"run_id": "run"}
    assert not store.pending("run")
