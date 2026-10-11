"""Server-local SQLite, permanent receipts and independently durable allocation audit."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

from ournotes.cloud.durability import sync_directory
from ournotes.cloud.models import Result
from ournotes.common.identity import alias_at, next_usable_index


class Conflict(ValueError):
    pass


class Forbidden(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


class AuthenticatedNode(str):
    """Retain the credential generation across slow external work without exposing its hash."""
    def __new__(cls, node_id, credential_hash, owner_username='Fuyuki'):
        value = super().__new__(cls, node_id)
        value.credential_hash = credential_hash
        value.owner_username = owner_username
        return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,revoked INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(account_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,
 index_no INTEGER UNIQUE NOT NULL,alias TEXT UNIQUE NOT NULL,channel TEXT NOT NULL,request_id TEXT NOT NULL,
 UNIQUE(node_id,request_id));
CREATE TABLE IF NOT EXISTS receipts(node_id TEXT NOT NULL,scope TEXT NOT NULL,request_id TEXT NOT NULL,
 payload TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(node_id,scope,request_id));
CREATE TABLE IF NOT EXISTS results(account_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,source_version INTEGER NOT NULL,
 version INTEGER NOT NULL,payload TEXT NOT NULL,usage_status TEXT NOT NULL DEFAULT 'available',
 usage_changed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,node_id TEXT NOT NULL,account_id TEXT NOT NULL,
 alias TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,state TEXT NOT NULL,
 baseline TEXT NOT NULL,rule TEXT NOT NULL,message_id TEXT,code TEXT);
CREATE TABLE IF NOT EXISTS consumed(message_id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL,consumed REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ticket_retries(account_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,
 original_ticket TEXT UNIQUE NOT NULL,retry_ticket TEXT UNIQUE NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL);
"""


