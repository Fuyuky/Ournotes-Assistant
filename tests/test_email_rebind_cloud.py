"""No real mail, game input, allocations or cloud writes."""
import base64
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_cloud_service import ADMIN, allocate, upload
from test_cloud_service import service as cloud_fixture

from ournotes.cloud.api import PREFIX, create_app
from ournotes.cloud.mail import GmailAuthorizationRequired, MailRule, Tickets
from ournotes.cloud.rebind import RebindTickets, authenticated, confirmation_params
from ournotes.cloud.store import Conflict, Forbidden, Unavailable
from ournotes.common.otp import NeedsReview

cloud_service = pytest.fixture(name="service")(cloud_fixture.__wrapped__)

RULE = MailRule(('help@biligames.cc',), '验证', r'code: (?P<code>\d{6})')


class Mail:
    def __init__(self):
        self.messages = {}

    def message_ids(self, query):
        return set(self.messages)

    def message(self, mid):
        return self.messages[mid]


def message(alias, now, purpose='identity', extra=''):
    text = ('code: 123456' if purpose == 'login' else
            '<a href="https://game-intl.biligame.net/email_verify/result.html?type=mail_change_current_verify&amp;ticket=synthetic&amp;code=fake">verify</a>')
    return {'internalDate': str(int(now * 1000)), 'payload': {
        'mimeType': 'text/html', 'headers': [
            {'name': 'From', 'value': 'help@biligames.cc'}, {'name': 'To', 'value': alias},
            {'name': 'Subject', 'value': '验证'},
            {'name': 'Authentication-Results', 'value': 'mx.google.com; dkim=pass header.i=@biligames.cc;'}],
        'body': {'data': base64.urlsafe_b64encode((text + extra).encode()).decode()}}}


def setup_ticket(service, purpose='identity', confirmer=lambda p: True):
    store, _, _ = service
    a = allocate(store)
    upload(store, a)
    mail = Mail()
    tickets = RebindTickets(store, mail, RULE, ['node1'], confirmer)
    value = tickets.prepare('node1', a['index'], uuid.uuid4().hex, purpose)
    ticket = value['ticket_id']
    tickets.action('node1', ticket, 'arm')
    mail.messages['m1'] = message(a['alias'], store.clock(), purpose)
    return tickets, ticket, mail, a


def test_booked_account_can_obtain_cloud_rebind_ticket(service):
    store, _, _ = service
    a = allocate(store)
    upload(store, a)
    store.usage('node1', a['account_id'], {'request_id': 'book', 'expected_version': 1,
                                         'target_status': 'booked'})
    tickets = RebindTickets(store, Mail(), RULE, ['node1'], lambda p: True)
    assert tickets.prepare('node1', a['index'], uuid.uuid4().hex, 'identity')['status'] == 'prepared'


def test_expired_google_grant_is_actionable_over_real_cloud_transport(service):
    from ournotes.integrations.cloud_client import CloudClient, CloudError

    store, credential, _ = service
    a = allocate(store)
    upload(store, a)

    class ExpiredMail(Mail):
        def message_ids(self, query):
            raise GmailAuthorizationRequired('private-sentinel')

    app = create_app(store, ADMIN, backend=ExpiredMail(), rule=RULE, rebind_nodes=['node1'])
    transport = TestClient(app)
    cloud = CloudClient('http://127.0.0.1', credential['token'], transport=transport)
    with pytest.raises(CloudError) as raised:
        cloud.request('POST', '/rebind/tickets', {'index': a['index'], 'request_id': uuid.uuid4().hex, 'purpose': 'login'})
    assert raised.value.status == 424 and raised.value.code == 'gmail_authorization_required'
    assert '重新授权' in str(raised.value) and 'private-sentinel' not in str(raised.value)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM rebind_tickets').fetchone()[0] == 0


def test_google_authorization_failure_during_poll_keeps_same_ticket_and_propagates(service):
    tickets, ticket, mail, _ = setup_ticket(service, 'login')

    def expired(query):
        raise GmailAuthorizationRequired('synthetic')

    mail.message_ids = expired
    with pytest.raises(GmailAuthorizationRequired):
        tickets.action('node1', ticket, 'poll')
    assert tickets.get('node1', ticket)['state'] == 'sent'



