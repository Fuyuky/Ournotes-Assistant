from __future__ import annotations

import ctypes as C
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image

from ournotes.platforms.system import current_platform, default_mumu_root

DEFAULT_ROOT = default_mumu_root()
GAME_PACKAGE = 'com.bilibili.sirius.official'


def _control(instance, *args, root=DEFAULT_ROOT, timeout=40):
    result = subprocess.run([str(root / 'nx_main/mumu-cli.exe'), 'control', '-v', str(instance), *args],
                            capture_output=True, check=True, timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    value = json.loads(result.stdout)
    if any(isinstance(item, dict) and item.get('errcode', 0) for item in value.values()):
        raise RuntimeError(f'MuMu实例 {instance} 的命令未成功；未自动重发。')
    return value


def launch_instance(instance, root=DEFAULT_ROOT):
    if type(instance) is not int or instance < 0:
        raise ValueError('MuMu实例编号无效。')
    if sys.platform == 'darwin':
        from ournotes.platforms.mumu_macos import MacMuMu
        current_platform()
        MacMuMu(root).command('open', instance)
        return
    _control(instance, 'launch', root=root)


def app_info(instance, package=GAME_PACKAGE, root=DEFAULT_ROOT):
    if not re.fullmatch(r'[A-Za-z0-9_.]+', package):
        raise ValueError('应用包名无效。')
    if sys.platform == 'darwin':
        from ournotes.platforms.mumu_macos import MacMuMu
        adapter = MacMuMu(root)
        adapter.connect(instance)
        raw = adapter.adb_command(instance, 'shell', 'dumpsys', 'package', package).decode('utf-8', 'replace')
        code = re.findall(r'\bversionCode=(\d+)\b', raw)
        name = re.findall(r'\bversionName=([^\s]+)', raw)
        if not code and not name:
            return None
        if len(set(code)) != 1 or len(set(name)) != 1:
            raise RuntimeError('ADB安装版本信息不明确，未猜测覆盖更新结果。')
        return {'version': name[0], 'version_code': int(code[0])}
    return _control(instance, 'app', 'info', '--installed', root=root).get(package)


def close_app(instance, package=GAME_PACKAGE, root=DEFAULT_ROOT):
    if not re.fullmatch(r'[A-Za-z0-9_.]+', package):
        raise ValueError('应用包名无效。')
    if sys.platform == 'darwin':
        from ournotes.platforms.mumu_macos import MacMuMu
        return MacMuMu(root).app_control(instance, 'close_app', package)
    return _control(instance, 'app', 'close', '-pkg', package, root=root)


def install_apk(instance, path, root=DEFAULT_ROOT):
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() != '.apk':
        raise ValueError('APK路径无效。')
    if sys.platform == 'darwin':
        from ournotes.platforms.mumu_macos import MacMuMu
        return MacMuMu(root).app_control(instance, 'install_apk', path=path, timeout=240)
    return _control(instance, 'app', 'install', '-apk', str(path), root=root, timeout=240)


@dataclass(frozen=True)
class Frame:
    image: Image.Image
    captured_at: float
    elapsed_ms: float
    instance: int
    display: int


class Device(Protocol):
    def capture(self) -> Frame: ...
    def tap(self, x: int, y: int, duration: float = 0.06): ...
    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.3): ...
    def input_text(self, text: str): ...
    def key(self, linux_code: int): ...
    def close(self): ...


