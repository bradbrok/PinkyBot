"""Connect recovery and inbound outage regressions for #1203 (T1–T8).

Real worker threads pin the wedged-executor case. Every blocked fake has a
teardown release; test deadlines must fail without hanging interpreter exit.
"""

from __future__ import annotations

import asyncio
import socket
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pinky_daemon import pollers
from pinky_outreach.discord import DiscordAdapter, DiscordError
from pinky_outreach.telegram import TelegramAdapter, TelegramError
from tests.test_poller_watchdog import FakeBroker, FakeHandler, FakeHardStuckAdapter, _fake_msg


class ConnectAdapter(FakeHardStuckAdapter):
    def __init__(self, failures=()):
        super().__init__()
        self.failures = list(failures)
        self.connect_calls = 0
        self.probe_timeouts = []
        self.poll_error = None
        self.connected_at = None
        self.monotonic = time.monotonic

    def get_me(self, *, http_timeout=None):
        self.connect_calls += 1
        self.probe_timeouts.append(http_timeout)
        if self.failures:
            raise self.failures.pop(0)
        self.connected_at = self.monotonic()
        return {"username": "retry_test_bot"}

    def get_updates(self, **kwargs):
        self._calls += 1
        if self.poll_error:
            raise self.poll_error
        if not self._delivered:
            self._delivered = True
            return [_fake_msg()]
        return []


@pytest.fixture(params=["legacy", "broker"])
def make_poller(request):
    def make(adapter, **kwargs):
        # Inject the notifier after construction so base RED exercises behavior;
        # T9 separately pins production constructor wiring on both API paths.
        notify = kwargs.pop("owner_notify", None)
        if request.param == "legacy":
            sink = FakeHandler()
            poller = pollers.TelegramPoller(adapter, sink, **kwargs)
        else:
            sink = FakeBroker()
            poller = pollers.BrokerTelegramPoller(adapter, "retry-test", sink, **kwargs)
        poller._backoff_for = lambda attempt: 0.01
        poller._owner_notify = notify
        poller._poll_interval = 0.005
        return poller, sink

    return make


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class InboundClock:
    """Advance outage age explicitly without changing asyncio's real deadlines.

    Sleep arrivals are loop-side checkpoints after failure/notification handling.
    Merely observing a worker or notifier call does not provide that ordering.
    """

    def __init__(self, monkeypatch):
        self.now = time.monotonic()
        self.sleeps = 0
        self.real_sleep = pollers._sleep_until_stopped
        monkeypatch.setattr(pollers, "time", self)
        monkeypatch.setattr(pollers, "_sleep_until_stopped", self.sleep)

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    async def sleep(self, event, delay):
        self.sleeps += 1
        await self.real_sleep(event, min(delay, 0.01))

    async def cycles(self, count=10):
        target = self.sleeps + count
        await until(lambda: self.sleeps >= target)


async def finish(poller, adapter, task):
    poller.stop()
    adapter.release()
    # Retrieve base-version failures too, without replacing the useful assertion.
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


async def test_connect_retries_transient_then_enters_poll_loop(make_poller, capsys):
    adapter = ConnectAdapter(
        [
            TimeoutError("probe"),
            httpx.ConnectError("boom"),
            socket.gaierror("dns"),
            TelegramError("Bad Gateway", 502),
        ]
    )
    poller, sink = make_poller(adapter)
    poller._backoff_for = lambda n: 0.01 * 2 ** (n - 1)
    task = asyncio.create_task(poller.start())
    try:
        await asyncio.wait_for(sink.delivered.wait(), 5)
        assert adapter.connect_calls == 5
        assert adapter.probe_timeouts == [10.0] * 5
        assert poller.connect_attempts == 0
        assert poller.is_running
        assert sink.received[0].content == "hello"
        log = capsys.readouterr().err
        for n, delay in enumerate([0.01, 0.02, 0.04, 0.08], 1):
            assert f"connect attempt {n} failed" in log
            assert f"retrying in {delay:g}s" in log
        assert "connected as @retry_test_bot (attempt 5, after " in log
        print(log)  # captured T1 excerpt for the PR receipt
    finally:
        await finish(poller, adapter, task)


async def test_stop_during_backoff_exits_promptly(make_poller, monkeypatch):
    adapter = ConnectAdapter([httpx.ConnectError("offline")])
    poller, _ = make_poller(adapter)
    poller._backoff_for = lambda _: 60
    backoff_entered = asyncio.Event()
    original_sleep = pollers._sleep_until_stopped

    async def observe_backoff(event, delay):
        backoff_entered.set()
        await original_sleep(event, delay)

    monkeypatch.setattr(pollers, "_sleep_until_stopped", observe_backoff)
    task = asyncio.create_task(poller.start())
    try:
        await asyncio.wait_for(backoff_entered.wait(), 5)
        assert not task.done(), "must remain alive in backoff before stop"
        poller.stop()
        await asyncio.wait_for(task, 1)
        assert adapter.connect_calls == 1
        assert not poller.is_running
    finally:
        await finish(poller, adapter, task)


