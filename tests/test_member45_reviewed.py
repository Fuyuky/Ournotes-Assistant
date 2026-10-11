import hashlib

import numpy as np
import pytest
from PIL import Image

from ournotes.paths import ROOT
from ournotes.vision.recognition import CardRecognizer
from ournotes.vision.templates import load_templates

SAMPLES = ROOT / 'assets/reference/runtime-regressions/member-45-reviewed'


@pytest.fixture(scope='module')
def engine():
    return CardRecognizer(ROOT / 'data/catalog/site')


@pytest.mark.parametrize('file,keys', [
    ('failed.png', ['support:36', 'member:13', 'member:21', 'member:23', 'support:51',
                    'member:18', 'support:21', 'member:25', 'member:45', 'support:28']),
    ('independent.png', ['support:31', 'support:21', 'member:25', 'support:3', 'member:6',
                         'member:39', 'support:8', 'support:56', 'support:57', 'member:45']),
])
def test_native_crop_matches_independent_draw_without_changing_other_identities(engine, file, keys):
    with Image.open(SAMPLES / file) as image:
        result = engine.recognize(image)
    assert result['status'] == 'complete'
    assert [s['card']['key'] for s in result['slots']] == keys


def test_crop_and_independent_evidence_hashes_are_pinned():
    spec, crop = next((s, i) for s, i in load_templates('card') if s['key'] == 'member:45')
    source = ROOT / spec['source']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == spec['source_sha256']
    with Image.open(source) as image:
        assert np.array_equal(np.asarray(image.convert('RGB').crop(spec['box'])), np.asarray(crop))
    for independent in spec['verification_samples']:
        assert independent['sha256'] != spec['source_sha256']
        assert hashlib.sha256((ROOT / independent['path']).read_bytes()).hexdigest() == independent['sha256']
