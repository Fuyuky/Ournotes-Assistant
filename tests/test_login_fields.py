import json

from PIL import Image, ImageDraw

from ournotes.paths import REFERENCE
from ournotes.ui.login_fields import focused


def test_native_misdirected_input_has_email_focus_not_code_focus():
    folder = REFERENCE / 'runtime-regressions/login-focus'
    source = Image.open(folder / 'email-focused-borders.png')
    boxes = json.loads((folder / 'provenance.json').read_text('utf-8'))['native_boxes']
    image = Image.new('RGB', (1600, 900), 'white')
    for i, box in enumerate(boxes):
        image.paste(source.crop((0, i*3, 180, i*3+3)), tuple(box[:2]))
    assert focused(image, 'email') and not focused(image, 'code')


def test_focus_requires_both_borders_and_rejects_conflicting_fields():
    image = Image.new('RGB', (1600, 900), 'white')
    draw = ImageDraw.Draw(image)
    assert not focused(image, 'code')
    draw.line((660, 425, 839, 425), fill='#ff4a92')
    assert not focused(image, 'code')
    draw.line((660, 484, 839, 484), fill='#ff4a92')
    assert focused(image, 'code') and not focused(image, 'email')
    for y in (350, 409):
        draw.line((660, y, 839, y), fill='#ff4a92')
    assert not focused(image, 'code') and not focused(image, 'email')
