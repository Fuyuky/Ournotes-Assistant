"""One persistent recognition result per draw; no UI calls or shared SQLite connection."""
from __future__ import annotations

import concurrent.futures
import json
import time

from PIL import Image

from ournotes.common.files import atomic_json, file_hash
from ournotes.paths import CATALOG
from ournotes.vision.recognition import CardRecognizer


class RecognitionQueue:
    def __init__(self, root, run_id, factory=None):
        self.root = root
        self.folder = root / "state/runs" / run_id / "recognition"
        self.factory = factory or (lambda: CardRecognizer(CATALOG))
        self.engine = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="cards")
        self.jobs = {}

    def submit(self, draw):
        if draw["id"] not in self.jobs:
            self.jobs[draw["id"]] = self.pool.submit(self._recognize, dict(draw), time.time())

    def _recognize(self, draw, queued_at):
        path = self.folder / (draw["id"] + ".json")
        fingerprint = draw["screenshot"]["sha256"]
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached["screenshot_sha256"] != fingerprint:
                raise ValueError("后台识别缓存与截图不匹配。")
            return dict(cached, cached=True)
        started = time.time()
        value = {"draw_id": draw["id"], "screenshot_sha256": fingerprint, "queued_at": queued_at,
                     "started_at": started, "queue_seconds": started - queued_at, "model_seconds": 0, "cached": False}
        try:
            source = self.root / draw["screenshot"]["path"]
            t = time.perf_counter()
            if file_hash(source) != fingerprint:
                raise ValueError("结果截图校验失败。")
            with Image.open(source) as image:
                image = image.convert("RGB")
            value["image_read_seconds"] = time.perf_counter() - t
            if self.engine is None:
                t = time.perf_counter()
                self.engine = self.factory()
                value["model_seconds"] = time.perf_counter() - t
            t = time.perf_counter()
            result = self.engine.recognize(image)
            value["recognition_seconds"] = time.perf_counter() - t
            value["result"] = result
            value["status"] = ("complete" if result["status"] == "complete" and result["layout"] == draw["pulls"]
                               else "needs_review")
        except Exception as error:  # noqa: BLE001 - record once; review after the account, not mid-draw
            value.update(status="needs_review", error=type(error).__name__)
        value["finished_at"] = time.time()
        value["worker_seconds"] = value["finished_at"] - started
        atomic_json(path, value)
        return value

    def completed(self):
        ready = []
        for key, future in list(self.jobs.items()):
            if future.done():
                ready.append((key, future.result()))
                del self.jobs[key]
        return ready

    def close(self):
        self.pool.shutdown(wait=True)
