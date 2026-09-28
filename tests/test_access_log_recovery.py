"""Access receipt recovery must preserve valid records and unrelated files."""

import gzip
import hashlib
import json
import logging
import os
import stat
import time

import pytest

from pinky_daemon.access_log import AccessLogWriter
from pinky_daemon.log_rotation import LogRotator
from pinky_daemon.routes.triggers import HookTokenRedactionFilter


def _records(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def test_rotation_reopen_failure_restores_live_and_next_tick_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "access.log"
    writer = AccessLogWriter(path, log=lambda _: None)
    rotator = LogRotator(path, mode="rename", on_rotate=writer.reopen, max_bytes=1)
    real_open = os.open
    failed = False

    def fail_once(target, *args, **kwargs):
        nonlocal failed
        if target == path and not failed:
            failed = True
            raise PermissionError("transient open failure")
        return real_open(target, *args, **kwargs)

    try:
        writer.write({"sequence": 0})
        monkeypatch.setattr(os, "open", fail_once)
        assert rotator.check_and_rotate() is None
        assert failed and path.exists()
        writer.write({"sequence": 1})
        assert _records(path) == [{"sequence": 0}, {"sequence": 1}]
        archive = rotator.check_and_rotate()
        assert archive is not None
        writer.write({"sequence": 2})
        archived = [json.loads(line) for line in gzip.decompress(archive.read_bytes()).splitlines()]
        assert archived + _records(path) == [{"sequence": n} for n in range(3)]
        assert writer.write_failures == 0
    finally:
        writer.close()


def test_rotation_recovers_crash_between_rename_and_handoff(tmp_path):
    path = tmp_path / "access.log"
    writer = AccessLogWriter(path, log=lambda _: None)
    raw = tmp_path / "access.log.2026-09-19T120000Z"
    try:
        writer.write({"before": 1})
        os.rename(path, raw)
        rotator = LogRotator(path, mode="rename", on_rotate=writer.reopen, max_bytes=1)
        archive = rotator.check_and_rotate()
        assert archive is not None and path.exists() and not raw.exists()
        writer.write({"after": 2})
        assert json.loads(gzip.decompress(archive.read_bytes())) == {"before": 1}
        assert _records(path) == [{"after": 2}]
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    finally:
        writer.close()


@pytest.mark.parametrize("published", [False, True])
def test_interrupted_compression_recovers_once_and_prunes(tmp_path, published):
    path = tmp_path / "access.log"
    raw = tmp_path / "access.log.2026-09-19T120000Z"
    raw.write_bytes(b'{"before":1}\n')
    raw.chmod(0o600)
    archive = raw.with_name(raw.name + ".gz")
    partial = archive.with_name(archive.name + ".part")
    if published:
        archive.write_bytes(gzip.compress(raw.read_bytes()))
        archive.chmod(0o600)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    else:
        partial.write_bytes(b"incomplete compressed bytes")
        os.utime(partial, (time.time() - 7200,) * 2)
    old = path.with_name(path.name + ".expired.gz")
    old.write_bytes(b"old archive")
    os.utime(old, (time.time() - 91 * 86400,) * 2)
    writer = AccessLogWriter(path, log=lambda _: None)
    try:
        writer.write({"after": 2})
        rotator = LogRotator(path, mode="rename", on_rotate=writer.reopen, backup_days=90)
        assert rotator.check_and_rotate() == archive
        assert not raw.exists() and not partial.exists() and not old.exists()
        assert list(tmp_path.glob("access.log.*.gz")) == [archive]
        assert json.loads(gzip.decompress(archive.read_bytes())) == {"before": 1}
        if published:
            assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
        writer.write({"after": 3})
        assert _records(path) == [{"after": 2}, {"after": 3}]
    finally:
        writer.close()


def test_prune_removes_only_old_regular_compression_temps(tmp_path):
    path = tmp_path / "access.log"
    stamp = "access.log.2026-09-19T120000Z.gz.part-"
    stale = tmp_path / (stamp + "old")
    fresh = tmp_path / (stamp + "fresh")
    linked = tmp_path / (stamp + "link")
    unrelated = tmp_path / "unrelated.part"
    for item in (stale, fresh, unrelated):
        item.write_bytes(b"preserve unless expired and owned")
    linked.symlink_to(unrelated)
    old = time.time() - 7200
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))
    os.utime(linked, (old, old), follow_symlinks=False)
    rotator = LogRotator(path, mode="rename", on_rotate=lambda: None)
    rotator._prune()
    assert not stale.exists()
    assert fresh.exists() and unrelated.exists()
    assert linked.is_symlink() and linked.read_bytes() == unrelated.read_bytes()


@pytest.mark.parametrize("fault", ["partial", "write_error", "rollback_error"])
def test_partial_append_completes_or_rolls_back_without_corrupting_next_record(
    tmp_path, monkeypatch, fault
):
    path = tmp_path / "access.log"
    messages = []
    writer = AccessLogWriter(path, log=messages.append)
    writer.write({"before": 0})
    real_write = os.write
    calls = 0

    def interrupted(fd, data):
        nonlocal calls
        if fd == writer.fd:
            calls += 1
            if calls == 1:
                return real_write(fd, data[:5])
            if calls == 2 and fault != "partial":
                raise OSError("injected write failure")
        return real_write(fd, data)

    def fail_rollback(*args):
        raise OSError("injected rollback failure")

    monkeypatch.setattr(os, "write", interrupted)
    if fault == "rollback_error":
        monkeypatch.setattr(os, "ftruncate", fail_rollback)
    try:
        writer.write({"first": 1})
        if fault == "partial":
            assert calls >= 2 and writer.write_failures == 0
            writer.write({"second": 2})
            assert _records(path) == [{"before": 0}, {"first": 1}, {"second": 2}]
        elif fault == "write_error":
            assert writer.write_failures == 1 and writer.enabled
            writer.write({"second": 2})
            assert _records(path) == [{"before": 0}, {"second": 2}]
            assert writer.write_failures == 1 and len(messages) == 1
        else:
            assert writer.write_failures == 1 and not writer.enabled
            damaged = path.read_bytes()
            writer.write({"second": 2})
            assert path.read_bytes() == damaged
            assert writer.write_failures == 2 and len(messages) == 1
    finally:
        writer.close()


def test_symlink_log_path_does_not_change_target_bytes_or_mode(tmp_path):
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"preserve existing bytes\n")
    victim.chmod(0o640)
    path = tmp_path / "access.log"
    path.symlink_to(victim)
    messages = []
    writer = AccessLogWriter(path, log=messages.append)
    try:
        writer.write({"receipt": 1})
        assert not writer.enabled and writer.write_failures == 1
        assert victim.read_bytes() == b"preserve existing bytes\n"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o640
        assert len(messages) == 1
    finally:
        writer.close()


@pytest.mark.parametrize("prefix", ["/a/", "/p/", "/ws/voice/"])
def test_preformatted_console_receipt_redacts_every_credential_position(prefix):
    record = logging.LogRecord("access", 20, "", 0, f"GET {prefix}planted-secret HTTP/1.1", (), None)
    assert HookTokenRedactionFilter().filter(record)
    assert record.getMessage() == f"GET {prefix}<redacted> HTTP/1.1"
    assert "planted-secret" not in record.getMessage()
