"""One in-memory state stream for the loopback console and all its panels."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from copy import deepcopy


def _stable(value):
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()
                if key not in {'observed_at', 'loaded_at', 'updated_at'}}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


class StateStream:
    """Project services once and share changed state with every browser."""

    def __init__(self, provider):
        self.provider = provider
        self.lock = threading.RLock()
        self.value = None
        self.signature = None
        self.revision = 0
        self.condition = asyncio.Condition()
        self.task = None
        self.closed = False

    def read(self):
        with self.lock:
            if self.value is None:
                self._sample()
            return deepcopy(self.value)

    def _sample(self):
        with self.lock:
            value = self.provider()
            signature = json.dumps(_stable(value), ensure_ascii=False, sort_keys=True)
            changed = signature != self.signature
            if changed:
                self.revision += 1
                self.signature = signature
            self.value = {**value, 'stream_revision': self.revision}
            return changed

    async def refresh(self):
        if self.closed:
            return self.read()
        changed = await asyncio.to_thread(self._sample)
        if changed:
            async with self.condition:
                self.condition.notify_all()
        return self.read()

    async def start(self):
        self.closed = False
        await self.refresh()
        self.task = asyncio.create_task(self._run(), name='console-state')

    async def _run(self):
        while True:
            value = self.read()
            active = value.get('running') or value.get('game_update', {}).get('status') == 'running'
            active = active or value.get('email_rebind', {}).get('running')
            await asyncio.sleep(1 if active else 3)
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 -- isolate a projection failure from all connected browsers
                await asyncio.sleep(2)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def next(self, revision, timeout=25):
        async with self.condition:
            try:
                await asyncio.wait_for(self.condition.wait_for(lambda: self.revision > revision), timeout)
            except TimeoutError:
                return {'type': 'heartbeat', 'at': time.time()}
        return {'type': 'snapshot', 'data': self.read()}