async def test_outer_deadline_recycles_executor_so_next_attempt_runs(make_poller, monkeypatch):
    class WedgedConnect(ConnectAdapter):
        def get_me(self, *, http_timeout=None):
            self.connect_calls += 1
            if self.connect_calls == 1:
                self._release.wait()  # recycle deliberately cannot free this
            return {"username": "retry_test_bot"}

    monkeypatch.setattr(pollers, "_CONNECT_OUTER_DEADLINE", 0.1, raising=False)
    adapter = WedgedConnect()
    poller, sink = make_poller(adapter)
    original_executor = poller._poll_executor
    task = asyncio.create_task(poller.start())
    try:
        await asyncio.wait_for(sink.delivered.wait(), 5)
        assert not adapter._release.is_set()
        assert adapter.connect_calls >= 2
        assert adapter.recycle_calls >= 1
        assert poller._poll_executor is not original_executor
        assert poller.watchdog_fires >= 1
    finally:
        await finish(poller, adapter, task)


@pytest.mark.parametrize("code", [401, 404])
async def test_terminal_credential_error_alerts_and_stops(make_poller, code):
    notify = MagicMock(return_value=True)
    adapter = ConnectAdapter([TelegramError("Unauthorized", code)])
    poller, _ = make_poller(adapter, owner_notify=notify)
    task = asyncio.create_task(poller.start())
    try:
        await asyncio.wait_for(task, 5)
        assert adapter.connect_calls == 1
        assert not poller.is_running
        notify.assert_called_once()
        name, message = notify.call_args.args
        assert name in message
        assert "bad token" in message and "not retrying" in message
    finally:
        await finish(poller, adapter, task)


@pytest.mark.parametrize(
    ("kind", "code", "terminal"),
    [
        *[(kind, code, False) for kind in ("legacy", "broker") for code in (409, 429, 500, 503)],
        *[(kind, code, True) for kind in ("legacy", "broker") for code in (401, 404)],
        *[("discord", code, False) for code in (404, 409, 429, 500, 503)],
        ("discord", 401, True),
    ],
)
async def test_connect_only_credential_errors_are_terminal(kind, code, terminal, capsys):
    """Conflict, plain 429, and server errors must not become bad-token exits."""
    error_type = DiscordError if kind == "discord" else TelegramError
    adapter = ConnectAdapter([error_type("connect failure", code)])
    notify = MagicMock(return_value=True)
    if kind == "legacy":
        poller = pollers.TelegramPoller(adapter, FakeHandler(), owner_notify=notify)
    elif kind == "broker":
        poller = pollers.BrokerTelegramPoller(
            adapter,
            "retry-test",
            FakeBroker(),
            owner_notify=notify,
        )
    else:
        # Keep using the connect fake; empty channel responses prove that a
        # retried identity probe reaches the real Discord polling loop.
        adapter.discover_text_channels = lambda: ["test-channel"]
        adapter.get_messages = lambda *args, **kwargs: []
        poller = pollers.BrokerDiscordPoller(
            adapter,
            "retry-test",
            FakeBroker(),
            owner_notify=notify,
        )
    poller._backoff_for = lambda _: 0.01
    task = asyncio.create_task(poller.start())
    try:
        if terminal:
            await asyncio.wait_for(task, 5)
            assert adapter.connect_calls == 1
            assert not poller.is_running
            assert poller.poll_count == 0
            notify.assert_called_once()
            assert "bad token" in notify.call_args.args[1]
        else:
            await until(lambda: poller.poll_count > 0)
            assert adapter.connect_calls == 2
            assert poller.is_running and not task.done()
            notify.assert_not_called()
            assert "terminal:" not in capsys.readouterr().err
    finally:
        await finish(poller, adapter, task)


