"""Regression fixtures were visually compared with the downloaded source art.

Golden labels are independent of runtime matcher output. Integration samples
are the user's preserved 2026-09-27 captures, not a lookup table used by code.
"""
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageEnhance

from ournotes.vision.recognition import (
    CardRecognizer,
    RecognitionError,
    artwork_box,
    normalize_screen,
    recognize_files,
    summarize,
)

PROJECT = Path(__file__).resolve().parents[1]
RUN = PROJECT / "assets/reference/gacha-workflow"
CATALOG = PROJECT / "data/catalog/site"
GOLDEN = {
    "01-mygo-10.png": ["support:55", "member:22", "member:55", "support:58", "support:12",
                       "member:18", "support:16", "support:39", "support:25", "support:47"],
    "02-ave-mujica-10.png": ["member:14", "member:34", "member:21", "member:35", "support:52",
                             "support:58", "member:15", "member:22", "member:25", "support:44"],
    "03-ssr-guaranteed-1.png": ["support:54"],
    "04-ssr-member-1.png": ["member:54"],
    "05-sr-plus-1.png": ["member:48"],
    "06-standard-10-first.png": ["member:60", "support:53", "member:19", "member:21", "member:6",
                                  "member:50", "support:12", "member:28", "member:12", "support:48"],
    "07-standard-10-second.png": ["support:27", "member:50", "member:25", "support:13", "support:8",
                                   "member:3", "member:11", "support:10", "member:11", "member:38"],
}


@pytest.fixture(scope="module")
def recognizer():
    if not (CATALOG / "catalog.json").is_file() or not (RUN / "results").is_dir():
        pytest.skip("需要cards-fetch生成的素材库和保存的7张实际结算截图")
    return CardRecognizer(CATALOG)


@pytest.fixture(scope="module")
def bdon_recognizer(tmp_path_factory):
    # Exercise the catalog-art fallback independently of reviewed game crops.
    folder = tmp_path_factory.mktemp('bdon-only')
    (folder / 'manifest.json').write_text('{"schema_version":1,"templates":[]}')
    return CardRecognizer(CATALOG, template_directory=folder)


def keys(report):
    assert report["status"] == "complete", report
    return [s["card"]["key"] for s in report["slots"]]


@pytest.fixture(scope="module")
def real_reports(recognizer):
    return {name: recognizer.recognize(Image.open(RUN / "results" / name)) for name in GOLDEN}


def test_all_43_actual_slots(real_reports):
    assert sum(len(v) for v in GOLDEN.values()) == 43
    for name, expected in GOLDEN.items():
        assert keys(real_reports[name]) == expected


def test_actual_new_and_duplicate_and_duplicate_within_same_draw(real_reports):
    a, b, c, d = [keys(real_reports[n]) for n in
                  ("01-mygo-10.png", "02-ave-mujica-10.png",
                   "06-standard-10-first.png", "07-standard-10-second.png")]
    assert a[3] == b[5] == "support:58"  # NEW vs no NEW, same SSR photo
    assert c[5] == d[1] == "member:50"  # NEW vs fragment badge, same SR member
    assert d[6] == d[8] == "member:11"  # two pulls must both count


@pytest.mark.parametrize("name", GOLDEN)
def test_new_fragments_and_rarity_overlays_cannot_change_identity(recognizer, name):
    image = Image.open(RUN / "results" / name).convert("RGB")
    variant = image.copy()
    draw = ImageDraw.Draw(variant)
    xs = [724] if len(GOLDEN[name]) == 1 else [34 + 154 * i for i in range(10)]
    for left in xs:
        # Replace whole overlays, stronger than toggling a small NEW glyph.
        draw.rectangle((left, 165, left + 149, 215), fill="magenta")
        draw.rectangle((left + 5, 460, left + 144, 594), fill="white")
        draw.rectangle((left + 5, 600, left + 144, 700), fill="black")
        assert np.array_equal(np.asarray(image.crop(artwork_box(left))),
                              np.asarray(variant.crop(artwork_box(left))))
    assert keys(recognizer.recognize(variant)) == GOLDEN[name]


