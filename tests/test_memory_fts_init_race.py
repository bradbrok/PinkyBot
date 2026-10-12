"""Opening a ReflectionStore concurrently must not silently lose FTS5 (#366).

The FTS trigger migration used to run ``DROP TRIGGER`` + ``CREATE TRIGGER`` on
*every* open. Two processes overlapping in that window made the loser fail with
``trigger reflections_au already exists``, and the over-broad ``except
OperationalError`` read that as "FTS5 is not compiled in" — leaving that process
with keyword search degraded to LIKE for its whole lifetime, silently.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from pinky_memory.store import ReflectionStore
from pinky_memory.types import Reflection, ReflectionType


def _seed(db: Path) -> None:
    store = ReflectionStore(str(db))
    store.insert(Reflection(type=ReflectionType.fact, content="il daemon riavvia il gateway"))
    store.close()


def _patch_executescript(monkeypatch, fake):
    """Swap executescript on the store's connection.

    sqlite3.Connection is immutable, so the only seam is the connection
    factory: hand sqlite3.connect a subclass that overrides the method.
    """
    real_connect = sqlite3.connect

    class Patched(sqlite3.Connection):
        def executescript(self, script):
            return fake(self, script, super().executescript)

    def traced(*args, **kwargs):
        kwargs["factory"] = Patched
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", traced)


def _make_trigger_legacy(db: Path) -> None:
    """Restore the pre-migration trigger so every opener must migrate."""
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "DROP TRIGGER IF EXISTS reflections_au;\n"
        "CREATE TRIGGER reflections_au AFTER UPDATE ON reflections BEGIN\n"
        "    INSERT INTO reflections_fts(reflections_fts, rowid, id, content, context, project)\n"
        "    VALUES ('delete', old.rowid, old.id, old.content, old.context, old.project);\n"
        "    INSERT INTO reflections_fts(rowid, id, content, context, project)\n"
        "    VALUES (new.rowid, new.id, new.content, new.context, new.project);\n"
        "END;"
    )
    conn.commit()
    conn.close()


def _trace_opens(monkeypatch) -> list[str]:
    """Capture every SQL statement the next ReflectionStore(s) execute."""
    seen: list[str] = []
    real_connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    return seen


class TestFtsInitIsIdempotent:
    def test_reopening_an_up_to_date_db_runs_no_trigger_ddl(self, tmp_path, monkeypatch):
        """No DDL on reopen means no window for a concurrent opener to lose."""
        db = tmp_path / "memory.db"
        _seed(db)

        seen = _trace_opens(monkeypatch)
        store = ReflectionStore(str(db))
        store.close()

        # A read of sqlite_master is fine — it takes no lock and opens no
        # window. Only writing the trigger does.
        ddl = [
            s
            for s in seen
            if s.strip().upper().startswith(("DROP TRIGGER", "CREATE TRIGGER"))
            and "reflections_au" in s
        ]
        assert ddl == [], f"reopen still rewrites the FTS trigger: {ddl}"

    def test_concurrent_first_opens_migrate_exactly_once(self, tmp_path, monkeypatch):
        """Racing openers serialize: one migrates, the rest see it already done.

        Asserting "exactly one rewrite" is what makes this deterministic. The
        migration re-checks sqlite_master under the write lock, so however the
        8 threads interleave, only the first one to hold the lock can still
        find the trigger stale.
        """
        db = tmp_path / "memory.db"
        _seed(db)
        _make_trigger_legacy(db)

        seen = _trace_opens(monkeypatch)
        barrier = threading.Barrier(8)
        results: list[bool] = []
        lock = threading.Lock()

        def open_once():
            barrier.wait()
            store = ReflectionStore(str(db))
            with lock:
                results.append(store._fts5_available)
            store.close()

        threads = [threading.Thread(target=open_once) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results == [True] * 8, f"some opens lost FTS5: {results}"
        rewrites = [s for s in seen if s.strip().upper().startswith("DROP TRIGGER")]
        assert len(rewrites) == 1, f"expected a single migration, got {len(rewrites)}"

    def test_reopen_keeps_bm25_keyword_search_working(self, tmp_path):
        """The user-visible half: FTS still ranks, it has not fallen back to LIKE."""
        db = tmp_path / "memory.db"
        _seed(db)

        store = ReflectionStore(str(db))
        try:
            assert store._fts5_available is True
            hits = store.search_by_keyword_scored("gateway", limit=5)
            assert [r.content for _, r in hits] == ["il daemon riavvia il gateway"]
        finally:
            store.close()


class TestFtsInitErrorHandling:
    def test_unexpected_operational_error_is_not_mistaken_for_missing_fts5(
        self, tmp_path, monkeypatch
    ):
        """Only "no such module: fts5" means FTS5 is unavailable.

        Anything else is a real fault and must surface instead of silently
        switching every later recall() to LIKE.
        """
        db = tmp_path / "memory.db"

        def boom(conn, script, real):
            if "reflections_fts" in script:
                raise sqlite3.OperationalError("disk I/O error")
            return real(script)

        _patch_executescript(monkeypatch, boom)

        try:
            store = ReflectionStore(str(db))
        except sqlite3.OperationalError as exc:
            assert "disk I/O error" in str(exc)
        else:
            store.close()
            raise AssertionError("a disk I/O error was swallowed as 'FTS5 unavailable'")

    def test_missing_fts5_module_still_degrades_gracefully(self, tmp_path, monkeypatch):
        """The #295 behaviour we must keep: a build without FTS5 falls back to LIKE."""
        db = tmp_path / "memory.db"

        def no_fts5(conn, script, real):
            if "reflections_fts" in script:
                raise sqlite3.OperationalError("no such module: fts5")
            return real(script)

        _patch_executescript(monkeypatch, no_fts5)

        store = ReflectionStore(str(db))
        try:
            assert store._fts5_available is False
        finally:
            store.close()
