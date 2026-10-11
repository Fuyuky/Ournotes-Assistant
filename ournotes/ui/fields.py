"""On-demand fields; bounded exact-ROI cache local to one emulator observer."""
import hashlib
import re
import time
from collections import OrderedDict

import numpy as np
from PIL import ImageOps
from rapidocr_onnxruntime import RapidOCR

from ournotes.vision.ocr import TextBox


class Fields:
    def __init__(self):
        self.engine = None
        self.cache = OrderedDict()
        self.metrics = []

    def clear(self):
        self.cache.clear()

    def read(self, frame, name, regions, epoch, *, scale=1, single_line=True, digits=False):
        crops = [frame.image.crop(box) for box in regions]
        fingerprint = hashlib.sha256(b"".join(crop.tobytes() for crop in crops)).hexdigest()
        key = (epoch, name, tuple(map(tuple, regions)), fingerprint, single_line, digits, scale)
        if key in self.cache:
            self.metrics.append({"field": name, "seconds": 0, "cached": True})
            return self.cache[key]
        start = time.perf_counter()
        if self.engine is None:
            self.engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        texts = []
        for box, crop in zip(regions, crops):
            if digits:
                # Find ALL bright glyphs in the number field before trimming; never
                # crop just the final digit of a potentially nonzero balance.
                ys, xs = np.where(np.asarray(crop).min(axis=2) > 160)
                if not len(xs):
                    continue
                crop = crop.crop((xs.min(), ys.min(), xs.max()+1, ys.max()+1))
                crop = ImageOps.expand(crop, border=2, fill=(62, 66, 101))
            if scale != 1:
                crop = crop.resize((crop.width*scale, crop.height*scale))
            result, _ = self.engine(np.asarray(crop)[:, :, ::-1], use_cls=False, use_det=not single_line)
            if single_line:
                for text, confidence in result or []:
                    if confidence >= .7 and (not digits or re.fullmatch(r"[0-9,]+", text)):
                        texts.append(TextBox(text, float(confidence), tuple(box)))
                continue
            for points, text, confidence in result or []:
                if confidence < .7:
                    continue
                points = np.asarray(points) / scale + np.array(box[:2])
                texts.append(TextBox(text, float(confidence), (int(points[:, 0].min()), int(points[:, 1].min()),
                                                              int(points[:, 0].max()), int(points[:, 1].max()))))
        self.cache[key] = texts
        while len(self.cache) > 24:
            self.cache.popitem(last=False)
        self.metrics.append({"field": name, "seconds": time.perf_counter()-start, "cached": False})
        return texts
