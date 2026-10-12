from copy import deepcopy

import pytest
from pydantic import ValidationError

from ournotes.cloud.models import PresetSnapshot, Result


def snapshot():
    return {'key': 'historical-preset', 'version': 7, 'plan': [
        {'pool_key': 'bdon-gacha-10', 'resource': 'stars', 'pulls': 10}]}


@pytest.mark.parametrize('fields', [
    {'until_owned': ['member:61']},
    {'fallback_pool_key': 'our-notes-standard'},
    {'until_owned': ['member:61'], 'fallback_pool_key': 'our-notes-standard'},
])
def test_new_preset_snapshots_reject_retired_conditional_fields(fields):
    value = snapshot()
    value['plan'][0].update(fields)
    with pytest.raises(ValidationError, match='Invalid preset plan snapshot'):
        PresetSnapshot.model_validate(value)


def test_result_preserves_historical_snapshot_as_opaque_metadata():
    value = snapshot()
    value['plan'][0].update(until_owned=['member:61'], fallback_pool_key='our-notes-standard')
    payload = {
        'schema_version': 1, 'account_id': 'account', 'index': 100,
        'email': 'synthetic@example.invalid', 'cards': [],
        'totals': {'member': {'SSR': 0, 'SR': 0, 'R': 10},
                   'support': {'SSR': 0, 'SR': 0, 'R': 0}},
        'pull_count': 10, 'used': False, 'preset_snapshot': value,
    }
    original = deepcopy(payload)
    result = Result.model_validate(payload)
    assert payload == original
    assert result.model_dump()['preset_snapshot'] == value
    assert Result.model_validate_json(result.model_dump_json()).model_dump() == result.model_dump()


def test_historical_result_snapshot_still_rejects_unknown_fields():
    value = snapshot()
    value['plan'][0]['unknown'] = True
    from ournotes.cloud.models import SavedPresetSnapshot

    with pytest.raises(ValidationError, match='Invalid preset plan snapshot'):
        SavedPresetSnapshot.model_validate(value)
