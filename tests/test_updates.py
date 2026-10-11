import base64
import hashlib
import io
import json
import zipfile
from contextlib import contextmanager

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ournotes.updates.github import SafeRedirect
from ournotes.updates.installer import install
from ournotes.updates.manifest import canonical, verify
from ournotes.updates.service import UpdateService


@pytest.fixture(autouse=True)
def windows_release_fixture_platform(monkeypatch):
    # These synthetic releases target Windows; dedicated cases pass macOS explicitly.
    monkeypatch.setattr('ournotes.updates.manifest.current_platform', lambda: 'windows-x64')
    monkeypatch.setattr('ournotes.updates.service.current_platform', lambda: 'windows-x64')


def release(tmp_path, extra=None, version="0.2.0", platform='windows-x64'):
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    executable = 'runtime/pythonw.exe' if platform == 'windows-x64' else 'runtime/bin/python3'
    files = {executable: b"fake runtime", "app/scripts/webui.py": b"fake script", "app/ournotes/control/web.py": b"fake app"}
    files.update(extra or {})
    package = tmp_path / "client.zip"
    with zipfile.ZipFile(package, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    manifest = {"schema": 2, "product": "ournotes-assistant", "platform": platform,
                "version": version, "channel": "stable", "executables": [executable] if platform == 'macos-arm64' else [],
                "artifact": {"name": "client.zip", "sha256": hashlib.sha256(package.read_bytes()).hexdigest(), "bytes": package.stat().st_size}, "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
    envelope = {"manifest": manifest, "signature": base64.b64encode(key.sign(canonical(manifest))).decode()}
    return package, envelope, public, key


@contextmanager
def unlocked(_):
    yield


def installation(tmp_path):
    root = tmp_path / "installed"
    root.mkdir()
    (root / "current.json").write_text('{"version":"0.1.0"}')
    data = root / "user-data"
    data.mkdir()
    (data / "counter.db").write_bytes(b"counter and unresolved evidence")
    return root, data


def test_rebind_blocks_version_switch_until_device_cleanup(tmp_path):
    from ournotes.common.devices import rebind_lease
    package, envelope, public, _ = release(tmp_path)
    root, data = installation(tmp_path)
    before = (root / 'current.json').read_bytes()
    with rebind_lease(data, 0), pytest.raises(RuntimeError, match='email-rebind'):
        install(root, data, package, envelope, public)
    assert (root / 'current.json').read_bytes() == before
    assert not list((root / 'versions').iterdir())


def test_signed_install_preserves_user_data_and_old_version(tmp_path):
    package, envelope, public, _ = release(tmp_path, {'app/config/storage-policy.json': b'{"keep_batches":3}'})
    root, data = installation(tmp_path)
    saved_policy = data / 'config/storage-policy.json'
    saved_policy.parent.mkdir()
    saved_policy.write_bytes(b'{"keep_batches":5}')
    old = root / "versions/0.1.0/app"
    old.mkdir(parents=True)
    (old / "old.py").write_bytes(b"old version")
    result = install(root, data, package, envelope, public, unlocked)
    assert result["installed_version"] == "0.2.0"
    assert json.loads((root / "current.json").read_text())["version"] == "0.2.0"
    assert (old / "old.py").read_bytes() == b"old version"
    assert (data / "counter.db").read_bytes() == b"counter and unresolved evidence"
    assert saved_policy.read_bytes() == b'{"keep_batches":5}'


@pytest.mark.parametrize("forbidden", ["../state/runtime.sqlite3", "app/data/catalog.json", "app/ournotes_dev/api.py",
                                        "app/state/runtime.sqlite3", "app/web/admin/index.html",
                                        "app/ournotes/integrations/oauth_setup.py", "app/client-connection.json"])
def test_signed_bundle_cannot_overwrite_data_or_ship_agent(tmp_path, forbidden):
    package, envelope, public, _ = release(tmp_path, {forbidden: b"bad"})
    root, data = installation(tmp_path)
    with pytest.raises(ValueError):
        install(root, data, package, envelope, public, unlocked)
    assert json.loads((root / "current.json").read_text())["version"] == "0.1.0"


def test_bad_signature_hash_and_active_writer_keep_old_version(tmp_path):
    package, envelope, public, _ = release(tmp_path)
    root, data = installation(tmp_path)
    envelope["manifest"]["version"] = "9.9.9"
    with pytest.raises(ValueError, match="签名"):
        install(root, data, package, envelope, public, unlocked)
    envelope["manifest"]["version"] = "0.2.0"
    with package.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="SHA256"):
        install(root, data, package, envelope, public, unlocked)
    package, envelope, public, _ = release(tmp_path)
    @contextmanager
    def active(name):
        if name == "runner":
            raise RuntimeError("active writer")
        yield
    with pytest.raises(RuntimeError):
        install(root, data, package, envelope, public, active)
    assert json.loads((root / "current.json").read_text())["version"] == "0.1.0"
    assert not list((root / "versions").glob(".staging-*"))


def test_prerelease_and_downgrade_rejected(tmp_path):
    package, envelope, public, key = release(tmp_path)
    envelope["manifest"]["version"] = "0.3.0-rc1"
    envelope["signature"] = base64.b64encode(key.sign(canonical(envelope["manifest"]))).decode()
    with pytest.raises(ValueError, match="稳定版"):
        verify(envelope, public)
    package, envelope, public, _ = release(tmp_path, version="0.0.1")
    root, data = installation(tmp_path)
    with pytest.raises(ValueError, match="高于"):
        install(root, data, package, envelope, public, unlocked)


def test_check_only_notifies_download_requires_explicit_call(tmp_path):
    package, envelope, public, _ = release(tmp_path)
    root, data = installation(tmp_path)
    (root / "update-public-key.txt").write_text(public)
    calls = []
    class Client:
        def latest(self):
            calls.append("check")
            return {"tag_name": "v0.2.0", "assets": [{"name": "ournotes-manifest-windows-x64.json", "id": 1}, {"name": "client.zip", "id": 2}]}
        def asset(self, identifier):
            calls.append(identifier)
            return io.BytesIO(json.dumps(envelope).encode() if identifier == 1 else package.read_bytes())
    service = UpdateService(data, root, client_factory=lambda repository: Client())
    assert service.check()["available"]
    assert calls == ["check"]
    assert not (data / "state/updates/downloads").exists()
    assert service.download()["downloaded"]
    assert calls == ["check", 1, 2]


def test_public_service_outage_does_not_change_running_state(tmp_path):
    root, data = installation(tmp_path)
    class Unavailable:
        def latest(self):
            raise ValueError("public release unavailable")
    service = UpdateService(data, root, client_factory=lambda *_: Unavailable())
    assert service.check()["status"] == "unavailable"
    assert (data / "counter.db").read_bytes() == b"counter and unresolved evidence"


def test_other_platform_or_failed_recheck_cannot_download_previous_release(tmp_path):
    root, data = installation(tmp_path)
    response = {'tag_name': 'v0.2.0', 'assets': [{'name': 'ournotes-manifest-windows-x64.json', 'id': 1}]}
    class Client:
        def latest(self):
            return response
    service = UpdateService(data, root, client_factory=lambda _: Client())
    assert service.check()['available']
    response['assets'][0]['name'] = 'ournotes-manifest-macos-arm64.json'
    assert service.check()['status'] == 'unavailable'
    assert service.release is None
    with pytest.raises(ValueError, match='先检查'):
        service.download()
    assert not (data / 'state/updates/downloads').exists()


def test_github_asset_redirect_rejects_non_github_destinations():
    from urllib.request import Request
    redirect = SafeRedirect()
    request = Request("https://api.github.com/repos/o/r/releases/assets/1", headers={"Authorization": "Bearer private-token"})
    target = redirect.redirect_request(request, None, 302, "redirect", {}, "https://release-assets.githubusercontent.com/asset")
    assert target.get_header("Authorization") is None
    with pytest.raises(ValueError):
        redirect.redirect_request(request, None, 302, "redirect", {}, "https://unexpected.example/asset")


def test_public_requests_never_require_or_transmit_credentials():
    from ournotes.updates.github import PUBLIC_REPOSITORY, GitHubReleases
    calls = []
    def transport(url, headers):
        calls.append((url, headers))
        return io.BytesIO(b'{"tag_name":"v0.2.0","assets":[]}')
    reader = GitHubReleases(transport=transport)
    reader.latest()
    assert PUBLIC_REPOSITORY in calls[0][0]
    assert 'Authorization' not in calls[0][1]
    with pytest.raises(ValueError, match='公开项目'):
        GitHubReleases('owner/private')


def test_platform_bound_updates_reject_windows_package_on_mac(tmp_path):
    package, envelope, public, _ = release(tmp_path)
    with pytest.raises(ValueError, match='平台'):
        verify(envelope, public, 'macos-arm64')
    root, data = installation(tmp_path)
    with pytest.raises(ValueError, match='平台'):
        install(root, data, package, envelope, public, unlocked, platform='macos-arm64')
    assert json.loads((root / 'current.json').read_text())['version'] == '0.1.0'


def test_mac_install_checks_mac_runtime_and_keeps_user_data(tmp_path):
    package, envelope, public, _ = release(tmp_path, platform='macos-arm64')
    root, data = installation(tmp_path)
    result = install(root, data, package, envelope, public, unlocked, platform='macos-arm64')
    assert result['installed_version'] == '0.2.0'
    assert json.loads((root / 'current.json').read_text())['platform'] == 'macos-arm64'
    assert (root / 'versions/0.2.0/runtime/bin/python3').is_file()
    assert (data / 'counter.db').read_bytes() == b'counter and unresolved evidence'


def test_source_checkout_does_not_attempt_binary_update(tmp_path):
    calls = []
    service = UpdateService(tmp_path, client_factory=lambda *_: calls.append(True))
    assert service.check()['status'] == 'source'
    assert not calls
