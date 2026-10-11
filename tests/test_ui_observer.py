"""Real screenshots exercise foreground evidence and payment field boundaries."""
import time

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from ournotes.paths import ASSETS, REFERENCE
from ournotes.runtime.pools import AVE, MEMBER, MYGO, SR, SSR, STANDARD, validate_confirmation
from ournotes.ui.fields import Fields
from ournotes.ui.frame import Frame
from ournotes.ui.observer import Observer


@pytest.fixture(scope='module')
def observer():
    return Observer(allow_fallback=False)


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
@pytest.mark.parametrize('folder,scene,boxes', [
    ('required-update', 'required_update', [(680, 125, 925, 175), (640, 355, 950, 401)]),
    ('server-maintenance', 'server_maintenance', [(740, 40, 862, 86), (890, 202, 1244, 243)]),
])
def test_service_interrupt_uses_independent_instance_and_requires_both_anchors(phase, folder, scene, boxes):
    image = Image.open(REFERENCE / 'runtime-regressions' / folder / 'instance-2.png').convert('RGB')
    vision = Observer(allow_fallback=False)
    result = vision.read(image, phase=phase)
    assert result.scene == scene and result.fast
    assert not result.texts  # Service interruptions do not offer game-flow clicks.
    for box in boxes:
        missing = image.copy()
        ImageDraw.Draw(missing).rectangle(box, fill='black')
        assert Observer(allow_fallback=False).read(missing, phase=phase).scene != scene


def test_login_feedback_detects_real_mail_limit_without_full_frame_ocr():
    from ournotes.runtime.model import MAIL_LIMIT_MESSAGE
    from ournotes.vision.ocr import Observation

    vision = Observer(allow_fallback=False)
    image = Image.new('RGB', (1600, 900), 'white')
    image.paste(Image.open(REFERENCE / 'runtime-regressions/mail-limit-toast.png'), (636, 434))
    observation = Observation(image, [], 'email_form', 'email_form')
    observation.frame = Frame(image, 1)
    vision.read_fields(observation, 'login_feedback')
    assert observation.has(MAIL_LIMIT_MESSAGE)
    assert vision.fallback_ocr is None
    assert [m['field'] for m in vision.fields.metrics] == ['login_feedback']


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
@pytest.mark.parametrize('file', ['promo-movie.png', 'promo-movie-replay.png'])
def test_opening_movie_uses_generic_foreground_controls(phase, file):
    vision = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'runtime-regressions/opening-updates'/file).convert('RGB')
    o = vision.read(image, phase=phase, last_action='TAP TO START')
    assert o.scene == 'opening_movie' and o.fast and vision.fallback_ocr is None
    target = o.find('跳过', exact=True)
    assert target and not o.find('SKIP')
    # Future event titles, characters, and video frames are not identity anchors.
    changed = Image.new('RGB', image.size, '#638bad')
    changed.paste(image.crop((1390, 20, 1590, 80)), (1390, 20))
    assert vision.read(changed, phase=phase).scene == 'opening_movie'
    assert vision.guard(o, changed, 'skip-opening-movie', target.center)
    for box in [(1424, 36, 1472, 64), (1494, 36, 1542, 64)]:
        missing = changed.copy()
        ImageDraw.Draw(missing).rectangle(box, fill='#638bad')
        assert not vision.guard(o, missing, 'skip-opening-movie', target.center)
        assert vision.read(missing, phase=phase).scene != 'opening_movie'
    dimmed = Image.fromarray((np.asarray(image) * .55).astype(np.uint8))
    assert not vision.guard(o, dimmed, 'skip-opening-movie', target.center)
    assert vision.read(dimmed, phase=phase).scene != 'opening_movie'


def test_opening_movie_does_not_replace_draw_animation():
    vision = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'gacha-workflow/screens/05-mygo-animation-ready.png')
    assert vision.read(image, phase='draws').scene == 'animation'


