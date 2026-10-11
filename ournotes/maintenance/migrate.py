"""One-time account/config migration with an immutable pre-change backup."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.paths import DATA_ROOT
from ournotes.runtime.store import RuntimeStore

VERSION = 2
ARCHIVE_FIELDS = ('schema_version', 'account_id', 'alias_index', 'alias', 'status', 'totals',
                  'cards', 'used', 'pull_count', 'node_id', 'cloud_version', 'draw_labels')
CONFIG_MOVES = (('mail-rule.json', 'config/mail-rule.local.json'),
                ('state/webui/settings.json', 'config/client.json'))


def normalize_archive(value, account_id):
    if value.get('owner_use') and not value.get('used', False):
        raise ValueError('自用标记与使用标记冲突，须先核查。')
    if value.get('account_id', account_id) != account_id:
        raise ValueError('成果 UUID 与原运行记录不一致。')
    result = {key: value[key] for key in ARCHIVE_FIELDS if key in value}
    result.update(schema_version=1, account_id=account_id, used=value.get('used', False))
    if result['status'] not in ('complete', 'partial', 'cancelled') or type(result['used']) is not bool:
        raise ValueError('最终档案格式无效。')
    return result


def inventory(root):
    root = Path(root).resolve()
    path = root / 'state/runtime.sqlite3'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        rows = db.execute('SELECT id,alias,alias_index,status,data FROM accounts').fetchall()
        counters = dict(db.execute('SELECT channel,next_index FROM counters'))
        draws = dict(db.execute('SELECT status,count(*) FROM draws GROUP BY status'))
        accounts = {}
        for row in rows:
            if row[2] in accounts:
                raise ValueError('运行库含重复 index，迁移已停止。')
            accounts[row[2]] = row
        entries = []
        for source in sorted((root / 'results/archive/accounts').glob('*.json')):
            if source.is_symlink() or source.resolve().parent != (root / 'results/archive/accounts').resolve():
                raise ValueError('档案路径越界。')
            raw = source.read_bytes()
            value = json.loads(raw)
            account = accounts.get(value['alias_index'])
            if account is None or value['alias'] != account[1]:
                raise ValueError('档案没有唯一匹配的历史身份，不生成替代 UUID。')
            normalized = normalize_archive(value, account[0])
            if value != normalized:
                entries.append((source, raw, normalized))
    return {'root': root, 'entries': entries, 'counters': counters, 'draws': draws,
            'account_ids': sorted(row[0] for row in rows), 'archive_count': len(list((root / 'results/archive/accounts').glob('*.json')))}


def migrate(root=DATA_ROOT, *, apply=False):
    root = Path(root).resolve()
    locks = SecretStore(root / 'state')
    with locks.lock('runner'), locks.lock('email-rebind'), locks.lock('account-used'):
        plan = inventory(root)
        moves = [(root / source, root / target) for source, target in CONFIG_MOVES if (root / source).is_file()]
        for source, target in moves:
            if target.exists() and target.read_bytes() != source.read_bytes():
                raise ValueError('新旧配置同时存在且不同，停止迁移。')
        report = {'version': VERSION, 'applied': False, 'archives': plan['archive_count'],
                  'changed_archives': len(plan['entries']), 'configuration_moves': len(moves),
                  'counters': plan['counters'], 'draws': plan['draws']}
        if not apply:
            return report
        backup = root / 'state/migrations' / f'v{VERSION}-{time.time_ns()}'
        backup.mkdir(parents=True)
        with (closing(sqlite3.connect((root / 'state/runtime.sqlite3').as_uri() + '?mode=ro', uri=True)) as source_db,
              closing(sqlite3.connect(backup / 'runtime.sqlite3')) as target_db):
            source_db.backup(target_db)
        manifest = []
        for source, raw, _ in plan['entries']:
            destination = backup / source.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            manifest.append({'path': source.relative_to(root).as_posix(), 'sha256': hashlib.sha256(raw).hexdigest()})
        for source, _ in moves:
            destination = backup / source.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        atomic_json(backup / 'manifest.json', dict(report, files=manifest))
        for source, raw, normalized in plan['entries']:
            if source.read_bytes() != raw:
                raise ValueError('档案在迁移前发生变化，保留备份并停止。')
            atomic_json(source, normalized, compact=True)
        db = RuntimeStore(root)
        try:
            with db.transaction():
                for identifier, raw in db.db.execute('SELECT id,data FROM accounts').fetchall():
                    value = json.loads(raw)
                    changed = 'owner_use' in value
                    value.pop('owner_use', None)
                    if value['status'] in ('complete', 'partial', 'cancelled'):
                        for key in ('game_uid', 'uid', 'UID'):
                            changed = key in value or changed
                            value.pop(key, None)
                    if changed:
                        db.db.execute('UPDATE accounts SET data=? WHERE id=?', (json.dumps(value), identifier))
                # Counters, draws, actions and completed counts are deliberately untouched.
        finally:
            db.close()
        for source in (root / 'results/accounts').glob('*.json'):
            value = json.loads(source.read_text('utf-8'))
            if value.get('status') in ('complete', 'partial', 'cancelled'):
                keys = set(value) & {'game_uid', 'uid', 'UID', 'owner_use'}
                if keys:
                    dest = backup / source.relative_to(root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                    atomic_json(source, {k: v for k, v in value.items() if k not in keys})
        for source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                source.unlink()
            else:
                source.replace(target)
        after = inventory(root)
        if any(after[key] != plan[key] for key in ('counters', 'draws', 'account_ids', 'archive_count')):
            raise RuntimeError('迁移后数据不变量不一致；保留现场与备份。')
        report.update(applied=True, backup=str(backup.relative_to(root)))
        atomic_json(root / 'state/migrations/current.json', report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DATA_ROOT)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, apply=args.apply), ensure_ascii=False))


if __name__ == '__main__':
    main()