def instance_info(root: Path = DEFAULT_ROOT, instance: int = 0) -> dict:
    if sys.platform == 'darwin':
        from ournotes.platforms.mumu_macos import instance_info as mac_info
        return mac_info(root, instance)
    result = subprocess.run(
        [str(root / "nx_main/mumu-cli.exe"), "info", "-v", str(instance)],
        capture_output=True, check=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    info = json.loads(result.stdout)
    return info.get(str(instance), info)


def ensure_game(instance, stop, root=DEFAULT_ROOT, timeout=120):
    """CLI starts the emulator/app; readiness is polled, never assumed after a delay."""
    if sys.platform == 'darwin':
        from ournotes.platforms.mumu_macos import ensure_game as mac_ensure
        return mac_ensure(instance, stop, root, timeout)
    cli = str(root / "nx_main/mumu-cli.exe")
    def command(*args):
        result = subprocess.run([cli, "control", "-v", str(instance), *args], capture_output=True,
                                check=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(result.stdout)
        for value in data.values():
            if isinstance(value, dict) and value.get("errcode", 0):
                raise RuntimeError(f"MuMu {instance} 启动失败：{value.get('errmsg', '')}")
        return data
    info = instance_info(root, instance)
    if not info.get("is_android_started") and not info.get("is_process_started"):
        for attempt in range(3):
            try:
                command("launch")
                break
            except subprocess.CalledProcessError:
                # After shutdown, the CLI can briefly reject launch while RPC exits.
                # A fresh state read decides whether launch already took effect.
                info = instance_info(root, instance)
                if info.get('is_process_started') or info.get('is_android_started'):
                    break
                if (attempt == 2 or info.get('error_code', 0) != 0
                        or info.get('is_process_started') is not False
                        or info.get('is_android_started') is not False):
                    raise
                if stop.wait(.5 * (attempt + 1)):
                    raise RuntimeError('启动期间暂停。') from None
    deadline = time.monotonic() + timeout
    while not info.get("is_android_started"):
        if stop.is_set():
            raise RuntimeError("启动期间暂停。")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"MuMu {instance} 启动超时。")
        stop.wait(.5)  # Bound CLI polling rate; the returned Android state decides readiness.
        info = instance_info(root, instance)
    if stop.is_set():
        raise RuntimeError("启动期间暂停。")
    apps = command("app", "info", "--installed")
    package = "com.bilibili.sirius.official"
    if package not in apps:
        raise RuntimeError(f"MuMu {instance} 未安装 Our Notes。")
    if apps.get("active") != package:
        command("app", "launch", "-pkg", package)


def restart_game(instance, stop, root=DEFAULT_ROOT, timeout=45):
    """Restart only Our Notes inside an already running Android instance."""
    if sys.platform == 'darwin':
        from ournotes.platforms.mumu_macos import restart_game as mac_restart
        return mac_restart(instance, stop, root, timeout)
    package = "com.bilibili.sirius.official"
    cli = str(root / "nx_main/mumu-cli.exe")

    def command(*args):
        if stop.is_set():
            raise RuntimeError("游戏重启期间暂停。")
        result = subprocess.run([cli, "control", "-v", str(instance), "app", *args],
                                capture_output=True, check=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(result.stdout)
        if any(isinstance(v, dict) and v.get("errcode", 0) for v in data.values()):
            raise RuntimeError(f"MuMu {instance} 游戏重启命令失败。")
        return data

    if not instance_info(root, instance).get("is_android_started"):
        raise RuntimeError("模拟器未运行；游戏重启不会启动或重启实例。")
    command("close", "-pkg", package)
    deadline = time.monotonic() + timeout
    # Wait for the old foreground to leave before launching; no blind delay.
    while command("info", "--installed").get("active") == package:
        if time.monotonic() >= deadline:
            raise RuntimeError("游戏关闭超时；未重发关闭或启动命令。")
        stop.wait(.5)
    command("launch", "-pkg", package)
    while command("info", "--installed").get("active") != package:
        if time.monotonic() >= deadline:
            raise RuntimeError("游戏启动超时；保留原账号等待检查。")
        stop.wait(.5)
    if not instance_info(root, instance).get("is_android_started"):
        raise RuntimeError("游戏重启期间实例退出；未启动实例。")
    return {"instance": instance, "status": "game_restarted"}


def shutdown_instances(instances, root=DEFAULT_ROOT, timeout=45):
    """Close only devices controlled by this run, while it still holds their leases."""
    instances = list(instances)
    if any(type(instance) is not int or instance < 0 for instance in instances):
        raise ValueError('关机必须指定有效实例编号。')
    instances = sorted(set(instances))
    if not instances:
        return {'status': 'kept_open', 'reason': 'no_device_started', 'instances': []}
    if sys.platform == 'darwin':
        from ournotes.platforms.mumu_macos import shutdown_instances as mac_shutdown
        return mac_shutdown(instances, root, timeout)
    start = time.monotonic()
    cli = str(root / "nx_main/mumu-cli.exe")
    def invoke(*args):
        r = subprocess.run([cli, *args], capture_output=True, check=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(r.stdout)
    for instance in instances:
        try:
            reply = invoke("control", "-v", str(instance), "shutdown")
        except subprocess.CalledProcessError:
            # MuMu can close its RPC process before returning the shutdown reply.
            # Verify the actual state; never replay the shutdown command.
            info = instance_info(root, instance)
            if (info.get('error_code', 0) == 0 and info.get('is_process_started') is False
                    and info.get('is_android_started') is False):
                continue
            raise
        if any(isinstance(v, dict) and v.get("errcode", 0) for v in reply.values()):
            raise RuntimeError(f'MuMu 实例 {instance} 返回关机失败，请查看运行日志。')
    poll = threading.Event()
    stopped_since = None
    while True:
        info = [instance_info(root, instance) for instance in instances]
        now = time.monotonic()
        closed = all(v.get('error_code', 0) == 0
                     and v.get('is_process_started') is False
                     and v.get('is_android_started') is False for v in info)
        if closed:
            if stopped_since is None:
                stopped_since = now
            # The first stopped snapshot can precede completion of RPC teardown.
            # Require sustained confirmation before returning to the launch path.
            if now - stopped_since >= 3:
                return {"status": "closed", "instances": instances, "seconds": now - start}
        else:
            stopped_since = None
        if now - start >= timeout:
            raise RuntimeError("MuMu 关机超时，未能持续确认实例已关闭。")
        poll.wait(.5)  # Poll shutdown state; never assume success after a fixed delay.


def _check(result: int, operation: str):
    if result != 0:
        raise RuntimeError(f"MuMu SDK {operation} 失败（{result}）；请重新观察画面后再决定操作。")


class MuMuSdk:
    """One connection per instance, serialized screen and input operations.

    Coordinates use the upright captured image, origin at top-left. A failed input
    is never automatically retried. Native calls have no SDK timeout; a future
    scheduler should isolate this adapter in a per-instance worker process.
    """

    def __new__(cls, root=DEFAULT_ROOT, instance=0, package='default'):
        if cls is MuMuSdk and sys.platform == 'darwin':
            current_platform()  # Reject Intel Mac rather than silently using Rosetta.
            from ournotes.platforms.mumu_macos import MacAdbDevice
            return MacAdbDevice(root, instance, package)
        return super().__new__(cls)

    def __init__(self, root: Path = DEFAULT_ROOT, instance: int = 0, package: str = "default"):
        if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("MuMu SDK 需要 Windows 和 64 位 Python。")
        if not instance_info(root, instance).get("is_android_started"):
            raise RuntimeError(f"MuMu 实例 {instance} 的 Android 尚未启动。")
        self.instance = instance
        self.package = package
        self._lock = threading.RLock()
        self._buffer = None
        self._handle = 0
        self._dll = C.CDLL(str(root / "nx_main/sdk/external_renderer_ipc.dll"))
        signatures = {
            "nemu_connect": ([C.c_wchar_p, C.c_int], C.c_int),
            "nemu_disconnect": ([C.c_int], None),
            "nemu_get_display_id": ([C.c_int, C.c_char_p, C.c_int], C.c_int),
            "nemu_capture_display": ([C.c_int, C.c_uint, C.c_int, C.POINTER(C.c_int),
                                      C.POINTER(C.c_int), C.POINTER(C.c_ubyte)], C.c_int),
            "nemu_input_text": ([C.c_int, C.c_int, C.c_char_p], C.c_int),
            "nemu_input_event_finger_touch_down": ([C.c_int] * 5, C.c_int),
            "nemu_input_event_finger_touch_up": ([C.c_int] * 3, C.c_int),
            "nemu_input_event_key_down": ([C.c_int] * 3, C.c_int),
            "nemu_input_event_key_up": ([C.c_int] * 3, C.c_int),
        }
        for name, (arguments, result) in signatures.items():
            fn = getattr(self._dll, name)
            fn.argtypes, fn.restype = arguments, result
        self._handle = self._dll.nemu_connect(str(root), instance)
        if self._handle <= 0:
            raise RuntimeError("MuMu SDK 连接失败。")

    def _display(self) -> int:
        if not self._handle:
            raise RuntimeError("MuMu SDK 连接已关闭。")
        display = self._dll.nemu_get_display_id(self._handle, self.package.encode("utf-8"), 0)
        if display < 0:
            raise RuntimeError("无法定位 MuMu 显示画面，请检查游戏是否已启动。")
        return display

    def _dimensions(self, display: int) -> tuple[int, int]:
        w, h = C.c_int(), C.c_int()
        _check(self._dll.nemu_capture_display(self._handle, display, 0, C.byref(w), C.byref(h), None),
               "读取尺寸")
        if not (0 < w.value <= 8192 and 0 < h.value <= 8192 and w.value * h.value <= 16_777_216):
            raise RuntimeError("MuMu 返回了无效的截图尺寸。")
        return w.value, h.value

    def capture(self) -> Frame:
        with self._lock:
            start = time.perf_counter()
            display = self._display()
            for _ in range(2):  # Read-only retry for a concurrent resolution change.
                width, height = self._dimensions(display)
                count = width * height * 4
                if self._buffer is None or len(self._buffer) != count:
                    self._buffer = (C.c_ubyte * count)()
                w, h = C.c_int(width), C.c_int(height)
                result = self._dll.nemu_capture_display(
                    self._handle, display, count, C.byref(w), C.byref(h), self._buffer)
                if result == 0 and (w.value, h.value) == (width, height):
                    pixels = bytes(self._buffer)
                    img = Image.frombytes("RGBA", (width, height), pixels)
                    img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM).convert("RGB")
                    return Frame(img, time.time(), (time.perf_counter() - start) * 1000,
                                 self.instance, display)
            raise RuntimeError("截图失败或分辨率变化，请重新连接 MuMu。")

    def _point(self, point: tuple[int, int], size: tuple[int, int]):
        x, y = point
        if not (isinstance(x, int) and isinstance(y, int) and 0 <= x < size[0] and 0 <= y < size[1]):
            raise ValueError("输入坐标超出当前画面范围。")

    def tap(self, x: int, y: int, duration: float = 0.06):
        self.swipe((x, y), (x, y), duration)

    def release_touch(self):
        """Release a possibly stuck finger without starting another click."""
        with self._lock:
            _check(self._dll.nemu_input_event_finger_touch_up(self._handle, self._display(), 1), "补发抬起")

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.3):
        if not math.isfinite(duration) or not 0.01 <= duration <= 10:
            raise ValueError("触摸持续时间必须在 0.01–10 秒内。")
        with self._lock:
            display = self._display()
            size = self._dimensions(display)
            self._point(start, size)
            self._point(end, size)
            try:
                _check(self._dll.nemu_input_event_finger_touch_down(self._handle, display, 1, *start),
                       "按下")
                steps = 1 if start == end else max(2, math.ceil(duration / 0.016))
                t0 = time.monotonic()
                for step in range(1, steps + 1):
                    time.sleep(max(0, t0 + duration * step / steps - time.monotonic()))
                    if start != end:
                        point = tuple(round(a + (b - a) * step / steps) for a, b in zip(start, end))
                        _check(self._dll.nemu_input_event_finger_touch_down(
                            self._handle, display, 1, *point), "移动")
            finally:
                _check(self._dll.nemu_input_event_finger_touch_up(self._handle, display, 1), "抬起")

    def input_text(self, text: str):
        encoded = text.encode("utf-8")
        if not encoded or b"\0" in encoded or len(encoded) > 4096:
            raise ValueError("文本为空、过长或含有 NUL。")
        with self._lock:
            self._display()
            _check(self._dll.nemu_input_text(self._handle, len(encoded), encoded), "文本输入")

    def key(self, linux_code: int):
        if not isinstance(linux_code, int) or not 1 <= linux_code <= 767:
            raise ValueError("需要 Linux input-event-codes 键码。")
        with self._lock:
            display = self._display()
            try:
                _check(self._dll.nemu_input_event_key_down(self._handle, display, linux_code), "按键")
            finally:
                _check(self._dll.nemu_input_event_key_up(self._handle, display, linux_code), "释放按键")

    def close(self):
        with self._lock:
            if self._handle:
                self._dll.nemu_disconnect(self._handle)
                self._handle = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