async def test_stall_alert_fires_once_per_outage_and_resets(make_poller, monkeypatch, capsys):
    interval = 300
    monkeypatch.setattr(pollers, "_INBOUND_STALL_ALERT_AFTER", interval)
    clock = InboundClock(monkeypatch)
    notify = AsyncMock(return_value=True)
    adapter = ConnectAdapter([httpx.ConnectError("offline")] * 1000)
    adapter.monotonic = clock.monotonic
    poller, _ = make_poller(adapter, owner_notify=notify)
    poller._backoff_for = lambda _: 1
    task = asyncio.create_task(poller.start())
    started = clock.monotonic()
    try:
        await clock.cycles()
        notify.assert_not_called()
        clock.advance(interval)
        await until(lambda: poller.stall_alerted)
        assert clock.monotonic() - started >= interval
        clock.advance(5 * interval)
        await clock.cycles()
        assert notify.call_count == 1
        assert poller.stall_alerted
        assert poller.connect_attempts > 1
        adapter.poll_error = TelegramError("Bad Gateway", 502)
        adapter.failures.clear()
        await until(
            lambda: poller._last_inbound_ok is not None
            and adapter.connected_at is not None
            and poller._last_inbound_ok >= adapter.connected_at
        )
        assert poller._last_inbound_ok >= adapter.connected_at
        assert not poller.stall_alerted
        assert poller.connect_attempts == 0
        await clock.cycles()
        assert notify.call_count == 1
        clock.advance(interval)
        await until(lambda: poller.stall_alerted)
        clock.advance(5 * interval)
        await clock.cycles()
        assert notify.call_count == 2
        assert "inbound recovered after " in capsys.readouterr().err
    finally:
        await finish(poller, adapter, task)


@pytest.mark.parametrize("failure", ["api", "transport", "watchdog"])
async def test_poll_loop_error_stall_alerts_keyed_on_last_success(
    make_poller,
    monkeypatch,
    failure,
):
    interval = 300
    monkeypatch.setattr(pollers, "_INBOUND_STALL_ALERT_AFTER", interval)
    clock = InboundClock(monkeypatch)
    notify = AsyncMock(return_value=True)

    class PollFailure(ConnectAdapter):
        def get_updates(self, **kwargs):
            if failure == "watchdog":
                self._release.wait()
                return []
            if failure == "api":
                raise TelegramError("Bad Gateway", 502)
            raise httpx.ConnectError("poll offline")

    adapter = PollFailure()
    adapter.monotonic = clock.monotonic
    poller, _ = make_poller(adapter, owner_notify=notify, poll_timeout=0, watchdog_grace=0.02)
    task = asyncio.create_task(poller.start())
    try:
        await clock.cycles()
        notify.assert_not_called()
        clock.advance(interval)
        await until(lambda: poller.stall_alerted)
        assert poller._last_inbound_ok >= adapter.connected_at
        assert poller.inbound_stalled_s >= interval
        clock.advance(5 * interval)
        await clock.cycles()
        assert notify.call_count == 1
        assert poller.last_poll_ok == 0
        if failure == "watchdog":
            assert poller.watchdog_fires >= 2
    finally:
        await finish(poller, adapter, task)


@pytest.mark.parametrize("failure", ["raise", "hang", "false"])
async def test_owner_notify_failure_never_breaks_retry(make_poller, monkeypatch, failure, capsys):
    monkeypatch.setattr(pollers, "_INBOUND_STALL_ALERT_AFTER", 0, raising=False)
    monkeypatch.setattr(pollers, "_OWNER_NOTIFY_TIMEOUT", 0.5, raising=False)
    calls = []

    async def notify(*args):
        calls.append(args)
        if failure == "raise":
            raise RuntimeError("notification transport down")
        if failure == "hang":
            await asyncio.Event().wait()
        return False

    adapter = ConnectAdapter([httpx.ConnectError("offline")])
    poller, sink = make_poller(adapter, owner_notify=notify)
    poller._backoff_for = lambda _: 0.1
    task = asyncio.create_task(poller.start())
    try:
        await asyncio.wait_for(sink.delivered.wait(), 5)
        assert len(calls) == 1
        assert adapter.connect_calls == 2
        assert "owner-notify failed" in capsys.readouterr().err
    finally:
        await finish(poller, adapter, task)


@pytest.mark.parametrize("platform", ["telegram", "discord"])
@pytest.mark.parametrize("http_timeout", [10.0, None])
def test_connect_probe_uses_per_request_timeout_below_outer_deadline(platform, http_timeout):
    """Two address-family attempts (10s each) plus 5s margin fit inside 30s.

    getUpdates.timeout remains a JSON API parameter, never an HTTP budget.
    Omitting the per-request budget must preserve the client's default.
    """
    adapter = TelegramAdapter("test-token") if platform == "telegram" else DiscordAdapter("test")
    adapter._client.close()
    adapter._client = MagicMock()
    try:
        call = adapter._client.post if platform == "telegram" else adapter._client.request
        call.return_value.status_code = 200
        call.return_value.json.return_value = (
            {"ok": True, "result": {"username": "test"}}
            if platform == "telegram"
            else {"username": "test"}
        )
        assert adapter.get_me(http_timeout=http_timeout)["username"] == "test"
        if http_timeout is None:
            assert "timeout" not in call.call_args.kwargs
        else:
            assert call.call_args.kwargs["timeout"] == 10.0
        if platform == "telegram":
            call.return_value.json.return_value = {"ok": True, "result": []}
            adapter.get_updates(timeout=30)
            assert call.call_args.kwargs["json"]["timeout"] == 30
            assert "timeout" not in call.call_args.kwargs
        assert 2 * pollers._CONNECT_PROBE_TIMEOUT + 5 <= pollers._CONNECT_OUTER_DEADLINE
    finally:
        adapter.close()


