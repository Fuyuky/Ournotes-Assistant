"""Real OS locks and SQLite; all device, cloud and subprocess effects are fakes."""
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_email_rebind import Cloud, Driver, wait

from ournotes.common.devices import instance_leases, lock_held, rebind_lease
from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.rebind.service import Service, idle_batch
from ournotes.runtime.pools import make_plan
from ournotes.runtime.scheduler import create_batch
from ournotes.runtime.store import RuntimeStore


def batch_state(root, status='running'):
    db = RuntimeStore(root)
    batch = create_batch(db, [(1, '都抽'), (3, 'millsage 一期活动')], 'test', 2)
    batch['status'] = status
    db.save_batch(batch)
    atomic_json(root / 'state/runner-status.json', {
        'batch_id': batch['id'], 'status': status, 'device_lease_protocol': 1,
    })
    return db, batch


def test_instances_conflict_across_processes_and_release_on_exit(tmp_path):
    script = """
import sys
from pathlib import Path
from ournotes.common.devices import rebind_lease
with rebind_lease(Path(sys.argv[1]), 3):
    print('ready', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert child.stdout.readline().strip() == 'ready'
        assert lock_held(tmp_path, 'device-3')
        with pytest.raises(ValueError, match='实例 3'), instance_leases(tmp_path, [1, 3]):
            pytest.fail('overlapping lease acquired')
        assert not lock_held(tmp_path, 'device-1')  # Roll back partial acquisition.
        with instance_leases(tmp_path, [0]):
            assert lock_held(tmp_path, 'device-0')
    finally:
        child.kill()  # Process failure, not cooperative cleanup, must release OS locks.
        child.communicate(timeout=5)
    with rebind_lease(tmp_path, 3):
        assert lock_held(tmp_path, 'device-3')
    assert not lock_held(tmp_path, 'device-3')


@pytest.mark.parametrize('status', ['running', 'paused'])
def test_unfinished_batch_reserves_even_completed_lanes(tmp_path, status):
    db, batch = batch_state(tmp_path, status)
    try:
        run = db.get_run(batch['lanes'][0]['run_id'])
        run['status'] = 'complete'
        db.save_run(run)
        before = list(db.db.iterdump())
        with pytest.raises(ValueError, match='未结束批次'):
            idle_batch(tmp_path, 1, 1622)
        idle_batch(tmp_path, 0, 1622)
        assert list(db.db.iterdump()) == before
    finally:
        db.close()


@pytest.mark.parametrize('problem', ['running_account', 'active_account', 'submitted', 'captured', 'needs_review', 'identity'])
def test_account_guard_preserves_unresolved_records(tmp_path, problem):
    db, batch = batch_state(tmp_path)
    try:
        run = db.get_run(batch['lanes'][0]['run_id'])
        account = db.reserve(run, make_plan(run['preset']))
        account['status'] = 'running' if problem == 'running_account' else 'complete'
        db.save_account(account)
        if problem != 'active_account':
            run['active_account'] = None
            db.save_run(run)
        if problem in ('submitted', 'captured', 'needs_review'):
            db.save_draw({'id': 'pending', 'account_id': account['id'], 'ordinal': 0, 'status': problem})
        before = list(db.db.iterdump())
        with pytest.raises(ValueError):
            idle_batch(tmp_path, 0, account['alias_index'], 'other' if problem == 'identity' else account['id'])
        assert list(db.db.iterdump()) == before
    finally:
        db.close()


def test_completed_account_can_rebind_on_spare_instance_with_new_scheduler(tmp_path):
    db, batch = batch_state(tmp_path)
    try:
        run = db.get_run(batch['lanes'][0]['run_id'])
        account = db.reserve(run, make_plan(run['preset']))
        account['status'] = 'complete'
        db.save_account(account)
        run['active_account'] = None
        db.save_run(run)
        before = list(db.db.iterdump())
        with (SecretStore(tmp_path / 'state').lock('runner'),
              instance_leases(tmp_path, [1, 3]), rebind_lease(tmp_path, 0)):
            idle_batch(tmp_path, 0, account['alias_index'], account['id'])
        assert list(db.db.iterdump()) == before
    finally:
        db.close()


@pytest.mark.parametrize('problem', ['old_protocol', 'wrong_batch', 'maintenance', 'missing_status', 'stale_protocol_without_leases'])
def test_old_or_unverifiable_controller_never_allows_parallel_device(tmp_path, problem):
    db, batch = batch_state(tmp_path)
    try:
        status = {'batch_id': batch['id'], 'status': 'running', 'device_lease_protocol': 1}
        if problem == 'old_protocol':
            status.pop('device_lease_protocol')
        elif problem == 'wrong_batch':
            status['batch_id'] = 'other'
        elif problem == 'maintenance':
            batch['status'] = 'paused'
            db.save_batch(batch)
        atomic_json(tmp_path / 'state/runner-status.json', status)
        if problem == 'missing_status':
            (tmp_path / 'state/runner-status.json').unlink()
        cloud = Mock(side_effect=AssertionError('must reject before cloud/device'))
        service = Service(tmp_path, client=cloud)
        with SecretStore(tmp_path / 'state').lock('runner'):
            service.start(1, 0, 'target@gmail.com', 'a' * 32)
            wait(service, 'error')
            service.thread.join(3)
        cloud.assert_not_called()
        assert not lock_held(tmp_path, 'device-0')
        service.close()
    finally:
        db.close()


def test_rebind_owns_device_until_cleanup_while_other_batch_runs(tmp_path):
    db, _ = batch_state(tmp_path)
    cleaning, release = threading.Event(), threading.Event()
    checked = []
    class CleanupDriver(Driver):
        def close(self):
            checked.append(lock_held(tmp_path, 'device-0'))
            checked.append(lock_held(tmp_path, 'email-rebind'))
            cleaning.set()
            assert release.wait(5)
            super().close()
    cloud = Cloud()
    service = Service(tmp_path, client=lambda: cloud, driver=CleanupDriver)
    try:
        with SecretStore(tmp_path / 'state').lock('runner'), instance_leases(tmp_path, [1, 3]):
            service.start(1, 0, 'target@gmail.com', 'a' * 32)
            task = wait(service, 'waiting_b')
            service.stop(task['id'])
            assert cleaning.wait(3)
            with pytest.raises(ValueError), instance_leases(tmp_path, [0]):
                pytest.fail('cleanup still owns device')
            assert lock_held(tmp_path, 'runner')
            release.set()
            service.thread.join(3)
        assert checked == [True, True]
        assert not lock_held(tmp_path, 'device-0') and not lock_held(tmp_path, 'email-rebind')
        assert service.status()['task'] is None and cloud.writes == 0
    finally:
        release.set()
        service.close()
        db.close()


def test_shutdown_only_targets_owned_instances(monkeypatch):
    from ournotes.integrations import mumu
    command = Mock(return_value=SimpleNamespace(stdout=b'{}'))
    info = Mock(return_value={'is_process_started': False, 'is_android_started': False})
    monkeypatch.setattr(mumu.subprocess, 'run', command)
    monkeypatch.setattr(mumu, 'instance_info', info)
    assert mumu.shutdown_instances([3, 1, 3])['instances'] == [1, 3]
    assert [call.args[0][1:] for call in command.call_args_list] == [
        ['control', '-v', '1', 'shutdown'], ['control', '-v', '3', 'shutdown'],
    ]
    assert [call.args[1] for call in info.call_args_list] == [1, 3] * (info.call_count // 2)
    assert info.call_count >= 4
    command.reset_mock()
    assert mumu.shutdown_instances([])['reason'] == 'no_device_started'
    command.assert_not_called()


def test_diagnostics_cannot_bypass_rebind_instance_lock(tmp_path):
    from devtools.diagnostics import Diagnostics
    service = SimpleNamespace(root=tmp_path, running=lambda: False,
                              runtime=lambda: {'error': None, 'version': 'v', 'lanes': [], 'unresolved': []})
    sdk = Mock(side_effect=AssertionError('no device connection'))
    diagnostics = Diagnostics(service, sdk_factory=sdk)
    with rebind_lease(tmp_path, 0):
        with pytest.raises(ValueError, match='实例 0'):
            diagnostics.open(0, 'v')
        assert not lock_held(tmp_path, 'runner')
    sdk.assert_not_called()


def test_migration_does_not_rewrite_accounts_during_rebind(tmp_path, monkeypatch):
    from ournotes.maintenance import migrate
    inventory = Mock(side_effect=AssertionError('migration must not reach inventory'))
    monkeypatch.setattr(migrate, 'inventory', inventory)
    with rebind_lease(tmp_path, 0), pytest.raises(RuntimeError, match='email-rebind'):
        migrate.migrate(tmp_path, apply=True)
    inventory.assert_not_called()


@pytest.mark.parametrize('running', [True, False])
def test_shutdown_nonzero_requires_confirmed_closed_state(monkeypatch, running):
    import subprocess

    from ournotes.integrations import mumu
    command=Mock(side_effect=subprocess.CalledProcessError(1,['shutdown']))
    monkeypatch.setattr(mumu.subprocess,'run',command)
    monkeypatch.setattr(mumu,'instance_info',lambda *args:{'is_process_started':running,'is_android_started':False})
    if running:
        with pytest.raises(subprocess.CalledProcessError):mumu.shutdown_instances([1])
    else:
        assert mumu.shutdown_instances([1])['status']=='closed'
    assert command.call_count==1


def test_launch_transient_error_rechecks_state_before_retry(monkeypatch):
    import subprocess
    import threading

    from ournotes.integrations import mumu
    command=Mock(side_effect=[subprocess.CalledProcessError(1,['launch']),
        SimpleNamespace(stdout=b'{}'),SimpleNamespace(stdout=b'{"com.bilibili.sirius.official":{},"active":"com.bilibili.sirius.official"}')])
    info=Mock(side_effect=[{'is_process_started':False,'is_android_started':False},
        {'is_process_started':False,'is_android_started':False},
        {'is_process_started':True,'is_android_started':True}])
    monkeypatch.setattr(mumu.subprocess,'run',command)
    monkeypatch.setattr(mumu,'instance_info',info)
    mumu.ensure_game(1,threading.Event())
    assert command.call_count==3 and info.call_count==3


@pytest.mark.parametrize('interruption', [
    {'is_process_started': True, 'is_android_started': False},
    {},
    {'error_code': 1, 'is_process_started': False, 'is_android_started': False},
])
def test_shutdown_requires_sustained_explicit_closed_state(monkeypatch, interruption):
    from ournotes.integrations import mumu
    clock = [0.0]
    def wait(seconds):
        clock[0] += seconds
    monkeypatch.setattr(mumu, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(mumu, 'threading', SimpleNamespace(Event=lambda: SimpleNamespace(wait=wait)))
    command = Mock(return_value=SimpleNamespace(stdout=b'{}'))
    monkeypatch.setattr(mumu.subprocess, 'run', command)
    def info(*args):
        if clock[0] == 2:
            return interruption
        return {'is_process_started': False, 'is_android_started': False}
    monkeypatch.setattr(mumu, 'instance_info', info)
    assert mumu.shutdown_instances([2])['seconds'] == 5.5
    command.assert_called_once()


def test_shutdown_unknown_state_times_out_without_replaying(monkeypatch):
    from ournotes.integrations import mumu
    clock = [0.0]
    def wait(seconds):
        clock[0] += seconds
    monkeypatch.setattr(mumu, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(mumu, 'threading', SimpleNamespace(Event=lambda: SimpleNamespace(wait=wait)))
    command = Mock(return_value=SimpleNamespace(stdout=b'{}'))
    monkeypatch.setattr(mumu.subprocess, 'run', command)
    monkeypatch.setattr(mumu, 'instance_info', lambda *args: {})
    with pytest.raises(RuntimeError, match='未能持续确认'):
        mumu.shutdown_instances([2], timeout=4)
    command.assert_called_once()
