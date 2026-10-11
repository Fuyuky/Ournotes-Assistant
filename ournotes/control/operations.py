"""Durable idempotency receipts. In-flight commands are never replayed."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class OperationStore:
    def __init__(self, root):
        self.path = Path(root) / "state/control.sqlite3"

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, action TEXT NOT NULL, parameters TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)")
        return db

    def get(self, command_id):
        if not self.path.exists():
            raise ValueError("操作回执不存在。")
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT receipt FROM operations WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise ValueError("操作回执不存在。")
        return json.loads(row[0])

    def listing(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT receipt FROM operations ORDER BY created DESC LIMIT 200").fetchall()
        return [json.loads(row[0]) for row in rows]

    def parameters(self, command_id):
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT action,parameters FROM operations WHERE id=?', (command_id,)).fetchone()
        if row is None:
            raise ValueError('操作回执不存在。')
        return row[0], json.loads(row[1])

    def mark(self, command_id, status, result=None):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT receipt FROM operations WHERE id=?", (command_id,)).fetchone()
            if row is None:
                raise ValueError("操作回执不存在。")
            receipt = json.loads(row[0])
            receipt.update(status=status, updated_at=time.time())
            if result is not None:
                receipt["result"] = result
            db.execute("UPDATE operations SET status=?,receipt=?,updated=? WHERE id=?", (status, json.dumps(receipt, ensure_ascii=False), receipt["updated_at"], command_id))
        return receipt

    def submit(self, command_id, action, parameters, execute):
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", command_id):
            raise ValueError("command_id 格式无效。")
        raw = json.dumps({"action": action, "parameters": parameters}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        now = time.time()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,receipt FROM operations WHERE id=?", (command_id,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError("command_id 已用于不同参数。")
                return json.loads(row[1])
            receipt = {"command_id": command_id, "action": action, "status": "running", "created_at": now, "updated_at": now}
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (command_id, fingerprint, action, json.dumps(parameters), "running", json.dumps(receipt), now, now))
        self.mark(command_id, "running")
        try:
            result = execute(command_id)
        except Exception as error:
            self.mark(command_id, "failed", {"reason": "operation_failed", "error_type": type(error).__name__})
            raise
        status = "running" if isinstance(result, dict) and result.get("status") == "running" else "accepted" if action in ("pause", "finish", "resume_lanes") else "succeeded"
        # An offline worker can acknowledge before process creation returns.
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = json.loads(db.execute('SELECT receipt FROM operations WHERE id=?', (command_id,)).fetchone()[0])
            if current['status'] != 'running':
                return current
            current.update(status=status, result=result, updated_at=time.time())
            db.execute('UPDATE operations SET status=?,receipt=?,updated=? WHERE id=?',
                       (status, json.dumps(current, ensure_ascii=False), current['updated_at'], command_id))
        return current

    def pending(self, batch_id, actions=("pause", "finish")):
        """Runner reads only requests explicitly bound to its batch."""
        if not self.path.exists():
            return []
        if not actions:
            return []
        placeholders = ",".join("?" for _ in actions)
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(f"SELECT id,action,parameters FROM operations WHERE status='accepted' AND action IN ({placeholders}) ORDER BY created", tuple(actions)).fetchall()
        return [{"command_id": identifier, "action": action, "parameters": json.loads(parameters)} for identifier, action, parameters in rows if (json.loads(parameters).get("batch_id") or json.loads(parameters).get("run_id")) == batch_id]
