"""Recovery through the real application/queue, with synthetic state and no devices."""
import copy
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import Future
from unittest.mock import MagicMock, Mock

import pytest
import test_launcher_web as launcher_tests

from ournotes.control.operations import OperationStore
from ournotes.control.recovery import lane_recovery, runtime_recovery
from ournotes.runtime.runner import Runner
from ournotes.runtime.scheduler import MultiRunner, create_batch
from ournotes.runtime.store import RuntimeStore


@pytest.fixture
def http_control(tmp_path, monkeypatch):
    yield from launcher_tests.setup.__wrapped__(tmp_path, monkeypatch)


def recovery_state():
    batch = {'id': 'b' * 32, 'status': 'running'}
    run = {'id': 'run', 'status': 'paused', 'active_account': 'account', 'completed': 0}
    account = {'id': 'account', 'run_id': 'run', 'batch_id': batch['id'], 'status': 'running',
               'phase': 'logout', 'alias_index': 2045, 'context': {'logout_requested': True}}
    return batch, run, account


@pytest.mark.parametrize('status', ['submitted', 'captured', 'needs_review'])
def test_unresolved_transaction_blocks_both_modes(status):
    capability = lane_recovery(*recovery_state(), [{'id': 'original', 'status': status}])
    assert not capability['retry']['enabled'] and not capability['restart_game']['enabled']
    assert '原交易' in capability['retry']['reason']


def test_otp_budget_and_uncertain_login_cannot_be_cleared_by_restart():
    batch, run, account = recovery_state()
    account.update(phase='login', context={'otp': {'expires_at': 10}, 'otp_retry': {'used': True}})
    capability = lane_recovery(batch, run, account, [], now=20)
    assert not capability['retry']['enabled'] and '一次重发' in capability['retry']['reason']
    account['context']['login_submitted'] = True
    capability = lane_recovery(batch, run, account, [], now=20)
    assert capability['retry']['enabled'] and not capability['restart_game']['enabled']
    run['recovery_request'] = {'status': 'starting'}
    assert not lane_recovery(batch, run, account, [])['retry']['enabled']


def test_other_lane_progress_does_not_change_selected_version():
    batch, run, account = recovery_state()
    old = lane_recovery(batch, run, account, [])['version']
    batch.update(active_seconds=1000, lanes=[{'instance': 1, 'completed': 50}])
    assert lane_recovery(batch, run, account, [])['version'] == old


def test_network_uncertain_login_blocks_both_modes_and_changes_version():
    batch, run, account = recovery_state()
    account['phase'] = 'login'
    before = lane_recovery(batch, run, account, [])
    account['context']['login_network_uncertain'] = True
    after = lane_recovery(batch, run, account, [])
    assert after['version'] != before['version']
    for mode in ('retry', 'restart_game'):
        assert not after[mode]['enabled']
        assert '原票据' in after[mode]['reason']


def prepare_http(http_control, active=True):
    client, service, root, calls, processes = http_control
    with sqlite3.connect(root / 'state/runtime.sqlite3') as db:
        db.execute('ALTER TABLE accounts ADD COLUMN data TEXT')
        account = {'id': 'a2', 'run_id': 'r1', 'batch_id': 'b' * 32, 'status': 'running',
                   'phase': 'logout', 'alias_index': 2, 'context': {}}
        db.execute("UPDATE accounts SET status='running',phase='logout',data=? WHERE id='a2'", (json.dumps(account),))
    (root / 'state/runner-status.json').write_text(json.dumps({'batch_id': 'b'*32, 'recovery_protocol': 1}))
    service.busy_probe = lambda: active
    return client, service, root, calls, processes


def body(client, **extra):
    data = client.get('/api/launcher/status').json()
    return {'action': 'resume_lane', 'instance': 0, 'batch_id': data['batch']['id'],
            'command_id': 'recover-example-001', 'expected_version': data['lanes'][0]['recovery']['version'], **extra}


def test_active_controller_queues_only_selected_lane_and_duplicate_is_idempotent(http_control):
    client, service, root, calls, _ = prepare_http(http_control)
    original = hashlib.sha256((root / 'state/runtime.sqlite3').read_bytes()).hexdigest()
    request = body(client, recovery_mode='restart_game')
    try:
        reply = client.post('/api/launcher/action', json=request)
        assert reply.status_code == 200 and reply.json()['operation']['status'] == 'accepted'
        assert client.post('/api/launcher/action', json=request).json() == reply.json()
        command = service.operations.pending('b' * 32, ('resume_lanes',))[0]
        assert command['parameters']['instances'] == [0]
        assert command['parameters']['recovery_mode'] == 'restart_game'
        assert calls == []
        assert hashlib.sha256((root / 'state/runtime.sqlite3').read_bytes()).hexdigest() == original
    finally:
        service.busy_probe = lambda: False


