import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from PIL import Image

from ournotes.common.files import file_hash
from ournotes.runtime.model import PauseRun
from ournotes.runtime.pools import make_plan
from ournotes.runtime.runner import Runner
from ournotes.runtime.store import RuntimeStore


@pytest.mark.parametrize('scene,message', [('required_update', '游戏要求更新客户端'),
                                          ('server_maintenance', '游戏服务器维护中')])
def test_service_interrupt_pauses_without_store_or_game_input(controller, scene, message):
    r = controller
    r.observation.scene = scene
    with pytest.raises(PauseRun, match=message):
        r.step()
    r.action.assert_not_called()
    r.click.assert_not_called()


@pytest.mark.parametrize('scene', ['pool', 'home', 'home_menu', 'user_center'])
def test_logout_waits_for_login_instead_of_reopening_old_menu(controller, scene):
    r = controller
    r.account['phase'] = 'logout'
    r.observation.scene = 'user_center'
    r.click.return_value = True
    r.finish_account = Mock()
    r.login_flow.switch_account()
    assert r.db.account(r.account['id'])['context']['logout_requested'] is True
    r.click.reset_mock()
    r.account = r.db.account(r.account['id'])
    r.observation.scene = scene
    r.login_flow.switch_account()
    r.click.assert_not_called()
    r.action.assert_not_called()
    r.finish_account.assert_not_called()
    r.observation.scene = 'title'
    r.login_flow.switch_account()
    r.click.assert_called_once_with('TAP TO START', exact=True)
    r.finish_account.assert_not_called()
    r.observation.scene = 'login_options'
    r.login_flow.switch_account()
    r.finish_account.assert_called_once_with()


def test_deferred_logout_does_not_arm_transition(controller):
    r = controller
    r.account['phase'] = 'logout'
    r.observation.scene = 'user_center'
    r.click.return_value = False
    r.login_flow.switch_account()
    assert not r.ctx.get('logout_requested')


@pytest.mark.parametrize('scene,label', [('pool', 'pool-menu'), ('home', 'home-menu')])
def test_menu_touch_release_resumes_sent_input_once(controller, monkeypatch, scene, label):
    r = controller
    clock = [100.0]
    monkeypatch.setattr('ournotes.runtime.login_flow.time.time', lambda: clock[0])
    r.account['phase'] = 'logout'
    r.observation.scene = scene
    action = r.db.action(r.account['id'], {'label': label, 'phase': 'logout'})
    r.db.action_done(action)
    r.login_flow.switch_account()
    r.action.assert_not_called()
    clock[0] = 103.0
    r.login_flow.switch_account()
    r.action.assert_called_once_with('release-menu-touch', kind='release_touch')
    # Reload the durable checkpoint: resuming cannot grant another release.
    r.account = r.db.account(r.account['id'])
    r.action.reset_mock()
    r.login_flow.switch_account()
    r.action.assert_called_once_with(label, args=(1541, 51))
    r.observation.scene = 'login_options'
    r.finish_account = Mock()
    r.login_flow.switch_account()
    r.finish_account.assert_called_once_with()


@pytest.mark.parametrize('sent,phase', [(False, 'logout'), (True, 'switch_account')])
def test_menu_release_requires_sent_input_in_current_phase(controller, sent, phase):
    r = controller
    r.account['phase'] = 'logout'
    r.observation.scene = 'pool'
    action = r.db.action(r.account['id'], {'label': 'pool-menu', 'phase': phase})
    if sent:
        r.db.action_done(action)
    r.login_flow.switch_account()
    r.action.assert_called_once_with('pool-menu', args=(1541, 51))


def test_title_start_waits_for_sdk_without_more_than_three_taps(controller, monkeypatch):
    r = controller
    clock = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: clock[0])
    frame = Image.new('RGB', (1600, 900))
    r.observation = SimpleNamespace(scene='title', signature='title', image=frame, fast=True)
    r.stop, r.device = threading.Event(), Mock()
    r.device.capture.return_value = (frame, 0)
    r.log.screenshot.return_value = 'evidence.png'
    r.last_action, r.navigation_attempts, r.same_scene = None, 0, 3
    assert Runner.action(r, 'TAP TO START', args=(1188, 732))
    clock[0] = 107
    assert not Runner.action(r, 'TAP TO START', args=(1188, 732))
    for tick in (110, 120):
        clock[0] = tick
        assert Runner.action(r, 'TAP TO START', args=(1188, 732))
    # Transient scene/signature changes cannot reset the title attempt budget.
    r.last_action = None
    clock[0] = 189
    assert not Runner.action(r, 'TAP TO START', args=(1188, 732))
    clock[0] = 190
    with pytest.raises(PauseRun, match='等待90秒'):
        Runner.action(r, 'TAP TO START', args=(1188, 732))
    assert r.device.tap.call_count == 3
    assert r.db.draws(r.account['id']) == []


@pytest.mark.parametrize('scene,cleared', [('unknown', False), ('loading', False),
                                        ('home', True), ('login_options', True)])
def test_title_start_budget_clears_only_after_confirmed_progress(controller, scene, cleared):
    r = controller
    r.account['phase'] = 'logout'
    r.check_pause = Mock(return_value=False)
    r.login_flow.switch_account = Mock()
    r.title_start_wait = {'account_id': r.account['id'], 'started_at': 1, 'attempts': 3}
    r.observation.scene = scene
    r.step()
    assert (r.title_start_wait is None) is cleared


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr("ournotes.runtime.runner.ROOT", tmp_path)
    runner = Runner()
    from ournotes.runtime.input_policy import InputPolicy
    runner.input_policy = InputPolicy({"click_radius_px": 0, "interval_seconds": [0, 0]})
    runner.db = RuntimeStore(tmp_path)
    runner.run = runner.db.create_run("都抽", "test", 1)
    runner.account = runner.db.reserve(runner.run, make_plan("都抽"))
    runner.account["phase"] = "draws"
    runner.save()
    runner.log = MagicMock()
    runner.click = Mock()
    runner.action = Mock()
    runner.phase = Mock()
    runner.recognizer = Mock()
    runner.recognition = Mock()
    runner.recognition.completed.return_value = []
    runner.pools = Mock()
    runner.observation = SimpleNamespace(scene="pool", result_layout=None)
    yield runner
    runner.db.close()


def test_uncertain_submitted_confirmation_is_never_repeated(controller):
    r = controller
    d = r.draw_flow.current_draw()
    d["status"] = "submitted"
    r.db.save_draw(d)
    r.observation.scene = "draw_confirm"
    r.draw_flow.draws()
    r.click.assert_not_called()
    r.action.assert_not_called()
    assert r.db.draws(r.account["id"])[0]["status"] == "submitted"


@pytest.mark.parametrize('status', ['captured', 'complete', 'needs_review'])
def test_pressed_result_top_releases_once_without_repeating_a_draw(controller, status):
    r = controller
    d = r.draw_flow.current_draw()
    d.update(status=status, result_exit_confirmed=False)
    r.db.save_draw(d)
    r.observation.scene = 'draw_result'
    r.observation.result_layout = {'top_state': 'pressed'}
    r.draw_flow.draws()
    r.action.assert_called_once_with('release-result-top-touch', kind='release_touch')
    r.click.assert_not_called()
    r.action.reset_mock()
    r.draw_flow.draws()
    r.action.assert_not_called()
    r.click.assert_not_called()
    saved = r.db.draws(r.account['id'])
    assert len(saved) == 1 and saved[0]['status'] == status
    assert saved[0]['result_top_release_attempted'] and not saved[0]['result_exit_confirmed']
    # A subsequent clear normal button can use the existing exit navigation.
    r.observation.result_layout = {'top_state': 'normal'}
    r.draw_flow.draws()
    r.click.assert_called_once_with('招募TOP')


def test_new_account_pins_settlement_source_before_first_device_frame(controller, tmp_path, monkeypatch):
    r = controller
    r.run = r.db.create_run('都抽', 'test', 1)
    run_id = r.run['id']
    r.account = None
    r.stop = threading.Event()
    r.instance, r.resume = 1, False
    r.publish, r.mail, r.executor = Mock(), Mock(), Mock()
    r.catalog_directory = tmp_path / 'data/catalog/snapshots/test'
    r.catalog_directory.mkdir(parents=True)
    for name in ('catalog.json', 'pools.json', 'history.json'):
        (r.catalog_directory / name).write_text('{}', encoding='utf-8')
    device = Mock()
    device.capture.side_effect = PauseRun('offline test boundary')
    monkeypatch.setattr('ournotes.runtime.runner.STATE', tmp_path / 'state')
    monkeypatch.setattr('ournotes.runtime.runner.ensure_game', Mock())
    monkeypatch.setattr('ournotes.runtime.runner.DeviceClient', lambda _: device)
    monkeypatch.setattr('ournotes.runtime.runner.Observer', Mock())
    r._execute()
    saved = RuntimeStore(tmp_path)
    try:
        run = saved.get_run(run_id)
        account = saved.account(run['active_account'])
        assert account['recognition_source'] == 'settlement'
        assert account['catalog_snapshot'] == 'data/catalog/snapshots/test'
        assert set(account['catalog_fingerprints']) == {'catalog.json', 'pools.json'}
        assert saved.draws(account['id']) == []
    finally:
        saved.close()
    device.capture.assert_called_once()
    device.tap.assert_not_called()


