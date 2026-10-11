"""Purpose-bound email tickets for authenticated administrators and legacy nodes."""
from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from contextlib import nullcontext
from email.utils import getaddresses
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from ournotes.cloud.mail import GmailAuthorizationRequired
from ournotes.cloud.store import Conflict, Forbidden, Unavailable
from ournotes.common.otp import NeedsReview, extract_code


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.urls.extend(v for k, v in attrs if k == 'href' and v)


def links(payload):
    if payload.get('filename'):
        return []
    if payload.get('parts'):
        return [u for part in payload['parts'] for u in links(part)]
    if payload.get('mimeType') not in ('text/html', 'text/plain'):
        return []
    raw = payload.get('body', {}).get('data', '')
    if len(raw) > 512_000:
        raise NeedsReview('Oversize verification message')
    text = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8')
    if payload['mimeType'] == 'text/html':
        parser = Links()
        parser.feed(text)
        return parser.urls
    return re.findall(r'https://[^\s<>"\x27]+', text)


def authenticated(message, alias, since, *, senders=None):
    if int(message.get('internalDate', 0)) < since * 1000:
        return False
    headers = {}
    for h in message.get('payload', {}).get('headers', []):
        headers.setdefault(h['name'].lower(), []).append(h['value'])
    sender = {a.lower() for _, a in getaddresses(headers.get('from', []))}
    recipient = {a.lower() for _, a in getaddresses(headers.get('to', []))}
    if len(sender) != 1 or recipient != {alias.lower()}:
        return False
    if senders is not None and not sender <= {s.lower() for s in senders}:
        return False
    # Only Gmail's own authentication result is trusted; a sender-injected header is not.
    address = next(iter(sender)).rsplit('@', 1)
    if len(address) != 2 or not all(address):
        return False
    domain = address[1]
    return any(re.match(r'^\s*mx\.google\.com\s*;', h, re.IGNORECASE)
               and re.search(r'\bdkim=pass\b[^;]*\bheader\.(?:i|d)=@?' + re.escape(domain) + r'(?:[;\s]|$)', h, re.IGNORECASE)
               for h in headers.get('authentication-results', []))


def confirmation_params(message):
    candidates = []
    for value in set(links(message['payload'])):
        p = urlsplit(value)
        if (p.scheme != 'https' or p.netloc != 'game-intl.biligame.net'
                or p.path != '/email_verify/result.html' or p.fragment):
            continue
        q = parse_qs(p.query, keep_blank_values=True)
        if q.get('type') != ['mail_change_current_verify']:
            continue
        if any(len(v) != 1 for v in q.values()):
            raise NeedsReview('Ambiguous link parameters')
        if not all(q.get(k) and 1 <= len(q[k][0]) <= 4096 for k in ('ticket', 'code')):
            raise NeedsReview('Missing link parameters')
        candidates.append({k: q[k][0] for k in ('ticket', 'code', 'game_id') if k in q})
    if len(candidates) != 1:
        raise NeedsReview('Expected one original-email verification link')
    return candidates[0]


def confirm(params):
    import requests
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get('https://l11-sdk-login-intl.biligame.net/gapi/client/mail.confirm',
                                   params=params, timeout=(5, 10), allow_redirects=False,
                                   headers={'Origin': 'https://game-intl.biligame.net'})
            if response.status_code != 200:
                raise Unavailable('Verification response uncertain')
            value = response.json()
            return type(value.get('code')) is int and value['code'] == 0
    except (requests.RequestException, ValueError, OSError):
        raise Unavailable('Verification response uncertain') from None


