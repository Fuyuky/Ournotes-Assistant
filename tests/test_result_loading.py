"""Settlement controls and stable placeholders must not imply loaded artwork."""
import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from ournotes.ui.observer import Observer
from ournotes.vision.recognition import ResultLayout, ResultLoading
from ournotes.vision.templates import load_templates

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'assets/reference/runtime-regressions/settlement-loading-2328'
GOLDEN = ROOT / 'assets/reference/gacha-workflow/results'


@pytest.fixture(scope='module')
def layout():
    return ResultLayout()


@pytest.mark.parametrize('filename', ['failed.png', 'independent-pending.png'])
def test_real_failure_rejects_stable_loading_placeholders(layout, filename):
    image = Image.open(FIXTURES / filename).convert('RGB')
    for _ in range(2):
        with pytest.raises(ResultLoading, match='卡面仍在加载'):
            layout.inspect(image, foreground=True)


def test_independent_transaction_confirms_circle_then_loaded_artwork(layout):
    source = FIXTURES / 'failed.png'
    independent = FIXTURES / 'independent-loading.png'
    assert hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(independent.read_bytes()).digest()
    # This is a different transaction's earlier animation frame. Its title is
    # not ready either; test the circle detector directly, independently of it.
    assert layout.loading_slots(Image.open(independent), [34 + 154*i for i in range(10)])
    image = Image.open(FIXTURES / 'independent-ready.png')
    assert not layout.loading_slots(image, [34 + 154*i for i in range(10)])
    assert len(layout.inspect(image, foreground=True)[0]) == 10


@pytest.mark.parametrize('name', [
    '01-mygo-10.png', '02-ave-mujica-10.png', '03-ssr-guaranteed-1.png',
    '04-ssr-member-1.png', '05-sr-plus-1.png', '06-standard-10-first.png',
    '07-standard-10-second.png',
])
def test_loaded_single_and_ten_pull_results_remain_ready(layout, name):
    image = Image.open(GOLDEN / name)
    positions, _ = layout.inspect(image, foreground=True)
    assert len(positions) == (10 if name.startswith(('01-', '02-', '06-', '07-')) else 1)
    assert not layout.loading_slots(image, positions)


@pytest.mark.parametrize('source_slot,target_slot', [(8, 1), (6, 5), (5, 10)])
def test_one_loading_card_blocks_the_whole_result(layout, source_slot, target_slot):
    image = Image.open(FIXTURES / 'independent-ready.png').convert('RGB')
    with Image.open(FIXTURES / 'failed.png') as source:
        left = 34 + 154 * (source_slot - 1)
        image.paste(source.crop((left, 170, left + 150, 724)),
                    (34 + 154 * (target_slot - 1), 170))
    with pytest.raises(ResultLoading):
        layout.inspect(image, foreground=True)
    observed = Observer(allow_fallback=False).read(image, phase='draws')
    assert observed.scene == 'loading' and observed.mode == 'result_loading'
    assert not observed.find('招募TOP')


def test_loading_scene_has_no_result_controls_or_ocr():
    observer = Observer()
    image = Image.open(FIXTURES / 'failed.png')
    for _ in range(3):
        observation = observer.read(image, phase='draws')
        assert observation.scene == 'loading' and observation.fast
        assert observation.mode == 'result_loading' and not observation.texts
    assert observer.fallback_ocr is None
    ready = observer.read(Image.open(FIXTURES / 'independent-ready.png'), phase='draws')
    assert ready.scene == 'draw_result' and ready.find('招募TOP')


def test_native_crops_match_verified_source_pixels_and_hashes():
    for spec, crop in load_templates('result_loading'):
        source = ROOT / spec['source']
        assert hashlib.sha256(source.read_bytes()).hexdigest() == spec['source_sha256']
        with Image.open(source) as image:
            assert np.array_equal(np.asarray(image.crop(spec['box']).convert('RGB')), np.asarray(crop))
        for sample in spec['verification_samples']:
            assert hashlib.sha256((ROOT / sample['path']).read_bytes()).hexdigest() == sample['sha256']


@pytest.mark.parametrize('template_index', [0, 1, 2])
def test_each_circle_style_has_independent_positive_and_negative_evidence(layout, monkeypatch, template_index):
    # Each crop must work beyond the screenshot from which it was cut.
    monkeypatch.setattr(layout, 'loading_templates',
                        layout.loading_templates[template_index*12:(template_index+1)*12])
    positions = [34 + 154*i for i in range(10)]
    assert layout.loading_slots(Image.open(FIXTURES / 'independent-loading.png'), positions)
    assert not layout.loading_slots(Image.open(FIXTURES / 'independent-ready.png'), positions)
