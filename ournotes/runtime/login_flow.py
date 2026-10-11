"""Persistent OTP attempts sharing one audited retry for rejection or expiry."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ournotes.runtime.login_history import history_control
from ournotes.runtime.model import LOGIN_FORM_FIELDS, MAIL_LIMIT_MESSAGE, PauseRun
from ournotes.ui.login_fields import focused

if TYPE_CHECKING:
    from ournotes.runtime.runner import Runner


def request_code_retry(account, *, reason='game_rejected', trigger='game_feedback'):
    """Persist this transition before resetting the form or contacting the server."""
    ctx = account['context']
    if account['phase'] != 'login' or not ctx.get('otp'):
        raise ValueError('只能重发当前登录的原验证码请求。')
    rejected = reason == 'game_rejected' and ctx.get('login_submitted')
    expired = (reason == 'expired' and not ctx.get('login_submitted')
               and ctx['otp']['expires_at'] <= time.time())
    misdirected = reason == 'input_misdirected' and ctx.get('code_ready') and ctx.get('code_filled')
    if not (rejected or expired or misdirected):
        raise ValueError('仅明确拒绝、输入错位或提交登录前已过期的验证码可以重发。')
    if ctx.get('otp_retry'):
        raise PauseRun('验证码已重发一次，仍出错或过期；保留两次票据与现场，暂停核查。')
    ctx['otp_retry'] = {'original': dict(ctx['otp']), 'requested_at': time.time(),
                        'reason': reason, 'trigger': trigger, 'stage': 'reset_form'}


class LoginFlow:
    def __init__(self, runner: Runner):
        self.runner = runner

    def input_focused(self, field):
        r = self.runner
        if focused(r.observation.image, field):
            return True
        sent = r.db.last_sent_action_at(r.account['id'], 'focus-' + field, 'login')
        if sent is None or time.time() - sent < 3:
            return False
        if r.ctx.get('focus_retried', {}).get(field):
            raise PauseRun('输入框焦点修复一次后仍未生效：' + field)
        if r.action('release-login-touch', kind='release_touch'):
            r.ctx.setdefault('focus_retried', {})[field] = True
            r.ctx.pop(field + '_focus', None)
            r.save()
        return False

    def mail_submit(self, kind, *args):
        r = self.runner
        if r.mail_job is None:
            if kind in ('prepare', 'retry'):
                r.check_memory()
            r.mail_job_kind = kind
            r.mail_job_started_at = time.monotonic()
            def call():
                started = time.time()
                tick = time.perf_counter()
                value = getattr(r.mail, kind)(*args)
                return value, started, time.perf_counter() - tick
            r.mail_job = r.executor.submit(call)


    def check_mail(self):
        r = self.runner
        if r.mail_job is None or not r.mail_job.done():
            return
        value, started, seconds = r.mail_job.result()
        kind = r.mail_job_kind
        r.log.event("mail_timing", operation=kind, started_at=started, seconds=seconds)
        r.mail_job = None
        r.mail_job_kind = None
        r.mail_job_started_at = None
        r.last_progress = time.monotonic()
        if kind in ('prepare', 'retry'):
            if value['status'] == 'expired':
                if (kind == 'retry' and r.ctx.get('proxy_limit_retry')
                        and not r.ctx.get('otp_retry') and not r.ctx.get('login_submitted')
                        and not r.ctx.get('send_intent') and value['expires_at'] <= time.time()):
                    # The idempotent limit-replacement receipt can expire during a pause.
                    # Adopt that receipt before using the ordinary single expiry retry.
                    recovery = r.ctx.pop('proxy_limit_retry')
                    recovery.update(stage='expired_before_send', ticket=value['ticket'])
                    r.ctx.setdefault('proxy_limit_history', []).append(recovery)
                    r.ctx['otp'] = value
                    request_code_retry(r.account, reason='expired', trigger='limit_replacement_expired')
                    r.resumed_login_account = None
                    r.last_action = None
                    r.save()
                    return
                raise PauseRun('验证码票据在发码前已过期；保留请求，暂停核查。')
            r.ctx["otp"] = value
            r.ctx["mail_ready"] = True
            if kind == 'retry':
                recovery = r.ctx.get('proxy_limit_retry') or r.ctx['otp_retry']
                recovery.update(stage='ready', ticket=value['ticket'])
                if r.ctx.get('proxy_limit_retry'):
                    r.ctx.setdefault('proxy_limit_history', []).append(r.ctx.pop('proxy_limit_retry'))
            r.save()
        elif kind == "poll":
            if r.ctx.get('proxy_limit_retry', {}).get('stage') in ('rotate', 'reset_form', 'prepare'):
                return  # The explicitly rejected request is being superseded.
            # Mail has its own bounded deadline; do not let the UI idle watchdog
            # interrupt it before that deadline.
            r.last_progress = time.monotonic()
            if value["status"] == "received":
                r.code = value["code"]
                r.ctx["code_ready"] = True
                r.save()
            elif value["status"] == "expired":
                request_code_retry(r.account, reason='expired', trigger='cloud_expired')
                r.save()
                r.last_action = None
                r.last_progress = time.monotonic()


    def resume_expired_login(self):
        r = self.runner
        if r.resumed_login_account != r.account['id']:
            return
        # Restore the same request; a confirmed expiry shares the one retry budget.
        r.resumed_login_account = None
        otp = r.ctx.get('otp')
        if not otp:
            return
        if r.ctx.get('login_submitted'):
            return
        if otp['expires_at'] > time.time():
            # The emulator may have reopened an empty form. Rebuild only the
            # inputs; keep the live ticket, send intent and remaining budget.
            for key in LOGIN_FORM_FIELDS:
                r.ctx.pop(key, None)
            r.last_action = None
            r.save()
            return
        request_code_retry(r.account, reason='expired', trigger='resume_expired')
        r.save()
        r.last_action = None
        r.last_progress = time.monotonic()
        return True


    def check_login_feedback(self):
        r = self.runner
        recovery = r.ctx.get('proxy_limit_retry') if r.account else None
        if recovery and recovery['stage'] == 'rotate':
            if r.task_gate is None:
                raise PauseRun('邮箱限额恢复需要代理协调器，保留账号。')
            try:
                r.task_gate.report_mail_limit('otp-' + recovery['original']['ticket'])
            except Exception as error:
                raise PauseRun('邮箱限额节点切换失败：' + str(error) if isinstance(error, ValueError)
                               else '邮箱限额节点切换未确认，保留账号。') from error
            recovery['stage'] = 'reset_form'
            r.resumed_login_account = None
            r.last_action = None
            r.last_progress = time.monotonic()
            r.save()
            return
        if recovery and recovery['stage'] in ('reset_form', 'prepare'):
            return
        if not r.account or r.account['phase'] != 'login' or r.observation.scene != 'email_form':
            return
        now = time.monotonic()
        if now - r.last_login_feedback_at >= .5:
            r.read_fields('login_feedback')
            r.last_login_feedback_at = now
        if r.observation.has(MAIL_LIMIT_MESSAGE):
            if r.task_gate is not None and r.ctx.get('otp') and r.ctx.get('send_intent') and not r.ctx.get('login_submitted'):
                previous = r.ctx.get('proxy_limit_retry')
                if previous:
                    r.ctx.setdefault('proxy_limit_history', []).append(previous)
                r.ctx['proxy_limit_retry'] = {'original': dict(r.ctx['otp']), 'reason': 'proxy_limit',
                                            'stage': 'rotate', 'requested_at': time.time()}
                r.save()
                return
            r.run['mail_send_limit_reached'] = True
            r.run['pause_reason'] = MAIL_LIMIT_MESSAGE
            r.db.save_run(r.run)
            raise PauseRun(MAIL_LIMIT_MESSAGE + '；已停止本轮，保留账号，不再重试发码。')


    def switch_account(self):
        r = self.runner
        o = r.observation
        if r.account["phase"] != "logout" and o.scene.startswith("login_history"):
            self.clear_login_history()
            return
        if o.scene in ("login_history", "login_options", "email_form"):
            if r.account["phase"] == "logout":
                r.finish_account()
                return
            if o.scene == "login_history":
                r.click("切换其他账号")
            elif o.scene == "login_options":
                r.ctx["history_cleared"] = True
                r.click("账号登录")
            else:
                r.ctx.pop('logout_requested', None)
                r.phase("login")
            return
        if r.ctx.get('logout_requested') and o.scene in ('pool', 'home', 'home_menu', 'user_center'):
            # Logout is asynchronous: the old menu briefly remains interactive.
            # Reopening the SDK user center here races the pending logout.
            # Only a verified login page can finish the account; the normal
            # progress deadline still bounds a transition that never completes.
            return
        if o.scene == "pool":
            self.open_menu("pool-menu")
        elif o.scene == "home":
            self.open_menu("home-menu")
        elif o.scene == "title":
            if r.ctx.get('title_auth_recovery') or r.ctx.get('logout_requested'):
                r.click('TAP TO START', exact=True)
            else:
                r.action("title-menu", args=(1541, 58))
        elif o.scene == "title_menu":
            r.click("用户中心", exact=True)
        elif o.scene == "user_center":
            if r.click("退出登录"):
                r.ctx['logout_requested'] = True
                r.save()
        elif o.scene == "home_menu":
            # Returning to title can leave the SDK user center disabled. The
            # in-game menu exposes the same logout route without that transition.
            r.click("用户中心", exact=True)
        elif o.scene == "return_title_confirm":
            r.click("OK", exact=True)
        elif o.scene == "notice":
            r.click("关闭", exact=True)
        elif o.scene == "startup_reward":
            r.reward_flow.dismiss_reward()

    def open_menu(self, label):
        r = self.runner
        phase = r.account['phase']
        released = r.ctx.get('menu_touch_released', {})
        if not released.get(phase):
            sent_at = r.db.last_sent_action_at(r.account['id'], label, phase)
            if sent_at is not None:
                # A lost SDK touch-up leaves the menu icon pressed and therefore
                # correctly fails the normal click guard. Finish that input once;
                # the persisted action also covers resuming this exact account.
                if time.time() - sent_at >= 3:
                    r.ctx.setdefault('menu_touch_released', {})[phase] = True
                    r.save()
                    r.action('release-menu-touch', kind='release_touch')
                return
        r.action(label, args=(1541, 51))


    def clear_login_history(self):
        r = self.runner
        o = r.observation
        if o.scene == "login_history_confirm":
            if r.click("确定", exact=True, region=(800, 450, 990, 530),
                          label="confirm-remove-login-history"):
                r.ctx["history_removed"] = r.ctx.get("history_removed", 0) + 1
            return
        control = "expand" if o.scene == "login_history" else "remove"
        target = history_control(o.image, control)
        if target:
            r.action("expand-login-history" if control == "expand" else "remove-login-history", args=target)


    def login(self):
        r = self.runner
        o = r.observation
        ctx = r.ctx
        retry = ctx.get('proxy_limit_retry') or ctx.get('otp_retry')
        if retry and retry.get('stage') == 'rotate':
            return
        if o.scene == 'network_error' and (ctx.get('send_intent') or ctx.get('login_submitted')):
            r.recover_network_error()
        if ctx.get('login_network_uncertain'):
            raise PauseRun('原发码或登录请求发生网络错误，结果未确认；保留票据，暂停核查。')
        if retry and retry.get('stage') in ('reset_form', 'prepare'):
            if r.resumed_login_account == r.account['id']:
                r.resumed_login_account = None
                retry['stage'] = 'reset_form'
                r.save()
            if retry['stage'] == 'reset_form':
                if o.scene == 'title':
                    r.click('TAP TO START', exact=True)
                    return
                if o.scene == 'email_form':
                    self.check_login_feedback()
                    if not retry.get('touch_released'):
                        if r.action('release-login-touch', kind='release_touch'):
                            retry['touch_released'] = True
                            r.save()
                        return
                    r.action('reset-email-form', args=(612, 258))
                    return
                if o.scene != 'login_options':
                    return
                for key in (*LOGIN_FORM_FIELDS, 'code_ready', 'login_submitted', 'mail_ready', 'send_intent'):
                    ctx.pop(key, None)
                r.code = None
                r.last_action = None
                retry['stage'] = 'prepare'
                r.save()
        if o.scene == 'title' and not ctx.get('login_submitted'):
            # A restart loses the form, even when no OTP request was prepared yet.
            # Re-enter through the ordinary logout/history path with the same alias.
            for key in LOGIN_FORM_FIELDS:
                ctx.pop(key, None)
            r.phase('switch_account')
            return
        if o.scene == "login_options":
            r.click("账号登录")
            return
        if o.scene == "title" and ctx.get("login_submitted"):
            ctx["login_confirmed"] = True
            if ctx.get("otp") and r.mail_job is None:
                self.mail_submit("finish", ctx["otp"]["ticket"])
            r.phase("rewards")
            return
        if o.scene != "email_form":
            return
        self.check_login_feedback()
        if ctx.get('proxy_limit_retry', {}).get('stage') == 'rotate':
            return
        if self.resume_expired_login():
            return
        if ctx.get("login_submitted"):
            if o.has("错误") or o.has("失效"):
                request_code_retry(r.account)
                r.save()
                r.log.incident('游戏拒绝验证码，准备唯一一次重发。',
                               {'account_id': r.account['id'], 'phase': 'login'}, event='login_code_retry')
                r.last_action = None
                r.last_progress = time.monotonic()
            return
        if not ctx.get("email_filled") and not o.has("请输入邮箱地址"):
            r.read_fields("email_input")
        if not ctx.get("email_filled") and not o.has("请输入邮箱地址"):
            if r.action("reset-email-form", args=(612, 258)):
                ctx.pop("email_focus", None)
                ctx.pop("agreement", None)
            return
        if not ctx.get("email_focus"):
            if r.action("focus-email", args=(722, 380)):
                ctx["email_focus"] = True
            return
        if not ctx.get("email_filled"):
            if not self.input_focused('email'):
                return
            if r.action("fill-email", kind="input_text", args=(r.account["alias"].split("@")[0],)):
                ctx["email_filled"] = True
            return
        if not ctx.get("agreement"):
            if r.action("accept-game-terms", args=(608, 516)):
                ctx["agreement"] = True
            return
        if not ctx.get("mail_ready"):
            retry = ctx.get('proxy_limit_retry') or ctx.get('otp_retry')
            if retry and retry['stage'] == 'prepare':
                self.mail_submit('retry', retry['original']['ticket'], r.account['id'], retry['reason'])
            else:
                self.mail_submit("prepare", r.account["alias"], r.account["id"])
            return
        if not ctx.get("send_intent"):
            if not o.find("发送验证码"):
                r.read_fields("login_send")
            if not o.find("发送验证码"):
                return
            r.check_memory()
            if r.task_permit is None:
                ctx["send_intent"] = True
                r.save()
                sent = r.click("发送验证码")
                if not sent:
                    ctx.pop('send_intent', None)
                    r.save()
            else:
                def perform(commit):
                    def before_input():
                        commit()
                        ctx['send_intent'] = True
                        r.save()
                    return r.click('发送验证码', before_input=before_input)
                try:
                    sent = r.task_permit.send('otp-' + ctx['otp']['ticket'],
                                              'login_retry' if ctx.get('otp_retry') else 'login', perform)
                except Exception as error:  # a failed guard/submission must retain the local pause
                    r.stop.set()
                    if ctx.get('send_intent'):
                        ctx['login_network_uncertain'] = True
                        r.save()
                    message = str(error) if isinstance(error, ValueError) else (
                        '代理发信提交未确认，保留票据和现场；禁止自动重发。')
                    raise PauseRun(message) from error
            if sent:
                self.mail_submit("mark_sent", ctx["otp"]["ticket"])
            return
        if not r.code:
            if r.mail_job is None and time.monotonic() - r.last_poll >= 2:
                r.last_poll = time.monotonic()
                self.mail_submit("poll", ctx["otp"]["ticket"], ctx["otp"]["expires_at"])
            return
        if not ctx.get("code_focus"):
            if r.action("focus-code", args=(729, 453)):
                ctx["code_focus"] = True
            return
        if not ctx.get("code_filled"):
            if not self.input_focused('code'):
                return
            r.read_fields("code_input")
            if not o.has("请输入验证码"):
                # Missing placeholder is not proof of existing input (focus/toast/OCR).
                # Reobserve without typing; the existing progress deadline bounds this.
                return
            if r.action("fill-code", kind="input_text", args=(r.code,)):
                ctx["code_filled"] = True
            return
        r.read_fields('code_input')
        if not o.find(r.code, exact=True, region=(610, 438, 862, 474)):
            if o.has('请输入验证码') and focused(o.image, 'email'):
                request_code_retry(r.account, reason='input_misdirected', trigger='empty_code_email_focused')
                r.save()
                r.log.incident('验证码输入错位，准备唯一一次重发。',
                               {'account_id': r.account['id'], 'phase': 'login'}, event='login_code_retry')
                r.last_action = None
                r.last_progress = time.monotonic()
            return
        if not o.find("登录/注册"):
            return
        ctx["login_submitted"] = True
        r.save()
        if r.click("登录/注册"):
            r.code = None
        else:
            # A foreground guard or input throttle sent no input. Uncertain
            # SDK exceptions retain the saved intent and must not be replayed.
            ctx.pop('login_submitted')
            r.save()
