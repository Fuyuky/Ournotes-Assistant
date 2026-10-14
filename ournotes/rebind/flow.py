"""State-observed device workflow, separate from draws and account allocation."""
import json
import logging
import random
import re
import time
import uuid
from types import SimpleNamespace

import numpy as np

from ournotes.integrations.cloud_client import CloudError
from ournotes.integrations.mumu import MuMuSdk, ensure_game
from ournotes.rebind.vision import field_text, input_focused, scene
from ournotes.runtime.input_policy import InputPolicy
from ournotes.runtime.mail import (
    MAIL_REQUEST_ATTEMPTS,
    MAIL_REQUEST_BACKOFF_SECONDS,
    MAIL_REQUEST_BUDGET_SECONDS,
)
from ournotes.ui.fields import Fields
from ournotes.ui.frame import Frame
from ournotes.ui.observer import Observer
from ournotes.vision.ocr import Observation


class Flow:
    def __init__(self, service, client, task, item, password):
        self.service, self.client, self.task, self.item, self.password = service, client, task, item, password
        self.device = None
        self.tickets = []
        self.observer = Observer(allow_fallback=False)
        self.fields = Fields()
        self.policy = InputPolicy()
        self.image = None
        self.observation = None
        self.last_step = "初始化"
        self.last_scene = "unknown"
        self.proxy_permit = None

    def read(self):
        self.service.check()
        self.image = self.device.capture().image
        page = scene(self.image)
        if page == 'loading':
            # The SDK spinner overlays an otherwise recognizable title/menu.
            self.observer.last_loading_at = time.monotonic()
            self.observation = Observation(self.image, [], 'loading', 'sdk_loading')
            return 'loading'
        self.observation = self.observer.read(self.image, phase='logout', account_id=self.task['id'])
        if self.observation.scene == 'network_error':
            raise ValueError('游戏网络连接失败；未重发邮件或登录，请核查后停止任务。')
        if self.observation.scene in ('loading', 'enable_prompt'):
            return self.observation.scene
        return page or self.observation.scene

    def dismiss_enable_prompt(self):
        control = self.observation.find('关闭额外弹窗', exact=True)
        if control is None:
            raise ValueError('通知弹窗关闭按钮未确认，未点击。')
        self.tap('enable_prompt', *control.center)

    def until(self, predicate, timeout=30):
        dismissals = 0
        deadline = time.monotonic() + timeout
        while True:
            current = self.read()
            self.last_scene = current
            if current == 'enable_prompt':
                if dismissals >= 3 or time.monotonic() >= deadline:
                    raise ValueError('通知弹窗未能关闭，已停止自动输入。')
                self.dismiss_enable_prompt()
                dismissals += 1
                continue
            if predicate(current):
                return current
            if time.monotonic() >= deadline:
                raise ValueError(f'页面等待超时：{self.last_step}；当前页面 {current}。已停止自动输入。')
            self.service.wait(.15)

    def tap(self, expected, x, y, *, before_input=None):
        if self.read() != expected:
            raise ValueError('点击前页面已变化；未发送输入。')
        with self.service.mutex:
            self.service.check()
            if before_input is not None:
                before_input()
            self.device.tap(*self.policy.point((x, y), self.observation))
        self.service.wait(self.policy.interval())

    def change(self, expected, x, y, wanted):
        self.last_step = expected + ' → ' + ', '.join(sorted(wanted))
        if expected == 'title' and (x, y) == (1541, 58):
            return self.open_title_menu(wanted)
        self.tap(expected, x, y)
        return self.until(lambda s: s in wanted)

    def open_title_menu(self, wanted):
        # Title controls can become visible before their first touch is accepted.
        # Retry only this reversible navigation, and only on a freshly observed title.
        started = time.monotonic()
        deadline = started + 30
        next_tap, attempts, dismissals = 0, 0, 0
        while True:
            current = self.read()
            self.last_scene = current
            if current == 'enable_prompt':
                if dismissals >= 3 or time.monotonic() >= deadline:
                    raise ValueError('通知弹窗未能关闭，已停止自动输入。')
                self.dismiss_enable_prompt()
                dismissals += 1
                continue
            if current in wanted:
                return current
            now = time.monotonic()
            if current == 'loading':
                deadline = started + 120  # One bounded SDK/network wait; no input during loading.
            if now >= deadline:
                raise ValueError(f'页面等待超时：{self.last_step}；当前页面 {current}。已停止自动输入。')
            if current == 'title' and attempts < 3 and now >= next_tap:
                self.tap('title', 1541, 58)
                attempts += 1
                next_tap = time.monotonic() + 3 * attempts
            self.service.wait(.15)

    def record_failure(self, error):
        # Preserve one original frame and non-secret context, even after task clearing.
        folder = self.service.root / 'results/maintenance/diagnostics/email-rebind' / self.task['id']
        folder.mkdir(parents=True, exist_ok=True)
        if self.image is not None:
            self.image.save(folder / 'failure.png')
        details = {
            'index': self.task['index'], 'instance': self.task['instance'],
            'stage': (self.service.status().get('task') or self.task)['state'], 'step': self.last_step,
            'scene': self.last_scene, 'error_type': type(error).__name__,
            'time': time.time(),
        }
        if isinstance(error, CloudError):
            details['http_status'] = error.status
            details['cloud_error_code'] = (error.code if error.code in
                                           {'unavailable', 'http_error', 'gmail_authorization_required'} else 'cloud_error')
        (folder / 'failure.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')

    def text(self, expected, field, value, binding=False):
        y = ((386, 470) if binding else (380, 454))[field]
        self.tap(expected, 715, y)
        self.until(lambda s: s == expected and input_focused(self.image, field, binding), 4)
        before = self.image.crop((610, y-17, 825, y+17)).tobytes()
        with self.service.mutex:
            self.service.check()
            self.device.input_text(value)
        self.until(lambda s: s == expected and self.image.crop((610, y-17, 825, y+17)).tobytes() != before, 5)

    def prepare(self, purpose):
        self.last_step = '准备原邮箱登录收码票据' if purpose == 'login' else '准备原邮箱认证票据'
        self.service.stage('preparing_mail', '正在建立云端收码监听；尚未发送游戏邮件。')
        body = {'index': self.task['index'], 'request_id': uuid.uuid4().hex, 'purpose': purpose}
        deadline = time.monotonic() + MAIL_REQUEST_BUDGET_SECONDS
        for attempt in range(MAIL_REQUEST_ATTEMPTS):
            self.service.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloudError('unavailable', '云端收码连接等待超时；未发送游戏邮件。')
            try:
                value = self.client.request('POST', '/rebind/tickets', body, timeout=remaining)
                self.tickets.append(value['ticket_id'])
                self.service.stage('preparing_mail', '云端收码监听已就绪，准备发送游戏邮件。')
                return value['ticket_id']
            except CloudError as error:
                remaining = deadline - time.monotonic()
                transient = error.code == 'unavailable' or error.status in (408, 429, 500, 502, 503, 504)
                base = MAIL_REQUEST_BACKOFF_SECONDS[min(attempt, len(MAIL_REQUEST_BACKOFF_SECONDS) - 1)]
                delay = random.uniform(base, base * 1.2)
                if not transient or attempt + 1 == MAIL_REQUEST_ATTEMPTS or delay >= remaining:
                    raise
                self.service.stage('preparing_mail', '正在建立云端收码监听，连接重试中；尚未发送游戏邮件。')
                self.service.wait(delay)

    def arm(self, ticket):
        self.service.check()
        result = self.client.request('POST', f'/rebind/tickets/{ticket}/arm', {})
        if result['status'] != 'sent':
            raise ValueError('邮件票据不可发送，未点击游戏发送按钮。')

    def mail(self, ticket, ready):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            self.service.check()
            self.read()
            feedback = field_text(self.fields, self.image, 'mail-feedback', (590, 340, 1010, 540))
            if '今日邮箱发送次数已达上限' in feedback:
                raise ValueError('今日邮箱发送次数已达上限；当前换绑任务停止，不会自动重试。')
            try:
                value = self.client.request('POST', f'/rebind/tickets/{ticket}/poll', {})
            except CloudError as error:
                if error.status is not None and error.status < 500:
                    raise
                self.service.wait(2)
                continue  # Same durable ticket; no new game send or confirmation replay.
            if value['status'] == ready:
                return value
            if value['status'] not in ('sent', 'prepared'):
                raise ValueError('邮件验证未确认或有歧义；保留当前游戏步骤，未自动重发。')
            self.service.wait(2)
        raise ValueError('等待邮件超时，未自动重发。')

    def cancel_ticket(self, ticket):
        self.client.request('POST', f'/rebind/tickets/{ticket}/cancel', {})
        self.tickets.remove(ticket)

    def login_form(self):
        # Always go through other-account login. Never use an emulator's saved identity.
        current = self.until(lambda s: s not in ('unknown', 'loading', 'unconfirmed'), 120)
        for _ in range(8):
            if current == 'email_form':
                current = self.change(current, 612, 258, {'login_options'})
            elif current == 'login_history':
                current = self.change(current, 800, 650, {'login_options'})
            elif current == 'login_options':
                self.change(current, 800, 546, {'email_form'})
                return
            elif current == 'title':
                current = self.change(current, 1541, 58, {'title_menu', 'title_menu_unavailable', 'login_history', 'login_options'})
            elif current == 'bindings':
                current = self.change(current, 615, 262, {'user_center'})
            elif current in ('verify_a', 'bind_form', 'confirm_b'):
                # A stopped task can leave an SDK subpage open. Only navigate back;
                # never replay its send or Next action when starting a different task.
                current = self.change(current, 615, 262, {'bindings', 'verify_a', 'bind_form', 'user_center'})
            elif current == 'title_menu_unavailable':
                self.change(current, 800, 716, {'title'})
                current = self.change('title', 1200, 730, {'login_history', 'login_options', 'email_form'})
            elif current in ('title_menu', 'home_menu'):
                control = self.observation.find('用户中心', exact=True)
                if not control:
                    raise ValueError('用户中心入口不可用，未点击。')
                current = self.change(current, *control.center, {'user_center'})
            elif current == 'user_center':
                current = self.change(current, 800, 610, {'login_history', 'login_options', 'email_form'})
            else:
                raise ValueError('请将所选实例停在标题页或登录页，再开始换绑。')
        raise ValueError('无法进入干净的邮箱登录表单。')

    def domain(self, expected, email, binding=False):
        domain = email.split('@')[1].lower()
        box = (838, 365, 966, 404)  # Exclude the dropdown arrow from the selected label.
        self.last_step = '选择邮箱域名 ' + domain
        if self.read() != expected:
            raise ValueError('选择邮箱域名前页面已变化，未输入。')
        if self.domain_value(field_text(self.fields, self.image, 'domain', box)) == domain:
            return email.split('@')[0]
        self.tap(expected, 974, 386)
        previous, direction = None, 'up'
        menu_box = (610, 432 if binding else 425, 981, 657 if binding else 650)
        for _ in range(12):
            image = self.stable_domain_menu(expected, menu_box)
            options = self.domain_options(image, menu_box)
            match = next((t for t in options if self.domain_value(t.text) == domain), None)
            if self.read() != expected or not self.domain_menu_open():
                raise ValueError('邮箱域名列表已关闭或页面已变化，未输入。')
            fresh = self.image
            if self.pixel_delta(fresh, image, menu_box) > 2:
                continue  # Inertia/toast disappearance: restabilize without another gesture or send.
            if match:
                # Recheck the exact label and its visible hit area in the fresh frame.
                candidates = [t for t in self.domain_options(fresh, menu_box)
                              if self.domain_value(t.text) == domain
                              and max(abs(a-b) for a, b in zip(t.box, match.box)) <= 4]
                if len(candidates) != 1:
                    raise ValueError('目标邮箱域名位置无法确认，未点击。')
                match = candidates[0]
                with self.service.mutex:
                    self.service.check()
                    point = self.policy.point(match.center, SimpleNamespace(image=fresh, texts=[match]))
                    self.device.tap(*point)
                self.service.wait(self.policy.interval())
                self.until(lambda page: page == expected and not self.domain_menu_open()
                           and self.domain_value(field_text(self.fields, self.image, 'domain', box)) == domain, 5)
                return email.split('@')[0]
            if previous is not None and self.pixel_delta(image, previous, menu_box) <= 2:
                if direction == 'down':
                    raise ValueError('游戏域名列表不含目标邮箱域名，未发送邮件。')
                direction = 'down'
            previous = image
            with self.service.mutex:
                self.service.check()
                start, end = ((902, 610), (902, 453)) if direction == 'up' else ((902, 453), (902, 610))
                self.device.swipe(start, end, .3)
            self.service.wait(self.policy.interval())
        raise ValueError('未找到目标邮箱域名，未发送邮件。')

    @staticmethod
    def domain_value(text):
        # Single-line OCR sometimes joins the native vertical field separator.
        return re.sub(r'\s+', '', text).lower().lstrip('|｜').lstrip('@')

    @staticmethod
    def pixel_delta(first, second, box):
        return np.abs(np.asarray(first.crop(box), dtype=float)
                      - np.asarray(second.crop(box), dtype=float)).mean()

    def domain_menu_open(self):
        from ournotes.rebind.vision import matches
        return matches(self.image, 'domain-menu-border')

    def domain_options(self, image, box):
        texts = self.fields.read(Frame(image, 0), 'domain-options', [box],
                                 self.task['id'], scale=2, single_line=False)
        # Clipped rows and ambiguous fragments are never click targets.
        return [t for t in texts if re.fullmatch(r'[a-z0-9]+(?:\.[a-z0-9]+)+', self.domain_value(t.text))
                and t.box[0] >= box[0] and t.box[2] <= box[2]
                and t.box[1] >= box[1] + 4 and t.box[3] <= box[3] - 4]

    def stable_domain_menu(self, expected, box):
        # Pixel stability is independent of OCR box jitter and the number of rows.
        deadline, stable_at = time.monotonic() + 5, time.monotonic()
        previous = None
        while time.monotonic() < deadline:
            page = self.read()
            if page in ('loading', 'unknown'):
                previous, stable_at = None, time.monotonic()
            elif page != expected:
                raise ValueError('邮箱列表等待期间页面已变化，未输入。')
            elif self.domain_menu_open():
                if previous is None or self.pixel_delta(self.image, previous, box) > .5:
                    previous, stable_at = self.image, time.monotonic()
                elif time.monotonic() - stable_at >= .3:
                    return self.image
            else:
                previous, stable_at = None, time.monotonic()
            self.service.wait(.15)
        raise ValueError('邮箱域名列表未稳定，未点击或发送邮件。')

    def uid(self):
        text = field_text(self.fields, self.image, 'uid', (594, 335, 1005, 371))
        values = re.findall(r'\d{12,24}', text)
        if len(values) != 1:
            raise ValueError('用户中心 UID 无法确定，未继续换绑。')
        return values[0]

    def send_mail(self, stage, message, expected, x, y, *, ticket=None, revision=0):
        """Every explicit mail input gets a separate durable, guarded submission."""
        def perform(commit):
            if ticket is not None:
                self.arm(ticket)
            self.service.stage(stage, message)
            self.tap(expected, x, y, before_input=commit)
            return True
        request = 'rebind-' + self.task['id'] + '-' + stage
        if revision:
            request += '-' + str(revision)
        self.proxy_permit.send(request, stage, perform)

    def target_confirmation(self):
        revision = 0
        while True:
            self.last_step = '提交目标邮箱并等待验证页面'
            self.send_mail('b_send_intent', '发送目标邮箱验证邮件…', 'bind_form', 800, 628, revision=revision)
            try:
                self.until(lambda page: page == 'confirm_b')
            except ValueError:
                # Any failed target-send outcome uses the explicit replacement flow.
                # Cancellation and failures before submission still propagate.
                self.service.check()
            else:
                return
            folder = self.service.root / 'results/maintenance/diagnostics/email-rebind' / self.task['id']
            folder.mkdir(parents=True, exist_ok=True)
            self.image.save(folder / f'target-send-failed-{revision}.png')
            previous = self.task['email'].split('@')[0]
            email, revision = self.service.wait_target_email()
            self.task['email'] = email
            self.until(lambda page: page == 'bind_form', 10)
            local = self.domain('bind_form', email, binding=True)
            self.replace_target(previous, local)
            self.proxy_permit.reserve_rebind_mail()

    def replace_target(self, previous, local):
        # SDK text insertion appends to existing text. Clear and verify the field first.
        self.last_step = '清空旧目标邮箱并填写新邮箱'
        self.tap('bind_form', 820, 386)
        self.until(lambda page: page == 'bind_form' and input_focused(self.image, 0, True), 4)
        self.guarded_key(107)  # Linux KEY_END: move beyond the horizontally clipped local part.
        for _ in range(len(previous) + 2):
            self.guarded_key(14)  # Linux KEY_BACKSPACE.
        self.until(lambda page: page == 'bind_form' and self.target_empty(), 5)
        self.text('bind_form', 0, local, binding=True)

    def guarded_key(self, code):
        if self.read() != 'bind_form' or not input_focused(self.image, 0, True):
            raise ValueError('修改邮箱前页面或焦点已变化，未继续输入或发信。')
        with self.service.mutex:
            self.service.check()
            self.device.key(code)
        self.service.wait(self.policy.interval())

    def target_empty(self):
        text = field_text(self.fields, self.image, 'empty-target', (610, 365, 825, 405))
        return text.strip() == '请输入邮箱地址'

    def run(self):
        s = self.service
        from ournotes.proxy.rotation import TaskProxyGate
        gate = TaskProxyGate(s.root)
        while self.proxy_permit is None:
            s.check()
            self.proxy_permit = gate.begin('rebind-' + self.task['id'], 3, 'rebind')
            if self.proxy_permit is None:
                s.stage('waiting_proxy', '代理节点达到额度，等待当前任务收尾后切换…')
                s.wait(.25)
        s.stage('starting', '启动指定实例…')
        ensure_game(self.task['instance'], s.stop_event)
        s.check()
        self.device = MuMuSdk(instance=self.task['instance'])
        s.stage('login', '使用原邮箱登录…')
        self.login_form()
        alias = self.item['result']['email']
        local = self.domain('email_form', alias)
        self.text('email_form', 0, local)
        ticket = self.prepare('login')
        # Fresh form's agreement box is initially empty; assert before toggling.
        from ournotes.rebind.vision import agreed
        if not agreed(self.image):
            self.tap('email_form', 609, 516)
            self.until(lambda page: page == 'email_form' and agreed(self.image), 4)
        self.send_mail('login_send_intent', '发送原邮箱登录验证码…', 'email_form', 932, 454, ticket=ticket)
        code = self.mail(ticket, 'ready')['code']
        self.text('email_form', 1, code)
        code = None
        s.stage('login_submit_intent', '登录请求已提交，等待游戏确认…')
        self.change('email_form', 800, 633, {'title'})
        self.cancel_ticket(ticket)
        s.stage('user_center', '登录已完成，正在进入用户中心…')
        self.change('title', 1541, 58, {'title_menu'})
        self.change('title_menu', 650, 374, {'user_center'})
        uid = self.uid()
        self.change('user_center', 800, 474, {'bindings'})
        self.change('bindings', 938, 378, {'verify_a'})
        ticket = self.prepare('identity')
        self.send_mail('a_send_intent', '验证原邮箱，认证链接由服务器处理…', 'verify_a', 800, 555, ticket=ticket)
        self.mail(ticket, 'done')
        self.change('verify_a', 800, 628, {'bind_form'})
        self.cancel_ticket(ticket)
        local = self.domain('bind_form', self.task['email'], binding=True)
        self.text('bind_form', 0, local, binding=True)
        self.text('bind_form', 1, self.password, binding=True)
        self.target_confirmation()
        message = '请在目标邮箱打开认证链接，再点击“邮件已确认”。'
        while True:
            s.wait_confirmation(message)
            # One explicit user confirmation authorizes exactly one Next click.
            s.outcome(self.task['index'], self.item['result']['account_id'], 'binding_uncertain')
            self.tap('confirm_b', 800, 605)
            outcome = self.until(lambda page: page in ('unconfirmed', 'bindings'), 15)
            if outcome == 'unconfirmed':
                s.outcome(self.task['index'], self.item['result']['account_id'])
                self.until(lambda page: page == 'confirm_b', 5)
                message = '邮箱链接尚未确认，请打开邮件中的链接后再次点击“邮件已确认”。'
                continue
            self.change('bindings', 615, 262, {'user_center'})
            if self.uid() != uid:
                raise ValueError('换绑后 UID 与原账号不同，停止核查。')
            return

    def close(self):
        self.password = None
        try:
            if self.device:
                self.device.close()
            for ticket in list(self.tickets):
                try:
                    self.cancel_ticket(ticket)
                except (CloudError, OSError, ValueError):
                    logging.getLogger(__name__).warning('Rebind ticket cleanup deferred to server expiry')
        finally:
            if self.proxy_permit is not None:
                self.proxy_permit.close()
                self.proxy_permit = None
