from pathlib import Path

import pytest
from PIL import Image

from ournotes.paths import REFERENCE
from ournotes.runtime.pools import AVE, MEMBER, MYGO, SR, SSR, STANDARD, PoolFinder, validate_confirmation
from ournotes.vision.ocr import Observation, TextBox, Vision


@pytest.fixture(scope="module")
def vision():
    return Vision()


@pytest.mark.parametrize(("name", "pool_key", "pulls", "resource"), [
    ("03-mygo-confirm.png", MYGO, 10, "stars"), ("14-ave-confirm.png", AVE, 10, "stars"),
    ("21-ssr-confirm.png", SSR, 1, "ticket"), ("27-member-confirm.png", MEMBER, 1, "ticket"),
    ("33-sr-confirm.png", SR, 1, "ticket"), ("38-standard-1-confirm.png", STANDARD, 10, "ticket"),
])
def test_confirmation_identity_count_and_payment(vision, name, pool_key, pulls, resource):
    image = Image.open(REFERENCE / "gacha-workflow/screens" / name)
    o = vision.read(image)
    # Historical screenshots describe their own transaction, not the current preset order.
    entry = {"pool_key": pool_key, "pulls": pulls, "resource": resource}
    assert o.scene == "draw_confirm"
    validate_confirmation(o, entry)
    with pytest.raises(ValueError):
        validate_confirmation(o, {**entry, "pulls": 99})
    with pytest.raises(ValueError):
        validate_confirmation(o, {**entry, "pool_key": AVE if pool_key != AVE else MYGO})


def test_pool_identity_survives_sidebar_reordering():
    finder = PoolFinder()
    base = REFERENCE / "pool-survey/screens"
    expected = {
        "01-mygo-main.png": MYGO, "04-ave-mujica-main.png": AVE,
        "07-ssr-guaranteed-ticket-main.png": "launch-ssr-guaranteed-ticket",
        "11-ssr-member-ticket-main.png": "launch-ssr-member-guaranteed-ticket",
        "15-sr-plus-ticket-main.png": "launch-sr-plus-guaranteed-ticket",
        "19-standard-main.png": "our-notes-standard",
    }
    for file, key in expected.items():
        current, positions, _ = finder.scan(Image.open(base / file))
        assert current == key
        assert key in positions


@pytest.mark.parametrize(("file", "scene"), [
    ("20-home-after-gifts.png", "home"), ("17-claim-all-result.png", "gift_confirm"),
    ("18-claim-all-confirmed.png", "gift_received"), ("19-inbox-empty.png", "gift_box"),
    ("14-retry-announcement.png", "notice"), ("03-login-reward-center.png", "startup_reward"),
])
def test_known_foreground_screens(vision, file, scene):
    o = vision.read(Image.open(REFERENCE / "game-onboarding" / file))
    assert o.scene == scene


def test_blank_is_unknown(vision):
    assert vision.read(Image.new("RGB", (1600, 900))).scene == "unknown"


def test_conflicting_title_and_sidebar_is_a_transition():
    image = Image.open(REFERENCE / "runtime-regressions/pool-transition-conflict.png")
    current, _, _ = PoolFinder().scan(image)
    assert current is None


def test_live_home_menu(vision):
    image = Image.open(REFERENCE / "runtime-regressions/home-menu.png")
    assert vision.read(image).scene == "home_menu"


def test_character_prompt_can_interrupt_logout(vision):
    image = Image.open(REFERENCE / "runtime-regressions/character-dialogue.png")
    assert vision.read(image).scene == "character_dialogue"


def test_split_button_text_uses_only_adjacent_boxes():
    o = Observation(Image.new("RGB", (1600, 900)), [
        TextBox("TAP TO", .99, (1028, 714, 1192, 751)),
        TextBox("START", .98, (1200, 714, 1348, 751)),
    ], "title", "test")
    assert o.find("TAP TO START").center == (1188, 732)
    o.texts[1].box = (1200, 514, 1348, 551)
    assert o.find("TAP TO START") is None


def test_ui_rules_have_no_business_sleep_calls():
    root = Path(__file__).resolve().parents[1]
    # Cloud transport backoff runs in a worker and is independently bounded by
    # test_mail_transport; it does not infer completion of a game UI operation.
    for name in ("runtime/runner.py", "runtime/login_flow.py", "runtime/reward_flow.py", "runtime/draw_flow.py", "vision/ocr.py", "runtime/pools.py"):
        assert "time.sleep(" not in (root / "ournotes" / name).read_text(encoding="utf-8")
