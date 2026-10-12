import concurrent.futures
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from ournotes.paths import ASSETS
from ournotes.runtime.login_history import history_control
from ournotes.runtime.model import PauseRun
from ournotes.runtime.pools import make_plan
from ournotes.runtime.runner import Runner
from ournotes.runtime.scheduler import DEFAULT_LANES, MultiRunner, create_batch, export_batch
from ournotes.runtime.store import RuntimeStore
from ournotes.vision.ocr import Vision


def test_cloud_preset_id_zero_is_pinned_independently_of_name(tmp_path, monkeypatch):
    from ournotes.runtime.presets import canonical_preset
    definition = canonical_preset({'name': 'Editable name', 'entries': [
        {'pool_key': 'example', 'resource': 'stars', 'pulls': 10}]})
    snapshot = {'version': 4, 'presets': {'0': definition}}
    monkeypatch.setattr('ournotes.integrations.cloud_client.CloudClient.from_root',
                        lambda _: SimpleNamespace(presets=lambda: snapshot))
    db = RuntimeStore(tmp_path)
    try:
        batch = create_batch(db, [(0, 'Editable name')], 'test', 1, preset_version=4)
        snapshot['presets']['0']['name'] = 'Changed later'
        run = db.get_run(batch['lanes'][0]['run_id'])
        assert (run['preset_key'], run['preset'], run['preset_version']) == ('0', 'Editable name', 4)
    finally:
        db.close()


def test_worker_keeps_actionable_initialization_error_and_trace(tmp_path, monkeypatch):
    with_store = RuntimeStore(tmp_path)
    batch = create_batch(with_store, [(1, '主抽 mygo 60 抽')], 'test', 200)
    with_store.close()
    monkeypatch.setattr('ournotes.runtime.scheduler.RuntimeStore', lambda: RuntimeStore(tmp_path))
    monkeypatch.setattr('ournotes.runtime.scheduler.Runner',
                        lambda: SimpleNamespace(db=None, device_touched=False, _initialize=Mock(side_effect=ValueError('卡池引用缺失卡牌'))))
    controller = MultiRunner()
    controller.worker(batch['lanes'][0])
    update = controller.updates.get_nowait()
    assert update['message'] == '初始化失败：卡池引用缺失卡牌'
    trace = json.loads((tmp_path / 'state/runs' / update['run_id'] / 'initialization-error.json').read_text('utf-8'))
    assert trace['error_type'] == 'ValueError' and trace['trace']


def test_initialized_runner_publishes_lane_status_without_overwriting_scheduler(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(2, '主抽 mygo 60 抽')], 'test', 200)
    run_id = batch['lanes'][0]['run_id']
    state = tmp_path / 'state'
    aggregate = state / 'runner-status.json'
    aggregate.write_text('{"scheduler": true}', encoding='utf-8')
    monkeypatch.setattr('ournotes.runtime.runner.RuntimeStore', lambda: db)
    monkeypatch.setattr('ournotes.runtime.runner.ROOT', tmp_path)
    monkeypatch.setattr('ournotes.runtime.runner.STATE', state)
    monkeypatch.setattr('ournotes.runtime.runner.sync_catalog', lambda: tmp_path / 'catalog')
    monkeypatch.setattr('ournotes.runtime.runner.PoolFinder',
                        lambda _: SimpleNamespace(catalog=SimpleNamespace(get=lambda key: {'key': key})))
    emulator = Mock(side_effect=PauseRun('test stop before device startup'))
    monkeypatch.setattr('ournotes.runtime.runner.ensure_game', emulator)
    updates = []
    runner = Runner()
    runner._initialize(run_id=run_id, callback=updates.append)
    runner._execute()
    emulator.assert_called_once_with(2, runner.stop)
    assert updates[0]['status'] == 'running'
    assert updates[0]['message'] == '加载本地识别模型与设备'
    assert updates[-1]['status'] == 'paused'
    assert updates[-1]['message'] == 'test stop before device startup'
    assert json.loads((state / 'instances/2/status.json').read_text(encoding='utf-8')) == updates[-1]
    assert json.loads(aggregate.read_text(encoding='utf-8')) == {'scheduler': True}


