"""Tests for the console-log copytruncate rotator (log rotation, task #169).

Covers redaction, the copytruncate invariants (inode preserved, live file
truncated, archive gzipped + 0600), day-roll vs size-roll naming, the no-op
case, retention pruning, and the background loop's first-pass rotation.
"""
import gzip
import os
import stat
import time
from pathlib import Path

from pinky_daemon.log_rotation import LogRotator, _redact, run_rotation_loop

_TOKEN_LINE = (
    b"INFO httpx HTTP Request: GET "
    b"https://api.telegram.org/bot8123456789:AAGZxQ_realish-token-MATERIAL12345/getUpdates\n"
)


def _gunzip(path: Path) -> bytes:
    with gzip.open(path, "rb") as f:
        return f.read()


def test_redact_strips_token_secret_keeps_id():
    out = _redact(_TOKEN_LINE)
    assert b"bot8123456789:REDACTED" in out
    assert b"AAGZxQ_realish-token-MATERIAL12345" not in out


def test_redact_leaves_normal_lines_untouched():
    line = b"INFO startup: db permission sweep ok\n"
    assert _redact(line) == line


def test_size_roll_copytruncate_invariants(tmp_path):
    log = tmp_path / "api.log"
    body = _TOKEN_LINE + b"a normal line\n" + b"another line\n"
    log.write_bytes(body)
    ino_before = log.stat().st_ino

    rot = LogRotator(log, max_bytes=1, backup_days=14)  # any non-empty file rolls
    archive = rot.check_and_rotate()

    assert archive is not None and archive.exists()
    assert archive.suffix == ".gz"
    # Archive is owner-only.
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    # Archive content: token redacted, normal lines intact.
    restored = _gunzip(archive)
    assert b"bot8123456789:REDACTED" in restored
    assert b"AAGZxQ_realish-token-MATERIAL12345" not in restored
    assert b"a normal line\n" in restored and b"another line\n" in restored
    # Live file truncated in place, same inode (launchd's fd stays valid).
    assert log.exists() and log.stat().st_size == 0
    assert log.stat().st_ino == ino_before


def test_day_roll_names_archive_for_closed_day(tmp_path):
    log = tmp_path / "api.log"
    log.write_bytes(b"yesterday content\n")
    rot = LogRotator(log, max_bytes=10**9, backup_days=14)  # huge cap → not a size roll
    rot._current_day = "2020-01-01"  # pretend the live file accumulated that day

    archive = rot.check_and_rotate()

    assert archive is not None
    assert archive.name == "api.log.2020-01-01.gz"  # named for the day that closed
    assert _gunzip(archive) == b"yesterday content\n"
    assert log.stat().st_size == 0
    assert rot._current_day != "2020-01-01"  # advanced to today


def test_no_roll_when_small_and_same_day(tmp_path):
    log = tmp_path / "api.log"
    log.write_bytes(b"small\n")
    rot = LogRotator(log, max_bytes=10**9, backup_days=14)

    assert rot.check_and_rotate() is None
    assert log.read_bytes() == b"small\n"  # untouched
    assert not list(tmp_path.glob("api.log.*.gz"))


def test_missing_file_is_noop(tmp_path):
    rot = LogRotator(tmp_path / "api.log", max_bytes=1)
    assert rot.check_and_rotate() is None


def test_prune_removes_archives_past_retention(tmp_path):
    log = tmp_path / "api.log"
    log.write_bytes(b"x\n")
    old = tmp_path / "api.log.2020-01-01.gz"
    recent = tmp_path / "api.log.2020-02-02.gz"
    old.write_bytes(b"old")
    recent.write_bytes(b"recent")
    old_time = time.time() - 30 * 86400
    os.utime(old, (old_time, old_time))  # 30 days old
    # recent keeps its fresh mtime

    rot = LogRotator(log, backup_days=14)
    rot._prune()

    assert not old.exists()  # 30d > 14d retention → pruned
    assert recent.exists()  # within window → kept


def test_size_roll_suffixed_to_avoid_collision(tmp_path):
    log = tmp_path / "api.log"
    rot = LogRotator(log, max_bytes=1, backup_days=14)
    log.write_bytes(b"first\n")
    a1 = rot.check_and_rotate()
    log.write_bytes(b"second\n")
    a2 = rot.check_and_rotate()
    # Two size-rolls; archives must not collide even within the same second.
    assert a1 != a2
    assert a1.exists() and a2.exists()


async def test_run_rotation_loop_rolls_oversized_file_on_first_pass(tmp_path):
    log = tmp_path / "api.log"
    log.write_bytes(_TOKEN_LINE + b"data\n")
    # interval far in the future so only the immediate first-pass check runs.
    import asyncio

    task = asyncio.create_task(
        run_rotation_loop(log, max_bytes=1, backup_days=14, interval_s=3600)
    )
    # Yield until the first-pass rotation lands (thread offload).
    for _ in range(50):
        await asyncio.sleep(0.02)
        if list(tmp_path.glob("api.log.*.gz")):
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    archives = list(tmp_path.glob("api.log.*.gz"))
    assert archives, "loop should have rotated the oversized file on first pass"
    assert log.stat().st_size == 0
