"""Explicit evidence-backed review, consumed by the normal recognition pipeline."""
import json
import re
import time
from pathlib import Path

from ournotes.common.files import atomic_json, file_hash
from ournotes.common.secrets import SecretStore
from ournotes.runtime.store import RuntimeStore


def _inside(root, value, *, boundary=None):
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_relative_to(boundary or root):
        raise ValueError('证据路径越界。')
    return path


def _stage(db, draw, audit):
    draw['status'] = 'captured'
    if audit not in draw.setdefault('reviews', []):
        draw['reviews'].append(audit)
    db.save_draw(draw)
    return {'account_id': draw['account_id'], 'draw_id': draw['id'], 'status': 'awaiting_normal_completion'}


def review(root, *, account_id, draw_id, screenshot_sha256, cards, reason, reviewer):
    root = Path(root).resolve()
    if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value)
           for value in (account_id, draw_id)):
        raise ValueError('账号或交易ID无效。')
    if not isinstance(screenshot_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', screenshot_sha256):
        raise ValueError('原图哈希无效。')
    if (not isinstance(reason, str) or not isinstance(reviewer, str)
            or not reason.strip() or not reviewer.strip()):
        raise ValueError('复核必须注明依据与复核者。')
    if not (root / 'state/runtime.sqlite3').is_file():
        raise ValueError('运行库不存在，不能创建复核记录。')
    with SecretStore(root / 'state').lock('runner'):
        db = RuntimeStore(root)
        try:
            account = db.account(account_id)
            if account and account.get('evidence_pruned_at'):
                from ournotes.runtime.storage import EVIDENCE_PRUNED
                raise ValueError(EVIDENCE_PRUNED)
            if account is None or account['status'] != 'running' or account.get('id') != account_id:
                raise ValueError('仅能复核仍在途账号的未决结果。')
            run = db.get_run(account['run_id'])
            if run and run.get('storage_retention', {}).get('pruned_at'):
                from ournotes.runtime.storage import EVIDENCE_PRUNED
                raise ValueError(EVIDENCE_PRUNED)
            if (not run or run['status'] != 'paused' or run.get('active_account') != account_id
                    or run.get('id') != account['run_id']
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', run['id'])):
                raise ValueError('复核前必须暂停所属运行。')
            draw = next((d for d in db.draws(account_id) if d['id'] == draw_id), None)
            if draw is None or draw['status'] not in ('captured', 'needs_review'):
                raise ValueError('只能复核已有截图的待识别交易，不可补造已提交交易。')
            if draw.get('account_id') != account_id:
                raise ValueError('交易不属于该账号。')
            source = _inside(root, draw['screenshot']['path'])
            if draw['screenshot']['sha256'] != screenshot_sha256 or file_hash(source) != screenshot_sha256:
                raise ValueError('原图哈希或证据路径不匹配。')
            if (not isinstance(cards, list) or type(draw['pulls']) is not int
                    or draw['pulls'] not in (1, 10) or len(cards) != draw['pulls']):
                raise ValueError('复核卡数与交易抽数不一致。')
            if not account.get('catalog_snapshot'):
                raise ValueError('账号缺少固定素材快照，不能回退到当前素材。')
            pinned = _inside(root, account['catalog_snapshot'],
                             boundary=(root / 'data/catalog/snapshots').resolve())
            fingerprints = account.get('catalog_fingerprints', {})
            if set(fingerprints) != {'catalog.json', 'pools.json'}:
                raise ValueError('固定素材校验清单不完整。')
            for name, fingerprint in fingerprints.items():
                if file_hash(_inside(pinned, name)) != fingerprint:
                    raise ValueError('固定素材快照已改变。')
            catalog_path = pinned / 'catalog.json'
            catalog = json.loads(catalog_path.read_text('utf-8'))
            definitions = {f"{c['kind']}:{c['id']}": c for c in catalog['cards']}
            slots = []
            for card in cards:
                if (not isinstance(card, dict) or set(card) != {'kind', 'id'}
                        or card['kind'] not in ('member', 'support')
                        or type(card['id']) is not int or card['id'] < 1):
                    raise ValueError('复核卡牌必须为明确的kind与整数id。')
                key = f"{card['kind']}:{card['id']}"
                if key not in definitions:
                    raise ValueError('复核身份不在固定素材目录中。')
                slots.append({'status': 'matched', 'card': definitions[key]})
            cache_path = _inside(root, Path('state/runs') / run['id'] / 'recognition' / (draw_id + '.json'),
                                 boundary=(root / 'state/runs').resolve())
            cached = json.loads(cache_path.read_text('utf-8'))
            if cached['screenshot_sha256'] != screenshot_sha256 or cached['draw_id'] != draw_id:
                raise ValueError('识别缓存不属于该原图和交易。')
            if cached.get('status') != 'needs_review':
                # A crash after the atomic cache write but before SQLite staging
                # must be recoverable without rewriting timings or another audit.
                audits = cached.get('reviews', [])
                expected = {'account_id': account_id, 'draw_id': draw_id, 'screenshot_sha256': screenshot_sha256,
                            'cards': cards, 'reason': reason, 'reviewer': reviewer,
                            'catalog_fingerprints': fingerprints}
                if (cached.get('status') == 'complete' and audits
                        and all(audits[-1].get(key) == value for key, value in expected.items())
                        and cached.get('result', {}).get('slots') == slots
                        and cached['result'].get('catalog_sha256') == fingerprints['catalog.json']):
                    return _stage(db, draw, audits[-1])
                raise ValueError('识别缓存不是待复核结果。')
            previous = cached.get('result', {})
            if previous and previous.get('catalog_sha256') != fingerprints['catalog.json']:
                raise ValueError('识别缓存素材版本不匹配。')
            audit = {'at': time.time(), 'reviewer': reviewer, 'reason': reason, 'screenshot_sha256': screenshot_sha256,
                     'account_id': account_id, 'draw_id': draw_id, 'previous_status': cached['status'],
                     'previous_result': previous, 'catalog_fingerprints': fingerprints, 'cards': cards}
            cached.setdefault('reviews', []).append(audit)
            cached.update(status='complete', result=dict(previous, status='complete', layout=draw['pulls'],
                                                        slots=slots, catalog_sha256=fingerprints['catalog.json']))
            # Original queued/started/finished times and recognition timings stay intact.
            atomic_json(cache_path, cached)
            return _stage(db, draw, audit)
        finally:
            db.close()
