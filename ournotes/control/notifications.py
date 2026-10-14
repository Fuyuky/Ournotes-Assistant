"""Native desktop notifications independent of browser polling; no game actions."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path

from ournotes.common.files import atomic_json
from ournotes.platforms.system import current_platform


def native_notifier(url):
    return WindowsNotifier(url)


class WindowsNotifier:
    """All window operations run on the worker owning the Windows message queue."""
    def __init__(self, url):
        import win32api
        import win32con
        import win32gui
        self.gui, self.con, self.url = win32gui, win32con, url
        self.name = f'OurNotesNotifications-{os.getpid()}-{id(self)}'
        self.module = win32api.GetModuleHandle(None)
        self.icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        cls = win32gui.WNDCLASS()
        cls.hInstance, cls.lpszClassName, cls.lpfnWndProc = self.module, self.name, self.callback
        win32gui.RegisterClass(cls)
        self.window = win32gui.CreateWindow(self.name, 'OurNotes Assistant', 0, 0, 0, 0, 0,
                                           0, 0, self.module, None)
        self.taskbar_created = win32gui.RegisterWindowMessage('TaskbarCreated')
        self.add_icon()

    def add_icon(self):
        g = self.gui
        g.Shell_NotifyIcon(g.NIM_ADD, (self.window, 1, g.NIF_ICON | g.NIF_MESSAGE | g.NIF_TIP,
                                     self.con.WM_USER + 20, self.icon, 'OurNotes Assistant'))

    def callback(self, hwnd, message, wparam, lparam):
        if message == getattr(self, 'taskbar_created', None):
            self.add_icon()
        elif message == self.con.WM_USER + 20 and lparam in (self.con.WM_LBUTTONUP, 0x405):
            webbrowser.open(self.url)
        return self.gui.DefWindowProc(hwnd, message, wparam, lparam)

    def send(self, title, body):
        g = self.gui
        # Windows decides placement and honours its notification / quiet-time settings.
        g.Shell_NotifyIcon(g.NIM_MODIFY, (self.window, 1, g.NIF_INFO, self.con.WM_USER + 20,
                          self.icon, 'OurNotes Assistant', body[:240], 10000, title[:60], 0x81))

    def pump(self):
        self.gui.PumpWaitingMessages()

    def close(self):
        try:
            self.gui.Shell_NotifyIcon(self.gui.NIM_DELETE, (self.window, 1))
        finally:
            self.gui.DestroyWindow(self.window)
            self.gui.UnregisterClass(self.name, self.module)


def events(runtime, rebind=None):
    """Stable fingerprints; notification bodies never contain raw logs or credentials."""
    found = []
    for lane in runtime.get('lanes', []):
        if lane.get('status') not in ('paused', 'failed', 'needs_review'):
            continue
        reason = str(lane.get('recovery_hold') or lane.get('pause_reason') or lane.get('message') or '').strip()
        if not reason or (lane.get('status') == 'paused' and not lane.get('recovery_hold')
                          and reason.startswith(('用户暂停', '用户请求暂停', '用户在启动前请求暂停'))):
            continue
        key = f"lane:{lane['run_id']}:{lane.get('status')}:{hashlib.sha256(reason.encode()).hexdigest()}"
        body = (f"实例 {lane['instance']}：游戏要求更新客户端，请在控制台手动更新。"
                if '更新客户端' in reason else f"实例 {lane['instance']} 已暂停，需要人工检查。点击打开控制台查看原因。")
        found.append((key, 'OurNotes · 运行需要处理', body))
    task = (rebind or {}).get('task')
    if task and task.get('state') in ('waiting_b', 'error', 'interrupted', 'success'):
        state = task['state']
        text = {'waiting_b': '换绑已到邮件确认阶段，请查看目标邮箱，完成后回控制台确认。',
                'error': '换绑遇到异常，请打开控制台核查。',
                'interrupted': '换绑任务中断，请打开控制台核查。',
                'success': '换绑已完成。'}[state]
        found.append((f"rebind:{task['id']}:{state}", 'OurNotes · 邮箱换绑', text))
    return found


class Notifications:
    def __init__(self, root, runtime, rebind=None, factory=native_notifier):
        self.path = Path(root) / 'state/notifications.json'
        self.runtime, self.rebind, self.factory = runtime, rebind, factory
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.tests, self.thread = queue.Queue(maxsize=1), None
        self.available = sys.platform == 'win32'
        self.platform = current_platform() if self.available else 'unsupported'
        self.error, self.last_sent = '', None
        try:
            saved = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError):
            saved = {}
        self.enabled = bool(saved.get('enabled', True))
        self.seen = dict(saved.get('seen', {}))
        self.url = 'http://127.0.0.1:8766/'

    def status(self):
        with self.lock:
            return {'enabled': self.enabled, 'available': self.available, 'error': self.error,
                    'last_sent': self.last_sent, 'platform': self.platform,
                    'click_opens_console': self.platform == 'windows-x64'}

    def save(self):
        self.seen = dict(list(self.seen.items())[-128:])
        atomic_json(self.path, {'enabled': self.enabled, 'seen': self.seen})

    def configure(self, enabled):
        with self.lock:
            self.enabled = enabled
            self.save()
            return self.status()

    def test(self):
        if not self.available or not self.enabled:
            raise ValueError('请先在支持的桌面系统上开启后台通知。')
        try:
            self.tests.put_nowait(True)
        except queue.Full:
            pass
        return {'message': '已请求测试通知；若未显示，请检查系统通知权限及勿扰设置。'}

    def start(self):
        self.thread = threading.Thread(target=self.work, name='desktop-notifications', daemon=True)
        self.thread.start()

    def deliver(self, notifier):
        current = events(self.runtime(), self.rebind() if self.rebind else None)
        with self.lock:
            if not self.enabled:
                return
            active = {key for key, _, _ in current}
            # Clear resolved transitions so a later recurrence can notify again.
            self.seen = {key: stamp for key, stamp in self.seen.items() if key in active}
            pending = [event for event in current if event[0] not in self.seen]
            if pending:
                title, body = pending[0][1:]
                if len(pending) > 1:
                    body += f' 另有 {len(pending) - 1} 项待查看。'
                notifier.send(title, body)
                self.last_sent = time.time()
                self.seen.update({key: self.last_sent for key, _, _ in pending})
            if pending or self.seen != getattr(self, '_saved_seen', None):
                self.save()
                self._saved_seen = dict(self.seen)

    def work(self):
        notifier, next_poll = None, 0
        try:
            while not self.stop.wait(.2):
                try:
                    if not self.enabled or not self.available:
                        if notifier:
                            notifier.close()
                            notifier = None
                        continue
                    if notifier is None:
                        notifier = self.factory(self.url)
                    notifier.pump()
                    if not self.tests.empty():
                        self.tests.get_nowait()
                        notifier.send('OurNotes · 通知测试', '后台通知已发送。网页关闭后，控制台仍可提醒你处理任务。')
                        self.last_sent = time.time()
                    if time.monotonic() >= next_poll:
                        next_poll = time.monotonic() + 3
                        self.deliver(notifier)
                        self.error = ''
                except Exception:  # noqa: BLE001 -- notifications must not affect running tasks
                    self.error = '后台通知暂不可用，请检查系统通知设置或点击测试重试。'
                    self.stop.wait(10)
        finally:
            if notifier:
                notifier.close()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)
