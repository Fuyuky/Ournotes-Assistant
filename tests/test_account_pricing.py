"""Pricing anchors, frozen review thresholds and deterministic public ordering."""

from itertools import combinations

import pytest

from ournotes.accounts.pricing import DREAM_PICKUPS, price_accounts, price_sort_key, quote


def account(index=1, m=0, a=0, ssr=4, pulls=62, cards=(), **extra):
    return dict(index=index, mygo_characters=m, mujica_characters=a,
                unique_characters=m+a, ssr_total=ssr, pull_count=pulls,
                cards=[{"key": key, "count": count} for key, count in cards], **extra)


@pytest.mark.parametrize("m,a,expected", [(0, 0, 10), (2, 2, 10), (3, 0, 15),
                                        (4, 0, 25), (0, 4, 25), (3, 3, 25),
                                        (4, 3, 35), (3, 4, 35)])
def test_standard_anchors_and_secondary_band(m, a, expected):
    assert quote(account(m=m, a=a))["amount_cents"] == expected*100


def test_every_up_combination_and_duplicates():
    targets = sorted(DREAM_PICKUPS)
    for count in range(5):
        for held in combinations(targets, count):
            points = sum(2 if key.startswith("member:") else 1 for key in held)
            expected = (10, 10, 15, 20, 25, 30, 35)[points]*100
            assert quote(account(cards=[(key, 1) for key in held]))["amount_cents"] == expected
            assert quote(account(cards=[(key, 3) for key in held]))["amount_cents"] == expected
    assert quote(account(cards=[("member:63", 1), ("support:61", 1)]))["amount_cents"] == 1000
    assert quote(account(cards=[("member:61", 0)]))["amount_cents"] == 1000


@pytest.mark.parametrize("ssr,extra", [(9, 0), (10, 5), (11, 5), (12, 10), (13, 15), (14, 20)])
def test_ssr_bonus_and_budget_bonus(ssr, extra):
    long = quote(account(ssr=ssr))
    short = quote(account(ssr=ssr, pulls=43))
    assert long["amount_cents"] == (10+extra)*100
    assert short["amount_cents"] == long["amount_cents"]+500


def test_all_up_is_base_35_not_final_cap_and_max_not_sum():
    held = [(key, 1) for key in DREAM_PICKUPS]
    assert quote(account(cards=held, ssr=9))["amount_cents"] == 3500
    assert quote(account(cards=held, ssr=12))["amount_cents"] == 4500
    assert quote(account(m=4, a=3, cards=held, ssr=12))["amount_cents"] == 4500


@pytest.mark.parametrize("changes,reason", [({"pull_count": None}, "unverified_pulls"),
    ({"pull_count": 0}, "unverified_pulls"), ({"mygo_characters": 4, "mujica_characters": 4}, "extreme_collection"),
    ({"mygo_characters": 5}, "extreme_collection"), ({"mujica_characters": 5}, "extreme_collection"),
    ({"ssr_total": 15}, "extreme_collection"), ({"unique_characters": 8}, "extreme_collection")])
def test_pending_precedence(changes, reason):
    result = quote({**account(), **changes})
    assert result["status"] == "pending" and result["amount_cents"] is None
    assert result["reason"] == reason


def test_frozen_prices_do_not_change_with_inventory_or_used_and_sort_pending_first():
    values = [account(index=3), account(index=2, m=4), account(index=1, pulls=None), account(index=4)]
    before = price_accounts(values)
    assert quote({**values[0], "used": True}) == before[0]["price"]
    assert price_accounts(values+[account(index=5, ssr=25)])[:4] == before
    assert [a["index"] for a in sorted(before, key=price_sort_key)] == [1, 2, 3, 4]
    assert [a["index"] for a in sorted(before, key=lambda a: price_sort_key(a, ascending=True))] == [1, 3, 4, 2]
    assert price_accounts([]) == ()


@pytest.mark.parametrize('held,amount', [(1,1500),(2,3000),(3,6000),(4,None)])
def test_millsage_fixed_prices_follow_saved_pool_and_distinct_pickups(held, amount):
    keys=['member:65','support:65','member:66','support:66'][:held]
    value=account(pulls=42,cards=[(key,3) for key in keys],pool_keys=['bdon-gacha-12'])
    result=quote(value)
    assert result['amount_cents']==amount
    assert result['status']==('priced' if held<4 else 'pending')
    assert result['reason']!= 'unverified_pulls'
    if held<4:
        assert quote({**value,'ssr_total':20,'mygo_characters':5})['amount_cents']==amount


def test_valid_new_pull_count_is_not_marked_unverified():
    assert quote(account(pulls=42))['reason'] is None


@pytest.mark.parametrize('pool', ['bdon-gacha-10', 'bdon-gacha-12'])
@pytest.mark.parametrize('count,amount', [(2, 3000), (3, 6000)])
def test_current_up_fixed_42_pull_price_uses_saved_pool(pool, count, amount):
    from ournotes.reports.pickups import EVENT_PICKUPS
    keys = sorted(EVENT_PICKUPS[pool])[:count]
    value = account(pulls=42, cards=[(key, 4) for key in keys], pool_keys=[pool])
    assert quote(value)['amount_cents'] == amount
    assert quote({**value, 'ssr_total': 20})['amount_cents'] == amount
    other = 'bdon-gacha-12' if pool == 'bdon-gacha-10' else 'bdon-gacha-10'
    from ournotes.accounts.pricing import current_pickup_count
    assert current_pickup_count({**value, 'pool_keys': [other]}) == 0
    assert current_pickup_count({**value, 'pool_keys': []}) == 0


def test_two_event_pools_are_not_combined_into_one_four_card_set():
    from ournotes.accounts.pricing import current_pickup_count
    value = account(pulls=42, cards=[('member:61', 1), ('member:65', 1)],
                    pool_keys=['bdon-gacha-10', 'bdon-gacha-12'])
    assert current_pickup_count(value) == 1
