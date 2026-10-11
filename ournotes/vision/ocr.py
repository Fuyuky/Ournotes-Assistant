"""Local OCR and pixel templates. No generative model, network or game input."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

from ournotes.paths import ASSETS
from ournotes.vision.recognition import RecognitionError, ResultLayout


def normalized(text):
    return re.sub(r"[\s!！?？:：,，。·・/\\（）()\-—]+", "", text).casefold()


@dataclass
class TextBox:
    text: str
    score: float
    box: tuple[int, int, int, int]

    @property
    def center(self):
        x, y, r, b = self.box
        return ((x + r) // 2, (y + b) // 2)


@dataclass
class Observation:
    image: Image.Image
    texts: list[TextBox]
    scene: str
    signature: str
    frame: object | None = None
    page_index: int | None = None
    result_layout: dict | None = None
    fast: bool = False
    mode: str = 'full_ocr'

    @property
    def text(self):
        return "\n".join(t.text for t in self.texts)

    def find(self, value, *, exact=False, region=None):
        value = normalized(value)
        found = []
        eligible = []
        for t in self.texts:
            x, y = t.center
            if region and not (region[0] <= x <= region[2] and region[1] <= y <= region[3]):
                continue
            eligible.append(t)
            if (normalized(t.text) == value if exact else value in normalized(t.text)):
                found.append(t)
        if not found:
            # OCR can split one animated button label into adjacent text boxes.
            for first in eligible:
                row = sorted((t for t in eligible if t.box[0] >= first.box[0]
                              and abs(t.center[1] - first.center[1]) <= 12), key=lambda t: t.box[0])[:4]
                pieces = []
                for t in row:
                    if pieces and t.box[0] - pieces[-1].box[2] > 80:
                        break
                    pieces.append(t)
                    if len(pieces) > 1 and normalized("".join(p.text for p in pieces)) == value:
                        found.append(TextBox(" ".join(p.text for p in pieces), min(p.score for p in pieces),
                            (min(p.box[0] for p in pieces), min(p.box[1] for p in pieces),
                             max(p.box[2] for p in pieces), max(p.box[3] for p in pieces))))
        return max(found, key=lambda t: t.score) if found else None

    def has(self, value):
        return normalized(value) in normalized(self.text)


class Vision:
    def __init__(self):
        self.ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        self.skip = cv2.imread(str(ASSETS / "vision-fallback/animation-skip.png"), 0)
        self.layout = ResultLayout()
        self.cache = None
        self.last_mode = "full_ocr"
        self.templates = {}
        for key, path, box in (
            ("pool", ASSETS / "vision-fallback/pool.png", (288, 30, 344, 65)),
            ("confirm", ASSETS / "vision-fallback/confirm.png", (721, 85, 880, 123)),
        ):
            self.templates[key] = (box, np.asarray(Image.open(path).convert("RGB")))

    def invalidate(self):
        self.cache = None

    def read(self, image):
        if abs(image.width / image.height - 16 / 9) > .02:
            raise ValueError("仅支持完整16:9模拟器画面。")
        image = image.resize((1600, 900), Image.Resampling.LANCZOS)
        pixels = np.asarray(image)
        # Button pixels are checked in absolute intensity so a dimming overlay is rejected.
        try:
            _slots, scores = self.layout.inspect(image, foreground=True)
            obs = Observation(image, [TextBox("招募结算", 1, (726, 64, 876, 105)),
                                     TextBox("招募TOP", 1, tuple(scores["top_box"]))], "draw_result", "result")
            self.last_mode = "result_template"
            return obs
        except RecognitionError:
            pass
        scope, box = "full_ocr", (0, 0, 1600, 900)
        for key in ("confirm", "pool"):
            bounds, reference = self.templates[key]
            x1, y1, x2, y2 = bounds
            if np.abs(pixels[y1:y2, x1:x2].astype(float) - reference).mean() < 4:
                scope = key + "_roi"
                box = (295, 70, 1305, 850) if key == "confirm" else (1000, 620, 1595, 805)
                break
        x1, y1, x2, y2 = box
        roi = pixels[y1:y2, x1:x2]
        if self.cache and self.cache[0] == scope and np.array_equal(roi, self.cache[1]):
            old = self.cache[2]
            self.last_mode = "cached_" + scope
            return Observation(image, old.texts, old.scene, old.signature)
        start = time.perf_counter()
        obs = self._read_ocr(image, box)
        if scope == "pool_roi" and (obs.has("招募10次") or obs.has("招募1次")):
            obs.texts.append(TextBox("招募详情", 1, (745, 819, 898, 864)))
            obs.scene = "pool"
            obs.signature = hashlib.sha256((obs.scene + obs.text).encode()).hexdigest()[:16]
        elif scope != "full_ocr" and obs.scene == "unknown":
            scope, box = "full_ocr_fallback", (0, 0, 1600, 900)
            obs = self._read_ocr(image, box)
            roi = pixels
        self.last_mode = scope
        self.last_ocr_seconds = time.perf_counter() - start
        self.cache = (scope, roi.copy(), obs)
        return obs

    def _read_ocr(self, image, region=(0, 0, 1600, 900)):
        if abs(image.width / image.height - 16 / 9) > .02:
            raise ValueError("仅支持完整16:9模拟器画面。")
        image = image.resize((1600, 900), Image.Resampling.LANCZOS)
        x0, y0, x2, y2 = region
        result, _ = self.ocr(np.array(image.crop(region))[:, :, ::-1])
        texts = []
        for box, text, score in result or []:
            if score < .65:
                continue
            points = np.array(box) + np.array([x0, y0])
            texts.append(TextBox(text, float(score), (int(points[:, 0].min()), int(points[:, 1].min()),
                                                       int(points[:, 0].max()), int(points[:, 1].max()))))
        obs = Observation(image, texts, "unknown", "")
        obs.scene = self.classify(obs)
        if obs.scene == "draw_confirm" and obs.has("付费"):
            # Tiny isolated zeroes are frequently omitted by full-screen detection.
            for x1, y1, x2, y2 in ((900, 440, 1010, 485), (495, 440, 595, 485)):
                crop = image.crop((x1, y1, x2, y2)).resize(((x2 - x1) * 3, (y2 - y1) * 3))
                numbers, _ = self.ocr(np.array(crop)[:, :, ::-1])
                for box, text, score in numbers or []:
                    if re.fullmatch(r"\d+", text) and score >= .9:
                        points = np.asarray(box) / 3 + np.array([x1, y1])
                        center = points.mean(axis=0)
                        if not any(abs(t.center[0] - center[0]) < 12 and abs(t.center[1] - center[1]) < 12
                                   for t in obs.texts):
                            obs.texts.append(TextBox(text, float(score),
                                (int(points[:, 0].min()), int(points[:, 1].min()),
                                 int(points[:, 0].max()), int(points[:, 1].max()))))
        # Exclude transient countdown values from the state transition fingerprint.
        semantic = re.sub(r"\d+\s*[sS秒]", "COUNTDOWN", obs.text)
        obs.signature = hashlib.sha256((obs.scene + semantic).encode()).hexdigest()[:16]
        return obs

    def classify(self, o):
        h = o.has
        if h("网络连接错误") or h("连接失败"):
            return "network_error"
        if h("邀请您撰写评价") or h("稍后再说"):
            return "review"
        if h("用户中心") and h("退出登录"):
            return "user_center"
        if h("是否确认删除该账号的登录记录") and h("确定") and h("取消"):
            return "login_history_confirm"
        if h("切换其他账号"):
            return "login_history"
        if h("登录记录"):
            return "login_history_expanded"
        if h("Sign in with Apple") and h("账号登录"):
            return "login_options"
        if (h("邮箱登录") or h("登录/注册")) and h("验证码"):
            return "email_form"
        if h("用户中心") and h("清除缓存"):
            return "title_menu"
        if h("标题画面") and h("OK") and not h("个人资料"):
            return "return_title_confirm"
        if h("招募结算"):
            return "draw_result"
        if h("发个动态吧") and o.find("爱音", region=(400, 600, 1000, 660)):
            return "character_dialogue"
        if h("是否一键领取"):
            return "gift_confirm"
        if h("已领取以下道具"):
            return "gift_received"
        if h("礼物盒") and (h("一键领取") or h("暂无可领取")):
            return "gift_box"
        if h("商店") and h("购买") and h("星钻"):
            return "shop"
        if h("数据下载") or (h("下载") and h("MB") and h("OK")):
            return "download_confirm" if h("OK") else "downloading"
        if h("NOW LOADING"):
            return "loading"
        if h("招募") and h("OK") and (h("消耗") or h("使用") or h("招募10次") or h("招募1次")):
            return "draw_confirm"
        if h("招募详情") and h("提供概率"):
            return "pool_detail"
        if h("招募详情") and (h("招募10次") or h("招募1次")):
            return "pool"
        if h("个人资料") and h("标题画面") and h("用户中心"):
            return "home_menu"
        if h("招募") and h("商店") and h("故事") and h("乐队"):
            return "home"
        if h("TAP TO START"):
            return "title"
        if (h("登录奖励") or h("获得奖励") or h("追加翻唱") or h("TOUCH")
                or h("RANK UP") or h("升级奖励")):
            return "startup_reward"
        if h("公告") and (h("活动") or h("更新")):
            return "notice"
        gray = cv2.cvtColor(np.array(o.image), cv2.COLOR_RGB2GRAY)
        if self.skip is not None:
            roi = gray[20:110, 1350:1590]
            score = cv2.matchTemplate(roi, self.skip, cv2.TM_CCOEFF_NORMED).max()
            if score >= .85:
                return "animation"
        if h("SKIP"):
            return "animation"
        return "unknown"


def main():
    import argparse
    import json
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="+")
    args = p.parse_args()
    engine = Vision()
    for path in args.paths:
        o = engine.read(Image.open(path))
        print(json.dumps({"path": path, "scene": o.scene,
                          "texts": [[t.text, t.box] for t in o.texts]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