def test_original_link_only_and_authentication():
    msg = message('a@gmail.com', 100, extra='<a href="https://track2.sendcloud.net/track/unsubscribe.do?p=x">unsubscribe</a>')
    assert authenticated(msg, 'a@gmail.com', 99)
    assert not authenticated(msg, 'a.b@gmail.com', 99)
    assert not authenticated(msg, 'a@gmail.com', 101)
    assert confirmation_params(msg) == {'ticket': 'synthetic', 'code': 'fake'}
    msg['payload']['headers'][-1]['value'] = 'attacker; dkim=pass header.i=@biligames.cc;'
    assert not authenticated(msg, 'a@gmail.com', 99)


@pytest.mark.parametrize('suffix', [
    '&type=mail_change_current_verify', '&ticket=other',
])
def test_duplicate_link_parameters_rejected(suffix):
    msg = message('a@gmail.com', 100)
    raw = base64.urlsafe_b64decode(msg['payload']['body']['data']).decode().replace('code=fake', 'code=fake' + suffix)
    msg['payload']['body']['data'] = base64.urlsafe_b64encode(raw.encode()).decode()
    with pytest.raises(NeedsReview):
        confirmation_params(msg)


def test_confirm_once_and_lost_response_never_replayed(service):
    calls = []
    def confirm(params):
        calls.append(params)
        raise Unavailable('lost response')
    tickets, ticket, _, _ = setup_ticket(service, confirmer=confirm)
    assert tickets.action('node1', ticket, 'poll')['status'] == 'uncertain'
    assert tickets.action('node1', ticket, 'poll')['status'] == 'uncertain'
    restarted = RebindTickets(tickets.store, tickets.backend, RULE, ['node1'], confirm)
    assert restarted.action('node1', ticket, 'poll')['status'] == 'uncertain'
    assert len(calls) == 1
    assert 'code' not in restarted.dto(restarted.get('node1', ticket))


def test_login_code_and_cancel_scrub(service):
    tickets, ticket, _, _ = setup_ticket(service, 'login')
    assert tickets.action('node1', ticket, 'poll')['code'] == '123456'
    assert tickets.action('node1', ticket, 'cancel')['status'] == 'cancelled'
    row = tickets.get('node1', ticket)
    assert row['code'] is None and not row['alias'] and row['baseline'] == '[]'


@pytest.mark.parametrize('purpose,configured,dkim_domain,expected', [
    ('login', True, 'intl-service.bilibili.com', 'ready'),
    ('login', False, 'intl-service.bilibili.com', 'sent'),
    ('login', True, 'untrusted.example', 'sent'),
    ('identity', True, 'intl-service.bilibili.com', 'done'),
    ('identity', False, 'intl-service.bilibili.com', 'done'),
    ('identity', True, 'untrusted.example', 'sent'),
])
def test_login_keeps_configured_senders_and_identity_keeps_dkim(service, purpose, configured, dkim_domain, expected):
    confirmations = []
    def confirmer(params):
        confirmations.append(params)
        return True
    tickets, ticket, mail, _ = setup_ticket(service, purpose, confirmer=confirmer)
    # Synthetic message from the new domain: credentials and real OTPs are never fixtures.
    sender = 'login@intl-service.bilibili.com'
    if configured:
        tickets.rule = MailRule((*RULE.senders, sender), RULE.subject_pattern, RULE.code_pattern)
    headers = mail.messages['m1']['payload']['headers']
    headers[0]['value'] = sender
    headers[-1]['value'] = f'mx.google.com; dkim=pass header.i=@{dkim_domain};'

    result = tickets.action('node1', ticket, 'poll')
    assert result['status'] == expected
    assert len(confirmations) == (1 if expected == 'done' else 0)
    if expected == 'ready':
        assert result['code'] == '123456'
    else:
        assert 'code' not in result


@pytest.mark.parametrize('sender', ['notice@new-provider.example', 'verify@another-provider.example'])
def test_identity_accepts_authenticated_sender_without_fixed_or_login_list(service, sender):
    calls = []
    def confirmer(params):
        calls.append(params)
        return True
    tickets, ticket, mail, _ = setup_ticket(service, confirmer=confirmer)
    headers = mail.messages['m1']['payload']['headers']
    headers[0]['value'] = sender
    headers[-1]['value'] = f"mx.google.com; dkim=pass header.d={sender.split('@')[1]};"
    for _ in range(3):
        assert tickets.action('node1', ticket, 'poll')['status'] == 'done'
    assert len(calls) == 1


