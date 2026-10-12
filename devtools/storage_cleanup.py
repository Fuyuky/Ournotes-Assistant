"""Explicit, offline cleanup of ended incomplete accounts; never a runtime policy.

Call plan() first. apply_database() requires the exact observed account set and
leaves all numbering counters and complete accounts untouched. File removal is
performed separately from the persisted, reviewed manifest.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ournotes.common.files import atomic_json, file_hash
from ournotes.runtime.storage import TERMINAL, inside


def complete_fingerprint(db):
    digest = hashlib.sha256()
    for sql in (
        "SELECT id,data FROM accounts WHERE status='complete' ORDER BY id",
        ("SELECT d.id,d.data FROM draws d JOIN accounts a ON a.id=d.account_id "
         "WHERE a.status='complete' ORDER BY d.id"),
        "SELECT channel,next_index FROM counters ORDER BY channel",
    ):
        for row in db.execute(sql):
            digest.update(json.dumps(list(row), ensure_ascii=False).encode())
    return digest.hexdigest()


def plan(root, db):
    root = Path(root).resolve()
    runs = {i: json.loads(d) for i, d in db.execute('SELECT id,data FROM runs')}
    batches = {i: json.loads(d) for i, d in db.execute('SELECT id,data FROM batches')}
    if any(b['status'] not in TERMINAL for b in batches.values()):
        raise ValueError('仍有未结束批次，禁止清理。')
    accounts = {i: json.loads(d) for i, d in db.execute('SELECT id,data FROM accounts')}
    rejected = [a for a in accounts.values() if a['status'] != 'complete']
    if any(runs.get(a['run_id'], {}).get('status') not in TERMINAL for a in rejected):
        raise ValueError('异常账号仍属于未结束或未知运行。')
    ids = {a['id'] for a in rejected}
    run_ids = {a['run_id'] for a in rejected}
    batch_ids = {runs[r].get('batch_id') for r in run_ids} - {None}
    files = set()
    protected = {}
    for path in (root / 'results/archive/accounts').glob('*.json'):
        inside(root, path, root / 'results/archive/accounts')
        value = json.loads(path.read_text('utf-8'))
        if value.get('account_id') in ids:
            if value.get('status') == 'complete':
                raise ValueError('异常账号存在成功档案，拒绝删除。')
            files.add(path)
        elif value.get('status') == 'complete':
            protected[path.relative_to(root).as_posix()] = file_hash(path)
        else:
            raise ValueError('异常档案无法关联数据库，需单独核查。')
    for account in rejected:
        path = root / 'results/accounts' / (account['id'] + '.json')
        if path.is_file():
            files.add(path)
    for run_id in run_ids:
        successful = [a for a in accounts.values() if a['run_id'] == run_id and a['status'] == 'complete']
        if any(not a.get('storage_compacted') for a in successful):
            raise ValueError('同一运行仍有未精简的成功账号证据，需先核查。')
        folder = root / 'state/runs' / run_id
        for path in folder.rglob('*'):
            inside(root, path, folder)
            if path.is_file():
                files.add(path)
    for batch_id in batch_ids:
        folder = root / 'results/batches' / batch_id
        # These generated ledgers include the removed accounts' step history.
        for name in ('performance.json', 'performance.json.gz', '完整分步骤用时.md'):
            path = folder / name
            if path.is_file():
                files.add(path)
    draws = [i for i, account in db.execute('SELECT id,account_id FROM draws') if account in ids]
    return {'account_ids': sorted(ids), 'indices': sorted(a['alias_index'] for a in rejected),
            'run_ids': sorted(run_ids), 'batch_ids': sorted(batch_ids), 'draw_ids': sorted(draws),
            'files': sorted(p.relative_to(root).as_posix() for p in files),
            'protected_archives': protected, 'complete_fingerprint': complete_fingerprint(db),
            'counters': [list(row) for row in db.execute('SELECT channel,next_index FROM counters ORDER BY channel')]}


def apply_database(root, db, expected):
    root = Path(root).resolve()
    db.execute('BEGIN IMMEDIATE')
    try:
        observed = plan(root, db)
        if observed != expected:
            raise ValueError('清理范围或成功档案已变化，停止执行。')
        ids = expected['account_ids']
        for identifier in ids:
            db.execute('DELETE FROM actions WHERE account_id=?', (identifier,))
            db.execute('DELETE FROM draws WHERE account_id=?', (identifier,))
            db.execute('DELETE FROM accounts WHERE id=? AND status!=?', (identifier, 'complete'))
        for identifier in expected['run_ids']:
            row = db.execute('SELECT data FROM runs WHERE id=?', (identifier,)).fetchone()
            run = json.loads(row[0])
            if run.get('active_account') in ids:
                run['active_account'] = None
            run.pop('pause_reason', None)
            run['storage_retention'] = {'pruned_at': time.time(), 'reason': 'explicit_history_cleanup'}
            db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(run), identifier))
        if complete_fingerprint(db) != expected['complete_fingerprint']:
            raise ValueError('成功记录或计数器不一致，回滚。')
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def rewrite_indexes(root, expected):
    root = Path(root)
    ids, indices = set(expected['account_ids']), set(expected['indices'])
    for batch_id in expected['batch_ids']:
        path = root / 'results/batches' / batch_id / 'results.json'
        if path.is_file():
            inside(root, path, root / 'results/batches')
            value = json.loads(path.read_text('utf-8'))
            value['accounts'] = [a for a in value.get('accounts', [])
                                 if a.get('id') not in ids and a.get('alias_index') not in indices]
            value.pop('summary', None)
            atomic_json(path, value, compact=True)
    # A stopped scheduler's transient snapshot is not a recovery authority.
    path = root / 'state/runner-status.json'
    if path.is_file():
        value = json.loads(path.read_text('utf-8'))
        if value.get('batch_id') in expected['batch_ids']:
            value['status'] = 'cancelled'
            for lane in value.get('lanes', []):
                if lane.get('run_id') in expected['run_ids']:
                    for key in ('account_id', 'alias', 'phase', 'waiting_for', 'observation', 'message'):
                        lane.pop(key, None)
                    lane['status'] = 'cancelled'
            atomic_json(path, value, compact=True)


def verify(root, db, expected):
    if complete_fingerprint(db) != expected['complete_fingerprint']:
        raise ValueError('成功记录或计数器核验失败。')
    if db.execute("SELECT count(*) FROM accounts WHERE status!='complete'").fetchone()[0]:
        raise ValueError('仍有异常账号记录。')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('数据库完整性校验失败。')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('数据库外键校验失败。')
    for relative, digest in expected['protected_archives'].items():
        if file_hash(Path(root) / relative) != digest:
            raise ValueError('成功档案核验失败。')
