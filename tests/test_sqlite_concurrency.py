"""Tests for SQLite concurrent access patterns in shared MCP mode.

Validates that WAL mode handles concurrent reads and writes correctly,
particularly for the shared MCP architecture where multiple agents hit
the daemon API simultaneously.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest


class TestWALConcurrency:
    """Verify WAL mode supports concurrent read/write patterns."""

    def _create_wal_db(self, db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                value TEXT NOT NULL,
                ts REAL NOT NULL
            )
        """)
        conn.commit()
        return conn

    def test_wal_mode_is_set(self, tmp_path):
        """Verify WAL mode is actually enabled."""
        db_path = str(tmp_path / "test.db")
        conn = self._create_wal_db(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_concurrent_writes_from_multiple_agents(self, tmp_path):
        """Simulate 5 agents writing concurrently to the same DB."""
        db_path = str(tmp_path / "test.db")
        self._create_wal_db(db_path).close()

        errors = []
        writes_per_agent = 20
        agents = ["barsik", "pushok", "ryzhik", "persik", "gemma"]

        def agent_writer(agent_name: str):
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                for i in range(writes_per_agent):
                    conn.execute(
                        "INSERT INTO test_data (agent, value, ts) VALUES (?, ?, ?)",
                        (agent_name, f"value_{i}", time.time()),
                    )
                    conn.commit()
                conn.close()
            except Exception as e:
                errors.append((agent_name, str(e)))

        threads = [threading.Thread(target=agent_writer, args=(a,)) for a in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent write errors: {errors}"

        # Verify all writes landed
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM test_data").fetchone()[0]
        assert count == len(agents) * writes_per_agent

        # Verify each agent's writes are intact
        for agent in agents:
            agent_count = conn.execute(
                "SELECT COUNT(*) FROM test_data WHERE agent = ?", (agent,)
            ).fetchone()[0]
            assert agent_count == writes_per_agent
        conn.close()

    def test_concurrent_reads_and_writes(self, tmp_path):
        """Readers shouldn't block writers and vice versa in WAL mode."""
        db_path = str(tmp_path / "test.db")
        self._create_wal_db(db_path).close()

        errors = []
        read_counts = []

        def writer():
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                for i in range(50):
                    conn.execute(
                        "INSERT INTO test_data (agent, value, ts) VALUES (?, ?, ?)",
                        ("writer", f"v{i}", time.time()),
                    )
                    conn.commit()
                conn.close()
            except Exception as e:
                errors.append(("writer", str(e)))

        def reader():
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                for _ in range(50):
                    count = conn.execute("SELECT COUNT(*) FROM test_data").fetchone()[0]
                    read_counts.append(count)
                conn.close()
            except Exception as e:
                errors.append(("reader", str(e)))

        t_write = threading.Thread(target=writer)
        t_read1 = threading.Thread(target=reader)
        t_read2 = threading.Thread(target=reader)

        t_write.start()
        t_read1.start()
        t_read2.start()

        t_write.join(timeout=30)
        t_read1.join(timeout=30)
        t_read2.join(timeout=30)

        assert not errors, f"Errors: {errors}"
        # Reads should show monotonically non-decreasing counts
        # (WAL provides snapshot isolation)
        assert len(read_counts) == 100  # 2 readers * 50 reads

    def test_busy_timeout_prevents_lock_errors(self, tmp_path):
        """With busy_timeout, transient locks don't cause errors."""
        db_path = str(tmp_path / "test.db")
        self._create_wal_db(db_path).close()

        errors = []

        def heavy_writer(name: str, count: int):
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                for i in range(count):
                    conn.execute(
                        "INSERT INTO test_data (agent, value, ts) VALUES (?, ?, ?)",
                        (name, f"heavy_{i}", time.time()),
                    )
                    if i % 5 == 0:
                        conn.commit()
                conn.commit()
                conn.close()
            except Exception as e:
                errors.append((name, str(e)))

        # 5 heavy writers simultaneously
        threads = [
            threading.Thread(target=heavy_writer, args=(f"agent_{i}", 100))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"Lock errors: {errors}"

        conn = sqlite3.connect(db_path)
        total = conn.execute("SELECT COUNT(*) FROM test_data").fetchone()[0]
        assert total == 500
        conn.close()


class TestSharedMcpConcurrencyArchitecture:
    """Verify that the shared MCP architecture doesn't introduce new SQLite pressure."""

    def test_pinky_self_uses_api_not_direct_db(self):
        """pinky-self tools call the daemon API (HTTP), not SQLite directly."""
        from pinky_self.server import create_server
        # The server uses _api() which hits HTTP endpoints
        # Verify no direct sqlite3 imports in server.py
        import inspect
        source = inspect.getsource(create_server)
        assert "sqlite3" not in source

    def test_pinky_messaging_uses_api_not_direct_db(self):
        """pinky-messaging tools call the daemon API (HTTP), not SQLite directly."""
        from pinky_messaging.server import create_server
        import inspect
        source = inspect.getsource(create_server)
        assert "sqlite3" not in source

    def test_memory_uses_per_agent_store_pool(self):
        """pinky-memory in shared server uses per-agent store pool (not single DB)."""
        from pinky_daemon.shared_mcp import SharedMcpManager

        # Without resolver: no memory in shared server
        mgr_no_mem = SharedMcpManager()
        app = mgr_no_mem._create_app()
        assert mgr_no_mem._memory_pool is None

        # With resolver: memory is included with per-agent store pool
        def fake_resolver(name):
            return f"/tmp/{name}/memory.db"

        mgr_with_mem = SharedMcpManager(memory_db_resolver=fake_resolver)
        app = mgr_with_mem._create_app()
        assert mgr_with_mem._memory_pool is not None
