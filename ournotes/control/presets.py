"""Shared cloud presets with catalog validation and read-only active plans."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from copy import deepcopy

from ournotes.control.query import read_json
from ournotes.runtime.draw_strategy import validate_strategy
from ournotes.runtime.presets import canonical_preset, expand_preset


class PresetCatalog:
    def __init__(self, root):
        from ournotes.catalogs.runtime import current_catalog
        directory = current_catalog(root / 'data/catalog')
        self.fingerprint = hashlib.sha256((directory / 'pools.json').read_bytes()).hexdigest()
        self.pools = json.loads((directory / 'pools.json').read_text('utf-8'))['pools']
        cards = json.loads((directory / 'catalog.json').read_text('utf-8'))['cards']
        self.cards = {c['key']: {k: c[k] for k in ('key', 'kind', 'name', 'title', 'rarity') if k in c} for c in cards}

    def get(self, key):
        return next((p for p in self.pools if key in (p['key'], f"bdon-gacha-{p['gacha_id']}")), None)

    @staticmethod
    def prices(pool, pulls):
        return {p['price'] for p in pool['products'] if p['itemType'] == 12 and p['drawCount'] == pulls}

    def validate(self, plan):
        validate_strategy(plan, self)
        for e in plan:
            keys = [e['pool_key']] + ([e['fallback_pool_key']] if e.get('fallback_pool_key') else [])
            for key in keys:
                pool = self.get(key)
                if e['resource'] == 'stars':
                    if len(self.prices(pool, e['pulls'])) != 1:
                        raise ValueError(f"卡池不支持该抽数的免费星钻招募：{pool['name']}")
                elif (not pool.get('ticket_name') or not any(
                        p['drawCount'] == e['pulls'] and p['itemType'] in (2, 12) for p in pool['products'])):
                    raise ValueError(f"卡池不支持该抽数的招募券：{pool['name']}")

    def public(self):
        pools = []
        for p in self.pools:
            resources = ['stars'] if self.prices(p, 1) or self.prices(p, 10) else []
            if p.get('ticket_name'):
                resources.append('ticket')
            pools.append({**{k: p.get(k) for k in ('key', 'name', 'gacha_id', 'ticket_name', 'pickup_card_keys')},
                          'resources': resources, 'supported': not p.get('unsupported_card_keys'),
                          'star_prices': {str(n): next(iter(prices)) for n in (1, 10)
                                          if len(prices := self.prices(p, n)) == 1}})
        return pools


class PresetEditor:
    def __init__(self, launcher):
        self.launcher, self.root = launcher, launcher.root

    def saved_plans(self):
        snapshot = read_json(self.root / 'state/runner-status.json', {})
        result = {'batch_id': None, 'batch_status': None, 'plans': [], 'error': ''}
        path = self.root / 'state/runtime.sqlite3'
        if not path.exists():
            return result
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('BEGIN')
                row = db.execute('SELECT data FROM batches ORDER BY created DESC LIMIT 1').fetchone()
                if not row:
                    return result
                batch = json.loads(row[0])
                result.update(batch_id=batch['id'], batch_status=batch['status'],
                              snapshot_matches=snapshot.get('batch_id') == batch['id'])
                columns = {r[1] for r in db.execute('PRAGMA table_info(accounts)')}
                if 'data' not in columns:
                    return result
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for lane in batch['lanes']:
                    run = json.loads(db.execute('SELECT data FROM runs WHERE id=?', (lane['run_id'],)).fetchone()[0])
                    row = db.execute('SELECT data FROM accounts WHERE id=?', (run.get('active_account'),)).fetchone()
                    if not row:
                        continue
                    account = json.loads(row[0])
                    if account.get('run_id') != run['id'] or account.get('batch_id') != batch['id']:
                        raise ValueError('保存账号的归属不一致。')
                    plan = [{k: e[k] for k in ('pool_key', 'resource', 'pulls', 'until_owned', 'fallback_pool_key') if k in e}
                            for e in account.get('plan', [])]
                    draws = [{'ordinal': r[0], 'status': r[1]} for r in db.execute(
                        'SELECT ordinal,status FROM draws WHERE account_id=? ORDER BY ordinal', (account['id'],))] if 'draws' in tables else []
                    result['plans'].append({'instance': lane['instance'], 'preset': account.get('preset', run['preset']),
                                                'index': account.get('alias_index'), 'status': run['status'], 'phase': account['phase'],
                                                'plan': plan, 'draws': draws})
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            result.update(plans=[], error='当前账号计划暂时不可读；共享预设仍用于后续分配。')
        return result

    def read(self, *, refresh=True):
        data = self.launcher.shared_presets(refresh=refresh)
        version = data['version']
        saved = self.saved_plans()
        reason = self.launcher.preset_error
        try:
            catalog = PresetCatalog(self.root)
            pools, cards = catalog.public(), catalog.cards
            catalog_error = ''
        except (OSError, ValueError, KeyError, TypeError):
            catalog, pools, cards = None, [], {}
            catalog_error = '运行卡池目录不可读；可以查看预设，修复目录后才能保存。'
            reason = reason or catalog_error
        presets = []
        for preset in data['presets']:
            errors = []
            plan = []
            try:
                plan = expand_preset(preset)
                if catalog:
                    catalog.validate(plan)
            except ValueError as error:
                errors.append(str(error))
            presets.append({**deepcopy(preset), 'validation_errors': errors, 'plan': plan})
        for item in saved['plans']:
            current = next((p['plan'] for p in presets if p['name'] == item['preset']), None)
            item['matches_preset'] = item['plan'] == current if current is not None else None
        return {'version': version, 'presets': presets, 'pools': pools, 'cards': cards,
                    'editable': not reason, 'blocked_reason': reason, 'catalog_error': catalog_error,
                    'saved': saved, 'observed_at': time.time()}

    def save(self, value):
        from ournotes.integrations.cloud_client import CloudClient
        with self.launcher.action_lock:
            current = self.read()
            if not current['editable']:
                raise ValueError(current['blocked_reason'])
            if value['expected_version'] != current['version']:
                raise ValueError('预设已被其他页面修改；请刷新并核对后再保存。')
            previous = next((p for p in current['presets'] if
                             (p.get('key') == value['key'] if value.get('key') else p['name'] == value['name'])), None)
            creating = value.get('operation', 'update') == 'create'
            if creating and any(p['name'] == value['name'] for p in current['presets']):
                raise ValueError('预设名称已存在，请使用其他名称。')
            if not creating and previous is None:
                raise ValueError('预设不存在，请刷新页面。')
            client = CloudClient.from_root(self.root)
            if value.get('operation') == 'delete':
                snapshot = client.delete_preset(previous['key'], current['version'])
            else:
                chosen = canonical_preset(value)
                catalog = PresetCatalog(self.root)
                catalog.validate(expand_preset(chosen))
                key = 'preset-' + uuid.uuid4().hex if creating else previous['key']
                snapshot = client.save_preset(key, chosen, current['version'])
            self.launcher._preset_snapshot = {'version': snapshot['version'],
                                             'presets': [{**preset, 'key': key}
                                                         for key, preset in snapshot['presets'].items()]}
            self.launcher._preset_checked_at = time.monotonic()
            return {**self.read(refresh=False), 'message': '共享预设已更新；已分配账号继续原计划。'}
