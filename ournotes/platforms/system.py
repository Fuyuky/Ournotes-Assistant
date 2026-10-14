"""Supported desktop targets and process / user-directory operations."""
from __future__ import annotations

import ctypes
import os
import platform as host_platform
import sys
from pathlib import Path

SUPPORTED_PLATFORMS = ("windows-x64",)


def current_platform(system=None, machine=None):
    system = sys.platform if system is None else system
    machine = (host_platform.machine() if machine is None else machine).lower()
    if system == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    raise RuntimeError("客户端仅支持 Windows x64。")


def app_data_dir():
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "OurNotesAssistant"
    raise RuntimeError("此系统没有受支持的客户端凭据存储。")


def default_mumu_root():
    from ournotes.paths import DATA_ROOT
    from ournotes.platforms.mumu_config import selected
    path = selected(DATA_ROOT)
    if path is None:
        raise ValueError('未唯一找到 MuMu，请在控制台保存安装目录。')
    return path


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not evidence that a writer has exited.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
