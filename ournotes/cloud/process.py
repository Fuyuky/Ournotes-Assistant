"""One cloud process per server-local database; lock released by OS on process exit."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from ournotes.cloud.store import Unavailable


@contextmanager
def process_lease(database):
    path = Path(database).resolve().with_suffix(".service.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except OSError:
        if not acquired:
            raise Unavailable("Another coordination process owns this database") from None
        raise
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
