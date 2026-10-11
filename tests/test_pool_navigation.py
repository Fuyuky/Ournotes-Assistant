import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from PIL import Image

from ournotes.runtime.model import PauseRun
from ournotes.runtime.pool_navigation import PoolNavigator, same_view, viewport
from ournotes.runtime.pools import MYGO, STANDARD, PoolFinder, make_plan
from ournotes.runtime.runner import Runner
from ournotes.runtime.store import RuntimeStore

FIXTURES = Path(__file__).resolve().parents[1]/'assets/reference/runtime-regressions/pool-scan'


def positions(y=400):
    return {MYGO: {'center': (200, y)}, 'other': {'center': (150, y+170)}}


def stable(nav, ctx, t, items=None, current=MYGO, draw='a'):
    items = positions() if items is None else items
    nav.observe(ctx, draw, STANDARD, current, items, t)
    return nav.observe(ctx, draw, STANDARD, current, items, t+.31)


def test_delayed_swipe_feedback_is_not_an_edge_or_a_second_swipe():
    nav, ctx = PoolNavigator(), {}
    assert stable(nav, ctx, 0) == 'swipe'
    nav.sent(ctx, MYGO, positions(), .4)
    for t in (.5, .7, .9, 1.2, 1.5):
        assert nav.observe(ctx, 'a', STANDARD, MYGO, positions(), t) == 'wait'
    assert ctx['scan']['swipes'] == 1 and not ctx['scan']['boundaries']
    moved = positions(250)  # Same card names, genuinely different viewport.
    assert stable(nav, ctx, 1.6, moved) == 'swipe'
    assert ctx['scan']['boundary_hits'] == 0 and not ctx['scan']['boundaries']


def test_motion_geometry_cannot_be_replaced_by_just_visible_names():
    assert not same_view(viewport(MYGO, positions()), viewport(MYGO, positions(300)))
    assert same_view(viewport(MYGO, positions()), viewport(MYGO, positions(402)))


def test_missing_requires_two_confirmed_probes_at_each_boundary():
    nav, ctx = PoolNavigator(), {}
    assert stable(nav, ctx, 0) == 'swipe'
    for n in range(4):
        direction = ctx['scan']['direction']
        assert direction == ('down' if n < 2 else 'up')
        sent = 1 + n*3
        nav.sent(ctx, MYGO, positions(), sent)
        assert stable(nav, ctx, sent+.1) == 'wait'
        decision = nav.observe(ctx, 'a', STANDARD, MYGO, positions(), sent+1.6)
        assert decision == ('missing' if n == 3 else 'swipe')
    assert ctx['scan']['boundaries'] == ['down', 'up']


def test_target_appearing_during_delayed_feedback_is_found():
    nav, ctx = PoolNavigator(), {}
    stable(nav, ctx, 0)
    nav.sent(ctx, MYGO, positions(), .4)
    target = {STANDARD: {'center': (150, 714)}, MYGO: {'center': (207, 400)}}
    assert stable(nav, ctx, .5, target) == 'select'
    assert not ctx['scan']['boundaries']


def test_motion_resets_stability_and_empty_or_loading_frames_prove_nothing():
    nav, ctx = PoolNavigator(), {}
    for i in range(12):
        assert nav.observe(ctx, 'a', STANDARD, MYGO, positions(400-i*5), i*.1) == 'wait'
    assert ctx['scan']['swipes'] == 0
    assert nav.observe(ctx, 'a', STANDARD, None, {}, 100) == 'wait'
    assert nav.observe(ctx, 'a', STANDARD, MYGO, positions(), 101) == 'wait'
    nav.invalidate()  # Non-pool foreground interrupt between two pool frames.
    assert nav.observe(ctx, 'a', STANDARD, MYGO, positions(), 102) == 'wait'


def test_restart_never_counts_time_offline_as_completed_scroll_feedback():
    nav, ctx = PoolNavigator(), {}
    stable(nav, ctx, 0)
    nav.sent(ctx, MYGO, positions(), .4)
    ctx = json.loads(json.dumps(ctx))
    restored = PoolNavigator()
    assert stable(restored, ctx, 1000) == 'swipe'
    assert ctx['scan']['swipes'] == 1
    assert ctx['scan']['boundary_hits'] == 0 and not ctx['scan']['boundaries']