@pytest.mark.parametrize("name", GOLDEN)
@pytest.mark.parametrize("size", [(1280, 720), (960, 540)])
def test_resizing_and_light_change(recognizer, name, size):
    image = Image.open(RUN / "results" / name).convert("RGB").resize(size, Image.Resampling.LANCZOS)
    image = ImageEnhance.Brightness(image).enhance(.88)
    assert keys(recognizer.recognize(image)) == GOLDEN[name]


@pytest.mark.parametrize("name", ["03-mygo-confirm.png", "05-mygo-animation-ready.png",
                                 "11-review-prompt.png", "46-final-resources.png"])
def test_non_result_screen_rejected(recognizer, name):
    with pytest.raises(RecognitionError):
        recognizer.recognize(Image.open(RUN / "screens" / name))


def test_unknown_art_and_ambiguous_catalog_rejected(recognizer):
    noise = Image.fromarray(np.random.default_rng(7).integers(0, 256, (225, 130), dtype=np.uint8))
    assert recognizer.recognize_artwork(noise)["status"] == "unknown"
    assert recognizer.recognize_artwork(Image.new("RGB", (130, 225), "gray"))["status"] == "unknown"
    # Remove the actual card: shared-background support cards must not replace it.
    missing = CardRecognizer(CATALOG, excluded_keys=frozenset({"support:12", "member:54"}))
    for name, left in (("01-mygo-10.png", 650), ("04-ssr-member-1.png", 724)):
        crop = Image.open(RUN / "results" / name).crop(artwork_box(left))
        assert missing.recognize_artwork(crop)["status"] == "unknown"
    # If an index contains two indistinguishable identities, do not invent a winner.
    references = recognizer.references
    member = next(r for r in references if r[0]["key"] == "member:54")
    recognizer.references = references + [(dict(member[0], key="member:ambiguous"), *member[1:])]
    try:
        crop = Image.open(RUN / "results/04-ssr-member-1.png").crop(artwork_box(724))
        assert recognizer.recognize_artwork(crop)["status"] == "unknown"
    finally:
        recognizer.references = references


def test_no_r_report_counts_and_input_deduplication(recognizer, real_reports, tmp_path):
    entries = summarize(list(real_reports.values()))
    assert all(e["rarity"] != "R" for e in entries)
    assert sum(e["count"] for e in entries if e["rarity"] == "SSR") == 9
    assert sum(e["count"] for e in entries if e["rarity"] == "SR") == 12
    assert len(entries) == 19
    assert next(e for e in entries if e["key"] == "member:50")["count"] == 2
    image = RUN / "results/04-ssr-member-1.png"
    with pytest.raises(ValueError, match="重复输入截图"):
        recognize_files(recognizer, [image, image], tmp_path)


def test_wrong_shape_rejected():
    with pytest.raises(RecognitionError):
        normalize_screen(Image.new("RGB", (1000, 1000)))


def test_snapshot_integrity_rejected(tmp_path):
    import json
    data = {"schema_version": 1, "cards": [{"key": "member:1", "image": "missing.webp", "sha256": "invalid"}]}
    (tmp_path / "catalog.json").write_text(json.dumps(data))
    (tmp_path / "missing.webp").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="素材校验失败"):
        CardRecognizer(tmp_path)


def test_narrow_ssr_crop_uses_constrained_geometry(bdon_recognizer):
    recognizer = bdon_recognizer
    # Visually checked against member-57 artwork. The original perspective fit
    # produced 32 inliers but .4946 appearance correlation on this actual frame.
    image = Image.open(PROJECT / "assets/reference/async-pipeline/ssr-member-narrow-crop.png")
    result = recognizer.recognize(image)
    assert keys(result) == ["member:57"]
    best = result["slots"][0]["candidates"][0]
    assert best["geometry"] == "similarity" and best["correlation"] >= .5
    missing = CardRecognizer(CATALOG, excluded_keys=frozenset({"member:57"}))
    assert missing.recognize(image)["slots"][0]["status"] == "unknown"


@pytest.mark.parametrize('fixture', ['ssr-edge-glow.png', 'ssr-edge-glow-new.png'])
def test_ssr_edge_glow_verifies_same_identity_without_weakening_threshold(bdon_recognizer, fixture):
    recognizer = bdon_recognizer
    image = Image.open(PROJECT / 'assets/reference/runtime-regressions' / fixture)
    result = recognizer.recognize(image)
    assert keys(result) == ['member:54']
    slot = result['slots'][0]
    assert slot['edge_inset'] == 16
    assert slot['full_crop_candidates'][0]['key'] == slot['card']['key']
    assert slot['candidates'][0]['correlation'] >= .55
    missing = CardRecognizer(CATALOG, excluded_keys=frozenset({'member:54'}))
    assert missing.recognize(image)['slots'][0]['status'] == 'unknown'


