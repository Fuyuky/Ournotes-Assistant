"""OTP attempts retain identity and share a durable, single-retry budget."""
import copy
import threading
from concurrent.futures import Future
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw
from preset_fixtures import historical_plan

from ournotes.runtime.model import MAIL_LIMIT_MESSAGE, PauseRun
from ournotes.runtime.runner import Runner
from ournotes.runtime.store import RuntimeStore
from ournotes.vision.ocr import Observation, TextBox


def focus(runner, field):
    image = Image.new('RGB', (1600, 900), 'white')
    draw = ImageDraw.Draw(image)
    for name, rows in [('email', (350, 409)), ('code', (425, 484))]:
        for y in rows:
            draw.line((660, y, 839, y), fill='#ff4a92' if name == field else '#cccccc')
    runner.observation.image = image


@pytest.fixture
def login_runner(tmp_path):
    r = Runner()
    r.db = RuntimeStore(tmp_path)
    r.run = r.db.create_run('MyGO only', 'test', 1)
    r.account = r.db.reserve(r.run, historical_plan('MyGO only'))
    r.account['phase'] = 'login'
    r.ctx.update(history_cleared=True, history_removed=1, otp_round=2,
                 otp={'ticket': 'expired-ticket', 'expires_at': 1, 'status': 'prepared'},
                 mail_ready=True, send_intent=True, email_focus=True, email_filled=True, agreement=True)
    r.resumed_login_account = r.account['id']
    r.code = None
    r.mail_job = None
    r.last_poll = 0
    r.log = Mock()
    r.stop = threading.Event()
    r.action = Mock(return_value=True)
    r.click = Mock(return_value=True)
    r.read_fields = Mock()
    r.login_flow.mail_submit = Mock()
    r.observation = Observation(Image.new('RGB', (1600, 900)),
        [TextBox('请输入邮箱地址', 1, (610, 363, 870, 400))], 'email_form', 'login')
    focus(r, 'email')
    r.save()
    yield r
    r.db.close()


def test_low_commit_does_not_record_send_intent(login_runner, monkeypatch):
    from ournotes.common.memory import GIB, CommitMemory
    r = login_runner
    r.resumed_login_account = None
    r.ctx.pop('send_intent')
    r.ctx['otp']['expires_at'] = float('inf')
    r.observation.texts.append(TextBox('发送验证码', 1, (880, 430, 1020, 480)))
    monkeypatch.setattr('ournotes.common.memory.read_commit_memory',
                        lambda: CommitMemory(159 * GIB, 164 * GIB, 19 * GIB))
    with pytest.raises(PauseRun, match='系统提交内存不足'):
        r.login_flow.login()
    assert 'send_intent' not in r.ctx
    r.click.assert_not_called()
    r.login_flow.mail_submit.assert_not_called()


def mail_result(r, kind, value):
    r.mail_job_kind = kind
    r.mail_job = Future()
    r.mail_job.set_result((value, 0, .1))
    r.login_flow.check_mail()


def test_login_idle_timeout_respects_bounded_mail_transport_wait(login_runner):
    r = login_runner
    before = copy.deepcopy(r.ctx)
    r.last_progress = 0
    r.mail_job = Future()
    r.mail_job_kind = 'prepare'
    r.mail_job_started_at = 10
    r.check_progress_timeout(99)
    with pytest.raises(PauseRun, match='90秒'):
        r.check_progress_timeout(100)
    assert r.ctx == before
    r.action.assert_not_called()
    r.click.assert_not_called()


def test_resume_records_one_expired_retry_before_any_input(login_runner):
    r = login_runner
    before = copy.deepcopy(r.ctx)
    r.login_flow.login()
    assert {k: v for k, v in r.ctx.items() if k != 'otp_retry'} == before
    assert r.ctx['otp_retry']['original'] == before['otp']
    assert r.ctx['otp_retry']['reason'] == 'expired'
    r.login_flow.mail_submit.assert_not_called()
    r.action.assert_not_called()


