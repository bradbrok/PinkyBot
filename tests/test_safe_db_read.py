"""Regression tests for scripts/safe_db_read.py — the #889 tier-1 snapshot.

The acute bug (found in review of PR #1107): tier-1 ``snapshot_connect`` used
``shutil.copy2`` of the main DB file. The high-churn stores run in ROLLBACK-journal
mode (DELETE/TRUNCATE), where an ``UPDATE`` rewrites existing rows *in place*; when
the writer's page cache spills it flushes those DIRTY, UNCOMMITTED pages straight
into the main file (the pre-image undo lives in the ``-journal`` sidecar, which the
old code did not copy). The row count is unchanged, so the DB header does not
shield the reader: a raw byte copy reads the in-place dirty pages and returns
uncommitted, transactionally-impossible rows.

The fix reads the source through the sanctioned reader (``BoundSQLiteFile``) and
SQLite's online backup API: it runs inside a real read transaction, blocks behind
the writer's lock, and returns only the last COMMITTED state.

Determinism (no wall-clock sync): the writer holds an uncommitted, spilled UPDATE;
the snapshot signals ``backup_started`` and then blocks in ``backup()``; the main
thread releases only after that. Both threads append to a shared ordered trace, and
the test asserts ``backup_returned`` lands AFTER ``writer_release`` — which can only
happen if the backup genuinely blocked on the lock. A non-contending/trivial read
fails that ordering (fail-closed), so the test cannot pass by accident.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import threading
import time
from pathlib import Path

_SAFE_DB_READ_PATH = Path(__file__).resolve().parents[1] / "scripts" / "safe_db_read.py"
_spec = importlib.util.spec_from_file_location("safe_db_read", _SAFE_DB_READ_PATH)
safe_db_read = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(safe_db_read)

N_ROWS = 2500
PAD = 500  # fat rows so a tiny cache is forced to spill dirty pages to disk
OLD = "old" + "O" * PAD
NEW = "new" + "N" * PAD


def _make_rollback_db(path: Path, n_rows: int) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=TRUNCATE")  # rollback-family, not WAL
        con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        con.executemany(
            "INSERT INTO t(id, v) VALUES(?, ?)",
            [(i, OLD) for i in range(1, n_rows + 1)],
        )
        con.commit()
    finally:
        con.close()


def _tag_counts(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT substr(v, 1, 3), count(*) FROM t GROUP BY 1").fetchall())


def test_snapshot_shows_pre_txn_state_not_dirty_rows(tmp_path):
    """A snapshot taken while a writer holds an open, uncommitted rollback-mode
    UPDATE (dirty pages already spilled to the main file) must return the
    PRE-transaction committed state — and the operation trace must prove the
    backup blocked on the writer's lock rather than racing it."""
    db = tmp_path / "churn.db"
    _make_rollback_db(db, N_ROWS)

    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=TRUNCATE")
    writer.execute("PRAGMA cache_size=10")  # tiny => dirty pages flush in-place to main file
    writer.execute("BEGIN")
    writer.execute("UPDATE t SET v = ?", (NEW,))
    # NOT committed: 'new' rows sit in the main DB file (in-place) and the writer
    # holds the rollback-mode EXCLUSIVE lock. A raw byte copy would read them.

    trace: list[str] = []
    trace_lock = threading.Lock()
    backup_started = threading.Event()
    result: dict = {}

    def _snapshot():
        try:
            backup_started.set()  # about to enter the (blocking) snapshot
            with safe_db_read.snapshot_connect(str(db)) as snap:
                with trace_lock:
                    trace.append("backup_returned")
                result["counts"] = _tag_counts(snap)
        except Exception as exc:  # noqa: BLE001 - surfaced to the assertion
            with trace_lock:
                trace.append("backup_error")
            result["error"] = exc

    th = threading.Thread(target=_snapshot)
    th.start()

    assert backup_started.wait(timeout=5), "snapshot thread never started"
    # Minimal nudge so the C backup_step engages the lock-wait before we release.
    # The trace-ordering assertion below is the real proof, not this sleep.
    time.sleep(0.3)
    with trace_lock:
        trace.append("writer_release")
    writer.rollback()  # release the lock; main file restored to committed state
    writer.close()

    th.join(timeout=35)
    assert not th.is_alive(), "snapshot did not complete (deadlock?)"
    assert "error" not in result, f"snapshot raised: {result.get('error')!r}"
    assert result["counts"] == {"old": N_ROWS}, (
        f"expected {N_ROWS} committed 'old' rows, got {result['counts']} "
        "(uncommitted 'new' rows leaked into the snapshot)"
    )
    # Proof it actually blocked: the backup returned only after the writer released.
    assert "writer_release" in trace and "backup_returned" in trace, trace
    assert trace.index("backup_returned") > trace.index("writer_release"), (
        f"backup returned before the writer released ({trace}) — it did not block "
        "on the lock, so the snapshot raced the writer instead of waiting it out"
    )


def test_snapshot_is_a_detached_copy(tmp_path):
    """The yielded connection is a copy: writes to it never touch the live file,
    and the live file is intact after the snapshot context exits."""
    db = tmp_path / "plain.db"
    _make_rollback_db(db, 40)

    with safe_db_read.snapshot_connect(str(db)) as snap:
        assert snap.execute("SELECT count(*) FROM t").fetchone()[0] == 40
        snap.execute("INSERT INTO t(v) VALUES('local-only')")
        snap.commit()

    live = sqlite3.connect(db)
    try:
        assert live.execute("SELECT count(*) FROM t").fetchone()[0] == 40
    finally:
        live.close()
