"""#889 — configure_rollback_journal must put a store connection into rollback
(TRUNCATE) journal mode and fail loud if it cannot leave WAL.

Companion to tests/test_agents_db_no_wal.py (#797/#220). The deleted-WAL
corruption family is eliminated by never running these stores in WAL mode:
rollback (TRUNCATE) journal mode has no ``-wal``/``-shm`` for an external
open/close to unlink out from under the daemon's connection. These tests pin the
contract for the shared helper: it really transitions a live WAL connection, and
it refuses to silently continue on the WAL substrate.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from pinky_daemon import db_journal
from pinky_daemon.db_journal import RollbackJournalError, configure_rollback_journal


def _journal_mode(db_path: str) -> str:
    c = sqlite3.connect(db_path)
    try:
        return str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        c.close()


def test_converts_live_wal_connection_to_truncate():
    """Required transition proof: a real connection is put into WAL first (negative
    pre-state), then drained and switched to TRUNCATE in place; fresh opens of the
    file are no longer WAL and the wal-index (-shm) is gone."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "store.db")
        conn = sqlite3.connect(path)
        # negative pre-state: the connection really is in WAL, with a live -shm
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE warmup(x)")
        conn.execute("INSERT INTO warmup VALUES (1)")
        conn.commit()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert os.path.exists(path + "-shm")  # WAL mode created the wal-index
        # the transition: drain the WAL, switch to rollback (TRUNCATE)
        assert configure_rollback_journal(conn) == "truncate"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
        # persistently non-WAL: fresh opens aren't WAL, and there is no -shm to orphan
        assert _journal_mode(path) != "wal"
        assert not os.path.exists(path + "-shm")
        conn.close()


class _FakeConn:
    """Connection stub: bare ``PRAGMA journal_mode`` always reports 'wal' (so the
    checkpoint branch runs), ``PRAGMA journal_mode=TRUNCATE`` returns the scripted
    sequence of effective modes, everything else is a no-op. Models a persistent
    writer/lock that never lets the DB leave WAL."""

    def __init__(self, truncate_results):
        self._seq = list(truncate_results)

    def execute(self, sql, *args):
        s = sql.strip().lower()
        fake = self

        class _Cur:
            def fetchone(self):
                if "journal_mode=truncate" in s:
                    return (fake._seq.pop(0),) if fake._seq else ("wal",)
                if s == "pragma journal_mode":
                    return ("wal",)
                return None

        return _Cur()


def test_strict_raises_when_it_cannot_leave_wal(monkeypatch):
    """Required fail demo: a stuck connection keeps reporting 'wal'. strict=True
    (default) must retry then RAISE, never silently continue on the WAL substrate."""
    monkeypatch.setattr(db_journal.time, "sleep", lambda *a, **k: None)
    conn = _FakeConn(truncate_results=["wal", "wal"])
    with pytest.raises(RollbackJournalError):
        configure_rollback_journal(conn, retries=2, busy_ms=10)


def test_non_strict_returns_last_mode_without_raising(monkeypatch):
    """strict=False is the diagnostic path: the same stuck connection returns the
    last observed (non-truncate) mode instead of raising."""
    monkeypatch.setattr(db_journal.time, "sleep", lambda *a, **k: None)
    conn = _FakeConn(truncate_results=["wal", "wal"])
    assert (
        configure_rollback_journal(conn, retries=2, busy_ms=10, strict=False) == "wal"
    )


def test_retries_then_succeeds(monkeypatch):
    """Busy once (reports 'wal'), then the lock clears and TRUNCATE takes: the
    helper returns 'truncate' without raising."""
    monkeypatch.setattr(db_journal.time, "sleep", lambda *a, **k: None)
    conn = _FakeConn(truncate_results=["wal", "truncate"])
    assert configure_rollback_journal(conn, retries=4, busy_ms=10) == "truncate"