class Store:
    def __init__(self, path, mailbox, *, clock=time.time):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self.mailbox = mailbox.lower()
        if not re.fullmatch(r"[a-z0-9]+@gmail\.com", self.mailbox):
            raise ValueError("Canonical undotted Gmail namespace required")
        alias_at(1 << 20, self.mailbox)
        self.clock = clock
        self.audit_path = self.path.with_suffix(".allocation-audit.jsonl")
        self.lock = threading.RLock()
        with self.connection() as db:
            db.executescript(SCHEMA)
            for table in ('nodes', 'allocations', 'results'):
                columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                if 'owner_username' not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN owner_username TEXT NOT NULL DEFAULT 'Fuyuki'")
            columns = {row['name'] for row in db.execute('PRAGMA table_info(allocations)')}
            if 'activity_payload' not in columns:
                db.execute('ALTER TABLE allocations ADD COLUMN activity_payload TEXT')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(results)')}
            if 'usage_status' not in columns:
                db.execute("ALTER TABLE results ADD COLUMN usage_status TEXT NOT NULL DEFAULT 'available'")
                db.execute("UPDATE results SET usage_status=CASE WHEN json_extract(payload,'$.used')=1 "
                           "THEN 'used' ELSE 'available' END")
            if 'usage_changed' not in columns:
                db.execute('ALTER TABLE results ADD COLUMN usage_changed INTEGER NOT NULL DEFAULT 0')
                db.execute('UPDATE results SET usage_changed=1 WHERE version>source_version')
            for key, value in {"allocation_enabled": "false", "test_next_index": "8",
                               "formal_next_index": str(1 << 20), "revision": "0"}.items():
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, value))
            stored = db.execute("SELECT value FROM metadata WHERE key='mailbox'").fetchone()
            if stored and stored[0] != self.mailbox:
                raise Conflict("Existing alias namespace cannot change mailbox")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('mailbox',?)", (self.mailbox,))
            db.commit()
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        # A commit may have failed after its fsynced reservation. Burn those numbers.
        try:
            with self.transaction() as db:
                self.reconcile_audit(db, force=True)
        except (Unavailable, sqlite3.IntegrityError):
            # A damaged allocation journal closes only allocation; existing private results remain queryable.
            self.close_allocations()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.lock, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def meta(db, key):
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def set_meta(db, key, value):
        db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))

    def audit_records(self):
        if not self.audit_path.exists():
            return []
        try:
            records = [json.loads(line) for line in self.audit_path.read_text("utf-8").splitlines()]
            for record in records:
                if record["channel"] not in ("test", "formal") or type(record["index"]) is not int:
                    raise ValueError
            return records
        except (ValueError, KeyError, TypeError):
            raise Unavailable("Allocation audit damaged; verify high water before recovery") from None

    def reconcile_audit(self, db, *, force=False):
        size = self.audit_path.stat().st_size if self.audit_path.exists() else 0
        recorded_size = self.meta(db, "allocation_audit_size")
        if recorded_size is not None:
            if size < int(recorded_size):
                raise Unavailable("Allocation journal missing or truncated; verify recovery evidence")
            if int(recorded_size) == size and not force:
                return
        for item in self.audit_records():
            key = item["channel"] + "_next_index"
            self.set_meta(db, key, max(int(self.meta(db, key)), item["index"] + 1))
            row = db.execute("SELECT * FROM allocations WHERE account_id=?", (item["account_id"],)).fetchone()
            if row and any(self.allocation_dto(row).get(key) != value for key, value in item.items()):
                raise Unavailable("Allocation audit identity conflict; allocation recovery required")
            if not row:
                activity_payload = encoded(item.get('activity')) if 'activity_id' in item else None
                owner = db.execute('SELECT owner_username FROM nodes WHERE id=?', (item['node_id'],)).fetchone()
                db.execute("INSERT INTO allocations(account_id,node_id,index_no,alias,channel,request_id,"
                           "activity_payload,owner_username) VALUES (?,?,?,?,?,?,?,?)",
                           (item["account_id"], item["node_id"], item["index"], item["alias"],
                            item["channel"], item["request_id"], activity_payload,
                            owner['owner_username'] if owner else 'Fuyuki'))
            request = {key: item[key] for key in ("request_id", "account_id", "channel")}
            if self.receipt(db, item["node_id"], "allocation", item["request_id"], request) is None:
                self.save_receipt(db, item["node_id"], "allocation", item["request_id"], request, item)
        self.set_meta(db, "allocation_audit_size", size)

    def close_allocations(self):
        with self.transaction() as db:
            self.set_meta(db, "allocation_enabled", "false")

    def high_water(self, request):
        with self.transaction() as db:
            for channel in ("test", "formal"):
                value = request[channel + "_next_index"]
                if value < int(self.meta(db, channel + "_next_index")):
                    raise Conflict("High water cannot decrease")
                alias_at(value - 1, self.mailbox)
                self.set_meta(db, channel + "_next_index", value)
            self.set_meta(db, "high_water_evidence", request["evidence"])
            self.set_meta(db, "allocation_enabled", "true")
        return {"allocation_enabled": True}

    def create_node(self, node_id):
        token = secrets.token_urlsafe(48)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone():
                raise Conflict("Node exists; revoke and explicitly rotate its credential")
            db.execute("INSERT INTO nodes(id,token_hash,revoked) VALUES (?,?,0)",
                       (node_id, hashlib.sha256(token.encode()).hexdigest()))
        return {"node_id": node_id, "token": token}

    def rotate_node(self, node_id):
        token = secrets.token_urlsafe(48)
        with self.transaction() as db:
            if not db.execute("UPDATE nodes SET token_hash=?,revoked=0 WHERE id=?",
                              (hashlib.sha256(token.encode()).hexdigest(), node_id)).rowcount:
                raise KeyError(node_id)
        return {"node_id": node_id, "token": token}

    def revoke_node(self, node_id):
        with self.transaction() as db:
            if not db.execute("UPDATE nodes SET revoked=1 WHERE id=?", (node_id,)).rowcount:
                raise KeyError(node_id)
        return {"node_id": node_id, "revoked": True}

    def authenticate(self, token):
        credential_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as db:
            row = db.execute("SELECT id,owner_username FROM nodes WHERE token_hash=? AND revoked=0",
                             (credential_hash,)).fetchone()
            if row and db.execute("SELECT 1 FROM sqlite_master WHERE name='administrators'").fetchone():
                owner = db.execute('SELECT enabled FROM administrators WHERE username=?',
                                   (row['owner_username'],)).fetchone()
                if owner and not owner['enabled']:
                    row = None
        if not row:
            raise Forbidden("Invalid or revoked node credential")
        return AuthenticatedNode(row['id'], credential_hash, row['owner_username'])

    @staticmethod
    def require_node(db, node):
        row = db.execute("SELECT token_hash,revoked,owner_username FROM nodes WHERE id=?", (node,)).fetchone()
        generation = getattr(node, "credential_hash", None)
        if (not row or row["revoked"] or
                (generation is not None and not hmac.compare_digest(row["token_hash"], generation))):
            raise Forbidden("Node credential revoked or rotated during request")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='administrators'").fetchone():
            owner = db.execute('SELECT enabled FROM administrators WHERE username=?',
                               (row['owner_username'],)).fetchone()
            if owner and not owner['enabled']:
                raise Forbidden('Administrator disabled')

    @staticmethod
    def owner(db, node):
        row = db.execute('SELECT owner_username FROM nodes WHERE id=?', (str(node),)).fetchone()
        if not row:
            raise Forbidden('Unknown node')
        return row['owner_username']

    def require_owner(self, db, identity, origin_node):
        self.require_node(db, identity)
        if self.owner(db, identity) != self.owner(db, origin_node):
            raise Forbidden('Allocation belongs to another administrator')

    def owned_receipt(self, db, identity, scope, request_id, payload):
        """Recover old receipts after a credential reset without changing their original scope."""
        self.require_node(db, identity)
        nodes = db.execute('SELECT id FROM nodes WHERE owner_username=? ORDER BY id',
                           (self.owner(db, identity),)).fetchall()
        for row in nodes:
            prior = self.receipt(db, row['id'], scope, request_id, payload)
            if prior is not None:
                return prior

    @staticmethod
    def receipt(db, node, scope, request_id, payload):
        row = db.execute("SELECT payload,response FROM receipts WHERE node_id=? AND scope=? AND request_id=?",
                         (node, scope, request_id)).fetchone()
        if row:
            if not hmac.compare_digest(row["payload"].encode("utf-8"), encoded(payload).encode("utf-8")):
                raise Conflict("Request ID reused with different parameters")
            return json.loads(row["response"])

    @staticmethod
    def save_receipt(db, node, scope, request_id, payload, response):
        db.execute("INSERT INTO receipts VALUES (?,?,?,?,?)",
                   (node, scope, request_id, encoded(payload), encoded(response)))

    def allocation(self, account_id, node=None):
        with self.connection() as db:
            row = db.execute("SELECT * FROM allocations WHERE account_id=?", (account_id,)).fetchone()
            if row and node is not None:
                self.require_owner(db, node, row['node_id'])
        if not row:
            raise KeyError(account_id)
        return self.allocation_dto(row)

    @staticmethod
    def allocation_dto(row):
        value = {"account_id": row["account_id"], "index": row["index_no"], "alias": row["alias"],
                "node_id": row["node_id"], "channel": row["channel"], "request_id": row["request_id"]}
        if row['activity_payload'] is not None:
            activity = json.loads(row['activity_payload'])
            value.update(activity_id=activity['id'] if activity else None, activity=activity)
        return value

    def allocate(self, node, request):
        with self.transaction() as db:
            self.require_node(db, node)
            self.reconcile_audit(db)
            prior = self.owned_receipt(db, node, "allocation", request["request_id"], request)
            if prior is not None:
                return prior
            if self.meta(db, "allocation_enabled") != "true":
                raise Unavailable("Allocation closed; verified high water required")
            if db.execute("SELECT 1 FROM allocations WHERE account_id=?", (request["account_id"],)).fetchone():
                raise Conflict("Account identity already allocated")
            key = request["channel"] + "_next_index"
            try:
                index = next_usable_index(int(self.meta(db, key)), self.mailbox)
            except ValueError:
                raise Unavailable("Usable alias namespace exhausted") from None
            if request["channel"] == "test" and index >= 1 << 20:
                raise Unavailable("Test namespace exhausted")
            alias = alias_at(index, self.mailbox)
            activity = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='activities'").fetchone():
                activity_id = self.meta(db, 'active_activity_id')
                row = db.execute('SELECT payload FROM activities WHERE id=?', (activity_id,)).fetchone()
                activity = json.loads(row['payload']) if row else None
            response = {**request, "node_id": node, "index": index, "alias": alias,
                        'activity_id': activity['id'] if activity else None, 'activity': activity}
            # Reserve outside the DB rollback domain before exposing/committing.
            with self.audit_path.open("a", encoding="utf-8") as handle:
                if os.name != "nt":
                    os.chmod(self.audit_path, 0o600)
                handle.write(encoded(response) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                sync_directory(self.audit_path.parent)
                self.set_meta(db, "allocation_audit_size", handle.tell())
            self.set_meta(db, key, index + 1)
            db.execute("INSERT INTO allocations(account_id,node_id,index_no,alias,channel,request_id,"
                       "owner_username,activity_payload) VALUES (?,?,?,?,?,?,?,?)",
                       (request["account_id"], node, index, alias, request["channel"], request["request_id"],
                        self.owner(db, node), encoded(activity)))
            self.save_receipt(db, node, "allocation", request["request_id"], request, response)
            return response

    @staticmethod
    def envelope(row):
        result = json.loads(row['payload'])
        result['used'] = row['usage_status'] != 'available'
        return {"result": result, "usage_status": row['usage_status'], "node_id": row["node_id"],
                "owner_username": row['owner_username'], 'activity_id': result.get('activity_id'),
                'activity': result.get('activity'),
                "version": row["version"], "source_version": row["source_version"]}

    def final(self, row):
        try:
            value = Result.model_validate(json.loads(row['payload'])).model_dump()
            if value['account_id'] != row['account_id']:
                return False
            validator = getattr(self, 'result_validator', None)
            if validator is not None:
                validator.account({**value, 'status': 'complete', 'alias_index': value['index']})
            return True
        except (ValueError, TypeError, KeyError):
            return False

    def results(self, account_id=None, *, owner_username=None):
        owner_username = owner_username or None
        with self.connection() as db:
            db.execute("BEGIN")
            revision = int(self.meta(db, "revision"))
            if account_id is not None:
                row = db.execute("SELECT * FROM results WHERE account_id=?", (account_id,)).fetchone()
                if not row or not self.final(row):
                    raise KeyError(account_id)
                return self.envelope(row)
            rows = db.execute('SELECT r.* FROM results r LEFT JOIN allocations a ON a.account_id=r.account_id '
                              'ORDER BY a.index_no').fetchall()
            return {"revision": revision, "results": [self.envelope(row) for row in rows if self.final(row)
                      and (owner_username is None or row['owner_username'] == owner_username)]}

    def result_by_index(self, index):
        """Resolve one shared index through its existing unique allocation index."""
        if type(index) is not int or not 0 <= index <= 2**53 - 1:
            raise ValueError("Invalid result index")
        with self.connection() as db:
            row = db.execute(
                "SELECT r.* FROM allocations a JOIN results r ON r.account_id=a.account_id WHERE a.index_no=?",
                (index,),
            ).fetchone()
            if row is None or not self.final(row):
                raise KeyError(index)
            return self.envelope(row)

    def sync_manifest(self, *, since=None, inventory=False):
        """Small state projection from one SQLite snapshot; never serialize card payloads."""
        with self.connection() as db:
            db.execute("BEGIN")
            revision = int(self.meta(db, "revision"))
            value = {"revision": revision, "usage_protocol": 2, "unchanged": since == revision, "states": []}
            rows = [row for row in db.execute('SELECT * FROM results ORDER BY account_id') if self.final(row)]
            if inventory:
                value["accounts"] = [{'account_id': row['account_id'], 'node_id': row['node_id'],
                                      'owner_username': row['owner_username'],
                                      'source_version': row['source_version'],
                                      'index': json.loads(row['payload'])['index']} for row in rows]
            if since != revision:
                value["states"] = [{"version": row["version"], "usage_status": row['usage_status'], "result": {
                    "account_id": row["account_id"], "index": json.loads(row['payload'])['index'],
                    "used": row['usage_status'] != 'available'}} for row in rows
                    if row['usage_status'] != 'available' or row['usage_changed'] or row['version'] > row['source_version']]
            return value

    def result_subset(self, account_ids):
        with self.connection() as db:
            db.execute("BEGIN")
            marks = ",".join("?" for _ in account_ids)
            rows = db.execute(f"SELECT * FROM results WHERE account_id IN ({marks})", account_ids)
            return {"results": [self.envelope(row) for row in rows if self.final(row)]}

    def bump_revision(self, db):
        value = int(self.meta(db, "revision")) + 1
        self.set_meta(db, "revision", value)
        self.set_meta(db, 'publication_pending_revision', value)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='cloud_events'").fetchone():
            from ournotes.cloud.events import Events
            Events.emit(db, 'results.changed', value)
        return value

    def upload(self, node, account_id, request):
        result = Result.model_validate(request["result"]).model_dump()
        if result["account_id"] != account_id:
            raise Conflict("Result identity does not match URL")
        with self.transaction() as db:
            self.require_node(db, node)
            allocation = db.execute("SELECT * FROM allocations WHERE account_id=?", (account_id,)).fetchone()
            if not allocation:
                raise Forbidden('Result requires its original allocation')
            self.require_owner(db, node, allocation['node_id'])
            origin = allocation['node_id']
            prior = self.receipt(db, origin, "upload", request["event_id"], request)
            if prior is not None:
                return prior
            if allocation["index_no"] != result["index"] or allocation["alias"] != result["email"]:
                raise Conflict("Result allocation identity mismatch")
            result.pop('activity', None)
            result.pop('activity_id', None)
            if allocation['activity_payload'] is not None:
                activity = json.loads(allocation['activity_payload'])
                result.update(activity_id=activity['id'] if activity else None, activity=activity)
            old = db.execute("SELECT * FROM results WHERE account_id=?", (account_id,)).fetchone()
            if old and request["version"] == old["source_version"]:
                previous = json.loads(old["payload"])
                if {k: v for k, v in previous.items() if k not in ("used",)} == {
                    k: v for k, v in result.items() if k not in ("used",)
                }:
                    response = self.envelope(old)
                    self.save_receipt(db, origin, "upload", request["event_id"], request, response)
                    return response
            if old and request["version"] <= old["source_version"]:
                raise Conflict("Old source version cannot overwrite result")
            version = old["version"] + 1 if old else 1
            if old:
                saved = json.loads(old["payload"])
                result["used"] = saved["used"]
            current = old['usage_status'] if old else ('used' if result['used'] else 'available')
            result['used'] = current != 'available'
            db.execute("INSERT INTO results (account_id,node_id,source_version,version,payload,usage_status,owner_username) "
                       "VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                       "source_version=excluded.source_version,version=excluded.version,payload=excluded.payload",
                       (account_id, origin, request["version"], version, encoded(result), current,
                        allocation['owner_username']))
            self.bump_revision(db)
            response = self.envelope(db.execute('SELECT * FROM results WHERE account_id=?', (account_id,)).fetchone())
            self.save_receipt(db, origin, "upload", request["event_id"], request, response)
            return response

    def used(self, node, account_id, request):
        return self._used(node, account_id, request, administrator=False)

    def used_admin(self, account_id, request):
        return self._used('admin', account_id, request, administrator=True)

    def _used(self, node, account_id, request, *, administrator):
        return self._usage(node, account_id, request, administrator=administrator, legacy=True)

    def usage(self, node, account_id, request):
        return self._usage(node, account_id, request, administrator=False)

    def usage_admin(self, username, account_id, request):
        return self._usage('administrator:' + username, account_id, request, administrator=True)

    def usage_receipt(self, node, account_id, request_id):
        with self.connection() as db:
            self.require_node(db, node)
            row = db.execute("SELECT r.payload,r.response FROM receipts r JOIN nodes n ON n.id=r.node_id "
                             "WHERE n.owner_username=? AND r.scope='usage' AND r.request_id=?",
                             (self.owner(db, node), request_id)).fetchone()
            if not row or json.loads(row['payload'])['account_id'] != account_id:
                raise KeyError(request_id)
            return json.loads(row['response'])

    def _usage(self, node, account_id, request, *, administrator, legacy=False):
        payload = {"account_id": account_id, **request}
        scope = 'used' if legacy else 'usage'
        with self.transaction() as db:
            if not administrator:
                self.require_node(db, node)
            prior = (self.receipt(db, node, scope, request['request_id'], payload) if administrator else
                     self.owned_receipt(db, node, scope, request['request_id'], payload))
            if prior is not None:
                return prior
            if legacy and not request['used']:
                raise Conflict('已使用状态不可撤销；撤销已拍下请使用账号状态接口')
            row = db.execute("SELECT * FROM results WHERE account_id=?", (account_id,)).fetchone()
            if not row or not self.final(row):
                raise KeyError(account_id)
            if row["version"] != request["expected_version"]:
                raise Conflict("Usage version conflict; fetch current result")
            target = 'used' if legacy else request['target_status']
            if target not in ('available', 'booked', 'used'):
                raise ValueError('Invalid usage status')
            if row['usage_status'] == 'used' and target != 'used':
                raise Conflict('已使用状态不可撤销')
            result = json.loads(row["payload"])
            result["used"] = target != 'available'
            version = row["version"] + 1
            db.execute("UPDATE results SET version=?,payload=?,usage_status=?,usage_changed=1 WHERE account_id=?",
                       (version, encoded(result), target, account_id))
            self.bump_revision(db)
            response = self.envelope(db.execute('SELECT * FROM results WHERE account_id=?', (account_id,)).fetchone())
            self.save_receipt(db, node, scope, request["request_id"], payload, response)
            return response

    def import_historical(self, request):
        result, node = request["result"], request["node_id"]
        Result.model_validate(result)
        channel, index = request["channel"], result["index"]
        if (channel == "test" and not 8 <= index < 1 << 20) or (channel == "formal" and index < 1 << 20):
            raise Conflict("Index outside historical namespace")
        if alias_at(index, self.mailbox) != result["email"]:
            raise Conflict("Historical alias mismatch")
        with self.transaction() as db:
            prior = self.receipt(db, "admin", "import", request["request_id"], request)
            if prior is not None:
                return prior
            if not db.execute("SELECT 1 FROM nodes WHERE id=?", (node,)).fetchone():
                raise KeyError(node)
            if index >= int(self.meta(db, channel + "_next_index")):
                raise Conflict("Historical index must be below verified high water")
            old = db.execute("SELECT * FROM allocations WHERE account_id=? OR index_no=?",
                             (result["account_id"], index)).fetchall()
            if old:
                if len(old) != 1 or self.allocation_dto(old[0]) != {
                    "account_id": result["account_id"], "node_id": node, "index": index,
                    "alias": result["email"], "channel": channel, "request_id": old[0]["request_id"]}:
                    raise Conflict("Historical identity collision")
                existing = db.execute("SELECT * FROM results WHERE account_id=?", (result["account_id"],)).fetchone()
                if not existing or json.loads(existing["payload"]) != result:
                    raise Conflict("Historical result collision")
                response = self.envelope(existing)
            else:
                db.execute("INSERT INTO allocations(account_id,node_id,index_no,alias,channel,request_id) VALUES (?,?,?,?,?,?)",
                           (result["account_id"], node, index, result["email"], channel,
                            "import:" + request["request_id"]))
                db.execute("INSERT INTO results (account_id,node_id,source_version,version,payload,usage_status) "
                           "VALUES (?,?,1,1,?,?)",
                           (result["account_id"], node, encoded(result), 'used' if result['used'] else 'available'))
                self.bump_revision(db)
                response = self.envelope(db.execute('SELECT * FROM results WHERE account_id=?',
                                         (result['account_id'],)).fetchone())
            self.save_receipt(db, "admin", "import", request["request_id"], request, response)
            return response

    def backup(self, destination):
        destination = Path(destination)
        if destination.exists() or destination.resolve() == self.path:
            raise Conflict("Backup destination must be a new file")
        credential_key = self.path.with_suffix('.credential-key')
        backup_key = destination.with_suffix('.credential-key')
        if credential_key.exists() and backup_key.exists():
            raise Conflict('Backup encryption-key destination already exists')
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.connection() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            target.execute("UPDATE metadata SET value='false' WHERE key='allocation_enabled'")
            # Backups must not extend ephemeral verification-code retention or revive login tickets.
            target.execute("UPDATE tickets SET code=NULL,state='expired' WHERE state!='done'")
            target.commit()
        if credential_key.exists():
            key_fd = os.open(backup_key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(key_fd, 'wb') as handle:
                handle.write(credential_key.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(backup_key.parent)
        return destination
