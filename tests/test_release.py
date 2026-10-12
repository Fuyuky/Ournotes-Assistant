from pathlib import Path

from scripts.build_release import stage_source


def test_committed_runtime_catalog_matches_pinned_digests():
    from ournotes.catalogs.runtime import current_catalog
    source = Path(__file__).resolve().parents[1] / 'assets/runtime-catalog'
    assert current_catalog(source).is_dir()


def test_failed_build_discards_only_its_workspace(tmp_path, monkeypatch):
    import pytest
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from scripts.build_release import build

    key = tmp_path / 'key.pem'
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    def fail(target):
        target.mkdir(parents=True)
        (target / 'partial.py').write_text('partial')
        raise OSError('synthetic failed build')

    monkeypatch.setattr('scripts.build_release.stage_source', fail)
    output = tmp_path / 'output'
    with pytest.raises(OSError, match='synthetic'):
        build(output, '0.2.0', tmp_path / 'unused.zip', key)
    assert not list(output.glob('stage-*'))
    assert key.is_file()


def test_client_source_excludes_server_admin_and_user_data(tmp_path):
    target = tmp_path / "app"
    stage_source(target)
    assert (target / "ournotes/control/operations.py").is_file()
    assert (target / "scripts/webui.py").is_file()
    for forbidden in ("ournotes_dev", "devtools", "ournotes/cloud", "state", "results", "data", ".venv",
                      "ournotes/dev_capture.py", "ournotes/runner_ui.py"):
        assert not (target / forbidden).exists()
    assert {path.name for path in (target / "config").iterdir()} == {"presets.json", "input.json", "storage-policy.json"}
    assert (target / 'scripts/maintain_storage.py').is_file()
    assert not (target / "mail-rule.json").exists()
    assert {path.name for path in (target / "assets/login-history").iterdir()} == {"expand.png", "remove.png"}
    assert not any(path.name == "__pycache__" for path in target.rglob("*"))
    assert not (target / 'web/admin').exists()
    assert not (target / 'ournotes/integrations/oauth_setup.py').exists()
    assert not (target / 'ournotes/integrations/browser.py').exists()
    assert not (target / 'ournotes/reports/statistics.py').exists()
    assert not (target / 'ournotes/vision/assets/history-icons').exists()
    assert (target / 'ournotes/rebind/service.py').is_file()
    assert (target / 'ournotes/proxy/rotation.py').is_file()


def test_one_client_contains_business_core_without_development_variant(tmp_path):
    target = tmp_path / "app"
    stage_source(target)
    assert not (target / 'ournotes_dev').exists()
    assert not (target / 'devtools').exists()
    assert (target / "ournotes/control/launcher.py").read_bytes() == Path("ournotes/control/launcher.py").read_bytes()


def test_client_dependency_group_has_no_oauth_or_analysis():
    import tomllib
    project = tomllib.loads(Path('pyproject.toml').read_text('utf-8'))
    assert project['dependency-groups']['client'] == ['ournotes-assistant[client]']
    client = project['project']['optional-dependencies']['client']
    assert not any('google-' in item or 'matplotlib' in item for item in client)
    assert 'websockets==15.0.1' in client
    assert any('google-auth-oauthlib' in item for item in project['project']['optional-dependencies']['server'])


def test_initial_resources_ship_only_one_current_snapshot(tmp_path, monkeypatch):
    import json

    import scripts.build_release as module
    source = tmp_path / 'source'
    snapshot = source / 'data/catalog/snapshots/current'
    snapshot.mkdir(parents=True)
    (snapshot / 'catalog.json').write_text('{}')
    (source / 'data/catalog/current.json').write_text(json.dumps({'directory': 'snapshots\\current'}))
    raw = source / 'data/catalog/site'
    raw.mkdir()
    (raw / 'private-or-large.json').write_text('not runtime')
    monkeypatch.setattr(module, 'ROOT', source)
    module.stage_resources(tmp_path / 'client')
    assert (tmp_path / 'client/user-data/data/catalog/snapshots/current/catalog.json').is_file()
    assert not (tmp_path / 'client/user-data/data/catalog/site').exists()
    assert json.loads((tmp_path / 'client/user-data/data/catalog/current.json').read_text())['directory'] == 'snapshots/current'