def test_updated_event_banner_keeps_the_existing_skip_route():
    vision = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'runtime-regressions/opening-updates/event-banner.png').convert('RGB')
    o = vision.read(image, phase='rewards')
    assert o.scene == 'animation' and o.find('SKIP', exact=True)
    assert vision.guard(o, image, 'skip-opening', o.find('SKIP').center)


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
def test_opening_confirmation_requires_its_question_and_guards_background(phase):
    vision = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'runtime-regressions/opening-updates/movie-skip-confirm.png').convert('RGB')
    movie = Image.open(REFERENCE/'runtime-regressions/opening-updates/promo-movie.png').convert('RGB')
    before = vision.read(movie, phase=phase)
    o = vision.read(image, phase=phase, last_action='skip-opening-movie')
    assert o.scene == 'opening_skip_confirm' and o.fast and vision.fallback_ocr is None
    target = o.find('OK', exact=True)
    assert target and not o.find('取消') and not o.find('跳过')
    assert vision.guard(o, image, 'confirm-opening-skip', target.center)
    assert not vision.guard(before, image, 'skip-opening-movie', before.find('跳过').center)
    assert not vision.guard(o, movie, 'confirm-opening-skip', target.center)
    for box in [(762, 135, 839, 169), (672, 387, 928, 421), (945, 696, 1007, 737)]:
        changed = image.copy()
        ImageDraw.Draw(changed).rectangle(box, fill='#262640')
        assert vision.read(changed, phase=phase).scene != 'opening_skip_confirm'
        assert not vision.guard(o, changed, 'confirm-opening-skip', target.center)


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
@pytest.mark.parametrize(('file', 'scene', 'text', 'label', 'anchors'), [
    ('sample2-story.png', 'story', '剧情菜单', 'open-story-menu',
     [(1170, 8, 1350, 68), (1476, 101, 1527, 154)]),
    ('sample2-menu.png', 'story_menu', 'SKIP', 'skip-story',
     [(1475, 101, 1528, 154), (1471, 188, 1532, 237), (1471, 259, 1532, 311)]),
    ('sample2-confirm.png', 'story_skip_confirm', '跳过', 'confirm-story-skip',
     [(720, 376, 880, 408), (914, 647, 974, 678)]),
    ('stable2-confirm.png', 'story_skip_confirm', '跳过', 'confirm-story-skip',
     [(720, 376, 880, 408), (917, 652, 977, 683)]),
])
def test_story_skip_route_requires_foreground_evidence(phase, file, scene, text, label, anchors):
    # These are SDK samples from instance 2, not the instance 1 template sources.
    image = Image.open(REFERENCE/'runtime-regressions/birthday-story'/file).convert('RGB')
    vision = Observer(allow_fallback=False)
    observation = vision.read(image, phase=phase)
    target = observation.find(text, exact=True)
    assert observation.scene == scene and observation.fast and target
    assert vision.fallback_ocr is None
    assert vision.guard(observation, image, label, target.center)
    for box in anchors:
        missing = image.copy()
        ImageDraw.Draw(missing).rectangle(box, fill='#262640')
        assert vision.read(missing, phase=phase).scene != scene
        assert not vision.guard(observation, missing, label, target.center)
    dimmed = Image.fromarray((np.asarray(image) * .55).astype(np.uint8))
    assert vision.read(dimmed, phase=phase).scene != scene
    assert not vision.guard(observation, dimmed, label, target.center)
    if scene != 'story_skip_confirm':
        changed = image.copy()
        ImageDraw.Draw(changed).rectangle((0, 81, 1400, 819), fill='#638bad')
        assert vision.read(changed, phase=phase).scene == scene


def test_story_menu_and_skip_confirmation_block_previous_controls():
    vision = Observer(allow_fallback=False)
    folder = REFERENCE/'runtime-regressions/birthday-story'
    story = Image.open(folder/'sample2-story.png').convert('RGB')
    menu = Image.open(folder/'sample2-menu.png').convert('RGB')
    confirm = Image.open(folder/'sample2-confirm.png').convert('RGB')
    closed = vision.read(story, phase='rewards')
    assert not vision.guard(closed, menu, 'open-story-menu', closed.find('剧情菜单').center)
    opened = vision.read(menu, phase='rewards')
    assert not vision.guard(opened, confirm, 'skip-story', opened.find('SKIP').center)
    question = vision.read(confirm, phase='rewards')
    assert not vision.guard(question, story, 'confirm-story-skip', question.find('跳过').center)


