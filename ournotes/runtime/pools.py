from __future__ import annotations

import json
import re

import cv2
import numpy as np
from PIL import Image

from ournotes.catalogs.cards import sha256
from ournotes.catalogs.runtime import current_catalog
from ournotes.runtime.presets import make_preset_plan, read_presets
from ournotes.vision.ocr import normalized

MYGO = "launch-mygo-pickup-20260831"
AVE = "launch-ave-mujica-pickup-20260831"
SSR = "launch-ssr-guaranteed-ticket"
MEMBER = "launch-ssr-member-guaranteed-ticket"
SR = "launch-sr-plus-guaranteed-ticket"
STANDARD = "our-notes-standard"
PRESETS = tuple(p['name'] for p in read_presets()['presets'])


def make_plan(preset):
    return make_preset_plan(preset)


class PoolCatalog:
    def __init__(self, directory=None):
        self.directory = directory or current_catalog()
        source = self.directory / 'pools.json'
        self.fingerprint = sha256(source)
        self.data = json.loads(source.read_text(encoding='utf-8'))
        self.pools = {x['key']: x for x in self.data['pools']}

    def get(self, key):
        if key.startswith('bdon-gacha-'):
            return next((x for x in self.pools.values() if key == f"bdon-gacha-{x['gacha_id']}"), None)
        return self.pools.get(key)


def confirm_identity(text, key, catalog=None):
    catalog = catalog or PoolCatalog()
    pool = catalog.get(key)
    if not pool:
        return False
    matched = [x['gacha_id'] for x in catalog.pools.values() if normalized(x['name']) in normalized(text)]
    return matched == [pool['gacha_id']]


def validate_confirmation(observation, entry, catalog=None):
    catalog = catalog or PoolCatalog()
    text = normalized(observation.text)
    if not confirm_identity(text, entry["pool_key"], catalog):
        raise ValueError("确认页的卡池身份与计划不符。")
    pool = catalog.get(entry['pool_key'])
    match = re.search(r"进行(\d+)次招募", text)
    if not match or int(match[1]) != entry["pulls"]:
        raise ValueError("确认页的抽数无法核实或不符。")
    if entry["resource"] == "stars":
        cost = re.search(r"消耗(\d+)个星钻", text)
        prices = {p['price'] for p in pool['products']
                  if p['itemType'] == 12 and p['drawCount'] == entry['pulls']}
        if len(prices) != 1 or not cost or int(cost[1]) not in prices:
            raise ValueError("免费星钻消耗与预设不符。")
        # Both paid balances must be explicitly zero for this first version.
        paid = [t for t in observation.texts if normalized(t.text) == "付费"]
        values = []
        for label in paid:
            x, y = label.center
            near = [t for t in observation.texts if x + 50 < t.center[0] < x + 175
                    and abs(t.center[1] - y) < 18 and re.fullmatch(r"[\d,]+", t.text)]
            if near:
                values.append(int(near[0].text.replace(",", "")))
        if len(values) != 2 or any(values):
            raise ValueError("无法确认付费星钻保持为零，已阻止提交。")
    else:
        cost = re.search(r"消耗(\d+)张", text)
        if (not cost or int(cost[1]) != entry["pulls"] or "个星钻" in text
                or not pool['ticket_name'] or normalized(pool['ticket_name']) not in text):
            raise ValueError("招募券种类、数量或支付方式不符。")
    return {"pool_key": entry["pool_key"], "pulls": entry["pulls"],
            "resource": entry["resource"], "confirmation_text": observation.text,
            "gacha_id": pool['gacha_id'], 'catalog_sha256': catalog.fingerprint}


class PoolFinder:
    def __init__(self, directory=None):
        self.catalog = PoolCatalog(directory)
        self.templates = []
        for asset in self.catalog.data['assets']:
            path = (self.catalog.directory / asset['image']).resolve()
            if not path.is_relative_to(self.catalog.directory.resolve()) or sha256(path) != asset['sha256']:
                raise ValueError('BDON卡池图校验失败。')
            raw = np.asarray(Image.open(path).convert('RGBA'))
            gray = cv2.cvtColor(raw[:, :, :3], cv2.COLOR_RGB2GRAY)
            scales = (.64, .73) if asset['role'] == 'banner' else (.835, .84, .845, .855, .86, .865, .955, .96, .965)
            for scale in scales:
                size = (round(gray.shape[1] * scale), round(gray.shape[0] * scale))
                template = cv2.resize(gray, size)
                mask = ((cv2.resize(raw[:, :, 3], size) > 245).astype(np.uint8) * 255
                        if asset['role'] == 'logo' else None)
                self.templates.append((asset, scale, template, mask))
        self.templates.sort(key=lambda x: x[0]['role'] != 'banner')

    def scan(self, image):
        rgb = np.array(image)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        positions, title_scores, selected = {}, [], []
        for asset, scale, template, mask in self.templates:
            key, role = asset["pool_key"], asset["role"]
            if role == 'logo':
                if not any(k == key for _s, k in selected):
                    continue
                roi = gray[78:335, 1000:1595]
                result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED, mask=mask)
                score = float(np.nan_to_num(result, nan=-1, posinf=-1, neginf=-1).max())
                title_scores.append((score, key))
            else:
                roi = gray[90:820, :365]
                result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
                _, score, _, point = cv2.minMaxLoc(result)
                if score >= .84:
                    entry = {"score": score, "center": (point[0] + template.shape[1] // 2,
                                                    point[1] + 90 + template.shape[0] // 2)}
                    if score > positions.get(key, {}).get("score", 0):
                        positions[key] = entry
                    if scale == .73 and abs(entry['center'][0] - 207) <= 12:
                        x, y = point[0], point[1] + 90
                        corners = [rgb[max(0, y-10):y+20, max(0, x-10):x+25],
                                   rgb[max(0, y-10):y+20, x+template.shape[1]-25:x+template.shape[1]+10]]
                        glowing = all(((c[:, :, 1] > 180) & (c[:, :, 2] > 180)
                                       & (c[:, :, 0] < 210)).mean() > .04 for c in corners)
                        if glowing:
                            selected.append((score, key))
        # Some game pages show only the shared video placeholder, with no logo.
        # Before accepting the highlighted BDON banner, rule out a conflicting
        # visible logo; the confirmation must still pass exact metadata identity.
        if selected and max((s for s, _ in title_scores), default=-1) < .80:
            chosen = {k for _, k in selected}
            for asset, _scale, template, mask in self.templates:
                if asset['role'] != 'logo' or asset['pool_key'] in chosen:
                    continue
                result = cv2.matchTemplate(gray[78:335, 1000:1595], template, cv2.TM_CCOEFF_NORMED, mask=mask)
                score = float(np.nan_to_num(result, nan=-1, posinf=-1, neginf=-1).max())
                title_scores.append((score, asset['pool_key']))
        title_scores = sorted(((max((s for s, k in title_scores if k == key), default=-1), key)
                               for key in self.catalog.pools), reverse=True)
        selected.sort(reverse=True)
        current = (title_scores[0][1] if title_scores and title_scores[0][0] >= .80
                   and title_scores[0][0] - title_scores[1][0] >= .05 else None)
        if current and selected and selected[0][1] != current:
            current = None
        if not current and selected and selected[0][0] >= .90 and title_scores[0][0] < .80:
            current = selected[0][1]
        if not selected or (current and selected[0][1] != current):
            current = None
        if current not in positions:
            current = None
        return current, positions, title_scores
