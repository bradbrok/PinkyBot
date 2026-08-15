"""Shared SQLite journal-mode configuration for long-lived daemon stores.

WAL mode is unsafe for the way this daemon holds SQLite open. Two properties
combine badly:

1. Every store opens the same database file once **per thread**
   (``self._thread_local.connection``), so a single process holds several
   independent connections to one file.
2. POSIX record locks are owned per ``(process, inode)``, not per descriptor.
   The moment *any* of those connections is closed — a worker thread ending, an
   explicit ``_reset_connection()`` — the process drops **all** of its advisory
   locks on that file, including the ones the surviving connections still rely
   on.

After that the daemon is invisible to other processes: the next external opener
(a backup, an operator running ``sqlite3``, a script) sees an unlocked database,
believes it is the sole connection, and on close checkpoints and **unlinks** the
``-wal``/``-shm``. The daemon's surviving connections keep writing happily into
the now-orphaned inode. Those writes are visible only through the daemon itself;
every external reader sees data frozen at the last checkpoint, and the whole tail
is lost when the process exits.

That failure mode was observed in production on 2026-08-01: four stores
(conversations, tasks, agent_comms, research) were left holding
``...-wal (deleted)`` with 54 messages, 5 inbox rows and 2 tasks reachable only
from the daemon's address space, while ``/proc/<pid>/locks`` showed the daemon
holding no locks at all.

Rollback (TRUNCATE) journalling sidesteps the whole class: committed data lives
in the main database file, there is no ``-wal`` for anyone to unlink and no
``-shm`` to map. It is the same remedy already applied to
``conversations_agents.db`` (#797/#220), the skills DB and the dream runner —
this module exists so those hand-rolled copies can converge on one
implementation instead of drifting.

Trade-off, deliberately accepted: rollback mode serialises readers against the
writer. These stores are low-throughput control-plane data, and correctness of
the write path matters more here than reader concurrency.
"""

from __future__ import annotations

import sqlite3
import time


class SqliteJournalConfigError(RuntimeError):
    """A store connection could not be moved off WAL."""


def configure_rollback_journal(
    conn: sqlite3.Connection,
    *,
    db_label: str,
    busy_ms: int = 30_000,
    retries: int = 6,
) -> str:
    """Put ``conn`` in rollback (TRUNCATE) journal mode and confirm it took.

    Must run BEFORE table init and before anything can spawn stdio children that
    hold the database, so an existing WAL file is converted in place while the
    daemon is still the only writer.

    Any hot WAL content is checkpointed first, so nothing committed is stranded
    when the wal-index is dropped. A busy database during that drain is
    non-fatal — the mode switch below retries.

    Fails LOUD: raises :class:`SqliteJournalConfigError` rather than silently
    running on WAL, because a silent fallback is exactly the state that loses
    writes.

    Args:
        conn: Connection to configure, before any table creation.
        db_label: Human-readable database name, used in the error message.
        busy_ms: ``busy_timeout`` applied to the connection.
        retries: Bounded attempts before giving up.

    Returns:
        The effective journal mode, always ``"truncate"``.
    """
    conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    last: str | None = None
    for attempt in range(retries):
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
        time.sleep(0.2 * (attempt + 1))

    raise SqliteJournalConfigError(
        f"{db_label} refused to leave WAL: journal_mode={last!r} after {retries} "
        f"attempts — refusing to run on WAL, where an unlinked -wal silently "
        f"strands committed writes in the daemon's address space (#797/#220)."
    )
