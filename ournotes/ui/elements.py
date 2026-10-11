"""Immutable image assets shared by lanes; all match state belongs to a Frame."""
import json
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image

from ournotes.paths import ASSETS


@dataclass(frozen=True)
class Match:
    name: str
    text: str
    score: float
    box: tuple
    detect_box: tuple


@lru_cache(maxsize=1)
def assets():
    folder = ASSETS / "ui"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    definitions = {}
    for name, spec in manifest["elements"].items():
        rgb = np.asarray(Image.open(folder / spec["file"]).convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        bright = gray >= np.percentile(gray, 80)
        rgb.flags.writeable = gray.flags.writeable = bright.flags.writeable = False
        definitions[name] = (spec, gray, bright)
    return manifest, definitions


def match(frame, name):
    if name in frame.matches:
        return frame.matches[name]
    frame.checks += 1
    spec, template, bright = assets()[1][name]
    x1, y1, x2, y2 = spec["area"]
    offset = spec.get("offset", 4)
    left, top = max(0, x1-offset), max(0, y1-offset)
    right, bottom = min(1600, x2+offset), min(900, y2+offset)
    scores = cv2.matchTemplate(frame.gray[top:bottom, left:right], template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (dx, dy) = cv2.minMaxLoc(scores)
    px, py = left+dx, top+dy
    patch = frame.gray[py:py+template.shape[0], px:px+template.shape[1]]
    # Correlation alone accepts dimmed pages below a modal. Independently require
    # the light part of this control to remain near its reference intensity.
    light_error = abs(float(patch[bright].mean()) - float(template[bright].mean()))
    found = None
    if score >= spec.get("similarity", .88) and light_error <= spec.get("light_tolerance", 24):
        click = spec.get("click", spec["area"])
        box = tuple(v + (px-x1 if i % 2 == 0 else py-y1) for i, v in enumerate(click))
        found = Match(name, spec.get("text", ""), score, box,
                      (px, py, px+template.shape[1], py+template.shape[0]))
    frame.matches[name] = found
    return found
