"""Tests for the SQLite-backed activity store."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from pinky_daemon.activity_store import ActivityStore


class TestActivityStoreConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        store = ActivityStore(db_path=str(tmp_path / "activity.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        store.log(
            "shared-agent",
            "shared-event",
            "Shared point-read seed",
            metadata={"kind": "shared-seed", "nested": {"valid": True}},
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
                ).fetchone()[0].lower() == "truncate"
                assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
                agent_name = f"worker-{worker_index}"
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    created = store.log(
                        agent_name,
                        "hammer",
                        f"Hammer event {marker}",
                        metadata={"marker": marker, "values": [worker_index, round_index]},
                    )
                    own = store.list(limit=1, agent_name=agent_name)
                    assert own[0]["id"] == created["id"]
                    assert own[0]["metadata"] == {
                        "marker": marker,
                        "values": [worker_index, round_index],
                    }
                    for _ in range(point_reads_per_round):
                        shared = store.list(
                            limit=1,
                            agent_name="shared-agent",
                            event_type="shared-event",
                        )
                        assert shared[0]["title"] == "Shared point-read seed"
                        assert shared[0]["metadata"] == {
                            "kind": "shared-seed",
                            "nested": {"valid": True},
                        }
                        point_reads += 1
                    snapshots.append((created, own[0]))
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
            assert store.get_stats()["total"] == worker_count * rounds + 1
        finally:
            store.close()