def test_offline_controller_starts_only_bound_command(http_control):
    client, service, _, calls, _ = prepare_http(http_control, active=False)
    request = body(client)
    reply = client.post('/api/launcher/action', json=request)
    assert reply.status_code == 200 and reply.json()['operation']['status'] == 'running'
    argv = calls[0][0]
    assert argv[-3:] == ['--resume', '--recovery-command', request['command_id']]
    assert service.owned_running()


def test_old_controller_and_stale_lane_version_are_rejected(http_control):
    client, service, root, calls, _ = prepare_http(http_control)
    request = body(client)
    try:
        with sqlite3.connect(root / 'state/runtime.sqlite3') as db:
            run = json.loads(db.execute("SELECT data FROM runs WHERE id='r1'").fetchone()[0])
            run['active_account'] = None
            db.execute("UPDATE runs SET data=? WHERE id='r1'", (json.dumps(run),))
        assert client.post('/api/launcher/action', json=request).status_code == 409
        request = body(client, command_id='recover-example-002')
        (root / 'state/runner-status.json').write_text('{}')
        reply = client.post('/api/launcher/action', json=request)
        assert reply.status_code == 409 and '调度器' in reply.json()['detail']
        assert calls == [] and not service.operations.pending('b'*32, ('resume_lanes',))
    finally:
        service.busy_probe = lambda: False


@pytest.fixture
def durable(tmp_path):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(2, '都抽'), (4, 'millsage 一期活动')], 'test', None)
    for lane in batch['lanes']:
        run = db.get_run(lane['run_id'])
        run.update(status='paused')
        db.save_run(run)
    yield db, batch
    db.close()


def test_scheduler_waits_for_original_worker_and_revalidates(durable):
    db, batch = durable
    controller = MultiRunner(resume=True)
    controller.batch = batch
    lane = batch['lanes'][0]
    run = db.get_run(lane['run_id'])
    params = {'batch_id': batch['id'], 'instances': [2], 'recovery_mode': 'retry',
              'expected_version': runtime_recovery(db, batch, run)['version']}
    operations = OperationStore(db.root)
    operations.submit('recovery-scheduler', 'resume_lanes', params, lambda _: {})
    active = {2: Future(), 4: Future()}
    pending, states = [], {2: {}, 4: {'status': 'running'}}
    peer = copy.deepcopy(db.get_run(batch['lanes'][1]['run_id']))
    controller.resume_lanes(db, pending, active, states)
    assert not pending
    active[2].set_result(None)
    controller.resume_lanes(db, pending, active, states)
    assert pending == [lane] and not active[4].done()
    assert db.get_run(peer['id']) == peer and not controller.stop.is_set()
    assert operations.get('recovery-scheduler')['status'] == 'running'
    assert db.get_run(run['id'])['recovery_request']['status'] == 'requested'


def test_offline_selected_scope_and_no_global_shutdown(durable, monkeypatch):
    db, batch = durable
    batch['status'] = 'paused'
    db.save_batch(batch)
    lane = batch['lanes'][0]
    params = {'batch_id': batch['id'], 'instances': [2], 'recovery_mode': 'retry',
              'expected_version': runtime_recovery(db, batch, db.get_run(lane['run_id']))['version']}
    operations = OperationStore(db.root)
    operations.submit('recovery-offline', 'resume_lanes', params, lambda _: {'status': 'running'})
    monkeypatch.setattr('ournotes.runtime.scheduler.RuntimeStore', lambda: RuntimeStore(db.root))
    monkeypatch.setattr('ournotes.runtime.scheduler.STATE', db.root / 'state')
    monkeypatch.setattr('ournotes.runtime.scheduler.export_batch', lambda *args: {})
    monkeypatch.setattr('ournotes.runtime.store.RuntimeStore.repair_exports', lambda *args: None)
    monkeypatch.setattr('ournotes.reports.performance.build', lambda *args: None)
    monkeypatch.setattr('ournotes.accounts.archive.cleanup_formal_runs', lambda *args: None)
    shutdown = Mock(side_effect=AssertionError('must not close peers'))
    monkeypatch.setattr('ournotes.runtime.scheduler.shutdown_instances', shutdown)
    started = []
    controller = MultiRunner(resume=True, recovery_command='recovery-offline')
    def worker(selected):
        started.append(selected['instance'])
        controller.updates.put(dict(selected, status='paused', completed=0))
    monkeypatch.setattr(controller, 'worker', worker)
    controller.execute()
    assert started == [2] and controller.batch['shutdown']['status'] == 'kept_open'
    assert 'recovery_request' not in db.get_run(batch['lanes'][1]['run_id'])
    shutdown.assert_not_called()


