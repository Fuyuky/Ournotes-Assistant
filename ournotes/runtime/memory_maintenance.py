"""Recycle one leased emulator only between fully settled accounts."""
from __future__ import annotations

import time

from ournotes.integrations.device import DeviceClient
from ournotes.integrations.mumu import ensure_game, instance_info, shutdown_instances
from ournotes.runtime.model import PauseRun
from ournotes.ui.observer import Observer

ACCOUNT_INTERVAL = 10
UNCERTAIN_STATES = {'restarting', 'needs_review'}


def require_confirmed_restart(run):
    if run.get('memory_restart', {}).get('status') in UNCERTAIN_STATES:
        raise ValueError('上次内存维护重启结果未确认；请核查实例，禁止自动重复重启。')


def reconcile_stopped_restart(db, run):
    """On explicit resume, confirm a completed shutdown without repeating it."""
    record = run.get('memory_restart', {})
    if record.get('status') not in UNCERTAIN_STATES or run.get('active_account'):
        return False
    if _unsettled(db, run['id']):
        return False
    info = instance_info(instance=run['instance'])
    if (info.get('error_code', 0) != 0 or info.get('is_process_started') is not False
            or info.get('is_android_started') is not False):
        return False
    record.setdefault('reconciliation', []).append({
        'at': time.time(), 'previous_status': record['status'], 'observed': 'instance_stopped'})
    record.update(status='stopped', finished_at=time.time())
    run.pop('recovery_hold', None)
    run.pop('pause_reason', None)
    db.save_run(run)
    return True


def _unsettled(db, run_id):
    unfinished = db.db.execute(
        "SELECT 1 FROM accounts WHERE run_id=? AND status NOT IN ('complete','partial','cancelled') LIMIT 1",
        (run_id,)).fetchone()
    unresolved = db.db.execute(
        "SELECT 1 FROM draws d JOIN accounts a ON a.id=d.account_id "
        "WHERE a.run_id=? AND NOT (a.status='cancelled' AND "
        "json_extract(a.data, '$.submission_abandoned') IS NOT NULL) "
        "AND d.status IN ('planned','submitted','captured','needs_review') LIMIT 1",
        (run_id,)).fetchone()
    return bool(unfinished or unresolved)


def _more_work(r, saved):
    return not (saved['status'] != 'running' or saved.get('finish_requested')
                or (saved.get('count') is not None and saved['completed'] >= saved['count'])
                or r.db.batch_quota_exhausted(saved)
                or (saved.get('allocation_deadline') is not None
                    and time.time() >= saved['allocation_deadline']))


def recycle_between_accounts(runner):
    """Caller retains scheduler/device leases throughout shutdown and startup."""
    r = runner
    require_confirmed_restart(r.run)
    previous = r.run.get('memory_restart', {}).get('completed', 0)
    if r.account is not None or r.run['completed'] - previous < ACCOUNT_INTERVAL:
        return False
    r.check_pause()
    saved = r.db.get_run(r.run['id'])
    if saved.get('active_account'):
        raise PauseRun('内存维护发现活动账号不一致，保留现场暂停。')
    if not _more_work(r, saved):
        return False
    if _unsettled(r.db, r.run['id']) or r.mail_job is not None or r.recognition.jobs:
        raise PauseRun('内存维护前仍有未完成账号、交易或后台任务，保留现场暂停。')
    record = {'status': 'restarting', 'completed': r.run['completed'],
              'instance': r.instance, 'interval': ACCOUNT_INTERVAL, 'started_at': time.time()}
    r.run['memory_restart'] = record
    r.db.save_run(r.run)  # Interrupted native calls must never be automatically replayed.
    r.log.event('memory_restart_started', **record)
    r.publish(message='已完成10个账号，正在重启本实例释放内存')
    try:
        if r.device:
            r.device.close()
            r.device = None
        r.log.clear_frames()
        r.observation = r.wallet_observation = None
        record['stage'] = 'shutdown'
        r.db.save_run(r.run)
        with r.log.timed('memory_restart_shutdown', instance=r.instance):
            shutdown_instances([r.instance])
        # Re-read commands/quotas that may have changed while the VM shut down.
        saved = r.db.get_run(r.run['id'])
        r.run.update(saved)
        r.run['memory_restart'] = record
        if r.stop.is_set() or not _more_work(r, saved):
            record.update(status='stopped', finished_at=time.time())
            r.db.save_run(r.run)
        else:
            r.check_memory()
            record['stage'] = 'startup'
            r.db.save_run(r.run)
            with r.log.timed('memory_restart_startup', instance=r.instance):
                ensure_game(r.instance, r.stop)
                if r.stop.is_set():
                    raise PauseRun('内存维护启动期间收到暂停；保留实例状态核查。')
                r.device = DeviceClient(r.instance)
                r.vision = Observer()
            record.update(status='complete', finished_at=time.time())
            r.db.save_run(r.run)
    except Exception as error:
        record.update(status='needs_review', error_type=type(error).__name__,
                      returncode=getattr(error, 'returncode', None), finished_at=time.time())
        command = getattr(error, 'cmd', None)
        if isinstance(command, (list, tuple)):
            # These maintenance commands contain only CLI operations, instance and package.
            record['failed_command'] = [str(part) for part in command[1:]]
        r.log.event('memory_restart_failed', **record)
        r.run['recovery_hold'] = '内存维护重启结果未确认；请核查实例，禁止自动重复重启。'
        r.db.save_run(r.run)
        raise PauseRun(r.run['recovery_hold']) from error
    r.log.event('memory_restart_finished', **record)
    if record['status'] == 'stopped':
        if r.stop.is_set():
            raise PauseRun('用户暂停；内存维护已关闭本实例，未重新启动。')
        return False  # The normal allocation/count check finishes this run.
    r.last_action = r.last_scene = r.last_known_scene = None
    r.result_stability = r.loading_episode = r.draw_wait_started = r.title_start_wait = None
    r.last_progress = time.monotonic()
    r.publish(message='本实例内存维护完成，继续领取下一个账号')
    return True
