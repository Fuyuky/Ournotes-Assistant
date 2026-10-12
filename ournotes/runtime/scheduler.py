"""One controller, independent MuMu lanes, shared durable alias allocation."""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import queue
import random
import threading
import time
import traceback
import uuid
from contextlib import ExitStack

from ournotes.common.devices import DEVICE_LEASE_PROTOCOL, instance_leases
from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.integrations.mumu import shutdown_instances
from ournotes.paths import DATA_ROOT as ROOT
from ournotes.paths import STATE
from ournotes.runtime.pools import make_plan
from ournotes.runtime.runner import MAIL_LIMIT_MESSAGE, Runner, mail_limit_reached
from ournotes.runtime.store import RuntimeStore

DEFAULT_LANES = [(0, "MyGO only"), (1, "Mujica only"), (2, "都抽")]


def clear_mail_limit_for_resume(db, run):
    """Only explicit control requests may reset a stopped client's mail limit."""
    if not mail_limit_reached(run):
        return
    if run.get('active_account'):
        account = db.account(run['active_account'])
        ctx = account.get('context', {})
        if (account.get('phase') == 'login' and ctx.get('proxy_task_id') and ctx.get('otp')
                and ctx.get('send_intent') and not ctx.get('login_submitted')):
            ctx.setdefault('proxy_limit_retry', {'original': dict(ctx['otp']), 'reason': 'proxy_limit',
                                                'stage': 'rotate', 'requested_at': time.time()})
            db.save_account(account)
    run['mail_limit_reset'] = {'at': time.time(), 'previous_reason': run.get('pause_reason', '')}
    run.pop('mail_send_limit_reached', None)
    if MAIL_LIMIT_MESSAGE in run.get('pause_reason', ''):
        run.pop('pause_reason', None)
    db.save_run(run)


