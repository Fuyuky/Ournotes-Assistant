"""Ticket preparation recovers transport failures without replaying game operations."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ournotes.integrations.cloud_client import CloudError
from ournotes.rebind.flow import Flow
from ournotes.rebind.service import Stopped


@pytest.fixture
def preparing(monkeypatch):
    clock, waits, stages = [0.0], [], []
    monkeypatch.setattr('ournotes.rebind.flow.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('ournotes.rebind.flow.random.uniform', lambda low, high: low)

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds

    flow = Flow.__new__(Flow)
    flow.service = SimpleNamespace(check=Mock(), wait=wait, stage=lambda *args: stages.append(args))
    flow.client = Mock()
    flow.device = Mock()
    flow.task = {'index': 1}
    flow.tickets = []
    return flow, clock, waits, stages


def test_first_connection_and_lost_response_reuse_same_prepare_request(preparing):
    flow, _, waits, _ = preparing
    flow.client.request.side_effect = [CloudError('http_error', 'synthetic cold failure', 503),
                                       CloudError('unavailable', 'synthetic lost response'),
                                       {'ticket_id': 'same-ticket', 'status': 'prepared'}]
    assert flow.prepare('login') == 'same-ticket'
    calls = flow.client.request.call_args_list
    assert len(calls) == 3 and len({c.args[2]['request_id'] for c in calls}) == 1
    assert all(c.args[:2] == ('POST', '/rebind/tickets') for c in calls)
    assert all(c.args[2]['purpose'] == 'login' for c in calls)
    assert flow.tickets == ['same-ticket'] and waits == [2, 4]
    flow.device.tap.assert_not_called()


@pytest.mark.parametrize('code,http', [('gmail_authorization_required', 424), ('http_error', 403), ('http_error', 409)])
def test_authorization_and_business_errors_stop_without_retry(preparing, code, http):
    flow, _, waits, _ = preparing
    flow.client.request.side_effect = CloudError(code, 'synthetic', http)
    with pytest.raises(CloudError) as raised:
        flow.prepare('identity')
    assert raised.value.code == code and flow.client.request.call_count == 1 and not waits
    assert not flow.tickets
    flow.device.tap.assert_not_called()


def test_slow_requests_share_original_ninety_second_budget(preparing):
    flow, clock, waits, _ = preparing

    def fail(*args, timeout):
        clock[0] += min(20, timeout)
        raise CloudError('unavailable', 'synthetic')

    flow.client.request.side_effect = fail
    with pytest.raises(CloudError):
        flow.prepare('login')
    assert clock[0] == 90 and waits == [2, 4, 8]
    assert flow.client.request.call_count == 4
    assert flow.client.request.call_args.kwargs['timeout'] == 16
    flow.device.tap.assert_not_called()


def test_stop_interrupts_backoff_without_another_http_call(preparing):
    flow, _, _, _ = preparing
    flow.client.request.side_effect = CloudError('unavailable', 'synthetic')
    flow.service.wait = Mock(side_effect=Stopped)
    with pytest.raises(Stopped):
        flow.prepare('login')
    assert flow.client.request.call_count == 1
    flow.device.tap.assert_not_called()


def test_fast_failures_stop_after_six_attempts(preparing):
    flow, _, waits, _ = preparing
    flow.client.request.side_effect = CloudError('http_error', 'synthetic', 503)
    with pytest.raises(CloudError):
        flow.prepare('login')
    assert flow.client.request.call_count == 6 and waits == [2, 4, 8, 16, 30]
    assert len({c.args[2]['request_id'] for c in flow.client.request.call_args_list}) == 1

