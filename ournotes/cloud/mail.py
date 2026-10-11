"""Per-account Gmail tickets; all network work occurs outside SQLite transactions."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from ournotes.cloud.durability import sync_directory
from ournotes.cloud.store import Conflict, Forbidden, Unavailable, encoded
from ournotes.common.otp import MailRule, NeedsReview, extract_code


class GmailAuthorizationRequired(Unavailable):
    """Google rejected the refresh grant; transport retries cannot renew consent."""


class SecretVault:
    """Restricted server-private file; never copied into results or publication directories."""
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)

    def load(self):
        with self.lock:
            try:
                return json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
            except PermissionError:
                raise Unavailable("Server Gmail credential permissions require maintenance") from None

    def save(self, value):
        with self.lock:
            previous = self.path.stat() if self.path.exists() else None
            temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    # A root maintenance refresh must not replace a service-owned file
                    # with a root-only one and break the next daemon token read.
                    if previous is not None and hasattr(os, "fchown"):
                        os.fchown(handle.fileno(), previous.st_uid, previous.st_gid)
                    handle.write(encoded(value))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                sync_directory(self.path.parent)
                if os.name != "nt":
                    os.chmod(self.path, 0o600)
            finally:
                temporary.unlink(missing_ok=True)


class GmailBackend:
    """Official Gmail REST with explicit local proxy for reads and OAuth refresh alike."""
    def __init__(self, vault, proxy_url, mailbox, clock):
        self.vault, self.mailbox, self.clock = vault, mailbox, clock
        parsed = urlsplit(proxy_url)
        if parsed.scheme not in ("http", "socks5h") or parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("Gmail requires a server-local proxy")
        self.proxy_url = proxy_url
        self.local = threading.local()
        self.authorization_required = False

    @contextmanager
    def bounded(self):
        previous = getattr(self.local, "deadline", None)
        self.local.deadline = time.monotonic() + 20
        try:
            yield
        finally:
            self.local.deadline = previous

    def session(self):
        import requests
        session = requests.Session()
        session.trust_env = False
        session.proxies = {"http": self.proxy_url, "https": self.proxy_url}
        return session

    def request(self, method, url, **kwargs):
        import requests
        try:
            remaining = max(0, (getattr(self.local, "deadline", None) or time.monotonic() + 15)
                            - time.monotonic())
            if remaining < 0.05:
                raise TimeoutError
            with self.session() as session:
                response = session.request(method, url, timeout=(min(5, remaining), min(10, remaining)), **kwargs)
                response.raise_for_status()
                return response.json()
        except (requests.RequestException, OSError, ValueError) as error:
            response = getattr(error, 'response', None)
            if url == 'https://oauth2.googleapis.com/token' and getattr(response, 'status_code', None) == 400:
                try:
                    rejected_grant = response.json().get('error') == 'invalid_grant'
                except (ValueError, AttributeError):
                    rejected_grant = False
                if rejected_grant:
                    self.authorization_required = True
                    credentials = self.vault.load()
                    if credentials:
                        self.vault.save({**credentials, 'authorization_required': True})
                    logging.getLogger(__name__).warning('Gmail authorization requires renewal: invalid_grant')
                    raise GmailAuthorizationRequired('Server Gmail authorization requires renewal') from None
            # Classify known TLS failures without logging URLs, headers or exception text.
            tls = 'none'
            if isinstance(error, requests.exceptions.SSLError):
                detail = str(error).upper()
                tls = next((name for marker, name in (
                    ('CERTIFICATE_VERIFY_FAILED', 'certificate_verify_failed'),
                    ('UNEXPECTED_EOF', 'unexpected_eof'),
                    ('HANDSHAKE_FAILURE', 'handshake_failure'),
                    ('WRONG_VERSION_NUMBER', 'wrong_version'),
                ) if marker in detail), 'other')
            logging.getLogger(__name__).warning(
                'Gmail/OAuth transport failure: %s HTTP=%s service=%s tls=%s', type(error).__name__,
                getattr(getattr(error, 'response', None), 'status_code', None),
                'oauth' if url == 'https://oauth2.googleapis.com/token' else 'gmail', tls)
            raise Unavailable("Gmail/OAuth service unavailable; ticket remains unchanged") from None

    def import_credentials(self, credentials):
        allowed = {"token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "expiry"}
        if (set(credentials) - allowed - {'universe_domain', 'account'}
                or credentials.get('universe_domain', 'googleapis.com') != 'googleapis.com'
                or credentials.get("token_uri", "https://oauth2.googleapis.com/token") != "https://oauth2.googleapis.com/token"):
            raise ValueError("Expected Google OAuth credential fields")
        credentials = {key: value for key, value in credentials.items() if key in allowed}
        credentials['token_uri'] = 'https://oauth2.googleapis.com/token'
        if (not credentials.get("refresh_token") or not credentials.get("client_secret")
                or not credentials.get("client_id", "").endswith(".apps.googleusercontent.com")):
            raise ValueError("Offline Google authorization required")
        if set(credentials.get("scopes", [])) != {"https://www.googleapis.com/auth/gmail.readonly"}:
            raise ValueError("Only Gmail read-only scope is permitted")
        # Verify via a refresh and the actual mailbox before committing new credentials.
        token = self.request("POST", "https://oauth2.googleapis.com/token", data={
            "grant_type": "refresh_token", "refresh_token": credentials["refresh_token"],
            "client_id": credentials["client_id"], "client_secret": credentials["client_secret"]})
        profile = self.request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                               headers={"Authorization": "Bearer " + token["access_token"]})
        if profile.get("emailAddress", "").replace(".", "").lower() != self.mailbox.replace(".", ""):
            raise Forbidden("Authorized mailbox does not match configured namespace")
        self.vault.save({**credentials, "token": token["access_token"],
                         "expires_at": self.clock() + token.get("expires_in", 3600)})
        self.authorization_required = False
        return {"authorized": True}

    def health(self):
        """A private, read-only status projection; no consent or token refresh is triggered."""
        credentials = self.vault.load()
        required = self.authorization_required or bool(credentials.get('authorization_required'))
        authorized = bool(credentials.get('refresh_token')) and not required
        return {'configured': True, 'authorized': authorized,
                'state': 'authorization_required' if required else
                         'configured' if authorized else 'not_authorized'}

    def access_token(self):
        # Only refresh shares a lock; this never holds a SQLite transaction or an OTP lifecycle lock.
        with self.vault.lock:
            credentials = self.vault.load()
            if not credentials.get("refresh_token"):
                raise Unavailable("Server Gmail authorization missing")
            if credentials.get("expires_at", 0) <= self.clock() + 60:
                token = self.request("POST", "https://oauth2.googleapis.com/token", data={
                    "grant_type": "refresh_token", "refresh_token": credentials["refresh_token"],
                    "client_id": credentials["client_id"], "client_secret": credentials["client_secret"]})
                credentials.update(token=token["access_token"],
                                   expires_at=self.clock() + token.get("expires_in", 3600))
                self.vault.save(credentials)
            return credentials["token"]

    def get(self, endpoint, params=None):
        return self.request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/" + endpoint,
                            params=params, headers={"Authorization": "Bearer " + self.access_token()})

    def message_ids(self, query):
        ids, page = set(), None
        while True:
            value = self.get("messages", {"q": query, "maxResults": 100, "pageToken": page,
                                          "includeSpamTrash": "true"})
            ids.update(item["id"] for item in value.get("messages", []))
            page = value.get("nextPageToken")
            if not page:
                return ids
            if len(ids) >= 500:
                raise Unavailable("Gmail candidate limit reached; tighten rule")

    def message(self, message_id):
        return self.get("messages/" + message_id, {"format": "full"})


class Tickets:
    def __init__(self, store, backend, rule):
        self.store, self.backend, self.rule = store, backend, rule

    def cleanup(self):
        now = self.store.clock()
        with self.store.transaction() as db:
            db.execute("UPDATE tickets SET state='expired',code=NULL WHERE expires<=? "
                       "AND state IN ('prepared','sent','ready')", (now,))
            # Retain message deduplication and terminal tickets for 30 days; never retain bodies.
            db.execute("DELETE FROM consumed WHERE consumed<?", (now - 30 * 86400,))
            # Keep terminal identity; explicit retries remain bounded by ticket_retries.
            db.execute("UPDATE tickets SET baseline='[]',message_id=NULL WHERE expires<?",
                       (now - 30 * 86400,))

    @staticmethod
    def dto(row):
        result = {"ticket_id": row["id"], "account_id": row["account_id"], "alias": row["alias"],
                  "created_at": row["created"], "expires_at": row["expires"], "status": row["state"],
                  "rule_version": hashlib.sha256(row["rule"].encode()).hexdigest()}
        if row["state"] == "ready":
            result["code"] = row["code"]
        return result

    def get(self, node, ticket):
        self.cleanup()
        with self.store.connection() as db:
            self.store.require_node(db, node)
            row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket,)).fetchone()
            if row:
                self.store.require_owner(db, node, row['node_id'])
        if not row:
            raise KeyError(ticket)
        return dict(row)

    def prepare(self, node, request):
        self.cleanup()
        allocation = self.store.allocation(request["account_id"], node)
        origin = allocation['node_id']
        with self.store.connection() as db:
            self.store.require_node(db, node)
            receipt = self.store.receipt(db, origin, "ticket", request["request_id"], request)
        if receipt:
            return self.dto(self.get(node, receipt["ticket_id"]))
        if allocation["alias"] != request["alias"]:
            raise Conflict("Ticket alias must match allocation")
        if not self.backend or not self.rule:
            raise Unavailable("Gmail service not configured")
        now = self.store.clock()
        with self.network_budget():
            # Use the same time window as polling; older messages cannot match this ticket.
            baseline = self.backend.message_ids(self.rule.query(now))
        with self.store.transaction() as db:
            self.store.require_node(db, node)
            prior = self.store.receipt(db, origin, "ticket", request["request_id"], request)
            if prior:
                return self.dto(db.execute("SELECT * FROM tickets WHERE id=?", (prior["ticket_id"],)).fetchone())
            active = db.execute("SELECT 1 FROM tickets WHERE account_id=?",
                                (request["account_id"],)).fetchone()
            if active:
                raise Conflict("Original ticket must be resolved; do not resend")
            ticket_id = uuid.uuid4().hex
            rule = encoded(asdict(self.rule))
            db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,'prepared',?,?,NULL,NULL)",
                       (ticket_id, origin, request["account_id"], request["alias"], now, now + request["timeout"],
                        encoded(sorted(baseline)), rule))
            row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            response = self.dto(row)
            self.store.save_receipt(db, origin, "ticket", request["request_id"], request, response)
            return response

    def sent(self, node, ticket):
        self.get(node, ticket)
        with self.store.transaction() as db:
            self.store.require_node(db, node)
            db.execute("UPDATE tickets SET state='sent' WHERE id=? AND state='prepared'", (ticket,))
            return self.dto(db.execute("SELECT * FROM tickets WHERE id=?", (ticket,)).fetchone())

    def retry(self, node, ticket, request):
        """One replacement for a rejected code or an expired original request."""
        row = self.get(node, ticket)
        origin = row['node_id']
        payload = dict(request, original_ticket=ticket)

        def check(db):
            self.store.require_node(db, node)
            receipt = self.store.receipt(db, origin, 'ticket_retry', request['request_id'], payload)
            if receipt:
                return self.dto(db.execute('SELECT * FROM tickets WHERE id=?',
                                           (receipt['ticket_id'],)).fetchone())
            current = db.execute('SELECT * FROM tickets WHERE id=?', (ticket,)).fetchone()
            rejected = (request['reason'] in ('game_rejected', 'input_misdirected') and current['state'] in ('ready', 'expired')
                        and current['message_id'])
            expired = (request['reason'] == 'expired' and current['state'] == 'expired'
                       and current['expires'] <= self.store.clock())
            limited = (request['reason'] == 'proxy_limit' and current['state'] in ('sent', 'expired')
                       and not current['message_id'])
            if not (rejected or expired or limited):
                raise Conflict('Only rejected codes or expired requests can be retried')
            if not limited and db.execute('SELECT 1 FROM ticket_retries WHERE account_id=?',
                          (row['account_id'],)).fetchone():
                raise Conflict('This account already used its one login-code retry')
            return None

        with self.store.connection() as db:
            db.execute('BEGIN')  # Receipt and predecessor checks must share one read snapshot.
            receipt = check(db)
        if receipt:
            return receipt
        if not self.backend:
            raise Unavailable('Gmail service not configured')
        rule_data = json.loads(row['rule'])
        rule = MailRule(**{**rule_data, 'senders': tuple(rule_data['senders'])})
        now = self.store.clock()
        with self.network_budget():
            baseline = self.backend.message_ids(rule.query(now))
        with self.store.transaction() as db:
            receipt = check(db)
            if receipt:
                return receipt
            retry_id = uuid.uuid4().hex
            db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,'prepared',?,?,NULL,NULL)",
                       (retry_id, origin, row['account_id'], row['alias'], now, now + request['timeout'],
                        encoded(sorted(baseline)), row['rule']))
            if request['reason'] != 'proxy_limit':
                db.execute('INSERT INTO ticket_retries VALUES (?,?,?,?,?,?)',
                           (row['account_id'], origin, ticket, retry_id, request['reason'], now))
            db.execute("UPDATE tickets SET state='superseded',code=NULL WHERE id=?", (ticket,))
            response = self.dto(db.execute('SELECT * FROM tickets WHERE id=?', (retry_id,)).fetchone())
            self.store.save_receipt(db, origin, 'ticket_retry', request['request_id'], payload, response)
            return response

    def poll(self, node, ticket):
        row = self.get(node, ticket)
        if row["state"] in ("ready", "done", "expired", "ambiguous", "superseded"):
            return self.dto(row)
        if not self.backend:
            raise Unavailable("Gmail service not configured")
        rule = MailRule(**{**json.loads(row["rule"]), "senders": tuple(json.loads(row["rule"])["senders"])})
        baseline = set(json.loads(row["baseline"]))
        with self.network_budget():
            ids = self.backend.message_ids(rule.query(row["created"])) - baseline
            if len(ids) > 30:
                raise Unavailable("Too many candidates to safely poll within deadline")
            candidates = []
            ambiguous = False
            for message_id in ids:
                try:
                    message = self.backend.message(message_id)
                    code = (extract_code(message, row["alias"], int(row["created"] * 1000), rule,
                                         mailbox=self.store.mailbox)
                            if int(message.get("internalDate", 0)) <= row["expires"] * 1000 else None)
                except (NeedsReview, UnicodeError, ValueError):
                    ambiguous = True
                    continue
                if code:
                    candidates.append((message_id, code))
        with self.store.transaction() as db:
            self.store.require_node(db, node)
            current = db.execute("SELECT * FROM tickets WHERE id=?", (ticket,)).fetchone()
            if current["state"] not in ("prepared", "sent"):
                return self.dto(current)
            if self.store.clock() >= current["expires"]:
                db.execute("UPDATE tickets SET state='expired',code=NULL WHERE id=?", (ticket,))
            elif ambiguous or len(candidates) > 1:
                db.execute("UPDATE tickets SET state='ambiguous',code=NULL WHERE id=?", (ticket,))
            elif candidates:
                message_id, code = candidates[0]
                consumed = db.execute("SELECT ticket_id FROM consumed WHERE message_id=?", (message_id,)).fetchone()
                if consumed and consumed[0] != ticket:
                    db.execute("UPDATE tickets SET state='ambiguous',code=NULL WHERE id=?", (ticket,))
                else:
                    db.execute("INSERT OR IGNORE INTO consumed VALUES (?,?,?)", (message_id, ticket, self.store.clock()))
                    db.execute("UPDATE tickets SET state='ready',code=?,message_id=? WHERE id=?",
                               (code, message_id, ticket))
            return self.dto(db.execute("SELECT * FROM tickets WHERE id=?", (ticket,)).fetchone())

    def complete(self, node, ticket):
        self.get(node, ticket)
        with self.store.transaction() as db:
            self.store.require_node(db, node)
            db.execute("UPDATE tickets SET state='done',code=NULL WHERE id=? AND state!='superseded'", (ticket,))
            return self.dto(db.execute("SELECT * FROM tickets WHERE id=?", (ticket,)).fetchone())

    @contextmanager
    def network_budget(self):
        if self.backend and hasattr(self.backend, "bounded"):
            with self.backend.bounded():
                yield
        else:
            yield
