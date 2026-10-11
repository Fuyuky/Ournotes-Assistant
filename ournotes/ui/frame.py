import time
from dataclasses import dataclass, field
from functools import cached_property

import cv2
import numpy as np
from PIL import Image


@dataclass
class Frame:
    image: Image.Image
    number: int
    captured_at: float = field(default_factory=time.monotonic)
    matches: dict = field(default_factory=dict)
    checks: int = 0

    @cached_property
    def rgb(self):
        value = np.asarray(self.image.convert("RGB"))
        value.flags.writeable = False
        return value

    @cached_property
    def gray(self):
        return cv2.cvtColor(self.rgb, cv2.COLOR_RGB2GRAY)

    def crop(self, box):
        x1, y1, x2, y2 = box
        return self.rgb[y1:y2, x1:x2]
