import json

import pytest
from PIL import Image

from ournotes.paths import REFERENCE
from ournotes.runtime.pools import AVE, MEMBER, MYGO, SR, SSR, STANDARD, make_plan, validate_confirmation
from ournotes.runtime.store import RuntimeStore
from ournotes.vision.ocr import Observation, TextBox


def test_both_uses_20_tickets_and_40_stars_with_correct_resources():
    plan = make_plan("都抽")
    standard = [d for d in plan if d["pool_key"] == STANDARD]
    assert [d["pulls"] for d in standard] == [10, 10, 10, 10, 10, 10]
    assert [d["resource"] for d in standard] == ["ticket", "ticket", "stars", "stars", "stars", "stars"]
    assert all(d["pool_key"] not in (MYGO, AVE) for d in plan)
    assert [(d["pool_key"], d["pulls"]) for d in plan if d["pool_key"] != STANDARD] == [
        (SSR, 1), (MEMBER, 1)]
    assert sum(d["pulls"] for d in plan) == 62 and len(plan) == 8


def test_mygo_mujica_62_pulls_keep_both_pickups_and_ticket_resources():
    plan = make_plan('mygo + mujica 60 抽')
    assert plan == [
        {'pool_key': key, 'pulls': pulls, 'resource': resource}
        for key, pulls, resource in [
            (MYGO, 10, 'stars'), (MYGO, 10, 'stars'),
            (AVE, 10, 'stars'), (AVE, 10, 'stars'),
            (SSR, 1, 'ticket'), (MEMBER, 1, 'ticket'),
            (STANDARD, 10, 'ticket'), (STANDARD, 10, 'ticket')]]
    assert sum(d['pulls'] for d in plan) == 62


def historical_plan(name):
    data = json.loads((REFERENCE / "gacha-workflow/presets.json").read_text(encoding="utf-8"))
    preset = next(p for p in data["presets"] if p["name"] == name)
    return [{"pool_key": e["pool_key"], "pulls": n,
             "resource": "stars" if e["pool_key"] in (MYGO, AVE) else "ticket"}
            for e in preset["entries"] for n in e["batches"]]


@pytest.mark.parametrize("name", ["MyGO only", "Mujica only"])
def test_other_presets_preserve_original_draws_and_resources(name):
    expected = historical_plan(name)
    if name == 'MyGO only':
        expected = [step for step in expected if step['pool_key'] != SR]
    assert make_plan(name) == expected


def test_saved_account_plan_remains_original_after_preset_change(tmp_path):
    db = RuntimeStore(tmp_path)
    run = db.create_run("都抽", "test", 1)
    account = db.reserve(run, historical_plan("都抽"))
    db.close()
    db = RuntimeStore(tmp_path)
    assert db.account(account["id"])["plan"] == historical_plan("都抽")
    assert db.account(account["id"])["plan"] != make_plan("都抽")
    db.close()


@pytest.mark.parametrize("paid", [0, 1])
def test_standard_stars_keep_paid_balance_guard(paid):
    entry = next(d for d in make_plan("都抽") if d["resource"] == "stars")
    texts = [TextBox("交织的乐章招募 消耗2000个星钻 进行10次招募", 1, (400, 200, 1200, 240))]
    for x in (400, 800):
        texts += [TextBox("付费", 1, (x, 450, x + 40, 470)),
                  TextBox(str(paid), 1, (x + 90, 450, x + 110, 470))]
    observation = Observation(Image.new("RGB", (1600, 900)), texts, "draw_confirm", "test")
    if paid:
        with pytest.raises(ValueError, match="付费星钻"):
            validate_confirmation(observation, entry)
    else:
        assert validate_confirmation(observation, entry)["resource"] == "stars"
        with pytest.raises(ValueError, match="支付方式"):
            validate_confirmation(observation, {**entry, "resource": "ticket"})