def test_cancelled_batch_cannot_resume_or_repair_exports(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(0, "MyGO only")], "test", 1)
    batch["status"] = "cancelled"
    db.save_batch(batch)
    repair = Mock(side_effect=AssertionError("cancelled batch must not repair or recreate archives"))
    monkeypatch.setattr("ournotes.runtime.scheduler.RuntimeStore", lambda: db)
    monkeypatch.setattr("ournotes.runtime.scheduler.STATE", tmp_path / "state")
    monkeypatch.setattr(db, "repair_exports", repair)
    with pytest.raises(ValueError, match="没有可恢复"):
        MultiRunner(resume=True).execute()
    repair.assert_not_called()


@pytest.mark.parametrize('legacy', [False, True])
def test_explicit_resume_clears_mail_limit_before_starting_lanes(tmp_path, monkeypatch, legacy):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(0, 'MyGO only'), (2, 'Mujica only')], 'test', 2)
    run = db.get_run(batch['lanes'][1]['run_id'])
    run.update(status='paused', pause_reason='今日邮箱发送次数已达上限' if legacy else '用户暂停',
               mail_send_limit_reached=not legacy)
    db.save_run(run)
    counters = db.counters()
    monkeypatch.setattr('ournotes.runtime.scheduler.RuntimeStore', lambda: db)
    monkeypatch.setattr('ournotes.runtime.scheduler.STATE', tmp_path / 'state')
    def inspect_before_devices():
        from ournotes.runtime.runner import mail_limit_reached
        saved = db.get_run(run['id'])
        assert not mail_limit_reached(saved)
        assert saved['mail_limit_reset']['previous_reason'] == run['pause_reason']
        assert db.counters() == counters
        assert saved['active_account'] == run['active_account']
        raise RuntimeError('offline boundary')
    monkeypatch.setattr(db, 'repair_exports', inspect_before_devices)
    controller = MultiRunner(resume=True)
    controller.worker = Mock()
    with pytest.raises(RuntimeError, match='offline boundary'):
        controller.execute()
    controller.worker.assert_not_called()


def test_cancelled_run_cannot_resume_or_repair_exports(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    run = db.create_run("MyGO only", "test", 1)
    run["status"] = "cancelled"
    db.save_run(run)
    repair = Mock(side_effect=AssertionError("cancelled run must not repair archives"))
    monkeypatch.setattr("ournotes.runtime.runner.RuntimeStore", lambda: db)
    monkeypatch.setattr(db, "repair_exports", repair)
    try:
        with pytest.raises(ValueError):
            Runner()._initialize(resume=True, run_id=run["id"])
        assert db.get_run()["status"] == "cancelled"
        repair.assert_not_called()
    finally:
        db.close()


def test_three_workers_share_atomic_counter_and_recover_each_account(tmp_path):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, DEFAULT_LANES, "test", 3)
    barrier = threading.Barrier(3)
    def reserve(lane):
        store = RuntimeStore(tmp_path)
        try:
            run = store.get_run(lane["run_id"])
            barrier.wait(timeout=10)
            return store.reserve(run, make_plan(run["preset"]))
        finally:
            store.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        accounts = list(pool.map(reserve, batch["lanes"]))
    assert sorted(a["alias_index"] for a in accounts) == [8, 9, 10]
    assert len({a["alias"] for a in accounts}) == 3
    assert db.counters()["test"] == 11
    for lane, account in zip(batch["lanes"], accounts):
        assert db.get_run(lane["run_id"])["active_account"] == account["id"]
        assert account["instance"] == lane["instance"]
        assert account["batch_id"] == batch["id"]
    with pytest.raises(ValueError, match="未结束"):
        create_batch(db, DEFAULT_LANES, "test", 1)
    db.close()


def test_duplicate_instances_cannot_create_or_consume_accounts(tmp_path):
    db = RuntimeStore(tmp_path)
    with pytest.raises(ValueError):
        create_batch(db, [(0, "都抽"), (0, "MyGO only")], "test", 1)
    assert db.get_batch() is None and db.get_run() is None
    assert db.counters()["test"] == 8
    db.close()


