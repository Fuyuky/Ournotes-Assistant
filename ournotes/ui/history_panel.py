"""Detect an unsupported foreground modal without navigating or reading records."""
from typing import ClassVar

import cv2
import numpy as np
from PIL import Image

from ournotes.paths import ROOT

ANCHORS = ROOT / "ournotes/vision/assets"

class HistoryPanel:
    boxes: ClassVar = {'title': (720, 43, 880, 84), 'subtitle': (597, 115, 1005, 148),
             'close': (645, 785, 955, 850)}

    def __init__(self):
        self.templates = {name: np.asarray(Image.open(ANCHORS / f'history-{name}.png').convert('L'))
                          for name in self.boxes}

    def matches(self, image):
        for name, box in self.boxes.items():
            patch = np.asarray(image.crop(box).convert('L'))
            reference = self.templates[name]
            # The modal is translucent; the pool animation changes its background brightness.
            if (patch.shape != reference.shape or
                    float(cv2.matchTemplate(patch, reference, cv2.TM_CCOEFF_NORMED)[0, 0]) < .97):
                return False
        return True
