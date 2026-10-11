from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from ournotes.common.devices import instance_leases, lock_held
from ournotes.runtime.memory_maintenance import recycle_between_accounts, require_confirmed_restart
from ournotes.runtime.model import PauseRun
from ournotes.runtime.runner import Runner
from ournotes.runtime.store import RuntimeStore

MODULE = 'ournotes.runtime.memory_maintenance.'


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    r = Runner()
    r.db = RuntimeStore(tmp_path)
    r.run = r.db.create_run('MyGO only', 'test', 100)
    r.run.update(instance=2, completed=10)
    r.db.save_run(r.run)
    r.instance = 2
    r.account = None
    r.log = MagicMock()
    r.device = Mock()
    r.recognition = SimpleNamespace(jobs={})
    r.publish = Mock()
    shutdown, start, client, observer = Mock(), Mock(), Mock(), Mock()
    for name, value in [('shutdown_instances', shutdown), ('ensure_game', start),
                        ('DeviceClient', client), ('Observer', observer)]:
        monkeypatch.setattr(MODULE + name, value)
    yield SimpleNamespace(r=r, shutdown=shutdown, start=start, client=client, observer=observer)
    r.db.close()


def test_restarts_only_current_leased_instance_every_ten_completed(maintenance, tmp_path):
    m = maintenance
    r = m.r
    old_device = r.device
    def shutdown(instances):
        assert instances == [2]
        assert lock_held(tmp_path, 'device-2')
        assert not lock_held(tmp_path, 'device-1')
        old_device.close.assert_called_once()
        assert r.db.get_run(r.run['id'])['memory_restart']['status'] == 'restarting'
    m.shutdown.side_effect = shutdown
    with instance_leases(tmp_path, [2]):
        assert recycle_between_accounts(r)
    m.start.assert_called_once_with(2, r.stop)
    m.client.assert_called_once_with(2)
    r.run = r.db.get_run(r.run['id'])  # Checkpoint survives a new controller.
    assert r.run['memory_restart']['completed'] == 10
    assert r.run['memory_restart']['status'] == 'complete'
    assert r.run['completed'] == 10 and r.run['active_account'] is None
    for completed in (10, 11, 19):
        r.run['completed'] = completed
        r.db.save_run(r.run)
        assert not recycle_between_accounts(r)
    m.shutdown.side_effect = None
    r.run['completed'] = 20
    r.db.save_run(r.run)
    assert recycle_between_accounts(r)
    assert m.shutdown.call_count == 2
    assert r.db.db.execute('SELECT count(*) FROM accounts').fetchone()[0] == 0


@pytest.mark.parametrize('condition', ['nine', 'active', 'finish', 'quota', 'deadline', 'count', 'cancelled'])
def test_no_restart_when_not_due_or_no_next_work(maintenance, monkeypatch, condition):
    m, r = maintenance, maintenance.r
    if condition == 'nine':
        r.run['completed'] = 9
    elif condition == 'active':
        r.account = {'id': 'active'}
    elif condition == 'finish':
        r.run['finish_requested'] = True
    elif condition == 'quota':
        monkeypatch.setattr(r.db, 'batch_quota_exhausted', lambda _: True)
    elif condition == 'deadline':
        r.run['allocation_deadline'] = 1
    elif condition == 'count':
        r.run['count'] = 10
    else:
        r.run['status'] = 'cancelled'
    r.db.save_run(r.run)
    assert not recycle_between_accounts(r)
    m.shutdown.assert_not_called()
    m.start.assert_not_called()


def test_pause_before_maintenance_does_not_touch_device(maintenance):
    m = maintenance
    m.r.stop.set()
    with pytest.raises(PauseRun):
        recycle_between_accounts(m.r)
    m.shutdown.assert_not_called()
    m.start.assert_not_called()


def test_pause_during_shutdown_does_not_restart(maintenance):
    m = maintenance
    m.shutdown.side_effect = lambda _: m.r.stop.set()
    with pytest.raises(PauseRun, match='未重新启动'):
        recycle_between_accounts(m.r)
    m.start.assert_not_called()
    saved = m.r.db.get_run(m.r.run['id'])
    assert saved['memory_restart']['status'] == 'stopped'
    assert 'recovery_hold' not in saved
    require_confirmed_restart(saved)


def test_finish_during_shutdown_is_preserved_without_booting(maintenance):
    m, r = maintenance, maintenance.r
    def finish(_):
        saved = r.db.get_run(r.run['id'])
        saved.update(finish_requested=True, count=saved['completed'])
        r.db.save_run(saved)
    m.shutdown.side_effect = finish
    assert not recycle_between_accounts(r)
    m.start.assert_not_called()
    saved = r.db.get_run(r.run['id'])
    assert saved['memory_restart']['status'] == 'stopped'
    assert saved['finish_requested'] and saved['count'] == 10
    assert r.run['finish_requested'] and r.run['count'] == 10


