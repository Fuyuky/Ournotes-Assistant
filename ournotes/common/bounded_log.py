"""Bounded local diagnostics, separate from durable operation receipts."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class LogBudget:
    def __init__(self, limit):
        self.limit = limit
        self.paths = {}
        self.lock = threading.RLock()

    def write(self, path, value):
        path = Path(path)
        data = value.encode('utf-8', errors='replace') if isinstance(value, str) else value
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.paths[path] = path.stat().st_size if path.exists() else 0
            # Keep the most recent bytes across this session's writers.
            data = data[-self.limit:]
            excess = sum(self.paths.values()) + len(data) - self.limit
            for old, size in list(self.paths.items()):
                if excess <= 0:
                    break
                # Reclaim a chunk so sustained logging does not rewrite the
                # entire retained tail on every small line once at capacity.
                cut = min(size, max(excess, self.limit // 4))
                if cut:
                    if old.exists():
                        with old.open('rb') as stream:
                            stream.seek(cut)
                            tail = stream.read()
                        # Do not leave a partial JSON line or UTF-8 character.
                        tail = tail.partition(b'\n')[2] if cut < size else b''
                    else:
                        tail = b''
                    old.write_bytes(tail)
                    self.paths[old] = len(tail)
                    excess -= size - len(tail)
            with path.open('ab') as stream:
                stream.write(data)
            self.paths[path] += len(data)

    def clear(self, protected=()):
        with self.lock:
            for path in list(self.paths):
                if path not in protected:
                    path.unlink(missing_ok=True)
                    self.paths.pop(path)


class LogStream:
    encoding = 'utf-8'

    def __init__(self, budget, path):
        self.budget, self.path = budget, Path(path)

    def write(self, text):
        self.budget.write(self.path, text)
        return len(text)

    def flush(self):
        pass

    def close(self):
        pass

    def isatty(self):
        return False


_console = {}


class ConsoleLogBudget(LogBudget):
    def __init__(self, root):
        super().__init__(10 * 1024**2)
        self.root = Path(root)
        self.registry = self.root / 'state/webui/session-logs.json'

    def write(self, path, value):
        with self.lock:
            path = Path(path)
            if path not in self.paths:
                from ournotes.common.files import atomic_json
                self.paths[path] = path.stat().st_size if path.exists() else 0
                atomic_json(self.registry, [p.relative_to(self.root).as_posix() for p in self.paths])
            super().write(path, value)

    def prepare(self):
        """Called under the console lease; collapse a crashed session to one file."""
        if not self.registry.exists():
            return
        from ournotes.runtime.storage import inside
        old = []
        content = b''
        for relative in json.loads(self.registry.read_text('utf-8')):
            path = inside(self.root, self.root / relative, self.root / 'state/webui')
            if path.suffix != '.log':
                raise ValueError('Unexpected console log registry entry')
            if path.is_file():
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - self.limit))
                    content = (content + stream.read())[-self.limit:]
                old.append(path)
        previous = self.root / 'state/webui/previous-crash.log'
        previous.parent.mkdir(parents=True, exist_ok=True)
        from ournotes.common.files import atomic_json, replace_file
        # Retain every cleanup target in the journal until deletion succeeds.
        atomic_json(self.registry, [p.relative_to(self.root).as_posix() for p in dict.fromkeys([*old, previous])])
        temporary = previous.with_suffix('.log.tmp')
        try:
            with temporary.open('wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            replace_file(temporary, previous)
        finally:
            temporary.unlink(missing_ok=True)
        for path in old:
            if path != previous:
                path.unlink(missing_ok=True)
        self.paths = {previous: len(content)}
        atomic_json(self.registry, [previous.relative_to(self.root).as_posix()])

    def clear(self, protected=()):
        super().clear(protected)
        if not self.paths:
            self.registry.unlink(missing_ok=True)


def console_logs(root):
    key = str(Path(root).resolve())
    if key not in _console:
        _console[key] = ConsoleLogBudget(root)
    return _console[key]
