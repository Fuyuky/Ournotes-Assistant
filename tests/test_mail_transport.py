"""Finite transport retries keep the same cloud operation and ticket identity."""
from unittest.mock import Mock

import pytest

from ournotes.integrations.cloud_client import CloudClient, CloudError
from ournotes.runtime.mail import RunnerMail


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    waits = []
    monkeypatch.setattr('ournotes.runtime.mail.time.monotonic', lambda: now[0])
    monkeypatch.setattr('ournotes.runtime.mail.time.time', lambda: 100 + now[0])
    monkeypatch.setattr('ournotes.runtime.mail.random.uniform', lambda low, high: low)

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr('ournotes.runtime.mail.time.sleep', sleep)
    return now, waits


def test_fifth_transient_failure_recovers_with_same_ticket_and_safe_audit(clock):
    _, waits = clock
    client = Mock()
    client.poll_mail.side_effect = [CloudError('http_error', 'synthetic', 503)] * 5 + [
        {'status': 'ready', 'code': '654321'}]
    events = []
    mail = RunnerMail(client=client, observer=lambda **fields: events.append(fields))
    assert mail.poll('same-ticket', 220) == {'status': 'received', 'code': '654321'}
    assert waits == [2, 4, 8, 16, 30]
    assert [call.args for call in client.poll_mail.call_args_list] == [('same-ticket',)] * 6
    assert events[-1]['status'] == 'recovered' and events[-1]['attempt'] == 6
    assert not any('same-ticket' in str(event) or '654321' in str(event) for event in events)
    client.retry_mail.assert_not_called()
    client.prepare_mail.assert_not_called()


def test_slow_requests_share_ninety_second_budget(clock):
    now, waits = clock
    client = Mock()

    def failed(ticket, *, request_timeout):
        now[0] += min(20, request_timeout)
        raise CloudError('unavailable', 'synthetic')

    client.poll_mail.side_effect = failed
    with pytest.raises(CloudError):
        RunnerMail(client=client).poll('same-ticket', 300)
    assert now[0] == 90 and waits == [2, 4, 8]
    assert client.poll_mail.call_count == 4
    assert client.poll_mail.call_args.kwargs['request_timeout'] == 16


def test_retries_and_request_timeouts_stop_at_original_ticket_deadline(clock):
    now, waits = clock
    client = Mock()

    def failed(ticket, *, request_timeout):
        now[0] += min(5, request_timeout)
        raise CloudError('http_error', 'synthetic', 503)

    client.poll_mail.side_effect = failed
    with pytest.raises(CloudError):
        RunnerMail(client=client).poll('same-ticket', 112)
    assert now[0] == 12 and waits == [2]
    assert client.poll_mail.call_count == 2
    assert [call.kwargs['request_timeout'] for call in client.poll_mail.call_args_list] == [12, 5]
    client.retry_mail.assert_not_called()


def test_expired_ticket_never_starts_an_http_request(clock):
    client = Mock()
    with pytest.raises(CloudError, match='等待期限'):
        RunnerMail(client=client).poll('same-ticket', 100)
    client.poll_mail.assert_not_called()


def test_cloud_transport_uses_remaining_budget_without_changing_shared_timeout(monkeypatch):
    opening = Mock(side_effect=TimeoutError)
    monkeypatch.setattr('ournotes.integrations.cloud_client.build_opener', Mock(return_value=Mock(open=opening)))
    client = CloudClient('http://127.0.0.1', 'synthetic-credential')
    with pytest.raises(CloudError):
        client.poll_mail('same-ticket', request_timeout=3)
    assert opening.call_args.kwargs['timeout'] == 3
    assert client.timeout == 20


def test_real_http_authorization_error_is_safe_and_not_a_generic_network_failure(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    error = HTTPError('https://cloud.example', 424, 'synthetic', {},
                      BytesIO(b'{"error":"gmail_authorization_required","detail":"private-sentinel"}'))
    monkeypatch.setattr('ournotes.integrations.cloud_client.build_opener',
                        Mock(return_value=Mock(open=Mock(side_effect=error))))
    with pytest.raises(CloudError) as raised:
        CloudClient('https://cloud.example', 'synthetic-credential').request('POST', '/rebind/tickets', {})
    assert raised.value.code == 'gmail_authorization_required' and raised.value.status == 424
    assert '重新授权' in str(raised.value) and 'private-sentinel' not in str(raised.value)
