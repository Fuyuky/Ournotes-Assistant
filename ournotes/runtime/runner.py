"""Deterministic, resumable reroll controller. No LLM or browser automation."""
from __future__ import annotations

import concurrent.futures
import re
import shutil
import sqlite3
import threading
import time
import traceback
import uuid

import numpy as np

from ournotes.catalogs.runtime import sync_catalog
from ournotes.common import memory as system_memory
from ournotes.common.files import atomic_json
from ournotes.common.memory import GIB
from ournotes.integrations.device import DeviceClient
from ournotes.integrations.mumu import ensure_game, restart_game
from ournotes.paths import DATA_ROOT as ROOT
from ournotes.paths import STATE
from ournotes.runtime.input_policy import InputPolicy
from ournotes.runtime.mail import MAIL_REQUEST_BUDGET_SECONDS, RunnerMail
from ournotes.runtime.memory_maintenance import recycle_between_accounts, require_confirmed_restart
from ournotes.runtime.model import LOGIN_FORM_FIELDS, MAIL_LIMIT_MESSAGE, PauseRun
from ournotes.runtime.pool_navigation import PoolNavigator
from ournotes.runtime.pools import PoolFinder, make_plan
from ournotes.runtime.presets import validate_plan
from ournotes.runtime.recognition_queue import RecognitionQueue
from ournotes.runtime.runlog import RunLog
from ournotes.runtime.store import RuntimeStore
from ournotes.ui.observer import Observer
from ournotes.vision.recognition import CardRecognizer, ResultLayout

NAVIGATION = {"home-menu", "title-menu", "verify-account-menu", "back-from-pool", "close-user-center",
              "关闭", "用户中心", "标题画面", "切换其他账号", "账号登录", "招募TOP", "招募", "退出登录",
              "TAP TO START", "open-gifts", "open-draw-confirmation", "advance-character-dialogue",
              "dismiss-reward-OK", "claim-gifts", "ack-gifts", "reset-email-form", "pool-menu", "back-from-shop",
              "expand-login-history", "remove-login-history", "confirm-remove-login-history",
              "重试", "返回标题画面", "close-unavailable-menu", "关闭额外弹窗", "confirm-download"}

# These screens legitimately repeat within one transition (one reveal per card,
# multiple daily rewards). Recheck the foreground on every click; bound the whole
# repetition window rather than treating the fourth card as a failed navigation.
REPEATABLE = {'advance-reward': .6, 'SKIP': .35, 'skip': .35, 'skip-opening': .6,
              'skip-opening-movie': .6, 'confirm-opening-skip': .6,
              'open-story-menu': .6, 'skip-story': .6, 'confirm-story-skip': .6}
OPENING_SCENES = ('opening_movie', 'opening_skip_confirm', 'story', 'story_menu', 'story_skip_confirm')
OPENING_CONTROLS = {
    'opening_movie': ('跳过', 'skip-opening-movie'),
    'opening_skip_confirm': ('OK', 'confirm-opening-skip'),
    'story': ('剧情菜单', 'open-story-menu'),
    'story_menu': ('SKIP', 'skip-story'),
    'story_skip_confirm': ('跳过', 'confirm-story-skip'),
}
PAUSE_SETTLEMENT_SECONDS = 90
DRAW_WAIT_SECONDS = 180
NETWORK_BACKOFF_SECONDS = (2, 4, 8)



def mail_limit_reached(run):
    return bool(run.get('mail_send_limit_reached') or MAIL_LIMIT_MESSAGE in run.get('pause_reason', ''))


from ournotes.runtime.draw_flow import DrawFlow
from ournotes.runtime.login_flow import LoginFlow
from ournotes.runtime.reward_flow import RewardFlow


