"""Durable single-writer workflow state and atomic per-account JSON exports."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from ournotes.common.files import atomic_json
from ournotes.common.identity import FORMAL_START, TEST_START, alias_at, alias_is_usable, mailbox_identity
from ournotes.paths import DATA_ROOT

SCHEMA_VERSION = 2


class AllocationClosed(RuntimeError):
    """The persisted deadline forbids another account; existing work may drain."""






class RuntimeStore:
    def __init__(self, root: Path = DATA_ROOT, *, allocator=None):
        self.root = Path(root)
        self.allocator = allocator
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state/runtime.sqlite3", isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.db.close()
            raise ValueError("状态库来自较新版本，拒绝降级写入。")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS counters (channel TEXT PRIMARY KEY, next_index INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, created REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE, channel TEXT NOT NULL,
                alias_index INTEGER NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL,
                phase TEXT NOT NULL, data TEXT NOT NULL, export_pending INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS draws (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                ordinal INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
                UNIQUE(account_id, ordinal));
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, account_id TEXT, created REAL NOT NULL,
                status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS actions_account_id ON actions(account_id);
            CREATE INDEX IF NOT EXISTS accounts_run_id ON accounts(run_id);
            CREATE TABLE IF NOT EXISTS allocation_intents (
                run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL UNIQUE, channel TEXT NOT NULL,
                plan TEXT NOT NULL, created REAL NOT NULL);
            PRAGMA user_version=2;
        """)
        self.db.execute("INSERT OR IGNORE INTO counters VALUES('test', ?)", (TEST_START,))
        self.db.execute("INSERT OR IGNORE INTO counters VALUES('formal', ?)", (FORMAL_START,))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def counters(self):
        return dict(self.db.execute("SELECT channel,next_index FROM counters"))

    def create_run(self, preset: str, channel: str, count: int | None):
        if channel not in ("test", "formal") or (count is not None and count < 1):
            raise ValueError("运行配置无效。")
        run = {"id": uuid.uuid4().hex, "preset": preset, "channel": channel, "count": count,
                   "created_at": time.time(), "status": "running", "active_account": None, "completed": 0}
        self.save_run(run)
        return run

    def save_run(self, run):
        self.db.execute("INSERT INTO runs VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data="
                        "json_patch(excluded.data,json_object('cloud_confirmed',"
                        "COALESCE(json_extract(runs.data,'$.cloud_confirmed'),0),'cloud_totals',"
                        "json(COALESCE(json_extract(runs.data,'$.cloud_totals'),'{}'))))",
                        (run["id"], json.dumps(run)))

    def get_run(self, run_id=None):
        row = (self.db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else
               self.db.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT 1").fetchone())
        return json.loads(row[0]) if row else None

    def unfinished_runs(self):
        return [r for (raw,) in self.db.execute("SELECT data FROM runs")
                if (r := json.loads(raw))["status"] not in ("complete", "cancelled")]

    def save_batch(self, batch):
        self.db.execute("INSERT INTO batches VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (batch["id"], batch["created_at"], json.dumps(batch)))

    def get_batch(self, batch_id=None):
        row = (self.db.execute("SELECT data FROM batches WHERE id=?", (batch_id,)).fetchone() if batch_id else
               self.db.execute("SELECT data FROM batches ORDER BY created DESC LIMIT 1").fetchone())
        return json.loads(row[0]) if row else None

    def reserve(self, run: dict, plan: list[dict]):
        """Persist intent, allocate remotely without holding a DB lock, then attach.

        A committed remote allocation is never reclaimed, including when the
        local commit fails. Resume uses the same durable request and account ID.
        """
        channel = run["channel"]
        with self.transaction():
            durable_run = self.get_run(run['id'])
            if durable_run is None:
                raise ValueError('分配请求所属运行不存在。')
            intent = self.db.execute('SELECT * FROM allocation_intents WHERE run_id=?', (run['id'],)).fetchone()
            if intent is None:
                if self.batch_quota_exhausted(durable_run):
                    raise AllocationClosed('批次总额度已分配完毕，当前账号已处理完毕')
                deadline = durable_run.get('allocation_deadline')
                if deadline is not None and time.time() >= deadline:
                    raise AllocationClosed('已到新账号分配截止时间')
                self.db.execute('INSERT INTO allocation_intents VALUES(?,?,?,?,?,?)',
                                (run['id'], uuid.uuid4().hex, uuid.uuid4().hex, channel, json.dumps(plan), time.time()))
                intent = self.db.execute('SELECT * FROM allocation_intents WHERE run_id=?', (run['id'],)).fetchone()
            elif json.loads(intent['plan']) != plan or intent['channel'] != channel:
                raise ValueError('未完成的分配请求配置已改变。')
        allocation = self._allocate(intent['request_id'], intent['account_id'], channel)
        if (allocation['account_id'] != intent['account_id'] or allocation['alias'] !=
                alias_at(allocation['index'], mailbox_identity(allocation['alias']))):
            raise ValueError('云端分配身份与请求不一致。')
        if not alias_is_usable(allocation['alias']):
            raise ValueError('云端分配邮箱的 @ 前长度超过 40 字符；未创建账号，请更新分号服务。')
        index, created_at = allocation['index'], intent['created']
        with self.transaction():
            existing = self.account(intent['account_id'])
            if existing is not None:
                run.update(self.get_run(run['id']))
                return existing
            durable_run = self.get_run(run['id'])
            account = {"id": intent['account_id'], "alias": allocation['alias'], "channel": channel,
                           "alias_index": index, "run_id": run["id"], "status": "running", "phase": "switch_account",
                           "created_at": created_at, "started_at": created_at, "active_seconds": 0.0,
                           "preset": run["preset"], "preset_version": run.get('preset_version'),
                           "plan": plan, "context": {}, "skipped": [], "game_uid": None,
                           "schema_version": SCHEMA_VERSION, "node_id": allocation['node_id'], "recognition_source": "settlement"}
            account.update(instance=run.get("instance", 0), batch_id=run.get("batch_id"))
            account['activity_id'] = allocation.get('activity_id')
            self.db.execute("INSERT INTO accounts(id,alias,channel,alias_index,run_id,status,phase,data) "
                            "VALUES(?,?,?,?,?,?,?,?)", tuple(account[k] for k in
                            ("id", "alias", "channel", "alias_index", "run_id", "status", "phase"))
                            + (json.dumps(account),))
            self.db.execute("UPDATE counters SET next_index=MAX(next_index,?) WHERE channel=?", (index + 1, channel))
            updated_run = dict(durable_run, active_account=account["id"])
            self.save_run(updated_run)
            self.db.execute('DELETE FROM allocation_intents WHERE run_id=?', (run['id'],))
        run.update(updated_run)
        return account

    def _allocate(self, request_id, account_id, channel):
        from ournotes.integrations.cloud_client import CloudClient
        allocator = self.allocator if self.allocator is not None else CloudClient.from_root(self.root)
        return allocator.allocate(request_id, account_id, channel)

    def batch_quota_exhausted(self, run):
        """Called inside reserve's write transaction: in-flight accounts consume slots too.

        Old batches have no shared quota and keep their existing per-lane counts.
        Deleted accounts retain their row, so deletion never frees an allocated slot.
        Only explicit cancellation with a matching batch exclusion frees a slot.
        """
        if self.db.execute('SELECT 1 FROM allocation_intents WHERE run_id=?', (run['id'],)).fetchone():
            return False
        batch = self.get_batch(run['batch_id']) if run.get('batch_id') else None
        if not batch or batch.get('count_mode') != 'shared' or batch.get('total_count') is None:
            return False
        allocated = self.db.execute(
            "SELECT COUNT(*) FROM accounts WHERE run_id IN "
            "(SELECT id FROM runs WHERE json_extract(data, '$.batch_id')=?) "
            "AND NOT (status='cancelled' AND "
            "COALESCE(json_extract(data, '$.batch_exclusion.batch_id'), '')=?)", (batch['id'], batch['id'])
        ).fetchone()[0]
        pending = self.db.execute(
            "SELECT count(*) FROM allocation_intents WHERE run_id IN "
            "(SELECT id FROM runs WHERE json_extract(data,'$.batch_id')=?)", (batch['id'],)
        ).fetchone()[0]
        confirmed = self.db.execute(
            "SELECT COALESCE(SUM(COALESCE(json_extract(data,'$.cloud_confirmed'),0)),0) FROM runs "
            "WHERE json_extract(data,'$.batch_id')=?", (batch['id'],)).fetchone()[0]
        return allocated + pending + confirmed >= batch['total_count']

    def account(self, account_id):
        row = self.db.execute("SELECT data FROM accounts WHERE id=?", (account_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_account(self, account):
        self.db.execute("UPDATE accounts SET status=?,phase=?,data=?,export_pending=1 WHERE id=?",
                        (account["status"], account["phase"], json.dumps(account), account["id"]))

    def draws(self, account_id):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM draws WHERE account_id=? ORDER BY ordinal", (account_id,))]

    def save_draw(self, draw):
        self.db.execute("INSERT INTO draws VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
                        "SET status=excluded.status,data=excluded.data",
                        (draw["id"], draw["account_id"], draw["ordinal"], draw["status"], json.dumps(draw)))
        self.db.execute("UPDATE accounts SET export_pending=1 WHERE id=?", (draw["account_id"],))

    def action(self, account_id, intent):
        action_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO actions VALUES(?,?,?,?,?)",
                        (action_id, account_id, time.time(), "intent", json.dumps(intent)))
        return action_id

    def action_done(self, action_id):
        self.db.execute("UPDATE actions SET status='sent' WHERE id=?", (action_id,))

    def last_sent_action_at(self, account_id, label, phase):
        row = self.db.execute(
            "SELECT created FROM actions WHERE account_id=? AND status='sent' "
            "AND json_extract(data, '$.label')=? AND json_extract(data, '$.phase')=? "
            "ORDER BY created DESC LIMIT 1", (account_id, label, phase)).fetchone()
        return row[0] if row else None

    def export(self, account_id):
        account = self.account(account_id)
        if not account or account.get('deleted_at') or account['status'] == 'deleted':
            return None
        from ournotes.accounts.archive import compact_account, expand_archive
        if account.get('archive'):
            report = expand_archive(self.root, account)
            (self.root / 'results/accounts' / (account_id + '.json')).unlink(missing_ok=True)
            self.db.execute('UPDATE accounts SET export_pending=0 WHERE id=?', (account_id,))
            return report
        report = {k: v for k, v in account.items() if k not in ("context", "game_uid", "uid", "UID", "owner_use")}
        report["draws"] = self.draws(account_id)
        if account['status'] == 'complete' and any(d['status'] != 'complete' for d in report['draws']):
            raise ValueError('成功账号尚有未完成识别，不能上传。')
        cards, counts = {}, Counter()
        for draw in report["draws"]:
            for slot in draw.get("result", {}).get("slots", []):
                if slot.get("status") == "matched":
                    card = slot["card"]
                    counts[card["key"]] += 1
                    cards[card["key"]] = card
        report["summary"] = [dict(cards[k], count=n) for k, n in sorted(counts.items())]
        report["totals"] = {kind: {r: sum(c["count"] for c in report["summary"]
                                                if c["kind"] == kind and c["rarity"] == r)
                                  for r in ("SSR", "SR", "R")} for kind in ("member", "support")}
        report["unknown_slots"] = sum(s.get("status") != "matched" for d in report["draws"]
                                      for s in d.get("result", {}).get("slots", []))
        path = self.root / "results/accounts" / (account_id + ".json")
        atomic_json(path, report)
        if account['status'] == 'complete':
            from ournotes.integrations.cloud_sync import CloudSync
            CloudSync(self.root).enqueue(compact_account(report))
        self.db.execute("UPDATE accounts SET export_pending=0 WHERE id=?", (account_id,))
        return report

    def report(self, account_id):
        account = self.account(account_id)
        if not account or account.get('deleted_at') or account['status'] == 'deleted':
            return None
        if account.get('archive'):
            from ournotes.accounts.archive import expand_archive
            return expand_archive(self.root, account)
        path = self.root / 'results/accounts' / (account_id + '.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def repair_exports(self):
        for row in self.db.execute("SELECT id FROM accounts WHERE export_pending=1").fetchall():
            self.export(row[0])