def test_batch_results_keep_only_cloud_confirmed_aggregate(tmp_path):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, DEFAULT_LANES, "test", 3)
    for lane in batch["lanes"]:
        account = db.reserve(db.get_run(lane["run_id"]), make_plan(lane["preset"]))
        card = {"key": "member:1", "kind": "member", "rarity": "SSR", "id": 1, "name": "名字", "title": "标题"}
        db.save_draw({"id": account["id"], "account_id": account["id"], "ordinal": 0, "status": "complete",
                          "result": {"slots": [{"status": "matched", "card": card}]}})
        db.export(account["id"])
    report = export_batch(db, batch)
    assert report['completed_accounts'] == 0
    assert report['accounts'] == []
    for lane in batch['lanes']:
        run = db.get_run(lane['run_id'])
        run.update(cloud_confirmed=1, cloud_totals={'member': {'SSR': 1}})
        # Simulate the cloud-ACK transaction, not a stale runner save.
        db.db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(run), run['id']))
    db.db.commit()
    report = export_batch(db, batch)
    assert report["totals"]["member"]["SSR"] == 3
    assert report["pulls"] == 3
    assert report['completed_accounts'] == 3
    assert report['accounts'] == [] and report['summary'] == []
    assert export_batch(db, batch) == report
    db.close()








def test_history_templates_and_specific_confirmation():
    vision = Vision()
    cases = [("history-initial", "login_history", "expand"),
             ("history-expanded", "login_history_expanded", "remove"),
             ("history-delete", "login_history_confirm", None)]
    for name, scene, control in cases:
        image = Image.open(ASSETS / "login-history" / (name + ".png"))
        assert vision.read(image).scene == scene
        if control:
            point = history_control(image, control)
            assert point and 950 <= point[0] <= 970 and 385 <= point[1] <= 425
    # A blank dialog never provides a target, nor does the collapsed dropdown expose deletion.
    assert history_control(Image.new("RGB", (1600, 900), "white"), "remove") is None
    assert history_control(Image.open(ASSETS / "login-history/history-initial.png"), "remove") is None


def test_history_cannot_skip_to_new_login_while_entries_remain():
    runner = Runner()
    runner.account = {"phase": "switch_account", "context": {}}
    runner.observation = SimpleNamespace(scene="login_history")
    runner.login_flow.clear_login_history, runner.click = Mock(), Mock()
    runner.login_flow.switch_account()
    runner.login_flow.clear_login_history.assert_called_once()
    runner.click.assert_not_called()


def test_resume_skips_completed_lane_and_does_not_reserve_new_alias(tmp_path, monkeypatch):
    import ournotes.runtime.scheduler as module
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, DEFAULT_LANES, "test", 1)
    first = db.get_run(batch["lanes"][0]["run_id"])
    first.update(status="complete", completed=1)
    db.save_run(first)
    counters = copy.deepcopy(db.counters())
    db.close()
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(module, "shutdown_instances", lambda instances: {"status": "closed", "instances": instances})
    monkeypatch.setattr(module, "RuntimeStore", lambda: RuntimeStore(tmp_path))
    controller = MultiRunner(resume=True)
    called = []
    def worker(lane):
        called.append(lane["instance"])
        store = RuntimeStore(tmp_path)
        run = store.get_run(lane["run_id"])
        run["status"] = "complete"
        store.save_run(run)
        store.close()
    monkeypatch.setattr(controller, "worker", worker)
    controller.execute()
    db = RuntimeStore(tmp_path)
    assert sorted(called) == [1, 2]
    assert db.counters() == counters
    assert db.get_batch()["status"] == "complete"
    db.close()


def test_batch_finalization_crash_requires_resume_not_new_batch(tmp_path):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, DEFAULT_LANES, "test", 1)
    for lane in batch["lanes"]:
        run = db.get_run(lane["run_id"])
        run["status"] = "complete"
        db.save_run(run)
    with pytest.raises(ValueError, match="未结束"):
        create_batch(db, DEFAULT_LANES, "test", 1)
    db.close()


def test_finish_is_delivered_to_every_unfinished_lane(tmp_path, monkeypatch):
    import ournotes.runtime.scheduler as module
    from ournotes.control.operations import OperationStore
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, DEFAULT_LANES, "test", 1)
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(module, "shutdown_instances", lambda instances: {"status": "closed", "instances": instances})
    controller = MultiRunner()
    controller.batch = batch
    operations = OperationStore(tmp_path)
    operations.submit("finish-command-1", "finish", {"batch_id": batch["id"]}, lambda _: {})
    controller.controls(db)
    for lane in batch["lanes"]:
        assert db.get_run(lane["run_id"])["finish_requested"] is True
    assert not controller.stop.is_set()
    operations.submit("pause-command-1", "pause", {"batch_id": batch["id"]}, lambda _: {})
    controller.controls(db)
    assert controller.stop.is_set()
    db.close()


