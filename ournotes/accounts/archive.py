"""Small, self-contained final account records (no execution evidence)."""
from __future__ import annotations

import json
import re

FIELDS = ('id', 'kind', 'rarity', 'title', 'name')
FINAL = ('complete', 'partial', 'cancelled')


def compact_account(report):
    if report['status'] not in FINAL:
        raise ValueError('运行中账号不能作为最终存档')
    if report['status'] == 'complete' and report.get('unknown_slots', 0):
        raise ValueError('完整账号不能包含未识别结果')
    merged = {}
    for item in report['summary']:
        if item['rarity'] not in ('SSR', 'SR'):
            continue
        key = (item['kind'], item['id'])
        if item['count'] <= 0 or int(item['count']) != item['count']:
            raise ValueError('卡牌数量必须为正整数')
        card = {k: item[k] for k in FIELDS}
        if key in merged and merged[key]['card'] != card:
            raise ValueError('同一卡牌 ID 的信息不一致')
        entry = merged.setdefault(key, {'card': card, 'count': 0})
        entry['count'] += item['count']
    for kind in ('member', 'support'):
        for rarity in ('SSR', 'SR'):
            actual = sum(e['count'] for e in merged.values()
                         if e['card']['kind'] == kind and e['card']['rarity'] == rarity)
            if actual != report['totals'][kind][rarity]:
                raise ValueError('汇总数量与卡牌明细不一致')
    cards = sorted(merged.values(), key=lambda e: (e['card']['kind'] != 'member',
                   e['card']['rarity'] != 'SSR', e['card']['id']))
    return {'schema_version': 1, 'account_id': report['id'],
            **{k: report[k] for k in ('alias_index', 'alias', 'status', 'totals')},
            'cards': cards, 'used': report.get('used', False),
            'pool_keys': sorted({step['pool_key'] for step in report.get('plan', []) if step.get('pool_key')}
                                | set(report.get('pool_keys', []))),
            **({'preset_snapshot': {'key': report.get('preset_key') or report['preset'], 'version': report.get('preset_version'),
                                     'plan': report['plan']}}
               if report.get('preset') and report.get('plan') else {}),
            **{k: report[k] for k in ('pull_count', 'node_id') if k in report}}


def write_archive(root, report):
    from ournotes.common.files import atomic_json
    from ournotes.common.secrets import SecretStore
    with SecretStore(root / 'state').lock('account-used', wait=True):
        value = compact_account(report)
        ssr = sum(value['totals'][kind]['SSR'] for kind in ('member', 'support'))
        folder = root / 'results/archive/accounts'
        path = folder / f"{value['alias_index']} {ssr} SSR.json"
        # The local archive caches the cloud usage version; runtime rows cannot overwrite it.
        if path.is_file():
            previous = json.loads(path.read_text('utf-8'))
            for field in ('used', 'usage_status', 'listed', 'cloud_version', 'used_pending', 'booking_pending',
                          'rebind_used_pending'):
                if field in previous:
                    value[field] = previous[field]
            if 'pull_count' not in report and 'pull_count' in previous:
                value['pull_count'] = previous['pull_count']
        atomic_json(path, value, compact=True)
        # Publish first, then remove only obsolete names for this exact alias index.
        pattern = re.compile(rf"{value['alias_index']} [0-9]+ SSR\.json")
        for previous in folder.iterdir():
            if previous != path and pattern.fullmatch(previous.name):
                previous.unlink()
        return path


def expand_archive(root, account):
    value = json.loads((root / account['archive']).read_text(encoding='utf-8'))
    summary = [dict(e['card'], key=f"{e['card']['kind']}:{e['card']['id']}", count=e['count'])
               for e in value['cards']]
    return {**account, **value, 'summary': summary, 'draws': [], 'unknown_slots': 0}


def cleanup_formal_runs(db, run_ids):
    """Apply the shared policy after workers exit; both numbering channels use it."""
    if any((run := db.get_run(identifier)) and run['status'] not in ('complete', 'cancelled')
           for identifier in run_ids):
        return []
    from ournotes.runtime.storage import maintain
    result = maintain(db.root, connection=db.db, powershell=True)
    return result.get('expired_runs', [])


def proven_count(account, draws):
    if account['status'] != 'complete' or account.get('skipped') or len(draws) != len(account.get('plan', [])):
        raise ValueError('incomplete_plan')
    if not draws or any(d['status'] != 'complete' or d['account_id'] != account['id']
                        or d['ordinal'] != i or len(d.get('result', {}).get('slots', [])) != d['pulls']
                        or any(s['status'] != 'matched' for s in d['result']['slots']) for i, d in enumerate(draws)):
        raise ValueError('incomplete_results')
    total = sum(d['pulls'] for d in draws)
    if total != sum(e['pulls'] for e in account['plan']) or total <= 0:
        raise ValueError('unsupported_count')
    return total
