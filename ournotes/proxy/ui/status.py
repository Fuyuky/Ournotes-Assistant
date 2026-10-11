"""Read the local controller and durable batch without allocating or switching."""
from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError

from ournotes.common.devices import lock_held
from ournotes.paths import DATA_ROOT
from ournotes.proxy.rotation import ProxyCoordinator
from ournotes.proxy.usage import usage_status


def _probe(pipe, root):
    try:
        controller = ProxyCoordinator(root)
        group, nodes, selected = controller.selection()
        value = {'state': 'connected', 'message': '本机控制接口已连接',
                 'transport': '命名管道' if controller.pipe else '本机 HTTP',
                 'group': group, 'node': selected, 'node_count': len(nodes), 'nodes': nodes}
    except HTTPError as error:
        value = {'state': 'unavailable', 'message': ('控制接口授权失败，请核对 Clash Verge 配置。'
                 if error.code in (401, 403) else 'Clash Verge 控制接口暂时不可用。')}
    except ValueError as error:
        # Only our controlled messages can be shown; parser errors may contain secrets.
        message = str(error)
        value = {'state': 'unavailable', 'message': message if message.startswith((
            '未找到本机 Clash Verge', 'Clash Verge 缺少', 'Clash Verge 当前',
            'Clash Verge 控制接口必须', 'Clash Verge 拒绝')) else '无法读取本机 Clash Verge 控制配置。'}
    except (OSError, KeyError):
        value = {'state': 'unavailable', 'message': '无法连接本机 Clash Verge，请确认应用和代理内核已启动。'}
    except Exception:  # noqa: BLE001 - private config and transport errors stay inside the worker
        value = {'state': 'unavailable', 'message': '无法读取 Clash Verge 状态，请检查本机控制配置。'}
    try:
        pipe.send(value)
    finally:
        pipe.close()


class ConnectionStatus:
    """One bounded read-only probe at a time; dashboard requests never wait on IPC."""
    def __init__(self, root=DATA_ROOT):
        self.root = root
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.checked = 0.0
        self.value = {'state': 'checking', 'message': '正在读取本机 Clash Verge…', 'checked_at': None}

    def read(self, *, force=False):
        with self.lock:
            checking = bool(self.thread and self.thread.is_alive())
            if not self.stop.is_set() and not checking and (force or time.monotonic() - self.checked >= 10):
                self.thread = threading.Thread(target=self._refresh, name='clash-status', daemon=True)
                self.thread.start()
                checking = True
            return {**self.value, 'checking': checking}

    def _refresh(self):
        context = mp.get_context('spawn')
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_probe, args=(child, self.root), daemon=True)
        value = {'state': 'unavailable', 'message': 'Clash Verge 在 8 秒内未响应，请确认代理内核正常运行。'}
        try:
            process.start()
            child.close()
            deadline = time.monotonic() + 8
            while not self.stop.is_set() and time.monotonic() < deadline:
                if parent.poll(.1):
                    value = parent.recv()
                    break
                if not process.is_alive():
                    break
        except (OSError, EOFError, RuntimeError):
            value = {'state': 'unavailable', 'message': '本机 Clash Verge 状态读取失败，请重新检查连接。'}
        finally:
            if process.pid is not None:
                process.join(.2)
                if process.is_alive():
                    process.terminate()
                    process.join(1)
                process.close()
            child.close()
            parent.close()
            with self.lock:
                self.checked = time.monotonic()
                self.value = {**value, 'checked_at': time.time()}

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)


def rotation_status(root, connection=None):
    root = Path(root).resolve()
    value = {**usage_status(root, connection), 'processed': 0, 'batch_id': None, 'batch_state': None}
    path = root / 'state/runtime.sqlite3'
    if not path.exists():
        return value
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('BEGIN')
            row = db.execute('SELECT data FROM batches ORDER BY created DESC LIMIT 1').fetchone()
            if row is None:
                return value
            batch = json.loads(row[0])
            processed = 0
            for lane in batch['lanes']:
                run = db.execute('SELECT data FROM runs WHERE id=?', (lane['run_id'],)).fetchone()
                processed += json.loads(run[0])['completed']
        running = lock_held(root, 'runner') and not lock_held(root, 'storage-maintenance')
        active = batch['status'] not in ('complete', 'cancelled')
        return {**value, 'batch_id': batch['id'], 'processed': processed,
                'batch_state': 'complete' if not active else 'running' if running else 'paused'}
    except (OSError, sqlite3.Error, ValueError, KeyError, IndexError, TypeError):
        return {**value, 'state': 'unavailable', 'message': '批次轮换记录暂时不可读，请稍后重新检查。'}
