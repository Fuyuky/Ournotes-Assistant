"""Incident pixels are reviewed assets; source-frame agreement is not validation."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from ournotes.ui.observer import Observer
from ournotes.vision.recognition import CardRecognizer, RecognitionError, ResultLayout, artwork_box
from ournotes.vision.templates import load_templates

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'assets/reference/runtime-regressions/settlement-top-pressed'
GOLDEN = ROOT / 'assets/reference/gacha-workflow/results'


@pytest.mark.parametrize('filename', ['failed.png', 'independent-frame.png'])
def test_real_pressed_top_is_foreground_without_ocr(filename):
    image = Image.open(FIXTURES / filename).convert('RGB')
    observer = Observer(allow_fallback=False)
    observed = observer.read(image, phase='draws', last_action='招募TOP')
    assert observed.scene == 'draw_result' and observed.fast and observer.fallback_ocr is None
    assert observed.result_layout['top_state'] == 'pressed'
    assert observed.result_layout['top_template_source']['source_sha256']
    assert len(observer.layout.inspect(image, foreground=True)[0]) == 10
    assert observer.guard(observed, image, '招募TOP', observed.find('招募TOP').center)


@pytest.mark.parametrize('problem', ['dimmed', 'missing_title', 'wrong_button', 'partial_button'])
def test_failure_template_does_not_accept_hidden_or_unrelated_controls(problem):
    image = Image.open(FIXTURES / 'independent-frame.png').convert('RGB')
    if problem == 'dimmed':
        image = Image.fromarray((np.asarray(image) * .65).astype(np.uint8))
    elif problem == 'missing_title':
        ImageDraw.Draw(image).rectangle((726, 64, 876, 105), fill='black')
    elif problem == 'wrong_button':
        image.paste(image.crop((1265, 790, 1580, 863)).resize((285, 69)), (943, 792))
    else:
        ImageDraw.Draw(image).rectangle((1020, 800, 1110, 850), fill='black')
    with pytest.raises(RecognitionError):
        ResultLayout().inspect(image, foreground=True)


def reviewed_card(folder):
    catalog = json.loads((ROOT / 'data/catalog/site/catalog.json').read_text('utf-8'))
    card = next(c for c in catalog['cards'] if c['key'] == 'support:58')
    source = GOLDEN / '01-mygo-10.png'
    box = artwork_box(496)
    Image.open(source).crop(box).save(folder / 'card.png')
    entry = {'id': 'reviewed-support-58', 'kind': 'card', 'key': card['key'],
             'file': 'card.png', 'sha256': hashlib.sha256((folder / 'card.png').read_bytes()).hexdigest(),
             'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
             'source_size': [1600, 900], 'box': list(box), 'catalog_card_sha256': card['sha256'],
             'verified': True, 'verification': 'Existing manually verified golden labels.'}
    save_manifest(folder, [entry])
    return entry


def save_manifest(folder, entries):
    (folder / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'templates': entries}), 'utf-8')


def test_reviewed_card_replaces_reference_and_matches_a_different_draw(tmp_path):
    reviewed_card(tmp_path)
    engine = CardRecognizer(ROOT / 'data/catalog/site', template_directory=tmp_path)
    reference = next(r for r in engine.references if r[0]['key'] == 'support:58')
    assert reference[1].shape == (225, 130)
    image = Image.open(GOLDEN / '02-ave-mujica-10.png').crop(artwork_box(804))
    result = engine.recognize_artwork(image)
    assert result['status'] == 'matched' and result['card']['key'] == 'support:58'
    assert result['template_source']['id'] == 'reviewed-support-58'


def test_changed_catalog_does_not_reuse_an_old_identity_template(tmp_path):
    entry = reviewed_card(tmp_path)
    entry['catalog_card_sha256'] = '0' * 64
    save_manifest(tmp_path, [entry])
    engine = CardRecognizer(ROOT / 'data/catalog/site', template_directory=tmp_path)
    assert 'support:58' not in engine.reference_sources


def test_excluded_identity_is_not_added_back_by_a_template(tmp_path):
    reviewed_card(tmp_path)
    engine = CardRecognizer(ROOT / 'data/catalog/site', template_directory=tmp_path,
                            excluded_keys=frozenset({'support:58'}))
    assert 'support:58' not in engine.cards and 'support:58' not in engine.reference_sources


@pytest.mark.parametrize('problem', ['unverified', 'duplicate', 'hash', 'path', 'dimensions'])
def test_unreviewed_or_corrupt_templates_are_rejected(tmp_path, problem):
    entry = reviewed_card(tmp_path)
    if problem == 'unverified':
        entry['verified'] = False
    elif problem == 'hash':
        entry['sha256'] = '0' * 64
    elif problem == 'path':
        entry['file'] = '../card.png'
    elif problem == 'dimensions':
        entry['box'][2] -= 1
    save_manifest(tmp_path, [entry, entry] if problem == 'duplicate' else [entry])
    with pytest.raises(ValueError):
        load_templates('card', tmp_path)
