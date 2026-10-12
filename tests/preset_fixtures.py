"""Explicit saved plan samples used only by offline historical regression tests."""
import json
from pathlib import Path

from ournotes.runtime.presets import expand_preset

_PRESETS = json.loads((Path(__file__).parent / 'fixtures/historical-presets.json').read_text('utf-8'))['presets']
HISTORICAL_PRESETS = tuple(p['name'] for p in _PRESETS)


def historical_plan(name):
    preset = next((p for p in _PRESETS if p['name'] == name), None)
    if preset is None:
        raise ValueError(f'Unknown historical test preset: {name}')
    return expand_preset(preset)
