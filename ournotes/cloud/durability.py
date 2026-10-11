"""Linux publication durability: file bytes and directory entries precede pointer commit."""
from __future__ import annotations

import os
from pathlib import Path


def sync_directory(path):
    if os.name == "nt":
        return  # The production coordination server runs on Linux.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_version(root, names):
    if os.name == "nt":
        return
    root = Path(root)
    directories = {root}
    for name in names:
        path = root / name
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        parent = path.parent
        while parent.is_relative_to(root):
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        sync_directory(directory)
    sync_directory(root.parent)
