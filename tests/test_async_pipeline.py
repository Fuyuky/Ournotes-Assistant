import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image, ImageEnhance

from ournotes.common.files import file_hash
from ournotes.paths import REFERENCE
from ournotes.runtime.model import PauseRun
from ournotes.runtime.pools import make_plan
from ournotes.runtime.recognition_queue import RecognitionQueue
from ournotes.runtime.runner import Runner
from ournotes.runtime.store import RuntimeStore
from ournotes.vision.ocr import Vision


def test_job_runs_once_and_cached_result_survives_restart(tmp_path):
    image = tmp_path / "result.png"
    Image.new("RGB", (1600, 900)).save(image)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def recognize(_):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"status": "complete", "layout": 10, "slots": []}
    engine = SimpleNamespace(recognize=recognize)
    draw = {"id": "draw", "pulls": 10, "screenshot": {"path": "result.png", "sha256": file_hash(image)}}
    worker = RecognitionQueue(tmp_path, "run", factory=lambda: engine)
    try:
        worker.submit(draw)
        assert entered.wait(5)
        for _ in range(10):
            worker.submit(draw)
        assert worker.completed() == []  # The caller remains free to drive UI.
    finally:
        release.set()
        worker.close()
    assert len(calls) == 1
    assert worker.completed()[0][1]["status"] == "complete"
    resumed = RecognitionQueue(tmp_path, "run", factory=lambda: pytest.fail("repeated model/recognition"))
    resumed.submit(draw)
    resumed.close()
    assert resumed.completed()[0][1]["cached"]


def test_bad_screenshot_is_saved_for_account_end_review(tmp_path):
    image = tmp_path / "bad.png"
    image.write_bytes(b"changed")
    engine = Mock()
    worker = RecognitionQueue(tmp_path, "run", factory=lambda: engine)
    worker.submit({"id": "bad", "pulls": 1, "screenshot": {"path": "bad.png", "sha256": "wrong"}})
    worker.close()
    value = worker.completed()[0][1]
    assert value["status"] == "needs_review"
    engine.recognize.assert_not_called()


def test_next_draw_does_not_wait_for_captured_or_failed_recognition(tmp_path):
    r = Runner()
    r.db = RuntimeStore(tmp_path)
    r.run = r.db.create_run("都抽", "test", 1)
    r.account = r.db.reserve(r.run, make_plan("都抽"))
    for status in ("captured", "needs_review"):
        draw = r.draw_flow.current_draw()
        draw.update(status=status, result_exit_confirmed=True)
        r.db.save_draw(draw)
    assert r.draw_flow.current_draw()["ordinal"] == 2
    r.recognition = Mock()
    r.recognition.completed.return_value = []
    r.publish = Mock()
    r.finish_account()
    assert r.run["completed"] == 0
    # Failures are reported only when all outstanding jobs have finished.
    first = r.db.draws(r.account["id"])[0]
    first["status"] = "complete"
    r.db.save_draw(first)
    with pytest.raises(PauseRun, match="账号操作完成"):
        r.finish_account()
    r.db.close()


def test_collecting_async_result_preserves_confirmed_ui_exit(tmp_path):
    r = Runner()
    r.db = RuntimeStore(tmp_path)
    r.run = r.db.create_run("都抽", "test", 1)
    r.account = r.db.reserve(r.run, make_plan("都抽"))
    d = r.draw_flow.current_draw()
    d.update(status="captured", result_exit_confirmed=True)
    r.db.save_draw(d)
    r.recognition, r.log = Mock(), Mock()
    r.recognition.completed.return_value = [(d["id"], {"status": "complete", "result": {"slots": []}, "finished_at": 123})]
    r.draw_flow.check_recognition()
    saved = r.db.draws(r.account["id"])[0]
    assert saved["status"] == "complete" and saved["result_exit_confirmed"]
    r.db.close()


def test_result_uses_no_ocr_and_overlay_does_not_use_fast_path():
    vision = Vision()
    image = Image.open(REFERENCE / "gacha-workflow/results/01-mygo-10.png")
    original = vision.ocr
    vision.ocr = Mock(side_effect=AssertionError("result must not run OCR"))
    assert vision.read(image).scene == "draw_result"
    assert vision.last_mode == "result_template"
    vision.ocr = original
    vision.read(ImageEnhance.Brightness(image).enhance(.4))
    assert vision.last_mode != "result_template"


def test_unchanged_confirm_uses_cache_and_invalidation_forces_read():
    vision = Vision()
    image = Image.open(REFERENCE / "gacha-workflow/screens/03-mygo-confirm.png")
    first = vision.read(image)
    assert first.scene == "draw_confirm" and vision.last_mode == "confirm_roi"
    original = vision.ocr
    vision.ocr = Mock(side_effect=AssertionError("unchanged confirmation must not repeat OCR"))
    assert vision.read(image).text == first.text
    assert vision.last_mode == "cached_confirm_roi"
    vision.invalidate()
    vision.ocr = original
    assert vision.read(image).scene == "draw_confirm"
    assert vision.last_mode == "confirm_roi"
