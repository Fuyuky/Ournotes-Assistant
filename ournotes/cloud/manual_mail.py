"""Explicit administrator mail reads and a separate, durable confirmation audit."""
from __future__ import annotations

from contextlib import nullcontext

from ournotes.cloud.rebind import authenticated, confirm, confirmation_params
from ournotes.cloud.store import Conflict, Forbidden, Unavailable
from ournotes.common.otp import NeedsReview, extract_code


class ManualMail:
    def __init__(self, store, backend, rule, confirmer=None):
        self.store, self.backend, self.rule = store, backend, rule
        self.confirmer = confirmer or confirm
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS manual_mail_actions('
                       'message_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,actor TEXT NOT NULL,'
                       'request_id TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,'
                       'UNIQUE(actor,request_id))')

    def health(self):
        if self.backend is None:
            return {'configured': False, 'authorized': False, 'state': 'not_configured'}
        if hasattr(self.backend, 'health'):
            return self.backend.health()
        return {'configured': True, 'authorized': True, 'state': 'configured'}

    def _account(self, index):
        return self.store.result_by_index(index)['result']

    def _classify(self, message, alias, since):
        received = int(message.get('internalDate', 0)) / 1000
        if received > self.store.clock() or not authenticated(message, alias, since):
            return None
        if self.rule and authenticated(message, alias, since, senders=self.rule.senders):
            code = extract_code(message, alias, int(since * 1000), self.rule, mailbox=self.store.mailbox)
            if code:
                return {'purpose': 'login', 'received_at': received, 'code': code}
        try:
            params = confirmation_params(message)
        except (NeedsReview, ValueError, UnicodeError, KeyError):
            return None
        return {'purpose': 'identity', 'received_at': received, '_params': params}

    def read(self, index, minutes=10):
        if type(minutes) is not int or not 10 <= minutes <= 1440:
            raise ValueError('Mail window must be 10 minutes through 24 hours')
        account = self._account(index)
        if self.backend is None:
            raise Unavailable('Gmail service unavailable')
        since, latest = self.store.clock() - minutes * 60, {}
        with self.backend.bounded() if hasattr(self.backend, 'bounded') else nullcontext():
            ids = self.backend.message_ids('to:' + account['email'] + ' after:' + str(int(since)))
            for mid in ids:
                try:
                    value = self._classify(self.backend.message(mid), account['email'], since)
                except (NeedsReview, ValueError, UnicodeError, KeyError):
                    continue
                if value is None:
                    continue
                value['message_id'] = mid
                prior = latest.get(value['purpose'])
                if not prior or (value['received_at'], mid) > (prior['received_at'], prior['message_id']):
                    latest[value['purpose']] = value
        with self.store.connection() as db:
            for value in latest.values():
                value.pop('_params', None)
                row = db.execute('SELECT state FROM manual_mail_actions WHERE message_id=?',
                                 (value['message_id'],)).fetchone()
                value['execution_status'] = row['state'] if row else None
                value['can_execute'] = (value['purpose'] == 'identity' and row is None
                                        and self.store.clock() - value['received_at'] <= 600)
        return {'index': index, 'minutes': minutes, 'observed_at': self.store.clock(),
                'messages': [latest[key] for key in ('login', 'identity') if key in latest]}

    def execute(self, actor, index, message_id, request_id):
        account = self._account(index)
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
            raise ValueError('Invalid message identity')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('Invalid request identity')
        with self.store.connection() as db:
            prior = db.execute('SELECT * FROM manual_mail_actions WHERE (actor=? AND request_id=?) '
                               'OR message_id=?', (str(actor), request_id, message_id)).fetchone()
            if prior:
                if prior['message_id'] != message_id or prior['account_id'] != account['account_id']:
                    raise Conflict('Confirmation request identity changed')
                return {'message_id': message_id, 'status': prior['state'], 'replayed': True}
        if self.backend is None:
            raise Unavailable('Gmail service unavailable')
        latest = next((value for value in self.read(index, 10)['messages']
                       if value['purpose'] == 'identity'), None)
        if latest is None or latest['message_id'] != message_id:
            raise Conflict('Verification message is no longer the latest valid identity email')
        with self.backend.bounded() if hasattr(self.backend, 'bounded') else nullcontext():
            value = self._classify(self.backend.message(message_id), account['email'], self.store.clock() - 600)
        if value is None or value['purpose'] != 'identity' or self.store.clock() - value['received_at'] > 600:
            raise Conflict('Verification link is expired or invalid')
        with self.store.transaction() as db:
            if getattr(actor, 'credential_hash', None) is not None:
                self.store.require_node(db, actor)
            else:
                owner = db.execute('SELECT enabled FROM administrators WHERE username=?', (str(actor),)).fetchone()
                if not owner or not owner['enabled']:
                    raise Forbidden('Administrator disabled')
            prior = db.execute('SELECT * FROM manual_mail_actions WHERE message_id=? OR '
                               '(actor=? AND request_id=?)', (message_id, str(actor), request_id)).fetchone()
            if prior:
                if prior['message_id'] != message_id or prior['account_id'] != account['account_id']:
                    raise Conflict('Confirmation request identity changed')
                return {'message_id': message_id, 'status': prior['state'], 'replayed': True}
            # Persist before calling the official endpoint. Unknown outcomes never replay.
            db.execute("INSERT INTO manual_mail_actions VALUES(?,?,?,?,'uncertain',?)",
                       (message_id, account['account_id'], str(actor), request_id, self.store.clock()))
        state = 'uncertain'
        try:
            state = 'done' if self.confirmer(value['_params']) else 'rejected'
        except (Unavailable, OSError, ValueError):
            pass
        with self.store.transaction() as db:
            db.execute('UPDATE manual_mail_actions SET state=? WHERE message_id=?', (state, message_id))
        # Neither a read nor a confirmation changes automatic tickets or the account's usage status.
        return {'message_id': message_id, 'status': state, 'replayed': False}