def test_connect_backoff_is_capped_and_unbounded(make_poller):
    adapter = ConnectAdapter()
    poller, _ = make_poller(adapter)
    try:
        del poller._backoff_for  # exercise the production schedule
        assert [poller._backoff_for(n) for n in range(1, 9)] == [2, 4, 8, 16, 32, 60, 60, 60]
        assert poller._backoff_for(100000) == 60
    finally:
        poller.stop()
        adapter.release()


async def test_failed_alert_delivery_is_retried_until_confirmed(make_poller, monkeypatch):
    """A failed owner route must not mark an undelivered alert as delivered."""
    monkeypatch.setattr(pollers, "_INBOUND_STALL_ALERT_AFTER", 0)
    notify = MagicMock(side_effect=[False, True])
    adapter = ConnectAdapter([httpx.ConnectError("offline")] * 4)
    poller, sink = make_poller(adapter, owner_notify=notify)
    poller._backoff_for = lambda _: 0.2
    task = asyncio.create_task(poller.start())
    try:
        await asyncio.wait_for(sink.delivered.wait(), 5)
        assert notify.call_count == 2
    finally:
        await finish(poller, adapter, task)


@pytest.mark.parametrize("kind", ["legacy", "broker", "discord"])
@pytest.mark.parametrize("notification", ["hang", "false"])
async def test_hanging_owner_notify_respects_cooldown_and_connect_cadence(
    kind,
    notification,
    monkeypatch,
):
    """T7: an outbound outage must not lengthen every inbound retry cycle."""
    interval = 300
    delay = 0.1
    clock = InboundClock(monkeypatch)
    monkeypatch.setattr(pollers, "_INBOUND_STALL_ALERT_AFTER", interval)
    monkeypatch.setattr(pollers, "_OWNER_NOTIFY_TIMEOUT", 5)
    attempts = []
    cancellations = []

    async def notify(*_):
        attempts.append(clock.monotonic())
        started = time.monotonic()
        try:
            if notification == "false":
                return False
            await asyncio.Event().wait()
        finally:
            elapsed = time.monotonic() - started
            cancellations.append(elapsed)
            # Charge the actual notification wait to the injected clock too.
            # Ordinary scheduler delays cannot expire the outage cooldown.
            clock.advance(elapsed)

    adapter = ConnectAdapter([httpx.ConnectError("offline")] * 1000)
    if kind == "legacy":
        poller = pollers.TelegramPoller(adapter, FakeHandler(), owner_notify=notify)
    elif kind == "broker":
        poller = pollers.BrokerTelegramPoller(
            adapter, "retry-test", FakeBroker(), owner_notify=notify
        )
    else:
        poller = pollers.BrokerDiscordPoller(
            adapter, "retry-test", FakeBroker(), owner_notify=notify
        )
    poller._backoff_for = lambda _: delay
    post_alert_sleeps = []
    completed_calls = []

    async def observe_remaining_backoff(event, remaining):
        clock.sleeps += 1
        completed_calls.append(adapter.connect_calls)
        if len(attempts) == 1:
            post_alert_sleeps.append(remaining)
        await clock.real_sleep(event, remaining)

    monkeypatch.setattr(pollers, "_sleep_until_stopped", observe_remaining_backoff)
    task = asyncio.create_task(poller.start())
    try:
        await clock.cycles(3)
        assert not attempts
        clock.advance(interval)
        # Allow both the preceding backoff and the notification budget, each
        # with 5x slack; the separate notifier timeout is still much longer.
        await until(lambda: bool(post_alert_sleeps), timeout=5 * 2 * delay)
        first_connect_count = completed_calls[-1]
        completed = clock.sleeps
        # Three completed retries, each with 5x real scheduling slack. The
        # unbounded notifier would still be hung at its separate 5s timeout.
        await until(lambda: clock.sleeps >= completed + 3, timeout=5 * 3 * delay)
        assert completed_calls[-1] >= first_connect_count + 3
        assert len(attempts) == 1, "failed notification must honor the outage cooldown"
        assert cancellations and cancellations[0] < 5 * delay
        if notification == "hang":
            assert post_alert_sleeps[0] < delay / 4, "notify wait consumes the backoff budget"
        clock.advance(interval)
        await until(lambda: len(attempts) >= 2)
        assert attempts[1] - attempts[0] >= interval
    finally:
        await finish(poller, adapter, task)