def create_batch(db, lanes, channel, count, *, carry_from=None, debug_mode=False, preset_version=None):
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError("总账号数必须为正整数，持续运行请使用0。")
    if not lanes or len({i for i, _ in lanes}) != len(lanes) or any(i < 0 for i, _ in lanes):
        raise ValueError("必须指定互不重复、非负的 MuMu 实例编号。")
    from ournotes.integrations.cloud_client import CloudClient, CloudClientConfig
    from ournotes.runtime.presets import expand_preset
    preset_keys = {}
    if preset_version is not None or CloudClientConfig.load(db.root) is not None:
        snapshot = CloudClient.from_root(db.root).presets()
        if preset_version is not None and snapshot['version'] != preset_version:
            raise ValueError('共享预设已更新，请刷新后重新启动。')
        definitions = {p['name']: p for p in snapshot['presets'].values()}
        preset_keys = {p['name']: key for key, p in snapshot['presets'].items()}
        try:
            plans = {name: expand_preset(definitions[name]) for _, name in lanes}
        except KeyError:
            raise ValueError('所选共享预设已删除，请刷新后重新选择。') from None
        preset_version = snapshot['version']
    else:
        # Pure local fixtures and first-run configuration only; allocation still requires cloud.
        plans = {name: make_plan(name) for _, name in lanes}
    batch = {"id": uuid.uuid4().hex, "created_at": time.time(), "status": "running", "lanes": [],
             "active_seconds": 0, "total_count": count, "count_mode": "shared", "debug_mode": debug_mode}
    with db.transaction():
        previous = db.get_batch()
        if db.unfinished_runs() or (previous and previous["status"] not in ("complete", "cancelled")):
            raise ValueError("已有未结束任务，请先恢复。")
        carried = {}
        if carry_from:
            if not previous or previous['id'] != carry_from or previous['status'] != 'cancelled':
                raise ValueError('只能显式接续刚结束的当前批次。')
            for lane in previous['lanes']:
                old_run = db.get_run(lane['run_id'])
                if mail_limit_reached(old_run):
                    raise ValueError(MAIL_LIMIT_MESSAGE + '；禁止换批绕过限额。')
                if not old_run.get('active_account'):
                    continue
                account = db.account(old_run['active_account'])
                if (not account or account['run_id'] != old_run['id']
                        or account.get('batch_id') != carry_from or account['status'] != 'running'
                        or account['channel'] != channel
                        or (lane['instance'], account['preset']) not in lanes):
                    raise ValueError('接续账号的批次、实例、预设或状态不一致。')
                if any(d['status'] in ('captured', 'needs_review') for d in db.draws(account['id'])):
                    raise ValueError('已保存结果必须先完成离线复核，不能通过换批丢弃识别缓存。')
                carried[lane['instance']] = (old_run, account)
            if count is not None and count < len(carried):
                raise ValueError('目标不能小于接续账号数。')
        for instance, preset in lanes:
            run = db.create_run(preset, channel, None)
            run.update(instance=instance, batch_id=batch["id"], debug_mode=debug_mode,
                       preset_plan=plans[preset], preset_version=preset_version, preset_key=preset_keys.get(preset))
            if instance in carried:
                old_run, account = carried[instance]
                audit = {'from_batch': carry_from, 'from_run': old_run['id'],
                         'to_batch': batch['id'], 'to_run': run['id'], 'at': time.time(),
                         'account_id': account['id'], 'index': account['alias_index']}
                account.setdefault('batch_transfers', []).append(audit)
                account.update(run_id=run['id'], batch_id=batch['id'])
                db.db.execute('UPDATE accounts SET run_id=? WHERE id=?', (run['id'], account['id']))
                db.save_account(account)
                run.update(active_account=account['id'], status='paused')
                if any(d['status'] == 'submitted' for d in db.draws(account['id'])):
                    run['recovery_hold'] = '接续账号存在已提交但无结算证据的交易；保留待复核，禁止自动补抽。'
                    run['pause_reason'] = run['recovery_hold']
                old_run['active_account'] = None
                old_run.setdefault('transferred_accounts', []).append(audit)
                db.save_run(old_run)
                previous.setdefault('transferred_accounts', []).append(audit)
                batch.setdefault('carried_accounts', []).append(audit)
            db.save_run(run)
            batch["lanes"].append({"instance": instance, "preset": preset, "run_id": run["id"]})
        db.save_batch(batch)
        if carry_from:
            db.save_batch(previous)
    return batch


def preserve_unsettled_devices(db, batch):
    """A pause must not destroy the only still-visible evidence for a submission."""
    return any(d['status'] == 'submitted'
               for lane in batch['lanes']
               if (account_id := db.get_run(lane['run_id']).get('active_account'))
               for d in db.draws(account_id))


def exclude_active_accounts(db, batch_id, indices, reason):
    """Explicit retirement under the caller's runner lock; never count as success."""
    if not indices or len(set(indices)) != len(indices) or not reason:
        raise ValueError('须指定互不重复的在途账号及排除原因。')
    with db.transaction():
        batch = db.get_batch()
        if not batch or batch['id'] != batch_id or batch['status'] != 'paused':
            raise ValueError('只能排除当前已暂停批次的在途账号。')
        selected = []
        for lane in batch['lanes']:
            run = db.get_run(lane['run_id'])
            if run['status'] not in ('paused', 'complete'):
                raise ValueError('等待全部实例安全暂停后再排除账号。')
            account = db.account(run['active_account']) if run.get('active_account') else None
            if account and account['alias_index'] in indices:
                if (account['status'] != 'running' or account['batch_id'] != batch_id
                        or account['run_id'] != run['id']):
                    raise ValueError('在途账号归属或状态不一致。')
                selected.append((run, account))
        if {a['alias_index'] for _, a in selected} != set(indices):
            raise ValueError('指定账号不全是本批在途账号，未修改任何记录。')
        for run, account in selected:
            audit = {'batch_id': batch_id, 'account_id': account['id'], 'index': account['alias_index'],
                     'instance': run['instance'], 'at': time.time(), 'reason': reason,
                     'previous_status': account['status'], 'phase': account['phase']}
            account.update(status='cancelled', batch_exclusion=audit)
            db.save_account(account)
            run['active_account'] = None
            run.pop('recovery_hold', None)
            run.pop('pause_reason', None)
            run.setdefault('excluded_accounts', []).append(audit)
            db.save_run(run)
            batch.setdefault('excluded_accounts', []).append(audit)
        db.save_batch(batch)
    return [a['batch_exclusion'] for _, a in selected]


