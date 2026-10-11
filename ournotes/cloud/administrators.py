"""Private administrator directory and recoverable, generation-bound client credentials."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ournotes.cloud.durability import sync_directory
from ournotes.cloud.store import Conflict, Forbidden, Unavailable, encoded

SUPERADMIN = 'Fuyuki'
ITERATIONS = 600_000


def password_record(password, username=SUPERADMIN):
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise ValueError('Invalid password')
    salt = secrets.token_hex(16)
    return {'username': username, 'salt': salt, 'iterations': ITERATIONS,
            'password_hash': hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt),
                                              ITERATIONS).hex()}


def validated_record(value):
    value = dict(value)
    # The old single administrator is migrated once, preserving its password verifier.
    if value.get('username') == 'admin':
        value['username'] = SUPERADMIN
    if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', value.get('username', ''))
            or type(value.get('iterations')) is not int or not 600_000 <= value['iterations'] <= 2_000_000
            or len(bytes.fromhex(value['salt'])) < 16 or len(bytes.fromhex(value['password_hash'])) != 32):
        raise ValueError('Invalid administrator password configuration')
    return value


class Administrators:
    def __init__(self, store, bootstrap=None, key_path=None):
        self.store = store
        self.key_path = Path(key_path) if key_path else store.path.with_suffix('.credential-key')
        if not self.key_path.exists():
            with store.connection() as db:
                stored = db.execute('SELECT 1 FROM sqlite_master WHERE name=\'administrator_credentials\'').fetchone()
                if stored and db.execute('SELECT 1 FROM administrator_credentials LIMIT 1').fetchone():
                    raise Unavailable('Administrator credential encryption key missing')
            fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(self.key_path.parent)
        try:
            self.cipher = Fernet(self.key_path.read_bytes())
        except (ValueError, OSError):
            raise Unavailable('Administrator credential encryption key unavailable') from None
        with store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS administrators(
                  username TEXT PRIMARY KEY,password_record TEXT NOT NULL,enabled INTEGER NOT NULL,
                  created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS administrator_credentials(
                  username TEXT PRIMARY KEY,node_id TEXT UNIQUE NOT NULL,encrypted_token TEXT NOT NULL,
                  generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS portal_sessions(
                  token_hash TEXT PRIMARY KEY,username TEXT NOT NULL,expires REAL NOT NULL,
                  credential_hash TEXT NOT NULL);
            ''')
        if bootstrap is not None:
            record = validated_record(bootstrap)
            if record['username'] != SUPERADMIN:
                raise ValueError('Bootstrap administrator must be Fuyuki')
            with store.transaction() as db:
                db.execute('INSERT INTO administrators VALUES(?,?,1,?) ON CONFLICT(username) '
                           'DO UPDATE SET password_record=excluded.password_record,enabled=1',
                           (SUPERADMIN, encoded(record), store.clock()))
                self._ensure_credential(db, SUPERADMIN)

    def _ensure_credential(self, db, username):
        if db.execute('SELECT 1 FROM administrator_credentials WHERE username=?', (username,)).fetchone():
            return
        node_id, token = 'admin-' + username, secrets.token_urlsafe(48)
        if db.execute('SELECT 1 FROM nodes WHERE id=?', (node_id,)).fetchone():
            raise Conflict('Administrator client identity already exists')
        db.execute('INSERT INTO nodes(id,token_hash,revoked,owner_username) VALUES(?,?,0,?)',
                   (node_id, hashlib.sha256(token.encode()).hexdigest(), username))
        db.execute('INSERT INTO administrator_credentials VALUES(?,?,?,1)',
                   (username, node_id, self.cipher.encrypt(token.encode()).decode()))

    @staticmethod
    def fingerprint(record):
        return hashlib.sha256(encoded(record).encode()).hexdigest()

    def authenticate_password(self, username, password):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM administrators WHERE username=? AND enabled=1',
                             (username,)).fetchone()
        # Perform the same expensive hash for an unknown account.
        record = json.loads(row['password_record']) if row else {
            'salt': '00' * 16, 'password_hash': '00' * 32, 'iterations': ITERATIONS}
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(record['salt']),
                                   record['iterations'])
        if not row or not hmac.compare_digest(digest, bytes.fromhex(record['password_hash'])):
            raise Forbidden('Invalid administrator password')
        return self.fingerprint(record)

    def session(self, token):
        if len(token) != 64:
            raise Forbidden('Administrator session required')
        with self.store.connection() as db:
            row = db.execute('SELECT s.*,a.password_record FROM portal_sessions s JOIN administrators a '
                             'ON a.username=s.username WHERE s.token_hash=? AND s.expires>? AND a.enabled=1',
                             (hashlib.sha256(token.encode()).hexdigest(), self.store.clock())).fetchone()
        if not row or not hmac.compare_digest(row['credential_hash'],
                                             self.fingerprint(json.loads(row['password_record']))):
            raise Forbidden('Administrator session expired')
        return row['username']

    def list(self):
        with self.store.connection() as db:
            return {'administrators': [dict(row) for row in db.execute(
                'SELECT username,enabled,created FROM administrators ORDER BY username')]}

    def create(self, actor, username, password):
        if actor != SUPERADMIN:
            raise Forbidden('Only Fuyuki can add administrators')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', username) or len(password) < 8:
            raise ValueError('Invalid administrator username or password')
        record = password_record(password, username)
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM administrators WHERE username=?', (username,)).fetchone():
                raise Conflict('Administrator identity cannot be reused')
            db.execute('INSERT INTO administrators VALUES(?,?,1,?)',
                       (username, encoded(record), self.store.clock()))
            self._ensure_credential(db, username)
        return {'username': username, 'enabled': True}

    def delete(self, actor, username):
        if actor != SUPERADMIN or username == SUPERADMIN:
            raise Forbidden('Only Fuyuki can delete another administrator')
        with self.store.transaction() as db:
            if not db.execute('UPDATE administrators SET enabled=0 WHERE username=?', (username,)).rowcount:
                raise KeyError(username)
            db.execute('DELETE FROM portal_sessions WHERE username=?', (username,))
            db.execute('UPDATE nodes SET revoked=1 WHERE owner_username=?', (username,))
        return {'username': username, 'enabled': False}

    def credential(self, username):
        with self.store.connection() as db:
            row = db.execute('SELECT c.* FROM administrator_credentials c JOIN administrators a '
                             'ON a.username=c.username WHERE c.username=? AND a.enabled=1', (username,)).fetchone()
        if not row:
            raise Forbidden('Administrator disabled')
        try:
            token = self.cipher.decrypt(row['encrypted_token'].encode()).decode()
        except (InvalidToken, ValueError):
            raise Unavailable('Administrator credential cannot be recovered') from None
        return {'node_id': row['node_id'], 'owner_username': username, 'token': token,
                'generation': row['generation']}

    def reset_credential(self, username):
        token = secrets.token_urlsafe(48)
        with self.store.transaction() as db:
            row = db.execute('SELECT c.* FROM administrator_credentials c JOIN administrators a '
                             'ON a.username=c.username WHERE c.username=? AND a.enabled=1', (username,)).fetchone()
            if not row:
                raise Forbidden('Administrator disabled')
            db.execute('UPDATE nodes SET revoked=1 WHERE owner_username=?', (username,))
            db.execute('UPDATE nodes SET token_hash=?,revoked=0 WHERE id=?',
                       (hashlib.sha256(token.encode()).hexdigest(), row['node_id']))
            db.execute('UPDATE administrator_credentials SET encrypted_token=?,generation=generation+1 '
                       'WHERE username=?', (self.cipher.encrypt(token.encode()).decode(), username))
        return self.credential(username)