@pytest.fixture
def scheduler_shutdown(tmp_path, monkeypatch):
    # Exercise the real controller/lock/receipts with no device or export effects.
    monkeypatch.setattr('ournotes.runtime.scheduler.RuntimeStore', lambda: RuntimeStore(tmp_path))
    monkeypatch.setattr('ournotes.runtime.scheduler.STATE', tmp_path / 'state')
    monkeypatch.setattr('ournotes.runtime.scheduler.ROOT', tmp_path)
    monkeypatch.setattr('ournotes.runtime.scheduler.export_batch', lambda *args: {})
    monkeypatch.setattr('ournotes.runtime.store.RuntimeStore.repair_exports', lambda *args: None)
    monkeypatch.setattr('ournotes.reports.performance.build', lambda *args: None)
    monkeypatch.setattr('ournotes.accounts.archive.cleanup_formal_runs', lambda *args: None)
    shutdown = Mock(return_value={'status': 'closed', 'instances': [1, 3]})
    monkeypatch.setattr('ournotes.runtime.scheduler.shutdown_instances', shutdown)
    return shutdown


@pytest.mark.parametrize('source', ['command', 'before_start', 'during_run'])
@pytest.mark.parametrize('draw_status', ['complete', 'captured'])
def test_safe_pause_keeps_emulators_and_durable_work(tmp_path, scheduler_shutdown, source, draw_status):
    from ournotes.control.launcher import runner_locked
    from ournotes.control.operations import OperationStore

    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(1, 'MyGO only'), (3, 'Mujica only')], 'test', 2)
    run = db.get_run(batch['lanes'][0]['run_id'])
    account = db.reserve(run, make_plan(run['preset']))
    draw = {'id': 'saved-draw', 'account_id': account['id'], 'ordinal': 0, 'status': draw_status}
    db.save_draw(draw)
    counters = db.counters()
    operations = OperationStore(tmp_path)
    controller = MultiRunner(resume=True)
    if source == 'command':
        operations.submit('pause-keep-emulators', 'pause', {'batch_id': batch['id']}, lambda _: {})
    elif source == 'before_start':
        controller.stop.set()
    started = []
    def worker(lane):
        started.append(lane['instance'])
        controller.stop.set()
        with_store = RuntimeStore(tmp_path)
        try:
            saved = with_store.get_run(lane['run_id'])
            saved['status'] = 'paused'
            with_store.save_run(saved)
            controller.updates.put(dict(lane, status='paused', completed=saved['completed']))
        finally:
            with_store.close()
    controller.worker = worker
    controller.execute()
    scheduler_shutdown.assert_not_called()
    assert bool(started) == (source == 'during_run')
    assert db.get_batch()['status'] == 'paused'
    assert db.get_batch()['shutdown'] == {'status': 'kept_open', 'reason': 'safe_pause'}
    assert not runner_locked(tmp_path)
    assert db.counters() == counters
    assert db.account(account['id']) == account and db.draws(account['id']) == [draw]
    assert db.get_run(run['id'])['active_account'] == account['id']
    if source == 'command':
        assert operations.get('pause-keep-emulators')['status'] == 'succeeded'
    db.close()


@pytest.mark.parametrize('finish_requested', [False, True])
def test_normal_completion_still_closes_emulators(tmp_path, scheduler_shutdown, finish_requested):
    from ournotes.control.operations import OperationStore

    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(1, 'MyGO only')], 'test', 1)
    if finish_requested:
        OperationStore(tmp_path).submit('finish-close-emulators', 'finish', {'batch_id': batch['id']}, lambda _: {})
    controller = MultiRunner(resume=True)
    def worker(lane):
        controller.device_instances.add(lane['instance'])
        with_store = RuntimeStore(tmp_path)
        try:
            run = with_store.get_run(lane['run_id'])
            assert bool(run.get('finish_requested')) == finish_requested
            run['status'] = 'complete'
            with_store.save_run(run)
            controller.updates.put(dict(lane, status='complete', completed=run['completed']))
        finally:
            with_store.close()
    controller.worker = worker
    controller.execute()
    scheduler_shutdown.assert_called_once_with([1])
    assert db.get_batch()['status'] == 'complete'
    assert db.get_batch()['shutdown']['status'] == 'closed'
    db.close()


