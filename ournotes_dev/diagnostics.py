"""Exclusive bounded diagnostic session; uncertain inputs are never repeated."""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from contextlib import ExitStack

from ournotes.common.devices import instance_leases
from ournotes.common.secrets import SecretStore
from ournotes.control.launcher import atomic_json


class Diagnostics:
    def __init__(self, service, sdk_factory=None, lock_factory=None):
        self.service = service
        self.sdk_factory = sdk_factory
        self.lock_factory = lock_factory or (lambda: SecretStore(service.root / "state").lock("runner"))
        self.lock = threading.RLock()
        self.sessions = {}

    def open(self, instance, expected_version):
        with self.lock:
            runtime = self.service.runtime()
            if self.service.running() or runtime["error"] or runtime.get("version") != expected_version:
                raise ValueError("需先暂停调度器并刷新状态，才能交接设备控制权。")
            lanes = [lane for lane in runtime["lanes"] if lane["instance"] == instance]
            if any(lane.get("mail_limit") or lane.get("recovery_hold") for lane in lanes):
                raise ValueError("目标实例处于邮箱限额或恢复核查状态，禁止诊断输入。")
            targets = {lane["run_id"] for lane in lanes}
            if any(item["run_id"] in targets for item in runtime["unresolved"]):
                raise ValueError("目标实例存在未决交易，禁止诊断输入。")
            if self.sessions:
                raise ValueError("已有独占诊断会话。")
            stack = ExitStack()
            try:
                stack.enter_context(self.lock_factory())
                stack.enter_context(instance_leases(self.service.root, [instance]))
                if self.sdk_factory is None:
                    from ournotes.integrations.mumu import MuMuSdk
                    sdk = stack.enter_context(MuMuSdk(instance=instance))
                else:
                    sdk = stack.enter_context(self.sdk_factory(instance))
                identifier = uuid.uuid4().hex
                self.sessions[identifier] = {"stack": stack, "sdk": sdk, "instance": instance, "expires": time.monotonic() + 120}
                timer = threading.Timer(120, self.close, args=(identifier,))
                timer.daemon = True
                self.sessions[identifier]["timer"] = timer
                timer.start()
                return {"session_id": identifier, "instance": instance, "expires_in": 120}
            except BaseException:
                stack.close()
                raise

    def close(self, identifier):
        with self.lock:
            session = self.sessions.pop(identifier, None)
            if session:
                session["timer"].cancel()
                session["stack"].close()
            return {"closed": True}

    def close_all(self):
        for identifier in list(self.sessions):
            self.close(identifier)

    def _session(self, identifier):
        session = self.sessions.get(identifier)
        if session is None:
            raise ValueError("诊断会话不存在。")
        if time.monotonic() >= session["expires"]:
            self.close(identifier)
            raise ValueError("诊断会话已到期。")
        return session

    def execute(self, identifier, command_id, action, coordinates, expected_hash):
        if action not in ("observe", "tap", "swipe", "key"):
            raise ValueError("不支持的诊断操作。")
        parameters = {"session_id": identifier, "coordinates": coordinates, "expected_hash": expected_hash}
        with self.lock:
            return self.service.operations.submit(command_id, "diagnostic_" + action, parameters, lambda _: self._perform(identifier, command_id, action, coordinates, expected_hash))

    def _perform(self, identifier, command_id, action, coordinates, expected_hash):
        session = self._session(identifier)
        sdk = session["sdk"]
        before = sdk.capture()
        digest = hashlib.sha256(before.image.tobytes()).hexdigest()
        if action != "observe" and expected_hash != digest:
            raise ValueError("前景画面已变化；未发送输入。")
        folder = self.service.root / "state/diagnostics" / identifier
        folder.mkdir(parents=True, exist_ok=True)
        before.image.save(folder / (command_id + "-before.png"))
        audit = {"command_id": command_id, "instance": session["instance"], "action": action, "coordinates": coordinates, "before_hash": digest, "status": "intent", "at": time.time()}
        atomic_json(folder / (command_id + ".json"), audit)
        if action == "tap":
            if len(coordinates) != 2:
                raise ValueError("tap需要两个坐标。")
            sdk.tap(*coordinates)
        elif action == "swipe":
            if len(coordinates) != 4:
                raise ValueError("swipe需要四个坐标。")
            sdk.swipe(tuple(coordinates[:2]), tuple(coordinates[2:]))
        elif action == "key":
            if len(coordinates) != 1:
                raise ValueError("key需要一个键码。")
            sdk.key(coordinates[0])
        after = sdk.capture() if action != "observe" else before
        after.image.save(folder / (command_id + "-after.png"))
        audit.update(status="captured", after_hash=hashlib.sha256(after.image.tobytes()).hexdigest())
        atomic_json(folder / (command_id + ".json"), audit)
        return {"before_hash": digest, "after_hash": audit["after_hash"], "evidence": str((folder / (command_id + ".json")).relative_to(self.service.root))}
