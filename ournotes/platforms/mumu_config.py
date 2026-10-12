"""Discover installations without executing vendor tools; persist user selection."""
from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path

from ournotes.common.files import atomic_json


def load(root):
    path = Path(root) / 'config/mumu.json'
    value = json.loads(path.read_text('utf-8')) if path.is_file() else {}
    if not isinstance(value, dict):
        raise ValueError('MuMu 配置格式无效。')  # noqa: TRY004 -- user configuration error
    return value


def valid(path):
    path = Path(path)
    if sys.platform == 'win32':
        return all((path / name).is_file() for name in
                   ('nx_main/mumu-cli.exe', 'nx_main/sdk/external_renderer_ipc.dll'))
    return path.is_dir() and path.suffix == '.app' and (path / 'Contents').is_dir()


def candidates():
    if sys.platform == 'darwin':
        return [base / name for base in (Path('/Applications'), Path.home() / 'Applications')
                for name in ('MuMuPlayer.app', 'MuMuPlayer Pro.app')]
    paths = [Path('D:/Tools/MuMuPlayer')]
    for name in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        if os.environ.get(name):
            base = Path(os.environ[name])
            paths.extend(base / suffix for suffix in ('Netease/MuMuPlayer', 'MuMuPlayer'))
    if sys.platform == 'win32':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                        0, winreg.KEY_READ | view) as parent:
                        for index in range(winreg.QueryInfoKey(parent)[0]):
                            try:
                                with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as key:
                                    name = str(winreg.QueryValueEx(key, 'DisplayName')[0])
                                    if 'mumu' in name.lower():
                                        location = str(winreg.QueryValueEx(key, 'InstallLocation')[0]).strip()
                                        if location:
                                            paths.append(Path(location))
                            except OSError:
                                continue
                except OSError:
                    continue
    return paths


@lru_cache(maxsize=1)
def discover():
    found = {}
    for path in candidates():
        if valid(path):
            resolved = str(path.resolve())
            found[os.path.normcase(resolved)] = resolved
    return list(found.values())


def selected(root):
    configured = load(root).get('root')
    if configured:
        return Path(configured)
    override = os.environ.get('OURNOTES_MUMU_ROOT')
    if override:
        return Path(override).expanduser()
    found = discover()
    return Path(found[0]) if len(found) == 1 else None


def status(root):
    path = selected(root)
    return {'root': str(path) if path else '', 'available': bool(path and valid(path)),
            'candidates': discover()}


def save(root, directory):
    path = Path(directory.strip()).expanduser()
    if not path.is_absolute() or not valid(path):
        raise ValueError('请选择有效的 MuMu 安装目录（Windows 须包含 CLI 和 SDK；Mac 选择 .app）。')
    value = load(root)
    value['root'] = str(path.resolve())
    atomic_json(Path(root) / 'config/mumu.json', value)
    return status(root)
