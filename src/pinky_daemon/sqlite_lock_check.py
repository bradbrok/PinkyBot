"""Probe SQLite's process-owned POSIX locks from a separate process."""

from __future__ import annotations

import ctypes
import fcntl
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

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


def _probe_posix(stores: list[list[str]], pid: int) -> dict:
    """Child only: opening and closing here cannot release the parent's locks."""
    missing = []
    inconclusive = []
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
                    present = held.type != fcntl.F_UNLCK
                    if present and held.pid != pid:
                        inconclusive.append(f"{name}:{label}")
                finally:
                    os.close(fd)
            except OSError:
                present = False
            if not present:
                missing.append(f"{name}:{label}")
    return {"missing": missing, "inconclusive": inconclusive}


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
        probe = json.loads(result.stdout)
        missing = probe["missing"]
        inconclusive = probe["inconclusive"]
        status = {
            "healthy": False if missing else None if inconclusive else True,
            "checked": len(stores),
            "missing": missing,
        }
        if inconclusive:
            status["inconclusive"] = inconclusive
    except Exception as exc:
        status = {
            "healthy": False,
            "checked": len(stores),
            "missing": [],
            "error": f"lock probe failed: {type(exc).__name__}",
        }
    if status["healthy"] is False:
        logger.critical("SQLite lock self-check failed: %s", status)
    elif status["healthy"] is None:
        logger.info("SQLite lock self-check inconclusive behind another owner: %s", status)
    return status


def _probe_linux(stores: list[list[str]], pid: int) -> dict:
    """Inspect every holder; a peer cannot mask this process in /proc/locks."""
    locks = []
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[1:4] != ["POSIX", "ADVISORY", "READ"]:
            continue
        if int(fields[4]) != pid:
            continue
        major, minor, inode = fields[5].split(":")
        locks.append(
            (
                (int(major, 16), int(minor, 16), int(inode)),
                int(fields[6]),
                float("inf") if fields[7] == "EOF" else int(fields[7]),
            )
        )
    missing = []
    for name, path in stores:
        for suffix, offset, length, label in (
            ("", 1073741826, 510, "SHARED"),
            ("-shm", 128, 1, "DMS"),
        ):
            try:
                info = os.stat(path + suffix, follow_symlinks=False)
                identity = os.major(info.st_dev), os.minor(info.st_dev), info.st_ino
                present = any(
                    key == identity and start <= offset and end >= offset + length - 1
                    for key, start, end in locks
                )
            except OSError:
                present = False
            if not present:
                missing.append(f"{name}:{label}")
    return {"missing": missing, "inconclusive": []}


if __name__ == "__main__":
    probe = _probe_linux if sys.platform == "linux" else _probe_posix
    print(json.dumps(probe(json.load(sys.stdin), int(sys.argv[1]))))