@pytest.mark.parametrize('wallet_ready', [False, True])
def test_settlement_draw_reads_wallet_without_opening_history(controller, wallet_ready):
    r = controller
    r.account['recognition_source'] = 'settlement'
    r.observation.image = Image.new('RGB', (1600, 900))
    r.observation.texts = []
    r.pool_navigation = Mock()
    r.pool_navigation.observe.return_value = 'ready'
    r.pools.scan.return_value = (None, {}, {})
    r.draw_flow.record_post_gifts_balance = Mock(return_value=wallet_ready)
    r.draw_flow.open_draw_confirmation, r.open_history, r.history_step = Mock(), Mock(), Mock()
    r.draw_flow.draws()
    draw = r.db.draws(r.account['id'])[0]
    assert draw['recognition_source'] == 'settlement' and draw['status'] == 'planned'
    r.draw_flow.record_post_gifts_balance.assert_called_once()
    assert r.draw_flow.open_draw_confirmation.call_count == int(wallet_ready)
    r.history_step.assert_not_called()
    r.open_history.assert_not_called()
    assert 'history_request' not in r.ctx and 'history_baseline' not in draw














def test_startup_reward_during_captured_resume_does_not_allocate_a_draw(controller):
    r = controller
    r.account['recognition_source'] = 'settlement'
    draw = r.draw_flow.current_draw()
    draw.update(status='captured', result_exit_confirmed=True)
    r.db.save_draw(draw)
    r.ctx['history_request'] = {'mode': 'capture', 'draw_id': draw['id'], 'started_at': 0}
    r.observation.scene = 'startup_reward'
    r.reward_flow.dismiss_reward = Mock()
    r.draw_flow.draws()
    r.reward_flow.dismiss_reward.assert_called_once()
    assert len(r.db.draws(r.account['id'])) == 1


def test_stuck_confirm_releases_once_without_a_new_press_or_submission(controller):
    r = controller
    draw = r.draw_flow.current_draw()
    draw.update(status='submitted', submitted_at=0)
    r.db.save_draw(draw)
    r.observation.scene = 'draw_confirm'
    for _ in range(5):
        r.draw_flow.draws()
    r.action.assert_called_once_with('release-confirm-touch', kind='release_touch')
    r.click.assert_not_called()
    stored = r.db.draws(r.account['id'])
    assert len(stored) == 1 and stored[0]['status'] == 'submitted'
    assert stored[0]['release_recovery_attempted'] is True


def test_paid_shortage_is_only_closed_never_submitted(controller):
    r = controller
    r.observation.scene = 'paid_shortage'
    r.step()
    r.click.assert_called_once_with('关闭', exact=True)
    assert r.db.draws(r.account['id']) == []
    r.action.assert_not_called()


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
@pytest.mark.parametrize(('scene', 'target', 'label'), [
    ('opening_movie', '跳过', 'skip-opening-movie'),
    ('opening_skip_confirm', 'OK', 'confirm-opening-skip'),
    ('story', '剧情菜单', 'open-story-menu'),
    ('story_menu', 'SKIP', 'skip-story'),
    ('story_skip_confirm', '跳过', 'confirm-story-skip'),
])
def test_opening_movie_does_not_create_transactions_or_change_identity(controller, phase, scene, target, label):
    import copy
    r = controller
    r.account['phase'] = phase
    before = copy.deepcopy(r.account)
    r.observation.scene = scene
    r.step()
    r.click.assert_called_once_with(target, exact=True, label=label)
    r.action.assert_not_called()
    r.phase.assert_not_called()
    assert r.account == before and r.db.draws(r.account['id']) == []


@pytest.mark.parametrize('scene', ['opening_movie', 'opening_skip_confirm',
                                  'story', 'story_menu', 'story_skip_confirm'])
def test_opening_movie_cannot_hide_a_submitted_draw(controller, scene):
    r = controller
    draw = r.draw_flow.current_draw()
    draw['status'] = 'submitted'
    r.db.save_draw(draw)
    r.observation.scene = scene
    r.step()
    assert r.account is None
    r.click.assert_not_called()
    assert r.db.draws(draw['account_id'])[0]['status'] == 'submitted'


def test_alternating_opening_pages_have_a_shared_action_budget(controller):
    r = controller
    scenes = ['opening_movie', 'opening_skip_confirm', 'story', 'story_menu', 'story_skip_confirm']
    for i in range(12):
        r.observation.scene = scenes[i % len(scenes)]
        r.step()
        r.observation.scene = 'loading'
        r.step()
    r.observation.scene = 'opening_movie'
    with pytest.raises(PauseRun, match='12次'):
        r.step()
    assert r.click.call_count == 12 and r.db.draws(r.account['id']) == []


def test_opening_deadline_survives_unknown_frames_and_failed_guards(controller, monkeypatch):
    r = controller
    r.account['phase'] = 'login'
    r.click.return_value = False
    now = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: now[0])
    r.observation.scene = 'opening_movie'
    r.step()
    assert r.opening_actions == 0
    r.observation.scene = 'unknown'
    now[0] += 61
    r.step()
    r.observation.scene = 'opening_skip_confirm'
    with pytest.raises(PauseRun, match='60秒'):
        r.step()
    assert r.click.call_count == 1


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
def test_download_confirmation_preserves_account_login_and_draws(controller, phase):
    import copy

    r = controller
    r.account['phase'] = phase
    r.ctx.update(login_confirmed=True, uid_verified=True)
    before = copy.deepcopy(r.account)
    r.observation.scene = 'download_confirm'
    r.step()
    r.click.assert_called_once_with('OK', exact=True)
    r.phase.assert_not_called()
    assert r.account == before and r.db.draws(r.account['id']) == []


def test_download_wait_tracks_only_increasing_progress_without_input(controller, monkeypatch):
    r = controller
    r.observation = SimpleNamespace(scene='downloading', text='数据下载中 1.2%')
    r.read_fields = Mock()
    r.publish = Mock()
    now = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: now[0])
    r.step()
    assert r.last_progress == 100 and r.download_percent == 1.2
    now[0] = 101
    r.step()
    assert r.read_fields.call_count == 1  # Only the small field, at most once per two seconds.
    now[0] = 105
    r.step()
    assert r.last_progress == 100
    r.observation.text = '数据下载中 12.5%'
    now[0] = 110
    r.step()
    assert r.last_progress == 110 and r.download_percent == 12.5
    for text in ('数据下载中 5%', '数据下载中 999%', '数据下载中'):
        now[0] += 5
        r.observation.text = text
        r.step()
        assert r.last_progress == 110
    r.click.assert_not_called()
    r.action.assert_not_called()
    r.phase.assert_not_called()


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
def test_extra_prompt_only_closes_and_preserves_transaction(controller, phase):
    r = controller
    d = r.draw_flow.current_draw()
    d['status'] = 'submitted'
    r.db.save_draw(d)
    before = r.db.draws(r.account['id'])
    r.account['phase'] = phase
    r.observation.scene = 'enable_prompt'
    r.step()
    r.click.assert_called_once_with('关闭额外弹窗', exact=True)
    r.phase.assert_not_called()
    assert r.db.draws(r.account['id']) == before


def test_disabled_menu_closes_then_enters_title_without_touching_draws(controller):
    r = controller
    r.account['phase'] = 'logout'
    d = r.draw_flow.current_draw()
    d['status'] = 'complete'
    r.db.save_draw(d)
    before = r.db.draws(r.account['id'])
    r.observation.scene = 'title_menu_unavailable'
    r.step()
    r.click.assert_called_once_with('关闭', exact=True, label='close-unavailable-menu')
    assert r.ctx['title_auth_recovery']
    r.observation.scene = 'title'
    r.step()
    r.click.assert_called_with('TAP TO START', exact=True)
    r.action.assert_not_called()
    assert r.db.draws(r.account['id']) == before


def test_disabled_menu_recovery_is_bounded_and_persisted(controller):
    r = controller
    r.observation.scene = 'title_menu_unavailable'
    r.click.return_value = False
    r.step()
    assert 'title_auth_recovery_attempts' not in r.ctx
    r.click.return_value = True
    r.step()
    r.step()
    assert r.db.account(r.account['id'])['context']['title_auth_recovery_attempts'] == 2
    with pytest.raises(PauseRun, match='用户中心持续不可用'):
        r.step()


def test_home_menu_uses_direct_user_center_for_logout(controller):
    r = controller
    r.account['phase'] = 'logout'
    r.observation.scene = 'home_menu'
    r.step()
    r.click.assert_called_once_with('用户中心', exact=True)
    r.action.assert_not_called()


def test_recovered_home_verifies_uid_before_rewards_or_draws(controller):
    r = controller
    r.account['phase'] = 'rewards'
    r.ctx.update(title_auth_recovery=True, uid_verified=False, gifts_done=True)
    r.observation.scene = 'home'
    r.step()
    r.action.assert_called_once_with('home-menu', args=(1541, 51))
    r.click.assert_not_called()
    r.phase.assert_not_called()


