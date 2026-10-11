from types import SimpleNamespace

import pytest

from ournotes.rebind.flow import Flow


def navigation(monkeypatch, scenes):
    clock = [0.0]
    monkeypatch.setattr('ournotes.rebind.flow.time.monotonic', lambda: clock[0])
    flow = Flow.__new__(Flow)
    flow.last_step = 'title → title_menu'
    flow.service = SimpleNamespace(wait=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    taps = []
    flow.tap = lambda *args: taps.append(args)
    flow.read = lambda: scenes(clock[0], len(taps))
    return flow, taps


def test_title_menu_recovers_ignored_first_touch(monkeypatch):
    flow, taps = navigation(monkeypatch, lambda now, count: 'title_menu' if count == 2 else 'title')
    assert flow.open_title_menu({'title_menu'}) == 'title_menu'
    assert len(taps) == 2


def test_title_retry_waits_through_loading_without_touching(monkeypatch):
    flow, taps = navigation(monkeypatch, lambda now, count: 'loading' if now < 8 else 'title_menu')
    assert flow.open_title_menu({'title_menu'}) == 'title_menu'
    assert not taps


def test_title_menu_waits_for_slow_sdk_loading_without_more_inputs(monkeypatch):
    flow, taps = navigation(monkeypatch, lambda now, count: 'loading' if now < 45 else 'title_menu')
    assert flow.open_title_menu({'title_menu'}) == 'title_menu'
    assert not taps


def test_sdk_loading_timeout_remains_bounded(monkeypatch):
    flow, taps = navigation(monkeypatch, lambda now, count: 'loading')
    with pytest.raises(ValueError, match='loading'):
        flow.open_title_menu({'title_menu'})
    assert not taps


def test_title_retry_is_bounded_and_does_not_guess_unknown_page(monkeypatch):
    flow, taps = navigation(monkeypatch, lambda now, count: 'title' if now < 10 else 'unknown')
    with pytest.raises(ValueError, match='unknown'):
        flow.open_title_menu({'title_menu'})
    assert len(taps) == 3


def test_submission_transition_is_not_retried(monkeypatch):
    flow, taps = navigation(monkeypatch, lambda now, count: 'email_form')
    flow.until = lambda predicate: (_ for _ in ()).throw(ValueError('timeout'))
    with pytest.raises(ValueError):
        flow.change('email_form', 800, 633, {'title'})
    assert len(taps) == 1


@pytest.mark.parametrize('initial', ['confirm_b', 'bind_form', 'verify_a'])
def test_start_after_stopped_rebind_leaves_old_subpages_without_sending(monkeypatch, initial):
    flow, _ = navigation(monkeypatch, lambda now, count: initial)
    flow.until = lambda predicate, timeout: initial
    calls = []
    destinations = {'confirm_b': 'bind_form', 'bind_form': 'verify_a', 'verify_a': 'bindings',
                    'bindings': 'user_center', 'user_center': 'login_history',
                    'login_history': 'login_options', 'login_options': 'email_form'}

    def change(page, x, y, wanted):
        calls.append((page, x, y))
        result = destinations[page]
        assert result in wanted
        return result

    flow.change = change
    flow.login_form()
    assert calls[-1] == ('login_options', 800, 546)
    assert not any(page in ('confirm_b', 'bind_form', 'verify_a') and y > 500 for page, x, y in calls)
