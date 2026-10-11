import copy

import pytest

from ournotes.accounts.archive import proven_count
from ournotes.runtime.draw_strategy import redirect_after_pickups, validate_strategy
from ournotes.runtime.pools import AVE, MEMBER, MYGO, SSR, STANDARD, PoolCatalog, make_plan

ADAPTIVE = '梦限大四UP转常驻 60 抽'
TARGETS = ['member:61', 'member:62', 'support:62', 'support:63']


def completed(ordinal, keys, status='complete'):
    return {'id': f'draw-{ordinal}', 'account_id': 'account', 'ordinal': ordinal,
            'pulls': 10, 'status': status, 'pool_key': 'bdon-gacha-10', 'resource': 'stars',
            'result': {'slots': [{'status': 'matched', 'card': {'key': key}}
                                 for key in (keys + ['member:1'] * 10)[:10]]}}


@pytest.mark.parametrize(('name', 'key'), [
    ('主抽 mygo 60 抽', MYGO), ('主抽 mujica 60 抽', AVE), ('主抽梦限大 60 抽', 'bdon-gacha-10'),
    (ADAPTIVE, 'bdon-gacha-10')])
def test_new_presets_are_62_pulls_eight_transactions_with_separate_resources(name, key):
    plan = make_plan(name)
    assert len(plan) == 8 and sum(e['pulls'] for e in plan) == 62
    assert [(e['pool_key'], e['pulls'], e['resource']) for e in plan] == [
        (key, 10, 'stars')] * 4 + [(SSR, 1, 'ticket'), (MEMBER, 1, 'ticket')] + [(STANDARD, 10, 'ticket')] * 2
    validate_strategy(plan, PoolCatalog())


@pytest.mark.parametrize('ten_pulls', [1, 2, 3])
def test_first_complete_four_targets_redirects_only_future_star_batches(ten_pulls):
    plan = make_plan(ADAPTIVE)
    original = copy.deepcopy(plan)
    draws = [completed(i, TARGETS if i == ten_pulls-1 else TARGETS[:3]) for i in range(ten_pulls)]
    updated, audit = redirect_after_pickups(plan, draws, 'account')
    assert plan == original and updated[:ten_pulls] == plan[:ten_pulls]
    assert audit['redirected_pulls'] == (4-ten_pulls)*10
    assert sum(e['pulls'] for e in updated) == 62 and len(updated) == 8
    standard = [e for e in updated if e['pool_key'] == STANDARD]
    assert [e['resource'] for e in standard] == ['ticket', 'ticket'] + ['stars']*(4-ten_pulls)
    assert redirect_after_pickups(updated, draws, 'account') is None  # restart/idempotence


def test_duplicates_unknown_and_pending_do_not_complete_the_collection():
    plan = make_plan(ADAPTIVE)
    assert redirect_after_pickups(plan, [completed(0, TARGETS[:3]*3)], 'account') is None
    for status in ('captured', 'submitted', 'needs_review', 'skipped'):
        assert redirect_after_pickups(plan, [completed(0, TARGETS, status)], 'account') is None
    d = completed(0, TARGETS)
    d['result']['slots'][0]['status'] = 'unknown'
    with pytest.raises(ValueError, match='未知'):
        redirect_after_pickups(plan, [d], 'account')
    with pytest.raises(ValueError, match='账号'):
        redirect_after_pickups(plan, [completed(0, TARGETS)], 'wrong')


def test_hard_cap_and_changed_pickup_catalog_fail_closed():
    plan = make_plan(ADAPTIVE)
    assert redirect_after_pickups(plan, [completed(i, TARGETS[:3]) for i in range(4)], 'account') is None
    catalog = PoolCatalog()
    catalog.get('bdon-gacha-10')['pickup_card_keys'] = TARGETS[:3]
    with pytest.raises(ValueError, match='UP目标'):
        validate_strategy(plan, catalog)


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
