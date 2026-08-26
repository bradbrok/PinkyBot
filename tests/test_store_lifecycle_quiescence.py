"""Shutdown-quiescence regressions supplementing the review probes."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from pinky_daemon import pollers
from pinky_daemon.store_catalog import StoreCatalog, StoreIntegrityTarget
from pinky_daemon.store_shutdown import StoreShutdownError


@pytest.mark.asyncio
async def test_delivery_quiescence_waits_for_a_mid_write_handler() -> None:
    """Store finalization cannot begin while an admitted delivery writer is active."""
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()
    writer_exited = asyncio.Event()
    finalizer_entered = asyncio.Event()

    async def writer() -> None:
        writer_entered.set()
        await release_writer.wait()
        writer_exited.set()

    delivery = pollers._deliver_in_background(writer(), "review mid-write")
    await writer_entered.wait()
    quiesce = getattr(pollers, "quiesce_delivery_tasks", None)
    if quiesce is None:
        release_writer.set()
        await delivery
        pytest.fail("pollers.quiesce_delivery_tasks is required")

    async def quiesce_then_finalize() -> None:
        await quiesce()
        assert writer_exited.is_set()
        finalizer_entered.set()

    shutdown_task = asyncio.create_task(quiesce_then_finalize())
    await asyncio.sleep(0)
    assert not finalizer_entered.is_set()

    release_writer.set()
    await shutdown_task

    assert finalizer_entered.is_set()
    assert delivery.done()
    assert not pollers._DELIVERY_TASKS


@pytest.mark.parametrize("failure_stage", ["commit", "close"])
def test_real_finalizer_failure_remains_loud(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    """Only the precise already-closed end state may be treated as success."""
    path = tmp_path / f"real-{failure_stage}-failure.db"
    manifest = {
        "delivery": StoreIntegrityTarget(
            logical_name="delivery",
            path=str(path),
            criticality="delivery",
            journal_mode="delete",
        )
    }
    catalog = StoreCatalog(
        expected_root=tmp_path,
        silence_allowlist={},
        manifest=manifest,
    )
    connection = catalog.open_connection("delivery", path, owner="review-owner")
    connection.execute("CREATE TABLE payload (value TEXT NOT NULL)")
    connection.commit()
    catalog.register("delivery", path, journal_mode="delete", owner="review-owner")

    connection_type = type(connection)
    original_close = connection_type.close

    def fail(_connection) -> None:
        raise sqlite3.OperationalError(f"forced real {failure_stage} failure")

    monkeypatch.setattr(connection_type, failure_stage, fail)
    try:
        with pytest.raises(StoreShutdownError, match=f"forced real {failure_stage} failure"):
            catalog.shutdown(deadline_seconds=2)
    finally:
        if failure_stage == "close":
            original_close(connection)
