"""Whole-operation leases using Windows client CRT locks or Linux server flock."""
from __future__ import annotations

import errno
import os


def try_lock(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        # flock protects distinct handles in the same process as well as other
        # processes; POSIX lockf would silently allow competing local threads.
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def busy(error):
    return error.errno in (errno.EACCES, errno.EAGAIN, 36)
