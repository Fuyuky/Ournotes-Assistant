"""SDK isolation: a blocked native call cannot block the controller forever."""
from __future__ import annotations

import multiprocessing as mp
import os
import threading
from pathlib import Path

from PIL import Image

from ournotes.integrations.mumu import DEFAULT_ROOT, MuMuSdk


class DeviceUncertain(RuntimeError):
    pass


def _worker(pipe, root, instance):
    # The vendor DLL prints input coordinates on stdout. Keep them out of the GUI.
    sink = os.open(os.devnull, os.O_WRONLY)
    os.dup2(sink, 1)
    os.close(sink)
    try:
        with MuMuSdk(Path(root), instance) as device:
            pipe.send(("ready", None))
            while True:
                name, args = pipe.recv()
                if name == "close":
                    return
                try:
                    if name == "capture":
                        frame = device.capture()
                        result = (frame.image.size, frame.image.tobytes(), frame.captured_at)
                    else:
                        result = getattr(device, name)(*args)
                    pipe.send(("ok", result))
                except Exception as e:  # noqa: BLE001 - serialize process-boundary failures
                    pipe.send(("error", f"{type(e).__name__}: {e}"))
    except BaseException as e:  # noqa: BLE001 - release native worker on every exit
        try:
            pipe.send(("error", str(e)))
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


class DeviceClient:
    def __init__(self, instance=0, root=DEFAULT_ROOT, timeout=8):
        if root is None:
            from ournotes.platforms.system import default_mumu_root
            root = default_mumu_root()
        self.timeout = timeout
        self.lock = threading.Lock()
        context = mp.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child, str(root), instance), daemon=True)
        self.process.start()
        child.close()
        if not self.pipe.poll(timeout):
            self.close()
            raise DeviceUncertain("MuMu SDK 连接超时。")
        status, payload = self.pipe.recv()
        if status != "ready":
            self.close()
            raise DeviceUncertain(payload)

    def call(self, name, *args):
        with self.lock:
            try:
                self.pipe.send((name, args))
                if not self.pipe.poll(self.timeout):
                    raise TimeoutError()
                status, value = self.pipe.recv()
            except (OSError, EOFError, TimeoutError):
                self.close()
                raise DeviceUncertain(f"SDK {name} 超时或断开；输入结果不确定，禁止自动重发。") from None
            if status != "ok":
                raise DeviceUncertain(value)
            return value

    def capture(self):
        size, pixels, captured_at = self.call("capture")
        return Image.frombytes("RGB", size, pixels), captured_at

    def tap(self, x, y):
        self.call("tap", int(x), int(y))

    def release_touch(self):
        self.call('release_touch')

    def swipe(self, start, end):
        self.call("swipe", tuple(start), tuple(end))

    def input_text(self, text):
        self.call("input_text", text)

    def key(self, code):
        self.call("key", code)

    def close(self):
        if self.process.is_alive():
            try:
                self.pipe.send(("close", ()))
            except (OSError, EOFError):
                pass
            self.process.join(.5)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(2)
        self.pipe.close()
