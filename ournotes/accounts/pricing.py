"""Versioned account asking prices; independent of filters and usage flags."""

from __future__ import annotations

from ournotes.reports.pickups import EVENT_PICKUPS

MODEL = "account-price-v3"
CURRENCY = "CNY"
# Frozen from the complete upper-tail <=0.2% cohort, not recalculated on refresh.
EXTREME_THRESHOLDS = {
    "ssr_total": 15,
    "mygo_characters": 5,
    "mujica_characters": 5,
    "unique_characters": 8,
}
DREAM_POOL = "bdon-gacha-10"
DREAM_PICKUPS = frozenset(("member:61", "member:62", "support:62", "support:63"))
TEST_CONSUMABLE_INDICES = frozenset((870,))
UP_VALUES = (10, 10, 15, 20, 25, 30, 35)


def dream_pickups(cards):
    return frozenset(card["key"] for card in cards
                     if card["key"] in DREAM_PICKUPS and card["count"] > 0)


def current_pickup_count(account):
    """Count distinct UPs within saved event pools, never mix two pools into x/4."""
    held = {c['key'] for c in account['cards'] if c['count'] > 0}
    return max((len(held & EVENT_PICKUPS[pool]) for pool in account.get('pool_keys', ())
                if pool in EVENT_PICKUPS), default=0)


def quote(account):
    """Quote validated holdings, counting duplicate SSRs but distinct UP identities."""
    price = {"model": MODEL, "currency": CURRENCY, "status": "pending",
             "amount_cents": None, "reason": None}
    pulls = account.get("pull_count")
    if type(pulls) is not int or pulls <= 0:
        return {**price, "reason": "unverified_pulls"}
    pickups = current_pickup_count(account)
    if pulls == 42 and pickups in (2, 3):
        return {**price, 'status': 'priced', 'amount_cents': {2: 3000, 3: 6000}[pickups]}
    if 'bdon-gacha-12' in account.get('pool_keys', ()):
        pickups = len({c['key'] for c in account['cards'] if c['count'] > 0} &
                      {'member:65', 'member:66', 'support:65', 'support:66'})
        if pickups >= 3:
            return {**price, 'reason': 'millsage_pickups'}
        if pickups:
            return {**price, 'status': 'priced', 'amount_cents': pickups * 1500}
    mygo, mujica = account["mygo_characters"], account["mujica_characters"]
    if (min(mygo, mujica) >= 4
            or any(account[key] >= threshold for key, threshold in EXTREME_THRESHOLDS.items())):
        return {**price, "reason": "extreme_collection"}
    largest = max(mygo, mujica)
    standard = 50 if largest >= 5 else 25 if largest == 4 else 15 if largest == 3 else 10
    if min(mygo, mujica) >= 3:
        standard += 10
    targets = dream_pickups(account["cards"])
    points = sum(2 if key.startswith("member:") else 1 for key in targets)
    base = max(standard, UP_VALUES[points])
    ssr = account["ssr_total"]
    extra = 0 if ssr < 10 else 5 if ssr < 12 else 10 + 5 * (ssr - 12)
    amount = base + extra + (5 if pulls == 43 else 0)
    return {**price, "status": "priced", "amount_cents": amount * 100}


def price_accounts(accounts):
    return tuple({**account, "price": quote(account)} for account in accounts)


def price_sort_key(account, *, ascending=False):
    price = account["price"]
    pending = price["status"] == "pending"
    amount = price["amount_cents"] or 0
    return (not pending, amount if ascending else -amount, account["index"])
