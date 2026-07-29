"""Tests for the SQLite-backed user profile store."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from pinky_daemon.user_profile_store import ProfileEntry, UserProfileStore


class TestUserProfileStoreConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        store = UserProfileStore(db_path=str(tmp_path / "user-profiles.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        shared_entry = store.upsert(
            ProfileEntry(
                chat_id="shared-chat",
                category="preferences",
                key="response_style",
                value="Concise, with examples",
                confidence=0.9,
                source="manual",
            )
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
                chat_id = f"worker-{worker_index}"
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    created = store.upsert(
                        ProfileEntry(
                            chat_id=chat_id,
                            category="patterns",
                            key=f"hammer-{round_index}",
                            value=marker,
                            confidence=0.75,
                            source="dream",
                        )
                    )
                    own = store.get(created.id)
                    assert own is not None
                    assert own.chat_id == chat_id
                    assert own.value == marker
                    for _ in range(point_reads_per_round):
                        shared = store.get(shared_entry.id)
                        assert shared is not None
                        assert shared.chat_id == "shared-chat"
                        assert shared.value == "Concise, with examples"
                        assert shared.confidence == 0.9
                        assert shared.source == "manual"
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
            assert store.stats() == {
                "total_users": worker_count + 1,
                "total_entries": worker_count * rounds + 1,
            }
        finally:
            store.close()