def test_verified_recovery_closes_home_menu_instead_of_getting_stuck(controller):
    r = controller
    r.account['phase'] = 'rewards'
    r.ctx['uid_verified'] = True
    r.observation.scene = 'home_menu'
    r.step()
    r.click.assert_called_once_with('关闭', exact=True)
    r.phase.assert_not_called()


def test_unreadable_confirm_button_does_not_record_a_submission(controller):
    r = controller
    r.draw_flow.current_draw()
    r.observation.scene = "draw_confirm"
    r.observation.find = Mock(return_value=None)
    r.draw_flow.draws()
    r.click.assert_not_called()
    assert r.db.draws(r.account["id"])[0]["status"] == "planned"


def test_submitted_draw_waits_through_pool_transition_without_spending_twice(controller):
    r = controller
    d = r.draw_flow.current_draw()
    d["status"] = "submitted"
    r.db.save_draw(d)
    for _ in range(5):
        r.draw_flow.draws()
    r.click.assert_not_called()
    assert len(r.db.draws(r.account["id"])) == 1


@pytest.mark.parametrize('source', [None, 'settlement'])
def test_recover_captured_result_after_crash(controller, tmp_path, source):
    r = controller
    if source:
        r.account['recognition_source'] = source
    d = r.draw_flow.current_draw()
    path = tmp_path / "result.png"
    Image.new("RGB", (1600, 900)).save(path)
    d.update(status="captured", screenshot={"path": "result.png", "sha256": file_hash(path)})
    r.db.save_draw(d)
    card = {"key": "member:1", "id": 1, "kind": "member", "rarity": "SSR", "name": "测试", "title": "测试"}
    r.recognizer.recognize.return_value = {"status": "complete", "layout": 10,
                                          "slots": [{"status": "matched", "card": card}] * 10}
    r.observation.scene = "draw_result"
    r.draw_flow.check_recognition()
    r.recognition.submit.assert_called_once()
    r.draw_flow.draws()
    r.click.assert_called_once_with("招募TOP")
    assert len(r.db.draws(r.account["id"])) == 1
    # Navigation can finish before recognition, and the next transaction may then start.
    r.observation.scene = "loading"
    r.draw_flow.draws()
    assert len(r.db.draws(r.account["id"])) == 1
    result = r.recognizer.recognize.return_value
    r.recognition.completed.return_value = [(d["id"], {"status": "complete", "result": result,
                                                      "finished_at": 123, "cached": False})]
    r.draw_flow.check_recognition()
    assert r.db.export(r.account["id"])["totals"]["member"]["SSR"] == 10


def test_corrupt_result_screenshot_refuses_recovery(controller, tmp_path):
    r = controller
    d = r.draw_flow.current_draw()
    (tmp_path / "result.png").write_bytes(b"changed")
    d.update(status="captured", screenshot={"path": "result.png", "sha256": "wrong"})
    r.db.save_draw(d)
    # Screenshot validation belongs to the background worker; do not block UI here.
    r.draw_flow.check_recognition()
    r.recognition.submit.assert_called_once()
    r.recognizer.recognize.assert_not_called()


def test_account_finish_and_batch_count_commit_together(controller, monkeypatch):
    r = controller
    account_id = r.account["id"]
    def crash(_):
        raise OSError("simulated power loss")
    monkeypatch.setattr(r.db, "save_run", crash)
    with pytest.raises(OSError):
        r.finish_account()
    assert r.db.account(account_id)["phase"] == "draws"
    persisted = r.db.get_run()
    assert persisted["completed"] == 0
    assert persisted["active_account"] == account_id


@pytest.mark.parametrize('count', [1, 2])
def test_execute_finishes_accounts_and_advances_without_idle_watchdog_failure(controller, tmp_path, monkeypatch, count):
    import time
    from contextlib import closing

    from ournotes.vision.ocr import Observation

    r = controller
    r.run['count'] = count
    r.db.save_run(r.run)
    r.instance, r.resume = 0, False
    r.last_scene = r.last_known_scene = None
    r.same_scene = 0
    r.last_progress = r.last_account_tick = time.monotonic()
    r.publish, r.mail, r.executor = Mock(), Mock(), Mock()
    r.catalog_directory = tmp_path / 'catalog'
    r.catalog_directory.mkdir()
    for name in ('catalog.json', 'pools.json'):
        (r.catalog_directory / name).write_text('{}', encoding='utf-8')
    completed_ids = []
    image = Image.new('RGB', (1600, 900))

    def completed_frame():
        # Enter the real logout -> finish -> next loop transition with durable results.
        r.account['phase'] = 'logout'
        r.account['plan'] = r.account['plan'][:1]
        draw = r.draw_flow.current_draw()
        card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'name': '测试', 'title': '测试'}
        draw.update(status='complete', result={'slots': [{'status': 'matched', 'card': card}] * draw['pulls']})
        r.db.save_draw(draw)
        r.save()
        completed_ids.append(r.account['id'])
        return image, 0

    device = Mock()
    device.capture.side_effect = completed_frame
    vision = Mock(last_mode='elements', last_metrics={})
    vision.read.return_value = Observation(image, [], 'login_history', 'stable')
    monkeypatch.setattr('ournotes.runtime.runner.ensure_game', Mock())
    monkeypatch.setattr('ournotes.runtime.runner.DeviceClient', lambda _: device)
    monkeypatch.setattr('ournotes.runtime.runner.Observer', lambda: vision)
    # This loop test simulates an available server; outbox failures/admission are
    # covered independently in test_cloud_only_storage.
    monkeypatch.setattr('ournotes.runtime.runner.Runner.wait_for_uploads', lambda self: None)
    run_id = r.run['id']
    r._execute()

    with closing(RuntimeStore(tmp_path)) as db:
        run = db.get_run(run_id)
        assert run['status'] == 'complete', r.log.incident.call_args
        assert run['completed'] == count and run['active_account'] is None
        assert len(set(completed_ids)) == count
        for account_id in completed_ids:
            assert db.account(account_id)['status'] == 'complete'
            assert [d['status'] for d in db.draws(account_id)] == ['complete']
    r.log.incident.assert_not_called()
    device.tap.assert_not_called()


@pytest.mark.parametrize('preset', ['MyGO only', '主抽梦限大 60 抽'])
@pytest.mark.parametrize('missing_result', [False, True])
def test_finished_old_and_new_presets_persist_actual_pull_tag(controller, preset, missing_result):
    r = controller
    r.account['plan'] = make_plan(preset)
    pulls = sum(item['pulls'] for item in r.account['plan'])
    r.save()
    account_id = r.account['id']
    card = {"key": 'member:1', "kind": 'member', "id": 1, "rarity": 'SSR', "name": '测试', "title": '测试'}
    for _ in r.account['plan']:
        draw = r.draw_flow.current_draw()
        draw.update(status='complete', result={'slots': [{"status": 'matched', "card": card}] * draw['pulls']})
        if missing_result:
            draw['result']['slots'] = []
        r.db.save_draw(draw)
    r.finish_account()
    assert r.db.account(account_id).get('pull_count') == (None if missing_result else pulls)
    assert r.db.export(account_id).get('pull_count') == (None if missing_result else pulls)
    assert r.db.get_run()['completed'] == 1


def test_conditional_plan_is_saved_before_next_transaction_and_reused_after_restart(controller):
    r = controller
    r.account['plan'] = make_plan('梦限大四UP转常驻 60 抽')
    r.save()
    first = r.draw_flow.current_draw()
    keys = ['member:61', 'member:62', 'support:62', 'support:63'] + ['member:1'] * 6
    first.update(status='complete', result={'slots': [
        {'status': 'matched', 'card': {'key': key}} for key in keys]})
    r.db.save_draw(first)
    next_draw = r.draw_flow.current_draw()
    saved = r.db.account(r.account['id'])
    assert next_draw['pool_key'] == 'launch-ssr-guaranteed-ticket'
    assert saved['plan'][next_draw['ordinal']]['pool_key'] == next_draw['pool_key']
    assert saved['plan_changes'][0]['redirected_pulls'] == 30
    assert sum(e['pulls'] for e in saved['plan']) == 62
    resumed = Runner()
    resumed.db, resumed.account = r.db, saved
    assert resumed.draw_flow.current_draw()['id'] == next_draw['id']
    assert len(r.db.draws(saved['id'])) == 2
    assert len(saved['plan_changes']) == 1


def test_future_state_version_is_rejected(tmp_path):
    s = RuntimeStore(tmp_path)
    s.db.execute("PRAGMA user_version=999")
    s.close()
    with pytest.raises(ValueError, match="较新版本"):
        RuntimeStore(tmp_path)


def test_character_prompt_is_dismissed_without_restarting_draws(controller):
    r = controller
    r.account["phase"] = "logout"
    r.observation.scene = "character_dialogue"
    r.step()
    r.action.assert_called_once_with("advance-character-dialogue", args=(1100, 770))
    assert r.db.draws(r.account["id"]) == []
    assert r.account["phase"] == "logout"


