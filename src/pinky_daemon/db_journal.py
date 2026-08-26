"""Rollback (TRUNCATE) journal-mode configuration for daemon SQLite stores.

#889 — deleted-WAL corruption family. Root cause (proven 2026-08-16 via auditd
syscall capture): an EXTERNAL process opening a live WAL-mode daemon DB read-write
and closing it (e.g. an ad-hoc ``sqlite3 data/conversations.db "SELECT ..."`` CLI
read, or any plain ``sqlite3.connect``) runs SQLite's checkpoint-on-last-close,
which unlinks the ``-wal``/``-shm`` files. Because POSIX advisory file locks are
held PER-PROCESS (not per-fd), the external process can become the effective
"last connection" while the daemon's own long-lived connection is idle between
transactions — so SQLite deletes the ``-wal``/``-shm`` out from under the daemon's
open fd. The daemon then holds an fd to a DELETED inode; its data stays intact
until the next process exit/restart, at which point the un-replayed WAL is gone
and the main DB can be left header-corrupt (observed: a main file beginning with a
b-tree leaf page instead of the SQLite header, a full outage on 2026-08-15).

The structural fix is the same one already applied to ``conversations_agents.db``
(#797/#220): run these stores in rollback (TRUNCATE) journal mode. Rollback mode
has no ``-wal``/``-shm`` at all, so there is nothing an external open can unlink —
the corruption substrate simply does not exist. Trade-off: rollback mode serializes
writers vs readers (no WAL concurrency), mitigated by a generous ``busy_timeout``.

Behavioral half of the fix (see ``scripts/safe_db_read.py``): never open a live
daemon DB with a plain/RW ``sqlite3`` client; use a copy or ``mode=ro&immutable=1``.
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
