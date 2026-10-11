from __future__ import annotations

import time

from ournotes.ui.elements import assets, match
from ournotes.ui.fields import Fields
from ournotes.ui.frame import Frame
from ournotes.ui.pages import INTERRUPTS, candidates
from ournotes.vision.ocr import Observation, TextBox, Vision
from ournotes.vision.recognition import RecognitionError, ResultLayout, ResultLoading


class Observer:
    """Cheap scene evidence first; OCR is explicit fields or budgeted recovery."""
    def __init__(self, *, allow_fallback=True):
        self.manifest = assets()[0]
        self.pages = self.manifest["pages"]
        self.layout = ResultLayout()
        self.fields = Fields()
        self.fallback_ocr = None
        self.allow_fallback = allow_fallback
        self.account_key = None
        self.last_scene = None
        self.page_epoch = 0
        self.sequence = 0
        self.unknown_since = None
        self.last_global = 0
        self.last_fallback = 0
        self.fallbacks = 0
        self.episode_fallbacks = 0
        self.last_mode = "elements"
        self.last_metrics = {}
        self.last_loading_at = float('-inf')
        from ournotes.ui.history_panel import HistoryPanel
        self.history_layout = HistoryPanel()

    def invalidate(self):
        # Every read creates a new frame, so same-frame matches never survive input.
        # Unchanged field ROIs remain reusable within their explicit account/transaction scope.
        pass

    def _page(self, frame, scene):
        for index, page in enumerate(self.pages):
            if page["scene"] != scene:
                continue
            if all(match(frame, name) for name in page["anchors"]):
                return index, page
        return None

    def _result(self, frame):
        try:
            _positions, scores = self.layout.inspect(frame.image, foreground=True)
        except ResultLoading:
            return Observation(frame.image, [], 'loading', 'draw_result_loading')
        except RecognitionError:
            return None
        observation = Observation(frame.image, [TextBox("招募结算", 1, (726, 64, 876, 105)),
                                                TextBox("招募TOP", 1, tuple(scores["top_box"]))],
                                  "draw_result", "draw_result")
        observation.result_layout = scores
        return observation

    def read(self, image, *, phase=None, account_id=None, last_action=None, captured_at=None):
        start = time.perf_counter()
        if abs(image.width/image.height-16/9) > .02:
            raise ValueError("仅支持完整16:9画面。")
        if image.size != (1600, 900):
            image = image.resize((1600, 900))
        if account_id != self.account_key:
            self.account_key = account_id
            self.fields.clear()
            self.page_epoch += 1
            self.fallbacks = 0
            self.episode_fallbacks = 0
            self.last_loading_at = float('-inf')
            self.unknown_since = None
            self.last_global = self.last_fallback = 0
        self.sequence += 1
        frame = Frame(image, self.sequence, captured_at or time.monotonic())
        wanted = (candidates(phase, last_action) if phase else
                  tuple(dict.fromkeys(INTERRUPTS + tuple(p['scene'] for p in self.pages))) + ('draw_result',))
        observation, selected = None, None
        sample = frame.gray[100:850:16, ::16]
        blank = (sample > 248).mean() > .985 or (sample < 4).mean() > .985
        if blank:
            observation = Observation(image, [], 'loading', 'loading')
        elif self.history_layout.matches(image):
            observation = Observation(image, [TextBox('关闭', 1, (645, 785, 955, 850))],
                                      'recruit_history', 'recruit_history')
        for scene in (() if observation else wanted):
            if scene == "draw_result":
                observation = self._result(frame)
                if observation:
                    break
            selected = self._page(frame, scene)
            if selected:
                break
        mode = "elements"
        now = time.monotonic()
        if not selected and not observation:
            if self.unknown_since is None:
                self.unknown_since = now
            # Recovery scans all cheap anchors at a lower rate; it never launches OCR per frame.
            if now-self.unknown_since >= .8 and now-self.last_global >= .5:
                self.last_global = now
                for scene in dict.fromkeys(p['scene'] for p in self.pages):
                    selected = self._page(frame, scene)
                    if selected:
                        mode = "global_elements"
                        break
                if not selected:
                    observation = self._result(frame)
            if (not selected and not observation and self.allow_fallback and now-self.unknown_since >= 3
                    and now-self.last_fallback >= 10 and self.episode_fallbacks < 3 and self.fallbacks < 12):
                self.last_fallback = now
                self.fallbacks += 1
                self.episode_fallbacks += 1
                if self.fallback_ocr is None:
                    self.fallback_ocr = Vision()
                observation = self.fallback_ocr.read(image)
                mode = "recovery_ocr"
        if selected:
            index, page = selected
            controls = [m for name in page['controls'] if (m := match(frame, name)) and m.text]
            texts = [TextBox(m.text, m.score, m.box) for m in controls]
            texts += [TextBox(c['text'], 1, tuple(c['box'])) for c in page.get('fixed_controls', [])]
            observation = Observation(image, texts, page['scene'], page['scene'])
            observation.page_index = index
        # The animated Now Loading lettering can momentarily miss its template
        # while the title controls remain visible underneath it. Require an
        # uninterrupted clear title interval before accepting those controls.
        if observation and observation.scene == 'loading':
            self.last_loading_at = now
        elif observation and observation.scene == 'title' and now-self.last_loading_at < 2:
            observation = Observation(image, [], 'loading', 'loading')
            mode = 'loading_transition'
        if observation is None:
            observation = Observation(image, [], "unknown", "transition")
            mode = "transition"
        elif observation.scene == "draw_result" and mode != "recovery_ocr":
            mode = "result_template"
        elif observation.signature == 'draw_result_loading':
            mode = 'result_loading'
        elif blank:
            mode = 'loading_pixels'
        if observation.scene != "unknown":
            self.unknown_since = None
            self.episode_fallbacks = 0
        if observation.scene != self.last_scene:
            self.page_epoch += 1
            self.last_scene = observation.scene
        observation.frame = frame
        observation.fast = mode not in ("recovery_ocr", "transition")
        observation.mode = mode
        self.last_mode = mode
        self.last_metrics = {"checks": frame.checks, "candidates": len(wanted),
                             "fallback_count": self.fallbacks, "observer_seconds": time.perf_counter()-start}
        return observation

    def read_fields(self, observation, name, transaction=None):
        frame = observation.frame
        scope = (self.account_key, self.page_epoch, transaction)
        numeric = []
        if name == 'history_button':
            texts = self.fields.read(frame, name, [(600, 790, 1550, 890)], scope,
                                     scale=2, single_line=False)
            observation.texts.extend(texts)
            return texts
        if name == 'wallet':
            regions = [(1090, 35, 1155, 68), (1320, 35, 1455, 68)]
            texts = self.fields.read(frame, name, regions, scope, scale=3, digits=True)
            observation.texts.extend(texts)
            return texts
        if name == "confirmation":
            if observation.page_index is None:
                return observation.texts
            page = self.pages[observation.page_index]
            layout = page.get('field_layout')
            if layout == 'single_ticket':
                regions = [(490, 268, 1110, 315), (480, 360, 1120, 400)]
            elif layout == 'ten_ticket':
                regions = [(470, 165, 1130, 220), (500, 335, 1100, 378)]
            else:
                regions = [(470, 165, 1130, 220), (500, 305, 1100, 345),
                           (390, 446, 451, 483), (808, 446, 872, 483)]
                numeric = [(470, 446, 590, 483), (887, 446, 1008, 483)]
        elif name == 'download_progress':
            regions = [(578, 750, 685, 797)]
        elif name == 'login_feedback':
            # The SDK's short-lived toast overlays the code row, while the
            # email-form anchors still match. Never rely on scene fallback OCR.
            texts = self.fields.read(frame, name, [(600, 410, 1000, 515)], scope,
                                     scale=2, single_line=False)
            observation.texts.extend(texts)
            return texts
        elif name in ('draw_button_1', 'draw_button_10'):
            region = (1030, 650, 1295, 738) if name == 'draw_button_1' else (1300, 650, 1595, 738)
            texts = self.fields.read(frame, name, [region], scope, scale=2, single_line=False)
            observation.texts.extend(texts)
            return texts
        else:
            regions = {"uid": [(588, 336, 1010, 374)], "email_input": [(610, 363, 870, 400)],
                       "code_input": [(610, 438, 862, 474)], "login_send": [(872, 438, 991, 474)]}[name]
        texts = self.fields.read(frame, name, regions, scope, scale=2)
        if numeric:
            texts = texts + self.fields.read(frame, 'paid_balances', numeric, scope, scale=3, digits=True)
        observation.texts.extend(texts)
        return texts

    def guard(self, observation, image, label, args):
        """Revalidate the foreground controls, never the animated surrounding background."""
        frame = Frame(image, -1)
        if observation.scene == 'recruit_history':
            return self.history_layout.matches(image)
        if self.history_layout.matches(image):
            return False
        if observation.scene == 'title' and time.monotonic()-self.last_loading_at < 2:
            return False
        if any(scene != observation.scene and self._page(frame, scene) for scene in INTERRUPTS):
            return False
        if observation.scene == 'draw_result':
            return self._result(frame) is not None
        page_index = observation.page_index
        if page_index is None:
            return None  # Recovery-only pages retain the foreground guard.
        page = self.pages[page_index]
        if not all(match(frame, name) for name in page['anchors'] + page.get('guards', [])):
            return False
        # Controls inside this known foreground page must remain present when they
        # are the actual target. The current frame owns all offsets and results.
        x, y = args
        for name in page['controls']:
            previous = match(observation.frame, name)
            if (previous and previous.box[0] <= x <= previous.box[2]
                    and previous.box[1] <= y <= previous.box[3] and not match(frame, name)):
                return False
        return True
