import json
from types import SimpleNamespace

import pytest

from deploy.client.configure_client import configure


class Config:
    value = None

    @classmethod
    def load(cls, root):
        return cls.value

    @classmethod
    def save(cls, root, service_url, credential):
        cls.value = SimpleNamespace(service_url=service_url, credential=credential)


def seed(tmp_path):
    path = tmp_path / 'client-connection.json'
    path.write_text(json.dumps({'service_url': 'https://example.com', 'credential': 'synthetic-node-token'}))
    return path


def test_first_boot_imports_and_removes_seed(tmp_path):
    Config.value = None
    path = seed(tmp_path)
    assert configure(tmp_path, tmp_path / 'data', Config) == 'imported'
    assert Config.value.credential == 'synthetic-node-token'
    assert not path.exists()
    assert configure(tmp_path, tmp_path / 'data', Config) == 'no_seed'


def test_existing_node_is_not_replaced(tmp_path):
    Config.value = SimpleNamespace(credential='existing-node')
    path = seed(tmp_path)
    assert configure(tmp_path, tmp_path / 'data', Config) == 'already_configured'
    assert Config.value.credential == 'existing-node'
    assert not path.exists()


def test_save_failure_keeps_seed_for_retry(tmp_path):
    class Broken(Config):
        value = None

        @classmethod
        def save(cls, *args):
            raise OSError('synthetic write failure')

    path = seed(tmp_path)
    with pytest.raises(OSError):
        configure(tmp_path, tmp_path / 'data', Broken)
    assert path.exists()


def test_bootstrap_waits_for_readiness_and_reports_exit():
    from unittest.mock import Mock

    from deploy.client.boot_client import wait_ready
    probe = Mock(side_effect=[False, True])
    process = SimpleNamespace(poll=lambda: None)
    wait_ready(process, 'http://127.0.0.1:8766', probe=probe, sleep=lambda _: None)
    assert probe.call_count == 2
    with pytest.raises(RuntimeError, match='提前退出'):
        wait_ready(SimpleNamespace(poll=lambda: 1), 'unused', probe=lambda _: False)


def test_bootstrap_timeout_does_not_kill_background():
    from deploy.client.boot_client import wait_ready
    ticks = iter([0, 0, 61])
    with pytest.raises(RuntimeError, match='60 秒'):
        wait_ready(SimpleNamespace(poll=lambda: None), 'unused', probe=lambda _: False,
                   clock=lambda: next(ticks), sleep=lambda _: None)


def test_bootstrap_spawns_bundled_python_with_isolated_data(tmp_path, monkeypatch):
    from deploy.client import boot_client
    root = tmp_path / '中文 portable'
    app = root / 'versions/0.1.0/app'
    (app / 'scripts').mkdir(parents=True)
    (app / 'scripts/webui.py').write_text('# synthetic', 'utf-8')
    (root / 'current.json').write_text('{"version":"0.1.0"}', 'utf-8')
    calls = []
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(boot_client, 'ready', lambda _: False)
    monkeypatch.setattr(boot_client.subprocess, 'Popen',
                        lambda args, **kwargs: calls.append((args, kwargs)) or process)
    monkeypatch.setattr(boot_client, 'wait_ready', lambda p, url: calls.append(('ready', p)))
    monkeypatch.setattr(boot_client.webbrowser, 'open', lambda url: calls.append(('browser', url)) or True)
    boot_client.boot(root)
    command, options = calls[0]
    assert command[0] == boot_client.sys.executable
    assert command[-1] == '--no-browser'
    assert options['env']['OURNOTES_DATA_ROOT'] == str(root / 'user-data')
    assert options['cwd'] == app
    assert calls[1] == ('ready', process)
    assert calls[2][0] == 'browser'