def test_single_recruit_button_local_ocr_when_template_control_is_missing(observer):
    image = Image.open(REFERENCE/'gacha-workflow/screens/26-member-main.png').convert('RGB')
    o = observer.read(image, phase='draws', account_id='local-button')
    assert o.scene == 'pool'
    o.texts = []  # Reproduce a missing control match on a correctly identified pool.
    observer.read_fields(o, 'draw_button_1', 'member-ticket')
    target = o.find('招募1次', exact=True, region=(1030, 650, 1295, 780))
    assert target is not None
    assert 1030 <= target.center[0] <= 1295 and 650 <= target.center[1] <= 738
    assert o.find('招募10次', exact=True) is None


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
def test_download_confirmation_is_foreground_for_every_phase(phase):
    observer = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'runtime-regressions/update-download-confirm.png').convert('RGB')
    # Download size and free storage are dynamic, never scene anchors.
    ImageDraw.Draw(image).rectangle((880, 345, 1040, 440), fill='#313960')
    o = observer.read(image, phase=phase, last_action='TAP TO START')
    assert o.scene == 'download_confirm' and o.fast and observer.fallback_ocr is None
    target = o.find('OK', exact=True)
    assert target and 825 <= target.center[0] <= 1125
    assert not o.find('取消', exact=True) and not o.find('TAP TO START')
    assert observer.guard(o, image, 'confirm-download', target.center)
    changed = image.copy()
    ImageDraw.Draw(changed).rectangle((825, 787, 1125, 852), fill='black')
    assert not observer.guard(o, changed, 'confirm-download', target.center)


def test_downloading_fast_detection_and_local_percentage():
    observer = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'game-onboarding/02-download-confirmed.png').convert('RGB')
    o = observer.read(image, phase='rewards', last_action='confirm-download')
    assert o.scene == 'downloading' and o.fast and observer.fallback_ocr is None
    observer.read_fields(o, 'download_progress')
    assert o.has('1.2%') and not o.find('OK')


@pytest.mark.parametrize(('name', 'key', 'pulls', 'resource'), [
    ('03-mygo-confirm.png', MYGO, 10, 'stars'), ('14-ave-confirm.png', AVE, 10, 'stars'),
    ('21-ssr-confirm.png', SSR, 1, 'ticket'), ('27-member-confirm.png', MEMBER, 1, 'ticket'),
    ('33-sr-confirm.png', SR, 1, 'ticket'), ('38-standard-1-confirm.png', STANDARD, 10, 'ticket'),
])
def test_confirmation_fields(observer, name, key, pulls, resource):
    im = Image.open(REFERENCE/'gacha-workflow/screens'/name).convert('RGB')
    o = observer.read(im, phase='draws', account_id='one')
    assert o.scene == 'draw_confirm' and o.fast
    observer.read_fields(o, 'confirmation', name)
    validate_confirmation(o, {'pool_key': key, 'pulls': pulls, 'resource': resource})
    with pytest.raises(ValueError):
        validate_confirmation(o, {'pool_key': key, 'pulls': 99, 'resource': resource})


@pytest.mark.parametrize(('file', 'scene'), [
    ('history-initial.png', 'login_history'), ('history-expanded.png', 'login_history_expanded'),
    ('history-delete.png', 'login_history_confirm'),
])
def test_history_foreground(observer, file, scene):
    o = observer.read(Image.open(ASSETS/'login-history'/file).convert('RGB'), phase='switch_account')
    assert o.scene == scene and o.fast


