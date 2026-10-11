import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ournotes.ui.fields import Fields
from ournotes.vision.ocr import TextBox
from ournotes.rebind.flow import Flow
from ournotes.rebind.service import Stopped
from ournotes.rebind.vision import registered_email

REFERENCE = Path(__file__).resolve().parents[1] / 'assets/reference/email-rebind'


@pytest.mark.parametrize('name,expected,shift', [('registered-toast', True, 0), ('no-registered-toast', False, 0),
                                              ('registered-toast', True, -80), ('registered-toast', True, 80)])
def test_ocr_recognizes_actual_toast_and_rejects_actual_plain_form(name, expected, shift):
    # Native pixels from the real reproduction, with no mailbox in this ROI.
    image = Image.new('RGB', (1600, 900))
    image.paste(Image.open(REFERENCE / (name + '.png')), (590 + shift, 425))
    assert bool(registered_email(Fields(), image)) is expected


@pytest.mark.parametrize('text,expected', [
    ('邮箱已被注册(6UG4nV)', True), ('邮箱已被注册', True),
    ('邮箱已被注册(other-code_42)', True), ('邮箱已被注册（错误码会变化）', True),
    ('郵箱已被註冊(another code)', True),
    ('邮箱未被注册', False), ('邮箱链接尚未确认', False),
    ('今日邮箱发送次数已达上限', False), ('网络错误', False),
    ('邮箱已被注册，请联系管理员', False), ('', False),
])
def test_only_explicit_registration_rejection_is_recoverable(text, expected):
    fields = SimpleNamespace(read=lambda *args, **kwargs: [TextBox(text, 1, (705, 456, 893, 481))])
    assert bool(registered_email(fields, None)) is expected


def target_flow(tmp_path, results, changes):
    flow = Flow.__new__(Flow)
    flow.task = {'id': 'a' * 32, 'index': 1, 'email': 'rejected@gmail.com'}
    flow.password = 'unchanged-password'
    flow.image = Image.new('RGB', (1600, 900))
    flow.service = SimpleNamespace(root=tmp_path, wait_target_email=lambda: next(changes))
    flow.fields = None
    calls = []
    flow.send_mail = lambda *args, **kwargs: calls.append(('send', kwargs['revision'], flow.task['email']))
    flow.until = lambda *args, **kwargs: next(results)
    flow.domain = lambda expected, email, **kwargs: email.split('@')[0]
    flow.replace_target = lambda previous, local: calls.append(('replace', previous, local))
    flow.proxy_permit = SimpleNamespace(reserve_rebind_mail=lambda: calls.append(('reserve',)))
    return flow, calls


def test_repeated_registered_targets_continue_same_task_without_login_or_password_reset(tmp_path):
    flow, calls = target_flow(tmp_path, iter(['registered_email', 'bind_form', 'registered_email', 'bind_form', 'confirm_b']),
                              iter([('second@qq.com', 1), ('third@163.com', 2)]))
    flow.target_confirmation()
    assert calls == [('send', 0, 'rejected@gmail.com'), ('replace', 'rejected', 'second'), ('reserve',),
                     ('send', 1, 'second@qq.com'), ('replace', 'second', 'third'), ('reserve',),
                     ('send', 2, 'third@163.com')]
    assert flow.password == 'unchanged-password' and flow.task['id'] == 'a' * 32
    assert flow.waiting_target is False
    assert len(list((tmp_path / 'results/maintenance/diagnostics/email-rebind' / flow.task['id']).glob('*.png'))) == 2


def test_unknown_target_result_does_not_offer_email_change_or_send_again(tmp_path):
    flow, calls = target_flow(tmp_path, iter([]), iter([]))
    flow.until = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('timeout'))
    with pytest.raises(ValueError, match='timeout'):
        flow.target_confirmation()
    assert calls == [('send', 0, 'rejected@gmail.com')] and not flow.waiting_target


def test_stop_while_waiting_for_replacement_does_not_submit_more_mail(tmp_path):
    flow, calls = target_flow(tmp_path, iter(['registered_email']), iter([]))
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


def test_toast_is_read_from_first_captured_frame_before_slow_full_observation(monkeypatch):
    image = Image.new('RGB', (1600, 900))
    image.paste(Image.open(REFERENCE / 'registered-toast.png'), (590, 425))
    monkeypatch.setattr('ournotes.rebind.flow.scene', lambda image: 'bind_form')
    flow = Flow.__new__(Flow)
    flow.service = SimpleNamespace(check=lambda: None)
    flow.device = SimpleNamespace(capture=lambda: SimpleNamespace(image=image))
    flow.fields = Fields()
    flow.waiting_target = True
    flow.observer = SimpleNamespace(read=lambda *args, **kwargs: pytest.fail('slow observation delayed toast detection'))
    assert flow.read() == 'registered_email'
