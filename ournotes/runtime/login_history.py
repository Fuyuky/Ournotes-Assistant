"""Visual controls restricted to the Bilibili login-history modal."""
from functools import lru_cache

import cv2
import numpy as np

from ournotes.paths import ASSETS


@lru_cache(maxsize=2)
def _template(control):
    template = cv2.imread(str(ASSETS / "login-history" / (control + ".png")), 0)
    if template is None:
        raise ValueError("缺少登录记录控件素材。")
    template.flags.writeable = False
    return template


def history_control(image, control):
    template = _template(control)
    # Deliberately excludes the dialog close button and all game controls.
    bounds = {"expand": (930, 360, 985, 435), "remove": (930, 370, 985, 640)}
    x1, y1, x2, y2 = bounds[control]
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    scores = cv2.matchTemplate(gray[y1:y2, x1:x2], template, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(scores >= .94)
    if not len(ys):
        return None
    # Remove the first visible entry. The list moves upward after each confirmation.
    index = min(range(len(ys)), key=lambda i: (ys[i], -scores[ys[i], xs[i]]))
    return (int(x1 + xs[index] + template.shape[1] // 2),
            int(y1 + ys[index] + template.shape[0] // 2))
