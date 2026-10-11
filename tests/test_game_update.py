import json
import struct
import zipfile

import pytest

from ournotes.control import game_update as g


def apk(path, package=g.PACKAGE, code=56, version='1.0.3'):
    strings = ['manifest', 'package', 'versionCode', 'versionName', package, version,
               'http://schemas.android.com/apk/res/android']
    raw, offsets = b'', []
    for s in strings:
        b = s.encode()
        offsets.append(len(raw))
        raw += bytes([len(s), len(b)]) + b + b'\0'
    pool = struct.pack('<HHI5I', 1, 28, 28 + 4 * len(strings) + len(raw), len(strings), 0,
                       0x100, 28 + 4 * len(strings), 0) + struct.pack('<7I', *offsets) + raw
    attr = lambda ns, key, dtype, value: struct.pack('<IIIHBBI', ns, key, 0xffffffff, 8, 0, dtype, value)
    attrs = attr(0xffffffff, 1, 3, 4) + attr(6, 2, 16, code) + attr(6, 3, 3, 5)
    tag = struct.pack('<HHIII', 0x102, 16, 36 + len(attrs), 1, 0xffffffff)
    tag += struct.pack('<II6H', 0xffffffff, 0, 20, 20, 3, 0, 0, 0) + attrs
    data = struct.pack('<HHI', 3, 8, 8 + len(pool) + len(tag)) + pool + tag
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('AndroidManifest.xml', data)


@pytest.mark.parametrize('url', ['http://pkg.biligame.com/games/a.apk',
    'https://pkg.biligame.com.evil.test/games/a.apk', 'https://evil.test/a.apk',
    'https://u@pkg.biligame.com/games/a.apk', 'https://pkg.biligame.com/games/../a.apk',
    'https://pkg.biligame.com/games/a.apk?url=http://127.0.0.1'])
def test_reject_non_official_url(url):
    with pytest.raises(ValueError):
        g.official_url(url)


def test_manifest_identity_and_wrong_channel(tmp_path):
    path = tmp_path / 'a.apk'
    apk(path)
    assert g.apk_identity(path) == {'package': g.PACKAGE, 'version': '1.0.3', 'version_code': 56}
    apk(path, package='com.bilibili.sirius')
    with pytest.raises(ValueError, match='官方渠道'):
        g.apk_identity(path)


def test_install_preserves_running_instances_and_skips_downgrade(monkeypatch, tmp_path):
    calls, closed, versions = [], [], {1: 50, 2: 60}
    monkeypatch.setattr(g, 'instance_info', lambda **kw: {'is_process_started': kw['instance'] == 1,
                                                        'is_android_started': True})
    monkeypatch.setattr(g, 'read_commit_memory', lambda: None)
    monkeypatch.setattr(g, 'shutdown_instances', lambda ids: closed.extend(ids))
    monkeypatch.setattr(g, 'app_info', lambda instance, package: {'version': '1.0.3', 'version_code': str(versions[instance])})
    monkeypatch.setattr(g, 'launch_instance', lambda instance: calls.append(('launch', instance)))
    monkeypatch.setattr(g, 'close_app', lambda instance, package: calls.append(('close', instance)))
    def install_apk(instance, path):
        calls.append(('install', instance))
        versions[instance] = 56
    monkeypatch.setattr(g, 'install_apk', install_apk)
    result = g.install([1, 2], tmp_path / 'a.apk', {'version_code': 56}, lambda **kw: None)
    assert [r['status'] for r in result] == ['updated', 'skipped']
    assert closed == [2]
    assert sum('install' in c for c in calls) == 1
    assert ('launch', 1) not in calls and ('launch', 2) in calls


