"""Resolve a saved conditional plan only from complete same-account draw results."""
from copy import deepcopy


def validate_strategy(plan, catalog):
    def supported_pool(key):
        pool = catalog.get(key)
        if not pool:
            raise ValueError(f'计划卡池不在固定目录中：{key}')
        if pool.get('unsupported_card_keys'):
            raise ValueError(f'计划卡池包含尚不支持识别的卡牌：{key}')
        return pool

    for entry in plan:
        pool = supported_pool(entry['pool_key'])
        if not entry.get('until_owned'):
            continue
        if not pool or set(entry['until_owned']) != set(pool.get('pickup_card_keys', [])):
            raise ValueError('预设的UP目标与固定BDON卡池不符，请核查后更新预设。')
        supported_pool(entry['fallback_pool_key'])
    conditional = [e for e in plan if e.get('until_owned')]
    if conditional:
        rule = conditional[0]
        if any(e['until_owned'] != rule['until_owned'] or e['pool_key'] != rule['pool_key']
               or e['fallback_pool_key'] != rule['fallback_pool_key'] for e in conditional):
            raise ValueError('同一预设仅支持一组一致的集齐转池规则。')


def redirect_after_pickups(plan, draws, account_id):
    if not any(e.get('until_owned') for e in plan[len(draws):]):
        return None
    if not draws or any(d['status'] != 'complete' for d in draws):
        return None
    if any(d['account_id'] != account_id or d['ordinal'] != i for i, d in enumerate(draws)):
        raise ValueError('转池依据的账号或交易顺序不符。')
    owned = set()
    for draw in draws:
        slots = draw.get('result', {}).get('slots', [])
        if len(slots) != draw['pulls'] or any(s['status'] != 'matched' for s in slots):
            raise ValueError('转池依据含未知或缺失的抽卡结果。')
        owned.update(s['card']['key'] for s in slots)
    remaining = deepcopy(plan[len(draws):])
    conditional = [e for e in remaining if e.get('until_owned')]
    if not conditional:
        return None
    rule = conditional[0]
    if any(e.get('until_owned') != rule['until_owned'] or e['pool_key'] != rule['pool_key']
           or e['fallback_pool_key'] != rule['fallback_pool_key'] for e in conditional):
        raise ValueError('保存的转池规则不一致。')
    if not set(rule['until_owned']).issubset(owned):
        return None
    # Consume the standard tickets before redirected star batches: the game
    # automatically spends tickets when available and must never surprise the payer.
    fixed = [e for e in remaining if not e.get('until_owned')]
    redirected = [{'pool_key': e['fallback_pool_key'], 'resource': e['resource'], 'pulls': e['pulls']}
                  for e in conditional]
    updated = deepcopy(plan[:len(draws)]) + fixed + redirected
    if sum(e['pulls'] for e in updated) != sum(e['pulls'] for e in plan):
        raise ValueError('转池不得改变总抽数。')
    return updated, {'after_ordinal': len(draws)-1, 'targets': rule['until_owned'],
                     'redirected_pulls': sum(e['pulls'] for e in redirected),
                     'source_pool': rule['pool_key'], 'target_pool': rule['fallback_pool_key']}