GLOW_CROPS = [
    ('member-57-single.png', 'member:57'),
    ('member-57-ten-a.png', 'member:57'),
    ('member-57-ten-b.png', 'member:57'),
    ('member-54-ten.png', 'member:54'),
]
GLOW_DIR = PROJECT / 'assets/reference/runtime-regressions/artwork-glow'


@pytest.mark.parametrize('filename,expected', GLOW_CROPS)
def test_marginal_glow_requires_two_consistent_windows(bdon_recognizer, filename, expected):
    recognizer = bdon_recognizer
    # Artwork-only crops, visually checked against the full catalog pictures.
    # None passes the full-crop check; both fixed interior windows must pass.
    with Image.open(GLOW_DIR / filename) as crop:
        assert recognizer.recognize_artwork(crop, allow_inner=False)['status'] == 'unknown'
        result = recognizer.recognize_artwork(crop)
    assert result['status'] == 'matched'
    assert result['card']['key'] == expected
    assert .5 <= result['candidates'][0]['correlation'] < .55
    assert result['edge_inset'] == 16
    assert result['full_crop_candidates'][0]['key'] == expected
    cross = result['edge_cross_check']
    assert cross['inset'] == 12
    assert cross['candidates'][0]['key'] == expected
    assert cross['candidates'][0]['plausible']
    assert cross['candidates'][0]['correlation'] >= .5


@pytest.mark.parametrize('filename,expected', GLOW_CROPS)
@pytest.mark.parametrize('problem', ['missing', 'ambiguous'])
def test_glow_does_not_invent_missing_or_ambiguous_identity(recognizer, monkeypatch,
                                                          filename, expected, problem):
    references = recognizer.references
    if problem == 'missing':
        references = [r for r in references if r[0]['key'] != expected]
    else:
        ref = next(r for r in references if r[0]['key'] == expected)
        references = references + [(dict(ref[0], key='member:ambiguous'), *ref[1:])]
    monkeypatch.setattr(recognizer, 'references', references)
    with Image.open(GLOW_DIR / filename) as crop:
        assert recognizer.recognize_artwork(crop)['status'] == 'unknown'


@pytest.mark.parametrize('problem', ['unknown', 'different_identity'])
def test_marginal_glow_rejects_failed_cross_check(bdon_recognizer, monkeypatch, problem):
    recognizer = bdon_recognizer
    original = recognizer.recognize_artwork
    checked = []

    def recognize(artwork, *, allow_inner=True):
        if artwork.width == 106:  # fixed 12-pixel inset
            checked.append(artwork.size)
            assert not allow_inner
            if problem == 'unknown':
                return {'status': 'unknown', 'candidates': []}
            return {'status': 'matched', 'card': {'key': 'member:54'}}
        return original(artwork, allow_inner=allow_inner)

    monkeypatch.setattr(recognizer, 'recognize_artwork', recognize)
    with Image.open(GLOW_DIR / 'member-57-single.png') as crop:
        assert recognizer.recognize_artwork(crop)['status'] == 'unknown'
    assert checked == [(106, 225)]


def test_reviewed_member57_crop_provenance_and_independent_samples(recognizer):
    import hashlib

    from ournotes.vision.templates import load_templates

    spec, crop = next((s, i) for s, i in load_templates('card') if s['key'] == 'member:57')
    source = PROJECT / spec['source']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == spec['source_sha256']
    with Image.open(source) as screen:
        assert np.array_equal(np.asarray(screen.convert('RGB').crop(spec['box'])), np.asarray(crop))
        assert keys(recognizer.recognize(screen)) == [
            'member:14', 'support:4', 'support:14', 'member:36', 'support:4',
            'support:2', 'member:57', 'member:4', 'support:46', 'member:32']
    for sample in spec['verification_samples']:
        path = PROJECT / sample['path']
        assert sample['sha256'] != spec['source_sha256']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sample['sha256']
        with Image.open(path) as image:
            result = recognizer.recognize_artwork(image)
        assert result['status'] == 'matched' and result['card']['key'] == sample['key']


