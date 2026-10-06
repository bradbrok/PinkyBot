"""Validate transcript descriptors and copy attachments outside this process."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio

from pinky_identity import live_sqlite

# Slack accepts the largest upload among the supported adapters: 1 GB.
MAX_MEDIA_BYTES = 1_000_000_000
# Bound filesystem stalls while allowing a local 1 GB copy to finish.
MEDIA_COPY_TIMEOUT_SEC = 30.0
_COPY_CHILD = Path(__file__).with_name("isolated_media_copy.py")


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


def open_owned_transcript(path: Path, predicate: Callable[[Path], bool]):
    """Open one regular transcript and validate the path of that descriptor."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        if not stat.S_ISREG(os.fstat(fd).st_mode) or not predicate(path_for_fd(fd)):
            return None
        handle = os.fdopen(fd, "rb")
        fd = None
        return handle
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


async def _copy_media(payload: dict) -> dict:
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(_COPY_CHILD), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            child.communicate(json.dumps(payload).encode()), MEDIA_COPY_TIMEOUT_SEC,
        )
        result = json.loads(stdout)
        if child.returncode or not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("reason", "invalid child output") if isinstance(result, dict) else "invalid child output"
            raise IsolatedFileError(f"attachment copy refused: {str(reason)[:500]}")
        if any(type(result.get(key)) is not int or result[key] < 0 for key in ("size", "dev", "ino")):
            raise IsolatedFileError("invalid attachment copy metadata")
        return result
    except IsolatedFileError:
        raise
    except (OSError, ValueError, asyncio.TimeoutError) as error:
        raise IsolatedFileError("attachment copy refused or timed out") from error
    finally:
        with anyio.CancelScope(shield=True):
            if child.returncode is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
            await child.wait()


@asynccontextmanager
async def media_snapshot(path: Path, working_dir: Path, tmp_parent: Path):
    """Await a private bounded child-process copy and remove it after sending."""
    directory = None
    try:
        try:
            await asyncio.to_thread(live_sqlite.refuse_sqlite_attachment, path)
            if (await asyncio.to_thread(path.stat)).st_size > MAX_MEDIA_BYTES:
                raise IsolatedFileError("attachment exceeds the upload size limit")
            identities = await asyncio.to_thread(live_sqlite.live_sqlite_identities)
            tmp_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="media-", dir=tmp_parent))
            snapshot = directory / path.name
            result = await _copy_media({
                "path": str(path), "working_dir": str(working_dir), "target": str(snapshot),
                "size_cap": MAX_MEDIA_BYTES, "live_sqlite": sorted(identities),
            })
            info = await asyncio.to_thread(snapshot.lstat)
            if (
                not stat.S_ISREG(info.st_mode)
                or type(result.get("size")) is not int
                or info.st_size != result["size"]
                or not 0 <= info.st_size <= MAX_MEDIA_BYTES
            ):
                raise IsolatedFileError("attachment snapshot size does not match the copy")
        except (OSError, RuntimeError, ValueError) as error:
            raise IsolatedFileError(str(error)) from error
        yield snapshot
    finally:
        if directory is not None:
            with anyio.CancelScope(shield=True):
                await asyncio.to_thread(shutil.rmtree, directory)