def test_navigation_retry_is_observation_driven_and_bounded(tmp_path):
    r = Runner()
    r.observation = SimpleNamespace(scene="home", signature="stable", image=Image.new("RGB", (1600, 900)))
    r.account = {"id": "test", "phase": "logout"}
    r.last_action = None
    r.same_scene = 1
    r.navigation_attempts = 0
    r.stop = threading.Event()
    r.log = MagicMock()
    r.db = Mock()
    r.device = Mock()
    r.device.capture.return_value = (r.observation.image, 0)
    assert r.action("home-menu", args=(1541, 58)) is False
    for _ in range(3):
        r.same_scene = 3
        r.last_action_at = 0
        assert r.action("home-menu", args=(1541, 58)) is True
        assert r.same_scene == 0
    r.same_scene = 3
    r.last_action_at = 0
    with pytest.raises(PauseRun, match="三次"):
        r.action("home-menu", args=(1541, 58))
    assert r.device.tap.call_count == 3


def test_disappearing_popup_prevents_stale_navigation_click():
    r = Runner()
    r.observation = SimpleNamespace(scene="startup_reward", signature="old", image=Image.new("RGB", (1600, 900)))
    r.account = {"id": "test", "phase": "logout"}
    r.last_action = None
    r.same_scene = 2
    r.navigation_attempts = 0
    r.stop = threading.Event()
    r.log = MagicMock()
    r.db = Mock()
    r.device = Mock()
    r.device.capture.return_value = (Image.new("RGB", (1600, 900), "white"), 0)
    assert r.action("dismiss-reward-OK", args=(800, 825)) is False
    r.device.tap.assert_not_called()
    r.db.action.assert_not_called()


def test_home_recruit_retries_wait_for_startup_without_exceeding_three_taps(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: clock[0])
    r = Runner()
    frame = Image.new('RGB', (1600, 900))
    r.observation = SimpleNamespace(scene='home', signature='home', image=frame, fast=True)
    r.account = {'id': 'test', 'phase': 'draws'}
    r.stop, r.log, r.db, r.device = threading.Event(), MagicMock(), Mock(), Mock()
    r.device.capture.return_value = (frame, 0)
    for tick in (100, 110, 120):
        clock[0] = tick
        assert r.action('招募', args=(723, 772))
        clock[0] = tick + 9.9
        assert not r.action('招募', args=(723, 772))
    clock[0] = 130
    with pytest.raises(PauseRun, match='页面导航三次未生效'):
        r.action('招募', args=(723, 772))
    assert r.device.tap.call_count == 3


@pytest.mark.parametrize(('scene', 'label'), [('animation', 'SKIP'), ('opening_movie', 'skip-opening-movie')])
def test_ten_reveals_are_not_three_failed_navigation_attempts(monkeypatch, scene, label):
    now = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: now[0])
    r = Runner()
    r.observation = SimpleNamespace(scene=scene, signature=scene, image=Image.new('RGB', (1600, 900)))
    r.account = {'id': 'test', 'phase': 'draws'}
    r.last_action, r.same_scene, r.navigation_attempts = None, 2, 0
    r.stop, r.log, r.db, r.device = threading.Event(), MagicMock(), Mock(), Mock()
    r.device.capture.return_value = (r.observation.image, 0)
    for _ in range(11):
        assert r.action(label, args=(1500, 60))
        assert not r.action(label, args=(1500, 60))  # Still observe during the click cooldown.
        now[0] += 1
    now[0] += 60
    with pytest.raises(PauseRun, match='60秒'):
        r.action(label, args=(1500, 60))


def test_network_error_never_clicks_the_retry_word_inside_instructions(controller, monkeypatch):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    clock = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.time', lambda: clock[0])
    r.observation = Observation(Image.new('RGB',(1600,900)), [
        TextBox('请在网络环境良好的地方重试。',1,(612,413,977,442)),
        TextBox('返回标题画面',1,(713,700,887,730))], 'network_error', 'error')
    r.step()
    r.action.assert_not_called()
    clock[0] += 2
    r.step()
    r.action.assert_called_once_with('返回标题画面', args=(800,715))
    assert r.ctx['network_retries'] == 1 and r.ctx['uid_verified'] is False
    r.phase.assert_called_once_with('rewards')


def test_network_error_after_submission_cannot_leave_result_recovery(controller):
    r = controller
    d = r.draw_flow.current_draw()
    d['status'] = 'submitted'
    r.db.save_draw(d)
    r.observation.scene = 'network_error'
    account_id = r.account['id']
    r.step()
    assert r.account is None
    assert r.db.account(account_id)['status'] == 'cancelled'
    assert r.run['completed'] == 0
    assert r.db.draws(account_id) == [d]
    r.action.assert_not_called()


def test_submission_abandon_is_durable_idempotent_and_requires_reset(controller, monkeypatch):
    from ournotes.runtime.abandon import retire_submission_error
    r = controller
    account = dict(r.account)
    d = r.draw_flow.current_draw()
    d['status'] = 'submitted'
    r.db.save_draw(d)
    counters = r.db.counters()
    reason = '已提交抽卡期间发生网络错误，交易需要核对。'
    r.abandon_submission(reason)
    audit = retire_submission_error(r.db, r.run, account, reason)
    assert audit['transaction_ids'] == [d['id']]
    assert r.db.counters() == counters and r.run['completed'] == 0
    assert len(r.run['excluded_accounts']) == 1
    assert r.db.draws(account['id']) == [d]
    assert not (r.db.root / 'state/cloud-sync.sqlite3').exists()
    r.instance = 2
    r.stop = threading.Event()
    restart = Mock(side_effect=RuntimeError('restart failed'))
    monkeypatch.setattr('ournotes.runtime.runner.restart_game', restart)
    with pytest.raises(RuntimeError, match='restart failed'):
        r.reset_after_abandonment()
    assert r.db.get_run(r.run['id'])['abandoned_game_reset']['status'] == 'restarting'
    assert r.account is None
    restart.side_effect = None
    monkeypatch.setattr('ournotes.runtime.runner.DeviceClient', Mock())
    monkeypatch.setattr('ournotes.runtime.runner.Observer', Mock())
    r.reset_after_abandonment()
    assert r.db.get_run(r.run['id'])['abandoned_game_reset']['status'] == 'complete'
    r.reset_after_abandonment()
    assert restart.call_count == 2


def test_late_retired_recognition_cannot_attach_to_current_account(controller):
    r = controller
    r.recognition.completed.return_value = [('retired-draw', {'status': 'complete'})]
    r.draw_flow.check_recognition(schedule=False)
    assert r.db.draws(r.account['id']) == []


def test_network_backoff_and_account_budget_survive_reload(controller, monkeypatch):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    clock = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.time', lambda: clock[0])
    r.observation = Observation(Image.new('RGB', (1600, 900)),
                               [TextBox('重试', 1, (700, 700, 900, 750))], 'network_error', 'error')
    r.step()
    for attempt, deadline in enumerate((102, 106, 114), 1):
        r.account = r.db.account(r.account['id'])
        clock[0] = deadline - .01
        r.step()
        assert r.action.call_count == attempt - 1
        clock[0] = deadline
        r.step()
        assert r.action.call_count == attempt
        assert r.db.account(r.account['id'])['context']['network_retries'] == attempt
    r.account = r.db.account(r.account['id'])
    clock[0] = 121.99
    r.step()  # The final retry still gets time to return a response.
    clock[0] = 122
    with pytest.raises(PauseRun, match='网络重试次数'):
        r.step()
    assert r.action.call_count == 3
    assert r.db.draws(r.account['id']) == []


def test_network_guard_rejection_does_not_consume_retry(controller):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    r.ctx['network_retry_at'] = 0
    r.observation = Observation(Image.new('RGB', (1600, 900)),
                               [TextBox('返回标题画面', 1, (700, 700, 900, 750))], 'network_error', 'error')
    r.action.return_value = False
    r.step()
    assert not r.ctx.get('network_retries')
    r.phase.assert_not_called()


@pytest.mark.parametrize('scene', ['loading', 'pool', 'unknown', 'draw_confirm', 'downloading'])
def test_submitted_slow_network_waits_180_seconds_without_restart(controller, monkeypatch, scene):
    r = controller
    restart = Mock()
    monkeypatch.setattr('ournotes.runtime.runner.restart_game', restart)
    d = r.draw_flow.current_draw()
    d['status'] = 'submitted'
    r.db.save_draw(d)
    r.observation.scene = scene
    r.last_progress = 0
    r.check_progress_timeout(0)
    for now in (90, 179.9):
        assert not r.recover_loading_stall(now)
        r.check_progress_timeout(now)
    account_id = r.account['id']
    r.check_progress_timeout(180)
    assert r.account is None and r.db.account(account_id)['status'] == 'cancelled'
    r.click.assert_not_called()
    r.action.assert_not_called()
    restart.assert_not_called()
    assert r.db.draws(account_id) == [d]