@pytest.mark.parametrize(('file', 'scene'), [
    ('20-home-after-gifts.png', 'home'), ('17-claim-all-result.png', 'gift_confirm'),
    ('18-claim-all-confirmed.png', 'gift_received'), ('19-inbox-empty.png', 'gift_box'),
    ('14-retry-announcement.png', 'notice'), ('03-login-reward-center.png', 'startup_reward'),
])
def test_known_pages_without_ocr(observer, file, scene):
    o = observer.read(Image.open(REFERENCE/'game-onboarding'/file).convert('RGB'))
    assert o.scene == scene and o.fast
    assert observer.fallback_ocr is None


def test_announcement_is_a_fast_draw_phase_candidate():
    observer = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'game-onboarding/14-retry-announcement.png').convert('RGB')
    observation = observer.read(image, phase='draws')
    assert observation.scene == 'notice' and observation.find('关闭', exact=True)
    assert observation.fast and observer.fallback_ocr is None


@pytest.mark.parametrize('phase', ['switch_account', 'login', 'rewards', 'gifts', 'draws', 'logout'])
def test_enable_prompt_is_foreground_and_only_exposes_close(phase):
    observer = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'runtime-regressions/enable-prompt-redacted.png').convert('RGB')
    # The masked private body is never an anchor; arbitrary body text is allowed.
    ImageDraw.Draw(image).rectangle((570, 390, 1040, 545), fill='white')
    o = observer.read(image, phase=phase, last_action='title-menu')
    assert o.scene == 'enable_prompt' and o.fast
    assert o.find('关闭额外弹窗', exact=True).center == (983, 261)
    assert not o.find('去开启', exact=True)
    assert observer.fallback_ocr is None
    changed = image.copy()
    ImageDraw.Draw(changed).rectangle((967, 245, 998, 277), fill='white')
    assert observer.guard(o, changed, '关闭额外弹窗', (983, 261)) is False
    assert observer.read(changed, phase=phase).scene != 'enable_prompt'


@pytest.mark.parametrize('phase', ['switch_account', 'rewards', 'logout'])
def test_disabled_title_menu_is_explicit_not_enabled_user_center(phase):
    observer = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'runtime-regressions/title-menu-unavailable.png').convert('RGB')
    o = observer.read(image, phase=phase, last_action='title-menu')
    assert o.scene == 'title_menu_unavailable' and o.fast
    assert o.find('关闭', exact=True) and not o.find('用户中心', exact=True)
    assert observer.fallback_ocr is None
    assert observer.guard(o, image, 'close-unavailable-menu', (800, 715))


def test_enabled_menu_and_control_guard_reject_disabled_transition():
    observer = Observer(allow_fallback=False)
    enabled = Image.open(REFERENCE/'runtime-regressions/title-menu-enabled.png').convert('RGB')
    disabled = Image.open(REFERENCE/'runtime-regressions/title-menu-unavailable.png').convert('RGB')
    o = observer.read(enabled, phase='logout', last_action='title-menu')
    assert o.scene == 'title_menu' and o.fast
    target = o.find('用户中心', exact=True)
    assert target
    assert observer.guard(o, enabled, '用户中心', target.center)
    assert observer.guard(o, disabled, '用户中心', target.center) is False


@pytest.mark.parametrize('phase', ['switch_account', 'rewards', 'logout'])
@pytest.mark.parametrize('file', ['rank-up.png', 'rank-up-reward.png'])
def test_rank_rewards_during_menu_recovery_are_fast_candidates(phase, file):
    observer = Observer(allow_fallback=False)
    image = Image.open(REFERENCE/'runtime-regressions'/file).convert('RGB')
    o = observer.read(image, phase=phase)
    assert o.scene == 'startup_reward' and o.fast
    assert o.find('OK', exact=True)
    assert observer.fallback_ocr is None


def test_guard_accepts_animated_background_and_rejects_overlay(observer):
    image = Image.open(REFERENCE/'pool-survey/screens/01-mygo-main.png').convert('RGB')
    o = observer.read(image, phase='draws')
    changed = image.copy()
    ImageDraw.Draw(changed).rectangle((400, 100, 1400, 600), fill='white')
    assert observer.guard(o, changed, 'pool-menu', (1541, 51)) is True
    dimmed = Image.fromarray((np.asarray(image)*.55).astype(np.uint8))
    assert observer.guard(o, dimmed, 'pool-menu', (1541, 51)) is False
    assert observer.read(dimmed, phase='draws').scene == 'unknown'


