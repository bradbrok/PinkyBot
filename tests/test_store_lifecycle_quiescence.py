"""Shutdown-quiescence regressions supplementing the review probes."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import pollers
from pinky_daemon.store_catalog import StoreCatalog, StoreIntegrityTarget
from pinky_daemon.store_shutdown import StoreShutdownError


@pytest.mark.asyncio
async def test_stop_promptly_unblocks_an_inflight_telegram_poll() -> None:
    """Stopping must recycle the poll client instead of awaiting its long-poll deadline."""
    poll_entered = threading.Event()
    poll_released = threading.Event()
    recycle_calls = 0

    class Adapter:
        @staticmethod
        def get_me():
            return {"username": "quiescence"}

        @staticmethod
        def get_updates(*, timeout: int):
            assert timeout == 30
            poll_entered.set()
            poll_released.wait()
            raise pollers.TelegramError("poll client recycled")

        @staticmethod
        def recycle() -> None:
            nonlocal recycle_calls
            recycle_calls += 1
            poll_released.set()

    poller = pollers.TelegramPoller(
        Adapter(),
        object(),
        poll_interval=30,
    )
    poller_task = pollers.start_poller(poller)
    assert await asyncio.to_thread(poll_entered.wait, 1)

    poller.stop()
    try:
        await asyncio.wait_for(asyncio.shield(poller_task), timeout=0.5)
    finally:
        poll_released.set()
        for _ in range(3):
            await asyncio.sleep(0)
        if not poller_task.done():
            poller_task.cancel()
        await asyncio.gather(poller_task, return_exceptions=True)

    assert recycle_calls == 1


@pytest.mark.asyncio
async def test_quiescence_drains_an_already_fetched_telegram_batch() -> None:
    """Stopping ingress must not strand the remainder of an offset-advanced batch."""
    first_callback_entered = asyncio.Event()
    release_first_callback = asyncio.Event()
    finalizer_entered = asyncio.Event()
    delivered: list[str] = []
    callback_count = 0

    messages = [
        SimpleNamespace(
            chat_id="chat",
            sender="sender",
            metadata={},
            content=content,
            timestamp=1.0,
            message_id=content,
        )
        for content in ("first", "second")
    ]

    class Adapter:
        @staticmethod
        def get_me():
            return {"username": "quiescence"}

        @staticmethod
        def get_updates(*, timeout: int):
            assert timeout == 30
            return messages

    class Handler:
        @staticmethod
        async def handle(message) -> None:
            delivered.append(message.content)

    async def event_callback(**_event) -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count == 1:
            first_callback_entered.set()
            await release_first_callback.wait()

    poller = pollers.TelegramPoller(
        Adapter(),
        Handler(),
        poll_interval=0,
        event_callback=event_callback,
    )
    start_poller = getattr(
        pollers,
        "start_poller",
        lambda current: asyncio.create_task(current.start()),
    )
    poller_task = start_poller(poller)
    await first_callback_entered.wait()
    poller.stop()

    async def quiesce_then_finalize() -> None:
        await pollers.quiesce_delivery_tasks()
        finalizer_entered.set()

    shutdown_task = asyncio.create_task(quiesce_then_finalize())
    await asyncio.sleep(0)
    finalized_before_batch_drain = finalizer_entered.is_set()

    release_first_callback.set()
    await asyncio.wait_for(poller_task, timeout=2)
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert not finalized_before_batch_drain
    assert delivered == ["first", "second"]
    assert callback_count == 2


def test_abandoned_telegram_batch_drop_after_stop_is_loud(capsys) -> None:
    """An unsafe late watchdog result is dropped with platform and count."""

    class Adapter:
        @staticmethod
        def get_me():
            return {"username": "quiescence"}

    poller = pollers.TelegramPoller(Adapter(), object())
    poller.stop()
    abandoned = Future()
    abandoned.set_result([object(), object()])

    poller._on_abandoned_poll_done(abandoned)

    stderr = capsys.readouterr().err
    assert "abandoned poll" in stderr
    assert "platform=telegram" in stderr
    assert "dropping 2" in stderr


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
