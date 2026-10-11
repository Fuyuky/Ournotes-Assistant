from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from email.header import decode_header, make_header
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path

from ournotes.common.identity import MAILBOX, mailbox_identity


class NeedsReview(RuntimeError):
    pass


def validate_alias(alias: str, mailbox: str = MAILBOX) -> str:
    alias = alias.strip().lower()
    if alias.count("@") != 1:
        raise ValueError("邮箱格式无效。")
    local, domain = alias.split("@")
    if (domain != "gmail.com" or not re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9]+)*", local)
            or mailbox_identity(alias) != mailbox_identity(mailbox)):
        raise ValueError("只允许指定 Gmail 邮箱的点号别名。")
    return alias


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "div", "tr", "td"):
            self.fragments.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "tr", "td"):
            self.fragments.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.fragments.append(data)


def message_text(payload: dict) -> str:
    if payload.get("filename"):
        return ""  # Never fetch or parse attachments as verification codes.
    if payload.get("parts"):
        return "\n".join(message_text(part) for part in payload["parts"])
    if payload.get("mimeType") not in ("text/plain", "text/html"):
        return ""
    body = payload.get("body", {}).get("data", "")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    content_type = next((h["value"] for h in payload.get("headers", [])
                         if h["name"].lower() == "content-type"), "")
    charset = re.search(r'charset=["\']?([\w-]+)', content_type, re.IGNORECASE)
    try:
        text = raw.decode(charset.group(1) if charset else "utf-8")
    except (UnicodeError, LookupError):
        raise NeedsReview("验证码邮件编码无法确定，请人工检查。") from None
    if payload["mimeType"] == "text/html":
        parser = TextExtractor()
        parser.feed(text)
        text = "".join(parser.fragments)
    return text


@dataclass(frozen=True)
class MailRule:
    senders: tuple[str, ...]
    subject_pattern: str
    code_pattern: str

    @classmethod
    def load(cls, path: Path):
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        rule = cls(tuple(config["senders"]), config["subject_pattern"], config["code_pattern"])
        rule.validate()
        return rule

    def validate(self):
        if not self.senders or any(not re.fullmatch(r"[a-zA-Z0-9_.+%-]+@[a-zA-Z0-9.-]+", s)
                                   for s in self.senders):
            raise ValueError("需要先填写游戏实际验证码发件地址。")
        if not self.subject_pattern or not self.code_pattern:
            raise ValueError("需要先填写游戏实际邮件主题和验证码格式。")
        re.compile(self.subject_pattern)
        if "code" not in re.compile(self.code_pattern).groupindex:
            raise ValueError("验证码正则须含 (?P<code>...) 命名分组。")

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def query(self, after: float):
        self.validate()
        senders = " ".join("from:" + s for s in self.senders)
        return "{" + senders + "} after:" + str(int(after))


def extract_code(message: dict, alias: str, since_ms: int, rule: MailRule, *, mailbox: str = MAILBOX) -> str | None:
    if int(message.get("internalDate", 0)) < since_ms:
        return None
    payload = message.get("payload", {})
    headers: dict[str, list[str]] = {}
    for header in payload.get("headers", []):
        headers.setdefault(header["name"].lower(), []).append(header["value"])
    senders = {addr.lower() for _, addr in getaddresses(headers.get("from", []))}
    if len(senders) != 1 or not senders.issubset({s.lower() for s in rule.senders}):
        return None
    subjects = headers.get("subject", [])
    if len(subjects) != 1 or not re.search(rule.subject_pattern, str(make_header(decode_header(subjects[0])))):
        return None
    # Prefer original To. Delivered-To may have had dots removed by Gmail.
    recipients = {addr.lower() for _, addr in getaddresses(headers.get("to", []))}
    relevant = {addr for addr in recipients if "@" in addr and
                mailbox_identity(addr) == mailbox_identity(mailbox)}
    if not relevant:
        delivery_headers = [value for key in ("delivered-to", "x-original-to", "envelope-to")
                            for value in headers.get(key, [])]
        relevant = {addr.lower() for _, addr in getaddresses(delivery_headers)
                    if "@" in addr and mailbox_identity(addr) == mailbox_identity(mailbox)}
    if alias not in relevant:
        return None
    if relevant != {alias}:
        raise NeedsReview("一封邮件包含多个目标邮箱别名，无法自动归属。")
    codes = {match.group("code") for match in re.finditer(rule.code_pattern, message_text(payload))}
    codes.discard("")
    if len(codes) > 1:
        raise NeedsReview("邮件出现多个验证码候选，请人工检查。")
    return next(iter(codes)) if codes else None