@pytest.mark.parametrize('problem', ['recipient', 'stale', 'forged_auth', 'no_dkim', 'malformed_sender', 'wrong_link', 'target_link'])
def test_identity_without_sender_list_still_refuses_invalid_authorization(service, problem):
    calls = []
    tickets, ticket, mail, _ = setup_ticket(service, confirmer=lambda params: calls.append(params))
    msg = mail.messages['m1']
    headers = msg['payload']['headers']
    headers[0]['value'] = 'notice@new-provider.example'
    headers[-1]['value'] = 'mx.google.com; dkim=pass header.d=new-provider.example;'
    if problem == 'recipient':
        headers[1]['value'] = 'different@gmail.com'
    elif problem == 'stale':
        msg['internalDate'] = str(int((tickets.store.clock() - 20) * 1000))
    elif problem == 'forged_auth':
        headers[-1]['value'] = 'attacker; dkim=pass header.d=new-provider.example;'
    elif problem == 'no_dkim':
        headers[-1]['value'] = 'mx.google.com; dkim=fail header.d=new-provider.example;'
    elif problem == 'malformed_sender':
        headers[0]['value'] = 'not-an-email'
    else:
        raw = base64.urlsafe_b64decode(msg['payload']['body']['data']).decode()
        raw = (raw.replace('game-intl.biligame.net', 'untrusted.example') if problem == 'wrong_link'
               else raw.replace('mail_change_current_verify', 'mail_change'))
        msg['payload']['body']['data'] = base64.urlsafe_b64encode(raw.encode()).decode()
    assert tickets.action('node1', ticket, 'poll')['status'] in ('sent', 'review')
    assert calls == []


def test_ambiguous_mail_does_not_verify(service):
    calls = []
    tickets, ticket, mail, _ = setup_ticket(service, confirmer=lambda p: calls.append(p))
    mail.messages['m2'] = mail.messages['m1']
    assert tickets.action('node1', ticket, 'poll')['status'] == 'review'
    assert calls == []


def test_default_closed_foreign_node_and_used(service):
    tickets, ticket, _, a = setup_ticket(service)
    with pytest.raises(Forbidden):
        tickets.action('node2', ticket, 'poll')
    store, n1, _ = service
    api = TestClient(create_app(store, ADMIN, backend=tickets.backend, rule=RULE))
    assert api.get(PREFIX + '/rebind/capability', headers={'Authorization': 'Bearer ' + n1['token']}).status_code == 403
    store.used('node1', a['account_id'], {'request_id':'used', 'used':True, 'expected_version':1})
    tickets.action('node1', ticket, 'cancel')
    with pytest.raises(Conflict):
        tickets.prepare('node1', a['index'], uuid.uuid4().hex, 'identity')


def test_prepare_idempotence_and_expiry(service):
    store, _, _ = service
    a = allocate(store)
    upload(store, a)
    tickets = RebindTickets(store, Mail(), RULE, ['node1'])
    request = uuid.uuid4().hex
    one = tickets.prepare('node1', a['index'], request, 'login')
    assert tickets.prepare('node1', a['index'], request, 'login') == one
    store.clock = lambda: one['expires_at'] + 1
    assert tickets.action('node1', one['ticket_id'], 'arm')['status'] == 'expired'


def test_batch_and_rebind_poll_share_mailbox_without_crossing_tickets(service):
    store, _, _ = service
    completed = allocate(store)
    upload(store, completed)
    active = allocate(store, account='active-batch-account', request='active-allocation')
    mail = Mail()
    rebind = RebindTickets(store, mail, RULE, ['node1'])
    normal = Tickets(store, mail, RULE)
    rt = rebind.prepare('node1', completed['index'], uuid.uuid4().hex, 'login')['ticket_id']
    nt = normal.prepare('node1', {'request_id': 'batch-mail', 'account_id': active['account_id'],
                                'alias': active['alias'], 'timeout': 300})['ticket_id']
    rebind.action('node1', rt, 'arm')
    normal.sent('node1', nt)
    mail.messages['rebind-message'] = message(completed['alias'], store.clock(), 'login')
    mail.messages['batch-message'] = message(active['alias'], store.clock(), 'login')
    mail.messages['batch-message']['payload']['body']['data'] = base64.urlsafe_b64encode(b'code: 654321').decode()
    with ThreadPoolExecutor(max_workers=2) as pool:
        r = pool.submit(rebind.action, 'node1', rt, 'poll')
        n = pool.submit(normal.poll, 'node1', nt)
        assert r.result()['code'] == '123456'
        assert n.result()['code'] == '654321'
    rebind.action('node1', rt, 'cancel')
    assert normal.poll('node1', nt)['code'] == '654321'
    with store.connection() as db:
        claims = dict(db.execute('SELECT message_id,ticket_id FROM consumed').fetchall())
        assert claims == {'rebind-message': rt, 'batch-message': nt}
