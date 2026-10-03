"""Listener recovery must preserve inbound delivery and supervisor contracts."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI

from pinky_daemon import __main__ as daemon_main
from pinky_daemon import api, api_readiness
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api_readiness import ApiReadiness
from pinky_daemon.broker import MessageBroker
from pinky_daemon.buzz_inbound import BrokerBuzzPoller, BuzzNostrSigner
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.ferry.host_pinky import HostPinky
from pinky_daemon.ferry.types import FerryEnvelope
from pinky_daemon.pollers import (
    BrokerDiscordPoller,
    BrokeriMessagePoller,
    BrokerSlackPoller,
    BrokerTelegramPoller,
)
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState
from pinky_daemon.wake_prompt import WakeReason
from tests import test_startup_api_readiness as boot_harness
from tests.test_startup_api_readiness import MODES, _run_boot

POLLERS = (BrokerTelegramPoller, BrokerDiscordPoller, BrokerSlackPoller, BrokeriMessagePoller)


@pytest.fixture
def boot_run(tmp_path, monkeypatch):
    return boot_harness.boot_run.__wrapped__(tmp_path, monkeypatch)


def _buzz_material():
    private_key = b"1" * 32
    return SimpleNamespace(
        agent="primary", private_key=private_key, pubkey=BuzzNostrSigner(private_key).pubkey,
        relay_url="", community_id="sample", relay_signing_pubkey="a" * 64,
    )


def _configure_pollers(run, monkeypatch, *, buzz=False):
    registry = run.app.state.agents
    monkeypatch.setattr(registry, "get_raw_token", lambda *args: "sample-token")
    monkeypatch.setattr(registry, "list_tokens", lambda *args: [
        SimpleNamespace(platform="slack", settings={"app_token": "sample-app-token"}),
    ])
    monkeypatch.setenv("PINKY_SLACK_SOCKET_MODE", "1")
    for module, name in (
        ("pinky_outreach.telegram", "TelegramAdapter"),
        ("pinky_outreach.discord", "DiscordAdapter"),
        ("pinky_outreach.slack", "SlackAdapter"),
        ("pinky_outreach.imessage", "iMessageAdapter"),
    ):
        monkeypatch.setattr(f"{module}.{name}", lambda *args, **kwargs: Mock())
    if buzz:
        monkeypatch.setattr(registry, "get_buzz_identity", lambda *args: {
            "enabled": True, "status": "active",
        })
        monkeypatch.setattr(registry, "get_buzz_inbound_policy", lambda *args: {
            "channels": ["sample"], "principals": ["sample"],
        })
        monkeypatch.setattr(registry, "get_buzz_signing_material", lambda *args: _buzz_material())
    starts = []

    async def record_start(poller):
        starts.append((type(poller).__name__, run.server.phase))

    for cls in (*POLLERS, BrokerBuzzPoller):
        monkeypatch.setattr(cls, "start", record_start)
    retry = Mock(return_value=None)
    monkeypatch.setattr(run.app.state.broker, "start_approval_notification_retries", retry)
    return starts, retry


@pytest.mark.parametrize("step", ("grandfather", "approved pending"))
def test_replay_error_starts_every_queued_poller_and_retries(
    boot_run, tmp_path, monkeypatch, capsys, step,
):
    starts, retry = _configure_pollers(boot_run, monkeypatch, buzz=True)
    if step == "grandfather":
        monkeypatch.setattr(api, "_resume_grandfather_migration", AsyncMock(
            side_effect=RuntimeError("replay failed"),
        ))
    else:
        monkeypatch.setattr(boot_run.app.state.broker, "reconcile_approved_pending_messages", AsyncMock(
            side_effect=RuntimeError("replay failed"),
        ))

    async def finish_replay():
        await asyncio.gather(boot_run.app.state.api_readiness._after_ready_task, return_exceptions=True)
        await boot_run.checkpoint()

    boot_run.after_ready_hook = finish_replay
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert sorted(name for name, _ in starts) == sorted(cls.__name__ for cls in (*POLLERS, BrokerBuzzPoller))
    assert all(phase == 2 for _, phase in starts)
    retry.assert_called_once_with()
    output = capsys.readouterr().err
    assert "ERROR" in output and step in output


def test_buzz_waits_for_the_approved_backlog_replay(
    boot_run, tmp_path, monkeypatch,
):
    starts, _ = _configure_pollers(boot_run, monkeypatch, buzz=True)
    boot_run.approved_backlog = True
    original = boot_run.app.state.broker.reconcile_approved_pending_messages
    replay_finished = False
    buzz_observations = []

    async def reconcile():
        nonlocal replay_finished
        result = await original()
        replay_finished = True
        return result

    async def buzz_start(poller):
        buzz_observations.append((replay_finished, boot_run.server.phase))

    monkeypatch.setattr(boot_run.app.state.broker, "reconcile_approved_pending_messages", reconcile)
    monkeypatch.setattr(BrokerBuzzPoller, "start", buzz_start)

    async def finish_replay():
        await boot_run.app.state.api_readiness._after_ready_task
        await boot_run.checkpoint()

    boot_run.after_ready_hook = finish_replay
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert buzz_observations == [(True, 2)], "Buzz raced the durable startup backlog"
    assert len(starts) == 4
    assert boot_run.pending_before_stop == []


def test_queued_poller_logs_do_not_claim_a_start_before_binding(
    boot_run, tmp_path, monkeypatch, capsys,
):
    starts, _ = _configure_pollers(boot_run, monkeypatch, buzz=True)
    before = []

    async def before_bind():
        before.append(capsys.readouterr().err)

    async def after_ready():
        await boot_run.app.state.api_readiness._after_ready_task
        await boot_run.checkpoint()

    boot_run.before_bind_hook = before_bind
    boot_run.after_ready_hook = after_ready
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert len(starts) == 5
    assert "poller queued" in before[0]
    assert "poller started" not in before[0]
    assert capsys.readouterr().err.count("poller started") >= 5


def _single_entry_app(tmp_path, monkeypatch, branch, app):
    from pinky_daemon.ferry.config import FerryConfig

    app.state.api_readiness = ApiReadiness()
    app.state.host_pinky = None
    monkeypatch.setattr(api, "create_api", lambda **kwargs: app)
    monkeypatch.setenv("PINKYBOT_FERRY_ENABLED", "0")
    monkeypatch.setattr(FerryConfig, "from_env", lambda: SimpleNamespace(
        enabled=branch == "fallback", bind_host="", bind_port=0, fleet_name="sample",
    ))
    return SimpleNamespace(
        host=None, port=0, working_dir=str(tmp_path), max_sessions=1,
        db_path=str(tmp_path / "test.db"),
    )


@pytest.mark.parametrize("branch", ("single", "fallback"))
def test_real_lifespan_failure_preserves_uvicorn_exit_code(tmp_path, monkeypatch, branch):
    @asynccontextmanager
    async def failed_lifespan(app):
        raise RuntimeError("lifespan failed")
        yield

    app = FastAPI(lifespan=failed_lifespan)
    args = _single_entry_app(tmp_path, monkeypatch, branch, app)
    exit_code = None
    try:
        daemon_main._run_api(args)
    except SystemExit as failure:
        exit_code = failure.code
    assert exit_code == 3


@pytest.mark.parametrize("branch", ("single", "fallback"))
@pytest.mark.parametrize("started", (False, True))
def test_single_listener_interrupt_has_no_traceback(tmp_path, monkeypatch, branch, started):
    import uvicorn

    args = _single_entry_app(tmp_path, monkeypatch, branch, FastAPI())

    def interrupted(server):
        server.started = started
        raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn.Server, "run", interrupted)
    exit_code = None
    try:
        daemon_main._run_api(args)
    except KeyboardInterrupt:
        raise AssertionError("Single-listener interrupt escaped the Uvicorn tail") from None
    except SystemExit as failure:
        exit_code = failure.code
    assert exit_code == (None if started else 3)


@pytest.mark.parametrize("mode", ("claude-tmux", "codex-tmux"))
def test_wake_marker_hashes_the_rendered_prompt(boot_run, tmp_path, monkeypatch, mode):
    markers = []

    async def stream_event(session, event):
        if event["type"] == "wake_prompt_sent":
            markers.append((event, boot_run.server.phase))

    monkeypatch.setattr(TmuxSession, "_emit_stream_event", stream_event)
    _run_boot(boot_run, tmp_path, monkeypatch, mode)
    assert len(markers) == 1
    prompt = boot_run.observed[0][1]
    marker, phase = markers[0]
    assert marker["prompt_chars"] == len(prompt)
    assert marker["prompt_hash"] == hashlib.sha256(prompt.encode()).hexdigest()[:12]
    assert phase == 2, "A deferred preview must not be reported as a submitted wake"


def test_deferred_sdk_builder_keeps_the_captured_restart_reason(
    boot_run, tmp_path, monkeypatch,
):
    original_connect = StreamingSession.connect
    rendered = []
    before = []

    async def connect(session):
        session._config.restart_reason = "context_restart"
        builder = session._config.wake_context_builder

        def record_builder(agent_name, reason):
            rendered.append((session._config.restart_reason, reason))
            return builder(agent_name, reason)

        session._config.wake_context_builder = record_builder
        await original_connect(session)

    async def before_bind():
        before.append(list(rendered))

    monkeypatch.setattr(StreamingSession, "connect", connect)
    boot_run.before_bind_hook = before_bind
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert before == [[]]
    assert rendered == [("", WakeReason.CONTEXT_RESTART)]


@pytest.mark.asyncio
async def test_ready_observation_before_cap_does_not_expire_a_healthy_server(monkeypatch):
    gate = ApiReadiness()
    gate.attach(SimpleNamespace(started=True, should_exit=False))
    gate.started_at = 10.0
    gate.cap_seconds = 1.0
    clock = SimpleNamespace(now=10.5)
    monkeypatch.setattr(api_readiness, "time", SimpleNamespace(monotonic=lambda: clock.now))
    assert await gate.wait("before cap") is True
    clock.now = 100.0
    assert await gate.wait("healthy long-running listener") is True
    assert gate.ready and not gate.expired and not gate.closed


@pytest.mark.asyncio
async def test_expired_monitor_backs_off_but_detects_late_readiness(monkeypatch):
    gate = ApiReadiness()
    server = SimpleNamespace(started=False, should_exit=False)
    gate.attach(server)
    gate.started_at = 1.0
    gate.cap_seconds = 1.0
    clock = SimpleNamespace(now=1.0)
    monkeypatch.setattr(api_readiness, "time", SimpleNamespace(monotonic=lambda: clock.now))
    sleeps = []

    async def advance(delay):
        sleeps.append((delay, gate.expired))
        if gate.expired:
            server.started = True
        else:
            clock.now = 2.0

    monkeypatch.setattr(api_readiness.asyncio, "sleep", advance)
    await gate._monitor()
    assert gate.expired and gate.ready and not gate.closed
    assert [delay for delay, expired in sleeps if expired] == [0.5]


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed", (1.0, 1.5))
async def test_first_started_observation_at_cap_refuses_old_waiters(
    monkeypatch, capsys, elapsed,
):
    gate = ApiReadiness()
    server = SimpleNamespace(started=False, should_exit=False)
    gate.attach(server)
    gate.started_at = 10.0
    gate.cap_seconds = 1.0
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(api_readiness, "time", SimpleNamespace(monotonic=lambda: clock.now))
    held = asyncio.create_task(gate.wait("held wake"))
    await asyncio.sleep(0)
    clock.now += elapsed
    server.started = True
    gate._refresh()
    assert await held is False, "Late observation released a prompt held beyond the cap"
    assert gate.expired and gate.ready and not gate.closed
    assert await gate.wait("new wake") is True
    output = capsys.readouterr().err
    assert "ERROR" in output and "held wake" in output
    assert "WARNING" in output and "ready after cap" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ("expired", "stopped"))
@pytest.mark.parametrize("path", ("approval", "reconcile", "retry", "ferry"))
async def test_refused_sdk_send_never_checkpoints_a_durable_row_or_ferry_ack(
    tmp_path, monkeypatch, capsys, condition, path,
):
    registry = AgentRegistry(str(tmp_path / "registry.db"))
    registry.register("primary", working_dir=str(tmp_path))
    registry.approve_user("primary", "sample-chat", display_name="sender")
    registry.queue_pending_message("primary", "web", "sample-chat", "sender", "held message")
    gate = ApiReadiness()
    gate.attach(SimpleNamespace(started=False, should_exit=condition == "stopped"))
    gate.started_at = 1.0
    gate.cap_seconds = 1.0
    monkeypatch.setattr(api_readiness, "time", SimpleNamespace(monotonic=lambda: 3.0))
    session = StreamingSession(StreamingSessionConfig(
        agent_name="primary", working_dir=str(tmp_path), api_readiness=gate,
    ))
    session._state_machine._state = SessionState.CONNECTED
    session._client = SimpleNamespace(query=AsyncMock())
    broker = MessageBroker(registry, Mock())
    monkeypatch.setattr(broker, "_get_streaming_session", lambda *args: session)
    result = None
    host = None
    try:
        if path == "approval":
            try:
                result = await broker.handle_approval("primary", "sample-chat")
            except RuntimeError:
                result = 0
        elif path == "reconcile":
            result = await broker.reconcile_approved_pending_messages()
        elif path == "retry":
            async def stop_after_cycle(delay):
                raise asyncio.CancelledError

            monkeypatch.setattr(asyncio, "sleep", stop_after_cycle)
            with pytest.raises(asyncio.CancelledError):
                await broker.run_approval_notification_retries()
        else:
            registry.add_peer_fleet_acl("primary", fleet="sample", agent_id="recovery@sample")
            host = HostPinky(registry=registry, broker=broker, fleet_name="sample")
            result = await host.deliver(FerryEnvelope(
                v="0.1", id="sample-envelope", from_="recovery@sample", to="primary@sample",
                ts=1, body={"kind": "message", "text": "held ferry message"},
            ))
        session._client.query.assert_not_awaited()
        assert len(registry.get_pending_messages("primary", "sample-chat")) == 1
        assert broker.stats["routed"] == 0
        if host is not None:
            assert result.status == "transient_failure"
            assert host.stats["delivered"] == 0 and host.stats["messages_routed"] == 0
        elif path != "retry":
            assert result == 0
        output = capsys.readouterr().err
        assert "ERROR" in output and "refus" in output.lower()
    finally:
        await gate.close()
        registry.close()


@pytest.mark.parametrize("kind", ("telegram", "discord", "slack", "imessage", "buzz"))
def test_stop_is_safe_before_a_real_poller_has_started(kind):
    adapter, broker, registry = Mock(), Mock(), Mock()
    if kind == "telegram":
        poller = BrokerTelegramPoller(adapter, "primary", broker, registry=registry)
    elif kind == "discord":
        poller = BrokerDiscordPoller(adapter, "primary", broker, registry=registry)
    elif kind == "slack":
        poller = BrokerSlackPoller(adapter, "primary", broker, registry=registry, app_token="sample")
    elif kind == "imessage":
        poller = BrokeriMessagePoller(adapter, "primary", broker)
    else:
        poller = BrokerBuzzPoller(_buzz_material(), broker, registry, Mock(), connect_factory=Mock())
    poller.stop()
    poller.stop()
    assert not poller._running
    assert adapter.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", (False, True))
async def test_healthy_ready_line_counts_released_waiters_once(monkeypatch, capsys, ready):
    gate = ApiReadiness()
    server = SimpleNamespace(started=False, should_exit=False)
    gate.attach(server)
    gate.started_at = 10.0
    gate.cap_seconds = 1.0
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(api_readiness, "time", SimpleNamespace(monotonic=lambda: clock.now))
    held = [asyncio.create_task(gate.wait(source)) for source in ("boot", "replay")]
    try:
        await asyncio.sleep(0)
        clock.now = 10.5 if ready else 11.0
        server.started = ready
        gate._refresh()
        assert await asyncio.gather(*held) == [ready, ready]
        gate._refresh()
        output = capsys.readouterr().err
        if ready:
            assert "INFO api listener ready after 0.50s; released 2 held submission(s)" in output
            assert output.count("INFO api listener ready after") == 1
        else:
            assert "ERROR" in output
            assert "INFO api listener ready after" not in output
    finally:
        await gate.close()


def test_deferred_startup_jobs_have_an_actual_start_line(boot_run, tmp_path, monkeypatch, capsys):
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert capsys.readouterr().err.count("startup: deferred startup jobs starting") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("condition", ("expired", "stopped", "pending", "ready"))
@pytest.mark.parametrize("path", ("approval", "ferry"))
async def test_every_transport_refuses_durable_handoff_before_queueing_unless_ready(
    boot_run, tmp_path, monkeypatch, mode, condition, path,
):
    registry = boot_run.app.state.agents
    registry.register("primary", working_dir=str(tmp_path))
    registry.approve_user("primary", "sample-chat", display_name="sender")
    registry.queue_pending_message("primary", "web", "sample-chat", "sender", "durable message")
    gate = boot_run.app.state.api_readiness
    server = SimpleNamespace(started=condition == "ready", should_exit=condition == "stopped", phase=2)
    boot_run.server = server
    gate.attach(server)
    gate.started_at = 10.0
    gate.cap_seconds = 10.0
    monkeypatch.setattr(api_readiness, "time", SimpleNamespace(
        monotonic=lambda: 21.0 if condition == "expired" else 10.5,
    ))
    config = StreamingSessionConfig(
        agent_name="primary", working_dir=str(tmp_path), api_readiness=gate,
        live_status_fn=lambda: {"status": "idle", "last_updated": 0},
    )
    cls = {
        "claude-sdk": StreamingSession, "claude-tmux": TmuxSession,
        "codex-tmux": CodexTmuxSession, "codex-exec": CodexSession,
        "codex-app-server": CodexSession,
    }[mode]
    session = cls(config)
    session._state_machine._state = SessionState.CONNECTED
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1" if mode == "codex-app-server" else "0")
    if mode == "claude-sdk":
        session._client = SimpleNamespace(query=AsyncMock(
            side_effect=lambda prompt: boot_run.submit("sdk-query", prompt),
        ))
    elif mode.endswith("tmux"):
        session._session_ready_event.set()

        async def paste(prompt, **kwargs):
            boot_run.submit("pane-paste", prompt)
            return SimpleNamespace(ok=True, returncode=0, stderr="")

        async def finish(turn):
            session._resolve_submission_receipt(turn, True)
            session._fire_on_delivered(turn)
            session._turn_done.set()

        session._tmux.paste_text = paste
        session._finish_submitted_turn = finish
        session._worker_task = asyncio.create_task(session._message_worker())
    else:
        class AppClient:
            async def request(self, method, params):
                if method == "turn/start":
                    boot_run.submit("app-server-turn", params["input"][0]["text"])
                    session._turn_done.set_result(None)
                    return {}
                return {"thread": {"id": "sample-thread"}}

        session._app_client = AppClient()
        session._ensure_app_server = AsyncMock(return_value=True)
        session._start_worker()
    send = AsyncMock(wraps=session.send)
    monkeypatch.setattr(session, "send", send)
    broker = boot_run.app.state.broker
    monkeypatch.setattr(broker, "_get_streaming_session", lambda *args: session)
    monkeypatch.setattr(broker, "_start_typing", AsyncMock())
    host = None
    result = None
    timed_out = False
    try:
        try:
            async with asyncio.timeout(0.5):
                if path == "approval":
                    try:
                        result = await broker.handle_approval("primary", "sample-chat")
                    except RuntimeError:
                        result = 0
                else:
                    registry.add_peer_fleet_acl("primary", fleet="sample", agent_id="recovery@sample")
                    host = HostPinky(registry=registry, broker=broker, fleet_name="sample")
                    result = await host.deliver(FerryEnvelope(
                        v="0.1", id="sample-envelope", from_="recovery@sample", to="primary@sample",
                        ts=1, body={"kind": "message", "text": "durable ferry message"},
                    ))
        except TimeoutError:
            timed_out = True
        assert not timed_out, "Ferry/broker admission waited past the prompt retry bound"
        if condition == "ready":
            send.assert_awaited_once()
            await boot_run.wait_for_delivery(1)
            assert broker.stats["routed"] == 1
            if host is not None:
                assert result.status == "delivered"
                assert host.stats["delivered"] == 1 and host.stats["messages_routed"] == 1
            else:
                assert result == 1
                assert registry.get_pending_messages("primary", "sample-chat") == []
        else:
            send.assert_not_awaited()
            await boot_run.checkpoint()
            assert boot_run.observed == []
            assert len(registry.get_pending_messages("primary", "sample-chat")) == 1
            assert broker.stats["routed"] == 0
            if host is not None:
                assert result.status == "transient_failure"
                assert host.stats["delivered"] == 0 and host.stats["messages_routed"] == 0
            else:
                assert result == 0
    finally:
        await gate.close()
        worker = getattr(session, "_worker_task", None)
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
