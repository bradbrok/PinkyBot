"""Read isolated attachments from the same descriptor that was validated."""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from pinky_identity import live_sqlite

# Slack accepts the largest upload among the supported adapters: 1 GB.
MAX_MEDIA_BYTES = 1_000_000_000


class IsolatedFileError(OSError):
    """The caller's attachment could not be safely snapshotted."""


def path_for_fd(fd: int) -> Path:
    """Return the kernel's current path for the opened file, or refuse."""
    if sys.platform == "darwin" and hasattr(fcntl, "F_GETPATH"):
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        return Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    if sys.platform.startswith("linux"):
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    raise OSError("opened file path lookup is unavailable")


@contextmanager
def media_snapshot(path: Path, working_dir: Path, tmp_parent: Path):
    """Yield a private bounded copy and remove it after the adapter finishes."""
    fd = None
    directory = None
    try:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("attachment must be a regular file with one link")
            opened_path = path_for_fd(fd)
            if not opened_path.is_relative_to(working_dir):
                raise ValueError("opened attachment is outside the caller's working directory")
            live_sqlite.refuse_sqlite_attachment(opened_path)
            if metadata.st_size > MAX_MEDIA_BYTES:
                raise ValueError("attachment exceeds the upload size limit")
            tmp_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="media-", dir=tmp_parent))
            snapshot = directory / path.name
            copied = 0
            with snapshot.open("xb") as output:
                while chunk := os.read(fd, min(65536, MAX_MEDIA_BYTES - copied + 1)):
                    copied += len(chunk)
                    if copied > MAX_MEDIA_BYTES:
                        raise ValueError("attachment exceeds the upload size limit")
                    output.write(chunk)
        except (OSError, RuntimeError, ValueError) as error:
            raise IsolatedFileError(str(error)) from error
        yield snapshot
    finally:
        try:
            if directory is not None:
                shutil.rmtree(directory)
        finally:
            if fd is not None:
                os.close(fd)
