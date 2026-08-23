"""Tests for the Telegram poller watchdog (#1145).

The 2026-08-23 incident: a network blip left every Telegram poller's
getUpdates long-poll blocked in ssl.read indefinitely — the httpx
client-level read timeout demonstrably did not fire — and the stuck polls
consumed the event loop's default executor, starving unrelated daemon work.

These tests pin the three-part recovery contract:
  * a poll that exceeds its hard deadline fires the watchdog instead of
    hanging the poller forever;
  * the watchdog recycles the adapter's HTTP client (which is what frees
    the stuck read in production) and replaces the poll thread;
  * polling then CONTINUES and delivers messages — the poller survives.

The delivery assertions are the non-vacuity proof: with the watchdog
removed, the first stuck poll never returns and the test times out RED.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from pinky_daemon.pollers import BrokerTelegramPoller, TelegramPoller
from pinky_outreach.telegram import TelegramAdapter


def _fake_msg(content: str = "hello") -> SimpleNamespace:
    return SimpleNamespace(
        chat_id="12345",
        sender="tester",
        content=content,
        timestamp=time.time(),
        message_id="m1",
        reply_to="",
        metadata={},
    )


class FakeStuckAdapter:
    """First poll blocks forever (the dead-connection read); after recycle()
    the next poll returns one message, then empties.

    ``recycle()`` releases the blocked thread — mirroring production, where
    closing the httpx client's sockets is what frees the stuck read. It also
    keeps the test process from hanging on a non-daemon executor thread.
    """

    def __init__(self) -> None:
        self._release = threading.Event()
        self._calls = 0
        self.recycle_calls = 0
        self._delivered = False

    def get_me(self) -> dict:
        return {"username": "watchdog_test_bot"}

    def get_updates(self, *, timeout: int = 0, limit: int = 100):
        self._calls += 1
        if self._calls == 1:
            self._release.wait()  # dead connection: never returns on its own
            return []
        if not self._delivered:
            self._delivered = True
            return [_fake_msg()]
        return []

    def recycle(self) -> None:
        self.recycle_calls += 1
        self._release.set()

    def close(self) -> None:
        self._release.set()


class FakeHardStuckAdapter:
    """First poll wedges somewhere recycle() cannot reach (the DNS-resolution
    case from the watchdog docstring): ``recycle()`` counts the call but does
    NOT free the thread. Delivery must still resume — that is the
    executor-replacement leg, and nothing else covers it.

    ``release()`` is a test-teardown-only handle so the wedged non-daemon
    thread doesn't hang the test process at exit.
    """

    def __init__(self) -> None:
        self._release = threading.Event()
        self._calls = 0
        self.recycle_calls = 0
        self._delivered = False

    def get_me(self) -> dict:
        return {"username": "hardstuck_test_bot"}

    def get_updates(self, *, timeout: int = 0, limit: int = 100):
        self._calls += 1
        if self._calls == 1:
            self._release.wait()  # recycle() will NOT free this
            return []
        if not self._delivered:
            self._delivered = True
            return [_fake_msg("post-stuck")]
        return []

    def recycle(self) -> None:
        self.recycle_calls += 1  # deliberately does NOT release the thread

    def release(self) -> None:
        self._release.set()

    def close(self) -> None:
        self._release.set()


class FakeLateCompletionAdapter:
    """First poll blocks until ``release()``, then returns a REAL message —
    the genuinely-late-completion leg. Telegram has already advanced the
    offset for that batch, so dropping it would be permanent loss; the
    watchdog must hand it to ``_process_messages`` when it finally lands.

    ``recycle()`` does not free the thread, so the test controls exactly
    when the abandoned poll completes.
    """

    def __init__(self) -> None:
        self._release = threading.Event()
        self._calls = 0
        self.recycle_calls = 0

    def get_me(self) -> dict:
        return {"username": "late_test_bot"}

    def get_updates(self, *, timeout: int = 0, limit: int = 100):
        self._calls += 1
        if self._calls == 1:
            self._release.wait()
            return [_fake_msg("late-hello")]
        return []

    def recycle(self) -> None:
        self.recycle_calls += 1

    def release(self) -> None:
        self._release.set()

    def close(self) -> None:
        self._release.set()


class FakeHandler:
    def __init__(self) -> None:
        self.received: list = []
        self.delivered = asyncio.Event()

    async def handle(self, inbound) -> None:
        self.received.append(inbound)
        self.delivered.set()


class FakeBroker:
    def __init__(self) -> None:
        self.received: list = []
        self.delivered = asyncio.Event()

    async def handle_inbound(self, msg) -> None:
        self.received.append(msg)
        self.delivered.set()


class TestWatchdogRecyclesStuckPoll:
    @pytest.mark.asyncio
    async def test_poller_survives_stuck_poll_and_delivers(self, capsys):
        adapter = FakeStuckAdapter()
        handler = FakeHandler()
        poller = TelegramPoller(
            adapter,
            handler,
            poll_timeout=0,
            poll_interval=0.01,
            watchdog_grace=0.3,
        )
        task = asyncio.create_task(poller.start())
        try:
            await asyncio.wait_for(handler.delivered.wait(), timeout=10)
            # Captured BEFORE teardown's own recycle() — this pins that the
            # WATCHDOG recycled the client, not the finally block below.
            recycles_at_delivery = adapter.recycle_calls
        finally:
            adapter.recycle()  # belt-and-suspenders: never leave the thread stuck
            poller.stop()
            await asyncio.wait_for(task, timeout=5)

        assert poller.watchdog_fires == 1
        assert recycles_at_delivery >= 1
        assert handler.received[0].content == "hello"
        assert poller.last_poll_ok > 0.0
        err = capsys.readouterr().err
        assert "WATCHDOG" in err
        assert "recycling HTTP client" in err

    @pytest.mark.asyncio
    async def test_broker_poller_survives_stuck_poll(self):
        adapter = FakeStuckAdapter()
        broker = FakeBroker()
        poller = BrokerTelegramPoller(
            adapter,
            "watchdog-test-agent",
            broker,
            poll_timeout=0,
            poll_interval=0.01,
            watchdog_grace=0.3,
        )
        task = asyncio.create_task(poller.start())
        try:
            await asyncio.wait_for(broker.delivered.wait(), timeout=10)
            recycles_at_delivery = adapter.recycle_calls
        finally:
            adapter.recycle()
            poller.stop()
            await asyncio.wait_for(task, timeout=5)

        assert poller.watchdog_fires == 1
        assert recycles_at_delivery >= 1
        assert broker.received[0].content == "hello"

    @pytest.mark.asyncio
    async def test_delivery_resumes_even_when_recycle_cannot_free_thread(self):
        """Executor-replacement leg: when the recycle can't free the wedged
        thread (DNS-style wedge), polling must continue on a fresh thread
        anyway. With executor replacement removed this times out RED —
        the replacement is the only thing keeping the poller alive here."""
        adapter = FakeHardStuckAdapter()
        handler = FakeHandler()
        poller = TelegramPoller(
            adapter,
            handler,
            poll_timeout=0,
            poll_interval=0.01,
            watchdog_grace=0.3,
        )
        task = asyncio.create_task(poller.start())
        try:
            await asyncio.wait_for(handler.delivered.wait(), timeout=10)
            fires_at_delivery = poller.watchdog_fires
            recycles_at_delivery = adapter.recycle_calls
        finally:
            adapter.release()  # free the wedged non-daemon thread for exit
            poller.stop()
            await asyncio.wait_for(task, timeout=5)

        assert fires_at_delivery == 1
        assert recycles_at_delivery >= 1
        assert handler.received[0].content == "post-stuck"

    @pytest.mark.asyncio
    async def test_late_completing_abandoned_poll_is_delivered_not_lost(self):
        """An abandoned poll that later completes WITH updates must deliver
        them (#1146 must-fix): getUpdates already confirmed the offset
        server-side, so a drop here is silent, permanent message loss."""
        adapter = FakeLateCompletionAdapter()
        handler = FakeHandler()
        poller = TelegramPoller(
            adapter,
            handler,
            poll_timeout=0,
            poll_interval=0.01,
            watchdog_grace=0.3,
        )
        task = asyncio.create_task(poller.start())
        try:
            deadline = time.monotonic() + 10
            while poller.watchdog_fires == 0:
                assert time.monotonic() < deadline, "watchdog never fired"
                await asyncio.sleep(0.01)
            # Poll #1 is now abandoned. Complete it — with a message.
            adapter.release()
            await asyncio.wait_for(handler.delivered.wait(), timeout=10)
        finally:
            adapter.release()
            poller.stop()
            await asyncio.wait_for(task, timeout=5)

        assert handler.received[0].content == "late-hello"
        assert poller.watchdog_fires == 1

    @pytest.mark.asyncio
    async def test_healthy_poll_never_fires_watchdog(self):
        adapter = FakeStuckAdapter()
        adapter._calls = 1  # skip the stuck first call entirely
        handler = FakeHandler()
        poller = TelegramPoller(
            adapter,
            handler,
            poll_timeout=0,
            poll_interval=0.01,
            watchdog_grace=0.3,
        )
        task = asyncio.create_task(poller.start())
        try:
            await asyncio.wait_for(handler.delivered.wait(), timeout=10)
        finally:
            poller.stop()
            await asyncio.wait_for(task, timeout=5)

        assert poller.watchdog_fires == 0
        assert adapter.recycle_calls == 0


class TestAdapterRecycle:
    def test_recycle_preserves_update_offset_and_swaps_client(self):
        adapter = TelegramAdapter("test-token", timeout=7.0)
        adapter._last_update_id = 42
        old_client = adapter._client

        adapter.recycle()

        assert adapter._last_update_id == 42, "offset loss would replay updates"
        assert adapter._client is not old_client
        assert old_client.is_closed
        assert not adapter._client.is_closed
        adapter.close()


class TestReadTimeoutFiresOnBlackhole:
    def test_get_updates_raises_instead_of_hanging(self):
        """The 'provably fires' regression from #1145: against a connection
        that accepts and never responds, the adapter-level read timeout must
        raise — a hang here is exactly the incident signature."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)  # never accept()ed: handshake completes, reads starve
        port = srv.getsockname()[1]
        try:
            adapter = TelegramAdapter("x", timeout=1.0)
            adapter._base = f"http://127.0.0.1:{port}/botx"
            t0 = time.monotonic()
            with pytest.raises(httpx.TimeoutException):
                adapter.get_updates(timeout=0)
            elapsed = time.monotonic() - t0
            assert elapsed < 6, f"read timeout took {elapsed:.1f}s to fire"
            adapter.close()
        finally:
            srv.close()