def export_batch(db, batch):
    """Write aggregate runtime metrics only; account records live on the server."""
    runs = [db.get_run(lane['run_id']) for lane in batch['lanes']]
    totals = {kind: {rarity: sum(run.get('cloud_totals', {}).get(kind, {}).get(rarity, 0)
                               for run in runs) for rarity in ('SSR', 'SR', 'R')}
              for kind in ('member', 'support')}
    result = dict(batch, accounts=[], totals=totals, summary=[],
                  completed_accounts=sum(run.get('cloud_confirmed', 0) for run in runs),
                  processed_accounts=sum(run.get('completed', 0) for run in runs),
                  pulls=sum(sum(counts.values()) for counts in totals.values()), unknown_slots=0)
    atomic_json(db.root / 'results/batches' / batch['id'] / 'results.json', result)
    return result


class MultiRunner:
    def __init__(self, lanes=None, channel="test", count=1, resume=False, callback=None, async_start=None,
                 recovery_command=None, progress_hook=None, task_gate=None, debug_mode=False, preset_version=None):
        self.lanes = DEFAULT_LANES if lanes is None else lanes
        self.channel, self.count, self.resume = channel, count, resume
        self.callback = callback or (lambda _: None)
        self.async_start = async_start
        self.debug_mode = debug_mode
        self.stop = threading.Event()
        self.updates = queue.Queue()
        self.control_ids = []
        self.recovery_command = recovery_command
        self.selected_instances = None
        self.device_instances = set()
        self.progress_hook = progress_hook
        self.task_gate = task_gate
        self.preset_version = preset_version

    def worker(self, lane):
        runner = Runner()
        try:
            # A small shared batch may exhaust its slots before staggered lanes
            # start. Do not boot a device merely to discover there is no work.
            with_db = RuntimeStore()
            try:
                run = with_db.get_run(lane['run_id'])
                if run.get('recovery_hold'):
                    self.updates.put(dict(lane, status='paused', completed=run['completed'],
                                          message=run['recovery_hold']))
                    return
                if (not run.get('active_account') and run.get('finish_requested')
                        and run['count'] is not None and run['completed'] >= run['count']):
                    run.update(status='complete', end_reason='finish_requested', finished_at=time.time())
                    with_db.save_run(run)
                    self.updates.put(dict(lane, status='complete', completed=run['completed'],
                                          message='当前账号已收尾，运行结束'))
                    self.acknowledge_no_work(with_db, run)
                    return
                if not run.get('active_account') and with_db.batch_quota_exhausted(run):
                    run.update(status='complete', end_reason='batch_quota', finished_at=time.time())
                    with_db.save_run(run)
                    self.updates.put(dict(lane, status='complete', completed=run['completed'],
                                          message='批次总额度已分配完毕'))
                    self.acknowledge_no_work(with_db, run)
                    return
            finally:
                with_db.close()
            runner._initialize(run_id=lane["run_id"], resume=self.resume or run['status'] == 'paused',
                               instance=lane["instance"], stop=self.stop, callback=self.updates.put)
            runner.task_gate = self.task_gate
            runner._execute()
        except Exception as error:  # noqa: BLE001 - persist initialization failure at the worker boundary
            db = RuntimeStore()
            try:
                run = db.get_run(lane["run_id"])
                detail = str(error) if isinstance(error, ValueError) else type(error).__name__
                request = run.get('recovery_request', {})
                if request.get('status') == 'requested':
                    request.update(status='failed', finished_at=time.time())
                    from ournotes.control.operations import OperationStore
                    OperationStore(db.root).mark(request['command_id'], 'failed',
                        {'message': '工作线程初始化失败，尚未执行恢复；请检查实例暂停原因。'})
                run.update(status="paused", pause_reason=f"初始化失败：{detail}")
                db.save_run(run)
                atomic_json(db.root / 'state/runs' / run['id'] / 'initialization-error.json',
                            {'error_type': type(error).__name__, 'time': time.time(),
                             'trace': traceback.format_tb(error.__traceback__)})
                self.updates.put(dict(lane, status="paused", completed=run["completed"],
                                      message=run["pause_reason"]))
            finally:
                db.close()
                if runner.db is not None:
                    runner.db.close()
        finally:
            if runner.device_touched:
                self.device_instances.add(lane['instance'])

    def acknowledge_no_work(self, db, run):
        request = run.get('recovery_request', {})
        if request.get('status') != 'requested':
            return
        request.update(status='complete', finished_at=time.time(), device_action=False)
        run.setdefault('recovery_audit', []).append(dict(request))
        db.save_run(run)
        from ournotes.control.operations import OperationStore
        OperationStore(db.root).mark(request['command_id'], 'succeeded',
            {'message': '该实例已无待执行任务，未启动或重启游戏。', 'instances': [run['instance']]})

    def controls(self, db):
        from ournotes.control.operations import OperationStore
        operations = OperationStore(db.root)
        for request in operations.pending(self.batch['id']):
            operations.mark(request['command_id'], 'running')
            self.control_ids.append((request['command_id'], request['action']))
            if request['action'] == "pause":
                self.stop.set()
            else:
                for lane in self.batch["lanes"]:
                    run = db.get_run(lane["run_id"])
                    if run["status"] != "complete":
                        run.update(count=run['completed'] + (1 if run['active_account'] else 0), finish_requested=True)
                        db.save_run(run)

    def publish(self, db, states):
        value = {"batch_id": self.batch["id"], "status": self.batch["status"],
                     "recovery_protocol": 1,
                     "device_lease_protocol": DEVICE_LEASE_PROTOCOL,
                     "count_mode": self.batch.get("count_mode", "per_lane"),
                     "total_count": self.batch.get("total_count"),
                     "async_start": self.batch.get("async_start", False),
                     "lanes": [states[lane["instance"]] for lane in self.batch["lanes"]],
                     "counters": db.counters(), "completed": sum(x.get("completed", 0) for x in states.values()),
                     "results": str(ROOT / "results/batches" / self.batch["id"] / "results.json")}
        atomic_json(STATE / "runner-status.json", value)
        self.callback(value)

    def resume_lanes(self, db, pending, active, states):
        """Explicitly resume an inspected paused lane without interrupting peers."""
        from ournotes.control.operations import OperationStore
        operations = OperationStore(db.root)
        for request in operations.pending(self.batch['id'], actions=('resume_lanes',)):
            selected = [lane for lane in self.batch['lanes'] if lane['instance'] in request['parameters']['instances']]
            if any(lane['instance'] in active and not active[lane['instance']].done() for lane in selected):
                continue
            operations.mark(request['command_id'], 'running')
            try:
                self.prepare_recovery(db, request, selected, pending)
            except ValueError as error:
                operations.mark(request['command_id'], 'failed', {'message': str(error)})
                continue
            for lane in selected:
                run = db.get_run(lane['run_id'])
                if lane not in pending and run['status'] == 'paused' and not run.get('recovery_hold'):
                    clear_mail_limit_for_resume(db, run)
                    pending.append(lane)
                    states[lane['instance']].update(status='waiting_start', message='恢复已复核的暂停实例')
            if not request['parameters'].get('expected_version'):
                operations.mark(request['command_id'], 'succeeded', {'instances': [lane['instance'] for lane in selected]})

    def prepare_recovery(self, db, request, selected, pending=()):
        from ournotes.control.recovery import require_recovery, runtime_recovery
        parameters = request['parameters']
        if ({lane['instance'] for lane in selected} != set(parameters['instances'])
                or not selected or any(lane in pending for lane in selected)):
            raise ValueError('实例不存在或已在等待恢复，请刷新状态。')
        # Check all selected lanes before changing any of them, under the controller lock.
        with db.transaction():
            runs = [db.get_run(lane['run_id']) for lane in selected]
            for run in runs:
                require_recovery(runtime_recovery(db, self.batch, run),
                                 parameters.get('recovery_mode', 'retry'), parameters.get('expected_version'))
            if parameters.get('expected_version'):
                for run in runs:
                    run['recovery_request'] = {'command_id': request['command_id'],
                        'mode': parameters.get('recovery_mode', 'retry'), 'status': 'requested',
                        'account_id': run.get('active_account'), 'requested_at': time.time()}
                    db.save_run(run)

    def execute(self):
        with SecretStore(STATE).lock("runner"), ExitStack() as devices:
            db = RuntimeStore()
            try:
                if self.resume:
                    self.batch = db.get_batch()
                    if not self.batch or self.batch["status"] in ("complete", "cancelled"):
                        raise ValueError("没有可恢复的多开批次。")
                    devices.enter_context(instance_leases(db.root, [lane['instance'] for lane in self.batch['lanes']]))
                else:
                    # Acquire every instance before creating a batch or allocating any work.
                    devices.enter_context(instance_leases(db.root, [instance for instance, _ in self.lanes]))
                    self.batch = create_batch(db, self.lanes, self.channel, self.count, debug_mode=self.debug_mode,
                                              preset_version=self.preset_version)
                if self.resume:
                    if self.recovery_command:
                        from ournotes.control.operations import OperationStore
                        operations = OperationStore(db.root)
                        request = operations.get(self.recovery_command)
                        # Parameters are private command data, not part of the public receipt.
                        action, parameters = operations.parameters(self.recovery_command)
                        if (action != 'resume_lanes' or request['status'] not in ('running', 'accepted')
                                or parameters.get('batch_id') != self.batch['id']):
                            raise ValueError('恢复回执不属于当前批次或已执行，禁止重复恢复。')
                        selected = [l for l in self.batch['lanes'] if l['instance'] in parameters['instances']]
                        try:
                            self.prepare_recovery(db, {'command_id': self.recovery_command,
                                'parameters': parameters}, selected)
                        except ValueError as error:
                            operations.mark(self.recovery_command, 'failed', {'message': str(error)})
                            raise
                        self.selected_instances = set(parameters['instances'])
                    for lane in self.batch['lanes']:
                        if self.selected_instances is not None and lane['instance'] not in self.selected_instances:
                            continue
                        run = db.get_run(lane['run_id'])
                        from ournotes.runtime.memory_maintenance import reconcile_stopped_restart
                        reconcile_stopped_restart(db, run)
                        from ournotes.runtime.abandon import SUBMISSION_ERRORS, retire_submission_error
                        reason = run.get('pause_reason', '')
                        if (run.get('active_account') and
                                any(reason.startswith(prefix) for prefix in SUBMISSION_ERRORS)):
                            retire_submission_error(db, run, db.account(run['active_account']), reason)
                        if run['status'] not in ('complete', 'cancelled') and not run.get('recovery_hold'):
                            clear_mail_limit_for_resume(db, run)
                db.repair_exports()
                close_devices = True
                if self.async_start is None:
                    self.async_start = self.batch.get("async_start", False)
                self.batch["async_start"] = self.async_start
                self.batch["status"] = "running"
                db.save_batch(self.batch)
                states = {lane["instance"]: dict(lane, status=db.get_run(lane["run_id"])["status"],
                          completed=db.get_run(lane["run_id"])["completed"])
                          for lane in self.batch["lanes"]}
                for lane in self.batch['lanes']:
                    if hold := db.get_run(lane['run_id']).get('recovery_hold'):
                        states[lane['instance']].update(status='paused', message=hold)
                if self.progress_hook:
                    self.progress_hook(db, self.batch)
                self.publish(db, states)
                started = time.monotonic()
                previous_seconds = self.batch["active_seconds"]
                self.controls(db)
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(states), thread_name_prefix="mumu") as pool:
                    pending = [lane for lane in self.batch["lanes"]
                               if (self.selected_instances is None or lane['instance'] in self.selected_instances)
                               and states[lane["instance"]]["status"] not in ("complete", "cancelled")
                               and not db.get_run(lane['run_id']).get('recovery_hold')]
                    futures = []
                    active = {}
                    next_start = time.monotonic()
                    for lane in pending:
                        states[lane["instance"]].update(status="waiting_start", message="等待按顺序启动")
                    self.publish(db, states)
                    while pending or any(not f.done() for f in futures) or not self.updates.empty():
                        try:
                            self.controls(db)
                            if not self.stop.is_set():
                                self.resume_lanes(db, pending, active, states)
                            if pending and self.stop.is_set():
                                for lane in pending:
                                    run = db.get_run(lane["run_id"])
                                    request = run.get('recovery_request', {})
                                    if request.get('status') == 'requested':
                                        request.update(status='failed', finished_at=time.time())
                                        from ournotes.control.operations import OperationStore
                                        OperationStore(db.root).mark(request['command_id'], 'failed',
                                            {'message': '启动前收到安全暂停；未执行恢复。'})
                                    run.update(status="paused", pause_reason="用户暂停，实例尚未启动。")
                                    db.save_run(run)
                                    states[lane["instance"]].update(status="paused", message=run["pause_reason"])
                                pending.clear()
                                self.publish(db, states)
                            # Stagger only worker startup; keep processing controls and running lanes.
                            while pending and not self.stop.is_set() and time.monotonic() >= next_start:
                                lane = pending.pop(0)
                                states[lane["instance"]].update(status="starting", message="正在启动模拟器")
                                future = pool.submit(self.worker, lane)
                                futures.append(future)
                                active[lane['instance']] = future
                                delay = random.uniform(60, 90) if self.async_start and pending else 0
                                next_start = time.monotonic() + delay
                                if pending and self.async_start:
                                    states[pending[0]["instance"]].update(
                                        message=f"异步启动：间隔 {delay:.1f} 秒后启动",
                                        startup_not_before=time.time() + delay)
                                self.publish(db, states)
                            item = self.updates.get(timeout=.25)
                            previous = states[item["instance"]]
                            states[item["instance"]] = item
                            self.batch["active_seconds"] = previous_seconds + time.monotonic() - started
                            db.save_batch(self.batch)
                            if item.get("completed", 0) != previous.get("completed", 0):
                                export_batch(db, self.batch)
                                if self.progress_hook:
                                    try:
                                        self.progress_hook(db, self.batch)
                                    except (OSError, ValueError, RuntimeError):
                                        self.batch['extension_error'] = '代理节点切换未确认，已请求安全暂停。'
                                        db.save_batch(self.batch)
                                        self.stop.set()
                            self.publish(db, states)
                        except queue.Empty:
                            continue
                        except KeyboardInterrupt:
                            self.stop.set()
                        except BaseException:
                            self.stop.set()
                            raise
                    for future in futures:
                        future.result()
                self.batch["active_seconds"] = previous_seconds + time.monotonic() - started
                self.batch["status"] = ("complete" if all(db.get_run(l["run_id"])["status"] == "complete"
                                                          for l in self.batch["lanes"]) else "paused")
                for lane in self.batch["lanes"]:
                    run = db.get_run(lane["run_id"])
                    states[lane["instance"]].update(status=run["status"], completed=run["completed"])
                if self.batch["status"] == "complete":
                    self.batch["finished_at"] = time.time()
                db.save_batch(self.batch)
                db.repair_exports()
                export_batch(db, self.batch)
                self.publish(db, states)
                from ournotes.control.operations import OperationStore
                operations = OperationStore(db.root)
                for command_id, action in self.control_ids:
                    success = action == 'pause' or self.batch['status'] == 'complete'
                    operations.mark(command_id, 'succeeded' if success else 'failed',
                                    {'batch_id': self.batch['id'], 'status': self.batch['status']})
            finally:
                # Cleanup also sets stop after normal completion. Capture the
                # requested pause first so it never closes a running emulator.
                paused = self.stop.is_set()
                self.stop.set()
                try:
                    if locals().get("close_devices"):
                        try:
                            if paused:
                                result = {'status': 'kept_open', 'reason': 'safe_pause'}
                            elif (self.selected_instances is not None
                                    or preserve_unsettled_devices(db, self.batch)
                                    or self.batch.get('keep_open_on_error') and self.batch['status'] != 'complete'):
                                result = {'status': 'kept_open', 'reason': 'unsettled_submission_or_keep_open'}
                            else:
                                result = shutdown_instances(sorted(self.device_instances))
                        except Exception as error:  # noqa: BLE001 - retain the completed results if shutdown fails
                            result = {"status": "failed", "error": str(error)}
                        self.batch["shutdown"] = result
                        db.save_batch(self.batch)
                        export_batch(db, self.batch)
                        from ournotes.reports.performance import build
                        build(self.batch, db.root)
                        from ournotes.accounts.archive import cleanup_formal_runs
                        try:
                            cleanup_formal_runs(db, [lane['run_id'] for lane in self.batch['lanes']])
                        except Exception as error:  # noqa: BLE001 - storage failure must not hide final runtime state
                            atomic_json(STATE / 'storage/latest.json', {
                                'status': 'deferred', 'finished_at': time.time(),
                                'reason': 'storage_maintenance_failed', 'error_type': type(error).__name__,
                            })
                        atomic_json(STATE / "shutdown-status.json", result)
                        self.publish(db, states)
                finally:
                    db.close()