def test_draw_wait_budget_survives_scene_changes_and_ends_on_capture(controller):
    r = controller
    d = r.draw_flow.current_draw()
    d['status'] = 'submitted'
    r.db.save_draw(d)
    r.check_progress_timeout(0)
    for now, scene in [(60, 'loading'), (120, 'unknown'), (179, 'draw_result')]:
        r.observation.scene = scene
        r.last_progress = now
        r.check_progress_timeout(now)
    d['status'] = 'captured'
    r.db.save_draw(d)
    r.check_progress_timeout(180)
    assert r.draw_wait_started is None


def test_fresh_network_overlay_does_not_create_submission_intent(controller):
    from ournotes.paths import REFERENCE
    from ournotes.ui.observer import Observer
    r = controller
    r.vision = Observer(allow_fallback=False)
    confirmation = Image.open(REFERENCE / 'gacha-workflow/screens/03-mygo-confirm.png').convert('RGB')
    r.observation = r.vision.read(confirmation, phase='draws', account_id=r.account['id'])
    assert r.observation.scene == 'draw_confirm'
    error = Image.open(REFERENCE / 'runtime-regressions/network-return-title.png').convert('RGB')
    r.device = Mock()
    r.device.capture.return_value = (error, 0)
    r.draw_flow.draws()
    assert r.observation.scene == 'network_error'
    assert r.db.draws(r.account['id'])[0]['status'] == 'planned'
    r.click.assert_not_called()
    r.action.assert_not_called()


def test_confirmation_validates_fresh_fields_and_keeps_sdk_uncertainty(controller, monkeypatch):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    old = Observation(Image.new('RGB', (1600, 900)),
                      [TextBox('OK', 1, (700, 700, 900, 750))], 'draw_confirm', 'old')
    fresh = Observation(old.image.copy(), list(old.texts), 'draw_confirm', 'fresh')
    r.observation = old
    r.device = Mock()
    r.device.capture.return_value = (fresh.image, 0)
    r.vision = Mock()
    r.vision.read.return_value = fresh
    r.vision.guard.return_value = True
    validate = Mock(return_value={'verified': True})
    monkeypatch.setattr('ournotes.runtime.draw_flow.validate_confirmation', validate)
    r.log.screenshot.return_value = {}
    r.click.side_effect = RuntimeError('SDK input outcome unknown')
    with pytest.raises(RuntimeError, match='SDK input'):
        r.draw_flow.draws()
    assert validate.call_args.args[0] is fresh
    saved = r.db.draws(r.account['id'])
    assert len(saved) == 1 and saved[0]['status'] == 'submitted'
    r.click.reset_mock()
    r.draw_flow.draws()
    r.click.assert_not_called()


def test_restart_before_confirm_reenters_same_account_and_keeps_plan(controller):
    r = controller
    d = r.draw_flow.current_draw()
    r.observation.scene = 'title'
    r.ctx['uid_verified'] = True
    r.draw_flow.draws()
    r.phase.assert_called_once_with('rewards')
    assert r.ctx['uid_verified'] is False
    assert r.draw_flow.current_draw()['id'] == d['id']
    r.action.assert_not_called()


def test_restart_after_submission_refuses_a_new_draw(controller):
    r = controller
    d = r.draw_flow.current_draw()
    d['status'] = 'submitted'
    r.db.save_draw(d)
    r.observation.scene = 'title'
    r.draw_flow.draws()
    assert r.account is None
    r.phase.assert_not_called()


@pytest.mark.parametrize('status', ['planned', 'submitted', 'captured'])
def test_announcement_during_draw_only_closes_modal_and_keeps_transaction(controller, status):
    r = controller
    draw = r.draw_flow.current_draw()
    draw['status'] = status
    r.db.save_draw(draw)
    before = r.db.draws(r.account['id'])
    r.observation.scene = 'notice'
    r.draw_flow.draws()
    r.click.assert_called_once_with('关闭', exact=True)
    r.phase.assert_not_called()
    assert r.db.draws(r.account['id']) == before
    assert r.account['skipped'] == []


def test_verified_uid_does_not_lose_pending_close_action(controller):
    r = controller
    r.account["phase"] = "rewards"
    r.ctx["uid_verified"] = True
    r.observation.scene = "user_center"
    r.reward_flow.rewards()
    r.action.assert_called_once_with("close-user-center", args=(612, 258))


@pytest.mark.parametrize("label", ["verify-account-menu", "title-menu", "TAP TO START"])
def test_title_movie_change_keeps_control_valid_but_modal_still_blocks(label):
    from ournotes.paths import ASSETS, REFERENCE
    r = Runner()
    first = Image.open(REFERENCE / "runtime-regressions/title-animation-a.png").convert("RGB")
    second = Image.open(REFERENCE / "runtime-regressions/title-animation-b.png").convert("RGB")
    r.observation = SimpleNamespace(scene="title", signature="title", image=first)
    r.account = {"id": "test", "phase": "rewards"}
    r.last_action, r.same_scene, r.navigation_attempts = None, 2, 0
    r.stop, r.log, r.db, r.device = threading.Event(), MagicMock(), Mock(), Mock()
    r.device.capture.return_value = (second, 0)
    assert r.action(label, args=(1541, 58)) is True
    r.last_action, r.same_scene = None, 2
    r.device.capture.return_value = (Image.open(ASSETS / "login-history/history-initial.png"), 0)
    assert r.action(label, args=(1541, 58)) is False
    assert r.device.tap.call_count == 1


def test_unopened_title_menu_uses_existing_uid_verification_path(controller):
    r = controller
    r.account['phase'] = 'rewards'
    r.account['game_uid'] = '123456789'
    r.ctx['uid_verified'] = True
    r.observation = SimpleNamespace(scene='title', signature='title', fast=True)
    r.last_action = ('verify-account-menu', 'title')
    r.last_action_scene = 'title'
    r.last_action_at = 0
    r.navigation_attempts = 3
    before = r.db.draws(r.account['id'])
    assert Runner.action(r, 'verify-account-menu') is False
    assert r.db.account(r.account['id'])['context']['title_auth_recovery'] is True
    assert r.ctx['uid_verified'] is False
    assert r.account['game_uid'] == '123456789' and r.db.draws(r.account['id']) == before
    r.reward_flow.rewards()
    r.click.assert_called_once_with('TAP TO START', exact=True)
    r.observation.scene = 'home'
    r.reward_flow.rewards()
    r.action.assert_called_once_with('home-menu', args=(1541, 51))
    r.phase.assert_not_called()


def test_title_fallback_limit_survives_restart_and_preserves_submitted_draw(controller):
    r = controller
    r.ctx['title_auth_recovery_attempts'] = 2
    r.save()
    r.account = r.db.account(r.account['id'])
    with pytest.raises(PauseRun, match='恢复已达上限'):
        r.recover_unopened_menu()
    r.ctx.clear()
    draw = r.draw_flow.current_draw()
    draw['status'] = 'submitted'
    r.db.save_draw(draw)
    with pytest.raises(PauseRun, match='已提交交易'):
        r.recover_unopened_menu()
    assert r.ctx == {}
    assert r.db.draws(r.account['id'])[0]['status'] == 'submitted'


def test_missed_button_uses_scoped_ocr_before_opening_confirmation(controller):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    draw = r.draw_flow.current_draw()
    draw['pulls'] = 1
    r.observation = Observation(Image.new('RGB', (1600, 900)), [], 'pool', 'pool')
    def read(name, transaction):
        assert name == 'draw_button_1' and transaction == draw['id']
        r.observation.texts.append(TextBox('招募 1 次', .98, (1080, 690, 1220, 720)))
    r.read_fields = Mock(side_effect=read)
    r.draw_flow.open_draw_confirmation(draw)
    r.click.assert_called_once_with('招募1次', exact=True, region=(1030, 650, 1295, 780),
                                    label='open-draw-confirmation')
    assert r.db.draws(r.account['id'])[0]['status'] == 'planned'


def test_button_ocr_does_not_guess_coordinates_and_is_bounded(controller, monkeypatch):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    draw = r.draw_flow.current_draw()
    draw['pulls'] = 1
    r.observation = Observation(Image.new('RGB', (1600, 900)),
                                [TextBox('招募10次', 1, (1360, 690, 1520, 720))], 'pool', 'pool')
    r.read_fields = Mock()
    now = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: now[0])
    for _ in range(3):
        r.draw_flow.open_draw_confirmation(draw)
        now[0] += 3
    r.account = r.db.account(r.account['id'])
    with pytest.raises(PauseRun, match='局部识别已达三次上限'):
        r.draw_flow.open_draw_confirmation(draw)
    assert r.read_fields.call_count == 3
    r.click.assert_not_called()


def test_pause_before_submission_does_not_allocate_or_confirm(controller):
    r = controller
    r.stop = threading.Event(); r.stop.set()
    r.observation.scene = 'draw_confirm'
    with pytest.raises(PauseRun, match='停止新输入'):
        r.step()
    assert r.db.draws(r.account['id']) == []
    r.click.assert_not_called()


