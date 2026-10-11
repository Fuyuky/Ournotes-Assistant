import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ournotes.integrations import mumu


@pytest.fixture
def cli(monkeypatch):
    calls = []
    replies = iter([{}, {'active': 'launcher'}, {}, {'active': 'com.bilibili.sirius.official'}])
    monkeypatch.setattr(mumu, 'instance_info', lambda *a: {'is_android_started': True})
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(next(replies)))
    monkeypatch.setattr(mumu.subprocess, 'run', run)
    return calls


def test_restart_only_closes_and_launches_the_game(cli):
    assert mumu.restart_game(2, threading.Event())['status'] == 'game_restarted'
    assert [c[4:] for c in cli] == [
        ['app', 'close', '-pkg', 'com.bilibili.sirius.official'],
        ['app', 'info', '--installed'],
        ['app', 'launch', '-pkg', 'com.bilibili.sirius.official'],
        ['app', 'info', '--installed'],
    ]
    assert all(c[1:4] == ['control', '-v', '2'] for c in cli)


def test_restart_never_boots_an_offline_instance(cli, monkeypatch):
    monkeypatch.setattr(mumu, 'instance_info', lambda *a: {'is_android_started': False})
    with pytest.raises(RuntimeError, match='不会启动或重启实例'):
        mumu.restart_game(2, threading.Event())
    assert not cli


def test_close_failure_does_not_launch_again(cli, monkeypatch):
    run = Mock(return_value=SimpleNamespace(stdout=json.dumps({'2': {'errcode': 1}})))
    monkeypatch.setattr(mumu.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='命令失败'):
        mumu.restart_game(2, threading.Event())
    assert run.call_count == 1


def test_stop_before_launch_is_respected(cli, monkeypatch):
    stop = threading.Event()
    def run(*a, **kw):
        stop.set()
        return SimpleNamespace(stdout='{}')
    call = Mock(side_effect=run)
    monkeypatch.setattr(mumu.subprocess, 'run', call)
    with pytest.raises(RuntimeError, match='暂停'):
        mumu.restart_game(2, stop)
    assert call.call_count == 1


def test_unclosed_game_times_out_without_repeating_close(cli, monkeypatch):
    clock = iter([0, 46])
    monkeypatch.setattr(mumu.time, 'monotonic', lambda: next(clock))
    replies = iter(['{}', json.dumps({'active': 'com.bilibili.sirius.official'})])
    call = Mock(side_effect=lambda *a, **kw: SimpleNamespace(stdout=next(replies)))
    monkeypatch.setattr(mumu.subprocess, 'run', call)
    with pytest.raises(RuntimeError, match='关闭超时'):
        mumu.restart_game(2, threading.Event())
    assert call.call_count == 2