def test_unknown_install_outcome_keeps_instance_open(monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(g, 'instance_info', lambda **kw: {'is_process_started': False, 'is_android_started': True})
    monkeypatch.setattr(g, 'read_commit_memory', lambda: None)
    monkeypatch.setattr(g, 'shutdown_instances', lambda ids: closed.extend(ids))
    monkeypatch.setattr(g, 'app_info', lambda *args: {'version_code': '50'})
    monkeypatch.setattr(g, 'launch_instance', lambda *args: None)
    monkeypatch.setattr(g, 'close_app', lambda *args: None)
    def fail(*args):
        raise TimeoutError('install outcome unknown')
    monkeypatch.setattr(g, 'install_apk', fail)
    with pytest.raises(TimeoutError):
        g.install([2, 3], tmp_path / 'a.apk', {'version_code': 56}, lambda **kw: None)
    assert closed == []


def test_manual_only_failure_cleanup_and_duplicate_request(tmp_path, monkeypatch):
    calls = []
    def fetch(url, path, progress, stop):
        calls.append(url)
        path.write_bytes(b'incomplete')
        raise ValueError('download failed')
    monkeypatch.setattr(g, 'download', fetch)
    service = g.GameUpdate(tmp_path)
    assert not calls and not service.thread
    monkeypatch.setattr(g, 'discover_official_apk', lambda: {'url': 'https://pkg.biligame.com/games/a.apk', 'version': '1.0.3'})
    service.start([1], 'a' * 32)
    service.thread.join(5)
    assert service.status()['status'] == 'failed'
    assert not (tmp_path / 'state/game-update-cache/download.apk').exists()
    service.start([1], 'a' * 32)
    assert len(calls) == 1
    assert not (tmp_path / 'state/runtime.sqlite3').exists()


def test_interrupted_update_never_restarts(tmp_path, monkeypatch):
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state/game-update.json').write_text(json.dumps({'status': 'running'}))
    service = g.GameUpdate(tmp_path)
    assert service.status()['status'] == 'interrupted' and service.thread is None


def test_pending_transaction_blocks_before_download(tmp_path, monkeypatch):
    import sqlite3
    (tmp_path / 'state').mkdir()
    with sqlite3.connect(tmp_path / 'state/runtime.sqlite3') as db:
        db.executescript('CREATE TABLE runs(id,data);CREATE TABLE accounts(id,run_id);'
                         'CREATE TABLE draws(account_id,status);')
        db.execute('INSERT INTO runs VALUES (?,?)', ('r', json.dumps({'instance': 1})))
        db.execute("INSERT INTO accounts VALUES ('a','r')")
        db.execute("INSERT INTO draws VALUES ('a','submitted')")
    service = g.GameUpdate(tmp_path)
    with pytest.raises(ValueError, match='未决'):
        service.start([1], 'b' * 32)
    assert service.thread is None

@pytest.mark.parametrize('url,version,valid', [
    ('https://pkg.biligame.com/games/current.apk', '1.0.3', True),
    ('https://evil.test/current.apk', '1.0.3', False),
    ('', '1.0.3', False),
    ('https://pkg.biligame.com/games/current.apk', '', False),
])
def test_official_discovery(monkeypatch, url, version, valid):
    from contextlib import nullcontext
    from types import SimpleNamespace
    calls = []
    def open_request(request, timeout):
        calls.append(request)
        return nullcontext(SimpleNamespace(headers={
            'x-client-download-url': url, 'x-client-recommended-version': version}))
    monkeypatch.setattr(g.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=open_request))
    if valid:
        assert g.discover_official_apk()['url'] == url
    else:
        with pytest.raises(ValueError):
            g.discover_official_apk()
    assert calls[0].full_url == g.VERSION_ENDPOINT
    assert calls[0].data == b'\0\0\0\0\0'
    assert not any('auth' in key.lower() for key in calls[0].headers)


def test_discovery_failure_never_downloads_or_installs(tmp_path, monkeypatch):
    def fail():
        raise ValueError('官方查询失败')
    monkeypatch.setattr(g, 'discover_official_apk', fail)
    monkeypatch.setattr(g, 'download', lambda *args: pytest.fail('must not download'))
    service = g.GameUpdate(tmp_path)
    assert service.status()['status'] == 'idle'
    service.start([1], 'c' * 32)
    service.thread.join(5)
    assert service.status()['status'] == 'failed'
    assert '官方查询失败' in service.status()['message']

def test_discovered_version_must_match_apk(tmp_path, monkeypatch):
    monkeypatch.setattr(g, 'discover_official_apk', lambda: {
        'url': 'https://pkg.biligame.com/games/current.apk', 'version': '1.0.4'})
    def fetch(url, path, *args):
        apk(path, version='1.0.3')
        return 'fakehash'
    monkeypatch.setattr(g, 'download', fetch)
    monkeypatch.setattr(g, 'install', lambda *args: pytest.fail('must not install stale APK'))
    service = g.GameUpdate(tmp_path)
    service.start([1], 'd' * 32)
    service.thread.join(5)
    assert service.status()['status'] == 'failed'
    assert '版本不一致' in service.status()['message']
    assert not (tmp_path / 'state/game-update-cache/download.apk').exists()


@pytest.mark.parametrize('install_fails', [False, True])
def test_busy_apk_cleanup_does_not_replace_verified_install_result(tmp_path, monkeypatch, install_fails):
    from pathlib import Path

    monkeypatch.setattr(g, 'discover_official_apk', lambda: {
        'url': 'https://pkg.biligame.com/games/current.apk', 'version': '1.0.3'})
    def fetch(url, path, *args):
        apk(path, version='1.0.3')
        return 'fakehash'
    monkeypatch.setattr(g, 'download', fetch)
    def install(*args):
        if install_fails:
            raise ValueError('installation failure')
        return [{'instance': 0, 'status': 'updated', 'version': '1.0.3'}]
    monkeypatch.setattr(g, 'install', install)
    original = Path.unlink
    def busy(path, *args, **kwargs):
        if path.name == 'download.apk':
            raise PermissionError('installer still holds the file')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', busy)
    service = g.GameUpdate(tmp_path)
    service.start([0], 'e' * 32)
    service.thread.join(5)
    value = service.status()
    assert value['cache_cleanup_pending']
    assert value['status'] == ('failed' if install_fails else 'success')
    if install_fails:
        assert value['message'] == 'installation failure'
    else:
        assert value['results'][0]['status'] == 'updated'