def main():
    p = argparse.ArgumentParser(description="一个调度器并行操作多个 MuMu 实例")
    p.add_argument("--lane", action="append", help="实例编号=预设，例如 0=MyGO only；可重复")
    p.add_argument("--channel", choices=("test", "formal"), default="test")
    p.add_argument("--count", type=int, default=1, help="所有实例共享的总账号数；0 表示持续运行")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--debug-mode", action="store_true", help="仅新批次记录详细步骤；恢复沿用原批次")
    p.add_argument('--recovery-command', help='控制台的单实例恢复回执；须与 --resume 一起使用')
    p.add_argument("--async-start", action=argparse.BooleanOptionalAction, default=None,
                   help="异步启动：按配置顺序启动实例，间隔随机 60～90 秒；恢复时默认沿用批次设置")
    p.add_argument('--preset-version', type=int, help='控制台确认的共享预设版本')
    args = p.parse_args()
    if args.recovery_command and not args.resume:
        p.error('--recovery-command 须与 --resume 一起使用')
    lanes = [(int(x.split("=", 1)[0]), x.split("=", 1)[1]) for x in args.lane] if args.lane else None
    from ournotes.proxy.rotation import TaskProxyGate
    from ournotes.runtime.storage import SchedulerOutput
    task_gate = TaskProxyGate()
    controller = MultiRunner(lanes, args.channel, args.count or None, args.resume,
                             callback=SchedulerOutput(STATE.parent, os.environ.get('OURNOTES_LAUNCHER_JOB_ID')),
                             async_start=args.async_start, recovery_command=args.recovery_command,
                             task_gate=task_gate, debug_mode=args.debug_mode, preset_version=args.preset_version)
    controller.execute()
    if controller.batch["status"] != "complete":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