def test_resume_selected_lanes_only_resets_selected_limit(tmp_path, monkeypatch):
    from ournotes.runtime.runner import mail_limit_reached

    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(0, 'MyGO only'), (1, 'Mujica only')], 'test', 2)
    for lane in batch['lanes']:
        run = db.get_run(lane['run_id'])
        run.update(status='paused', mail_send_limit_reached=True)
        db.save_run(run)
    operations = Mock()
    operations.pending.return_value = [{'command_id': 'manual', 'parameters': {'instances': [0]}}]
    monkeypatch.setattr('ournotes.control.operations.OperationStore', lambda _: operations)
    controller = MultiRunner()
    controller.batch = batch
    pending = []
    controller.resume_lanes(db, pending, {}, {0: {}, 1: {}})
    assert pending == [batch['lanes'][0]]
    assert not mail_limit_reached(db.get_run(batch['lanes'][0]['run_id']))
    assert mail_limit_reached(db.get_run(batch['lanes'][1]['run_id']))
    db.close()


def test_finish_without_active_account_never_initializes_device(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(4, '都抽')], 'test', 2)
    lane = batch['lanes'][0]
    run = db.get_run(lane['run_id'])
    run.update(status='paused', count=0, completed=0, finish_requested=True)
    db.save_run(run)
    monkeypatch.setattr('ournotes.runtime.scheduler.RuntimeStore', lambda: RuntimeStore(tmp_path))
    initialize = Mock(side_effect=AssertionError('must not boot'))
    monkeypatch.setattr(Runner, '_initialize', initialize)
    MultiRunner(resume=True).worker(lane)
    initialize.assert_not_called()
    assert db.get_run(run['id'])['status'] == 'complete'
    db.close()


@pytest.mark.parametrize('resume', [False, True])
def test_rebind_conflict_prevents_batch_creation_or_resume_mutation(tmp_path, scheduler_shutdown, resume):
    from ournotes.common.devices import lock_held, rebind_lease
    db = RuntimeStore(tmp_path)
    if resume:
        batch = create_batch(db, [(0, 'MyGO only')], 'test', 2)
        run = db.get_run(batch['lanes'][0]['run_id'])
        run.update(status='paused', mail_send_limit_reached=True)
        db.save_run(run)
    before = list(db.db.iterdump())
    controller = MultiRunner(lanes=[(0, 'MyGO only')], resume=resume)
    controller.worker = Mock()
    try:
        with rebind_lease(tmp_path, 0), pytest.raises(ValueError, match='实例 0'):
            controller.execute()
        assert list(db.db.iterdump()) == before
        assert not lock_held(tmp_path, 'runner')
        controller.worker.assert_not_called()
        scheduler_shutdown.assert_not_called()
    finally:
        db.close()


def test_batch_can_start_after_spare_instance_rebind_and_keeps_its_lease(tmp_path, scheduler_shutdown):
    from ournotes.common.devices import lock_held, rebind_lease
    controller = MultiRunner(lanes=[(1, 'MyGO only')], count=1)
    def worker(lane):
        assert lock_held(tmp_path, 'device-0') and lock_held(tmp_path, 'device-1')
        controller.device_instances.add(lane['instance'])
        db = RuntimeStore(tmp_path)
        try:
            run = db.get_run(lane['run_id'])
            run['status'] = 'complete'
            db.save_run(run)
        finally:
            db.close()
    def shutdown(instances):
        assert lock_held(tmp_path, 'device-0') and lock_held(tmp_path, 'device-1')
        return {'status': 'closed', 'instances': instances}
    scheduler_shutdown.side_effect = shutdown
    controller.worker = worker
    with rebind_lease(tmp_path, 0):
        controller.execute()
        assert lock_held(tmp_path, 'device-0') and not lock_held(tmp_path, 'device-1')
    scheduler_shutdown.assert_called_once_with([1])
    status = json.loads((tmp_path / 'state/runner-status.json').read_text('utf-8'))
    assert status['device_lease_protocol'] == 1
