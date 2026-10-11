"""MuMu's documented macOS CLI plus ADB; inputs are never automatically replayed.

The vendor's public manual documents commands but not the info JSON schema.
Unknown state stays unknown. A local config may name verified boolean fields and
actual per-instance ADB ports; no port is calculated from the instance number.
"""
from __future__ import annotations

import io
import json
import math
import re
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image

from ournotes.paths import DATA_ROOT
from ournotes.platforms.system import default_mumu_root

PACKAGE = 'com.bilibili.sirius.official'


class MacMuMu:
    def __init__(self, root=None, config=None):
        self.root = Path(root or default_mumu_root())
        if config is None:
            path = DATA_ROOT / 'config/mumu.json'
            config = json.loads(path.read_text('utf-8')) if path.is_file() else {}
        self.config = config
        self.tool = self.binary('mumutool')
        self.adb = self.binary('adb')

    def binary(self, name):
        selected = self.config.get(name)
        if selected:
            path = Path(selected).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise ValueError(f'macOS {name} 必须指向实际存在的绝对工具路径。')
            return path
        candidates = list(self.root.rglob(name)) if self.root.is_dir() else []
        candidates = [path for path in candidates if path.is_file()]
        if len(candidates) != 1:
            raise ValueError(f'未唯一定位 MuMu 官方 {name}，请在 config/mumu.json 中配置实际路径。')
        return candidates[0]

    def command(self, *args, timeout=20):
        return subprocess.run([str(self.tool), *map(str, args)], capture_output=True,
                              check=True, timeout=timeout).stdout

    def serial(self, instance):
        if type(instance) is not int or instance < 0:
            raise ValueError('MuMu实例编号无效。')
        port = self.config.get('instances', {}).get(str(instance), {}).get('adb_port')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f'未配置MuMu实例 {instance} 的实际ADB端口；不会按编号猜测。')
        return f'127.0.0.1:{port}'

    def adb_command(self, instance, *args):
        try:
            return subprocess.run([str(self.adb), '-s', self.serial(instance), *map(str, args)],
                                  capture_output=True, check=True, timeout=20).stdout
        except subprocess.SubprocessError as error:
            # CalledProcessError includes argv, which may contain an email or OTP.
            raise RuntimeError(f'MuMu ADB命令失败（{type(error).__name__}）；未重发输入。') from None

    def connect(self, instance):
        # Connection setup is not a game input; still bind only the configured local serial.
        serial = self.serial(instance)
        subprocess.run([str(self.adb), 'connect', serial], capture_output=True, check=True, timeout=20)
        if self.adb_command(instance, 'get-state').strip() != b'device':
            raise RuntimeError('MuMu ADB未确认连接；未发送游戏输入。')

    def info(self, instance):
        raw = json.loads(self.command('info', instance))
        if not isinstance(raw, dict):
            raise TypeError('MuMu macOS实例状态格式尚未验证；未猜测设备状态。')
        raw = raw.get(str(instance), raw)
        if not isinstance(raw, dict):
            raise TypeError('MuMu macOS实例状态归属不明确。')
        fields = self.config.get('info_fields', {})
        process = raw.get(fields.get('process_started', 'is_process_started'))
        android = raw.get(fields.get('android_started', 'is_android_started'))
        process = process if type(process) is bool else None
        android = android if type(android) is bool else None
        # A connected booted Android is direct evidence of readiness. Failed ADB
        # queries are not evidence of shutdown and cannot release restart protection.
        if android is not False:
            try:
                self.connect(instance)
                if self.adb_command(instance, 'shell', 'getprop', 'sys.boot_completed').strip() == b'1':
                    process = android = True
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
                pass
        return {'index': instance, 'is_process_started': process, 'is_android_started': android,
                'error_code': 0 if process is not None and android is not None else 1,
                'platform': 'macos-arm64'}

    def foreground(self, instance):
        raw = self.adb_command(instance, 'shell', 'dumpsys', 'activity', 'activities').decode('utf-8', 'replace')
        found = re.findall(r'(?:mResumedActivity|topResumedActivity)\s*[:=].*?\s([\w.]+)/', raw)
        unique = set(found)
        if len(unique) != 1:
            raise RuntimeError('ADB前景应用信息不明确；未猜测游戏是否在前台。')
        return unique.pop()

    def app_control(self, instance, action, package=PACKAGE, path=None, timeout=20):
        args = ['control', str(instance), '--action', action]
        args += ['--path', str(path)] if path is not None else ['--package', package]
        self.command(*args, timeout=timeout)


def instance_info(root, instance):
    return MacMuMu(root).info(instance)


def ensure_game(instance, stop, root, timeout):
    adapter = MacMuMu(root)
    info = adapter.info(instance)
    if info.get('is_android_started') is not True:
        if info.get('is_process_started') is not False or info.get('is_android_started') is not False:
            raise RuntimeError('MuMu macOS实例状态未知；未启动或恢复实例。')
        if stop.is_set():
            raise RuntimeError('启动期间暂停。')
        adapter.command('open', instance)
    deadline = time.monotonic() + timeout
    while info.get('is_android_started') is not True:
        if stop.is_set() or time.monotonic() >= deadline:
            raise RuntimeError('MuMu macOS启动未确认；未重发启动命令。')
        stop.wait(.5)
        info = adapter.info(instance)
    if stop.is_set():
        raise RuntimeError('启动期间暂停。')
    if not adapter.adb_command(instance, 'shell', 'pm', 'path', PACKAGE).strip().startswith(b'package:'):
        raise RuntimeError(f'MuMu {instance} 未安装 Our Notes。')
    if adapter.foreground(instance) != PACKAGE:
        adapter.app_control(instance, 'open_app')


