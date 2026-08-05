"""The skills DB must run in rollback (TRUNCATE) journal mode, never WAL.

Counterpart to ``test_agents_db_no_wal.py`` (#797/#220). The agents DB was
moved off WAL and survived an incident where the ``-wal`` sidecars of the
daemon's open DBs were unlinked under the live process; the skills DB was still
on WAL and lost every agent_skills row that had not been checkpointed — the API
kept reporting the assignments as written. Rollback mode keeps committed data in
the main DB file and maps no ``-shm``. These tests pin the contract.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from pinky_daemon.skill_store import (
    SkillDbConfigError,
    SkillStore,
    _configure_skills_db_connection,
)


def _journal_mode(db_path: str) -> str:
    c = sqlite3.connect(db_path)
    try:
        return str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        c.close()


def test_new_store_is_truncate_and_creates_no_shm():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "skills.db")
        store = SkillStore(db_path=path)
        store.register(name="alpha", description="Alpha")
        assert store.get("alpha") is not None
        assert store.assign_to_agent("engineer", "alpha", assigned_by="user")
        assert "alpha" in [s["name"] for s in store.get_agent_skills("engineer")]
        assert store._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
        assert _journal_mode(path) != "wal"
        assert not os.path.exists(path + "-shm"), "skills DB must not create a -shm (WAL) file"


def test_existing_wal_db_is_converted_in_place():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "skills.db")
        # seed an existing WAL-mode DB with a live -wal/-shm pair
        seed = sqlite3.connect(path)
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("CREATE TABLE warmup(x)")
        seed.execute("INSERT INTO warmup VALUES (1)")
        seed.commit()
        assert os.path.exists(path + "-shm")  # WAL mode created the wal-index
        seed.close()
        # store init must convert it and keep working
        store = SkillStore(db_path=path)
        store.register(name="beta", description="Beta")
        assert store.get("beta") is not None
        assert store._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
        # the conversion is persistent: fresh opens aren't WAL, -shm gone
        assert _journal_mode(path) != "wal"
        assert not os.path.exists(path + "-shm")


def test_assignment_is_visible_without_wal_sidecar():
    """The incident invariant: a committed assignment survives losing any
    sidecar journal, because in rollback mode it lives in the main DB file."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "skills.db")
        store = SkillStore(db_path=path)
        store.register(name="hydra", description="Hydra")
        assert store.assign_to_agent("hydra-manager", "hydra", assigned_by="user")
        for sidecar in (path + "-wal", path + "-shm"):
            assert not os.path.exists(sidecar)
        # an independent reader (a fresh process would see exactly this) sees it
        c = sqlite3.connect(path)
        try:
            rows = c.execute(
                "SELECT skill_name FROM agent_skills WHERE agent_name=?", ("hydra-manager",)
            ).fetchall()
        finally:
            c.close()
        assert [r[0] for r in rows] == ["hydra"]


class _FakeConn:
    """Minimal connection stub: journal_mode=TRUNCATE returns the scripted
    sequence of effective modes; everything else is a no-op."""

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


def test_configure_fails_loud_when_it_cannot_leave_wal():
    # A persistent writer / lock: PRAGMA journal_mode=TRUNCATE keeps reporting
    # 'wal'. The helper must retry then RAISE, never silently stay on WAL.
    conn = _FakeConn(truncate_results=["wal", "wal", "wal"])
    with pytest.raises(SkillDbConfigError):
        _configure_skills_db_connection(conn, retries=3, busy_ms=10)


def test_configure_retries_then_succeeds():
    # Busy once (reports 'wal'), then the lock clears and it reports 'truncate'.
    conn = _FakeConn(truncate_results=["wal", "truncate"])
    assert _configure_skills_db_connection(conn, retries=4, busy_ms=10) == "truncate"
