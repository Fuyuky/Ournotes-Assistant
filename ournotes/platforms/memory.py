"""macOS memory-pressure readings, without pretending macOS has Windows commit limits."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class MacMemory:
    limit: int
    physical_available: int
    source: str = "macos-memory"

    @property
    def total(self):
        return self.limit - self.physical_available

    @property
    def available(self):
        return self.physical_available

    @property
    def reserve(self):
        return max(1024 ** 3, self.limit // 20)

    @property
    def low(self):
        return self.available < self.reserve

    def report(self):
        return {"source": self.source, "physical_total_bytes": self.limit,
                "physical_available_bytes": self.available, "reserve_bytes": self.reserve}


def read_macos_memory():
    try:
        total_raw = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"],
                                   capture_output=True, check=True, timeout=5).stdout
        pressure_raw = subprocess.run(["/usr/bin/memory_pressure", "-Q"],
                                      capture_output=True, check=True, timeout=5,
                                      env={"PATH": "/usr/bin:/usr/sbin:/bin", "LC_ALL": "C"}).stdout
        total = int(total_raw.strip())
        match = re.search(rb"System-wide memory free percentage:\s*(\d+)%", pressure_raw)
        if total <= 0 or match is None or not 0 <= int(match[1]) <= 100:
            raise ValueError("invalid memory-pressure response")
        return MacMemory(total, total * int(match[1]) // 100)
    except (subprocess.SubprocessError, ValueError) as error:
        raise OSError("无法读取 macOS 系统内存压力。") from error
