"""Read-only runtime projection, including WAL; never constructs RuntimeStore."""
import contextlib
import hashlib
import json
import sqlite3
import time
from pathlib import Path

TERMINAL = {"complete", "cancelled"}

def read_json(path, fallback=None):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return fallback

def redact(value):
    from ournotes.control.launcher import redact as hide
    return hide(value)

class RuntimeQuery:
    def __init__(self, root, index=None):
        self.root, self.index = Path(root).resolve(), index

    def runtime(self):
        result = {"batch": None, "lanes": [], "counters": {}, "error": None, "observed_at": time.time(), "unresolved": [], "version": hashlib.sha256(b"empty").hexdigest()}
        database = self.root / "state/runtime.sqlite3"
        if not database.exists():
            return result
        try:
            with contextlib.closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
            ) as db:
                db.execute("BEGIN")
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                account_columns = {r[1] for r in db.execute('PRAGMA table_info(accounts)')}
                row = db.execute("SELECT data FROM batches ORDER BY created DESC LIMIT 1").fetchone()
                batch = json.loads(row[0]) if row else None
                if batch:
                    result["batch"] = {
                        k: batch.get(k)
                        for k in ("id", "status", "created_at", "active_seconds", "async_start", "debug_mode", "count_mode", "total_count")
                    }
                    for lane in batch["lanes"]:
                        raw = db.execute("SELECT data FROM runs WHERE id=?", (lane["run_id"],)).fetchone()
                        run = json.loads(raw[0]) if raw else {}
                        rows = db.execute(
                            "SELECT alias_index,status FROM accounts WHERE run_id=?", (lane["run_id"],)
                        ).fetchall()
                        active = db.execute(
                            "SELECT alias_index,phase FROM accounts WHERE id=?", (run.get("active_account"),)
                        ).fetchone()
                        account_row = (db.execute('SELECT data FROM accounts WHERE id=?',
                                       (run.get('active_account'),)).fetchone()
                                       if 'data' in account_columns else None)
                        account = json.loads(account_row[0]) if account_row else None
                        unresolved = (db.execute(
                            "SELECT d.id,d.status FROM draws d JOIN accounts a ON a.id=d.account_id "
                            "WHERE a.run_id=? AND NOT (a.status='cancelled' AND json_extract(a.data, '$.submission_abandoned') IS NOT NULL) AND d.status IN ('submitted','captured','needs_review')",
                            (lane['run_id'],)).fetchall() if 'draws' in tables else [])
                        from ournotes.control.recovery import lane_recovery
                        recovery = lane_recovery(batch, run, account,
                            [{'id': r[0], 'status': r[1]} for r in unresolved])
                        result["lanes"].append(
                            {
                                "instance": lane["instance"],
                                "preset": lane["preset"],
                                "run_id": lane["run_id"],
                                "channel": run.get("channel"),
                                "target": run.get("count"),
                                "status": run.get("status", "unknown"),
                                "processed": run.get("completed", 0),
                                "complete": run.get('cloud_confirmed', 0),
                                "legacy_recorded_complete": sum(status == 'complete' for _, status in rows),
                                "partial": sum(status == "partial" for _, status in rows),
                                "index": active[0] if active else None,
                                "phase": active[1] if active else None,
                                "message": redact(run.get("closed_reason") or
                                    (run.get("pause_reason", "") if run.get("status") == "paused" else "")),
                                "transactions": 0,
                                "mail_limit": bool(run.get("mail_send_limit_reached") or "今日邮箱发送次数已达上限" in run.get("pause_reason", "")),
                                "recovery_hold": redact(run.get("recovery_hold") or ""),
                                "pause_reason": redact(run.get("pause_reason") or "") if run.get('status') == 'paused' else '',
                                "recovery": recovery,
                            }
                        )
                result["counters"] = dict(db.execute("SELECT channel,next_index FROM counters"))
                if batch and "draws" in tables:
                    run_ids = [lane["run_id"] for lane in result["lanes"]]
                    for run_id in run_ids:
                        rows = db.execute("SELECT d.id,d.status,d.account_id FROM draws d JOIN accounts a ON a.id=d.account_id WHERE a.run_id=? AND NOT (a.status='cancelled' AND json_extract(a.data, '$.submission_abandoned') IS NOT NULL) AND d.status IN ('submitted','captured','needs_review')", (run_id,)).fetchall()
                        result["unresolved"].extend({"transaction_id": r[0], "status": r[1], "account_id": r[2], "run_id": run_id} for r in rows)
                if batch:
                    targets = [lane['target'] for lane in result['lanes']]
                    result['batch']['target'] = (batch.get('total_count') if batch.get('count_mode') == 'shared'
                        else sum(targets) if all(n is not None for n in targets) else None)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            result["error"] = "运行数据库暂时不可读，运行操作已禁用。"
            return result
        current = read_json(self.root / "state/runner-status.json", {})
        batch = result["batch"]
        result['recovery_protocol'] = current.get('recovery_protocol', 0) if batch and current.get('batch_id') == batch['id'] else 0
        if batch and batch["status"] not in TERMINAL and (
            current.get("batch_id") == batch["id"]
        ):
            for lane in result["lanes"]:
                live = next(
                    (r for r in current.get("lanes", [current]) if r.get("run_id") == lane["run_id"]), {}
                )
                for key in ("phase", "scene", "transactions", "observation"):
                    if key in live:
                        lane[key] = live[key]
                if lane["status"] == "running":
                    lane["message"] = redact(live.get("message", ""))
        result["version"] = hashlib.sha256(json.dumps({k: v for k, v in result.items() if k not in ("observed_at", "version")}, sort_keys=True).encode()).hexdigest()
        return result
