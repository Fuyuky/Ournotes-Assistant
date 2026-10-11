"""Reviewed event-pool cohorts; never infer eligibility from a preset's current name."""
import json
import sqlite3
from contextlib import closing

# hk-tw-mo MasterGachaPrize pickUpType=2, verified against runtime pools.json.
EVENT_PICKUPS = {
    'bdon-gacha-10': frozenset(('member:61', 'member:62', 'support:62', 'support:63')),
    'bdon-gacha-12': frozenset(('member:65', 'member:66', 'support:65', 'support:66')),
}
METHOD = '当期 up 种数按账号保存计划（精简后使用已完成交易的池身份）中的 #10/#12 UP 身份 kind:id 去重，角色卡和支援卡均计入；不适用或计划缺失不计入该项样本。'


def pickup_count(keys, plan):
    pools = {step.get('pool_key') for step in plan or []} & EVENT_PICKUPS.keys()
    if not pools:
        return None
    eligible = set().union(*(EVENT_PICKUPS[pool] for pool in pools))
    return len(set(keys) & eligible)


def saved_plans(root):
    path = root / 'state/runtime.sqlite3'
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        return plans_from_database(db)


def plans_from_database(db):
    plans = {identifier: json.loads(raw).get('plan')
             for identifier, raw in db.execute('SELECT id,data FROM accounts')}
    # Successful retention removes plans but permanently preserves draw pool identities.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='draws'").fetchone():
        fallback = {}
        for identifier, raw in db.execute("SELECT account_id,data FROM draws WHERE status='complete'"):
            fallback.setdefault(identifier, []).append({'pool_key': json.loads(raw).get('pool_key')})
        plans = {identifier: plan or fallback.get(identifier) for identifier, plan in plans.items()}
    return plans
