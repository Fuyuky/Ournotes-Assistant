"""Retire confirmed local results without retaining a second account archive."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def retire_confirmed(root, account_id, result=None):
    """Called only after an authenticated upload receipt is durable in the outbox.

    File cleanup precedes the SQL commit. A crash retries cleanup; the account
    row is the transaction guard preventing aggregate counters being added twice.
    Never constructs RuntimeStore or changes an unfinished account.
    """
    root = Path(root).resolve()
    database = root / 'state/runtime.sqlite3'
    if not database.exists():
        return False
    db = sqlite3.connect(database, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            db.commit()
            return False
        account = json.loads(row['data'])
        if account['status'] != 'complete':
            raise ValueError('只允许清理云端已确认的完整成功账号。')
        draws = [json.loads(r[0]) for r in db.execute(
            'SELECT data FROM draws WHERE account_id=? ORDER BY ordinal', (account_id,))]
        if any(d['status'] != 'complete' for d in draws):
            raise ValueError('账号仍存在未确认交易，不能清理。')
        run_row = db.execute('SELECT data FROM runs WHERE id=?', (account['run_id'],)).fetchone()
        if run_row is None:
            raise ValueError('账号运行归属缺失，不能清理。')
        run = json.loads(run_row[0])
        if run.get('active_account') == account_id:
            raise ValueError('账号仍被运行任务持有，不能清理。')
        # Use the existing, boundary-checked evidence cleanup with this connection.
        from ournotes.runtime.retention import cleanup_success, evidence_folders
        from ournotes.runtime.storage import inside
        from ournotes.runtime.store import RuntimeStore
        adapter = RuntimeStore.__new__(RuntimeStore)
        adapter.root, adapter.db = root, db
        report = dict(account, draws=draws, unknown_slots=0)
        cleanup_success(adapter, report)
        result_path = root / 'results/accounts' / (account_id + '.json')
        candidates = [result_path]
        for folder in evidence_folders(adapter, report):
            for checkpoint in (folder / 'incidents').glob('*/state.json'):
                inside(root, checkpoint, root / 'state/runs')
                incident = json.loads(checkpoint.read_text('utf-8'))
                if incident.get('snapshot', {}).get('account_id') == account_id:
                    candidates.append(checkpoint)
                    frames = checkpoint.with_name('frames.json')
                    if frames.is_file():
                        candidates.append(frames)
        for draw in draws:
            cache = root / 'state/runs' / account['run_id'] / 'recognition' / (draw['id'] + '.json')
            inside(root, cache, root / 'state/runs')
            if cache.is_file():
                cached = json.loads(cache.read_text('utf-8'))
                if cached.get('draw_id') != draw['id']:
                    raise ValueError('识别缓存身份不一致。')
                cache.unlink()
        if account.get('archive'):
            archive = inside(root, root / account['archive'], root / 'results/archive/accounts')
            value = json.loads(archive.read_text('utf-8')) if archive.is_file() else None
            if value and value.get('account_id') != account_id:
                raise ValueError('历史档案身份不一致。')
            candidates.append(archive)
        for path in candidates:
            boundary = (root / 'state/runs' if path.is_relative_to(root / 'state/runs') else
                        root / ('results/archive/accounts' if path != result_path else 'results/accounts'))
            inside(root, path, boundary)
        for path in candidates:
            path.unlink(missing_ok=True)
        run['cloud_confirmed'] = run.get('cloud_confirmed', 0) + 1
        totals = run.setdefault('cloud_totals', {})
        if result is not None:
            for kind, rarities in result['totals'].items():
                counts = totals.setdefault(kind, {})
                for rarity, count in rarities.items():
                    counts[rarity] = counts.get(rarity, 0) + count
        db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(run), run['id']))
        db.execute('DELETE FROM actions WHERE account_id=?', (account_id,))
        db.execute('DELETE FROM draws WHERE account_id=?', (account_id,))
        db.execute('DELETE FROM accounts WHERE id=?', (account_id,))
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
