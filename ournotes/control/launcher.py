"""Local launcher services. Reading status never initializes or repairs the runtime DB."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ournotes.common.devices import lock_held, require_instances_available
from ournotes.control.operations import OperationStore
from ournotes.paths import DATA_ROOT, ROOT

TERMINAL = {"complete", "cancelled"}
TITLES = {
    "start": "开始新批次",
    "resume": "恢复已有批次",
    "resume_lane": "恢复所选实例",
    "mumu_check": "检查 MuMu 实例",
    "gmail_check": "检查 Gmail 连接",
}


def read_json(path, fallback=None):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return fallback


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def redact(value):
    text = re.sub(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+", "[邮箱已隐藏]", str(value))
    text = re.sub(
        r"(?i)((?:access_token|refresh_token|client_secret|authorization|otp_code|验证码)\s*[=:：]\s*)[^\s,;]+",
        r"\1[已隐藏]",
        text,
    )
    return text


def runner_locked(root):
    """Scheduler/maintenance ownership; a separate rebind is not a scheduler."""
    return lock_held(root, 'runner')


def process_alive(pid):
    """Unknown or inaccessible ownership is treated as live."""
    if type(pid) is not int or pid <= 0:
        return True
    from ournotes.platforms.system import process_alive as alive
    return alive(pid)


class JobManager:
    def __init__(self, root, popen=subprocess.Popen):
        self.root, self.popen = root, popen
        self.folder = root / "state/webui/jobs"
        self.lock = threading.RLock()
        self.jobs, self.processes = {}, {}
        self.log_threads = {}
        from ournotes.common.bounded_log import console_logs
        self.log_budget = console_logs(root)
        if self.folder.is_dir():
            for path in sorted(self.folder.glob("*.json")):
                saved = read_json(path)
                if (isinstance(saved, dict) and saved.get("id") == path.stem and "resources" in saved
                        and saved.get('action') in TITLES):
                    if saved.get("status") == "running":
                        binding = read_json(self.folder / (path.stem + '-runtime.json'), {})
                        pid = binding.get('pid') or saved.get('pid')
                        saved.update(status="needs_review", reason="controller_restarted")
                        if not process_alive(pid):
                            saved.update(finished_at=time.time(), recovered_at=time.time())
                            atomic_json(path, saved)
                    self.jobs[path.stem] = saved

    def poll(self):
        with self.lock:
            for identifier, process in self.processes.items():
                job = self.jobs[identifier]
                code = process.poll()
                if job["status"] == "running" and code is not None:
                    job.update(
                        exit_code=code,
                        finished_at=time.time(),
                        status=(
                            "success"
                            if code == 0
                            else "paused"
                            if code == 2 and job["action"] in ("start", "resume", "resume_lane")
                            else "failed"
                        ),
                    )
                    atomic_json(self.folder / (identifier + ".json"), job)
                    # Session diagnostics are bounded and removed on normal console exit.

    def compress_finished_log(self, identifier):
        from ournotes.runtime.storage import StoragePolicy, compress_log
        try:
            policy = StoragePolicy.load(self.root)
            if policy.compress_logs:
                from ournotes.common.secrets import SecretStore
                with SecretStore(self.root / 'state').lock('storage-maintenance'):
                    compress_log(self.folder / (identifier + '.log'), policy.gzip_level)
        except (OSError, ValueError, RuntimeError):
            pass  # The original log remains available; startup maintenance retries.

    def busy(self, resource):
        with self.lock:
            self.poll()
            return any(j["status"] == "running" and resource in j["resources"] for j in self.jobs.values())

    def submit(self, action, builder, resources=(), account_index=None):
        with self.lock:
            if any(self.busy(resource) for resource in resources):
                raise ValueError("相关任务正在执行，请等待其结束。")
            identifier = uuid.uuid4().hex
            argv = [sys.executable, "-X", "utf8", *builder(identifier)]
            self.folder.mkdir(parents=True, exist_ok=True)
            process = self.popen(
                    argv,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "OURNOTES_DATA_ROOT": str(self.root),
                         "OURNOTES_LAUNCHER_JOB_ID": identifier},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            output = getattr(process, 'stdout', None)
            if output is not None:
                def collect():
                    try:
                        read = getattr(output, 'read1', output.read)
                        while chunk := read(8192):
                            self.log_budget.write(self.folder / (identifier + '.log'), chunk)
                    finally:
                        output.close()
                reader = threading.Thread(target=collect, name='console-log-' + identifier[:8], daemon=True)
                self.log_threads[identifier] = reader
                reader.start()
            job = {
                "id": identifier,
                "action": action,
                "title": TITLES[action],
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "exit_code": None,
                "resources": list(resources),
                "pid": getattr(process, 'pid', None),
                "log_policy": "session",
                "index": account_index,
            }
            self.jobs[identifier], self.processes[identifier] = job, process
            atomic_json(self.folder / (identifier + ".json"), job)
            return dict(job)

    def listing(self):
        self.poll()
        with self.lock:
            return [dict(job) for job in reversed(list(self.jobs.values()))][:40]

    def submit_function(self, action, function, resources=()):
        with self.lock:
            if any(self.busy(resource) for resource in resources):
                raise ValueError('相关任务正在执行，请等待其结束。')
            identifier = uuid.uuid4().hex
            job = {'id': identifier, 'action': action, 'title': TITLES[action], 'status': 'running',
                   'pid': os.getpid(),
                   'started_at': time.time(), 'finished_at': None, 'exit_code': None,
                   'resources': list(resources), 'index': None,
                   'progress': {'stage': 'queued', 'message': '任务已接收，正在准备…'}}
            self.jobs[identifier] = job
            atomic_json(self.folder / (identifier + '.json'), job)

            def progress(**values):
                with self.lock:
                    job['progress'] = {**job['progress'], **values, 'updated_at': time.time()}
                    atomic_json(self.folder / (identifier + '.json'), job)

            def work():
                try:
                    result = function(progress)
                    with self.lock:
                        job.update(status='success', result=result, exit_code=0)
                except Exception as error:  # noqa: BLE001 -- persist terminal failure at the worker boundary
                    with self.lock:
                        job.update(status='failed', exit_code=1, error=redact(error))
                finally:
                    with self.lock:
                        job['finished_at'] = time.time()
                        atomic_json(self.folder / (identifier + '.json'), job)
            threading.Thread(target=work, name='publication-' + identifier[:8], daemon=True).start()
            return dict(job)

    def detail(self, identifier):
        self.poll()
        with self.lock:
            if identifier not in self.jobs:
                raise ValueError("没有这个任务回执。")
            job = dict(self.jobs[identifier])
        try:
            with (self.folder / (identifier + ".log")).open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 12000))
                tail = redact(stream.read().decode("utf-8", errors="replace"))
        except OSError:
            saved_tail = read_json(self.folder / (identifier + '.log.tail.json'), {})
            if saved_tail:
                tail = redact(saved_tail.get('tail', ''))
            elif (self.folder / (identifier + '.log.gz')).exists():
                tail = '历史日志已无损压缩。'
            elif job.get('finished_at'):
                tail = '历史日志已按保留策略清理。'
            else:
                tail = '日志尚未生成。'
        job["log"] = "\n".join(tail.splitlines()[-55:])
        return job


class Launcher:
    def __init__(self, root=DATA_ROOT, index=None, manager=None, busy_probe=None, presets=None):
        self.root, self.index = Path(root).resolve(), index
        self.jobs = manager or JobManager(self.root)
        self.busy_probe = busy_probe or (lambda: runner_locked(self.root))
        self.action_lock = threading.RLock()
        self.operations = OperationStore(self.root)
        from ournotes.updates.service import UpdateService
        self.updates = UpdateService(self.root, os.environ.get("OURNOTES_INSTALL_ROOT"))
        from ournotes.control.game_update import GameUpdate
        self.game_update = GameUpdate(self.root)
        from ournotes.control.notifications import Notifications
        self.notifications = Notifications(self.root, self.runtime)
        self._presets = presets
        self._preset_snapshot = None
        self._preset_checked_at = 0
        self._preset_fetching = False
        self._preset_lock = threading.RLock()
        self.preset_error = ''

    @property
    def presets(self):
        return self.shared_presets()['presets']

    def shared_presets(self, *, refresh=False):
        if self._presets is not None:
            return {'version': 0, 'presets': self._presets}
        if not refresh:
            if not self._preset_fetching and time.monotonic() - self._preset_checked_at >= 30:
                self._preset_fetching = True
                def update():
                    try:
                        self.shared_presets(refresh=True)
                    except (ValueError, OSError, RuntimeError):
                        pass
                    finally:
                        self._preset_fetching = False
                threading.Thread(target=update, name='shared-presets', daemon=True).start()
            return self._preset_snapshot or {'version': None, 'presets': []}
        from ournotes.integrations.cloud_client import CloudClient, CloudError
        with self._preset_lock:
            self._preset_checked_at = time.monotonic()
            try:
                value = CloudClient.from_root(self.root).presets()
                if type(value.get('version')) is not int or not isinstance(value.get('presets'), dict):
                    raise ValueError('云端预设响应无效。')
                self._preset_snapshot = {'version': value['version'],
                                         'presets': [{**preset, 'key': key} for key, preset in value['presets'].items()]}
                self.preset_error = ''
            except (CloudError, ValueError, OSError) as error:
                self.preset_error = redact(error)
                raise
        return self._preset_snapshot or {'version': None, 'presets': []}

    def runtime(self):
        from ournotes.control.query import RuntimeQuery
        return RuntimeQuery(self.root, self.index).runtime()

    def operation(self, command_id):
        receipt = self.operations.get(command_id)
        job_id = receipt.get("result", {}).get("id")
        if job_id and receipt["status"] == "running":
            job = self.jobs.detail(job_id)
            status = {"success": "succeeded", "failed": "failed", "paused": "paused", "needs_review": "needs_review"}.get(job["status"])
            if status:
                return self.operations.mark(command_id, status, {**receipt["result"], "status": job["status"], "exit_code": job.get("exit_code")})
        return receipt

    def settings(self):
        from ournotes.integrations.cloud_client import CloudClientConfig
        try:
            saved = CloudClientConfig.load(self.root)
            return saved.public() if saved else {"configured": False, "service_url": ""}
        except (OSError, ValueError):
            return {"configured": False, "service_url": ""}

    def running(self):
        return self.owned_running() or self.busy_probe()

    def owned_running(self):
        return any(
            j["action"] in ("start", "resume", "resume_lane") and j["status"] == "running" for j in self.jobs.listing()
        )

    def status(self):
        from dataclasses import asdict

        from ournotes.runtime.storage import StoragePolicy
        runtime = self.runtime()
        jobs = self.jobs.listing()
        presets = self.shared_presets()
        runtime.update(
            running=self.running(),
            observed_at=time.time(),
            environment={
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "catalog": self.catalog_available(),
                "mumu": self.mumu_available(),
            },
            presets=[
                {k: p[k] for k in ("key", "name", "total_pulls", "transactions") if k in p} for p in presets['presets']
            ],
            preset_version=presets['version'],
            preset_error=self.preset_error,
            settings=self.settings(),
            updates=self.updates.status(),
            game_update=self.game_update.status(),
            jobs=jobs,
            storage={'policy': asdict(StoragePolicy.load(self.root))},
        )
        if runtime['running'] and runtime.get('recovery_protocol') != 1:
            reason = '当前调度器尚不支持此接口；请先安全暂停，退出并重新启动控制台。'
            for lane in runtime['lanes']:
                for mode in ('retry', 'restart_game'):
                    lane['recovery'][mode] = {'enabled': False, 'reason': reason}
        return runtime

    def mumu_available(self):
        from ournotes.platforms.mumu_config import selected, valid
        try:
            directory = selected(self.root)
            return bool(directory and valid(directory))
        except (OSError, ValueError):
            return False

    def catalog_available(self):
        from ournotes.catalogs.runtime import current_catalog
        try:
            current_catalog(self.root / 'data/catalog')
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return True

    def control(self, action, expected):
        runtime = self.runtime()
        batch = runtime["batch"]
        if runtime["error"] or not batch or batch["id"] != expected or batch["status"] in TERMINAL:
            raise ValueError("当前批次已变化或不可控制，请刷新页面。")
        if not self.running():
            raise ValueError("没有活动调度进程，无需发送控制指令。")
        return {
            "message": "已请求安全暂停，等待结算收尾与状态保存。"
            if action == "pause"
            else "已请求各实例完成当前账号后结束。"
        }

    def execute(self, action, values):
        command_id = values.get("command_id")
        if not command_id:
            raise ValueError("写操作需要 command_id。")
        parameters = {k: v for k, v in values.items() if k != "command_id"}
        if action == 'resume_lane':
            parameters['instances'] = [values['instance']]
        def apply(_):
            expected = values.get("expected_version")
            if action != 'resume_lane' and expected is not None and expected != self.runtime().get("version"):
                raise ValueError("状态版本已变化，请重新读取。")
            return self.dispatch(action, values)
        return self.operations.submit(command_id, 'resume_lanes' if action == 'resume_lane' else action, parameters, apply)

    def dispatch(self, action, values):
        with self.action_lock:
            if self.game_update.busy() and action in ('start', 'resume', 'resume_lane', 'cancel'):
                raise ValueError('游戏更新正在执行，请等待结果后再控制批次。')
            runtime = self.runtime()
            batch = runtime["batch"]
            if action == 'resume_lane':
                from ournotes.control.recovery import require_recovery
                if runtime['error'] or not batch or batch['id'] != values.get('batch_id'):
                    raise ValueError('当前批次已变化或不可读，请刷新页面。')
                lane = next((l for l in runtime['lanes'] if l['instance'] == values['instance']), None)
                if not lane or not values.get('expected_version'):
                    raise ValueError('请读取所选实例的恢复版本后再操作。')
                require_recovery(lane['recovery'], values.get('recovery_mode', 'retry'), values['expected_version'])
                if self.running():
                    if runtime.get('recovery_protocol') != 1:
                        raise ValueError('当前调度器尚不支持实例恢复；请先安全暂停并重启控制台。')
                    return {'message': '已请求恢复所选实例，等待原工作线程退出及调度器确认。',
                            'instances': [values['instance']]}
                args = ['-m', 'ournotes.runtime.scheduler', '--resume',
                        '--recovery-command', values['command_id']]
                require_instances_available(self.root, [lane['instance'] for lane in runtime['lanes']])
                return self.jobs.submit(action, lambda _: args, ('device', 'mail'))
            if action in ("pause", "finish"):
                return self.control(action, values.get("batch_id"))
            if action == "cancel":
                return self.close_batch(values.get("batch_id"))
            if action in ("start", "resume", "gmail_check") and (
                runtime["error"] or self.running()
            ):
                raise ValueError(runtime["error"] or "调度器正在运行，请先安全暂停，再执行这项操作。")
            if action == "start":
                if batch and batch["status"] not in TERMINAL:
                    raise ValueError("已有未结束批次。可恢复已有批次，或结束当前批次后再开始。")
                run = values["run"]
                lanes = run["lanes"]
                snapshot = self.shared_presets(refresh=True)
                if run.get('preset_version') != snapshot['version']:
                    raise ValueError('共享预设版本已变化，请刷新运行配置后重试。')
                if len({lane["instance"] for lane in lanes}) != len(lanes):
                    raise ValueError("实例编号不能重复。")
                if any(lane["preset"] not in {p["name"] for p in snapshot['presets']} for lane in lanes):
                    raise ValueError("预设不存在。")
                require_instances_available(self.root, [lane['instance'] for lane in lanes])
                args = [
                    "-m",
                    "ournotes.runtime.scheduler",
                    "--channel",
                    run["channel"],
                    "--count",
                    str(run["count"]),
                    '--preset-version', str(snapshot['version']),
                    "--async-start" if run["async_start"] else "--no-async-start",
                ]
                if run.get("debug_mode", False):
                    args.append("--debug-mode")
                for lane in lanes:
                    args += ["--lane", f"{lane['instance']}={lane['preset']}"]
                return self.jobs.submit(action, lambda _: args, ("device", "mail"))
            if action == "resume":
                if not batch or batch["status"] in TERMINAL or batch["id"] != values.get("batch_id"):
                    raise ValueError("没有可恢复的当前批次，请刷新页面。")
                require_instances_available(self.root, [lane['instance'] for lane in runtime['lanes']])
                module = "ournotes.runtime.scheduler"
                return self.jobs.submit(action, lambda _: ["-m", module, "--resume"], ("device", "mail"))
            if action == "mumu_check":
                from ournotes.integrations.mumu import instance_info
                info = instance_info(instance=values['instance'])
                state = ('Android 已启动' if info.get('is_android_started') else
                         '模拟器进程已启动' if info.get('is_process_started') else '模拟器已关闭')
                return {'instance': values['instance'], 'info': info,
                        'message': f"实例 {values['instance']}：{state}。"}
            if action == "gmail_check":
                from ournotes.integrations.cloud_client import CloudClient
                health = CloudClient.from_root(self.root).health()
                return {**health, "message": "服务连接正常；" + ("Gmail 已授权。" if health.get("gmail_configured") else "管理员尚未完成 Gmail 授权迁移。")}
            raise ValueError("不支持的操作。")

    def close_batch(self, expected):
        """Retire scheduling only, under the same lock used by both game runners.

        Accounts, draws, counters and exports remain untouched, including any
        unresolved evidence. A cancelled run must never resume or replay them.
        """
        from ournotes.common.secrets import SecretStore
        from ournotes.runtime.store import SCHEMA_VERSION

        if self.running() or self.jobs.busy("device") or self.jobs.busy("mail"):
            raise ValueError("请先安全暂停，等待调度器和相关任务退出后再结束批次。")
        try:
            with SecretStore(self.root / "state").lock("runner"):
                runtime = self.runtime()
                batch = runtime["batch"]
                if runtime["error"] or not batch or batch["id"] != expected or batch["status"] in TERMINAL:
                    raise ValueError("当前批次已变化或已结束，请刷新页面。")
                path = self.root / "state/runtime.sqlite3"
                with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2)) as db:
                    if db.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                        raise ValueError("状态库来自较新版本，拒绝修改。")
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        table = "batches"
                        row = db.execute(f"SELECT data FROM {table} WHERE id=?", (expected,)).fetchone()
                        saved = json.loads(row[0]) if row else None
                        if not saved or saved["status"] in TERMINAL:
                            raise ValueError("批次已变化，请刷新页面。")
                        run_ids = [l["run_id"] for l in saved["lanes"]]
                        closed = {
                            "status": "cancelled", "closed_at": time.time(),
                            "closed_reason": "用户结束批次；保留已有结果，未完成账号不继续运行。",
                            "closed_by": "webui",
                        }
                        for run_id in run_ids:
                            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
                            run = json.loads(row[0]) if row else None
                            if not run or run.get("batch_id") != expected:
                                raise ValueError("实例记录不完整或不属于此批次，未修改任何记录。")
                            if run["status"] not in TERMINAL:
                                run.update(closed)
                                db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run_id))
                        saved.update(closed)
                        db.execute("UPDATE batches SET data=? WHERE id=?", (json.dumps(saved), expected))
        except RuntimeError as error:
            raise ValueError("调度器正在使用运行锁，请等待其安全退出。") from error
        return {"message": "当前批次已结束，已有记录与账号计数保留。现在可以配置并开始新批次。"}

    def account_set_usage(self, index, token=None, target_status='used'):
        from ournotes.accounts.used import preview, set_usage
        with self.action_lock:
            value = set_usage(self.root, index, token, target_status) if token else preview(self.root, index)
            return value

    def open_folder(self, target):
        folders = {
            "results": self.root / "results",
            "logs": self.root / "state",
        }
        if target == "batch":
            batch = self.runtime()["batch"]
            if batch and re.fullmatch(r"[a-f0-9]{32}", batch["id"]):
                folders["batch"] = self.root / "results/batches" / batch["id"]
        path = folders.get(target)
        if path is None or not path.is_dir() or not path.resolve().is_relative_to(self.root):
            raise ValueError("目录尚未生成或不可访问。")
        from ournotes.platforms.system import current_platform
        if current_platform() == 'windows-x64':
            os.startfile(str(path))
        else:
            subprocess.run(['open', str(path)], check=True, timeout=10, capture_output=True)
        return {"message": "已打开目录。"}