@pytest.mark.parametrize('global_stop', [True, False])
def test_safe_pause_waits_for_result_and_captures_without_new_draw(controller, global_stop):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    r.stop = threading.Event()
    draw = r.draw_flow.current_draw(); draw.update(status='submitted', pulls=1); r.db.save_draw(draw)
    if global_stop: r.stop.set()
    assert r.check_pause(None if global_stop else '实例暂停')
    r.observation.scene = 'loading'
    r.step()
    r.click.assert_not_called()
    r.same_scene = 2
    r.result_stability = None
    r.layout = Mock()
    r.layout.inspect.return_value = ([700], {})
    r.log.screenshot.return_value = {'path': 'result.png', 'sha256': 'test'}
    r.observation = Observation(Image.new('RGB', (1600, 900)),
                                [TextBox('招募TOP', 1, (750, 800, 850, 850))], 'draw_result', 'result')
    r.click = Runner.click.__get__(r)
    r.action = Runner.action.__get__(r)
    r.last_action = None
    r.step(); r.step()
    stored = r.db.draws(r.account['id'])
    assert len(stored) == 1 and stored[0]['status'] == 'captured'
    r.recognition.submit.assert_called_once()
    with pytest.raises(PauseRun, match='后台识别'):
        r.step()
    assert len(r.db.draws(r.account['id'])) == 1


def test_result_capture_waits_for_artwork_even_after_stable_ocr_result(controller):
    from ournotes.paths import ROOT
    from ournotes.vision.ocr import Observation
    from ournotes.vision.recognition import ResultLayout

    r = controller
    draw = r.draw_flow.current_draw()
    draw.update(status='submitted', pulls=10)
    r.db.save_draw(draw)
    r.same_scene, r.result_stability, r.layout = 3, None, ResultLayout()
    fixture = ROOT / 'assets/reference/runtime-regressions/settlement-loading-2328'
    r.observation = Observation(Image.open(fixture / 'failed.png'), [], 'draw_result', 'ocr')
    for _ in range(3):
        r.draw_flow.draws()
    assert r.db.draws(r.account['id'])[0]['status'] == 'submitted'
    r.log.screenshot.assert_not_called()
    r.recognition.submit.assert_not_called()
    r.click.assert_not_called()
    r.observation = Observation(Image.open(fixture / 'independent-ready.png'), [], 'draw_result', 'result')
    r.log.screenshot.return_value = {'path': 'result.png', 'sha256': 'test'}
    r.draw_flow.draws()
    assert r.db.draws(r.account['id'])[0]['status'] == 'submitted'
    r.draw_flow.draws()
    stored = r.db.draws(r.account['id'])
    assert len(stored) == 1 and stored[0]['id'] == draw['id'] and stored[0]['status'] == 'captured'
    r.recognition.submit.assert_called_once()
    r.click.assert_called_once_with('招募TOP')


def test_safe_pause_deadline_does_not_reset_and_never_retries_confirmation(controller, monkeypatch):
    r = controller
    now = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: now[0])
    draw = r.draw_flow.current_draw(); draw.update(status='submitted', submitted_at=0); r.db.save_draw(draw)
    r.stop = threading.Event(); r.stop.set()
    r.observation.scene = 'draw_confirm'
    r.step(); r.step()
    r.action.assert_called_once_with('release-confirm-touch', kind='release_touch')
    r.click.assert_not_called()
    now[0] += 91
    with pytest.raises(PauseRun, match='收尾超时'):
        r.step()
    assert r.db.draws(r.account['id'])[0]['status'] == 'submitted'


def test_safe_pause_on_lost_result_keeps_uncertain_transaction(controller):
    r = controller
    draw = r.draw_flow.current_draw(); draw['status'] = 'submitted'; r.db.save_draw(draw)
    r.stop = threading.Event(); r.stop.set()
    r.observation.scene = 'title'
    with pytest.raises(PauseRun, match='禁止补抽'):
        r.step()
    r.click.assert_not_called(); r.phase.assert_not_called()
    assert r.db.draws(r.account['id'])[0]['status'] == 'submitted'


def test_execute_drains_recognition_before_device_close(controller, tmp_path, monkeypatch):
    import time

    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    account_id, run_id = r.account['id'], r.run['id']
    draw = r.draw_flow.current_draw(); draw.update(status='submitted', pulls=1); r.db.save_draw(draw)
    r.stop = threading.Event()
    r.stop.set()
    r.instance = 0; r.resume = True
    r.last_scene = r.last_known_scene = None
    r.same_scene = 0; r.last_action = None; r.result_stability = None
    r.last_progress = r.last_account_tick = time.monotonic()
    r.publish = Mock(); r.mail = Mock(); r.executor = Mock()
    r.layout = Mock(); r.layout.inspect.return_value = ([700], {})
    r.click = Runner.click.__get__(r); r.action = Runner.action.__get__(r)
    image = Image.new('RGB', (1600, 900))
    device = Mock(); device.capture.return_value = (image, 0)
    events = []
    device.close.side_effect = lambda: events.append('device_closed')
    scenes = iter(['loading', 'draw_result', 'draw_result', 'draw_result'])
    vision = Mock(last_mode='elements', last_metrics={})
    vision.read.side_effect = lambda *args, **kw: Observation(image,
        [TextBox('招募TOP', 1, (750, 800, 850, 850))], next(scenes), 'stable')
    monkeypatch.setattr('ournotes.runtime.runner.STATE', tmp_path/'state')
    monkeypatch.setattr('ournotes.runtime.runner.ensure_game', Mock())
    monkeypatch.setattr('ournotes.runtime.runner.DeviceClient', lambda instance: device)
    monkeypatch.setattr('ournotes.runtime.runner.Observer', lambda: vision)
    queued = {}; closed = [False]; delivered = [False]
    r.recognition.submit.side_effect = lambda d: queued.setdefault(d['id'], d)
    def close():
        closed[0] = True; events.append('recognition_closed')
    r.recognition.close.side_effect = close
    def completed():
        if not closed[0] or delivered[0]: return []
        delivered[0] = True
        events.append('recognition_collected')
        card = {'key': 'member:56', 'id': 56, 'kind': 'member', 'rarity': 'SSR', 'title': '测试', 'name': '测试'}
        return [(draw['id'], {'status': 'complete', 'finished_at': time.time(),
                    'result': {'status': 'complete', 'layout': 1, 'slots': [{'status': 'matched', 'card': card}]}})]
    r.recognition.completed.side_effect = completed
    def screenshot(im, name, *args, **kw):
        if kw.get('persist'):
            path = tmp_path/'result.png'; im.save(path)
            return {'path': 'result.png', 'sha256': file_hash(path)}
        return {}
    r.log.screenshot.side_effect = screenshot
    r._execute()
    assert list(queued) == [draw['id']], r.log.incident.call_args
    assert events == ['recognition_closed', 'recognition_collected', 'device_closed']
    device.tap.assert_not_called()
    store = RuntimeStore(tmp_path)
    assert store.get_run(run_id)['status'] == 'paused'
    assert store.get_run(run_id)['completed'] == 0
    saved = store.draws(account_id)
    assert len(saved) == 1 and saved[0]['status'] == 'complete'
    assert (tmp_path/'result.png').exists()
    store.close()


def test_low_commit_does_not_mark_draw_submitted(controller, monkeypatch):
    from ournotes.common.memory import GIB, CommitMemory
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    draw = r.draw_flow.current_draw()
    r.observation = Observation(Image.new('RGB', (1600, 900)),
                               [TextBox('OK', 1, (700, 700, 900, 750))], 'draw_confirm', 'confirm')
    r.refresh_draw_confirmation = Mock(return_value=True)
    r.read_fields = Mock()
    monkeypatch.setattr('ournotes.runtime.draw_flow.validate_confirmation', lambda *args: {'verified': True})
    monkeypatch.setattr('ournotes.common.memory.read_commit_memory',
                        lambda: CommitMemory(159 * GIB, 164 * GIB, 19 * GIB))
    with pytest.raises(PauseRun, match='系统提交内存不足'):
        r.draw_flow.draws()
    saved = r.db.draws(r.account['id'])[0]
    assert saved['id'] == draw['id'] and saved['status'] == 'planned'
    assert 'submitted_at' not in saved
    r.click.assert_not_called()
    r.action.assert_not_called()


def test_stop_between_saved_intent_and_tap_finishes_only_that_commit(controller, monkeypatch):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    draw = r.draw_flow.current_draw()
    r.stop = threading.Event()
    r.observation = Observation(Image.new('RGB', (1600, 900)),
                                [TextBox('OK', 1, (700, 700, 900, 750))], 'draw_confirm', 'confirm')
    r.last_action = None; r.navigation_attempts = 0; r.same_scene = 2
    r.device = Mock(); r.log.screenshot.return_value = {}
    r.device.capture.return_value = (r.observation.image, 0)
    r.vision = Mock()
    r.vision.read.return_value = r.observation
    r.vision.guard.return_value = True
    r.click = Runner.click.__get__(r); r.action = Runner.action.__get__(r)
    monkeypatch.setattr('ournotes.runtime.draw_flow.validate_confirmation', lambda *args: {'verified': True})
    save = r.db.save_draw
    def request_pause(d):
        save(d)
        if d['status'] == 'submitted': r.stop.set()
    monkeypatch.setattr(r.db, 'save_draw', request_pause)
    r.draw_flow.draws()
    r.device.tap.assert_called_once_with(800, 725)
    assert r.db.draws(r.account['id'])[0]['id'] == draw['id']
    r.step()
    assert r.device.tap.call_count == 1