def test_second_expiry_does_not_reset_filled_form_or_budget(login_runner):
    r = login_runner
    r.observation.texts = []
    r.ctx['otp_retry'] = {'stage': 'ready', 'original': {'ticket': 'first'}}
    before = copy.deepcopy(r.ctx)
    with pytest.raises(PauseRun):
        r.login_flow.login()
    assert r.ctx == before
    r.action.assert_not_called()


@pytest.mark.parametrize('has_ticket', [False, True])
def test_restart_at_title_reenters_login_without_reallocating_or_resending(login_runner, has_ticket):
    r = login_runner
    if not has_ticket:
        for key in ('otp', 'mail_ready', 'send_intent', 'otp_round'):
            r.ctx.pop(key, None)
    before = copy.deepcopy(r.account)
    r.stop_after = None
    r.observation.scene = 'title'
    r.login_flow.login()
    assert r.account['phase'] == 'switch_account'
    assert {k: v for k, v in r.account.items() if k not in ('phase', 'context')} == {
        k: v for k, v in before.items() if k not in ('phase', 'context')}
    assert 'email_filled' not in r.ctx and 'agreement' not in r.ctx
    if has_ticket:
        assert r.ctx['otp'] == before['context']['otp'] and r.ctx['send_intent']
    r.login_flow.mail_submit.assert_not_called()
    r.click.assert_not_called()
    assert r.db.draws(r.account['id']) == []


def test_submitted_login_at_title_continues_without_logout(login_runner):
    r = login_runner
    r.ctx['login_submitted'] = True
    r.observation.scene = 'title'
    r.stop_after = None
    r.login_flow.login()
    assert r.account['phase'] == 'rewards' and r.ctx['login_confirmed']
    r.login_flow.mail_submit.assert_called_once_with('finish', 'expired-ticket')


@pytest.mark.parametrize('submitted', [False, True])
@pytest.mark.parametrize('safe_pause', [False, True])
def test_network_failure_preserves_original_login_request_even_after_title(login_runner, submitted, safe_pause):
    r = login_runner
    r.ctx['login_submitted'] = submitted
    original = copy.deepcopy(r.ctx['otp'])
    r.observation.scene = 'network_error'
    if safe_pause:
        r.ctx['otp']['expires_at'] = 10**12
        original = copy.deepcopy(r.ctx['otp'])
        r.stop.set()
    with pytest.raises(PauseRun, match='网络错误'):
        r.step()
    r.account = r.db.account(r.account['id'])
    assert r.ctx['login_network_uncertain']
    r.observation.scene = 'title'
    with pytest.raises(PauseRun, match='网络错误'):
        r.login_flow.login()
    assert r.account['phase'] == 'login'
    assert r.ctx['otp'] == original and r.ctx['send_intent']
    assert r.ctx['login_submitted'] is submitted
    assert not r.ctx.get('otp_retry') and not r.ctx.get('login_confirmed')
    r.login_flow.mail_submit.assert_not_called()
    r.action.assert_not_called()
    r.click.assert_not_called()


def test_network_uncertain_login_refuses_initialization_before_device_start(login_runner, monkeypatch):
    r = login_runner
    r.ctx['login_network_uncertain'] = True
    r.save()
    monkeypatch.setattr('ournotes.runtime.runner.RuntimeStore', lambda: r.db)
    startup = Mock()
    monkeypatch.setattr('ournotes.runtime.runner.sync_catalog', startup)
    with pytest.raises(ValueError, match='禁止自动恢复设备'):
        Runner()._initialize(run_id=r.run['id'], resume=True)
    startup.assert_not_called()


@pytest.mark.parametrize('resume_id', [None, 'different-account'])
def test_ordinary_expiry_grants_only_one_retry(login_runner, resume_id):
    r = login_runner
    r.resumed_login_account = resume_id
    r.login_flow.login()
    r.login_flow.mail_submit.assert_called_once_with('poll', 'expired-ticket', 1)
    mail_result(r, 'poll', {'status': 'expired'})
    assert r.ctx['otp_retry']['reason'] == 'expired'
    with pytest.raises(PauseRun, match='已重发一次'):
        mail_result(r, 'poll', {'status': 'expired'})
    assert r.ctx['otp_round'] == 2
    r.action.assert_not_called()


