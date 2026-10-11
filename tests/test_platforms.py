import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ournotes.platforms.system import current_platform


@pytest.mark.parametrize('system,machine,expected', [
    ('win32', 'AMD64', 'windows-x64'), ('win32', 'x86_64', 'windows-x64'),
    ('darwin', 'arm64', 'macos-arm64'), ('darwin', 'aarch64', 'macos-arm64'),
])
def test_supported_client_architectures(system, machine, expected):
    assert current_platform(system, machine) == expected


@pytest.mark.parametrize('system,machine', [('darwin', 'x86_64'), ('win32', 'ARM64'), ('linux', 'aarch64')])
def test_intel_mac_and_other_targets_are_not_shipped(system, machine):
    with pytest.raises(RuntimeError, match='Apple Silicon'):
        current_platform(system, machine)


def test_macos_credentials_go_to_keychain_without_disk_plaintext(tmp_path, monkeypatch):
    import ournotes.common.secrets as module
    saved = {}
    keychain = SimpleNamespace(load=lambda name: saved.get(name),
                               save=lambda name, raw: saved.__setitem__(name, raw),
                               delete=lambda name: saved.pop(name, None))
    store = module.SecretStore(tmp_path)
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(store, 'keychain', lambda: keychain)
    value = {'credential': 'synthetic-secret', 'service_url': 'https://example.com'}
    store.save('node', value)
    assert store.load('node') == value
    assert not list(tmp_path.iterdir())
    store.delete('node')
    assert store.load('node') is None


@pytest.mark.parametrize('name', ['../other', 'a/b', 'a\\b', 'bad:name'])
def test_secret_and_lock_names_cannot_escape_their_root(tmp_path, name):
    from ournotes.common.secrets import SecretStore
    store = SecretStore(tmp_path)
    with pytest.raises(ValueError):
        store.load(name)
    with pytest.raises(ValueError):
        store.save(name, {})
    with pytest.raises(ValueError):
        store.delete(name)
    with pytest.raises(ValueError), store.lock(name):
        pass
    assert not list(tmp_path.iterdir())


def test_macos_memory_is_pressure_based_and_does_not_publish_windows_commit(monkeypatch):
    import ournotes.platforms.memory as module
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=b'8589934592\n' if 'sysctl' in command[0]
                               else b'System-wide memory free percentage: 5%\n')
    monkeypatch.setattr(module.subprocess, 'run', run)
    memory = module.read_macos_memory()
    assert memory.low and memory.source == 'macos-memory'
    assert 'commit_limit_bytes' not in memory.report()
    assert calls == [['/usr/sbin/sysctl', '-n', 'hw.memsize'], ['/usr/bin/memory_pressure', '-Q']]


def test_macos_memory_unknown_output_fails_closed(monkeypatch):
    import ournotes.platforms.memory as module
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=b'unknown'))
    with pytest.raises(OSError, match='内存压力'):
        module.read_macos_memory()


def test_mac_mumu_requires_actual_adb_port_and_known_state(tmp_path, monkeypatch):
    from ournotes.platforms.mumu_macos import MacMuMu
    for name in ('mumutool', 'adb'):
        (tmp_path / name).write_bytes(b'fake')
    adapter = MacMuMu(tmp_path, config={})
    with pytest.raises(ValueError, match='不会按编号猜测'):
        adapter.serial(1)
    monkeypatch.setattr(adapter, 'command', lambda *args: b'{"0":{"state":"not verified"}}')
    info = adapter.info(0)
    assert info['is_process_started'] is None and info['is_android_started'] is None
    assert info['error_code'] == 1


