import pytest

from ournotes.accounts.archive import proven_count
from ournotes.runtime.pools import AVE, MEMBER, MYGO, SSR, STANDARD, PoolCatalog, make_plan
from ournotes.runtime.presets import validate_plan


def completed(ordinal, keys, status='complete'):
    return {'id': f'draw-{ordinal}', 'account_id': 'account', 'ordinal': ordinal,
            'pulls': 10, 'status': status, 'pool_key': 'bdon-gacha-10', 'resource': 'stars',
            'result': {'slots': [{'status': 'matched', 'card': {'key': key}}
                                 for key in (keys + ['member:1'] * 10)[:10]]}}


@pytest.mark.parametrize(('name', 'key'), [
    ('主抽 mygo 60 抽', MYGO), ('主抽 mujica 60 抽', AVE), ('主抽梦限大 60 抽', 'bdon-gacha-10')])
def test_new_presets_are_62_pulls_eight_transactions_with_separate_resources(name, key):
    plan = make_plan(name)
    assert len(plan) == 8 and sum(e['pulls'] for e in plan) == 62
    assert [(e['pool_key'], e['pulls'], e['resource']) for e in plan] == [
        (key, 10, 'stars')] * 4 + [(SSR, 1, 'ticket'), (MEMBER, 1, 'ticket')] + [(STANDARD, 10, 'ticket')] * 2
    validate_plan(plan, PoolCatalog())


def test_backfill_requires_actual_complete_slots_and_plan():
    plan = make_plan('MyGO only')
    account = {'id': 'account', 'status': 'complete', 'plan': plan}
    draws = []
    for i, entry in enumerate(plan):
        draw = completed(i, [])
        draw.update(entry)
        draw['result']['slots'] = draw['result']['slots'][:entry['pulls']]
        draws.append(draw)
    assert proven_count(account, draws) == sum(item['pulls'] for item in account['plan'])
    draws[-1]['status'] = 'needs_review'
    with pytest.raises(ValueError):
        proven_count(account, draws)
