"""Tests for agent hooks and their SQLite-backed audit store."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from pinky_daemon.hooks import AuditStore


class TestAuditStoreConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        store = AuditStore(db_path=str(tmp_path / "audit.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        shared = store.log(
            "shared-event",
            agent_name="shared-agent",
            session_id="shared-session",
            tool_name="shared-tool",
            tool_input_summary='{"kind": "shared-seed", "values": [1, 2]}',
        )

        def hammer(worker_index):
            point_reads = 0
            snapshots = []
            try:
                start.wait(timeout=10)
                connection = store._db
                connection_id = id(connection)
                assert connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0].lower() == "wal"
                assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    created = store.log(
                        "hammer",
                        agent_name=f"worker-{worker_index}",
                        session_id=marker,
                        tool_name=f"hammer-tool-{marker}",
                        tool_input_summary=f'{{"marker": "{marker}"}}',
                    )
                    own = store.get_log(session_id=marker, event="hammer", limit=1)
                    assert own[0].id == created.id
                    assert own[0].tool_name == f"hammer-tool-{marker}"
                    for _ in range(point_reads_per_round):
                        point_read = store.get_log(
                            agent_name="shared-agent",
                            session_id="shared-session",
                            event="shared-event",
                            limit=1,
                        )
                        assert point_read[0].id == shared.id
                        assert point_read[0].tool_name == "shared-tool"
                        assert point_read[0].tool_input_summary == (
                            '{"kind": "shared-seed", "values": [1, 2]}'
                        )
                        point_reads += 1
                    snapshots.append((created.to_dict(), own[0].to_dict()))
                return connection_id, snapshots, point_reads, None
            except Exception as exc:
                return None, snapshots, point_reads, exc
            finally:
                store.close()

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(hammer, range(worker_count)))

            errors = [error for _, _, _, error in results if error is not None]
            database_errors = [
                error
                for error in errors
                if isinstance(error, sqlite3.DatabaseError)
                or "malformed" in str(error).lower()
            ]
            assert database_errors == []
            assert errors == []

            connection_ids = [connection_id for connection_id, _, _, _ in results]
            assert len(set(connection_ids)) == worker_count
            assert sum(point_reads for _, _, point_reads, _ in results) == (
                worker_count * rounds * point_reads_per_round
            )
            assert all(len(snapshots) == rounds for _, snapshots, _, _ in results)
            assert len(store.get_log(limit=worker_count * rounds + 1)) == (
                worker_count * rounds + 1
            )
        finally:
            store.close()