def test_recovered_title_does_not_accept_different_uid(controller):
    from ournotes.vision.ocr import Observation, TextBox
    r = controller
    r.account['phase'] = 'rewards'
    r.account['game_uid'] = '123456789'
    r.ctx.update(title_auth_recovery=True, uid_verified=False)
    r.observation = Observation(Image.new('RGB', (1600, 900)),
                                [TextBox('UID:987654321', 1, (588, 336, 1010, 374))], 'user_center', 'uid')
    with pytest.raises(PauseRun, match='UID与保存记录不符'):
        r.reward_flow.rewards()
    assert r.ctx['uid_verified'] is False
    r.phase.assert_not_called()


@pytest.mark.parametrize('phase', ['switch_account', 'logout', 'rewards', 'gifts'])
def test_unresponsive_blue_user_center_uses_bounded_title_fallback(controller, phase):
    r = controller
    r.account['phase'] = phase
    r.account['game_uid'] = '123456789'
    r.ctx['uid_verified'] = True
    r.observation = SimpleNamespace(scene='title_menu', signature='menu', fast=True)
    r.last_action = ('用户中心', 'menu')
    r.last_action_scene = 'title_menu'
    r.last_action_at = 0
    r.navigation_attempts = 3
    r.click.return_value = True
    before = r.db.draws(r.account['id'])
    assert Runner.action(r, '用户中心') is False
    r.click.assert_called_once_with('关闭', exact=True, label='close-unavailable-menu')
    assert r.db.account(r.account['id'])['context']['title_auth_recovery_attempts'] == 1
    assert r.ctx['title_auth_recovery']
    if phase in ('rewards', 'gifts'):
        assert r.ctx['uid_verified'] is False
    assert r.account['game_uid'] == '123456789'
    assert r.db.draws(r.account['id']) == before
    r.account = r.db.account(r.account['id'])
    r.ctx['title_auth_recovery_attempts'] = 2
    with pytest.raises(PauseRun, match='用户中心持续不可用'):
        Runner.action(r, '用户中心')
    assert r.click.call_count == 1


@pytest.mark.parametrize('scene', ['title_menu_unavailable', 'title_menu'])
def test_user_center_fallback_never_leaves_submitted_transaction(controller, scene):
    r = controller
    r.account['phase'] = 'rewards'
    draw = r.draw_flow.current_draw()
    draw['status'] = 'submitted'
    r.db.save_draw(draw)
    r.observation = SimpleNamespace(scene=scene, signature='menu', fast=True)
    r.last_action = ('用户中心', 'menu')
    r.last_action_scene = scene
    r.last_action_at = 0
    r.navigation_attempts = 3
    with pytest.raises(PauseRun, match='已提交交易'):
        if scene == 'title_menu':
            Runner.action(r, '用户中心')
        else:
            r.step()
    r.click.assert_not_called()
    assert 'title_auth_recovery_attempts' not in r.ctx
    assert r.db.draws(r.account['id'])[0]['status'] == 'submitted'


def test_unresponsive_home_user_center_does_not_loop_back_through_title(controller):
    r = controller
    r.account['phase'] = 'rewards'
    r.observation = SimpleNamespace(scene='home_menu', signature='menu', fast=True)
    r.last_action = ('用户中心', 'menu')
    r.last_action_scene = 'home_menu'
    r.last_action_at = 0
    r.navigation_attempts = 3
    with pytest.raises(PauseRun, match='页面导航三次未生效'):
        Runner.action(r, '用户中心')
    r.click.assert_not_called()


@pytest.mark.parametrize('changed', [False, True])
def test_blue_menu_fallback_closes_only_the_current_foreground(controller, changed):
    from ournotes.paths import REFERENCE
    from ournotes.ui.observer import Observer
    r = controller
    r.account['phase'] = 'rewards'
    r.vision = Observer(allow_fallback=False)
    frame = Image.open(REFERENCE / 'runtime-regressions/title-user-center-unresponsive.png').convert('RGB')
    r.observation = r.vision.read(frame, phase='rewards', account_id=r.account['id'])
    assert r.observation.scene == 'title_menu'
    r.last_action = ('用户中心', r.observation.signature)
    r.last_action_scene = 'title_menu'
    r.last_action_at = 0
    r.same_scene = 3
    r.navigation_attempts = 3
    r.stop = threading.Event()
    r.device = Mock()
    r.device.capture.return_value = (Image.new('RGB', frame.size) if changed else frame, 0)
    r.log.screenshot.return_value = 'evidence.png'
    r.action = Runner.action.__get__(r)
    r.click = Runner.click.__get__(r)
    assert r.action('用户中心', args=(655, 373)) is False
    if changed:
        r.device.tap.assert_not_called()
        assert 'title_auth_recovery_attempts' not in r.ctx
    else:
        r.device.tap.assert_called_once_with(801, 715)
        assert r.ctx['title_auth_recovery_attempts'] == 1
        assert r.db.account(r.account['id'])['context']['uid_verified'] is False


@pytest.fixture
def app_restart(controller, monkeypatch):
    r = controller
    r.stop = threading.Event()
    r.instance = 2
    r.device = Mock()
    r.observation = SimpleNamespace(scene='loading')
    restart = Mock()
    monkeypatch.setattr('ournotes.runtime.runner.restart_game', restart)
    monkeypatch.setattr('ournotes.runtime.runner.DeviceClient', Mock())
    monkeypatch.setattr('ournotes.runtime.runner.Observer', Mock())
    return r, restart


@pytest.fixture
def logout_title_stall(app_restart):
    r, restart = app_restart
    r.account['phase'] = 'logout'
    r.ctx.update(logout_requested=True, login_confirmed=True, login_submitted=True)
    r.observation = SimpleNamespace(scene='title')
    r.title_start_wait = {'account_id': r.account['id'], 'started_at': 0,
                          'last_tap_at': 20, 'attempts': 3}
    draw = r.draw_flow.current_draw()
    draw.update(status='complete', result_exit_confirmed=True)
    r.db.save_draw(draw)
    r.save()
    return r, restart, draw


def test_logout_title_timeout_restarts_once_and_requires_verified_login(logout_title_stall, monkeypatch):
    import copy
    r, restart, draw = logout_title_stall
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: 90)
    identity = copy.deepcopy(r.account)
    old_device = r.device
    r.finish_account = Mock()

    def verify_persisted_budget(*args):
        assert r.db.account(r.account['id'])['context']['game_restart_attempts'] == 1

    restart.side_effect = verify_persisted_budget
    assert not r.recover_logout_title_stall(89.9)
    assert not Runner.action(r, 'TAP TO START', args=(1188, 732))
    restart.assert_called_once_with(2, r.stop)
    old_device.close.assert_called_once_with()
    old_device.tap.assert_not_called()
    assert all(r.account[k] == identity[k] for k in ('id', 'alias', 'alias_index', 'plan', 'phase'))
    assert r.ctx['logout_requested'] and r.ctx['game_restart_attempts'] == 1
    assert r.db.draws(r.account['id']) == [draw] and r.run['completed'] == 0
    assert r.observation is None and r.title_start_wait is None
    r.finish_account.assert_not_called()
    r.observation = SimpleNamespace(scene='title')
    r.login_flow.switch_account()
    r.click.assert_called_once_with('TAP TO START', exact=True)
    r.finish_account.assert_not_called()
    r.observation.scene = 'login_options'
    r.login_flow.switch_account()
    r.finish_account.assert_called_once_with()


def test_logout_title_restart_inside_action_discards_current_execute_cycle(logout_title_stall, monkeypatch):
    from ournotes.vision.ocr import Observation, TextBox
    r, restart, _ = logout_title_stall
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: 90)
    r.resume = False
    r.last_scene = r.last_known_scene = None
    r.same_scene = 0
    r.last_account_tick = r.last_progress = 89
    r.publish, r.mail, r.executor = Mock(), Mock(), Mock()
    r.check_memory = r.check_storage = r.wait_for_admission = Mock()
    r.check_progress_timeout = Mock(side_effect=AssertionError('invalidated frame reached watchdog'))
    frame = Image.new('RGB', (1600, 900))
    old_device, new_device = Mock(), Mock()
    old_device.capture.return_value = (frame, 0)
    new_device.capture.side_effect = PauseRun('测试结束')
    vision = Mock(last_mode='elements', last_metrics={})
    vision.read.return_value = Observation(
        frame, [TextBox('TAP TO START', 1, (1100, 710, 1276, 754))], 'title', 'title')
    monkeypatch.setattr('ournotes.runtime.runner.ensure_game', Mock())
    monkeypatch.setattr('ournotes.runtime.runner.DeviceClient', Mock(side_effect=[old_device, new_device]))
    monkeypatch.setattr('ournotes.runtime.runner.Observer', lambda: vision)
    r.action, r.click = Runner.action.__get__(r), Runner.click.__get__(r)
    recover = r.recover_logout_title_stall
    calls = []

    def defer_pre_step(now):
        calls.append(now)
        # Simulate crossing the deadline after the loop's pre-step check.
        return False if len(calls) == 1 else recover(now)

    r.recover_logout_title_stall = defer_pre_step
    r._execute()
    assert r.run['pause_reason'] == '测试结束', r.log.incident.call_args
    restart.assert_called_once_with(2, r.stop)
    assert old_device.capture.call_count == 1 and new_device.capture.call_count == 1
    r.check_progress_timeout.assert_not_called()
    assert r.run['completed'] == 0 and r.run['status'] == 'paused'


