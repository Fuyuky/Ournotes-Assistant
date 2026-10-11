import base64
import copy
import threading
from types import SimpleNamespace

import pytest

from ournotes.common.otp import MailRule, NeedsReview, extract_code, validate_alias
from ournotes.common.secrets import SecretStore
from ournotes.integrations.mumu import MuMuSdk
from ournotes.common.identity import MAILBOX

ALIAS = "ourn.otesexamplemailboxfortests@gmail.com"
OTHER = "ournotes.examplemailboxfortests@gmail.com"
RULE = MailRule(("game@example.test",), r"^Test verification$", r"Code:\s*(?P<code>[0-9]{6})(?![0-9])")


def message(alias=ALIAS, text="Code: 001234", timestamp=2000):
    return {"internalDate": str(timestamp), "payload": {
        "mimeType": "text/plain", "headers": [
            {"name": "From", "value": "Game <game@example.test>"},
            {"name": "Subject", "value": "Test verification"},
            {"name": "To", "value": alias},
            {"name": "Delivered-To", "value": MAILBOX}],
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")}}}


def test_alias_is_exact_even_when_delivery_header_is_canonical():
    assert extract_code(message(), ALIAS, 1000, RULE) == "001234"
    assert extract_code(message(OTHER), ALIAS, 1000, RULE) is None


def test_old_wrong_sender_and_ambiguous_message_rejected():
    assert extract_code(message(timestamp=999), ALIAS, 1000, RULE) is None
    wrong = message()
    wrong["payload"]["headers"][0]["value"] = "Game <other@example.test>"
    assert extract_code(wrong, ALIAS, 1000, RULE) is None
    with pytest.raises(NeedsReview):
        extract_code(message(text="Code: 001234\nCode: 005678"), ALIAS, 1000, RULE)
    with pytest.raises(NeedsReview):
        extract_code(message(alias=f"{ALIAS}, {OTHER}"), ALIAS, 1000, RULE)


def test_multipart_html_and_attachment_handling():
    item = message()
    plain = copy.deepcopy(item["payload"])
    html = copy.deepcopy(plain)
    html["mimeType"] = "text/html"
    html["body"]["data"] = base64.urlsafe_b64encode(
        b"<style>Code: 999999</style><div>Code: <b>001234</b></div>").decode()
    attachment = copy.deepcopy(plain)
    attachment["filename"] = "unrelated.txt"
    attachment["body"]["data"] = base64.urlsafe_b64encode(b"Code: 999999").decode()
    item["payload"] = {"headers": plain["headers"], "mimeType": "multipart/mixed",
                       "parts": [plain, html, attachment]}
    assert extract_code(item, ALIAS, 1000, RULE) == "001234"


@pytest.mark.parametrize("alias", ["a@gmail.com", "." + MAILBOX,
                                     MAILBOX.replace("@", "..@"), MAILBOX.replace("@", "+x@")])
def test_only_the_requested_mailbox_dot_aliases_allowed(alias):
    with pytest.raises(ValueError):
        validate_alias(alias)


def test_dpapi_roundtrip_and_process_lock(tmp_path):
    store = SecretStore(tmp_path)
    value = {"refresh_token": "not-a-real-token-but-must-be-encrypted", "汉字": "测试"}
    store.save("test", value)
    assert store.load("test") == value
    assert value["refresh_token"].encode() not in (tmp_path / "test.dpapi").read_bytes()
    with store.lock("gmail"), pytest.raises(RuntimeError), SecretStore(tmp_path).lock("gmail"):
        pytest.fail("concurrent use allowed")


class FakeMail:
    def __init__(self):
        self.messages = {"old": message(timestamp=0)}
        self.read = []

    def message_ids(self, *_):
        return set(self.messages)

    def message(self, mid, *_):
        self.read.append(mid)
        return self.messages[mid]














def test_touch_failure_still_releases_and_never_retries():
    device = object.__new__(MuMuSdk)
    device._lock = threading.RLock()
    device._handle = 1
    device._display = lambda: 0
    device._dimensions = lambda _: (100, 100)
    events = []
    def down(*_):
        events.append("down")
        return 1
    def up(*_):
        events.append("up")
        return 0
    device._dll = SimpleNamespace(nemu_input_event_finger_touch_down=down,
                                  nemu_input_event_finger_touch_up=up)
    with pytest.raises(ValueError):
        device.tap(101, 0)
    assert events == []
    with pytest.raises(RuntimeError):
        device.tap(10, 10)
    assert events == ["down", "up"]
