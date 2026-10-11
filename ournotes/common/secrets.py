from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from ournotes.platforms.locking import busy, try_lock, unlock
from ournotes.platforms.system import app_data_dir


def data_dir() -> Path:
    root = app_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


class SecretStore:
    """Current-user DPAPI or login Keychain; file leases are shared on both OSes."""

    def __init__(self, root: Path | None = None):
        self.root = root or data_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_name(name):
        if not name or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in name):
            raise ValueError('凭据或资源锁名称无效。')

    def keychain(self):
        from ournotes.platforms.keychain import Keychain
        identity = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:24]
        return Keychain('OurNotesAssistant:' + identity)

    def load(self, name: str) -> dict | None:
        self.validate_name(name)
        if sys.platform == 'darwin':
            raw = self.keychain().load(name)
            return json.loads(raw.decode('utf-8')) if raw is not None else None
        import win32crypt

        path = self.root / f"{name}.dpapi"
        if not path.exists():
            return None
        _, raw = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 1)
        return json.loads(raw.decode("utf-8"))

    def save(self, name: str, value: dict):
        self.validate_name(name)
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if sys.platform == 'darwin':
            self.keychain().save(name, raw)
            return
        import win32crypt
        encrypted = win32crypt.CryptProtectData(raw, "OurNotes Assistant", None, None, None, 1)
        fd, temporary = tempfile.mkstemp(dir=self.root, suffix=".dpapi")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / f"{name}.dpapi")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def delete(self, name: str):
        self.validate_name(name)
        if sys.platform == 'darwin':
            self.keychain().delete(name)
            return
        (self.root / f'{name}.dpapi').unlink(missing_ok=True)

    @contextmanager
    def lock(self, name: str, *, wait=False):
        """Fail fast if another process owns this mailbox/auth operation."""
        self.validate_name(name)

        with (self.root / f"{name}.lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            deadline = time.monotonic() + 30 if wait else 0
            while True:
                try:
                    try_lock(stream)
                    break
                except OSError as error:
                    if not busy(error):
                        raise
                    if not wait or time.monotonic() >= deadline:
                        raise RuntimeError(f"资源锁 {name} 正被占用，等待超时。") from None
                    # CRT blocking locking can retain the GIL while another lane
                    # needs it to release this lock. Yield between nonblocking attempts.
                    time.sleep(.05)
            try:
                yield
            finally:
                unlock(stream)
