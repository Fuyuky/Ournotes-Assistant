"""Explicit legacy archive retirement after authenticated cloud verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from ournotes.common.secrets import SecretStore
from ournotes.integrations.cloud_client import CloudClient
from ournotes.integrations.cloud_sync import CloudSync, result_projection
from ournotes.paths import DATA_ROOT
from ournotes.runtime.cloud_storage import retire_confirmed
from ournotes.runtime.storage import inside


def migrate(root, client, *, apply=False):
    """No allocation, mail, publication or restore operations; mismatches stop cleanup.

    Old uncertain usage/rebind flags require their existing reconciliation before
    retirement. A dry run only reads remote results and local archives.
    """
    root = Path(root).resolve()
    stats = {'verified': 0, 'retired': 0, 'protected': 0, 'invalid': 0, 'apply': apply}
    sync = CloudSync(root)
    locks = SecretStore(root / 'state')
    with locks.lock('runner'), locks.lock('email-rebind'), locks.lock('cloud-upload', wait=True), locks.lock('account-used', wait=True):
        for path in sorted((root / 'results/archive/accounts').glob('*.json')):
            inside(root, path, root / 'results/archive/accounts')
            raw = path.read_bytes()
            value = json.loads(raw)
            if value.get('status') != 'complete':
                stats['invalid'] += 1
                continue
            if any(value.get(key) for key in ('used_pending', 'booking_pending', 'rebind_used_pending')):
                stats['protected'] += 1
                continue
            expected = result_projection(value)
            envelope = client.get_result(expected['account_id'])
            actual = envelope['result']
            if any(actual.get(key) != expected.get(key) for key in
                   ('account_id', 'index', 'email', 'cards', 'totals', 'pull_count')):
                raise ValueError('历史账号与云端内容不一致，停止迁移且保留本机数据。')
            stats['verified'] += 1
            if not apply:
                continue
            payload = json.dumps(expected, sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            # Store the already verified acknowledgement before removing any data.
            with sync.database() as db:
                old = db.execute('SELECT * FROM outbox WHERE key=?', (expected['account_id'],)).fetchone()
                if old is not None and old['pending'] and old['payload'] != payload:
                    raise ValueError('发送队列存在不同版本的待确认成果，保留本机数据。')
                if old is None:
                    db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?)',
                               (expected['account_id'], 'result', payload,
                                envelope.get('source_version', 1), 'verified-' + digest, 2, time.time()))
                else:
                    db.execute('UPDATE outbox SET payload=?,pending=2 WHERE key=?', (payload, expected['account_id']))
            retire_confirmed(root, expected['account_id'], expected)
            # Foreign replicas / legacy archive-only rows have no local runtime row.
            if path.exists():
                if path.read_bytes() != raw:
                    raise ValueError('迁移期间档案已改变，保留该文件。')
                path.unlink()
            with sync.database() as db:
                db.execute('UPDATE outbox SET payload=?,pending=0 WHERE key=?',
                           (json.dumps({'sha256': digest}), expected['account_id']))
            stats['retired'] += 1
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DATA_ROOT)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = migrate(args.root, CloudClient.from_root(args.root), apply=args.apply)
    except Exception as error:  # noqa: BLE001 -- CLI errors must not disclose account contents
        # An upstream exception must never print an account payload or credentials.
        raise SystemExit('迁移停止：' + type(error).__name__ + '；保留未确认数据，请检查云端与迁移条件。') from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