def restart_game(instance, stop, root, timeout):
    adapter = MacMuMu(root)
    if adapter.info(instance).get('is_android_started') is not True:
        raise RuntimeError('模拟器未确认运行；游戏重启不会启动实例。')
    if stop.is_set():
        raise RuntimeError('游戏重启期间暂停。')
    adapter.app_control(instance, 'close_app')
    deadline = time.monotonic() + timeout
    for expected, action in ((False, 'open_app'), (True, None)):
        while (adapter.foreground(instance) == PACKAGE) is not expected:
            if stop.is_set() or time.monotonic() >= deadline:
                raise RuntimeError('MuMu macOS游戏重启未确认；未重发关闭或启动命令。')
            stop.wait(.5)
        if action:
            adapter.app_control(instance, action)
    return {'instance': instance, 'status': 'game_restarted'}


def shutdown_instances(instances, root, timeout):
    adapter = MacMuMu(root)
    for instance in instances:
        info = adapter.info(instance)
        if info.get('is_process_started') is None or info.get('is_android_started') is None:
            raise RuntimeError('MuMu macOS实例状态未知；未发送关机命令。')
    for instance in instances:
        adapter.command('close', instance)
    start, stopped_since = time.monotonic(), None
    poll = threading.Event()
    while True:
        info = [adapter.info(instance) for instance in instances]
        now = time.monotonic()
        if all(item['is_process_started'] is False and item['is_android_started'] is False for item in info):
            stopped_since = now if stopped_since is None else stopped_since
            if now - stopped_since >= 3:
                return {'status': 'closed', 'instances': instances, 'seconds': now - start}
        else:
            stopped_since = None
        if now - start >= timeout:
            raise RuntimeError('MuMu macOS关机未能持续确认；保留重启核查保护。')
        poll.wait(.5)


class MacAdbDevice:
    def __init__(self, root, instance, package='default'):
        self.adapter, self.instance = MacMuMu(root), instance
        self.package = PACKAGE if package == 'default' else package
        self.lock, self.closed = threading.RLock(), False
        self.adapter.connect(instance)
        if self.adapter.adb_command(instance, 'shell', 'getprop', 'sys.boot_completed').strip() != b'1':
            raise RuntimeError('MuMu Android尚未完成启动。')

    def capture(self):
        from ournotes.integrations.mumu import Frame
        with self.lock:
            start = time.perf_counter()
            self.require_foreground()
            raw = self.adapter.adb_command(self.instance, 'exec-out', 'screencap', '-p')
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > 16_777_216:
                    raise RuntimeError('ADB截图尺寸无效。')
                image = source.convert('RGB')
            return Frame(image, time.time(), (time.perf_counter() - start) * 1000, self.instance, 0)

    def require_foreground(self):
        if self.closed or self.adapter.foreground(self.instance) != self.package:
            raise RuntimeError('MuMu游戏前景未确认或连接已关闭；未发送输入。')

    def swipe(self, start, end, duration=.3):
        if not math.isfinite(duration) or not .01 <= duration <= 10:
            raise ValueError('触摸持续时间必须在0.01–10秒内。')
        with self.lock:
            self.require_foreground()
            image = self.capture().image
            for x, y in (start, end):
                if type(x) is not int or type(y) is not int or not 0 <= x < image.width or not 0 <= y < image.height:
                    raise ValueError('输入坐标超出当前画面范围。')
            self.adapter.adb_command(self.instance, 'shell', 'input', 'swipe', *start, *end, round(duration * 1000))

    def tap(self, x, y, duration=.06):
        self.swipe((x, y), (x, y), duration)

    def input_text(self, text):
        # Current login/rebind input consists of dot aliases, ASCII email and OTP.
        # Reject shell metacharacters rather than trying to quote a remote shell.
        if not isinstance(text, str) or not re.fullmatch(r'[A-Za-z0-9@._+\-]{1,4096}', text):
            raise ValueError('ADB文本输入仅支持已核验的ASCII邮箱与数字字段。')
        with self.lock:
            self.require_foreground()
            self.adapter.adb_command(self.instance, 'shell', 'input', 'text', text)

    def key(self, linux_code):
        codes = {14: 67, 15: 61, 28: 66, 102: 3, 107: 123, 111: 112, 158: 4}
        if linux_code not in codes:
            raise ValueError('该Linux按键尚未映射为Android键码。')
        with self.lock:
            self.require_foreground()
            self.adapter.adb_command(self.instance, 'shell', 'input', 'keyevent', codes[linux_code])

    def release_touch(self):
        # ADB input swipe releases in the Android command itself; no new input is needed.
        if self.closed:
            raise RuntimeError('ADB设备连接已关闭。')

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
