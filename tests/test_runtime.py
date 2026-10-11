import json
from pathlib import Path

import pytest

from ournotes.reports.account import format_account
from ournotes.runtime.pools import PRESETS, make_plan
from ournotes.runtime.store import FORMAL_START, RuntimeStore, alias_at


def test_disjoint_contiguous_alias_channels(tmp_path):
    assert alias_at(8) == "ourn.otesexamplemailboxfortests@gmail.com"
    store = RuntimeStore(tmp_path)
    test_run = store.create_run("都抽", "test", 2)
    a, b = [store.reserve(test_run, make_plan("都抽")) for _ in range(2)]
    formal_run = store.create_run("都抽", "formal", 1)
    c = store.reserve(formal_run, make_plan("都抽"))
    assert [a["alias_index"], b["alias_index"], c["alias_index"]] == [8, 9, FORMAL_START]
    assert len({a["alias"], b["alias"], c["alias"]}) == 3
    store.close()
    store = RuntimeStore(tmp_path)
    assert store.counters() == {"test": 10, "formal": FORMAL_START + 1}
    assert store.get_run(formal_run["id"])["active_account"] == c["id"]
    store.close()


def test_reservation_rolls_back_counter_and_account_on_crash(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    run = store.create_run("都抽", "test", 1)
    def fail(_):
        raise RuntimeError("crash before commit")
    monkeypatch.setattr(store, "save_run", fail)
    with pytest.raises(RuntimeError):
        store.reserve(run, make_plan("都抽"))
    assert store.counters()["test"] == 8
    assert store.db.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("preset", [name for name in PRESETS if sum(e["pulls"] for e in make_plan(name)) == 43])
def test_presets_use_only_ten_pulls_for_multiples_of_ten(preset):
    plan = make_plan(preset)
    assert sum(e["pulls"] for e in plan) == 43
    assert len(plan) == 7
    assert sorted(e["pulls"] for e in plan) == [1, 1, 1, 10, 10, 10, 10]
    assert plan[-1]["resource"] == "ticket"


def test_result_export_retains_r_and_is_idempotent(tmp_path):
    store = RuntimeStore(tmp_path)
    run = store.create_run("都抽", "test", 1)
    a = store.reserve(run, make_plan("都抽"))
    card = {"key": "member:1", "kind": "member", "rarity": "R", "id": 1, "name": "测试", "title": "测试"}
    draw = {"id": "same-transaction", "account_id": a["id"], "ordinal": 0,
            "status": "complete", "result": {"slots": [{"status": "matched", "card": card}] * 10}}
    store.save_draw(draw)
    store.save_draw(draw)
    result = store.export(a["id"])
    assert result["totals"]["member"]["R"] == 10
    assert result["summary"][0]["count"] == 10
    assert len(result["draws"]) == 1
    path = tmp_path / "results/accounts" / (a["id"] + ".json")
    path.write_text("broken export", encoding="utf-8")
    store.save_account(a)
    store.repair_exports()
    assert json.loads(path.read_text(encoding="utf-8"))["alias"] == a["alias"]
    assert "R 【" not in format_account(result)
    store.close()


def test_export_failure_remains_pending(tmp_path, monkeypatch):
    import ournotes.runtime.store as module
    store = RuntimeStore(tmp_path)
    run = store.create_run("都抽", "test", 1)
    a = store.reserve(run, make_plan("都抽"))
    def fail(*_):
        raise OSError("disk full")
    monkeypatch.setattr(module, "atomic_json", fail)
    with pytest.raises(OSError):
        store.export(a["id"])
    assert store.db.execute("SELECT export_pending FROM accounts").fetchone()[0] == 1
    store.close()


def test_test_counter_cannot_cross_into_formal_range(tmp_path):
    store = RuntimeStore(tmp_path)
    store.db.execute("UPDATE counters SET next_index=? WHERE channel='test'", (FORMAL_START,))
    run = store.create_run("都抽", "test", 1)
    with pytest.raises(ValueError, match="耗尽"):
        store.reserve(run, make_plan("都抽"))
    store.close()


def test_no_runtime_dependency_on_codex_output():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "ournotes").glob("*.py"):
        assert "20-mumu-1-windows-adb-2" not in path.read_text(encoding="utf-8")
