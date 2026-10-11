"""Offline artwork recognition for the Chinese 16:9 gacha settlement screen.

Only the unobstructed upper artwork is read. NEW, rarity glyphs, fragment badges,
and screenshot filenames are not inputs to card identification. All rarities are
matched; filtering R belongs to the final report, not to the candidate search.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ournotes.vision.templates import load_templates, provenance

RECOGNIZER_VERSION = 'settlement-reviewed-templates-v2'


class RecognitionError(ValueError):
    """Unsupported screen/layout; no draw should be inferred from this input."""


class ResultLoading(RecognitionError):
    """Settlement controls are visible, but at least one card is still loading."""


def _gray(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"))


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 100 or float(a.std()) < 2 or float(b.std()) < 2:
        return 0.0
    return float(np.corrcoef(a.ravel(), b.ravel())[0, 1])


def _appearance(query, reference, matrix):
    size = query.shape[1], query.shape[0]
    warped = cv2.warpPerspective(reference, matrix, size)
    coverage = cv2.warpPerspective(np.full_like(reference, 255), matrix, size) > 250
    coverage = cv2.erode(coverage.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
    query_detail = query.astype(np.float32) - cv2.GaussianBlur(query.astype(np.float32), (0, 0), 3)
    ref_detail = warped.astype(np.float32) - cv2.GaussianBlur(warped.astype(np.float32), (0, 0), 3)
    score = max(_correlation(query_detail[coverage], ref_detail[coverage]),
                _correlation(query[coverage], warped[coverage]))
    return score, float(coverage.mean())


def _regional_appearance(query, reference, matrix, inlier_points):
    """Corroborate one geometric candidate across a fixed 2 x 3 interior grid.

    A majority of well-covered tiles must agree, with feature support spread
    across the artwork. No sliding windows, best-tile-only score or new identity
    selection is allowed here. Broad/ambiguous occlusion remains unknown.
    """
    height, width = query.shape
    if width < 120 or height < 200:
        return None
    warped = cv2.warpPerspective(reference, matrix, (width, height))
    coverage = cv2.warpPerspective(np.full_like(reference, 255), matrix, (width, height)) > 250
    coverage = cv2.erode(coverage.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
    q_detail = query.astype(np.float32) - cv2.GaussianBlur(query.astype(np.float32), (0, 0), 3)
    r_detail = warped.astype(np.float32) - cv2.GaussianBlur(warped.astype(np.float32), (0, 0), 3)
    xs = np.linspace(12, width - 12, 3, dtype=int)
    ys = np.linspace(8, height - 8, 4, dtype=int)
    regions, trusted, supported = [], [], []
    eligible = 0
    trusted_area = 0
    for row in range(3):
        for col in range(2):
            x0, x1 = int(xs[col]), int(xs[col + 1])
            y0, y1 = int(ys[row]), int(ys[row + 1])
            sl = np.s_[y0:y1, x0:x1]
            valid = coverage[sl]
            score = max(_correlation(q_detail[sl][valid], r_detail[sl][valid]),
                        _correlation(query[sl][valid], warped[sl][valid]))
            points = inlier_points[(inlier_points[:, 0] >= x0 + 3)
                                   & (inlier_points[:, 0] < x1 - 3)
                                   & (inlier_points[:, 1] >= y0 + 3)
                                   & (inlier_points[:, 1] < y1 - 3)]
            usable = float(valid.mean()) >= .85
            eligible += int(usable)
            accepted = usable and score >= .65
            regions.append({'box': [x0, y0, x1, y1], 'correlation': round(score, 4),
                            'coverage': round(float(valid.mean()), 4),
                            'inliers': len(points), 'trusted': accepted})
            if accepted:
                trusted.append((row, col, len(points)))
                trusted_area += int(valid.sum())
                supported.extend(points)
    # Perspective/artwork boundaries can leave part of a tile outside the
    # reference. Such tiles cannot vote either way; still require >= 4 usable
    # tiles, >= 3 agreeing tiles and broad coverage of the actual common art.
    if (eligible < 4 or len(trusted) < 3 or len(trusted) / eligible < .75
            or trusted_area / query.size < .35
            or trusted_area / max(int(coverage.sum()), 1) < .45 or len(supported) < 14):
        return None
    supported_tiles = [(row, col) for row, col, count in trusted if count >= 2]
    if (len(supported_tiles) < 3 or len({r for r, _ in supported_tiles}) < 2
            or len({c for _, c in supported_tiles}) < 2
            or len(supported) / len(inlier_points) < .55):
        return None
    spread = float(cv2.contourArea(cv2.convexHull(np.float32(supported)))) / query.size
    if spread < .12:
        return None
    return {'grid': [2, 3], 'regions': regions, 'eligible_regions': eligible,
            'trusted_regions': len(trusted),
            'trusted_coverage': round(trusted_area / query.size, 4),
            'supported_inliers': len(supported), 'spread': round(spread, 4)}


def normalize_screen(image: Image.Image) -> Image.Image:
    w, h = image.size
    if w < 960 or h < 540 or abs(w / h - 16 / 9) > .015:
        raise RecognitionError("需要完整16:9游戏截图，至少960×540；不支持含模拟器窗口边框的图片。")
    return image.convert("RGB").resize((1600, 900), Image.Resampling.LANCZOS)


def artwork_box(left: int) -> tuple[int, int, int, int]:
    # Crop BEFORE computing descriptors, so features cannot read pixels across
    # a mask boundary into NEW/fragment overlays. Their presence is irrelevant.
    return left + 10, 225, left + 140, 450


class ResultLayout:
    """Cheap settlement geometry; independent of the card catalog/matcher."""
    def __init__(self):
        anchor_dir = Path(__file__).with_name("assets")
        self.anchors = {name: _gray(Image.open(anchor_dir / f"{name}.png"))
                        for name in ("summary-title", "top-left", "top-right")}
        self.buttons = [({'id': side, 'side': side, 'state': 'normal', 'box': [x, 790, x + 315, 863]},
                         self.anchors[side]) for side, x in (('top-left', 928), ('top-right', 1265))]
        for spec, image in load_templates('result_button'):
            if (spec.get('text') != '招募TOP' or spec.get('side') not in ('top-left', 'top-right')
                    or spec.get('state') not in ('normal', 'pressed')
                    or spec['source_size'] != [1600, 900]):
                raise ValueError('结算按钮素材的语义或画面尺寸无效。')
            self.buttons.insert(0, (spec, _gray(image)))
        self.loading_templates = []
        for spec, image in load_templates('result_loading'):
            if spec['source_size'] != [1600, 900] or image.size != (85, 85):
                raise ValueError('结算加载圆圈素材尺寸无效。')
            reference = _gray(image)
            # The highlighted arc rotates while the artwork downloads. Check a
            # bounded set of rotations of the verified circle, never card art.
            for angle in range(0, 360, 30):
                matrix = cv2.getRotationMatrix2D((42, 42), angle, 1)
                rotated = cv2.warpAffine(reference, matrix, (85, 85), borderMode=cv2.BORDER_REFLECT)
                self.loading_templates.append(cv2.resize(rotated, (43, 43), interpolation=cv2.INTER_AREA))

    def loading_slots(self, screen, positions):
        """Detect native loading circles only inside the result card band."""
        # Matching half-size pixels keeps this guard cheaper than a normal
        # observation cycle; the stored templates remain native pixel crops.
        band = cv2.resize(_gray(screen)[330:490], (800, 80), interpolation=cv2.INTER_AREA)
        scores = np.zeros(len(positions))
        for reference in self.loading_templates:
            matches = cv2.matchTemplate(band, reference, cv2.TM_CCOEFF_NORMED)
            for index, left in enumerate(positions):
                window = matches[:, left//2:min(left//2 + 43, matches.shape[1])]
                if window.size:
                    scores[index] = max(scores[index], float(window.max()))
        return [i + 1 for i, score in enumerate(scores) if score >= .85]

    def inspect(self, screen, *, foreground=False):
        gray = _gray(screen)
        title = _correlation(gray[64:105, 726:876], self.anchors["summary-title"])
        candidates = []
        for spec, template in self.buttons:
            x1, y1, x2, y2 = spec['box']
            patch = gray[y1:y2, x1:x2]
            score = _correlation(patch, template)
            difference = float(np.abs(patch.astype(float) - template).mean())
            if score >= .8 and (not foreground or difference <= 8):
                candidates.append((score, spec))
        if title < .85 or not candidates:
            raise RecognitionError("未检测到稳定的招募结算标题和招募TOP按钮。")
        score, spec = max(candidates, key=lambda item: item[0])
        positions = [34 + 154 * i for i in range(10)]
        texture = [float(cv2.Laplacian(_gray(screen.crop(artwork_box(x))), cv2.CV_32F).var())
                   for x in positions]
        if all(v > 45 for v in texture):
            slots = positions
        elif all(texture[i] < 25 for i in (0, 1, 2, 3, 6, 7, 8, 9)) and min(texture[4:6]) > 45:
            slots = [724]
        else:
            raise RecognitionError("结果槽布局不完整或仍在动画中。")
        if self.loading_slots(screen, slots):
            raise ResultLoading('结算卡面仍在加载；等待加载圆圈消失后再保存原交易结果。')
        return slots, {"title_score": round(title, 4), "top_score": round(score, 4),
                       "top_box": spec['box'], 'top_state': spec['state'],
                       'top_template': spec['id'],
                       **({'top_template_source': provenance(spec)} if 'source_sha256' in spec else {})}


class CardRecognizer:
    def __init__(self, catalog_directory: Path, *, excluded_keys: frozenset[str] = frozenset(),
                 template_directory=None):
        self.directory = Path(catalog_directory)
        catalog_path = self.directory / "catalog.json"
        self.catalog_hash = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("不支持的卡片快照格式。")
        self.cards = {card["key"]: card for card in data["cards"] if card["key"] not in excluded_keys}
        if not self.cards:
            raise ValueError("素材库为空。")
        self.sift = cv2.SIFT_create(contrastThreshold=.018, edgeThreshold=15)
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        self.references = []
        reviewed = {spec['key']: (spec, image) for spec, image in load_templates('card', template_directory)}
        self.reference_sources = {}
        for card in self.cards.values():
            path = (self.directory / card["image"]).resolve()
            if not path.is_relative_to(self.directory.resolve()):
                raise ValueError("素材路径超出快照目录。")
            if hashlib.sha256(path.read_bytes()).hexdigest() != card["sha256"]:
                raise ValueError(f"素材校验失败：{card['key']}")
            with Image.open(path) as image:
                gray = _gray(image)
            if card['key'] in reviewed:
                spec, image = reviewed[card['key']]
                # Same kind:id alone is insufficient when a catalog changes its artwork.
                if spec['catalog_card_sha256'] == card['sha256']:
                    gray = _gray(image)
                    self.reference_sources[card['key']] = provenance(spec)
            points, descriptors = self.sift.detectAndCompute(gray, None)
            if descriptors is None:
                raise ValueError(f"素材缺少可匹配特征：{card['key']}")
            self.references.append((card, gray, points, descriptors))
        self.layout = ResultLayout()
        self.anchors = self.layout.anchors

    def _layout(self, screen: Image.Image) -> tuple[list[int], dict]:
        return self.layout.inspect(screen)

    def recognize_artwork(self, artwork: Image.Image, *, allow_inner=True) -> dict:
        """Match one safe artwork crop. Unknowns retain candidates for review."""
        query = _gray(artwork)
        points, descriptors = self.sift.detectAndCompute(query, None)
        rankings = []
        transforms = {}
        if descriptors is not None and len(points) >= 14:
            for card, reference, ref_points, ref_desc in self.references:
                pairs = self.matcher.knnMatch(descriptors, ref_desc, k=2)
                good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < .75 * pair[1].distance]
                # Multiple SIFT orientations at one feature may target the same
                # reference descriptor. Count each target once.
                used = set()
                unique = []
                for match in sorted(good, key=lambda m: m.distance):
                    if match.trainIdx not in used:
                        unique.append(match)
                        used.add(match.trainIdx)
                if len(unique) < 8:
                    continue
                source = np.float32([ref_points[m.trainIdx].pt for m in unique])
                target = np.float32([points[m.queryIdx].pt for m in unique])
                cv2.setRNGSeed(0)
                matrix, mask = cv2.findHomography(source, target, cv2.RANSAC, 3.0)
                if matrix is None or mask is None or not np.isfinite(matrix).all():
                    continue
                valid = mask.ravel().astype(bool)
                inliers = int(valid.sum())
                if inliers < 8:
                    continue
                spread = float(cv2.contourArea(cv2.convexHull(target[valid]))) / query.size
                # Broad animated glow changes absolute brightness. Compare local
                # detail after removing low-frequency illumination, inside an
                # eroded valid region so the warped image edge cannot dominate.
                photometric, coverage = _appearance(query, reference, matrix)
                transforms[card['key']] = (reference, matrix, target[valid])
                ratio = inliers / len(unique)
                model = "homography"
                if photometric < .5 and inliers >= 14 and ratio >= .55 and spread >= .08:
                    # A narrow crop can overfit perspective despite good matches.
                    # Verify the SAME candidate with the game's scale/translation
                    # geometry, using the same appearance and geometric thresholds.
                    affine, affine_mask = cv2.estimateAffinePartial2D(
                        source, target, method=cv2.RANSAC, ransacReprojThreshold=3.0)
                    if affine is not None and affine_mask is not None and np.isfinite(affine).all():
                        valid_a = affine_mask.ravel().astype(bool)
                        count_a = int(valid_a.sum())
                        spread_a = float(cv2.contourArea(cv2.convexHull(target[valid_a]))) / query.size
                        score_a, coverage_a = _appearance(query, reference, np.vstack((affine, [0, 0, 1])))
                        rotation = abs(float(np.degrees(np.arctan2(affine[1, 0], affine[0, 0]))))
                        if (count_a >= 14 and count_a / len(unique) >= .55 and spread_a >= .08
                                and coverage_a >= .4 and score_a >= .5 and rotation <= 3):
                            inliers, ratio, spread = count_a, count_a / len(unique), spread_a
                            photometric, coverage, model = score_a, coverage_a, "similarity"
                geometric_plausible = inliers >= 14 and ratio >= .55 and spread >= .08 and coverage >= .4
                plausible = geometric_plausible and photometric >= .5
                rankings.append({"key": card["key"], "inliers": inliers,
                                 "matches": len(unique), "inlier_ratio": round(ratio, 4),
                                 "spread": round(spread, 4), "correlation": round(photometric, 4),
                                 "coverage": round(coverage, 4),
                                 "geometric_plausible": geometric_plausible,
                                 "plausible": plausible, "geometry": model})
        # Do not fall back to a weaker identity when the strongest geometric
        # candidate fails appearance verification; that should remain unknown.
        rankings.sort(key=lambda x: (x["inliers"], x["correlation"]), reverse=True)
        best = rankings[0] if rankings else None
        runner = max((x["inliers"] for x in rankings[1:] if x["plausible"]), default=0)
        accepted = best is not None and best["plausible"] and best["inliers"] >= max(runner + 8, runner * 1.65)
        result = {"status": "matched" if accepted else "unknown", "candidates": rankings[:3],
                  "reason": "几何与图像一致性通过" if accepted else "素材不足、遮挡过大或候选不明确，需要复核"}
        if accepted:
            card = self.cards[best["key"]]
            result["card"] = {field: card[field] for field in
                              ("key", "id", "kind", "rarity", "title", "name", "display_name", "source_page")}
        elif (allow_inner and best and not best['plausible'] and best['inliers'] >= 14
              and best['inlier_ratio'] >= .55 and best['spread'] >= .08 and artwork.width >= 100):
            # Animated SSR borders sometimes cover the safe crop's left/right
            # edges. Recheck the same identity on its interior, without lowering
            # any geometric thresholds or resolving an ambiguous full-crop match.
            inset = 16
            inner = self.recognize_artwork(artwork.crop((inset, 0, artwork.width-inset, artwork.height)),
                                           allow_inner=False)
            if inner['status'] == 'matched' and inner['card']['key'] == best['key']:
                verified = inner['candidates'][0]['correlation'] >= .55
                if not verified:
                    # A single marginal interior fit is insufficient. Require a
                    # second, wider fixed window to independently pass normal
                    # geometry, appearance AND winner-margin checks for the same
                    # identity. Never search arbitrary crops until one matches.
                    check_inset = 12
                    check = self.recognize_artwork(
                        artwork.crop((check_inset, 0, artwork.width-check_inset, artwork.height)),
                        allow_inner=False)
                    verified = check['status'] == 'matched' and check['card']['key'] == best['key']
                    if verified:
                        inner['edge_cross_check'] = {
                            'inset': check_inset, 'candidates': check['candidates']}
                if verified:
                    inner.update(reason='边缘光效裁除后，同一候选通过几何与图像一致性校验',
                                 edge_inset=inset, full_crop_candidates=result['candidates'])
                    return inner
                # A failed independent check must not be bypassed by another
                # recovery method, including a conflicting crop identity.
                return result
            if inner['status'] == 'matched':
                return result
            geometric_runner = max((x['inliers'] for x in rankings[1:]
                                    if x['geometric_plausible']), default=0)
            if (best['geometric_plausible'] and best['correlation'] < .5
                    and best['inliers'] >= max(geometric_runner + 8, geometric_runner * 1.65)):
                regions = _regional_appearance(query, *transforms[best['key']])
                if regions:
                    card = self.cards[best['key']]
                    result.update(status='matched', regional_verification=regions,
                                  reason='固定卡面内区多数区域及分布特征共同确认同一候选',
                                  card={field: card[field] for field in
                                        ('key', 'id', 'kind', 'rarity', 'title', 'name',
                                         'display_name', 'source_page')})
        if result['status'] == 'matched' and result['card']['key'] in self.reference_sources:
            result['template_source'] = self.reference_sources[result['card']['key']]
        return result

    def recognize(self, image: Image.Image) -> dict:
        screen = normalize_screen(image)
        positions, scores = self._layout(screen)
        slots = []
        for index, left in enumerate(positions, 1):
            roi = artwork_box(left)
            result = self.recognize_artwork(screen.crop(roi))
            result.update(slot=index, bbox=[left, 170, left + 150, 724], artwork_bbox=list(roi))
            slots.append(result)
        return {"status": "complete" if all(x["status"] == "matched" for x in slots) else "needs_review",
                "layout": len(slots), "normalized_size": [1600, 900], "layout_scores": scores, "slots": slots,
                "recognizer_version": RECOGNIZER_VERSION, "catalog_sha256": self.catalog_hash}


def summarize(reports: list[dict], *, include_r: bool = False) -> list[dict]:
    counts: Counter = Counter()
    cards = {}
    for report in reports:
        for slot in report.get("slots", []):
            if slot["status"] == "matched":
                card = slot["card"]
                cards[card["key"]] = card
                counts[card["key"]] += 1
    entries = [dict(cards[key], count=count) for key, count in counts.items()
               if include_r or cards[key]["rarity"] != "R"]
    return sorted(entries, key=lambda x: (-{"SSR": 4, "SR": 3, "R": 2}[x["rarity"]],
                                         x["kind"] != "member", x["id"]))


def summary_text(entries: list[dict]) -> str:
    return "\n".join(f"{e['rarity']} 【{e['title']}】{e['name']} x{e['count']}" for e in entries)


def recognize_files(recognizer: CardRecognizer, paths: list[Path], output: Path) -> dict:
    """No networking or game input. Duplicate screenshot bytes are rejected."""
    reports, seen = [], set()
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            raise ValueError(f"重复输入截图：{path.name}，已停止以免重复计数。")
        seen.add(digest)
        with Image.open(path) as image:
            try:
                report = recognizer.recognize(image)
            except RecognitionError as error:
                report = {"status": "needs_review", "error": str(error), "slots": []}
        report.update(source=str(path.resolve()), sha256=digest)
        reports.append(report)
    entries = summarize(reports)
    complete = bool(reports) and all(r["status"] == "complete" for r in reports)
    result = {"schema_version": 1, "status": "complete" if complete else "needs_review",
              "catalog_sha256": recognizer.catalog_hash,
              "matched_pulls": sum(s["status"] == "matched" for r in reports for s in r["slots"]),
              "unknown_slots": sum(s["status"] != "matched" for r in reports for s in r["slots"]),
              "screens": reports, "summary_without_r": entries}
    output.mkdir(parents=True, exist_ok=True)
    (output / "recognition.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    heading = "抽卡结果（不含R）" if complete else "部分结果（存在未识别项目，不是完整抽卡结果）"
    (output / "抽卡结果.txt").write_text(heading + "\n\n" + summary_text(entries) + "\n", encoding="utf-8")
    return result
