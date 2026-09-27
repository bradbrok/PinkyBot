"""Opening a ReflectionStore must not write when there is nothing to backfill (#368).

_migrate_backfill_review_schedule ran an unconditional UPDATE on every open, so
every component that constructs a store took a write lock at daemon start even
on a database where every row was already scheduled.

Careful: despite its "on first run" docstring, this UPDATE is load-bearing.
insert() never sets next_review_date, so this is also what schedules *new*
memories. Making it a one-time migration would silently stop the review system
from ever picking up anything inserted later — hence the tests below pin the
scheduling behaviour, not just the write.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pinky_memory.store import ReflectionStore
from pinky_memory.types import Reflection, ReflectionType


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


def _backfill_writes(seen: list[str]) -> list[str]:
    return [
        s
        for s in seen
        if s.strip().upper().startswith("UPDATE REFLECTIONS") and "next_review_date" in s
    ]


def _scheduled(db: Path, reflection_id: str) -> str | None:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT next_review_date FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def _seed(db: Path, **kwargs) -> str:
    store = ReflectionStore(str(db))
    try:
        r = store.insert(
            Reflection(
                type=ReflectionType.fact,
                content="il daemon riavvia il gateway",
                **kwargs,
            )
        )
        return r.id
    finally:
        store.close()


class TestBackfillOnlyWritesWhenNeeded:
    def test_reopening_a_fully_scheduled_db_writes_nothing(self, tmp_path, monkeypatch):
        db = tmp_path / "memory.db"
        _seed(db)
        ReflectionStore(str(db)).close()  # this open does the backfill

        seen = _trace_opens(monkeypatch)
        ReflectionStore(str(db)).close()

        writes = _backfill_writes(seen)
        assert writes == [], f"reopen still takes a write lock to backfill nothing: {writes}"

    def test_an_empty_db_writes_nothing(self, tmp_path, monkeypatch):
        db = tmp_path / "memory.db"
        ReflectionStore(str(db)).close()

        seen = _trace_opens(monkeypatch)
        ReflectionStore(str(db)).close()

        assert _backfill_writes(seen) == []


class TestBackfillStillSchedules:
    def test_a_memory_inserted_later_is_scheduled_on_the_next_open(self, tmp_path):
        """Load-bearing: insert() leaves next_review_date NULL, this fills it in."""
        db = tmp_path / "memory.db"
        rid = _seed(db)
        assert _scheduled(db, rid) is None, "insert() unexpectedly schedules the review itself"

        ReflectionStore(str(db)).close()

        assert _scheduled(db, rid) is not None, "a new memory never became reviewable"

    def test_protected_high_salience_memories_stay_unscheduled(self, tmp_path):
        db = tmp_path / "memory.db"
        rid = _seed(db, salience=5)

        ReflectionStore(str(db)).close()

        assert _scheduled(db, rid) is None, "a protected memory was pulled into the review cycle"