def test_each_draw_and_old_checkpoint_start_a_new_traversal():
    nav, ctx = PoolNavigator(), {'scan': {'direction': 'down', 'tries': 8, 'seen': ['old']}}
    stable(nav, ctx, 0)
    assert ctx['scan']['version'] == 2 and ctx['scan']['swipes'] == 0
    ctx['scan'].update(boundaries=['down', 'up'], boundary_hits=2, swipes=7)
    assert stable(nav, ctx, 2, draw='b') == 'swipe'
    assert ctx['scan']['boundaries'] == [] and ctx['scan']['swipes'] == 0


def test_attempt_limit_is_uncertain_not_a_missing_pool():
    nav, ctx = PoolNavigator(), {}
    stable(nav, ctx, 0)
    ctx['scan']['swipes'] = nav.max_swipes
    assert nav.observe(ctx, 'a', STANDARD, MYGO, positions(), 1) == 'uncertain'


@pytest.fixture
def runner(tmp_path):
    r = Runner()
    r.db = RuntimeStore(tmp_path)
    r.run = r.db.create_run('都抽', 'test', 1)
    r.account = r.db.reserve(r.run, make_plan('都抽'))
    r.account['phase'] = 'draws'
    r.account['post_gifts_balance'] = {'paid_stars': 0, 'free_stars': 3000}
    r.log, r.pools = MagicMock(), Mock()
    r.action, r.click = Mock(return_value=True), Mock(return_value=True)
    r.pool_selection_attempts = {}
    r.observation = SimpleNamespace(scene='pool', image=Image.new('RGB', (1600, 900)), texts=[],
                                    find=Mock(return_value=True))
    yield r
    r.db.close()


def test_rejected_input_does_not_advance_scan_or_spend_the_plan(runner, monkeypatch):
    now = [0.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.time', lambda: now[0])
    runner.pools.scan.return_value = (MYGO, positions(), [])
    runner.action.return_value = False
    runner.draw_flow.draws()
    now[0] = .4
    runner.draw_flow.draws()
    assert runner.ctx['scan']['swipes'] == 0 and 'pending' not in runner.ctx['scan']
    assert [(d['ordinal'], d['status']) for d in runner.db.draws(runner.account['id'])] == [(0, 'planned')]
    runner.click.assert_not_called()
    runner.ctx['scan']['swipes'] = runner.pool_navigation.max_swipes
    now[0] = .8
    with pytest.raises(PauseRun, match='禁止推断'):
        runner.draw_flow.draws()
    assert runner.account['skipped'] == []


def test_actual_account84_frames_never_skip_the_unsubmitted_ticket_draw(runner, monkeypatch):
    now = [0.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.time', lambda: now[0])
    runner.pools = PoolFinder()
    observed_actions = []
    for frame in sorted(FIXTURES.glob('*.png')):
        now[0] = float(frame.stem.split('-')[1])
        runner.observation.image = Image.open(frame).convert('RGB')
        before = runner.action.call_count
        runner.draw_flow.draws()
        if runner.action.call_count != before:
            observed_actions.append((frame.name, runner.action.call_args.args[0]))
    # The last frame is the first where the BDON logo and selected banner agree.
    # A second stable observation is required before opening the confirmation.
    now[0] += .31
    runner.draw_flow.draws()
    assert observed_actions, 'Recorded screenshot fixtures must be present'
    assert any(label == 'select-'+STANDARD for _, label in observed_actions)
    assert runner.account['skipped'] == []
    draws = runner.db.draws(runner.account['id'])
    assert len(draws) == 1 and draws[0]['ordinal'] == 0 and draws[0]['resource'] == 'ticket'
    assert draws[0]['status'] == 'planned'
    runner.click.assert_called_with('招募10次', exact=True, region=(1300, 650, 1595, 780),
                                    label='open-draw-confirmation')
