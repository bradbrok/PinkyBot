"""Daily, size-bounded rotation for the daemon's console log.

The daemon's console output (every ``_log()`` helper is a plain ``print()``)
is captured by launchd's ``StandardOutPath`` / ``StandardErrorPath`` into a
single ``logs/api.log``. Left unmanaged it grows without bound — in production
it reached ~1.5 GB — and, before #676 silenced httpx INFO request logging, it
accumulated cleartext Telegram bot-token poll URLs.

This module rotates that file **in place using copytruncate**: the live file is
copied to a gzipped, token-redacted archive and then truncated to zero *without*
changing its inode, so launchd's open file descriptor keeps appending to the
same file with no reopen needed. This matters because launchd does **not**
reopen ``StandardOutPath`` after a rename — a move-based rotation (newsyslog /
``TimedRotatingFileHandler``) would leave launchd writing to the renamed
archive while ``api.log`` stayed empty. Truncating in place sidesteps that
entirely and needs no plist change.

Rotation runs from a background task on the daemon event loop
(:func:`run_rotation_loop`); the blocking copy/gzip work is offloaded to a
thread so it never stalls the loop. It triggers when the UTC day rolls over or
the live file crosses a size cap (whichever comes first), so a single runaway
day cannot reproduce the 1.5 GB file. Archives are created ``0600``, gzipped,
have Telegram bot tokens redacted as they are written, and are pruned past a
retention window.

copytruncate caveat: a log line written in the narrow window between the copy
reaching EOF and the truncate is lost from the archive. For a daemon console
log this is an acceptable trade for never dropping launchd's file descriptor.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("pinky.log_rotation")

_ARCHIVE_FILE_MODE = 0o600
_DEFAULT_MAX_BYTES = 500 * 1024 * 1024  # 500 MB — size-roll guard
_DEFAULT_BACKUP_DAYS = 14
_CHECK_INTERVAL_S = 300  # how often the loop checks whether a roll is due

# Redact Telegram bot tokens in archived lines. The token in
# ``api.telegram.org/bot<id>:<secret>/...`` is the secret after the colon; the
# numeric bot id is not sensitive and is kept so the archive stays diagnosable.
_TELEGRAM_TOKEN_RE = re.compile(rb"(bot\d{6,}:)[A-Za-z0-9_-]{20,}")

__all__ = [
    "LogRotator",
    "run_rotation_loop",
]


def _redact(line: bytes) -> bytes:
    return _TELEGRAM_TOKEN_RE.sub(rb"\1REDACTED", line)


def _archive_path(log_path: Path, stamp: str) -> Path:
    """``logs/api.log`` + stamp -> a non-colliding ``logs/api.log.<stamp>.gz``."""
    base = log_path.name
    candidate = log_path.with_name(f"{base}.{stamp}.gz")
    n = 1
    while candidate.exists():
        candidate = log_path.with_name(f"{base}.{stamp}.{n}.gz")
        n += 1
    return candidate


def _copytruncate(log_path: Path, archive: Path) -> int:
    """Copy ``log_path`` into ``archive`` (gzip + redact), then truncate in place.

    Returns the number of source bytes archived. The live file's inode is
    preserved (``truncate`` rather than ``unlink``) so launchd's append-mode fd
    keeps writing to the same file after the roll.
    """
    tmp = archive.with_name(archive.name + ".part")
    if tmp.exists():
        tmp.unlink()
    written = 0
    # O_EXCL + 0600 from creation: the archive is never briefly world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _ARCHIVE_FILE_MODE)
    with open(log_path, "rb") as src, gzip.GzipFile(
        fileobj=os.fdopen(fd, "wb"), mode="wb"
    ) as dst:
        for line in src:
            dst.write(_redact(line))
            written += len(line)
    os.replace(tmp, archive)
    # Truncate the live file in place — preserves inode + perms for launchd's fd.
    with open(log_path, "r+b") as f:
        f.truncate(0)
    return written


class LogRotator:
    """Stateful copytruncate rotator for a single console log file.

    Holds the UTC day the live file is currently accumulating so a day-roll
    archive is named for the day it actually covers. Methods are synchronous
    and intended to be called from a thread (see :func:`run_rotation_loop`).
    """

    def __init__(
        self,
        log_path: str | Path,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_days: int = _DEFAULT_BACKUP_DAYS,
    ) -> None:
        self.log_path = Path(log_path)
        self.max_bytes = max_bytes
        self.backup_days = backup_days
        self._current_day = self._today()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def check_and_rotate(self) -> Path | None:
        """Rotate if the day rolled over or the file exceeds the size cap.

        Returns the archive path if a rotation happened, else ``None``.
        Best-effort: any failure is logged and swallowed (returns ``None``).
        """
        try:
            size = self.log_path.stat().st_size
        except FileNotFoundError:
            return None
        today = self._today()
        size_roll = size >= self.max_bytes
        day_roll = today != self._current_day and size > 0
        if not (size_roll or day_roll):
            return None

        # Day-roll archives are named for the day that just closed; a same-day
        # size-roll gets a time suffix so multiple rolls per day never collide.
        if day_roll and not size_roll:
            stamp = self._current_day
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        archive = _archive_path(self.log_path, stamp)

        try:
            _copytruncate(self.log_path, archive)
        except Exception:
            logger.exception("log_rotation: copytruncate failed")
            return None

        if day_roll:
            self._current_day = today
        self._prune()
        logger.info("log_rotation: rotated console log and pruned old archives")
        return archive

    def _prune(self) -> None:
        """Delete archives older than the retention window. Best-effort."""
        cutoff = time.time() - self.backup_days * 86400
        for p in self.log_path.parent.glob(f"{self.log_path.name}.*.gz"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


async def run_rotation_loop(
    log_path: str | Path,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_days: int = _DEFAULT_BACKUP_DAYS,
    interval_s: int = _CHECK_INTERVAL_S,
) -> None:
    """Background task: rotate ``log_path`` on day-roll or size cap, forever.

    An oversized pre-existing file (e.g. the legacy 1.5 GB ``api.log``) is
    rolled promptly on the first check. Blocking copy/gzip runs in a thread.
    """
    rotator = LogRotator(log_path, max_bytes=max_bytes, backup_days=backup_days)
    first = True
    while True:
        if not first:
            await asyncio.sleep(interval_s)
        first = False
        try:
            await asyncio.to_thread(rotator.check_and_rotate)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("log_rotation: periodic check failed")
