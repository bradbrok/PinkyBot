"""Tests for the SQLite-backed research store."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from pinky_daemon.research_store import ResearchStore


class TestResearchStoreConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        store = ResearchStore(db_path=str(tmp_path / "research.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        shared = store.create_topic(
            "Shared research seed",
            submitted_by="shared-agent",
            tags=["shared", "point-read"],
            scope="Shared row parsed by every worker",
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
                assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    created = store.create_topic(
                        f"Hammer topic {marker}",
                        submitted_by=f"worker-{worker_index}",
                        tags=["hammer", marker],
                        scope=f"scope-{marker}",
                    )
                    own = store.get_topic(created.id)
                    assert own is not None
                    assert own.title == f"Hammer topic {marker}"
                    assert own.tags == ["hammer", marker]
                    for _ in range(point_reads_per_round):
                        point_read = store.get_topic(shared.id)
                        assert point_read is not None
                        assert point_read.title == "Shared research seed"
                        assert point_read.tags == ["shared", "point-read"]
                        assert point_read.scope == "Shared row parsed by every worker"
                        point_reads += 1
                    snapshots.append((created.to_dict(), own.to_dict()))
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
            assert len(
                store.list_topics(
                    include_cancelled=True,
                    limit=worker_count * rounds + 1,
                )
            ) == worker_count * rounds + 1
        finally:
            store.close()
