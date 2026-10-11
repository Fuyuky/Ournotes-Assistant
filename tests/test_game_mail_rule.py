"""Synthetic mail with the sender, subject and wording observed in the game test."""

import base64
from pathlib import Path

import pytest

from ournotes.common.identity import MAILBOX
from ournotes.common.otp import MailRule, extract_code

ALIAS = "ourn.otesexamplemailboxfortests@gmail.com"
RULE = MailRule.load(Path(__file__).resolve().parents[1] / "deploy/cloud/mail-rule.example.json")


def game_message(body, sender="help@intl-service.bilibili.com"):
    return {
        "internalDate": "2000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": f"bilibili <{sender}>"},
                {"name": "Subject", "value": "【bilibili】请验证您的邮箱地址  "},
                {"name": "To", "value": f'"{ALIAS.split("@")[0]}" <{ALIAS}>'},
                {"name": "Delivered-To", "value": MAILBOX},
            ],
            "parts": [{
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
            }],
        },
    }


def test_observed_game_wording_preserves_zeroes_and_alias_isolation():
    item = game_message("<div>您的验证码是:&nbsp;&nbsp;<b>001234</b></div>"
                        "<p>您正在使用bilibili的邮箱验证功能。</p>")
    assert extract_code(item, ALIAS, 1000, RULE) == "001234"
    assert extract_code(item, "ournotes.examplemailboxfortests@gmail.com", 1000, RULE) is None
    assert extract_code(item, ALIAS, 2001, RULE) is None


@pytest.mark.parametrize("body", [
    "您的验证码是: 01234",
    "您的验证码是: 0012345",
    "您的验证码是: 001234A",
    "用户编号: 001234",
])
def test_game_rule_does_not_guess_numbers_or_partial_codes(body):
    assert extract_code(game_message(body), ALIAS, 1000, RULE) is None


@pytest.mark.parametrize("sender", ["help@intl-service.bilibili.com", "help@biligames.co", "help@biligames.cc", "help@biligames.vip"])
def test_all_observed_senders(sender):
    item = game_message("您的验证码是: 001234", sender=sender)
    assert extract_code(item, ALIAS, 1000, RULE) == "001234"


def test_other_sender_is_not_in_the_live_rule():
    item = game_message("您的验证码是: 001234", sender="help@unrelated.example")
    assert extract_code(item, ALIAS, 1000, RULE) is None


def test_gmail_search_includes_spam_and_trash_but_keeps_query_filter(tmp_path):
    from unittest.mock import Mock

    from ournotes.cloud.mail import GmailBackend, SecretVault
    client = GmailBackend(SecretVault(tmp_path / 'private/oauth.json'),
                          'http://127.0.0.1:7890', MAILBOX, lambda: 1000)
    client.get = Mock(side_effect=[
        {'messages': [{'id': 'spam-game-message'}], 'nextPageToken': 'page2'},
        {'messages': [{'id': 'trash-game-message'}]},
    ])
    query = RULE.query(1000)
    assert client.message_ids(query) == {'spam-game-message', 'trash-game-message'}
    assert client.get.call_count == 2
    for call, page in zip(client.get.call_args_list, (None, 'page2')):
        assert call.args == ('messages', {'q': query, 'maxResults': 100,
                                         'pageToken': page, 'includeSpamTrash': 'true'})
