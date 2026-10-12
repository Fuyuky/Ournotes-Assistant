from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image

from devtools.diagnostics import Diagnostics
from ournotes.control.operations import OperationStore


class Service:
    def __init__(self, root):
        self.root = root
        self.operations = OperationStore(root)
        self.active = False
        self.unresolved = []

    def running(self):
        return self.active

    def runtime(self):
        return {"version": "v", "error": None, "lanes": [{"instance": 0, "run_id": "r"}], "unresolved": self.unresolved}


class Device:
    def __init__(self):
        self.taps = []

    def capture(self):
        return SimpleNamespace(image=Image.new("RGB", (20, 20), "black"))

    def tap(self, x, y):
        self.taps.append((x, y))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_diagnostic_handoff_foreground_and_idempotency(tmp_path):
    held = []
    @contextmanager
    def ownership():
        held.append(True)
        try:
            yield
        finally:
            held.pop()
    service, device = Service(tmp_path), Device()
    diagnostic = Diagnostics(service, lambda _: device, ownership)
    service.active = True
    with pytest.raises(ValueError):
        diagnostic.open(0, "v")
    service.active = False
    service.unresolved = [{"run_id": "r"}]
    with pytest.raises(ValueError):
        diagnostic.open(0, "v")
    service.unresolved = [{"run_id": "old"}]
    session = diagnostic.open(0, "v")["session_id"]
    try:
        assert held
        observation = diagnostic.execute(session, "observe-001", "observe", [], None)
        digest = observation["result"]["after_hash"]
        with pytest.raises(ValueError, match="画面已变化"):
            diagnostic.execute(session, "tap-bad-001", "tap", [1, 2], "wrong")
        assert not device.taps
        result = diagnostic.execute(session, "tap-good-001", "tap", [1, 2], digest)
        assert diagnostic.execute(session, "tap-good-001", "tap", [1, 2], digest) == result
        assert device.taps == [(1, 2)]
    finally:
        diagnostic.close(session)
    assert not held


@pytest.mark.parametrize("flag", ["mail_limit", "recovery_hold"])
def test_diagnostics_hold_is_scoped_to_target_instance(tmp_path, flag):
    service = Service(tmp_path)
    service.runtime = lambda: {"version": "v", "error": None, "lanes": [{"instance": 0, "run_id": "r", flag: True}, {"instance": 1, "run_id": "other"}], "unresolved": []}
    diagnostic = Diagnostics(service, lambda _: Device())
    with pytest.raises(ValueError, match="目标实例"):
        diagnostic.open(0, "v")
    session = diagnostic.open(1, "v")["session_id"]
    diagnostic.close(session)