def test_initial_resources_refuse_site_or_parent_pointer(tmp_path, monkeypatch):
    import json

    import pytest

    import scripts.build_release as module
    source = tmp_path / 'source'
    catalog = source / 'data/catalog'
    catalog.mkdir(parents=True)
    monkeypatch.setattr(module, 'ROOT', source)
    for name in ('site', '../private', 'snapshots/version/../private', 'snapshots\\version'):
        (catalog / 'current.json').write_text(json.dumps({'directory': name}))
        with pytest.raises(ValueError, match='快照'):
            module.stage_resources(tmp_path / 'client')
        assert not (tmp_path / 'client').exists()


def test_macos_runtime_flattens_internal_links_and_rejects_intel(tmp_path):
    import io
    import struct
    import tarfile

    import pytest

    from scripts.build_release import extract_runtime
    path = tmp_path / 'python.tar.gz'
    def archive(cpu):
        with tarfile.open(path, 'w:gz') as output:
            raw = struct.pack('<II', 0xFEEDFACF, cpu) + b'fake runtime'
            item = tarfile.TarInfo('python/bin/python3.12')
            item.size, item.mode = len(raw), 0o755
            output.addfile(item, io.BytesIO(raw))
            link = tarfile.TarInfo('python/bin/python3')
            link.type, link.linkname = tarfile.SYMTYPE, 'python3.12'
            output.addfile(link)
    archive(0x0100000C)
    target = tmp_path / 'mac'
    target.mkdir()
    assert extract_runtime(path, target, 'macos-arm64') == {'runtime/bin/python3', 'runtime/bin/python3.12'}
    assert not (target / 'bin/python3').is_symlink()
    archive(0x01000007)
    with pytest.raises(ValueError, match='Apple Silicon'):
        extract_runtime(path, tmp_path / 'intel', 'macos-arm64')


def test_mac_initial_zip_preserves_executable_boot_and_runtime(tmp_path):
    import zipfile

    from scripts.build_release import pack_tree
    source = tmp_path / 'initial'
    (source / 'runtime/bin').mkdir(parents=True)
    (source / 'boot.command').write_text('#!/bin/bash\n')
    (source / 'runtime/bin/python3').write_bytes(b'fake')
    package = tmp_path / 'client.zip'
    pack_tree(source, package, {'runtime/bin/python3'})
    with zipfile.ZipFile(package) as archive:
        for name in ('boot.command', 'runtime/bin/python3'):
            assert archive.getinfo(name).external_attr >> 16 & 0o111
            assert archive.getinfo(name).compress_type == zipfile.ZIP_DEFLATED



def test_release_zip_excludes_runtime_generated_bytecode(tmp_path):
    import zipfile

    from scripts.build_release import pack_tree

    app = tmp_path / "app"
    cache = app / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    (app / "module.py").write_text("pass", "utf-8")
    artifact = tmp_path / "release.zip"
    pack_tree(app, artifact)
    with zipfile.ZipFile(artifact) as archive:
        assert archive.namelist() == ["module.py"]


def test_build_cleanup_keeps_packages_and_refuses_output_root(tmp_path):
    import pytest

    from scripts.build_release import remove_build_workspace
    output = tmp_path/'output'
    stage = output/'stage'
    stage.mkdir(parents=True)
    (stage/'module.py').write_text('pass', encoding='utf8')
    artifact = output/'release.zip'
    artifact.write_bytes(b'preserved-package')
    with pytest.raises(ValueError):
        remove_build_workspace(output, output)
    assert artifact.is_file() and stage.is_dir()
    remove_build_workspace(output, stage)
    assert not stage.exists() and artifact.read_bytes() == b'preserved-package'
