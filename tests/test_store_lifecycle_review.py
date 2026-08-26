"""Adversarial review probes for Lane A shutdown centralization."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon.pollers import TelegramPoller
from pinky_daemon.store_authority import assert_no_open_store_descriptors
from pinky_daemon.store_catalog import StoreCatalog, StoreIntegrityTarget


def _target(
    logical_name: str,
    path: Path,
    criticality: str,
    *,
    journal_mode: str = "wal",
) -> StoreIntegrityTarget:
    return StoreIntegrityTarget(
        logical_name=logical_name,
        path=os.fspath(path),
        criticality=criticality,
        journal_mode=journal_mode,
    )


def test_review_owner_close_after_shutdown_snapshot_is_already_finalized(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """An owner close racing after the snapshot must not create a false failure."""
    path = tmp_path / "owner-close.db"
    manifest = {"delivery": _target("delivery", path, "delivery")}
    catalog = StoreCatalog(
        expected_root=tmp_path,
        silence_allowlist={},
        manifest=manifest,
    )
    connection = catalog.open_connection("delivery", path, owner="owner")
    mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
    connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
    connection.commit()
    catalog.register("delivery", path, journal_mode=mode, owner="owner")

    authority = catalog._connection_authority
    authority_type = type(authority)
    original_finalize = authority_type._finalize
    finalizer_entered = threading.Event()
    release_finalizer = threading.Event()

    def delayed_finalize(handle) -> None:
        finalizer_entered.set()
        assert release_finalizer.wait(timeout=2)
        original_finalize(handle)

    monkeypatch.setattr(authority_type, "_finalize", staticmethod(delayed_finalize))
    outcome: dict[str, object] = {}

    def run_shutdown() -> None:
        try:
            outcome["report"] = catalog.shutdown(deadline_seconds=2)
        except BaseException as exc:
            outcome["error"] = exc

    shutdown_thread = threading.Thread(target=run_shutdown)
    shutdown_thread.start()
    assert finalizer_entered.wait(timeout=2)
    connection.close()
    release_finalizer.set()
    shutdown_thread.join(timeout=2)

    assert not shutdown_thread.is_alive()
    assert "error" not in outcome
    assert outcome["report"].finalized == ("delivery",)


def test_review_shared_wal_file_tolerates_one_finalizer_per_handle(tmp_path: Path) -> None:
    """Two logical handles on one WAL file may both checkpoint and close."""
    path = tmp_path / "sessions.db"
    manifest = {
        "sessions": _target("sessions", path, "delivery"),
        "session_events": _target("session_events", path, "telemetry"),
    }
    catalog = StoreCatalog(
        expected_root=tmp_path,
        silence_allowlist={},
        manifest=manifest,
    )
    for logical_name in manifest:
        connection = catalog.open_connection(logical_name, path, owner="shared-owner")
        mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        connection.execute("CREATE TABLE IF NOT EXISTS payload (value TEXT NOT NULL)")
        connection.commit()
        catalog.register(logical_name, path, journal_mode=mode, owner="shared-owner")

    report = catalog.shutdown(deadline_seconds=2)

    assert report.ok
    assert report.finalized == ("session_events", "sessions")
    assert_no_open_store_descriptors([path])
    assert not Path(os.fspath(path) + "-wal").exists()
    assert not Path(os.fspath(path) + "-shm").exists()


def test_review_authority_open_applies_the_declared_busy_timeout(tmp_path: Path) -> None:
    """The authority seam itself must establish its manifest connection policy."""
    path = tmp_path / "tasks.db"
    manifest = {"tasks": _target("tasks", path, "memory")}
    catalog = StoreCatalog(
        expected_root=tmp_path,
        silence_allowlist={},
        manifest=manifest,
    )

    connection = catalog.open_connection("tasks", path, owner="review-owner")
    try:
        row = connection.execute("PRAGMA busy_timeout").fetchone()
        assert row == (30_000,)
    finally:
        connection.close()


def test_review_shutdown_commits_an_explicit_inflight_transaction(tmp_path: Path) -> None:
    """Pin the dispatch spec's final-commit behavior, not merely journal cleanup."""
    path = tmp_path / "commit.db"
    manifest = {
        "delivery": _target("delivery", path, "delivery", journal_mode="delete")
    }
    catalog = StoreCatalog(
        expected_root=tmp_path,
        silence_allowlist={},
        manifest=manifest,
    )
    connection = catalog.open_connection("delivery", path, owner="owner")
    mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
    connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
    connection.commit()
    catalog.register("delivery", path, journal_mode=mode, owner="owner")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO payload(value) VALUES ('inflight')")

    catalog.shutdown(deadline_seconds=2)

    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT value FROM payload").fetchall() == [("inflight",)]


@pytest.mark.asyncio
async def test_review_poller_stop_is_a_delivery_publication_barrier() -> None:
    """A poll completing after stop must not publish a new writer coroutine."""
    poll_entered = threading.Event()
    release_poll = threading.Event()
    delivered = asyncio.Event()

    class Adapter:
        @staticmethod
        def get_me():
            return {"username": "review"}

        @staticmethod
        def get_updates(*, timeout: int):
            assert timeout == 30
            poll_entered.set()
            assert release_poll.wait(timeout=2)
            return [
                SimpleNamespace(
                    chat_id="chat",
                    sender="sender",
                    metadata={},
                    content="late",
                    timestamp=1.0,
                    message_id="message",
                )
            ]

    class Handler:
        @staticmethod
        async def handle(_message) -> None:
            delivered.set()

    poller = TelegramPoller(Adapter(), Handler(), poll_interval=0)
    poller_task = asyncio.create_task(poller.start())
    assert await asyncio.to_thread(poll_entered.wait, 2)

    poller.stop()
    release_poll.set()
    await asyncio.wait_for(poller_task, timeout=2)
    await asyncio.sleep(0)

    assert not delivered.is_set()
