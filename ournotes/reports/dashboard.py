"""Fresh, read-only statistics for the local dashboard; never repair an archive."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import statistics
from collections import Counter
from datetime import UTC, datetime

from ournotes.reports.pickups import METHOD, pickup_count, plans_from_database

METRICS = {
    "ssr_total": "SSR 总张数",
    "mygo_characters": "不同 MyGO SSR 角色数",
    "mujica_characters": "不同 Mujica SSR 角色数",
    "unique_characters": "不同 SSR 角色数",
    "current_up_types": "当期 up 种数",
}


def report(root, index, scope="current"):
    index.refresh()
    snapshot = index.snapshot
    if not snapshot.available:
        raise ValueError("账号目录不可读，无法生成当前统计。")
    batches, runs, bindings = [], {}, {}
    database = root / "state/runtime.sqlite3"
    if database.is_file():
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            batches = [json.loads(r[0]) for r in db.execute("SELECT data FROM batches ORDER BY created DESC")]
            runs = {r["id"]: r for (raw,) in db.execute("SELECT data FROM runs") if (r := json.loads(raw))}
            plans = plans_from_database(db)
            bindings = {
                i: {"status": status, "run": runs.get(run_id, {}), "plan": plans.get(identifier)}
                for i, status, run_id, identifier in db.execute("SELECT alias_index,status,run_id,id FROM accounts")
            }
    selected = (
        (batches[0] if batches else None)
        if scope == "current"
        else next((b for b in batches if b["id"] == scope), None)
    )
    if scope not in ("current", "all") and selected is None:
        raise ValueError("批次不存在。")
    run_ids = {l["run_id"] for l in selected["lanes"]} if selected else set()
    mapped = [
        a
        for a in snapshot.accounts
        if (scope == "all" or bindings.get(a["index"], {}).get("run", {}).get("id") in run_ids)
        and bindings.get(a["index"], {}).get("status", "complete") == "complete"
    ]
    members = json.loads((root / "data/catalog/site/raw/MasterMemberCard.json").read_text("utf-8"))["_allData"]
    characters = json.loads((root / "data/catalog/site/raw/MasterCharacter.json").read_text("utf-8"))["_allData"]
    member_characters = {c["_id"]: c["_characterID"] for c in members}
    bands = {c["_id"]: c["_bandID"] for c in characters}
    rows, frequencies = [], {k: dict(c, copies=0, accounts=0) for k, c in index.cards.items()}
    for account in mapped:
        held = account["cards"]
        try:
            identities = {
                member_characters[index.cards[c["key"]]["id"]]
                for c in held
                if index.cards[c["key"]]["kind"] == "member"
            }
            row = {k: account[k] for k in ("index", "ssr_total")}
            run = bindings.get(account["index"], {}).get("run", {})
            row.update(
                preset=run.get("preset", "未记录预设"),
                instance=run.get("instance"),
                mygo_characters=sum(bands[c] == 1 for c in identities),
                mujica_characters=sum(bands[c] == 2 for c in identities),
                unique_characters=len(identities),
                unique_cards=len(held),
                duplicates=account["ssr_total"] - len(held),
                current_up_types=pickup_count((c['key'] for c in held),
                                              bindings.get(account['index'], {}).get('plan')),
            )
        except KeyError as error:
            raise ValueError("角色映射不完整，无法准确统计不同角色数。") from error
        rows.append(row)
        for card in held:
            frequencies[card["key"]]["copies"] += card["count"]
            frequencies[card["key"]]["accounts"] += 1
    metrics = []
    for key, label in METRICS.items():
        values = [r[key] for r in rows if r[key] is not None]
        counts = Counter(values)
        high = max(values) if values else None
        metrics.append(
            {
                "key": key,
                "label": label,
                "n": len(values),
                "max": high,
                "indices": sorted(r["index"] for r in rows if high is not None and r[key] == high),
                "mean": statistics.mean(values) if values else None,
                "histogram": [
                    {"value": n, "count": counts[n], "percent": counts[n] / len(values) * 100}
                    for n in range(max(values) + 1)
                ]
                if values
                else [],
            }
        )
    groups = []
    for preset in sorted({r["preset"] for r in rows}):
        members = [r for r in rows if r["preset"] == preset]
        groups.append(
            {
                "preset": preset,
                "n": len(members),
                "means": {k: statistics.mean(v) if (v := [r[k] for r in members if r[k] is not None]) else None for k in METRICS},
                "with_duplicates_percent": sum(r["duplicates"] > 0 for r in members) / len(members) * 100,
            }
        )
    for card in frequencies.values():
        card["percent"] = card["accounts"] / len(rows) * 100 if rows else 0
        card["image"] = "/cards" + card["image"]
    target = None
    if selected:
        counts = [runs.get(l["run_id"], {}).get("count") for l in selected["lanes"]]
        target = (
            selected.get("total_count")
            if selected.get("count_mode") == "shared"
            else (sum(counts) if all(n is not None for n in counts) else None)
        )
    sampled = {r["index"] for r in rows}
    batch_accounts = {i: v for i, v in bindings.items() if v["run"].get("id") in run_ids}
    outside = [
        {
            "index": i,
            "reason": "档案缺失或未通过校验"
            if v["status"] == "complete"
            else ("已删除" if v["status"] == "deleted" else "未完整完成"),
        }
        for i, v in sorted(batch_accounts.items())
        if i not in sampled
    ]
    return {
        "scope": scope,
        "batch_id": selected["id"] if selected else None,
        "batch_status": selected["status"] if selected else None,
        "target": target,
        "sample_size": len(rows),
        "recorded_complete": sum(v["status"] == "complete" for v in batch_accounts.values())
        if selected
        else None,
        "unallocated": max(0, target - len(batch_accounts)) if target is not None else None,
        "not_in_sample": outside,
        "loaded_at": snapshot.loaded_at,
        "read_at": datetime.now(UTC).isoformat(),
        "excluded_files": snapshot.excluded,
        "available_files": snapshot.files,
        "missing_or_incomplete": len(outside) if selected else snapshot.excluded,
        "batches": [{"id": b["id"], "status": b["status"], "created_at": b["created_at"]} for b in batches],
        "metrics": metrics,
        "groups": groups,
        "accounts": sorted(rows, key=lambda r: (-r["ssr_total"], r["index"])),
        "cards": sorted(frequencies.values(), key=lambda c: (c["kind"], -c["copies"], c["id"])),
        "method": "只读取现存、完整且有效的账号档案；缺失或删除的档案不补回。角色按角色ID去重，支援卡不计入角色数。频次不等于真实抽取概率。" + METHOD,
    }