def test_mac_mumu_uses_explicit_instance_port_and_verified_boolean_fields(tmp_path, monkeypatch):
    from ournotes.platforms.mumu_macos import MacMuMu
    for name in ('mumutool', 'adb'):
        (tmp_path / name).write_bytes(b'fake')
    adapter = MacMuMu(tmp_path, config={'instances': {'2': {'adb_port': 16660}},
                                     'info_fields': {'process_started': 'vm', 'android_started': 'boot'}})
    monkeypatch.setattr(adapter, 'command', lambda *args: b'{"2":{"vm":false,"boot":false}}')
    assert adapter.serial(2) == '127.0.0.1:16660'
    assert adapter.info(2)['is_process_started'] is False


def test_adb_input_error_is_not_replayed_and_remote_shell_data_is_restricted(monkeypatch):
    import threading

    from ournotes.platforms.mumu_macos import MacAdbDevice
    device = object.__new__(MacAdbDevice)
    device.lock, device.instance = threading.RLock(), 1
    monkeypatch.setattr(device, 'require_foreground', lambda: None)
    calls = []
    def fail(*args):
        calls.append(args)
        raise subprocess.CalledProcessError(1, ['adb'])
    device.adapter = SimpleNamespace(adb_command=fail)
    with pytest.raises(subprocess.CalledProcessError):
        device.input_text('person.alias@example.com')
    assert len(calls) == 1
    with pytest.raises(ValueError):
        device.input_text('x; print(secret)')
    assert len(calls) == 1


@pytest.mark.parametrize('failure', [subprocess.CalledProcessError, subprocess.TimeoutExpired])
def test_adb_transport_errors_cannot_log_input_text(tmp_path, monkeypatch, failure):
    from ournotes.platforms.mumu_macos import MacMuMu
    for name in ('mumutool', 'adb'):
        (tmp_path / name).write_bytes(b'fake')
    adapter = MacMuMu(tmp_path, config={'instances': {'0': {'adb_port': 16660}}})
    def fail(command, **kwargs):
        raise (failure(1, command) if failure is subprocess.CalledProcessError else failure(command, 20))
    monkeypatch.setattr('ournotes.platforms.mumu_macos.subprocess.run', fail)
    with pytest.raises(RuntimeError) as error:
        adapter.adb_command(0, 'shell', 'input', 'text', '123456')
    assert '123456' not in str(error.value)
    assert '未重发输入' in str(error.value)


def test_mac_notification_passes_text_as_arguments_not_script_source(monkeypatch):
    import ournotes.control.notifications as module
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    title = 'title "quoted"'
    module.MacNotifier('http://127.0.0.1:8766/').send(title, 'body')
    assert calls[0][-2:] == [title, 'body']
    assert title not in calls[0][2]


def test_controller_config_finds_mac_unix_socket(tmp_path, monkeypatch):
    import ournotes.platforms.proxy as module
    config = tmp_path / 'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/config.yaml'
    config.parent.mkdir(parents=True)
    config.write_text('external-controller-unix: /tmp/mihomo-test.sock\nsecret: synthetic-only\n')
    monkeypatch.delenv('OURNOTES_CLASH_CONFIG', raising=False)
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert module.controller_config()['external-controller-unix'] == '/tmp/mihomo-test.sock'


def test_controller_rejects_remote_transport_and_header_injection():
    from ournotes.platforms.proxy import request
    with pytest.raises(ValueError, match='本机'):
        request('/proxies', address='https://remote.example.com')
    with pytest.raises(ValueError, match='路径'):
        request('/proxies\r\nBad: yes', address='127.0.0.1:9090')
    with pytest.raises(ValueError, match='凭据'):
        request('/proxies', secret='bad\nsecret', address='127.0.0.1:9090')


def test_controller_cannot_redirect_a_local_request_or_credential():
    from urllib.request import Request

    from ournotes.platforms.proxy import NoRedirect
    request = Request('http://localhost:9090/proxies', headers={'Authorization': 'Bearer synthetic-only'})
    with pytest.raises(ValueError, match='不接受重定向'):
        NoRedirect().redirect_request(request, None, 302, 'redirect', {}, 'https://remote.example.com')