def test_unexpired_ticket_is_reused_and_cannot_later_reset_budget(login_runner, monkeypatch):
    r = login_runner
    monkeypatch.setattr('ournotes.runtime.runner.time.time', lambda: 100)
    r.ctx['otp']['expires_at'] = 200
    r.ctx.update(code_focus=True, code_filled=True)
    for _ in range(4):
        r.login_flow.login()
    r.login_flow.mail_submit.assert_called_once_with('poll', 'expired-ticket', 200)
    assert 'code_filled' not in r.ctx and 'code_focus' not in r.ctx
    assert r.ctx['send_intent'] and r.ctx['email_filled'] and r.ctx['agreement']
    assert r.resumed_login_account is None and r.ctx['otp_round'] == 2
    monkeypatch.setattr('ournotes.runtime.runner.time.time', lambda: 201)
    r.login_flow.resume_expired_login()
    mail_result(r, 'poll', {'status': 'expired'})
    assert r.ctx['otp_retry']['original']['expires_at'] == 200
    with pytest.raises(PauseRun, match='已重发一次'):
        mail_result(r, 'poll', {'status': 'expired'})


def test_pending_login_submission_is_not_repeated_or_discarded(login_runner):
    r = login_runner
    r.ctx.update(login_submitted=True, code_filled=True)
    before = copy.deepcopy(r.ctx)
    r.login_flow.login()
    assert r.ctx == before
    r.login_flow.mail_submit.assert_not_called()
    r.action.assert_not_called()
    r.click.assert_not_called()


def test_mail_limit_without_coordinator_pauses_only_affected_lane(login_runner):
    r = login_runner
    r.observation.texts.append(TextBox(MAIL_LIMIT_MESSAGE + '(6fVwzz)', 1, (636, 444, 964, 501)))
    before = copy.deepcopy(r.ctx)
    with pytest.raises(PauseRun, match=MAIL_LIMIT_MESSAGE):
        r.login_flow.login()
    assert r.ctx == before
    assert not r.stop.is_set()
    assert r.db.get_run(r.run['id'])['mail_send_limit_reached'] is True
    r.login_flow.mail_submit.assert_not_called()
    r.action.assert_not_called()
    r.click.assert_not_called()


def test_mail_limit_rotates_before_reset_without_stopping_peers(login_runner):
    r = login_runner
    r.task_gate = Mock()
    r.observation.texts.append(TextBox(MAIL_LIMIT_MESSAGE, 1, (636, 444, 964, 501)))
    r.login_flow.login()
    assert r.ctx['proxy_limit_retry']['stage'] == 'rotate'
    assert 'otp_retry' not in r.ctx
    r.login_flow.check_login_feedback()
    r.task_gate.report_mail_limit.assert_called_once_with('otp-expired-ticket')
    assert r.ctx['proxy_limit_retry']['stage'] == 'reset_form'
    r.login_flow.check_login_feedback()
    assert r.task_gate.report_mail_limit.call_count == 1
    assert not r.stop.is_set()


def test_expiry_does_not_drop_history_or_sending_state(login_runner):
    r = login_runner
    before = copy.deepcopy(r.ctx)
    mail_result(r, 'poll', {'status': 'expired'})
    assert {k: v for k, v in r.ctx.items() if k != 'otp_retry'} == before


def test_expired_limit_replacement_uses_one_normal_retry(login_runner):
    r = login_runner
    r.ctx.pop('send_intent', None)
    r.ctx['proxy_limit_retry'] = {'original': dict(r.ctx['otp']), 'stage': 'prepare'}
    mail_result(r, 'retry', {'ticket': 'replacement', 'status': 'expired', 'expires_at': 1})
    assert r.ctx['otp_retry']['original']['ticket'] == 'replacement'
    assert r.ctx['otp_retry']['reason'] == 'expired'
    assert r.ctx['otp_retry']['stage'] == 'reset_form'
    assert 'proxy_limit_retry' not in r.ctx
    assert r.ctx['proxy_limit_history'][-1]['stage'] == 'expired_before_send'
    with pytest.raises(PauseRun):
        mail_result(r, 'retry', {'ticket': 'second', 'status': 'expired', 'expires_at': 1})


