"""Tests for the holder-side KG ephemeral sweep (#654).

The sweep used to be an external script opening the live memory.db directly;
storage-authority (#619/#1118/#1132) drives such direct opens to zero, so the
sweep now runs INSIDE the store owner as ``ReflectionStore.kg_sweep_ephemeral``.
These tests pin the contract the script provided: same detector as the
write-guard, dry-run by default, backup-before-delete, all-or-nothing apply,
and one JSONL regrowth record per run — plus the boundary the tool adds:
log/backup paths can never escape the store's own directory.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from pinky_memory.ephemeral_guard import is_ephemeral_entity
from pinky_memory.store import ReflectionStore

EPHEMERAL_NAME = "PR #124"
EPHEMERAL_STAMP = "26.08.027"
DURABLE_NAME = "Brad"
DURABLE_NAME_2 = "SQLite"


@pytest.fixture
def store(tmp_path):
    return ReflectionStore(db_path=str(tmp_path / "test_memory.db"))


def _seed_entity(store: ReflectionStore, name: str, etype: str = "unknown") -> None:
    """Insert an entity directly — the write-guard (correctly) refuses
    ephemeral names through kg_add, so seeding must bypass it."""
    store._conn.execute(
        "INSERT INTO kg_entities (id, name, type, created_at) VALUES (?, ?, ?, ?)",
        (f"seed-{name}", name, etype, time.time()),
    )
    store._conn.commit()


def _seed_edge(store: ReflectionStore, subject: str, obj: str) -> None:
    store._conn.execute(
        "INSERT INTO kg_triples (id, subject, predicate, object, extracted_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (f"seed-{subject}-{obj}", subject, "mentions", obj, time.time()),
    )
    store._conn.commit()


def _seed_graph(store: ReflectionStore) -> None:
    """One ephemeral node with two edges, plus a durable-durable edge that a
    correct sweep must NEVER touch (guards against over-broad deletes)."""
    _seed_entity(store, EPHEMERAL_NAME)
    _seed_entity(store, DURABLE_NAME, "person")
    _seed_entity(store, DURABLE_NAME_2, "tool")
    _seed_edge(store, EPHEMERAL_NAME, DURABLE_NAME)
    _seed_edge(store, DURABLE_NAME, EPHEMERAL_NAME)
    _seed_edge(store, DURABLE_NAME, DURABLE_NAME_2)


def test_seed_names_are_what_they_claim():
    """Precondition: the seeds exercise both sides of the detector — if the
    guard patterns change under these names, fail here, not mysteriously."""
    assert is_ephemeral_entity(EPHEMERAL_NAME)
    assert is_ephemeral_entity(EPHEMERAL_STAMP)
    assert not is_ephemeral_entity(DURABLE_NAME)
    assert not is_ephemeral_entity(DURABLE_NAME_2)


class TestDryRun:
    def test_detects_but_does_not_delete(self, store):
        _seed_graph(store)
        result = store.kg_sweep_ephemeral()

        assert result["n_candidates"] == 1
        assert result["candidates"] == [EPHEMERAL_NAME]
        assert result["n_edges"] == 2
        assert result["applied"] is False
        assert result["backup"] is None
        # Nothing deleted
        n = store._conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
        assert n == 3
        n = store._conn.execute("SELECT COUNT(*) FROM kg_triples").fetchone()[0]
        assert n == 3
        # Regrowth record written, applied=false, no edge dump on dry runs
        lines = [
            json.loads(line)
            for line in open(result["log"], encoding="utf-8")
        ]
        assert lines[-1]["n_candidates"] == 1
        assert lines[-1]["applied"] is False
        assert lines[-1]["deleted_edges"] == []
        assert lines[-1]["runner"] == "store"


class TestApply:
    def test_deletes_candidates_and_their_edges_only(self, store):
        _seed_graph(store)
        result = store.kg_sweep_ephemeral(apply=True)

        assert result["applied"] is True
        assert result["n_candidates"] == 1
        names = {
            r[0] for r in store._conn.execute("SELECT name FROM kg_entities")
        }
        assert names == {DURABLE_NAME, DURABLE_NAME_2}
        # The durable-durable edge survives — an over-broad edge delete
        # (e.g. an unconditioned DELETE FROM kg_triples) must fail HERE.
        edges = store._conn.execute(
            "SELECT subject, object FROM kg_triples"
        ).fetchall()
        assert [(e[0], e[1]) for e in edges] == [(DURABLE_NAME, DURABLE_NAME_2)]

    def test_backup_is_consistent_and_pre_delete(self, store):
        _seed_graph(store)
        result = store.kg_sweep_ephemeral(apply=True)

        assert result["backup"] is not None
        bconn = sqlite3.connect(result["backup"])
        try:
            names = {r[0] for r in bconn.execute("SELECT name FROM kg_entities")}
            edges = bconn.execute("SELECT COUNT(*) FROM kg_triples").fetchone()[0]
        finally:
            bconn.close()
        # The backup captures the graph BEFORE the delete — that is what
        # makes the sweep reversible.
        assert EPHEMERAL_NAME in names
        assert edges == 3

    def test_apply_log_records_deleted_edges(self, store):
        _seed_graph(store)
        result = store.kg_sweep_ephemeral(apply=True)
        last = json.loads(
            open(result["log"], encoding="utf-8").readlines()[-1]
        )
        assert last["applied"] is True
        assert last["n_edges"] == 2
        assert {e["via"] for e in last["deleted_edges"]} == {EPHEMERAL_NAME}
        # Script-parity: candidates are bare name strings, one consumer can
        # read the whole regrowth series across both writers.
        assert last["candidates"] == [EPHEMERAL_NAME]

    def test_empty_graph_apply_is_noop_without_backup(self, store):
        result = store.kg_sweep_ephemeral(apply=True)
        assert result["n_candidates"] == 0
        assert result["applied"] is False
        assert result["backup"] is None


class _FailingConn:
    """Proxy that fails the entity delete for a specific name — simulates a
    mid-loop OperationalError so the rollback contract can be pinned."""

    def __init__(self, real: sqlite3.Connection, fail_name: str) -> None:
        self._real = real
        self._fail_name = fail_name

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        if "DELETE FROM kg_entities" in sql and self._fail_name in params:
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        return self._real.execute(sql, params)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class TestApplyRollback:
    def test_mid_loop_failure_rolls_back_everything(self, store):
        """A partial sweep left in an open transaction would be silently
        committed by the next unrelated write. Apply must be all-or-nothing."""
        _seed_graph(store)
        _seed_entity(store, EPHEMERAL_STAMP)  # second candidate: fails mid-loop
        real_conn = store._conn
        store._conn = _FailingConn(real_conn, EPHEMERAL_STAMP)
        try:
            result = store.kg_sweep_ephemeral(apply=True)
        finally:
            store._conn = real_conn

        assert result["applied"] is False
        assert "apply_error" in result
        assert not real_conn.in_transaction, "open transaction left behind"
        # EVERYTHING survives — including the candidate whose delete succeeded
        # before the failure (rolled back, not half-swept).
        names = {r[0] for r in real_conn.execute("SELECT name FROM kg_entities")}
        assert {EPHEMERAL_NAME, EPHEMERAL_STAMP} <= names
        n = real_conn.execute("SELECT COUNT(*) FROM kg_triples").fetchone()[0]
        assert n == 3
        # The failed run is still visible in the regrowth log.
        last = json.loads(open(result["log"], encoding="utf-8").readlines()[-1])
        assert last["applied"] is False
        assert "apply_error" in last


class TestPathBoundary:
    def test_log_path_outside_store_dir_is_refused(self, store, tmp_path):
        with pytest.raises(ValueError, match="log_path"):
            store.kg_sweep_ephemeral(log_path=str(tmp_path.parent / "x.jsonl"))

    def test_backup_dir_outside_store_dir_is_refused(self, store, tmp_path):
        with pytest.raises(ValueError, match="backup_dir"):
            store.kg_sweep_ephemeral(backup_dir="/tmp/elsewhere")

    def test_paths_under_store_dir_are_honored(self, store, tmp_path):
        _seed_entity(store, EPHEMERAL_STAMP)
        log = tmp_path / "custom" / "sweep.jsonl"
        result = store.kg_sweep_ephemeral(log_path=str(log))
        assert result["log"] == str(log)
        assert log.exists()
        rec = json.loads(log.read_text().splitlines()[-1])
        assert rec["candidates"] == [EPHEMERAL_STAMP]