class RebindTickets:
    def __init__(self, store, backend, rule, allowed_nodes=(), confirmer=confirm):
        self.store, self.backend, self.rule = store, backend, rule
        self.allowed = frozenset(allowed_nodes)
        self.confirmer = confirmer
        self.lock = threading.RLock()
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rebind_tickets '
                       '(id TEXT PRIMARY KEY,node TEXT,account TEXT,request TEXT,purpose TEXT,created REAL,'
                       'expires REAL,state TEXT,alias TEXT,baseline TEXT,code TEXT,UNIQUE(node,request))')

    def authorize(self, node):
        with self.store.connection() as db:
            self.store.require_node(db, node)
            owner = self.store.owner(db, node)
            permitted = any(row['owner_username'] == owner for row in db.execute(
                'SELECT id,owner_username FROM nodes') if row['id'] in self.allowed)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='administrator_credentials'").fetchone():
                permitted = permitted or db.execute(
                    'SELECT 1 FROM administrator_credentials c JOIN administrators a '
                    'ON a.username=c.username WHERE c.node_id=? AND a.enabled=1', (node,)).fetchone() is not None
            if not permitted:
                raise Forbidden('Rebind is not enabled for this administrator')
        if not self.backend or not self.rule:
            raise Unavailable('Mail service unavailable')

    def cleanup(self):
        with self.store.transaction() as db:
            db.execute("UPDATE rebind_tickets SET state='expired',alias='',baseline='[]',code=NULL "
                       "WHERE expires<=? AND state NOT IN ('cancelled','expired')", (self.store.clock(),))

    def get(self, node, ticket):
        self.authorize(node)
        self.cleanup()
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM rebind_tickets WHERE id=?', (ticket,)).fetchone()
            if row:
                self.store.require_owner(db, node, row['node'])
        if not row:
            raise KeyError(ticket)
        return dict(row)

    @staticmethod
    def dto(row):
        result = {'ticket_id': row['id'], 'status': row['state'], 'expires_at': row['expires']}
        if row['purpose'] == 'login' and row['state'] == 'ready':
            result['code'] = row['code']
        return result

    def prepare(self, node, index, request, purpose):
        self.authorize(node)
        if purpose not in ('login', 'identity'):
            raise ValueError('Invalid purpose')
        with self.lock:
            self.cleanup()
            envelope = self.store.result_by_index(index)
            item = envelope['result']
            if envelope['usage_status'] == 'used':
                raise Conflict('Used account cannot be rebound')
            with self.store.connection() as db:
                allocation = db.execute('SELECT alias FROM allocations WHERE account_id=?', (item['account_id'],)).fetchone()
                prior = db.execute('SELECT t.* FROM rebind_tickets t JOIN nodes n ON n.id=t.node '
                                   'WHERE n.owner_username=? AND t.request=?',
                                   (self.store.owner(db, node), request)).fetchone()
            if not allocation or allocation['alias'] != item['email']:
                raise Conflict('Original account identity unavailable')
            if prior:
                if prior['account'] != item['account_id'] or prior['purpose'] != purpose:
                    raise Conflict('Request identity changed')
                return self.dto(prior)
            now = self.store.clock()
            with self.backend.bounded() if hasattr(self.backend, 'bounded') else nullcontext():
                baseline = self.backend.message_ids('to:' + item['email'] + ' after:' + str(int(now - 60)))
            with self.store.transaction() as db:
                self.store.require_node(db, node)
                active = db.execute("SELECT 1 FROM rebind_tickets WHERE account=? AND state NOT IN "
                                    "('cancelled','expired','done')", (item['account_id'],)).fetchone()
                if active:
                    raise Conflict('Account has an active rebind ticket')
                ticket = uuid.uuid4().hex
                db.execute('INSERT INTO rebind_tickets VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (ticket, str(node), item['account_id'], request, purpose, now, now + 300,
                            'prepared', item['email'], json.dumps(sorted(baseline)), None))
            return self.dto(self.get(node, ticket))

    def action(self, node, ticket, action):
        with self.lock:
            row = self.get(node, ticket)
            if action == 'cancel':
                with self.store.transaction() as db:
                    db.execute("UPDATE rebind_tickets SET state='cancelled',alias='',baseline='[]',code=NULL WHERE id=?", (ticket,))
            elif action == 'arm' and row['state'] == 'prepared':
                with self.store.transaction() as db:
                    db.execute("UPDATE rebind_tickets SET state='sent',created=? WHERE id=?", (self.store.clock(), ticket))
            elif action == 'poll' and row['state'] == 'sent':
                self.poll(row, node)
            elif action not in ('arm', 'poll'):
                raise ValueError('Invalid ticket action')
            return self.dto(self.get(node, ticket))

    def poll(self, row, node):
        try:
            found = []
            with self.backend.bounded() if hasattr(self.backend, 'bounded') else nullcontext():
                ids = self.backend.message_ids('to:' + row['alias'] + ' after:' + str(int(row['created'])))
                for mid in ids - set(json.loads(row['baseline'])):
                    msg = self.backend.message(mid)
                    # Login OTPs use the same configured sender rule as ordinary login.
                    # Identity links use recipient/time/DKIM and the official URL, without a fixed sender list.
                    senders = self.rule.senders if row['purpose'] == 'login' else None
                    if not authenticated(msg, row['alias'], row['created'] - 1, senders=senders):
                        continue
                    if row['purpose'] == 'login':
                        value = extract_code(msg, row['alias'], int((row['created'] - 1) * 1000),
                                             self.rule, mailbox=self.store.mailbox)
                        if value:
                            found.append((mid, value))
                    else:
                        found.append((mid, confirmation_params(msg)))
            if not found:
                return
            if len(found) != 1:
                raise NeedsReview('Multiple verification messages')
            mid, value = found[0]
            with self.store.transaction() as db:
                self.store.require_node(db, node)
                live = db.execute('SELECT state,expires FROM rebind_tickets WHERE id=?', (row['id'],)).fetchone()
                if live['state'] != 'sent' or live['expires'] <= self.store.clock():
                    return
                if db.execute('SELECT 1 FROM consumed WHERE message_id=?', (mid,)).fetchone():
                    raise NeedsReview('Message already claimed')
                db.execute('INSERT INTO consumed VALUES (?,?,?)', (mid, row['id'], self.store.clock()))
                # Commit before any confirmation request. A crash never replays the URL.
                db.execute("UPDATE rebind_tickets SET state='uncertain' WHERE id=?", (row['id'],))
            if row['purpose'] == 'login':
                state, code = 'ready', value
            else:
                state, code = ('done' if self.confirmer(value) else 'rejected'), None
            with self.store.transaction() as db:
                db.execute('UPDATE rebind_tickets SET state=?,code=? WHERE id=?', (state, code, row['id']))
        except (NeedsReview, UnicodeError, ValueError):
            with self.store.transaction() as db:
                db.execute("UPDATE rebind_tickets SET state='review',code=NULL WHERE id=?", (row['id'],))
        except GmailAuthorizationRequired:
            raise
        except Unavailable:
            # Read-only mail errors can be polled again; committed verification cannot.
            pass
