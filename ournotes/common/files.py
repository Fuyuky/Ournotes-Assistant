"""Atomic files and content fingerprints shared by runtime and maintenance."""
import gzip
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path


def atomic_json(path: Path, data: dict, *, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=None if compact else 2,
                      separators=(',', ':') if compact else None)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        replace_file(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def replace_file(source, target):
    """Windows readers may briefly deny rename. Keep the old valid JSON until replacement succeeds."""
    deadline = time.monotonic() + 1
    delay = .01
    wait = threading.Event()
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if getattr(error, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            wait.wait(min(delay, max(0, deadline-time.monotonic())))  # Filesystem retry, not a game wait.
            delay = min(.1, delay*2)


def file_hash(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json_gzip(path: Path, data: dict):
    """Publish a compact, verified gzip document without losing the previous one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as stream:
            with gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0, compresslevel=6) as archive:
                archive.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        with gzip.open(name, 'rb') as stream:
            if hashlib.file_digest(stream, 'sha256').digest() != hashlib.sha256(payload).digest():
                raise ValueError('压缩报告校验失败，保留原文件。')
        replace_file(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
