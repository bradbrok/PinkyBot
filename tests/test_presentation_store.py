"""Tests for presentation store - versioning and listing."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pinky_daemon.presentation_store import PresentationStore


@pytest.fixture
def store(tmp_path):
    return PresentationStore(str(tmp_path / "presentations.db"))


class TestPresentationStoreConcurrency:
    def test_point_read_hammer_uses_thread_local_connections(self, tmp_path):
        presentation_store = PresentationStore(str(tmp_path / "presentations.db"))
        worker_count = 12
        rounds = 25
        point_reads_per_round = 8
        start = threading.Barrier(worker_count)
        shared = presentation_store.create(
            "Shared presentation seed",
            "<main>Shared point-read content</main>",
            created_by="shared-agent",
            tags=["shared", "point-read"],
        )

        def hammer(worker_index):
            point_reads = 0
            snapshots = []
            try:
                start.wait(timeout=10)
                connection = presentation_store._db
                connection_id = id(connection)
                assert connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0].lower() == "truncate"
                assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                for round_index in range(rounds):
                    marker = f"{worker_index}-{round_index}"
                    created = presentation_store.create(
                        f"Hammer Deck {marker}",
                        f"<main>hammer-{marker}</main>",
                        created_by=f"worker-{worker_index}",
                        tags=["hammer", marker],
                    )
                    own = presentation_store.get_with_content(created.id)
                    assert own is not None
                    assert own.title == f"Hammer Deck {marker}"
                    assert own.tags == ["hammer", marker]
                    assert own.current_html == f"<main>hammer-{marker}</main>"
                    for _ in range(point_reads_per_round):
                        point_read = presentation_store.get_with_content(shared.id)
                        assert point_read is not None
                        assert point_read.title == "Shared presentation seed"
                        assert point_read.tags == ["shared", "point-read"]
                        assert point_read.current_html == (
                            "<main>Shared point-read content</main>"
                        )
                        point_reads += 1
                    snapshots.append((created.to_dict(), own.to_dict()))
                return connection_id, snapshots, point_reads, None
            except Exception as exc:
                return None, snapshots, point_reads, exc
            finally:
                presentation_store.close()

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
            assert presentation_store.get_stats()["total"] == (
                worker_count * rounds + 1
            )
        finally:
            presentation_store.close()


class TestVersioning:
    def test_update_increments_version(self, store):
        pres = store.create("Deck", "<p>v1</p>")
        assert pres.current_version == 1
        updated = store.update(pres.id, "<p>v2</p>")
        assert updated.current_version == 2

    def test_restore_version_rewinds(self, store):
        pres = store.create("Deck", "<p>v1</p>")
        store.update(pres.id, "<p>v2</p>")
        restored = store.restore_version(pres.id, 1)
        assert restored.current_version == 1
        assert restored.current_html == "<p>v1</p>"

    def test_update_after_restore_does_not_collide(self, store):
        pres = store.create("Deck", "<p>v1</p>")
        store.update(pres.id, "<p>v2</p>")
        store.update(pres.id, "<p>v3</p>")
        restored = store.restore_version(pres.id, 2)
        assert restored.current_version == 2

        updated = store.update(pres.id, "<p>v4</p>")
        assert updated is not None
        assert updated.current_version == 4
        assert updated.current_html == "<p>v4</p>"

        # Subsequent edits keep working
        again = store.update(pres.id, "<p>v5</p>")
        assert again.current_version == 5


class TestListTagFilter:
    def test_tag_filter_applies_before_limit(self, store):
        # Tagged presentation is the oldest (lowest updated_at), so it falls
        # outside the LIMIT window unless the tag filter runs in SQL.
        tagged = store.create("Tagged", "<p>t</p>", tags=["demo"])
        for i in range(3):
            store.create(f"Other {i}", "<p>o</p>")
        results = store.list(tag="demo", limit=2)
        assert [p.id for p in results] == [tagged.id]

    def test_tag_filter_no_match(self, store):
        store.create("Plain", "<p>p</p>")
        assert store.list(tag="missing") == []

    def test_list_without_tag_unaffected(self, store):
        store.create("A", "<p>a</p>")
        store.create("B", "<p>b</p>")
        assert len(store.list()) == 2