def test_reviewed_member54_crop_provenance_and_independent_samples(recognizer):
    import hashlib

    from ournotes.vision.templates import load_templates

    spec, crop = next((s, i) for s, i in load_templates('card') if s['key'] == 'member:54')
    source = PROJECT / spec['source']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == spec['source_sha256']
    with Image.open(source) as screen:
        assert np.array_equal(np.asarray(screen.convert('RGB').crop(spec['box'])), np.asarray(crop))
        assert keys(recognizer.recognize(screen)) == [
            'support:47', 'support:56', 'member:13', 'support:6', 'member:33',
            'support:36', 'support:41', 'member:5', 'support:10', 'member:54']
    for sample in spec['verification_samples']:
        path = PROJECT / sample['path']
        assert sample['sha256'] != spec['source_sha256']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sample['sha256']
        with Image.open(path) as image:
            if sample['format'] == 'artwork':
                result = recognizer.recognize_artwork(image)
                assert result['status'] == 'matched' and result['card']['key'] == sample['key']
            else:
                assert keys(recognizer.recognize(image)) == [sample['key']]
    missing = CardRecognizer(CATALOG, excluded_keys=frozenset({'member:54'}))
    assert missing.recognize_artwork(crop)['status'] == 'unknown'


def regional_occlusion(left, *, extra_occlusion=False):
    """Localized high-contrast effects on a real crop, without changing its ID.

    The fixed seed simulates dense sparkle texture; the surrounding overexposed
    border and two patches make whole-crop brightness/detail unreliable.
    """
    with Image.open(RUN / 'results/01-mygo-10.png') as screen:
        pixels = np.asarray(screen.convert('RGB').crop(artwork_box(left))).copy()
    rng = np.random.default_rng(123)
    boxes = [(65, 8, 118, 77), (12, 77, 65, 147)]
    if extra_occlusion:
        boxes.append((65, 147, 118, 217))
    for x0, y0, x1, y1 in boxes:
        pixels[y0:y1, x0:x1] = rng.integers(0, 256, (y1-y0, x1-x0, 3), dtype=np.uint8)
    pixels[:8] = pixels[-8:] = 255
    pixels[:, :12] = pixels[:, -12:] = 255
    return Image.fromarray(pixels)


@pytest.mark.parametrize('left,expected', [(496, 'support:58'), (650, 'support:12')])
def test_fixed_regions_recover_local_effects_with_distributed_evidence(recognizer, left, expected):
    image = regional_occlusion(left)
    assert recognizer.recognize_artwork(image, allow_inner=False)['status'] == 'unknown'
    for inset in (12, 16):
        assert recognizer.recognize_artwork(
            image.crop((inset, 0, image.width-inset, image.height)), allow_inner=False
        )['status'] == 'unknown'
    result = recognizer.recognize_artwork(image)
    assert result['status'] == 'matched' and result['card']['key'] == expected
    evidence = result['regional_verification']
    assert evidence['trusted_regions'] / evidence['eligible_regions'] >= .75
    assert evidence['trusted_coverage'] >= .35
    assert evidence['supported_inliers'] >= 14 and evidence['spread'] >= .12
    trusted = [x for x in evidence['regions'] if x['trusted']]
    assert all(x['correlation'] >= .65 and x['coverage'] >= .85 for x in trusted)


@pytest.mark.parametrize('left,expected', [(496, 'support:58'), (650, 'support:12')])
@pytest.mark.parametrize('problem', ['missing', 'ambiguous', 'broad_occlusion'])
def test_regions_reject_missing_ambiguous_and_insufficient_clean_art(
        recognizer, monkeypatch, left, expected, problem):
    refs = recognizer.references
    if problem == 'missing':
        refs = [r for r in refs if r[0]['key'] != expected]
    elif problem == 'ambiguous':
        ref = next(r for r in refs if r[0]['key'] == expected)
        refs = refs + [(dict(ref[0], key='support:ambiguous'), *ref[1:])]
    monkeypatch.setattr(recognizer, 'references', refs)
    image = regional_occlusion(left, extra_occlusion=problem == 'broad_occlusion')
    assert recognizer.recognize_artwork(image)['status'] == 'unknown'
