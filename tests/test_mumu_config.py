import json

import pytest

from ournotes.platforms import mumu_config as config


def installation(path):
    for name in ('nx_main/mumu-cli.exe', 'nx_main/sdk/external_renderer_ipc.dll'):
        item = path / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(b'synthetic')
    return path


@pytest.fixture(autouse=True)
def discovery(monkeypatch):
    config.discover.cache_clear()
    monkeypatch.setattr(config.sys, 'platform', 'win32')
    monkeypatch.delenv('OURNOTES_MUMU_ROOT', raising=False)
    monkeypatch.setattr(config, 'candidates', list)
    yield
    config.discover.cache_clear()


def test_discovery_requires_unambiguous_complete_install(tmp_path, monkeypatch):
    first = installation(tmp_path / '一号 MuMu')
    second = installation(tmp_path / '二号 MuMu')
    monkeypatch.setattr(config, 'candidates', lambda: [first, first])
    assert config.selected(tmp_path) == first
    config.discover.cache_clear()
    monkeypatch.setattr(config, 'candidates', lambda: [first, second])
    assert config.selected(tmp_path) is None
    assert len(config.status(tmp_path)['candidates']) == 2


def test_save_preserves_mac_options_and_relocates_next_operation(tmp_path, monkeypatch):
    from ournotes.platforms import system
    first = installation(tmp_path / 'old')
    second = installation(tmp_path / 'new')
    target = tmp_path / 'config/mumu.json'
    target.parent.mkdir()
    target.write_text(json.dumps({'instances': {'1': {'adb_port': 12345}}}), 'utf-8')
    monkeypatch.setattr('ournotes.paths.DATA_ROOT', tmp_path)
    config.save(tmp_path, str(first))
    assert system.default_mumu_root() == first
    config.save(tmp_path, str(second))
    assert system.default_mumu_root() == second
    assert config.load(tmp_path)['instances']['1']['adb_port'] == 12345
    with pytest.raises(ValueError):
        config.save(tmp_path, str(tmp_path / 'missing'))
    assert system.default_mumu_root() == second


def test_explicit_invalid_selection_does_not_fall_back(tmp_path, monkeypatch):
    good = installation(tmp_path / 'good')
    config.save(tmp_path, str(good))
    (good / 'nx_main/mumu-cli.exe').unlink()
    assert config.status(tmp_path)['available'] is False
