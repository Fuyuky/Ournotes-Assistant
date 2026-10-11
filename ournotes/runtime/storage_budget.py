"""Diagnostic size guard. Submitted work drains through the normal pause path."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

LIMIT = 512 * 1024**2
_lock = threading.Lock()
_cache = {}


def usage(root, *, refresh=False):
    root = Path(root)
    with _lock:
        stamp, size = _cache.get(str(root), (float('-inf'), 0))
        if not refresh and time.monotonic() - stamp < 2:
            return size
        size = 0
        for folder in (root / 'state/runs', root / 'results/batches'):
            for base, dirs, names in os.walk(folder, followlinks=False):
                dirs[:] = [d for d in dirs if not Path(base, d).is_symlink()]
                for name in names:
                    if folder.name == 'batches' and name not in ('performance.json', 'performance.json.gz', '完整分步骤用时.md'):
                        continue
                    try:
                        size += Path(base, name).stat().st_size
                    except FileNotFoundError:
                        pass
        _cache[str(root)] = (time.monotonic(), size)
        return size


def pause_reason(root):
    if usage(root) >= LIMIT:
        return '运行日志与异常现场达到 512 MiB 预算；暂停新增操作，保留已提交交易并收尾。请整理历史现场后再恢复'
    return None
