from ournotes.reports.pickups import EVENT_PICKUPS, pickup_count


def test_pool_identity_and_distinct_card_kinds():
    cards = ['member:65', 'member:65', 'support:65', 'member:61', 'support:62']
    assert pickup_count(cards, [{'pool_key': 'bdon-gacha-12'}]) == 2
    assert pickup_count(cards, [{'pool_key': 'bdon-gacha-10'}]) == 2
    assert pickup_count([], [{'pool_key': 'bdon-gacha-12'}]) == 0
    assert pickup_count(cards, [{'pool_key': 'bdon-gacha-11'}]) is None
    assert pickup_count(cards, None) is None
    assert all(len(keys) == 4 for keys in EVENT_PICKUPS.values())


def test_multiple_eligible_pools_union_and_duplicate_plan_steps():
    plan = [{'pool_key': key} for key in ['bdon-gacha-10', 'bdon-gacha-12', 'bdon-gacha-12']]
    assert pickup_count(['member:61', 'member:65', 'support:65'], plan) == 3


def test_compacted_accounts_use_completed_draws_not_current_preset(tmp_path):
    import sqlite3

    from ournotes.reports.pickups import saved_plans

    (tmp_path / 'state').mkdir()
    with sqlite3.connect(tmp_path / 'state/runtime.sqlite3') as db:
        db.executescript('CREATE TABLE accounts(id TEXT,data TEXT); CREATE TABLE draws(account_id TEXT,status TEXT,data TEXT);')
        db.execute('INSERT INTO accounts VALUES (?,?)', ('a', '{}'))
        db.executemany('INSERT INTO draws VALUES (?,?,?)', [
            ('a', 'complete', '{"pool_key":"bdon-gacha-12"}'),
            ('a', 'skipped', '{"pool_key":"bdon-gacha-10"}')])
    assert pickup_count(['member:61', 'support:65'], saved_plans(tmp_path)['a']) == 1
