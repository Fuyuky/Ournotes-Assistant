"""Pure preset loading and plan expansion shared by the editor and runner."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ournotes.paths import DATA_ROOT, ROOT


def preset_source(root=None):
    local = Path(root or DATA_ROOT) / 'config/presets.json'
    return local if local.is_file() else ROOT / 'config/presets.json'


def read_presets(root=None):
    return preset_document(root)[0]


def validate_plan(plan, catalog):
    """Check saved pool identities without changing the plan or its metadata."""
    for entry in plan:
        key = entry['pool_key']
        pool = catalog.get(key)
        if not pool:
            raise ValueError(f'计划卡池不在固定目录中：{key}')
        if pool.get('unsupported_card_keys'):
            raise ValueError(f'计划卡池包含尚不支持识别的卡牌：{key}')


def preset_document(root=None):
    try:
        raw = preset_source(root).read_bytes()
        data = json.loads(raw)
        names = [p['name'] for p in data['presets']]
        if not names or len(names) != len(set(names)):
            raise ValueError('预设名称为空或重复。')
        return data, hashlib.sha256(raw).hexdigest()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('抽卡预设配置无法读取，请检查 config/presets.json。') from error


def canonical_entry(entry):
    if not isinstance(entry, dict) or set(entry) - {
            'pool_key', 'resource', 'pulls', 'batches'}:
        raise ValueError('预设项目包含未知字段。')
    pulls = entry.get('pulls')
    if type(pulls) is not int or not 1 <= pulls <= 1000:
        raise ValueError('每项抽数须为 1～1000 的整数。')
    key = entry.get('pool_key')
    if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9-]{1,100}', key):
        raise ValueError('预设卡池标识无效。')
    if entry.get('resource') not in ('stars', 'ticket'):
        raise ValueError('预设资源类型无效。')
    batches = [10] * (pulls // 10) + [1] * (pulls % 10)
    if 'batches' in entry and (entry['batches'] != batches
                              or any(type(n) is not int for n in entry['batches'])):
        raise ValueError('预设抽数或十连分批规则无效。')
    result = {k: entry[k] for k in ('pool_key', 'pulls', 'resource')}
    result['batches'] = batches
    return result


def canonical_preset(preset):
    name = preset.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or name != name.strip():
        raise ValueError('预设名称无效。')
    entries = preset.get('entries')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        raise ValueError('预设须包含 1～64 个项目。')
    description = preset.get('description', '')
    if not isinstance(description, str) or len(description) > 500:
        raise ValueError('预设说明不能超过 500 字。')
    result = {'name': name, 'description': description, 'entries': [canonical_entry(e) for e in entries]}
    result['total_pulls'] = sum(e['pulls'] for e in result['entries'])
    result['transactions'] = sum(len(e['batches']) for e in result['entries'])
    return result


def expand_preset(preset):
    canonical = canonical_preset(preset)
    if (preset.get('total_pulls') != canonical['total_pulls']
            or preset.get('transactions') != canonical['transactions']):
        raise ValueError('预设总抽数或交易数无效。')
    return [{**{k: e[k] for k in ('pool_key', 'resource')}, 'pulls': n}
            for e in canonical['entries'] for n in e['batches']]


def make_preset_plan(name, root=None):
    chosen = next((p for p in read_presets(root)['presets'] if p['name'] == name), None)
    if chosen is None:
        raise ValueError('未知抽卡预设。')
    return expand_preset(chosen)