def test_field_cache_is_local_and_scoped(observer):
    image = Image.open(REFERENCE/'gacha-workflow/screens/03-mygo-confirm.png').convert('RGB')
    o = observer.read(image, phase='draws', account_id='cache-one')
    observer.read_fields(o, 'confirmation', 'transaction-one')
    observer.fields.metrics.clear()
    observer.read_fields(o, 'confirmation', 'transaction-one')
    assert all(m['cached'] for m in observer.fields.metrics)
    observer.fields.metrics.clear()
    observer.read_fields(o, 'confirmation', 'transaction-two')
    assert all(not m['cached'] for m in observer.fields.metrics)
    observer.read(image, account_id='cache-two')
    assert not observer.fields.cache
    assert not Observer().fields.cache


def test_number_field_trims_all_digits_including_nonzero_prefix():
    fields = Fields()
    image = Image.new('RGB', (1600, 900), (62, 66, 101))
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 26)
    ImageDraw.Draw(image).text((482, 447), '1,000', fill='white', font=font)
    values = fields.read(Frame(image, 1), 'paid', [(470, 440, 590, 485)], (1,), digits=True, scale=3)
    assert len(values) == 1 and int(values[0].text.replace(',', '')) == 1000


def test_unknown_ocr_has_delay_interval_and_budget(monkeypatch):
    class Legacy:
        def read(self, image):
            from ournotes.vision.ocr import Observation
            return Observation(image, [], 'unknown', '')
    monkeypatch.setattr('ournotes.ui.observer.Vision', Legacy)
    now = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    observer = Observer()
    image = Image.new('RGB', (1600, 900), '#777777')
    observer.read(image, phase='draws')
    now[0] += 2
    observer.read(image, phase='draws')
    assert observer.fallbacks == 0
    for _ in range(20):
        now[0] += 1
        observer.read(image, phase='draws')
    assert observer.fallbacks == 2
    for _ in range(30):
        now[0] += 1
        observer.read(image, phase='draws')
    assert observer.fallbacks == 3


def test_cold_start_blank_does_not_consume_recovery_budget(observer):
    for color in ['black', 'white']:
        assert observer.read(Image.new('RGB', (1600, 900), color)).scene == 'loading'
    assert observer.fallback_ocr is None


def test_new_song_popup_precedes_background_home(observer):
    image = Image.open(REFERENCE/'runtime-regressions/new-song.png').convert('RGB')
    o = observer.read(image, phase='gifts')
    assert o.scene == 'startup_reward' and o.fast


def test_title_loading_blocks_background_start_button(observer):
    image = Image.open(REFERENCE/'runtime-regressions/title-loading.png').convert('RGB')
    assert observer.read(image, phase='rewards').scene == 'loading'


def test_title_loading_glow_and_short_template_gaps_block_input(monkeypatch):
    observer = Observer(allow_fallback=False)
    now = [100.0]
    monkeypatch.setattr('ournotes.ui.observer.time.monotonic', lambda: now[0])
    busy = Image.open(REFERENCE/'runtime-regressions/title-loading-glow.png').convert('RGB')
    clear = busy.copy()
    ImageDraw.Draw(clear).rectangle((1190, 770, 1599, 899), fill='#445577')
    title = observer.read(clear, phase='rewards', account_id='one')
    assert title.scene == 'title'
    assert not observer.guard(title, busy, 'TAP TO START', (1188, 732))
    loading = observer.read(busy, phase='rewards', account_id='one')
    assert loading.scene == 'loading' and not loading.find('TAP TO START')
    now[0] += 1.5
    assert observer.read(clear, phase='rewards', account_id='one').scene == 'loading'
    assert not observer.guard(title, clear, 'TAP TO START', (1188, 732))
    # Real interruptions retain priority, rather than being hidden by the hold.
    error = Image.open(REFERENCE/'runtime-regressions/network-return-title.png').convert('RGB')
    assert observer.read(error, phase='rewards', account_id='one').scene == 'network_error'
    now[0] += .6
    ready = observer.read(clear, phase='rewards', account_id='one')
    assert ready.scene == 'title' and observer.guard(ready, clear, 'TAP TO START', (1188, 732))
    assert observer.fallback_ocr is None


