from __future__ import annotations

import json
import os
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ournotes.common.files import atomic_json, file_hash


class RunLog:
    def __init__(self, root: Path, run_id: str, *, debug_mode=False):
        self.root = root
        self.folder = root / "state/runs" / run_id
        self.folder.mkdir(parents=True, exist_ok=True)
        self.frames = deque(maxlen=30)
        self.last_frame_at = 0.0
        self.debug_mode = debug_mode
        from ournotes.common.bounded_log import LogBudget, LogStream
        budget = LogBudget(4 * 1024**2)
        self.events = ((self.folder / "events.jsonl").open("a", encoding="utf-8", buffering=1)
                       if debug_mode else LogStream(budget, self.folder / 'events.jsonl'))
        self.human = LogStream(budget, self.folder / 'run.log')
        self.metrics = ((self.folder / "timings.jsonl").open("a", encoding="utf-8", buffering=1)
                        if debug_mode else None)
        summary = self.folder / 'timing-summary.json'
        self.summary = json.loads(summary.read_text('utf-8')) if summary.exists() else {}

    @contextmanager
    def timed(self, operation, **fields):
        started = time.time()
        tick = time.perf_counter()
        try:
            yield fields
        finally:
            seconds = time.perf_counter() - tick
            key = fields.get('phase', 'other') + ':' + operation
            total = self.summary.setdefault(key, {'calls': 0, 'seconds': 0.0})
            total['calls'] += 1
            total['seconds'] += seconds
            if self.metrics:
                self.metrics.write(json.dumps(dict(operation=operation, started_at=started,
                                       seconds=seconds, **fields), ensure_ascii=False) + "\n")

    def observe(self, image):
        now = time.monotonic()
        if now - self.last_frame_at >= .2:
            self._remember(image, 'observation')
            self.last_frame_at = now

    def _remember(self, image, label):
        # Windows wall-clock timestamps may repeat across successive frames.
        stamp, frame_id = time.time(), uuid4().hex
        self.frames.append((stamp, frame_id, label, image.copy()))
        while self.frames and self.frames[0][0] < stamp - 10:
            self.frames.popleft()
        return {'storage': 'memory', 'frame_id': frame_id, 'label': label}

    def clear_frames(self):
        self.frames.clear()

    def event(self, name, **data):
        item = dict(time=time.time(), event=name, **data)
        self.events.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.human.write(time.strftime("%H:%M:%S") + " " + name + " " +
                         json.dumps(data, ensure_ascii=False) + "\n")
        self.events.flush()
        return item

    def screenshot(self, image, label, *, persist=False):
        # Only results awaiting durable recognition need a file during normal play.
        if not persist:
            return self._remember(image, label)
        path = self.folder / "screens" / (f"{time.time_ns()}-{uuid4().hex}-{label}.png")
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as f:
            image.save(f, format="PNG")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        return {"path": path.relative_to(self.root).as_posix(), "sha256": file_hash(path),
                "storage": "temporary"}

    def incident(self, reason, snapshot, *, event='paused'):
        import hashlib

        from ournotes.runtime.storage_budget import LIMIT, usage
        key = hashlib.sha256(json.dumps([reason, snapshot.get('phase'), snapshot.get('account_id'),
                                         snapshot.get('transaction_ids')], sort_keys=True).encode()).hexdigest()
        ledger = self.folder / 'incident-index.json'
        seen = json.loads(ledger.read_text('utf-8')) if ledger.exists() else {}
        previous = seen.get(key)
        if previous and (self.folder / 'incidents' / previous['directory']).is_dir():
            previous['repeats'] += 1
            previous['last_seen'] = time.time()
            atomic_json(ledger, seen)
            self.event(event, reason=reason, repeated=True, account_id=snapshot.get('account_id'))
            return self.folder / 'incidents' / previous['directory']
        target = self.folder / "incidents" / f"{time.time_ns()}-{uuid4().hex}"
        target.mkdir(parents=True)
        atomic_json(target / "state.json", {"reason": reason, "snapshot": snapshot})
        frames = []
        # Settlement images are stored independently; incident context is sampled.
        selected = list(self.frames)
        if len(selected) > 6:
            selected = selected[:3] + selected[-3:]
        if usage(self.root, refresh=True) >= LIMIT:
            selected = []
        for index, (stamp, frame_id, label, image) in enumerate(selected):
            path = target / f"{index:03d}-{stamp:.3f}.png"
            image.save(path)
            frames.append({'time': stamp, 'frame_id': frame_id, 'label': label, 'file': path.name})
        atomic_json(target / 'frames.json', {'frames': frames})
        seen[key] = {'directory': target.name, 'repeats': 1, 'last_seen': time.time()}
        atomic_json(ledger, dict(list(seen.items())[-100:]))
        self.event(event, reason=reason, incident=str(target))
        return target

    def close(self):
        self.clear_frames()
        self.events.close()
        self.human.close()
        if self.metrics:
            self.metrics.close()
        atomic_json(self.folder / 'timing-summary.json', self.summary)