def test_initialize_arms_only_explicit_resume_of_active_login(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from ournotes.catalogs.runtime import current_catalog

    monkeypatch.setattr('ournotes.runtime.runner.sync_catalog', current_catalog)

    db = RuntimeStore(tmp_path)
    run = db.create_run('MyGO only', 'test', 1)
    account = db.reserve(run, historical_plan('MyGO only'))
    account.update(phase='login', context={'otp_round': 2, 'otp': {'expires_at': 1}})
    db.save_account(account)
    db.close()
    monkeypatch.setattr('ournotes.runtime.runner.RuntimeStore', lambda: RuntimeStore(tmp_path))
    monkeypatch.setattr('ournotes.runtime.runner.RunLog', MagicMock())
    monkeypatch.setattr('ournotes.runtime.runner.RecognitionQueue', MagicMock())
    monkeypatch.setattr('ournotes.runtime.runner.RunnerMail', MagicMock())
    for resume in (False, True):
        r = Runner()
        r._initialize(run_id=run['id'], resume=resume)
        try:
            assert r.resumed_login_account == (account['id'] if resume else None)
            # Initialization itself never changes durable state or sends mail.
            assert r.ctx['otp_round'] == 2
            r.mail.prepare.assert_not_called()
        finally:
            r.executor.shutdown()
            r.db.close()


def test_limited_single_run_cannot_initialize_or_prepare_mail(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    run = db.create_run('MyGO only', 'test', 1)
    run.update(status='paused', mail_send_limit_reached=True)
    db.save_run(run)
    monkeypatch.setattr('ournotes.runtime.runner.RuntimeStore', lambda: db)
    mail = Mock()
    monkeypatch.setattr('ournotes.runtime.runner.RunnerMail', mail)
    try:
        with pytest.raises(ValueError, match=MAIL_LIMIT_MESSAGE):
            Runner()._initialize(resume=True, run_id=run["id"])
        mail.assert_not_called()
    finally:
        db.close()


def test_submitted_login_is_not_rearmed_by_empty_form(login_runner):
    r = login_runner
    r.ctx['login_submitted'] = True
    r.observation.texts.append(TextBox('请输入验证码', 1, (610, 438, 862, 474)))
    before = copy.deepcopy(r.ctx)
    r.login_flow.login()
    assert r.ctx == before
    r.login_flow.mail_submit.assert_not_called()


def test_missing_code_placeholder_waits_without_typing(login_runner):
    r = login_runner
    r.resumed_login_account = None
    r.ctx['code_focus'] = True
    r.code = 'test-only'
    focus(r, 'code')
    r.login_flow.login()
    r.action.assert_not_called()
    assert 'code_filled' not in r.ctx
    r.observation.texts.append(TextBox('请输入验证码', 1, (610, 438, 862, 474)))
    r.login_flow.login()
    r.action.assert_called_once_with('fill-code', kind='input_text', args=('test-only',))


def test_rejected_code_persists_retry_before_clearing_form_and_sends_once(login_runner):
    r = login_runner
    r.ctx.update(login_submitted=True, code_ready=True, code_filled=True)
    r.observation.texts.append(TextBox('验证码错误', 1, (610, 440, 880, 480)))
    original = copy.deepcopy(r.ctx['otp'])
    r.login_flow.login()
    saved = r.db.account(r.account['id'])['context']
    assert saved['otp_retry']['original'] == original and saved['otp_retry']['stage'] == 'reset_form'
    assert saved['login_submitted'] and saved['send_intent']
    r.login_flow.mail_submit.assert_not_called()
    r.click.assert_not_called()
    r.login_flow.login()
    r.action.assert_called_once_with('release-login-touch', kind='release_touch')
    r.login_flow.login()
    r.action.assert_called_with('reset-email-form', args=(612, 258))
    r.observation.scene = 'login_options'
    r.login_flow.login()
    assert r.ctx['otp_retry']['stage'] == 'prepare'
    assert not {'code_ready', 'code_filled', 'login_submitted', 'send_intent', 'mail_ready'} & r.ctx.keys()
    r.observation.scene = 'email_form'
    r.ctx.update(email_focus=True, email_filled=True, agreement=True)
    r.login_flow.login()
    r.login_flow.mail_submit.assert_called_once_with('retry', original['ticket'], r.account['id'], 'game_rejected')
    mail_result(r, 'retry', {'ticket': 'replacement', 'expires_at': 9999999999, 'status': 'prepared'})
    assert r.ctx['otp_retry']['stage'] == 'ready' and r.ctx['otp']['ticket'] == 'replacement'
    r.observation.texts = [TextBox('发送验证码', 1, (872, 438, 991, 474))]
    r.click.reset_mock()
    r.login_flow.mail_submit.reset_mock()
    r.login_flow.login()
    r.login_flow.login()
    r.click.assert_called_once_with('发送验证码')
    assert r.ctx['send_intent']
    assert r.login_flow.mail_submit.call_args_list[0].args == ('mark_sent', 'replacement')
    r.ctx.update(login_submitted=True, code_filled=True)
    r.observation.texts = [TextBox('验证码已失效', 1, (610, 440, 880, 480))]
    with pytest.raises(PauseRun, match='已重发一次'):
        r.login_flow.login()
    assert r.ctx['otp_retry']['original'] == original


def test_retry_resumes_reset_before_reusing_idempotent_request(login_runner):
    from ournotes.runtime.login_flow import request_code_retry

    r = login_runner
    r.ctx['login_submitted'] = True
    request_code_retry(r.account)
    r.ctx['otp_retry']['stage'] = 'prepare'
    r.save()
    r.login_flow.login()
    assert r.ctx['otp_retry']['stage'] == 'reset_form'
    r.action.assert_called_once_with('release-login-touch', kind='release_touch')
    r.login_flow.login()
    r.action.assert_called_with('reset-email-form', args=(612, 258))
    r.login_flow.mail_submit.assert_not_called()


def test_expired_replacement_does_not_send_or_reset_retry_budget(login_runner):
    from ournotes.runtime.login_flow import request_code_retry

    r = login_runner
    r.ctx['login_submitted'] = True
    request_code_retry(r.account)
    before = copy.deepcopy(r.ctx)
    with pytest.raises(PauseRun, match='发码前已过期'):
        mail_result(r, 'retry', {'ticket': 'replacement', 'expires_at': 1, 'status': 'expired'})
    assert r.ctx == before
    r.click.assert_not_called()


def test_safe_pause_finishes_original_login_without_sending_new_mail(login_runner, monkeypatch):
    r = login_runner
    monkeypatch.setattr('ournotes.runtime.runner.time.time', lambda: 100)
    r.resumed_login_account = None
    r.ctx['otp']['expires_at'] = 220
    r.stop.set()
    r.observation.texts.extend([TextBox('请输入验证码', 1, (610, 438, 862, 474)),
                                TextBox('登录/注册', 1, (600, 550, 900, 620))])
    r.step()
    assert r.pending_login() and r.pause_reason
    assert not r.pause_input_allowed('发送验证码')
    assert not r.pause_input_allowed('reset-email-form')
    assert r.pause_input_allowed('登录/注册')
    r.login_flow.mail_submit.assert_called_once_with('poll', 'expired-ticket', 220)
    mail_result(r, 'poll', {'status': 'received', 'code': '123456'})
    focus(r, 'code')
    r.step(); r.step()
    r.observation.texts.append(TextBox('123456', 1, (610, 438, 862, 474)))
    r.step()
    assert r.ctx['login_submitted']
    r.click.assert_called_once_with('登录/注册')
    r.observation.scene = 'title'
    r.step()
    assert r.account['phase'] == 'rewards'
    with pytest.raises(PauseRun, match='停止新输入'):
        r.step()
    assert [call.args[0] for call in r.login_flow.mail_submit.call_args_list] == ['poll', 'finish']


def test_safe_pause_never_starts_expiry_retry(login_runner, monkeypatch):
    r = login_runner
    now = [100]
    monkeypatch.setattr('ournotes.runtime.runner.time.time', lambda: now[0])
    r.resumed_login_account = None
    r.ctx['otp']['expires_at'] = 110
    r.stop.set()
    r.step()
    now[0] = 111
    mail_result(r, 'poll', {'status': 'expired'})
    assert r.ctx['otp_retry']['stage'] == 'reset_form'
    with pytest.raises(PauseRun, match='停止新输入'):
        r.step()
    r.action.assert_not_called()
    r.click.assert_not_called()


def test_wrong_focus_never_types_and_gets_one_persisted_release(login_runner, monkeypatch):
    r = login_runner
    r.resumed_login_account = None
    r.ctx['code_focus'] = True
    r.code = '123456'
    r.observation.texts.append(TextBox('请输入验证码', 1, (610, 438, 862, 474)))
    monkeypatch.setattr('ournotes.runtime.login_flow.time.time', lambda: 105)
    monkeypatch.setattr(r.db, 'last_sent_action_at', lambda *_: 100)
    r.login_flow.login()
    r.action.assert_called_once_with('release-login-touch', kind='release_touch')
    assert r.db.account(r.account['id'])['context']['focus_retried'] == {'code': True}
    assert 'code_focus' not in r.ctx and 'code_filled' not in r.ctx
    r.login_flow.login()
    r.action.assert_called_with('focus-code', args=(729, 453))
    with pytest.raises(PauseRun, match='焦点修复一次'):
        r.login_flow.login()
    focus(r, 'code')
    r.login_flow.login()
    r.action.assert_called_with('fill-code', kind='input_text', args=('123456',))


def test_misdirected_code_is_not_submitted_and_uses_shared_retry_budget(login_runner):
    r = login_runner
    r.resumed_login_account = None
    r.ctx.update(code_ready=True, code_filled=True, code_focus=True)
    r.code = '123456'
    r.observation.texts.extend([TextBox('请输入验证码', 1, (610, 438, 862, 474)),
                                TextBox('登录/注册', 1, (600, 600, 1000, 666))])
    r.login_flow.login()
    assert r.ctx['otp_retry']['reason'] == 'input_misdirected'
    assert not r.ctx.get('login_submitted')
    r.click.assert_not_called()
    from ournotes.runtime.login_flow import request_code_retry
    with pytest.raises(PauseRun, match='已重发一次'):
        request_code_retry(r.account, reason='input_misdirected')


def test_filled_code_requires_exact_value_in_code_field_before_submission(login_runner):
    r = login_runner
    r.resumed_login_account = None
    r.ctx.update(code_ready=True, code_filled=True)
    r.code = '012345'
    focus(r, 'code')
    r.observation.texts.extend([TextBox('012345', 1, (610, 363, 870, 400)),
                                TextBox('登录/注册', 1, (600, 600, 1000, 666))])
    r.login_flow.login()
    assert not r.ctx.get('login_submitted')
    r.click.assert_not_called()
    r.observation.texts.append(TextBox('012345', 1, (610, 438, 862, 474)))
    r.click.return_value = False
    r.login_flow.login()
    assert not r.ctx.get('login_submitted') and r.code == '012345'
    r.click.reset_mock()
    r.click.return_value = True
    r.login_flow.login()
    r.click.assert_called_once_with('登录/注册')
    assert r.ctx['login_submitted']


def test_retry_can_reenter_login_after_emulator_restart(login_runner):
    from ournotes.runtime.login_flow import request_code_retry
    r = login_runner
    request_code_retry(r.account, reason='expired')
    r.observation.scene = 'title'
    r.login_flow.login()
    r.click.assert_called_once_with('TAP TO START', exact=True)
    assert r.ctx['otp_retry']['stage'] == 'reset_form'


def test_submitted_login_status_no_longer_claims_it_is_waiting_for_mail(login_runner):
    r = login_runner
    r.run['status'] = 'running'
    r.ctx.update(code_ready=True, login_submitted=True)
    r.instance = 0
    r.callback = Mock()
    r.last_published = None
    r.publish()
    item = r.callback.call_args.args[0]
    assert item['waiting_for'] == 'login_confirmation'
    assert 'wait_seconds_remaining' not in item