def test_loading_hold_does_not_carry_into_another_account(monkeypatch):
    observer = Observer(allow_fallback=False)
    monkeypatch.setattr('ournotes.ui.observer.time.monotonic', lambda: 100.0)
    busy = Image.open(REFERENCE/'runtime-regressions/title-loading-glow.png').convert('RGB')
    clear = busy.copy()
    ImageDraw.Draw(clear).rectangle((1190, 770, 1599, 899), fill='#445577')
    assert observer.read(busy, phase='rewards', account_id='one').scene == 'loading'
    assert observer.read(clear, phase='rewards', account_id='two').scene == 'title'


@pytest.mark.parametrize(('file', 'phase', 'scene'), [
    ('comic-loading.png', 'draws', 'loading'), ('network-return-title.png', 'rewards', 'network_error'),
])
def test_wait_and_error_screens_need_no_full_ocr(observer, file, phase, scene):
    o = observer.read(Image.open(REFERENCE/'runtime-regressions'/file).convert('RGB'), phase=phase)
    assert o.scene == scene and o.fast
    assert observer.fallback_ocr is None


@pytest.mark.parametrize('name,scene,label', [
    ('gift', 'gift_box', '关闭（按下）'),
    ('result', 'draw_result', '招募TOP'),
])
def test_pressed_navigation_independent_capture_and_foreground(name, scene, label):
    image = Image.open(REFERENCE / 'runtime-regressions/pressed-navigation' / f'{name}-validation.png').convert('RGB')
    vision = Observer(allow_fallback=False)
    observed = vision.read(image, phase='gifts' if name == 'gift' else 'draws')
    assert observed.scene == scene and observed.fast
    target = observed.find(label, exact=True)
    assert target
    assert vision.guard(observed, image, label, target.center)
    if name == 'result':
        assert observed.result_layout['top_state'] == 'pressed'
    dimmed = Image.fromarray((np.asarray(image) * .55).astype(np.uint8))
    assert not vision.guard(observed, dimmed, label, target.center)
    missing = image.copy()
    ImageDraw.Draw(missing).rectangle(target.box, fill='black')
    assert not vision.guard(observed, missing, label, target.center)


def test_global_fallback_includes_result_layout_outside_draw_phase(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('ournotes.ui.observer.time.monotonic', lambda: clock[0])
    image = Image.open(REFERENCE / 'runtime-regressions/pressed-navigation/result-validation.png')
    vision = Observer(allow_fallback=False)
    assert vision.read(image, phase='gifts').scene == 'unknown'
    clock[0] += 1
    result = vision.read(image, phase='gifts')
    assert result.scene == 'draw_result' and result.fast
    assert result.result_layout['top_state'] == 'pressed'


def test_ocr_result_keeps_recovery_mode_and_foreground_guard(monkeypatch):
    from unittest.mock import Mock

    from ournotes.vision.ocr import Observation, TextBox

    clock = [100.0]
    monkeypatch.setattr('ournotes.ui.observer.time.monotonic', lambda: clock[0])
    image = Image.new('RGB', (1600, 900), '#667788')
    vision = Observer()
    vision.fallback_ocr = Mock()
    vision.fallback_ocr.read.return_value = Observation(
        image, [TextBox('招募TOP', 1, (1280, 793, 1566, 861))], 'draw_result', 'ocr')
    assert vision.read(image, phase='draws').scene == 'unknown'
    clock[0] += 4
    result = vision.read(image, phase='draws')
    assert result.scene == 'draw_result'
    assert result.mode == 'recovery_ocr' and not result.fast
    assert not vision.guard(result, image, '招募TOP', result.find('招募TOP').center)
