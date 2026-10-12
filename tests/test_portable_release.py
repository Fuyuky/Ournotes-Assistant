import base64
import hashlib
import io
import json
import os
import zipfile
from contextlib import nullcontext

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ournotes.updates.installer import install
from ournotes.updates.manifest import canonical
from ournotes.updates.portable import validate_package
from ournotes.updates.service import UpdateService


def release(tmp_path, platform='windows-x64', extra=None):
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    version = '0.2.0'
    prefix = f'versions/{version}/'
    runtime = 'runtime/python.exe' if platform == 'windows-x64' else 'runtime/bin/python3'
    digest = hashlib.sha256(b'{}').hexdigest()
    snapshot = 'snapshots/' + 'a' * 32
    files = {prefix + runtime: b'fake runtime', prefix + 'app/scripts/webui.py': b'pass',
             prefix + 'app/ournotes/control/web.py': b'pass',
             'current.json': json.dumps({'version': version, 'platform': platform}).encode(),
             'update-public-key.txt': public.encode(), 'boot_client.py': b'new launcher',
             'user-data/data/catalog/current.json': json.dumps({'directory': snapshot,
                 'sha256': {'catalog.json': digest, 'pools.json': digest}}).encode(),
             f'user-data/data/catalog/{snapshot}/catalog.json': b'{}',
             f'user-data/data/catalog/{snapshot}/pools.json': b'{}'}
    files.update({'boot.ps1': b'new boot', '启动控制台.cmd': b'new cmd'} if platform == 'windows-x64'
                 else {'boot.command': b'new boot', '启动控制台.command': b'new command'})
    files.update(extra or {})
    package = tmp_path / f'ournotes-{version}-{platform}.zip'
    manifest = {'schema': 3, 'product': 'ournotes-assistant', 'platform': platform,
                'version': version, 'channel': 'stable', 'artifact': {'name': package.name},
                'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
                'executables': [prefix + runtime] if platform == 'macos-arm64' else []}
    envelope = {'manifest': manifest, 'signature': base64.b64encode(key.sign(canonical(manifest))).decode()}
    with zipfile.ZipFile(package, 'w') as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
        archive.writestr('release.json', json.dumps(envelope))
    return package, envelope, public


def installation(tmp_path, platform='windows-x64'):
    root = tmp_path / 'installation'
    data = root / 'user-data'
    (data / 'config').mkdir(parents=True)
    (data / 'config/mumu.json').write_bytes(b'user config')
    (data / 'state').mkdir()
    (data / 'state/runtime.sqlite3').write_bytes(b'pending transaction')
    (root / 'current.json').write_text(json.dumps({'version': '0.1.0', 'platform': platform}))
    (root / 'boot_client.py').write_bytes(b'old launcher')
    return root, data


@pytest.mark.parametrize('platform', ['windows-x64', 'macos-arm64'])
def test_complete_zip_upgrades_launchers_and_materials_without_touching_state(tmp_path, platform):
    package, envelope, public = release(tmp_path, platform)
    root, data = installation(tmp_path, platform)
    assert validate_package(package, public, platform) == envelope
    result = install(root, data, package, envelope, public, lambda _: nullcontext(), platform=platform)
    assert result['installed_version'] == '0.2.0'
    assert (root / 'boot_client.py').read_bytes() == b'new launcher'
    assert (data / 'config/mumu.json').read_bytes() == b'user config'
    assert (data / 'state/runtime.sqlite3').read_bytes() == b'pending transaction'
    assert (data / 'data/catalog/current.json').is_file()
    if platform == 'macos-arm64' and os.name != 'nt':
        assert (root / 'versions/0.2.0/runtime/bin/python3').stat().st_mode & 0o111
        assert (root / 'boot.command').stat().st_mode & 0o111


@pytest.mark.parametrize('name', ['user-data/state/runtime.sqlite3', 'user-data/config/mumu.json',
                                 '../escape', 'versions/0.1.0/app/a.py', 'versions/0.2.0/app/web/admin/a.js'])
def test_signed_archive_still_cannot_modify_user_data(tmp_path, name):
    package, _, public = release(tmp_path, extra={name: b'bad'})
    with pytest.raises(ValueError):
        validate_package(package, public, 'windows-x64')


def test_file_corruption_rejected(tmp_path):
    package, _, public = release(tmp_path)
    with zipfile.ZipFile(package) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files['boot_client.py'] = b'corrupt'
    with zipfile.ZipFile(package, 'w') as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    with pytest.raises(ValueError, match='摘要'):
        validate_package(package, public, 'windows-x64')


def test_version_switch_failure_restores_launchers_and_catalog_pointer(tmp_path, monkeypatch):
    from ournotes.updates import portable
    package, envelope, public = release(tmp_path)
    root, data = installation(tmp_path)
    monkeypatch.setattr(portable, 'atomic_json', lambda *args: (_ for _ in ()).throw(OSError('test failure')))
    with pytest.raises(OSError):
        install(root, data, package, envelope, public, lambda _: nullcontext(), platform='windows-x64')
    assert (root / 'boot_client.py').read_bytes() == b'old launcher'
    assert json.loads((root / 'current.json').read_text())['version'] == '0.1.0'
    assert not (root / 'versions/0.2.0').exists()
    assert not (data / 'data/catalog/current.json').exists()


def test_single_asset_download_verifies_embedded_signature(tmp_path):
    package, _, public = release(tmp_path)
    root, data = installation(tmp_path)
    (root / 'update-public-key.txt').write_text(public)
    calls = []
    class Client:
        def latest(self):
            calls.append('check')
            return {'tag_name': 'v0.2.0', 'assets': [{'name': package.name, 'size': package.stat().st_size}]}

        def package(self, version, name):
            calls.append(name)
            return io.BytesIO(package.read_bytes())
    service = UpdateService(data, root, client_factory=lambda _: Client(), platform='windows-x64')
    assert service.check()['available']
    assert calls == ['check']
    assert service.download()['downloaded']
    assert calls == ['check', package.name]