def test_manual_restart_persists_intent_and_preserves_plan_and_budgets(durable, monkeypatch):
    db, batch = durable
    lane = batch['lanes'][0]
    run = db.get_run(lane['run_id'])
    account = {'id': 'account', 'run_id': run['id'], 'batch_id': batch['id'], 'status': 'running',
               'phase': 'draws', 'alias_index': 99, 'plan': [{'saved': True}], 'game_uid': 'private-uid',
               'context': {'otp_retry': {'used': True}, 'game_restart_attempts': 1, 'uid_verified': True}}
    db.db.execute('INSERT INTO accounts(id,alias,channel,alias_index,run_id,status,phase,data) VALUES(?,?,?,?,?,?,?,?)',
                  ('account','synthetic','test',99,run['id'],'running','draws',json.dumps(account)))
    run['active_account'] = account['id']
    run['recovery_request'] = {'status': 'requested', 'mode': 'restart_game', 'account_id': account['id'],
                               'command_id': 'recovery-restart'}
    db.save_run(run)
    operations = OperationStore(db.root)
    operations.submit('recovery-restart', 'resume_lanes', {'batch_id': batch['id']}, lambda _: {})
    monkeypatch.setattr('ournotes.runtime.runner.ROOT', db.root)
    ensure = Mock()
    monkeypatch.setattr('ournotes.runtime.runner.ensure_game', ensure)
    def restart(instance, stop):
        assert instance == 2 and db.get_run(run['id'])['recovery_request']['status'] == 'starting'
        assert not stop.is_set()
    effect = Mock(side_effect=restart)
    monkeypatch.setattr('ournotes.runtime.runner.restart_game', effect)
    runner = Runner()
    runner.db, runner.run, runner.account = db, run, account
    runner.instance, runner.stop, runner.log = 2, threading.Event(), MagicMock()
    runner.publish = Mock()
    runner.apply_manual_recovery()
    runner.apply_manual_recovery()
    effect.assert_called_once()
    saved = db.account(account['id'])
    assert saved['plan'] == account['plan'] and saved['game_uid'] == 'private-uid' and saved['alias_index'] == 99
    assert saved['phase'] == 'rewards' and not saved['context']['uid_verified']
    assert saved['context']['otp_retry'] == {'used': True} and saved['context']['game_restart_attempts'] == 1
    assert operations.get('recovery-restart')['status'] == 'succeeded'


def test_commands_are_not_consumable_before_validation_or_overwrite_worker_ack(tmp_path):
    operations = OperationStore(tmp_path)
    def apply(command_id):
        assert not operations.pending('batch', ('resume_lanes',))
        operations.mark(command_id, 'succeeded', {'message': 'worker acknowledged'})
        return {'status': 'running'}
    reply = operations.submit('recovery-race', 'resume_lanes', {'batch_id': 'batch'}, apply)
    assert reply['status'] == 'succeeded' and reply['result']['message'] == 'worker acknowledged'


def test_restart_failure_keeps_uncertain_intent_without_replay(durable, monkeypatch):
    db, batch = durable
    run = db.get_run(batch['lanes'][0]['run_id'])
    run['recovery_request'] = {'status': 'requested', 'mode': 'restart_game', 'account_id': None,
                               'command_id': 'recovery-crash'}
    db.save_run(run)
    operations = OperationStore(db.root)
    operations.submit('recovery-crash', 'resume_lanes', {}, lambda _: {})
    runner = Runner()
    runner.db, runner.run, runner.account, runner.instance = db, run, None, 2
    runner.log, runner.publish = MagicMock(), Mock()
    monkeypatch.setattr('ournotes.runtime.runner.ensure_game', Mock())
    effect = Mock(side_effect=RuntimeError('CLI result uncertain'))
    monkeypatch.setattr('ournotes.runtime.runner.restart_game', effect)
    with pytest.raises(RuntimeError, match='uncertain'):
        runner.apply_manual_recovery()
    assert db.get_run(run['id'])['recovery_request']['status'] == 'starting'
    assert not runtime_recovery(db, batch, db.get_run(run['id']))['retry']['enabled']
    runner.apply_manual_recovery()
    effect.assert_called_once()
    assert operations.get('recovery-crash')['status'] == 'failed'
