"""Rollback (TRUNCATE) journal-mode configuration for daemon SQLite stores.

An idle WAL connection retains a SHARED lock on the main database and a DMS
lock on shared memory. Ordinary external readers and writers cannot unlink
its sidecars while those locks remain held. The deleted-WAL incidents required
an earlier lock drop: a startup permission sweep opened and closed raw file
descriptors in the daemon process, releasing all POSIX locks on those inodes.
SQLite's own lock accounting then diverged from the kernel's state. An external
read-write closer could checkpoint and unlink the WAL; subsequent daemon writes
went to the orphaned inode and could be lost on a crash.

Rollback mode removes the WAL/shared-memory failure surface, at the cost of
reader/writer concurrency. It does not make raw in-process closes safe: they
also release rollback-journal locks. Preserve SQLite's locks in either mode.
Use the daemon snapshot interface for consistent external inspection.
"""

from __future__ import annotations

import sqlite3
import time

from pinky_daemon.store_catalog import (
    StoreConnectionPolicy,
    apply_store_connection_policy,
    default_store_connection_policy,
)


class RollbackJournalError(RuntimeError):
    """Raised when a store DB cannot be confirmed in rollback (TRUNCATE) journal
    mode — we refuse to silently run on the WAL corruption substrate (#889)."""


def configure_rollback_journal(
    conn: sqlite3.Connection,
    *,
    busy_ms: int | None = None,
    retries: int | None = None,
    strict: bool = True,
    policy: StoreConnectionPolicy | None = None,
) -> str:
    """Put ``conn`` into rollback (TRUNCATE) journal mode. Returns the effective
    journal mode (``"truncate"`` on success).

    Drains any existing WAL first (``wal_checkpoint(TRUNCATE)``) so no hot WAL
    content is stranded before the wal-index is dropped, then switches to
    ``journal_mode=TRUNCATE`` with bounded retries. Sets ``busy_timeout`` first so
    the mode switch and subsequent access tolerate the rollback-mode lock
    serialization. With ``strict=True`` (default) raises :class:`RollbackJournalError`
    if the mode cannot be confirmed, rather than silently continuing on WAL.
    """
    declared_policy = policy or default_store_connection_policy("conversations")
    effective_busy_ms = declared_policy.busy_timeout_ms if busy_ms is None else busy_ms
    effective_retries = declared_policy.rollback_retries if retries is None else retries
    apply_store_connection_policy(
        conn,
        StoreConnectionPolicy(
            busy_timeout_ms=effective_busy_ms,
            rollback_retries=declared_policy.rollback_retries,
            rollback_retry_delay_seconds=declared_policy.rollback_retry_delay_seconds,
        ),
    )
    last: str | None = None
    for attempt in range(effective_retries):
        try:
            cur = conn.execute("PRAGMA journal_mode").fetchone()
            if cur and str(cur[0]).lower() == "wal":
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        try:
            row = conn.execute("PRAGMA journal_mode=TRUNCATE").fetchone()
            last = str(row[0]).lower() if row else None
            if last == "truncate":
                return last
        except sqlite3.OperationalError as exc:
            last = f"error:{exc}"
        time.sleep(declared_policy.rollback_retry_delay_seconds * (attempt + 1))
    if strict:
        raise RollbackJournalError(
            f"DB refused to leave WAL: journal_mode={last!r} after {effective_retries} "
            f"attempts — refusing to run on the #889 deleted-WAL substrate."
        )
    return last or "unknown"
