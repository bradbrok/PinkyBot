"""Probe SQLite's process-owned POSIX locks from a separate process."""

from __future__ import annotations

import ctypes
import fcntl
import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger("pinky.storage")


# struct flock has a different field order on Darwin and Linux. ctypes supplies
# the native alignment (including trailing padding) on both supported platforms.
class _Flock(ctypes.Structure):
    _fields_ = (
        [
            ("start", ctypes.c_longlong),
            ("length", ctypes.c_longlong),
            ("pid", ctypes.c_int),
            ("type", ctypes.c_short),
            ("whence", ctypes.c_short),
        ]
        if sys.platform == "darwin"
        else [
            ("type", ctypes.c_short),
            ("whence", ctypes.c_short),
            ("start", ctypes.c_longlong),
            ("length", ctypes.c_longlong),
            ("pid", ctypes.c_int),
        ]
    )


def _probe(stores: list[list[str]], pid: int) -> list[str]:
    """Child only: opening and closing here cannot release the parent's locks."""
    missing = []
    for name, path in stores:
        for suffix, offset, length, label in (
            ("", 1073741826, 510, "SHARED"),
            ("-shm", 128, 1, "DMS"),
        ):
            try:
                fd = os.open(path + suffix, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    lock = _Flock()
                    lock.type = fcntl.F_WRLCK
                    lock.whence, lock.start, lock.length = os.SEEK_SET, offset, length
                    held = _Flock.from_buffer_copy(fcntl.fcntl(fd, fcntl.F_GETLK, bytes(lock)))
                    present = held.type == fcntl.F_RDLCK and held.pid == pid
                finally:
                    os.close(fd)
            except OSError:
                present = False
            if not present:
                missing.append(f"{name}:{label}")
    return missing


def check_sqlite_locks(stores: list[tuple[str, str]], *, timeout: float = 10) -> dict:
    """Return a health snapshot; a failed probe never prevents startup."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pinky_daemon.sqlite_lock_check", str(os.getpid())],
            input=json.dumps(stores),
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
        missing = json.loads(result.stdout)
        status = {"healthy": not missing, "checked": len(stores), "missing": missing}
    except Exception as exc:
        status = {
            "healthy": False,
            "checked": len(stores),
            "missing": [],
            "error": f"lock probe failed: {type(exc).__name__}",
        }
    if not status["healthy"]:
        logger.critical("SQLite lock self-check failed: %s", status)
    return status


if __name__ == "__main__":
    print(json.dumps(_probe(json.load(sys.stdin), int(sys.argv[1]))))
