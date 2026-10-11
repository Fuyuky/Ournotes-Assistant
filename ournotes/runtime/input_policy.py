"""Small bounded input variation; scheduling never blocks observation."""
from __future__ import annotations

import json
import random

from ournotes.paths import ROOT


class InputPolicy:
    def __init__(self, config=None, rng=None):
        if config is None:
            config = json.loads((ROOT / 'config/input.json').read_text(encoding='utf-8'))
        self.rng = rng or random.Random()
        self.radius = int(config['click_radius_px'])
        self.low, self.high = config['interval_seconds']
        if not 0 <= self.radius <= 8 or not 0 <= self.low <= self.high <= 2:
            raise ValueError('随机输入范围无效')

    def interval(self):
        return self.rng.uniform(self.low, self.high)

    def point(self, point, observation):
        x, y = map(int, point)
        width, height = observation.image.size
        left, top = max(0, x-self.radius), max(0, y-self.radius)
        right, bottom = min(width-1, x+self.radius), min(height-1, y+self.radius)
        # Text/button interiors constrain the jitter; never expand a target's hit area.
        boxes = [t.box for t in getattr(observation, 'texts', [])
                 if t.box[0] <= x < t.box[2] and t.box[1] <= y < t.box[3]]
        for a, b, c, d in boxes:
            left, right = max(left, min(x, a+2)), min(right, max(x, c-3))
            top, bottom = max(top, min(y, b+2)), min(bottom, max(y, d-3))
        return self.rng.randint(left, right), self.rng.randint(top, bottom)
