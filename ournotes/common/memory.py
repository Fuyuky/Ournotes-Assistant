"""System memory guards using each platform's native budget."""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

GIB = 1024 ** 3


class _PerformanceInformation(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            'CommitTotal', 'CommitLimit', 'CommitPeak', 'PhysicalTotal',
            'PhysicalAvailable', 'SystemCache', 'KernelTotal', 'KernelPaged',
            'KernelNonpaged', 'PageSize')
    ] + [(name, wintypes.DWORD) for name in ('HandleCount', 'ProcessCount', 'ThreadCount')]


@dataclass(frozen=True)
class CommitMemory:
    total: int
    limit: int
    physical_available: int
    source: str = 'windows-commit'

    @property
    def available(self):
        return max(0, self.limit - self.total)

    @property
    def reserve(self):
        # Leave room for concurrent screenshots, OCR and durable incident output.
        return max(4 * GIB, self.limit // 20)

    @property
    def low(self):
        return self.available < self.reserve

    def report(self):
        return {'source': self.source, 'commit_bytes': self.total, 'commit_limit_bytes': self.limit,
                'commit_available_bytes': self.available, 'reserve_bytes': self.reserve,
                'physical_available_bytes': self.physical_available}


def read_commit_memory():
    if sys.platform == 'darwin':
        from ournotes.platforms.memory import read_macos_memory
        return read_macos_memory()
    if os.name != 'nt':
        return None
    api = ctypes.WinDLL('psapi', use_last_error=True).GetPerformanceInfo
    api.argtypes = [ctypes.POINTER(_PerformanceInformation), wintypes.DWORD]
    api.restype = wintypes.BOOL
    info = _PerformanceInformation()
    info.cb = ctypes.sizeof(info)
    if not api(ctypes.byref(info), info.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    if not info.PageSize or not info.CommitLimit:
        raise OSError('Windows 返回了无效的系统提交内存信息。')
    return CommitMemory(info.CommitTotal * info.PageSize, info.CommitLimit * info.PageSize,
                        info.PhysicalAvailable * info.PageSize)
