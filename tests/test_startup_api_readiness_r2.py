"""Refusal, shutdown, and replay observations at the listener boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import api, api_readiness
from pinky_daemon import broker as broker_module
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api_readiness import ApiReadiness
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.buzz_inbound import BrokerBuzzPoller
from pinky_daemon.transport_state import SessionState
from tests import test_startup_api_readiness as boot_harness
from tests.test_startup_api_readiness import _run_boot
from tests.test_startup_api_readiness_r1 import POLLERS, _configure_pollers


@pytest.fixture
def boot_run(tmp_path, monkeypatch):
    return boot_harness.boot_run.__wrapped__(tmp_path, monkeypatch)


async def _until(predicate):
    for _ in range(128):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate(), "Expected lifecycle transition did not occur within 128 loop steps"


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuild", (False, True))
@pytest.mark.parametrize("session_state", (None, SessionState.IDLE_SLEEPING, SessionState.RECONNECTING))
async def test_unready_route_never_cold_starts_reconnects_or_waits(
    tmp_path, monkeypatch, session_state, rebuild,
):
    registry = AgentRegistry(tmp_path / "agents.db")
    gate = ApiReadiness()
    gate.attach(SimpleNamespace(started=False, should_exit=False))
    broker = MessageBroker(registry, None, api_readiness=gate)
    ensure = AsyncMock(return_value=None)
    broker.set_ensure_session_callback(ensure)
    connect = AsyncMock()
    session = None if session_state is None else SimpleNamespace(
        state=session_state, resume_handle="saved-session", connect=connect,
        _config=SimpleNamespace(api_readiness=gate),
    )
    monkeypatch.setattr(broker, "_get_streaming_session", Mock(return_value=session))
    monkeypatch.setattr(broker, "_send_message", AsyncMock())
    broker._compatible_delivery = AsyncMock()
    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "1" if rebuild else "0")
    clock = SimpleNamespace(now=100.0)

    async def reconnect_wait(delay):
        # A deleted early guard must fail an assertion, never spin on a fixed clock.
        clock.now += broker_module._INBOUND_RECONNECT_WAIT_SEC + 1

    sleep = AsyncMock(side_effect=reconnect_wait)
    monkeypatch.setattr(broker_module, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(broker_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    message = BrokerMessage(
        agent_name="primary", platform="web", chat_id="sample-chat",
        sender_name="sender", sender_id="sample-sender", content="held message",
    )
    try:
        assert await broker._route_streaming("primary", message) is False
        ensure.assert_not_awaited()
        connect.assert_not_awaited()
        sleep.assert_not_awaited()
        assert clock.now == 100.0
        assert broker.stats["routed"] == 0
    finally:
        await gate.close()
        registry.close()


@pytest.mark.parametrize("outcome", ("cancel", "raise"))
def test_shutdown_during_replay_creates_no_retry_task_or_poller_start(
    boot_run, tmp_path, monkeypatch, outcome,
):
    starts, retry = _configure_pollers(boot_run, monkeypatch, buzz=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(api, "_resume_grandfather_migration", AsyncMock())

    async def reconcile():
        entered.set()
        await release.wait()
        raise RuntimeError("replay interrupted by shutdown")

    monkeypatch.setattr(boot_run.app.state.broker, "reconcile_approved_pending_messages", reconcile)
    created = []

    def start_retry():
        task = asyncio.create_task(release.wait(), name="probe-approval-retry")
        created.append(task)
        return task

    retry.side_effect = start_retry

    async def shutdown_replay():
        gate = boot_run.app.state.api_readiness
        await _until(entered.is_set)
        assert not gate._after_ready_task.done()
        gate._close_now("shutdown during replay")
        if outcome == "cancel":
            await gate.close()
        else:
            release.set()
            result = await asyncio.gather(gate._after_ready_task, return_exceptions=True)
            assert isinstance(result[0], RuntimeError)
        await boot_run.checkpoint()

    boot_run.after_ready_hook = shutdown_replay
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert starts == [], "A closed generation started an inbound poller"
    retry.assert_not_called()
    assert created == [], "A closed generation created an approval-retry task"
    assert getattr(boot_run.app.state, "approval_notification_retry_task", None) is None
    assert boot_run.surviving_tasks == []


def test_policy_edit_never_resurrects_the_retired_queued_buzz_poller(
    boot_run, tmp_path, monkeypatch,
):
    _configure_pollers(boot_run, monkeypatch, buzz=True)
    created = []
    started = []
    original_init = BrokerBuzzPoller.__init__

    def record_init(poller, *args, **kwargs):
        original_init(poller, *args, **kwargs)
        created.append(poller)

    async def record_start(poller):
        started.append(poller)

    monkeypatch.setattr(BrokerBuzzPoller, "__init__", record_init)
    monkeypatch.setattr(BrokerBuzzPoller, "start", record_start)
    monkeypatch.setattr(api, "verify_session_cookie", Mock(return_value={"user": "owner"}))
    policy = {"community_id": "sample", "channels": ["sample"], "principals": ["sample"]}
    configure = Mock(return_value=policy)
    monkeypatch.setattr(
        boot_run.app.state.agents, "configure_buzz_inbound_owner_control", configure,
    )
    stopped = []

    async def edit_policy_before_ready():
        assert len(created) == 1 and started == []
        retired = created[0]
        stop = Mock(wraps=retired.stop)
        monkeypatch.setattr(retired, "stop", stop)
        endpoint = next(
            route.endpoint for route in boot_run.app.routes
            if getattr(route, "path", "") == "/system/buzz-identities/{name}/inbound"
            and "PUT" in getattr(route, "methods", ())
        )
        request = SimpleNamespace(
            owner_pubkey="a" * 64, channels=[], approved_users=[],
        )
        result = await endpoint("primary", request, SimpleNamespace(cookies={}))
        assert result["poller_started"] is True
        configure.assert_called_once_with(
            "primary", owner_pubkey="a" * 64, channels=[], approved_users=[], owner_actor="ui:owner",
        )
        stop.assert_called_once_with()
        stopped.append(retired)
        await _until(lambda: len(started) == 1)
        assert len(created) == 2 and created[0] is not created[1]
        assert started == [created[1]]

    async def finish_replay():
        await boot_run.app.state.api_readiness._after_ready_task
        await boot_run.checkpoint()

    boot_run.before_bind_hook = edit_policy_before_ready
    boot_run.after_ready_hook = finish_replay
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert stopped == [created[0]]
    assert started == [created[1]], "Readiness resurrected the retired queued Buzz poller"
    assert boot_run.surviving_tasks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ("session-held", "shutdown-during-lookup"))
async def test_live_session_refusal_skips_typing_context_and_transcription(
    tmp_path, monkeypatch, condition,
):
    registry = AgentRegistry(tmp_path / "agents.db")
    gate = ApiReadiness()
    server = SimpleNamespace(started=condition == "shutdown-during-lookup", should_exit=False)
    gate.attach(server)
    typing = AsyncMock()
    context_store = Mock()
    broker = MessageBroker(
        registry, None, typing_callback=typing, message_context_store=context_store,
        api_readiness=gate if condition == "shutdown-during-lookup" else None,
    )
    session = SimpleNamespace(
        state=SessionState.CONNECTED, _config=SimpleNamespace(api_readiness=gate), send=AsyncMock(),
    )

    def lookup(*args):
        if condition == "shutdown-during-lookup":
            server.should_exit = True
        return session

    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "0")
    monkeypatch.setattr(broker, "_get_streaming_session", lookup)
    photos = AsyncMock()
    transcribe = AsyncMock(return_value="spoken message")
    remember = Mock(wraps=broker.remember_message_context)
    monkeypatch.setattr(broker, "_download_photo_attachments", photos)
    monkeypatch.setattr(broker, "_transcribe_voice", transcribe)
    monkeypatch.setattr(broker, "remember_message_context", remember)
    message = BrokerMessage(
        agent_name="primary", platform="web", chat_id="sample-chat", message_id="sample-message",
        sender_name="sender", sender_id="sample-sender", content="held message",
        attachments=[{"type": "voice"}],
    )
    try:
        assert await broker._route_streaming("primary", message) is False
        typing.assert_not_awaited()
        photos.assert_not_awaited()
        transcribe.assert_not_awaited()
        remember.assert_not_called()
        context_store.put.assert_not_called()
        session.send.assert_not_awaited()
        assert message.content == "held message"
        assert broker._voice_pending == {}
        assert broker.stats["routed"] == 0
    finally:
        await gate.close()
        registry.close()


@pytest.mark.asyncio
async def test_late_ready_info_excludes_refused_waiters_still_awaiting_cleanup(monkeypatch, capsys):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(api_readiness, "time", SimpleNamespace(monotonic=lambda: clock.now))
    gate = ApiReadiness()
    server = SimpleNamespace(started=False, should_exit=False)
    gate.attach(server)
    gate.started_at = clock.now
    gate.cap_seconds = 1.0
    held = [asyncio.create_task(gate.wait(source)) for source in ("boot", "replay")]
    try:
        await _until(lambda: len(gate._waiters) == 2)
        clock.now = 101.0
        gate._refresh()
        assert gate.expired and len(gate._waiters) == 2
        assert all(future.done() and future.result() is False for future in gate._waiters)
        # Observe the late bind before either wait() coroutine removes its refused future.
        server.started = True
        gate._refresh()
        gate._refresh()
        output = capsys.readouterr().err
        assert "INFO api listener ready after 1.00s; released 0 held submission(s)" in output
        assert output.count("INFO api listener ready after") == 1
        assert "WARNING api listener readiness ready after cap" in output
        assert await asyncio.gather(*held) == [False, False]
        assert await gate.wait("new-after-ready") is True
        assert gate._waiters == {}
    finally:
        await gate.close()
        for task in held:
            task.cancel()
        await asyncio.gather(*held, return_exceptions=True)


@pytest.mark.parametrize("step", ("grandfather migration", "approved pending-message reconcile"))
def test_replay_failure_surfaces_at_both_error_boundaries_and_starts_retries(
    boot_run, tmp_path, monkeypatch, capsys, step,
):
    starts, retry = _configure_pollers(boot_run, monkeypatch, buzz=True)
    error = RuntimeError("replay failed")
    if step == "grandfather migration":
        monkeypatch.setattr(api, "_resume_grandfather_migration", AsyncMock(side_effect=error))
    else:
        monkeypatch.setattr(
            boot_run.app.state.broker, "reconcile_approved_pending_messages",
            AsyncMock(side_effect=error),
        )
    results = []

    async def finish_replay():
        results.extend(await asyncio.gather(
            boot_run.app.state.api_readiness._after_ready_task, return_exceptions=True,
        ))
        await boot_run.checkpoint()

    boot_run.after_ready_hook = finish_replay
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert results == [error], "The readiness owner lost the replay failure"
    output = capsys.readouterr().err
    assert f"ERROR startup: {step} failed (RuntimeError)" in output
    assert output.count("ERROR api listener readiness deferred startup replay failed") == 1
    assert sorted(name for name, _ in starts) == sorted(
        cls.__name__ for cls in (*POLLERS, BrokerBuzzPoller)
    )
    assert all(phase == 2 for _, phase in starts)
    retry.assert_called_once_with()
    assert boot_run.surviving_tasks == []
