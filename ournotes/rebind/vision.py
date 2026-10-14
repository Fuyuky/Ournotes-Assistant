"""Native ROI assets; no OCR in the ordinary rebind page classifier."""
import json
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ournotes.ui.frame import Frame


@lru_cache(maxsize=1)
def assets():
    root = Path(__file__).parent / 'assets'
    return {s['name']: (s, np.asarray(Image.open(root / s['file']).convert('RGB')))
            for s in json.loads((root / 'manifest.json').read_text('utf-8'))}


def matches(image, name):
    spec, template = assets()[name]
    x1, y1, x2, y2 = spec['box']
    pixels = np.asarray(image.convert('RGB'))
    roi = pixels[y1-4:y2+4, x1-4:x2+4]
    _, score, _, (dx, dy) = cv2.minMaxLoc(cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED))
    patch = roi[dy:dy+template.shape[0], dx:dx+template.shape[1]]
    bright = template.mean(axis=2) > 220
    return score >= .89 and (not bright.any() or abs(float(patch[bright].mean()) - float(template[bright].mean())) < 20)


def scene(image):
    if image.size != (1600, 900):
        raise ValueError('换绑要求 1600×900 原生画面。')
    if matches(image, 'sdk-loading-frame'):
        return 'loading'
    if matches(image, 'unconfirmed-text'):
        return 'unconfirmed'
    if matches(image, 'bind-confirm-title') and matches(image, 'bind-confirm-instructions'):
        return 'confirm_b'
    if matches(image, 'verify-identity-title'):
        return 'verify_a'
    if matches(image, 'bind-form-title'):
        return 'bind_form'
    if matches(image, 'binding-title') and matches(image, 'change-binding'):
        return 'bindings'
    if matches(image, 'user-center-title') and matches(image, 'binding-tile'):
        return 'user_center'
    return None


def field_text(fields, image, name, box):
    return ''.join(t.text for t in fields.read(Frame(image, 0), name, [box], 0, scale=2))


def input_focused(image, field, binding=False):
    pixels = np.asarray(image.convert('RGB'))
    borders = ((353, 418), (437, 502)) if binding else ((350, 409), (425, 484))
    checks = []
    for pair in borders:
        checks.append(all(((pixels[y-1:y+2, 665:820, 0] > 230)
                           & (pixels[y-1:y+2, 665:820, 1] < 165)
                           & (pixels[y-1:y+2, 665:820, 2] > 95)).mean(axis=1).max() > .85 for y in pair))
    return checks[field] and not checks[1-field]


def agreed(image):
    pixels = np.asarray(image.convert('RGB'))[508:526, 601:618]
    return ((pixels[:, :, 0] > 230) & (pixels[:, :, 1] < 160)
            & (pixels[:, :, 2] > 90)).mean() > .2