@pytest.mark.parametrize('stage', ['shutdown', 'start', 'client'])
def test_uncertain_restart_is_persistent_and_not_replayed(maintenance, stage):
    m = maintenance
    getattr(m, stage).side_effect = RuntimeError('native error')
    with pytest.raises(PauseRun, match='未确认'):
        recycle_between_accounts(m.r)
    saved = m.r.db.get_run(m.r.run['id'])
    assert saved['memory_restart']['status'] == 'needs_review'
    assert saved['recovery_hold']
    with pytest.raises(ValueError, match='未确认'):
        require_confirmed_restart(saved)
    with pytest.raises(ValueError):
        recycle_between_accounts(m.r)
    assert m.shutdown.call_count == 1


def test_crash_after_restart_intent_is_not_replayed():
    with pytest.raises(ValueError, match='未确认'):
        require_confirmed_restart({'memory_restart': {'status': 'restarting'}})


def test_restart_failure_records_cli_operation(maintenance):
    import subprocess
    m = maintenance
    m.start.side_effect = subprocess.CalledProcessError(
        4294967095, ['mumu-cli.exe', 'control', '-v', '2', 'launch'])
    with pytest.raises(PauseRun):
        recycle_between_accounts(m.r)
    record = m.r.db.get_run(m.r.run['id'])['memory_restart']
    assert record['stage'] == 'startup'
    assert record['failed_command'] == ['control', '-v', '2', 'launch']
    assert record['returncode'] == 4294967095
    assert any(c.args[0] == 'memory_restart_failed' for c in m.r.log.event.call_args_list)


@pytest.mark.parametrize('pending', ['mail', 'recognition', 'active_pointer', 'account',
                                     'submitted', 'captured', 'needs_review'])
def test_unsettled_work_blocks_restart(maintenance, pending):
    m, r = maintenance, maintenance.r
    if pending == 'mail':
        r.mail_job = Mock()
    elif pending == 'recognition':
        r.recognition.jobs['draw'] = Mock()
    elif pending == 'active_pointer':
        r.run['active_account'] = 'unexpected'
        r.db.save_run(r.run)
    else:
        r.db.db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?)',
                        ('a', 'test', 'test', 1, r.run['id'],
                         'running' if pending == 'account' else 'complete', 'finished', '{}', 0))
        if pending != 'account':
            r.db.db.execute('INSERT INTO draws VALUES(?,?,?,?,?)', ('d', 'a', 0, pending, '{}'))
    with pytest.raises(PauseRun):
        recycle_between_accounts(r)
    m.shutdown.assert_not_called()
    m.start.assert_not_called()


def test_instances_count_independently(maintenance):
    r = maintenance.r
    r.run['completed'] = 9
    r.db.save_run(r.run)
    other = r.db.create_run('MyGO only', 'test', 100)
    other.update(instance=3, completed=80)
    r.db.save_run(other)
    assert not recycle_between_accounts(r)
    maintenance.shutdown.assert_not_called()


@pytest.mark.parametrize('audited', [True, False])
def test_cancelled_transactions_only_ignore_explicit_abandonment(maintenance, audited):
    import json
    m,r=maintenance,maintenance.r
    data={'submission_abandoned': {'reason':'submitted_error'}} if audited else {}
    r.db.db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?)',
                   ('a','test','test',1,r.run['id'],'cancelled','draws',json.dumps(data),0))
    r.db.db.execute('INSERT INTO draws VALUES(?,?,?,?,?)',('d','a',0,'submitted','{}'))
    if audited:
        assert recycle_between_accounts(r)
        assert r.db.db.execute('SELECT status FROM draws WHERE id="d"').fetchone()[0]=='submitted'
    else:
        with pytest.raises(PauseRun):recycle_between_accounts(r)
        m.shutdown.assert_not_called()


@pytest.mark.parametrize('info,confirmed', [({'is_process_started':False,'is_android_started':False},True),
    ({'is_process_started':True,'is_android_started':False},False), ({},False)])
def test_reconcile_only_confirmed_stopped_instance(maintenance, monkeypatch, info, confirmed):
    from ournotes.runtime.memory_maintenance import reconcile_stopped_restart
    r=maintenance.r
    r.run.update(memory_restart={'status':'needs_review','completed':10},recovery_hold='uncertain')
    monkeypatch.setattr(MODULE+'instance_info',lambda **kwargs:info)
    assert reconcile_stopped_restart(r.db,r.run) is confirmed
    assert r.run['memory_restart']['status']==('stopped' if confirmed else 'needs_review')
    maintenance.shutdown.assert_not_called()
    maintenance.start.assert_not_called()
