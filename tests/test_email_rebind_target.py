import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from ournotes.rebind.flow import Flow
from ournotes.rebind.service import Stopped


def target_flow(tmp_path, results, changes):
    flow = Flow.__new__(Flow)
    flow.task = {'id': 'a' * 32, 'index': 1, 'email': 'rejected@gmail.com'}
    flow.password = 'unchanged-password'
    flow.image = Image.new('RGB', (1600, 900))
    flow.service = SimpleNamespace(root=tmp_path, wait_target_email=lambda: next(changes), check=lambda: None)
    flow.fields = None
    calls = []
    flow.send_mail = lambda *args, **kwargs: calls.append(('send', kwargs['revision'], flow.task['email']))
    def until(predicate, *args, **kwargs):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        assert predicate(result)
        return result
    flow.until = until
    flow.domain = lambda expected, email, **kwargs: email.split('@')[0]
    flow.replace_target = lambda previous, local: calls.append(('replace', previous, local))
    flow.proxy_permit = SimpleNamespace(reserve_rebind_mail=lambda: calls.append(('reserve',)))
    return flow, calls


def test_repeated_failed_targets_continue_same_task_without_login_or_password_reset(tmp_path):
    flow, calls = target_flow(tmp_path, iter([ValueError('timeout'), 'bind_form', ValueError('network'), 'bind_form', 'confirm_b']),
                              iter([('second@qq.com', 1), ('third@163.com', 2)]))
    flow.target_confirmation()
    assert calls == [('send', 0, 'rejected@gmail.com'), ('replace', 'rejected', 'second'), ('reserve',),
                     ('send', 1, 'second@qq.com'), ('replace', 'second', 'third'), ('reserve',),
                     ('send', 2, 'third@163.com')]
    assert flow.password == 'unchanged-password' and flow.task['id'] == 'a' * 32
    assert len(list((tmp_path / 'results/maintenance/diagnostics/email-rebind' / flow.task['id']).glob('*.png'))) == 2


@pytest.mark.parametrize('error', ['timeout', 'network', 'unknown page'])
def test_failed_target_result_offers_replacement_without_automatic_resend(tmp_path, error):
    flow, calls = target_flow(tmp_path, iter([ValueError(error)]), iter([]))
    offered = []
    def wait_target():
        offered.append(True)
        raise Stopped()
    flow.service.wait_target_email = wait_target
    with pytest.raises(Stopped):
        flow.target_confirmation()
    assert offered == [True]
    assert calls == [('send', 0, 'rejected@gmail.com')]


def test_successful_target_does_not_offer_replacement(tmp_path):
    flow, calls = target_flow(tmp_path, iter(['confirm_b']), iter([]))
    flow.target_confirmation()
    assert calls == [('send', 0, 'rejected@gmail.com')]


def test_stop_during_target_wait_does_not_offer_replacement(tmp_path):
    flow, calls = target_flow(tmp_path, iter([Stopped()]), iter([]))
    with pytest.raises(Stopped):
        flow.target_confirmation()
    assert len(calls) == 1


def test_failure_before_submission_does_not_offer_replacement(tmp_path):
    flow, calls = target_flow(tmp_path, iter([]), iter([]))
    flow.send_mail = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('not submitted'))
    with pytest.raises(ValueError, match='not submitted'):
        flow.target_confirmation()
    assert not calls


def test_stop_while_waiting_for_replacement_does_not_submit_more_mail(tmp_path):
    flow, calls = target_flow(tmp_path, iter([ValueError('timeout')]), iter([]))
    flow.service.wait_target_email = lambda: (_ for _ in ()).throw(Stopped())
    with pytest.raises(Stopped):
        flow.target_confirmation()
    assert calls == [('send', 0, 'rejected@gmail.com')]


def test_new_target_send_has_distinct_durable_identity():
    flow = Flow.__new__(Flow)
    flow.task = {'id': 'task'}
    calls = []
    flow.service = SimpleNamespace(stage=lambda *args: None)
    flow.tap = lambda *args, **kwargs: kwargs['before_input']()
    flow.proxy_permit = SimpleNamespace(send=lambda request, purpose, perform: (calls.append(request), perform(lambda: None)))
    for revision in (0, 1, 2):
        flow.send_mail('b_send_intent', '', 'bind_form', 800, 628, revision=revision)
    assert calls == ['rebind-task-b_send_intent', 'rebind-task-b_send_intent-1', 'rebind-task-b_send_intent-2']


def test_clearing_requires_same_page_and_field_focus(monkeypatch):
    flow = Flow.__new__(Flow)
    inputs = []
    flow.read = lambda: 'bind_form'
    flow.image = None
    monkeypatch.setattr('ournotes.rebind.flow.input_focused', lambda *args: False)
    flow.device = SimpleNamespace(key=inputs.append)
    flow.service = SimpleNamespace(mutex=threading.RLock(), check=lambda: None)
    with pytest.raises(ValueError, match='焦点'):
        flow.guarded_key(14)
    assert not inputs


def test_field_cannot_receive_new_text_until_empty_placeholder_is_proven():
    flow = Flow.__new__(Flow)
    calls = []
    flow.tap = lambda *args: calls.append(('focus',))
    flow.until = lambda predicate, timeout: None
    flow.guarded_key = lambda key: calls.append(('key', key))
    flow.text = lambda *args, **kwargs: calls.append(('text', args[2]))
    flow.replace_target('old', 'new')
    assert calls == [('focus',), ('key', 107)] + [('key', 14)] * 5 + [('text', 'new')]
    calls.clear()
    waits = [0]

    def timeout(predicate, seconds):
        waits[0] += 1
        if waits[0] == 2:
            raise ValueError('empty field not proven')

    flow.until = timeout
    with pytest.raises(ValueError, match='not proven'):
        flow.replace_target('old', 'new')
    assert not any(call[0] == 'text' for call in calls)
