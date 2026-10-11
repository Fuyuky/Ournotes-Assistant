"""Durable outbound results; upload failure never rolls back a completed account."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ournotes.common.secrets import SecretStore
from ournotes.integrations.cloud_client import CloudClient, CloudClientConfig, CloudError


def result_projection(archive):
    if archive['status'] != 'complete':
        raise ValueError('只有成功成果可以上传。')
    return {'schema_version': 1, 'account_id': archive['account_id'], 'index': archive['alias_index'],
            'email': archive['alias'], 'cards': archive['cards'], 'totals': archive['totals'],
            'pull_count': archive.get('pull_count') or sum(sum(v.values()) for v in archive['totals'].values()), 'draw_labels': archive.get('draw_labels', []),
            **({'pool_keys': archive['pool_keys']} if archive.get('pool_keys') else {}),
            **({'preset_snapshot': archive['preset_snapshot']} if archive.get('preset_snapshot') else {}),
            'used': (archive['usage_status'] == 'used' if 'usage_status' in archive else archive['used'])}


class CloudSync:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'state/cloud-sync.sqlite3'

    @contextmanager
    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('CREATE TABLE IF NOT EXISTS outbox (key TEXT PRIMARY KEY, kind TEXT NOT NULL, '
                       'payload TEXT NOT NULL, version INTEGER NOT NULL, event_id TEXT NOT NULL, '
                       'pending INTEGER NOT NULL, updated REAL NOT NULL)')
            yield db
            db.commit()
        finally:
            db.close()

    def enqueue(self, archive, *, retry_missing=False):
        if archive.get('cloud_replica'):
            return  # A downloaded copy must never become this node's original upload.
        result = result_projection(archive)
        payload = json.dumps(result, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload,version FROM outbox WHERE key=?', (result['account_id'],)).fetchone()
            if old is not None and (old['payload'] == payload or
                                    json.loads(old['payload']) == {'sha256': digest}):
                if retry_missing:
                    db.execute('UPDATE outbox SET payload=?,pending=1,updated=? WHERE key=?',
                               (payload, time.time(), result['account_id']))
                return
            version = 1 if old is None else old['version'] + 1
            event = hashlib.sha256(f"{result['account_id']}:{version}:{payload}".encode()).hexdigest()
            db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,1,?) ON CONFLICT(key) DO UPDATE SET '
                       'payload=excluded.payload,version=excluded.version,event_id=excluded.event_id,pending=1,updated=excluded.updated',
                       (result['account_id'], 'result', payload, version, event, time.time()))

    def finish_mail(self, ticket):
        with self.database() as db:
            db.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,1,?)',
                       ('mail:' + ticket, 'mail_finish', json.dumps({'ticket': ticket}), 1, ticket, time.time()))

    def flush(self, client=None, *, stop=None, progress=None, account_id=None):
        with SecretStore(self.root / 'state').lock('cloud-upload', wait=True):
            return self._flush(client, stop=stop, progress=progress, account_id=account_id)

    def _flush(self, client=None, *, stop=None, progress=None, account_id=None):
        client = client or CloudClient.from_root(self.root)
        with self.database() as db:
            rows = (db.execute('SELECT * FROM outbox WHERE pending>0 AND key=?', (account_id,)).fetchall()
                    if account_id is not None else
                    db.execute('SELECT * FROM outbox WHERE pending>0 ORDER BY updated').fetchall())
        sent = 0
        results_sent = 0
        for row in rows:
            if stop is not None and stop.is_set():
                return {'uploaded': sent, 'pending': True}
            payload = json.loads(row['payload'])
            if row['kind'] == 'result':
                if row['pending'] == 1:
                    receipt = client.upload(payload, row['event_id'], row['version'])
                    remote = receipt.get('result') if isinstance(receipt, dict) else None
                    if not isinstance(remote, dict):
                        remote = client.get_result(payload['account_id'])['result']
                    if any(remote.get(k) != payload.get(k) for k in
                           ('account_id', 'index', 'email', 'cards', 'totals', 'pull_count')):
                        raise ValueError('云端成果回执与待上传内容不一致，保留本机数据。')
                    with self.database() as db:
                        db.execute('UPDATE outbox SET pending=2 WHERE key=? AND event_id=?',
                                   (row['key'], row['event_id']))
                from ournotes.runtime.cloud_storage import retire_confirmed
                retire_confirmed(self.root, payload['account_id'], payload)
                results_sent += 1
            else:
                client.finish_mail(payload['ticket'])
            with self.database() as db:
                fingerprint = json.dumps({'sha256': hashlib.sha256(row['payload'].encode()).hexdigest()})
                db.execute('UPDATE outbox SET pending=0,payload=? WHERE key=? AND event_id=?',
                           (fingerprint, row['key'], row['event_id']))
            sent += 1
            if progress:
                progress(results_sent)
        return {'uploaded': sent, 'results_uploaded': results_sent}

    def pending_results(self):
        if not self.path.exists():
            return 0
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            return db.execute("SELECT COUNT(*) FROM outbox WHERE kind='result' AND pending>0").fetchone()[0]

    def upload_completed(self, account_id):
        """Attempt this account immediately; durable outbox retries survive any transport failure."""
        try:
            if CloudClientConfig.load(self.root) is None:
                return {'pending': True, 'reason': 'not_configured'}
            return self.flush(account_id=account_id)
        except CloudError as error:
            return {'pending': True, 'reason': error.code, 'http_status': error.status}
        except (OSError, ValueError, sqlite3.Error) as error:
            return {'pending': True, 'reason': type(error).__name__}

    def try_flush(self, client=None):
        try:
            return self.flush(client)
        except CloudError as error:
            return {'uploaded': 0, 'pending': True, 'reason': error.code}



class SyncWorker:
    """Best-effort upload and used refresh, owned by the graphical controller."""
    def __init__(self, root, interval=15, *, observer=None, event_observer=None):
        self.root, self.interval = Path(root), interval
        self.observer = observer
        self.event_observer = event_observer
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        self.status = {'state': 'not_configured'}
        self.revision = None
        self.observed_states = []
        from ournotes.integrations.cloud_events import CloudEvents
        self.events = CloudEvents(self.root, self.on_event)
        self.event_revision = 0

    def on_event(self, event):
        self.event_revision += 1
        self.status = {**self.status, 'event_revision': self.event_revision,
                       'event_type': event['type']}
        self.wakeup.set()
        if self.event_observer:
            self.event_observer(event)

    def start(self):
        self.events.start()
        self.thread = threading.Thread(target=self.run, name='cloud-sync', daemon=True)
        self.thread.start()

    def run_once(self):
        if CloudClientConfig.load(self.root) is None:
            self.status = {'state': 'not_configured'}
            return
        client = CloudClient.from_root(self.root)
        sent = CloudSync(self.root).flush(client, stop=self.stop)
        if self.stop.is_set():
            return
        from ournotes.accounts.used import reconcile_pending
        reconcile_pending(self.root, client)
        remote = client.health()
        if self.observer:
            self.observer(self.observed_states)
        self.revision = remote.get('revision')
        self.status = {**self.status, 'state': 'connected', 'revision': self.revision, **sent,
                       'event_revision': self.event_revision, 'events': self.events.state,
                       'updated_at': time.time()}

    def run(self):
        while not self.stop.is_set():
            self.wakeup.clear()
            try:
                self.run_once()
            except CloudError as error:
                self.status = {'state': 'pending', 'reason': error.code}
            except (OSError, ValueError, sqlite3.Error) as error:
                self.status = {'state': 'failed', 'reason': type(error).__name__}
            self.wakeup.wait(self.interval)

    def close(self):
        self.stop.set()
        self.wakeup.set()
        self.events.close()
        if self.thread is not None:
            self.thread.join()
