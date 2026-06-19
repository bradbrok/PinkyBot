"""#797/#220 — agents DB must run in rollback (TRUNCATE) journal mode, never WAL.

The WAL wal-index (``-shm``) is always memory-mapped in WAL mode. Under the
daemon's long-lived registry connection plus the per-request read-only
signing-key resolver churn, that mapped ``-shm`` page went stale and a SQLite
pager read SIGBUS'd the daemon (``si_addr`` confirmed inside
``conversations_agents.db-shm``). Rollback journal mode has no ``-shm`` at all,
so the daemon never maps it. These tests pin the contract.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from pinky_daemon.agent_registry import (
    AgentDbConfigError,
    AgentRegistry,
    _configure_agents_db_connection,
)
from pinky_daemon.auth import make_db_signing_key_resolver


def _journal_mode(db_path: str) -> str:
    c = sqlite3.connect(db_path)
    try:
        return str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        c.close()


def test_new_registry_is_truncate_and_creates_no_shm():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "agents.db")
        reg = AgentRegistry(db_path=path)
        # ordinary register/get/update
        reg.register(name="alpha", display_name="Alpha")
        assert reg.get("alpha") is not None
        reg.register(name="alpha", display_name="Alpha Updated")
        # the registry's OWN connection is in rollback (TRUNCATE) mode; a fresh
        # connection defaults to "delete" (also rollback) — both are non-WAL.
        assert reg._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
        assert _journal_mode(path) != "wal"
        # the real SIGBUS-relevant invariant: never WAL, so no wal-index (-shm)
        assert not os.path.exists(path + "-shm"), "agents DB must not create a -shm (WAL) file"


def test_existing_wal_db_is_converted_in_place():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "agents.db")
        # seed an existing WAL-mode DB that has a live -wal/-shm pair
        seed = sqlite3.connect(path)
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("CREATE TABLE warmup(x)")
        seed.execute("INSERT INTO warmup VALUES (1)")
        seed.commit()
        assert os.path.exists(path + "-shm")  # WAL mode created the wal-index
        seed.close()
        # registry init must convert it and keep working
        reg = AgentRegistry(db_path=path)
        reg.register(name="beta")
        assert reg.get("beta") is not None
        assert reg._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
        # the prior WAL was left persistently: fresh opens aren't WAL, -shm gone
        assert _journal_mode(path) != "wal"
        assert not os.path.exists(path + "-shm")


def test_resolver_resolves_current_key_in_truncate_mode():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "agents.db")
        reg = AgentRegistry(db_path=path)
        reg.register(name="gamma")
        key1 = reg.get_or_create_signing_key("gamma")
        resolver = make_db_signing_key_resolver(path)
        assert resolver("gamma") == key1
        # "rotation": the resolver must read the CURRENT DB value, not a stale one (#641)
        reg._db.execute(
            "INSERT OR REPLACE INTO agent_signing_keys (agent_name, signing_key, created_at) "
            "VALUES (?, ?, ?)",
            ("gamma", "rotated-key-v2", 0),
        )
        reg._db.commit()
        assert resolver("gamma") == "rotated-key-v2"
        # fail-soft: unknown agent / bad name -> None (global-secret fallback)
        assert resolver("does-not-exist") is None
        assert resolver("BAD NAME!") is None
        # still rollback (registry conn = truncate), DB non-WAL, no -shm
        assert reg._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
        assert _journal_mode(path) != "wal"
        assert not os.path.exists(path + "-shm")


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
    with pytest.raises(AgentDbConfigError):
        _configure_agents_db_connection(conn, retries=3, busy_ms=10)


def test_configure_retries_then_succeeds():
    # Busy once (reports 'wal'), then the lock clears and it reports 'truncate'.
    conn = _FakeConn(truncate_results=["wal", "truncate"])
    assert _configure_agents_db_connection(conn, retries=4, busy_ms=10) == "truncate"