@pytest.mark.parametrize('status', ['planned', 'error', 'skipped', 'submitted', 'captured', 'needs_review'])
def test_logout_title_restart_rejects_unresolved_draw(logout_title_stall, status):
    r, restart, draw = logout_title_stall
    draw['status'] = status
    r.db.save_draw(draw)
    with pytest.raises(PauseRun, match='禁止重启游戏'):
        r.recover_logout_title_stall(90)
    restart.assert_not_called()
    r.device.close.assert_not_called()
    assert r.db.draws(r.account['id']) == [draw]
    assert 'game_restart_attempts' not in r.ctx


def test_logout_title_restart_requires_existing_draws(logout_title_stall):
    r, restart, draw = logout_title_stall
    r.db.db.execute('DELETE FROM draws WHERE id=?', (draw['id'],))
    with pytest.raises(PauseRun, match='交易未全部完成'):
        r.recover_logout_title_stall(90)
    restart.assert_not_called()
    r.device.close.assert_not_called()
    assert 'game_restart_attempts' not in r.ctx


@pytest.mark.parametrize('guard', ['stop', 'pause', 'mail_limit', 'login_network_uncertain'])
def test_logout_title_restart_respects_pause_and_request_guards(logout_title_stall, guard):
    r, restart, _ = logout_title_stall
    if guard == 'stop':
        r.stop.set()
    elif guard == 'pause':
        r.pause_reason = '用户暂停'
    elif guard == 'mail_limit':
        r.run['mail_send_limit_reached'] = True
    else:
        r.ctx['login_network_uncertain'] = True
    if guard in ('stop', 'pause'):
        assert not r.recover_logout_title_stall(90)
    else:
        with pytest.raises(PauseRun):
            r.recover_logout_title_stall(90)
    restart.assert_not_called()
    r.device.close.assert_not_called()
    assert 'game_restart_attempts' not in r.ctx


@pytest.mark.parametrize('first', ['loading', 'logout_title'])
def test_loading_and_logout_title_share_restart_budget_after_reload(logout_title_stall, first):
    r, restart, _ = logout_title_stall
    if first == 'loading':
        r.observation.scene = 'loading'
        assert not r.recover_loading_stall(0)
        assert r.recover_loading_stall(90)
    else:
        assert r.recover_logout_title_stall(90)
    r.account = r.db.account(r.account['id'])
    r.observation = SimpleNamespace(scene='title' if first == 'loading' else 'loading')
    r.title_start_wait = {'account_id': r.account['id'], 'started_at': 100,
                          'last_tap_at': 120, 'attempts': 3}
    if first == 'loading':
        with pytest.raises(PauseRun, match='一次后仍未恢复'):
            r.recover_logout_title_stall(190)
    else:
        assert not r.recover_loading_stall(100)
        with pytest.raises(PauseRun, match='一次后仍未恢复'):
            r.recover_loading_stall(190)
    assert restart.call_count == 1


def test_failed_logout_title_restart_keeps_budget_and_never_replays(logout_title_stall):
    r, restart, draw = logout_title_stall
    restart.side_effect = RuntimeError('启动失败')
    with pytest.raises(RuntimeError, match='启动失败'):
        r.recover_logout_title_stall(90)
    r.account = r.db.account(r.account['id'])
    assert r.ctx['game_restart_attempts'] == 1 and r.device is None
    with pytest.raises(PauseRun, match='一次后仍未恢复'):
        r.recover_logout_title_stall(190)
    assert restart.call_count == 1 and r.db.draws(r.account['id']) == [draw]
    assert r.run['completed'] == 0


@pytest.mark.parametrize('phase,requested', [('logout', False), ('switch_account', True),
                                            ('login', True), ('rewards', True), ('draws', True)])
def test_other_title_timeouts_do_not_restart(logout_title_stall, monkeypatch, phase, requested):
    r, restart, _ = logout_title_stall
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: 90)
    r.account['phase'] = phase
    r.ctx['logout_requested'] = requested
    with pytest.raises(PauseRun, match='等待90秒'):
        Runner.action(r, 'TAP TO START', args=(1188, 732))
    restart.assert_not_called()
    assert 'game_restart_attempts' not in r.ctx


def test_loading_restarts_only_game_once_and_preserves_identity_and_plan(app_restart):
    import copy
    r, restart = app_restart
    r.account['game_uid'] = '123456789'
    r.ctx['uid_verified'] = True
    identity = {k: copy.deepcopy(r.account[k]) for k in ('id', 'alias', 'alias_index', 'game_uid', 'plan')}
    d = r.draw_flow.current_draw()
    assert not r.recover_loading_stall(0)
    assert not r.recover_loading_stall(89.9)
    assert r.recover_loading_stall(90)
    restart.assert_called_once_with(2, r.stop)
    assert all(r.account[k] == v for k, v in identity.items())
    assert r.db.draws(r.account['id']) == [d]
    assert r.account['phase'] == 'rewards' and r.ctx['uid_verified'] is False
    assert r.ctx['gifts_done'] and r.ctx['game_restart_attempts'] == 1
    assert r.observation is None and r.last_action is None
    r.account = r.db.account(r.account['id'])
    r.observation = SimpleNamespace(scene='loading')
    assert not r.recover_loading_stall(100)
    with pytest.raises(PauseRun, match='一次后仍未恢复'):
        r.recover_loading_stall(190)
    assert restart.call_count == 1


@pytest.mark.parametrize('status', ['captured', 'needs_review'])
def test_game_restart_forbidden_with_unsettled_draw(app_restart, status):
    r, restart = app_restart
    d = r.draw_flow.current_draw(); d['status'] = status; r.db.save_draw(d)
    assert not r.recover_loading_stall(0)
    with pytest.raises(PauseRun, match='禁止重启游戏'):
        r.recover_loading_stall(90)
    restart.assert_not_called()
    r.device.close.assert_not_called()
    assert 'game_restart_attempts' not in r.ctx


@pytest.mark.parametrize('scene', ['unknown', 'downloading', 'network_error', 'home'])
def test_known_pages_and_unknown_dialogs_do_not_trigger_game_restart(app_restart, scene):
    r, restart = app_restart
    assert not r.recover_loading_stall(0)
    r.observation.scene = scene
    assert not r.recover_loading_stall(90)
    r.observation.scene = 'loading'
    assert not r.recover_loading_stall(91)
    restart.assert_not_called()


def test_stop_prevents_game_restart_and_failed_restart_keeps_budget(app_restart):
    r, restart = app_restart
    assert not r.recover_loading_stall(0)
    r.stop.set()
    assert not r.recover_loading_stall(90)
    restart.assert_not_called()
    r.stop.clear()
    restart.side_effect = RuntimeError('启动失败')
    assert not r.recover_loading_stall(100)
    with pytest.raises(RuntimeError, match='启动失败'):
        r.recover_loading_stall(190)
    assert r.db.account(r.account['id'])['context']['game_restart_attempts'] == 1
    assert r.device is None


def test_restart_before_login_submission_preserves_otp_but_rebuilds_form(app_restart):
    r, _ = app_restart
    r.account['phase'] = 'login'
    r.ctx.update(email_filled=True, code_filled=True, otp={'expires_at': 999}, otp_round=1)
    r.recover_loading_stall(0)
    assert r.recover_loading_stall(90)
    assert r.ctx['otp'] == {'expires_at': 999} and r.ctx['otp_round'] == 1
    assert 'email_filled' not in r.ctx and 'code_filled' not in r.ctx


def test_restarted_home_returns_to_pool_without_repeating_captured_draw(controller):
    r = controller
    d = r.draw_flow.current_draw(); d['status'] = 'complete'; r.db.save_draw(d)
    r.observation.scene = 'home'
    r.draw_flow.draws()
    r.click.assert_called_once_with('招募', exact=True)
    assert r.db.draws(r.account['id']) == [d]


def test_pressed_gift_close_releases_once_without_claiming_or_drawing(controller):
    from ournotes.paths import REFERENCE
    from ournotes.ui.observer import Observer

    r = controller
    r.ctx['uid_verified'] = True
    r.account['phase'] = 'gifts'
    image = Image.open(REFERENCE / 'runtime-regressions/pressed-navigation/gift-validation.png')
    r.observation = Observer(allow_fallback=False).read(image, phase='gifts')
    before = r.db.draws(r.account['id'])
    r.reward_flow.rewards()
    r.action.assert_called_once_with('release-gift-close-touch', kind='release_touch')
    r.click.assert_not_called()
    assert r.db.account(r.account['id'])['context']['gift_close_release_attempted']
    r.action.reset_mock()
    r.reward_flow.rewards()
    r.action.assert_not_called()
    assert r.db.draws(r.account['id']) == before