class Runner:
    def __init__(self):
        self.db = None
        self.mail_job = None
        self.mail_job_kind = None
        self.mail_job_started_at = None
        self.code = None
        self.draw_flow = DrawFlow(self)
        self.reward_flow = RewardFlow(self)
        self.login_flow = LoginFlow(self)
        self.pool_navigation = PoolNavigator()
        self.wallet_observation = None
        self.observation = None
        self.vision = None
        self.device = None
        self.device_touched = False
        self.last_action = None
        self.last_published = None
        self.last_action_at = 0.0
        self.repeat_started = 0.0
        self.title_start_wait = None
        self.committing_draw_id = None
        self.draw_button_ocr_target = None
        self.resumed_login_account = None
        self.last_login_feedback_at = float('-inf')
        self.stop = threading.Event()
        self.pause_reason = None
        self.pause_deadline = None
        self.loading_episode = None
        self.draw_wait_started = None
        self.next_action_at = 0.0
        self.opening_started_at = None
        self.download_read_at = -2.0
        self.download_percent = None
        self.last_button_ocr = {}
        self.input_policy = InputPolicy()
        self.last_memory_log_at = float('-inf')
        self.task_gate = None
        self.task_permit = None


    def _initialize(self, *, run_id, instance=0, stop=None, callback=None, resume=False):
        self.db = RuntimeStore()
        self.run = self.db.get_run(run_id)
        if not self.run or self.run["status"] in ("complete", "cancelled"):
            raise ValueError("实例任务不存在或已结束。")
        require_confirmed_restart(self.run)
        if self.run.get('recovery_request', {}).get('status') == 'starting':
            raise ValueError('上次恢复或重启结果未确认；保留原回执等待核查。')
        if mail_limit_reached(self.run):
            raise ValueError(MAIL_LIMIT_MESSAGE + '；本轮已停止，不能自动恢复发码。')
        self.account = self.db.account(self.run["active_account"]) if self.run["active_account"] else None
        if self.account and self.ctx.get('login_network_uncertain'):
            raise ValueError('发码或登录网络错误的结果尚未确认；保留原票据，禁止自动恢复设备。')
        if self.run.get('storage_retention', {}).get('pruned_at') or (self.account or {}).get('evidence_pruned_at'):
            from ournotes.runtime.storage import EVIDENCE_PRUNED
            raise ValueError(EVIDENCE_PRUNED)
        if self.account and self.account.get("recognition_source") != "settlement":
            raise ValueError("该历史账号使用已退役的识别方案；保留证据供离线核查，禁止恢复设备执行。")
        if self.account and self.account.get('catalog_snapshot'):
            from ournotes.catalogs.cards import sha256
            from ournotes.catalogs.runtime import saved_snapshot
            pinned = saved_snapshot(ROOT, self.account['catalog_snapshot'])
            fingerprints = self.account.get('catalog_fingerprints', {})
            if set(fingerprints) != {'catalog.json', 'pools.json'}:
                raise ValueError('账号保存的运行素材校验清单不完整。')
            for name, fingerprint in fingerprints.items():
                if name not in ('catalog.json', 'pools.json') or sha256(pinned / name) != fingerprint:
                    raise ValueError('账号保存的运行素材快照已改变。')
            self.catalog_directory = pinned
        else:
            self.catalog_directory = sync_catalog()
        self.stop = stop or threading.Event()
        self.callback = callback or (lambda item: None)
        self.log = RunLog(ROOT, self.run["id"], debug_mode=self.run.get("debug_mode", False))
        self.instance = self.run.get("instance", instance)
        self.device = None
        self.vision = None
        self.recognizer = None
        self.recognition = RecognitionQueue(ROOT, self.run["id"],
                                            factory=lambda: CardRecognizer(self.catalog_directory))
        self.layout = ResultLayout()
        self.result_stability = None
        self.pools = PoolFinder(self.catalog_directory)
        validate_plan(self.account['plan'] if self.account else self.planned_draws(), self.pools.catalog)
        self.mail = RunnerMail(observer=lambda **fields: self.log.event('mail_transport', **fields))
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gmail")
        self.mail_job = None
        self.mail_job_kind = None
        self.code = None
        self.last_poll = 0.0
        self.observation = None
        self.last_action = None
        self.last_scene = None
        self.same_scene = 0
        self.last_progress = time.monotonic()
        self.last_account_tick = time.monotonic()
        self.resume = resume
        # Restore the saved login inputs without allocating a new ticket.
        self.resumed_login_account = (self.account['id'] if resume and self.account
                                      and self.account['phase'] == 'login' else None)
        self.last_published = None
        self.pool_selection_attempts = {}
        self.navigation_attempts = 0
        self.last_known_scene = None
        self.input_policy = InputPolicy()
        self.next_action_at = 0.0
        self.pause_reason = None
        self.pause_deadline = None

    @property
    def ctx(self):
        return self.account["context"]

    def save(self):
        self.db.save_account(self.account)

    def phase(self, phase):
        if self.account["phase"] == phase:
            return
        self.account["phase"] = phase
        self.save()
        self.last_action = None
        self.last_progress = time.monotonic()
        self.log.event("phase", account=self.account["id"], phase=phase)

    def publish(self, **extra):
        item = dict(run_id=self.run["id"], status=self.run["status"],
                    instance=self.instance, preset=self.run["preset"],
                    completed=self.run["completed"], counters=self.db.counters(), **extra)
        if self.account:
            item.update(account_id=self.account["id"], alias=self.account["alias"],
                        phase=self.account["phase"], preset=self.account["preset"],
                        transactions=len([d for d in self.db.draws(self.account["id"])
                                          if d["status"] == "complete"]))
            if (self.run['status'] == 'running' and self.account['phase'] == 'login'
                    and self.ctx.get('send_intent') and not self.ctx.get('code_ready')
                    and not self.ctx.get('login_submitted') and not self.code):
                remaining = max(0, int(self.ctx.get('otp', {}).get('expires_at', time.time()) - time.time()))
                item.update(waiting_for='verification_email', wait_seconds_remaining=remaining)
                item.setdefault('message', f"等待验证码，剩余 {remaining} 秒")
            elif self.run['status'] == 'running' and self.account['phase'] == 'login' and self.ctx.get('login_submitted'):
                item.update(waiting_for='login_confirmation')
                item.setdefault('message', '验证码已提交，等待登录确认')
            if self.pause_reason:
                item.update(waiting_for='pause_settlement',
                            message=('正在安全暂停：停止新任务，等待原验证码请求登录收尾。'
                                     if self.pending_login() else
                                     '正在安全暂停：停止新交易，等待已提交抽卡结算保存。'))
        if self.observation is not None:
            item['observation'] = {'scene': self.observation.scene,
                                   'last_action': self.last_action[0] if self.last_action else None}
        if item == self.last_published:
            return
        self.last_published = item
        target = STATE / f"instances/{self.instance}/status.json"
        published = dict(item, updated_at=time.time())
        atomic_json(target, published)
        self.callback(published)

    def check_memory(self):
        try:
            memory = system_memory.read_commit_memory()
        except OSError as error:
            raise PauseRun('无法读取系统内存，暂停以避免无保护地继续操作。') from error
        if memory is None:
            return
        now = time.monotonic()
        if memory.low or now - self.last_memory_log_at >= 60:
            self.log.event('system_memory', **memory.report())
            self.last_memory_log_at = now
        if memory.low:
            label = '系统可用内存' if getattr(memory, 'source', '') == 'macos-memory' else '系统提交内存'
            raise PauseRun(
                f'{label}不足：剩余 {memory.available / GIB:.1f} GiB，'
                f'保护阈值 {memory.reserve / GIB:.1f} GiB；'
                '保留原账号及交易，停止操作。请检查 MuMu 等进程占用；未决交易核查前不要重启实例。')

    def check_storage(self):
        from ournotes.runtime.storage_budget import pause_reason
        reason = pause_reason(ROOT)
        if reason and self.stop is not None:
            self.stop.set()
        self.check_pause(reason)

    def action(self, label, kind="tap", args=(), *, force=False, before_input=None):
        o = self.observation
        title_start = label == 'TAP TO START' and o.scene == 'title'
        title_wait = self.title_start_wait
        if title_start and title_wait and title_wait['account_id'] == self.account['id']:
            now = time.monotonic()
            if now - title_wait['started_at'] >= 90:
                if self.recover_logout_title_stall(now):
                    return False
                raise PauseRun('TAP TO START 等待90秒仍未进入登录或主页；保留原账号核查。')
            if title_wait['attempts'] >= 3 or now - title_wait['last_tap_at'] < 10:
                return False
        signature = (label, o.signature)
        repeated = (self.last_action and self.last_action[0] == label and self.last_action_scene == o.scene)
        navigation = label in NAVIGATION
        # Home controls appear before startup resources/rewards finish loading.
        # Continue observing interrupts while allowing each recruit tap to settle.
        retry_interval = 10 if label == '招募' and o.scene == 'home' else 2
        if (repeated and (navigation or label.startswith("select-"))
                and time.monotonic() - self.last_action_at < retry_interval):
            return False
        if navigation and not getattr(o, "fast", False) and self.same_scene < (3 if repeated else 2):
            return False
        if navigation and repeated and self.navigation_attempts >= 3 and not title_start:
            if (o.scene == 'title_menu' and label == '用户中心'
                    and self.account['phase'] in ('switch_account', 'logout', 'rewards', 'gifts')):
                self.recover_unavailable_menu()
                return False
            if (o.scene == 'title' and label in ('title-menu', 'verify-account-menu')
                    and self.account['phase'] in ('switch_account', 'logout', 'rewards', 'gifts')):
                self.recover_unopened_menu()
                return False
            raise PauseRun("页面导航三次未生效：" + label)
        reward_repeat = repeated and label in REPEATABLE
        if reward_repeat and time.monotonic() - self.last_action_at < REPEATABLE[label]:
            return False
        if reward_repeat and time.monotonic() - self.repeat_started > 60:
            raise PauseRun('界面重复操作超过60秒：' + label)
        if self.last_action == signature and not force and not navigation and not reward_repeat:
            return False
        if (repeated and o.scene not in ("startup_reward", "animation")
                and not reward_repeat and not force and not navigation):
            return False
        committing = (kind == 'tap' and label == 'OK' and o.scene == 'draw_confirm'
                      and self.committing_draw_id)
        if ((self.stop.is_set() or self.pause_reason)
                and not committing and not self.pause_input_allowed(label)):
            return False
        nominal_args = args
        policy = self.input_policy
        if kind == 'tap' and policy:
            args = policy.point(args, o)
        if kind == "tap" and (navigation or label in REPEATABLE or before_input is not None):
            with self.log.timed("action_guard_capture", label=label):
                fresh, _ = self.device.capture()
            fresh = fresh.resize(o.image.size)
            guarded = (self.vision.guard(o, fresh, label, args)
                       if isinstance(self.vision, Observer) else None)
            if guarded is False:
                self.log.event("stale_action_avoided", label=label, reason="foreground_control_changed")
                return False
            if label == 'open-draw-confirmation' and self.draw_button_ocr_target:
                # OCR fallback has no template match: reject a changed button even
                # when the surrounding pool anchors still match.
                b = self.draw_button_ocr_target
                difference = float(np.abs(np.asarray(fresh.crop(b), dtype=np.float32)
                                          - np.asarray(o.image.crop(b), dtype=np.float32)).mean())
                if difference > 8:
                    return False
            x, y = args
            box = (max(0, x - 60), max(0, y - 28), min(1600, x + 60), min(900, y + 28))
            boxes = [box]
            if o.scene == "title" and label in ("title-menu", "verify-account-menu", "TAP TO START"):
                # The movie and TAP glow animate underneath the controls. Verify the
                # opaque menu and Live2D badge instead; an overlay dims these too.
                boxes = [(1510, 30, 1570, 85), (1275, 25, 1340, 90)]
            difference = max(float(np.abs(np.asarray(fresh.crop(b), dtype=np.float32)
                                          - np.asarray(o.image.crop(b), dtype=np.float32)).mean()) for b in boxes)
            if guarded is None and difference > 8:
                self.log.event("stale_action_avoided", label=label, pixel_difference=difference)
                return False
        with self.log.timed("action_evidence", label=label):
            evidence = self.log.screenshot(o.image, "before-" + label)
        action_id = self.db.action(self.account["id"], {"label": label, "kind": kind,
                                  "args": list(args) if kind not in ("input_text", "replace_text") else "REDACTED",
                                  "nominal_args": list(nominal_args) if kind == 'tap' else None,
                                  "scene": o.scene, "frame": evidence, "phase": self.account["phase"]})
        self.log.event("action_intent", action_id=action_id, label=label, scene=o.scene,
                       account=self.account["id"], evidence=evidence)
        if before_input is not None:
            before_input()
        with self.log.timed("device_input", label=label, kind=kind):
            getattr(self.device, kind)(*args)
        self.db.action_done(action_id)
        if title_start:
            now = time.monotonic()
            if not title_wait or title_wait['account_id'] != self.account['id']:
                title_wait = {'account_id': self.account['id'], 'started_at': now, 'attempts': 0}
                self.title_start_wait = title_wait
            title_wait.update(last_tap_at=now, attempts=title_wait['attempts'] + 1)
        if self.vision:
            self.vision.invalidate()
        self.log.event("action_finished", action_id=action_id, label=label, scene=o.scene,
                       account=self.account["id"])
        self.last_action = signature
        if not repeated:
            self.repeat_started = time.monotonic()
        self.last_action_at = time.monotonic()
        if policy:
            delay = policy.interval()
            self.next_action_at = self.last_action_at + delay
            self.log.event('input_variation', kind=kind, label=label,
                           nominal=list(nominal_args) if kind == 'tap' else None,
                           actual=list(args) if kind == 'tap' else None, interval_seconds=delay)
        self.last_action_scene = o.scene
        self.navigation_attempts = self.navigation_attempts + 1 if repeated else 1
        if navigation:
            self.same_scene = 0
        self.last_progress = time.monotonic()
        return True

    def click(self, value, *, exact=False, region=None, label=None, before_input=None):
        target = self.observation.find(value, exact=exact, region=region)
        if target is None:
            return False  # Transient OCR gaps are re-observed; the progress watchdog is the deadline.
        if before_input is not None:
            return self.action(label or value, args=target.center, before_input=before_input)
        return self.action(label or value, args=target.center)

    def acquire_proxy_task(self):
        """Optional local admission before devices/new allocations; core has no Clash dependency."""
        if self.task_gate is None or self.task_permit is not None:
            return True
        budget = 2
        task_id = 'account-' + self.run['id'] + '-' + uuid.uuid4().hex
        if self.account:
            task_id = self.ctx.get('proxy_task_id', 'account-' + self.run['id'] + '-' + self.account['id'])
            retry = self.ctx.get('otp_retry', {})
            if self.account['phase'] not in ('switch_account', 'login') or self.ctx.get('login_confirmed'):
                budget = 0
            elif retry:
                budget = 0 if retry.get('stage') == 'ready' and self.ctx.get('send_intent') else 1
            elif self.ctx.get('send_intent'):
                budget = 1
        while True:
            self.check_pause()
            saved = self.db.get_run(self.run['id'])
            if not self.account and (saved.get('finish_requested') or self.db.batch_quota_exhausted(saved)):
                return False
            try:
                self.task_permit = self.task_gate.begin(task_id, budget, resume=self.account is not None)
            except Exception as error:  # quota/transport/storage failures all fail closed
                self.stop.set()  # No live peers remain on quota exhaustion; retain devices on safe pause.
                # Keep diagnostic identity, never transport messages or credential values.
                self.log.event('proxy_admission_failed', exception_type=type(error).__name__,
                               errno=getattr(error, 'errno', None),
                               winerror=getattr(error, 'winerror', None),
                               sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
                               trace=traceback.format_tb(error.__traceback__))
                reason = str(error) if isinstance(error, ValueError) else '代理额度检查失败，保留现场等待核查。'
                raise PauseRun(reason) from error
            if self.task_permit is not None:
                if self.account:
                    self.ctx['proxy_task_id'] = self.task_permit.task_id
                    self.save()
                return True
            self.publish(message='代理节点达到额度，等待当前账号或换绑任务收尾后切换')
            if self.stop.wait(.25):
                self.check_pause()

    def release_proxy_task(self):
        permit, self.task_permit = self.task_permit, None
        if permit is not None:
            permit.close()

    def complete_without_new_task(self):
        self.run.update(self.db.get_run(self.run['id']))
        reason = 'finish_requested' if self.run.get('finish_requested') else 'batch_quota'
        self.run.update(status='complete', end_reason=reason, finished_at=time.time())
        self.db.save_run(self.run)
        self.publish(message='当前账号已收尾，无需启动新的代理任务')

    def refresh_draw_confirmation(self):
        """Reobserve before saving submission intent; validate fields on this frame."""
        with self.log.timed('draw_confirmation_guard'):
            fresh, _ = self.device.capture()
            observation = self.vision.read(fresh, phase=self.account['phase'], account_id=self.account['id'])
        self.observation = observation
        self.log.observe(observation.image)
        target = observation.find('OK', exact=True)
        if observation.scene != 'draw_confirm' or target is None:
            self.log.event('stale_action_avoided', label='OK', reason='confirmation_foreground_changed')
            return False
        return self.vision.guard(observation, observation.image, 'OK', target.center) is not False

    def read_fields(self, name, transaction=None):
        if not isinstance(self.vision, Observer):
            return
        with self.log.timed("field_ocr", field=name, phase=self.account["phase"]) as metric:
            self.vision.read_fields(self.observation, name, transaction)
            metric.update(reads=list(self.vision.fields.metrics))
            self.vision.fields.metrics.clear()






    def submitted_draws(self):
        return ([d for d in self.db.draws(self.account['id']) if d['status'] == 'submitted']
                if self.account else [])

    def pending_login(self):
        if not self.account or self.account['phase'] != 'login' or not self.ctx.get('send_intent'):
            return False
        if self.ctx.get('otp_retry', {}).get('stage') in ('reset_form', 'prepare'):
            return False
        return bool(self.ctx.get('login_submitted') or self.ctx.get('otp', {}).get('expires_at', 0) > time.time())

    def check_pause(self, reason=None):
        """Drain already-sent OTP and draw requests without starting new work."""
        stop = self.stop
        if (reason or (stop is not None and stop.is_set())) and not self.pause_reason:
            self.pause_reason = reason or '用户暂停'
            remaining = self.ctx.get('otp', {}).get('expires_at', 0) - time.time() if self.pending_login() else 0
            budget = max(PAUSE_SETTLEMENT_SECONDS, remaining + 15)
            self.pause_deadline = time.monotonic() + budget
            self.log.event('safe_pause_requested', reason=self.pause_reason,
                           settlement_timeout_seconds=budget)
        if not self.pause_reason:
            return False
        pending = self.submitted_draws()
        if not pending and not self.pending_login():
            raise PauseRun(self.pause_reason + '，已停止新输入；已采集结果将完成后台识别后退出。')
        if time.monotonic() >= self.pause_deadline:
            raise PauseRun('安全暂停收尾超时；保留原请求及已提交交易，禁止重复提交。')
        return True

    def pause_input_allowed(self, label):
        if not self.pause_reason:
            return False
        if self.pending_login():
            return self.observation.scene == 'email_form' and label in ('focus-code', 'fill-code', '登录/注册')
        if not self.submitted_draws():
            return False
        return ((self.observation.scene == 'animation' and label in ('SKIP', 'skip'))
                or (self.observation.scene == 'draw_confirm' and label == 'release-confirm-touch'))

    def settle_before_pause(self):
        if self.pending_login():
            self.login_flow.login()
            return
        scene = self.observation.scene
        if scene in ('title', 'network_error'):
            raise PauseRun('安全暂停期间已提交交易失去结算入口；保留待核验，禁止补抽。')
        if scene in ('animation', 'draw_result', 'draw_confirm'):
            self.draw_flow.draws()

    def wait_for_admission(self):
        from ournotes.runtime.quiet_hours import admission_delay

        if self.account is not None:
            return
        self.wait_for_uploads()
        waiting = False
        while admission_delay() > 0:
            self.check_pause()
            saved = self.db.get_run(self.run['id'])
            if saved.get('finish_requested') or self.db.batch_quota_exhausted(saved):
                return
            if not waiting:
                self.publish(message='每日休息时段：北京时间23:05自动继续新账号。')
                waiting = True
            self.stop.wait(min(1, admission_delay()))
        self.check_pause()
        if waiting:
            self.publish(message='休息时段结束，继续新账号。')

    def planned_draws(self):
        import copy
        plan = self.run.get('preset_plan')
        return copy.deepcopy(plan) if plan is not None else make_plan(self.run['preset'])

    def wait_for_uploads(self):
        """Block only new accounts; keep the controller alive for automatic retry."""
        from ournotes.integrations.cloud_client import CloudError
        from ournotes.integrations.cloud_sync import CloudSync
        sync = CloudSync(self.db.root)
        while sync.pending_results():
            self.check_pause()
            saved = self.db.get_run(self.run['id'])
            if saved.get('finish_requested'):
                return
            self.publish(message='等待云端确认待上传成果；确认后自动继续新账号。')
            try:
                sync.flush(stop=self.stop)
            except (CloudError, OSError, ValueError, sqlite3.Error):
                pass  # Durable outbox retains the original request and content.
            if sync.pending_results():
                self.stop.wait(3)
        self.check_pause()

    def _execute(self):
        try:
            self.check_memory()
            self.check_storage()
            self.wait_for_admission()
            if not self.acquire_proxy_task():
                self.complete_without_new_task()
                return
            self.apply_manual_recovery()
            self.run["status"] = "running"
            self.db.save_run(self.run)
            self.publish(message="加载本地识别模型与设备")
            self.log.event("run_started", instance=self.instance, resumed=self.resume)
            with self.log.timed("startup_emulator"):
                self.device_touched = True
                ensure_game(self.instance, self.stop)
            with self.log.timed("startup_sdk"):
                self.device = DeviceClient(self.instance)
            with self.log.timed("startup_vision"):
                self.vision = Observer()
            while True:
                tick = time.monotonic()
                # Scheduler commands persist allocation boundaries; preserve local progress.
                saved = self.db.get_run(self.run['id'])
                if saved.get('finish_requested'):
                    self.run['count'] = saved['count']
                    self.run['finish_requested'] = True
                self.check_storage()
                self.check_memory()
                if shutil.disk_usage(ROOT).free < 2 * 1024**3:
                    raise PauseRun("磁盘剩余不足2 GiB。")
                if self.account is None:
                    self.reset_after_abandonment()
                    if self.run["count"] is not None and self.run["completed"] >= self.run["count"]:
                        self.run["status"] = "complete"
                        self.db.save_run(self.run)
                        self.publish(message="本批运行完成")
                        return
                    from ournotes.runtime.store import AllocationClosed
                    try:
                        self.wait_for_admission()
                        if not self.acquire_proxy_task():
                            self.complete_without_new_task()
                            return
                        if recycle_between_accounts(self):
                            tick = time.monotonic()  # Maintenance is not the next account's active time.
                        self.wait_for_admission()
                        tick = time.monotonic()  # Waiting is not active time of the next account.
                        self.account = self.db.reserve(self.run, self.planned_draws())
                    except AllocationClosed as error:
                        reason = 'batch_quota' if self.db.batch_quota_exhausted(self.run) else 'allocation_deadline'
                        self.run.update(status='complete', end_reason=reason, finished_at=time.time())
                        self.db.save_run(self.run)
                        self.log.event('allocation_closed', reason=reason, deadline=self.run.get('allocation_deadline'))
                        self.publish(message=str(error))
                        return
                    if self.task_permit is not None:
                        self.ctx['proxy_task_id'] = self.task_permit.task_id
                    self.log.event("account_started", account=self.account["id"])
                    self.account['recognition_source'] = 'settlement'
                    self.account['catalog_snapshot'] = self.catalog_directory.relative_to(ROOT).as_posix()
                    from ournotes.catalogs.cards import sha256
                    self.account['catalog_fingerprints'] = {name: sha256(self.catalog_directory / name)
                                                           for name in ('catalog.json', 'pools.json')}
                    self.save()
                    self.last_account_tick = tick
                    self.last_action = None
                    self.last_progress = tick
                with self.log.timed("background_collect", phase=self.account["phase"]):
                    self.draw_flow.check_recognition()
                with self.log.timed("capture", phase=self.account["phase"]):
                    image, _ = self.device.capture()
                with self.log.timed("vision", phase=self.account["phase"]) as metric:
                    o = self.vision.read(image, phase=self.account["phase"], account_id=self.account["id"],
                                         last_action=self.last_action[0] if self.last_action else None)
                    metric.update(mode=self.vision.last_mode, scene=o.scene, **self.vision.last_metrics)
                self.observation = o
                if o.scene not in ('downloading', 'loading', 'unknown'):
                    self.download_percent = None
                with self.log.timed("frame_history", phase=self.account["phase"]):
                    self.log.observe(o.image)
                self.account["active_seconds"] += time.monotonic() - self.last_account_tick
                self.last_account_tick = time.monotonic()
                self.same_scene = self.same_scene + 1 if o.scene == self.last_scene else 1
                if o.scene != self.last_scene:
                    self.log.event("scene", scene=o.scene, phase=self.account["phase"],
                                   evidence=self.log.screenshot(o.image, o.scene))
                    self.last_scene = o.scene
                if o.scene != "unknown" and o.scene != self.last_known_scene:
                    self.last_known_scene = o.scene
                    self.last_progress = tick
                if not self.pause_reason or self.pending_login():
                    self.login_flow.check_login_feedback()
                    self.login_flow.check_mail()
                with self.log.timed("publish", phase=self.account["phase"]):
                    self.publish(scene=o.scene)
                if self.recover_loading_stall(tick) or self.recover_logout_title_stall(time.monotonic()):
                    continue
                with self.log.timed("step", phase=self.account["phase"], scene=o.scene):
                    self.step()
                if self.observation is None:
                    continue  # A restart during the step invalidates the captured frame.
                if self.account:
                    with self.log.timed("checkpoint", phase=self.account["phase"]):
                        self.save()
                self.log.event("cycle", seconds=time.monotonic() - tick, scene=o.scene,
                               phase=self.account["phase"] if self.account else "finished", mode=self.vision.last_mode)
                self.check_progress_timeout(time.monotonic())
                # Sampling throttle; all completion remains driven by current UI/API evidence.
                interval = .25 if self.account and self.account["phase"] == "login" and self.ctx.get("send_intent") and not self.code else .1
                # A set global stop event must not turn settlement draining into a busy loop.
                waiter = threading.Event() if self.pause_reason else self.stop
                waiter.wait(max(0, interval - (time.monotonic() - tick)))
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - persist before leaving runner boundary
            reason = str(error) if isinstance(error, (RuntimeError, ValueError, KeyboardInterrupt)) else type(error).__name__
            self.run["status"] = "paused"
            self.run["pause_reason"] = reason
            self.db.save_run(self.run)
            if self.account:
                self.save()
                self.db.export(self.account["id"])
            self.log.incident(reason, {"run": self.run, "account_id": self.account["id"] if self.account else None,
                                      "transaction_ids": [d['id'] for d in self.db.draws(self.account['id'])
                                                          if d['status'] in ('submitted', 'captured', 'needs_review')] if self.account else [],
                                      "phase": self.account["phase"] if self.account else None,
                                      "trace": traceback.format_tb(error.__traceback__)})
            self.publish(message=reason)
        finally:
            try:
                with self.log.timed("recognition_drain"):
                    try:
                        self.draw_flow.check_recognition()
                    finally:
                        self.recognition.close()
                    self.draw_flow.check_recognition(schedule=False)
            finally:
                try:
                    if self.device:
                        self.device.close()
                    self.executor.shutdown(wait=True, cancel_futures=True)
                    self.mail.close()
                    self.log.event("run_finished", status=self.run["status"])
                    self.log.close()
                finally:
                    try:
                        self.release_proxy_task()
                    finally:
                        self.db.close()

    def apply_manual_recovery(self):
        request = self.run.get('recovery_request', {})
        if request.get('status') != 'requested':
            return
        from ournotes.control.operations import OperationStore
        from ournotes.control.recovery import require_recovery, runtime_recovery
        operations = OperationStore(self.db.root)
        command_id = request['command_id']
        try:
            # Recheck after worker initialization, before any CLI/SDK action.
            checked = dict(self.run)
            checked.pop('recovery_request', None)
            require_recovery(runtime_recovery(self.db, self.db.get_batch(), checked), request['mode'])
            saved = self.db.get_run(self.run['id'])
            if (self.stop.is_set() or request.get('account_id') != self.run.get('active_account')
                    or saved.get('active_account') != self.run.get('active_account')
                    or saved['status'] != 'paused'):
                raise ValueError('恢复已停止或活动账号已变化；未操作设备。')
            request.update(status='starting', started_at=time.time())
            self.db.save_run(self.run)  # Never replay an interrupted restart automatically.
            if request['mode'] == 'restart_game':
                self.publish(message='正在重启游戏，保留原账号和交易')
                self.device_touched = True
                ensure_game(self.instance, self.stop)
                with self.log.timed('manual_restart_game', instance=self.instance):
                    restart_game(self.instance, self.stop)
                self.restore_after_game_restart()
            request.update(status='complete', finished_at=time.time())
            self.run.setdefault('recovery_audit', []).append(dict(request))
            self.db.save_run(self.run)
            operations.mark(command_id, 'succeeded', {'message': '所选实例已接入正常状态机；后续进展请看实例状态。',
                                                     'instances': [self.instance]})
        except Exception as error:
            if request.get('status') == 'requested':
                request.update(status='failed', finished_at=time.time())
                self.db.save_run(self.run)
            operations.mark(command_id, 'failed', {'message': '恢复未完成，请核对实例状态与原回执。',
                                                   'error_type': type(error).__name__})
            raise

    def restore_after_game_restart(self):
        if not self.account:
            return
        if self.account['phase'] in ('rewards', 'gifts', 'draws'):
            if self.account['phase'] == 'draws':
                self.ctx['gifts_done'] = True
            self.account['phase'] = 'rewards'
            self.ctx['uid_verified'] = False
        elif self.account['phase'] == 'login' and not self.ctx.get('login_submitted'):
            for key in LOGIN_FORM_FIELDS:
                self.ctx.pop(key, None)
        self.save()

    def recover_loading_stall(self, now):
        """A sustained blank/loading episode may restart the app once, never the VM."""
        if (self.observation.scene != 'loading' or self.pause_reason
                or self.stop.is_set()):
            self.loading_episode = None
            return False
        if self.submitted_draws():
            # The draw watchdog grants 180 seconds, including card-art loading.
            # Never restart the app while it may still deliver this result.
            self.loading_episode = None
            return False
        episode = self.loading_episode
        if not episode or episode[0] != self.account['id']:
            self.loading_episode = (self.account['id'], now)
            return False
        if now - episode[1] < 90:
            return False
        return self.restart_stalled_game('连续90秒加载或黑屏；只重启游戏，保留原实例、账号和交易。')

    def recover_logout_title_stall(self, now):
        """A sent logout may leave the game's SDK login entry unresponsive."""
        waiting = self.title_start_wait
        if (not self.account or self.account['phase'] != 'logout'
                or not self.ctx.get('logout_requested') or self.observation is None
                or self.observation.scene != 'title'
                or not waiting or waiting['account_id'] != self.account['id']
                or now - waiting['started_at'] < 90 or self.pause_reason or self.stop.is_set()):
            return False
        draws = self.db.draws(self.account['id'])
        if not draws or any(draw['status'] != 'complete' for draw in draws):
            raise PauseRun('退出登录时抽卡交易未全部完成，禁止重启游戏；保留原交易核查。')
        if self.ctx.get('login_network_uncertain'):
            raise PauseRun('发码或登录请求结果尚未确认，禁止重启游戏；保留原票据核查。')
        return self.restart_stalled_game(
            '退出登录后TAP TO START等待90秒仍未进入登录页；只重启游戏，保留原账号和交易。')

    def restart_stalled_game(self, reason):
        """Share one durable automatic restart budget across known stalled pages."""
        if self.pause_reason or self.stop.is_set():
            return False
        if any(d['status'] in ('submitted', 'captured', 'needs_review')
               for d in self.db.draws(self.account['id'])):
            raise PauseRun('页面停滞且有未决抽卡或待复核结果，禁止重启游戏；保留交易核查。')
        if mail_limit_reached(self.run):
            raise PauseRun(MAIL_LIMIT_MESSAGE)
        if self.ctx.get('game_restart_attempts', 0) >= 1:
            raise PauseRun('当前账号重启游戏一次后仍未恢复；停止自动重启。')
        self.ctx.update(game_restart_attempts=1, game_restart_at=time.time())
        # Persist the budget before any side effect; interruption cannot reset it.
        self.save()
        self.log.incident(reason, {'account_id': self.account['id'], 'phase': self.account['phase']})
        self.log.event('game_restart_intent', account=self.account['id'], instance=self.instance)
        self.device.close()
        self.device = None
        with self.log.timed('restart_game', instance=self.instance):
            self.device_touched = True
            restart_game(self.instance, self.stop)
        self.device = DeviceClient(self.instance)
        self.vision = Observer()
        self.restore_after_game_restart()
        self.last_action = self.last_scene = self.last_known_scene = None
        self.observation = None
        self.same_scene = self.navigation_attempts = 0
        self.result_stability = None
        self.pool_navigation.invalidate()
        self.loading_episode = None
        self.title_start_wait = None
        self.last_progress = time.monotonic()
        self.log.event('game_restarted', account=self.account['id'], instance=self.instance)
        return True

    def check_progress_timeout(self, now):
        # The current step can finish and clear the account before this check.
        # The next loop owns quota checks and allocation; no account is stalled here.
        if self.account is None or self.pause_reason:
            return
        scene = self.observation.scene
        pending = self.submitted_draws()
        if pending:
            draw_id = pending[0]['id']
            if not self.draw_wait_started or self.draw_wait_started[0] != draw_id:
                self.draw_wait_started = (draw_id, now)
            if now - self.draw_wait_started[1] >= DRAW_WAIT_SECONDS:
                self.abandon_submission('已提交抽卡180秒未取得结果证据，放弃账号且禁止补抽。')
            return
        self.draw_wait_started = None
        limit = 300 if scene == 'downloading' else 90
        if (self.account['phase'] == 'login' and self.mail_job is not None and not self.mail_job.done()
                and self.mail_job_kind in ('prepare', 'retry', 'mark_sent', 'poll')
                and self.mail_job_started_at is not None
                and now - self.mail_job_started_at < MAIL_REQUEST_BUDGET_SECONDS):
            return  # The bounded transport wait owns this login interval.
        if scene == 'loading':
            return  # The separate loading-restart policy owns this case.
        if now - self.last_progress < limit:
            return
        if scene == 'downloading':
            raise PauseRun('数据下载300秒未检测到进度增加；保留当前账号，等待检查网络或存储。')
        raise PauseRun('90秒未获得可确认的流程进展，当前页面：' + scene)

    def abandon_submission(self, reason):
        from ournotes.runtime.abandon import retire_submission_error
        audit = retire_submission_error(self.db, self.run, self.account, reason)
        self.log.event('account_abandoned', **audit)
        self.account = None
        self.release_proxy_task()
        self.last_action = None
        self.draw_wait_started = None
        self.pool_navigation.invalidate()

    def reset_after_abandonment(self):
        reset = self.run.get('abandoned_game_reset')
        if not reset or reset['status'] == 'complete':
            return
        self.check_pause()  # A user stop still prevents starting the next account.
        reset.update(status='restarting', started_at=time.time())
        self.db.save_run(self.run)
        if self.device:
            self.device.close()
            self.device = None
        self.device_touched = True
        restart_game(self.instance, self.stop)
        self.device = DeviceClient(self.instance)
        self.vision = Observer()
        self.observation = None
        self.last_action = self.last_scene = self.last_known_scene = None
        self.result_stability = None
        self.loading_episode = None
        self.pool_navigation.invalidate()
        reset.update(status='complete', finished_at=time.time())
        self.db.save_run(self.run)
        self.log.event('abandoned_account_game_reset', account_id=reset['account_id'])

    def recover_network_error(self):
        if self.submitted_draws():
            self.abandon_submission('已提交抽卡期间发生网络错误，交易需要核对。')
            return
        if self.account['phase'] == 'login' and (self.ctx.get('send_intent') or self.ctx.get('login_submitted')):
            # Returning to title is not authentication evidence; retry could send
            # another code/login request. Persist this even if the UI later changes.
            self.ctx['login_network_uncertain'] = True
            self.save()
            raise PauseRun('发码或登录请求期间发生网络错误；保留原票据和提交状态，暂停核查。')
        retries = self.ctx.get('network_retries', 0)
        now = time.time()
        # Allow the last click's response to arrive before exhausting the budget.
        if now < self.ctx.get('network_retry_at', 0):
            return
        if retries >= len(NETWORK_BACKOFF_SECONDS):
            raise PauseRun('网络重试次数已达上限。')
        if 'network_retry_at' not in self.ctx:
            self.ctx['network_retry_at'] = now + NETWORK_BACKOFF_SECONDS[retries]
            self.save()
            return
        o = self.observation
        target = (o.find('重试', exact=True, region=(300, 600, 1300, 895))
                  or o.find('返回标题画面', exact=True, region=(300, 600, 1300, 895)))
        if target and self.action(target.text, args=target.center):
            self.ctx['network_retries'] = retries + 1
            delay = NETWORK_BACKOFF_SECONDS[min(retries + 1, len(NETWORK_BACKOFF_SECONDS) - 1)]
            self.ctx['network_retry_at'] = now + delay
            if target.text == '返回标题画面' and self.account['phase'] in ('rewards', 'gifts', 'draws'):
                self.ctx['uid_verified'] = False
                self.phase('rewards')
            self.save()

    def step(self):
        # Gate before any transaction/login intent is saved. A deferred click must
        # never leave a "submitted" intent that was not actually sent.
        if time.monotonic() < self.next_action_at:
            return
        if self.check_pause():
            self.settle_before_pause()
            return
        o = self.observation
        if o.scene in ('login_history', 'login_options', 'email_form', 'user_center', 'home'):
            self.title_start_wait = None
        if o.scene in OPENING_SCENES:
            if self.submitted_draws():
                self.abandon_submission('已提交抽卡期间出现开屏演出；放弃账号并保留交易。')
                return
            # Bound the whole movie/confirmation chain, including alternating
            # pages and unknown/loading frames, not just repeated identical taps.
            now = time.monotonic()
            if self.opening_started_at is None:
                self.opening_started_at, self.opening_actions = now, 0
            if now - self.opening_started_at > 60 or self.opening_actions >= 12:
                raise PauseRun('开屏演出处理超过60秒或12次操作；保留现场核查。')
            target, label = OPENING_CONTROLS[o.scene]
            if self.click(target, exact=True, label=label):
                self.opening_actions += 1
            return
        if o.scene not in ('unknown', 'loading'):
            self.opening_started_at = None
        if o.scene == 'server_maintenance':
            raise PauseRun('游戏服务器维护中；保留当前账号，确认实际开服后从控制台恢复。')
        if o.scene == 'required_update':
            raise PauseRun('游戏要求更新客户端；请覆盖安装官方新版本后恢复当前批次。')
        if o.scene == 'enable_prompt':
            self.click('关闭额外弹窗', exact=True)
            return
        if o.scene == 'title_menu_unavailable':
            self.recover_unavailable_menu()
            return
        if o.scene == 'paid_shortage':
            if self.ctx.get('paid_dialog_dismissals', 0) >= 3:
                raise PauseRun('意外付费资源提示反复出现，停止导航。')
            if self.click('关闭', exact=True):
                self.ctx['paid_dialog_dismissals'] = self.ctx.get('paid_dialog_dismissals', 0) + 1
            return
        if o.scene == "network_error":
            self.recover_network_error()
            return
        if o.scene not in ('loading', 'unknown'):
            self.ctx.pop('network_retry_at', None)
        if o.scene == 'download_confirm':
            self.click('OK', exact=True)
            return
        if o.scene == 'downloading':
            self.observe_download()
            return
        if o.scene == 'loading':
            return
        if o.scene == "review":
            self.click("稍后再说")
            return
        if o.scene == "shop":
            self.action("back-from-shop", args=(62, 50))
            return
        if o.scene == "character_dialogue" and self.account["phase"] in ("rewards", "gifts", "logout"):
            self.action("advance-character-dialogue", args=(1100, 770))
            return
        phase = self.account["phase"]
        if phase in ("switch_account", "logout"):
            self.login_flow.switch_account()
        elif phase == "login":
            self.login_flow.login()
        elif phase in ("rewards", "gifts"):
            self.reward_flow.rewards()
        elif phase == "draws":
            self.draw_flow.draws()
        else:
            raise PauseRun("未知任务阶段：" + phase)

    def observe_download(self):
        """Only verified progress extends the wait; animation alone is not progress."""
        now = time.monotonic()
        if now - self.download_read_at < 2:
            return
        self.download_read_at = now
        self.read_fields('download_progress')
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', self.observation.text)
        if not match or not 0 <= (percent := float(match[1])) <= 100:
            return
        previous = self.download_percent
        if previous is None or percent > previous:
            self.download_percent = percent
            self.last_progress = now
            self.log.event('download_progress', percent=percent, account=self.account['id'])
            self.publish(message=f'正在下载游戏数据：{percent:g}%')


    def recover_unavailable_menu(self):
        """Recover a grey button or three confirmed taps with no transition."""
        if self.submitted_draws():
            raise PauseRun('标题菜单不可用且有已提交交易，保留待核验，禁止自动切换入口。')
        attempts = self.ctx.get('title_auth_recovery_attempts', 0)
        if attempts >= 2:
            raise PauseRun('标题菜单用户中心持续不可用，已尝试两次返回入口；保留交易等待检查。')
        if self.click('关闭', exact=True, label='close-unavailable-menu'):
            self.ctx.update(title_auth_recovery=True, title_auth_recovery_attempts=attempts+1)
            if self.account['phase'] in ('rewards', 'gifts'):
                self.ctx['uid_verified'] = False
            self.save()
            self.log.event('title_user_center_fallback', account=self.account['id'],
                           scene=self.observation.scene, attempt=attempts+1)

    def recover_unopened_menu(self):
        if self.submitted_draws():
            raise PauseRun('标题菜单不可用且有已提交交易，保留待核验，禁止自动切换入口。')
        attempts = self.ctx.get('title_auth_recovery_attempts', 0)
        if attempts >= 2:
            raise PauseRun('标题菜单三次未生效且备用入口恢复已达上限。')
        self.ctx.update(title_auth_recovery=True, title_auth_recovery_attempts=attempts + 1)
        if self.account['phase'] in ('rewards', 'gifts'):
            self.ctx['uid_verified'] = False
        self.save()
        self.last_action = None
        self.log.event('title_menu_fallback', account=self.account['id'], attempt=attempts + 1)











    def finish_account(self):
        self.draw_flow.check_recognition()
        draws = self.db.draws(self.account["id"])
        if any(d["status"] == "captured" for d in draws):
            self.publish(message="账号操作完成，等待后台识别收尾")
            return
        if any(d["status"] == "needs_review" for d in draws):
            raise PauseRun("账号操作完成，但后台识别存在待复核结果；原图与已识别内容已保存。")
        self.account.update(status="partial" if self.account["skipped"] else "complete",
                            phase="finished", finished_at=time.time())
        self.account.pop('game_uid', None)
        self.account.pop('pull_count', None)
        if (self.account['status'] == 'complete' and draws and len(draws) == len(self.account['plan'])
                and all(d['status'] == 'complete'
                        and len(d.get('result', {}).get('slots', [])) == d['pulls']
                        and all(s['status'] == 'matched' for s in d['result']['slots']) for d in draws)):
            pulls = sum(d['pulls'] for d in draws)
            if pulls == sum(e['pulls'] for e in self.account['plan']):
                self.account['pull_count'] = pulls
        with self.db.transaction():
            self.save()
            self.run["completed"] += 1
            self.run["active_account"] = None
            self.db.save_run(self.run)
        report = self.db.export(self.account["id"])
        self.log.event("account_finished", account=self.account["id"],
                       seconds=self.account["active_seconds"], totals=report["totals"])
        self.log.clear_frames()
        self.account = None
        self.release_proxy_task()
        if report['status'] == 'complete':
            from ournotes.integrations.cloud_sync import CloudSync
            uploaded = CloudSync(self.db.root).upload_completed(report['id'])
            self.log.event('account_upload', **uploaded)
        self.last_action = None
        if not any(d["status"] == "complete" for d in report["draws"]):
            raise PauseRun("本账号所有抽卡项目均不可执行，整批已暂停。")
