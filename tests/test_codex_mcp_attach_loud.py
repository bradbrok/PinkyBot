"""Codex attach failures require two quiet windows and one bounded retry."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.api import create_api
from pinky_daemon.session_watchdog import (
    DEFAULT_MCP_PROBE_DEADLINE,
    DEFAULT_MCP_RECOVER_MIN_INTERVAL,
    DEFAULT_MCP_UNBOUND_FLOOR,
)
from pinky_daemon.shared_mcp import (
    bump_gateway_epoch,
    get_probe_request,
    record_mcp_success,
    record_probe_request,
    record_probe_success,
)
from pinky_daemon.transport_state import SessionState

ORIGIN = 1_800_000_000.0
SENTINEL = "test-mcp-secret-" + "q" * 48
FAILURE_PROTOCOL = (
    "If mcp_probe is unavailable or errors: do NOT call MCP endpoints by hand "
    "(curl/SSE/HTTP), never copy any token into a command; "
    "reply 'MCP attach failed' in one line and wait."
)


def assert_outputs_clean(*outputs):
    """Fail without echoing the value that made a capture unsafe."""
    unsafe = any(forbidden in output for output in outputs for forbidden in (SENTINEL, "Bearer"))
    assert not unsafe, "credential material found in captured output"


class Clock:
    def __init__(self):
        self.now = ORIGIN

    def advance(self, seconds):
        self.now += seconds


class InertSession:
    """Only delivery is faked; watchdog, broker and gateway ledgers are real."""

    def __init__(self, name, notices, runtime="codex_cli"):
        self._launch_runtime = runtime
        self._config = SimpleNamespace(
            agent_name=name,
            label="main",
            provider_url=runtime,
            mcp_servers={"self": {"headers": {"Authorization": f"Bearer {SENTINEL}"}}},
        )
        self.state = SessionState.CONNECTED
        self.notices = notices

    @property
    def stats(self):
        return {"state": self.state.value, "turns": 0, "pending_messages": 0}

    async def send(self, text):
        self.notices.append(text)
        return True


@pytest.fixture
def harness(tmp_path, monkeypatch, caplog, capsys):
    clock = Clock()
    monkeypatch.setattr(time, "time", lambda: clock.now)
    caplog.set_level(logging.INFO)
    app = create_api(db_path=str(tmp_path / "test.db"), default_working_dir=str(tmp_path))
    bump_gateway_epoch()
    alerts, notices, recovered = [], [], []

    async def alert(name, message):
        alerts.append((name, message))
        return True

    async def recover(name, label, reason):
        recovered.append((name, label, reason))

    watchdog = app.state.watchdog
    real_recover = watchdog._mcp_recover_fn
    watchdog._alert_fn = alert
    watchdog._mcp_recover_fn = recover
    watchdog._tmux_liveness_fn = None
    watchdog._login_wall_probe_fn = None

    def add(name="test-agent", *, runtime="codex_cli", enabled=False, mcp_recover=False):
        app.state.agents.register(
            name,
            runtime=runtime,
            transport="tmux",
            heartbeat_interval=0,
            watchdog_config={"enabled": enabled, "mcp_recover": mcp_recover},
        )
        session = InertSession(name, notices, runtime)
        app.state.broker._streaming[name] = {"main": session}
        return session

    def launch(name="test-agent", *, seed=False):
        text = app.state._build_streaming_wake_context(name)
        if seed:
            # Isolate detection RED from the independently tested injection gap.
            record_probe_request(name, f"launch-{int(clock.now)}", "test-nonce")
        return text

    def prove(name="test-agent", *, generic=False):
        if generic:
            record_mcp_success(name)
        else:
            req = get_probe_request(name)
            record_probe_success(name, req["nonce"], req["launch_id"])

    def check_outputs():
        captured = capsys.readouterr()
        output = "\n".join(
            [
                caplog.text,
                captured.out,
                captured.err,
                *notices,
                *(message for _, message in alerts),
                *(reason for _, _, reason in recovered),
            ]
        )
        try:
            assert_outputs_clean(output)
        except AssertionError:
            # Keep both the test report and fixture teardown values-free.
            caplog.clear()
            notices.clear()
            alerts.clear()
            recovered.clear()
            pytest.fail("credential material found in captured output", pytrace=False)

    h = SimpleNamespace(
        app=app,
        watchdog=watchdog,
        clock=clock,
        add=add,
        launch=launch,
        prove=prove,
        alerts=alerts,
        notices=notices,
        recovered=recovered,
        caplog=caplog,
        real_recover=real_recover,
        check_outputs=check_outputs,
    )
    yield h
    # Scan ALL captured output, including successful controls and error paths.
    check_outputs()


@pytest.mark.parametrize("channel", ["log", "alert", "notice"])
def test_secret_scanner_positive_control(harness, channel):
    """Each captured output channel must be scanned, not just the notice."""
    h = harness
    if channel == "log":
        logging.getLogger("pinky.watchdog").warning("%s", SENTINEL)
    elif channel == "alert":
        h.alerts.append(("test-agent", SENTINEL))
    else:
        h.notices.append(SENTINEL)
    captured = "\n".join([h.caplog.text, *h.notices, *(message for _, message in h.alerts)])
    # The deliberately planted value never reaches pytest's report.
    h.caplog.clear()
    h.alerts.clear()
    h.notices.clear()
    with pytest.raises(AssertionError, match="credential material"):
        assert_outputs_clean(captured)


@pytest.mark.asyncio
async def test_outputs_are_values_only(harness):
    h = harness
    h.add()
    h.launch(seed=True)
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.alerts) == len(h.notices) == 1
    h.check_outputs()


@pytest.mark.parametrize("transport", ["sdk", "tmux"])
def test_codex_probe_is_always_committed(harness, transport):
    h = harness
    h.add()
    h.app.state.agents.register("test-agent", transport=transport)
    text = h.launch()
    req = get_probe_request("test-agent")
    assert req.get("current") is True
    assert req["nonce"] in text and req["launch_id"] in text
    assert FAILURE_PROTOCOL in text
    assert text.startswith("⚠️ MCP BIND CHECK")


def test_codex_preview_does_not_record_probe(harness):
    h = harness
    h.add()
    text = h.app.state._build_streaming_wake_context("test-agent", commit=False)
    assert "MCP BIND CHECK" not in text
    assert get_probe_request("test-agent") == {}


@pytest.mark.asyncio
async def test_deadline_is_loud_once_then_waits_a_second_window(harness):
    h = harness
    h.add()
    h.launch(seed=True)
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE - 1)
    await h.watchdog._sweep()
    assert h.alerts == h.notices == h.recovered == []
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert len(h.alerts) == len(h.notices) == 1
    failures = [r for r in h.caplog.records if r.message.startswith("MCP_ATTACH_FAILED ")]
    assert len(failures) == 1
    assert "agent=test-agent" in failures[0].message
    assert "launch_id=" in failures[0].message and "age_s=120" in failures[0].message
    assert FAILURE_PROTOCOL in h.notices[0]
    assert "call mcp_probe now" in h.notices[0]
    assert h.recovered == []
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE - 1)
    await h.watchdog._sweep()
    assert h.recovered == []
    assert len(h.alerts) == len(h.notices) == 1
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    assert len(h.alerts) == len(h.notices) == 1
    assert sum(r.message.startswith("MCP_ATTACH_FAILED ") for r in h.caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("generic", [False, True])
async def test_success_in_first_window_is_quiet(harness, generic):
    h = harness
    h.add()
    h.launch(seed=True)
    h.clock.advance(30)
    h.prove(generic=generic)
    h.clock.advance(1000)
    await h.watchdog._sweep()
    assert h.alerts == h.notices == h.recovered == []
    assert "MCP_ATTACH_FAILED" not in h.caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("generic", [False, True])
async def test_success_after_notice_cancels_relaunch(harness, generic):
    h = harness
    h.add()
    h.launch(seed=True)
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.alerts) == len(h.notices) == 1
    h.clock.advance(30)
    h.prove(generic=generic)
    h.clock.advance(1000)
    await h.watchdog._sweep()
    assert h.recovered == []
    assert len(h.alerts) == len(h.notices) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("opted_in", [False, True])
async def test_second_failed_launch_never_restarts_even_after_unregister(harness, opted_in):
    h = harness
    h.add(mcp_recover=opted_in)
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    h.launch(seed=True)
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    h.app.state.broker._streaming.clear()
    await h.watchdog._sweep()
    h.add(mcp_recover=opted_in)
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    h.launch(seed=True)
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.alerts) == len(h.notices) == 2
    h.clock.advance(10000)
    await h.watchdog._sweep()
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    assert len(h.alerts) == len(h.notices) == 2


@pytest.mark.asyncio
async def test_global_limiter_defers_without_repeating_notice(harness):
    h = harness
    for name in ("test-agent", "other-agent"):
        h.add(name)
        h.launch(name, seed=True)
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.alerts) == len(h.notices) == 2
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    h.clock.advance(DEFAULT_MCP_RECOVER_MIN_INTERVAL - 1)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert len(h.recovered) == 2
    assert len(h.alerts) == len(h.notices) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["test-agent", "legacy-agent"])
async def test_global_limiter_is_shared_with_legacy_recovery(harness, first):
    h = harness
    order = [first, "legacy-agent" if first == "test-agent" else "test-agent"]
    for name in order:
        legacy = name == "legacy-agent"
        h.add(name, runtime="claude_sdk" if legacy else "codex_cli", mcp_recover=legacy)
        if legacy:
            h.app.state.agents.register(name, heartbeat_interval=60)
        h.launch(name, seed=not legacy)
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert [name for name, _, _ in h.recovered] == [first]
    h.clock.advance(DEFAULT_MCP_RECOVER_MIN_INTERVAL)
    await h.watchdog._sweep()
    assert [name for name, _, _ in h.recovered] == order


@pytest.mark.asyncio
async def test_no_current_probe_makes_no_inference(harness):
    h = harness
    h.add()
    h.launch(seed=True)
    bump_gateway_epoch()
    h.clock.advance(10000)
    await h.watchdog._sweep()
    assert h.alerts == h.notices == h.recovered == []


@pytest.mark.asyncio
async def test_recovery_error_spends_attempt_and_never_logs_exception_value(harness):
    h = harness
    h.add()
    h.launch(seed=True)

    async def fail(*args):
        h.recovered.append(args)
        raise RuntimeError(SENTINEL)

    h.watchdog._mcp_recover_fn = fail
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    h.clock.advance(1000)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1


@pytest.mark.asyncio
async def test_non_codex_stays_opt_in(harness):
    h = harness
    h.add(runtime="claude_sdk")
    text = h.launch()
    assert get_probe_request("test-agent") == {}
    assert FAILURE_PROTOCOL not in text
    h.clock.advance(10000)
    await h.watchdog._sweep()
    assert h.alerts == h.notices == h.recovered == []


@pytest.mark.asyncio
async def test_old_epoch_success_does_not_hide_missed_new_probe(harness):
    h = harness
    h.add()
    h.prove(generic=True)
    bump_gateway_epoch()
    h.launch(seed=True)
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.alerts) == len(h.notices) == 1
    assert h.recovered == []


@pytest.mark.asyncio
async def test_old_launch_success_suppresses_failure_but_does_not_rearm_budget(harness):
    h = harness
    h.add()
    h.launch(seed=True)
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    h.prove(generic=True)
    h.clock.advance(1)
    h.launch(seed=True)
    h.clock.advance(500)
    await h.watchdog._sweep()
    assert len(h.alerts) == 1  # any current-epoch call suppresses detection
    bump_gateway_epoch()
    h.launch(seed=True)
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.alerts) == 2
    assert len(h.recovered) == 1  # earlier launch's success cannot buy a retry


@pytest.mark.asyncio
async def test_current_launch_success_rearms_after_a_new_outage(harness):
    h = harness
    h.add()
    h.launch(seed=True)
    for _ in range(2):
        h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
        await h.watchdog._sweep()
    assert len(h.recovered) == 1
    h.launch(seed=True)
    h.clock.advance(1)
    h.prove(generic=True)
    await h.watchdog._sweep()
    bump_gateway_epoch()
    h.launch(seed=True)
    for _ in range(2):
        h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
        await h.watchdog._sweep()
    assert len(h.recovered) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_notice_must_arrive_before_second_window_starts(harness, failure):
    h = harness
    session = h.add()
    h.launch(seed=True)
    real_send = session.send

    async def unavailable(text):
        if failure == "exception":
            raise RuntimeError(SENTINEL)
        return False

    session.send = unavailable
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    h.clock.advance(1000)
    await h.watchdog._sweep()
    assert len(h.alerts) == 1
    assert h.notices == h.recovered == []
    session.send = real_send
    h.clock.advance(60)
    await h.watchdog._sweep()
    assert len(h.notices) == 1
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE - 1)
    await h.watchdog._sweep()
    assert h.recovered == []
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "exception", "no_callback"])
async def test_owner_alert_failure_does_not_block_retry_after_notice(harness, failure):
    h = harness
    h.add()
    h.launch(seed=True)

    async def unavailable(name, message):
        h.alerts.append((name, message))
        if failure == "exception":
            raise RuntimeError(SENTINEL)
        return False

    h.watchdog._alert_fn = None if failure == "no_callback" else unavailable
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.notices) == 1
    assert h.recovered == []
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE - 1)
    await h.watchdog._sweep()
    assert h.recovered == []
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
    h.clock.advance(DEFAULT_MCP_RECOVER_MIN_INTERVAL)
    await h.watchdog._sweep()
    assert len(h.recovered) == len(h.notices) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("opted_in", [True, False])
async def test_codex_legacy_recovery_after_fulfilled_launch_probe(harness, opted_in):
    h = harness
    h.add(mcp_recover=opted_in)
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    h.launch()
    h.prove()
    assert get_probe_request("test-agent")["fulfilled"] is True
    await h.watchdog._sweep()
    assert h.alerts == h.notices == h.recovered == []

    # The previously bound client loses its gateway epoch without a new wake.
    # Legacy opt-in recovery must still observe this sustained outage.
    h.clock.advance(10)
    bump_gateway_epoch()
    assert get_probe_request("test-agent") == {}
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_UNBOUND_FLOOR - 1)
    await h.watchdog._sweep()
    assert h.recovered == []
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert len(h.recovered) == int(opted_in)
    assert h.notices == []  # this is not a failed launch probe
    if opted_in:
        assert "MCP transport unbound" in h.recovered[0][2]
        assert "MCP_ATTACH_FAILED" not in h.caplog.text


@pytest.mark.asyncio
async def test_success_during_alert_await_prevents_retry(harness):
    h = harness
    h.add()
    h.launch(seed=True)

    async def alert(name, message):
        h.alerts.append((name, message))
        if len(h.alerts) == 2:
            h.prove(generic=True)
            return True
        return False

    h.watchdog._alert_fn = alert
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
    await h.watchdog._sweep()
    assert len(h.alerts) == 2
    assert h.recovered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sdk", "tmux"])
async def test_real_codex_wake_builder_delivers_probe(harness, tmp_path, monkeypatch, transport):
    from pinky_daemon.codex_session import CodexSession
    from pinky_daemon.codex_tmux_session import CodexTmuxSession
    from pinky_daemon.streaming_session import StreamingSessionConfig
    from pinky_daemon.wake_prompt import WakeReason

    h = harness
    h.add()
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(tmp_path),
        provider_url="codex_cli",
        wake_context_builder=h.app.state._build_streaming_wake_context,
    )
    if transport == "tmux":
        session = CodexTmuxSession(config)
        session._skip_wake_prompt_for_tests = False
        enqueue = AsyncMock()
        monkeypatch.setattr(session, "_enqueue_internal_prompt", enqueue)
        await session._enqueue_wake_prompt(WakeReason.NEW_SESSION)
        text = enqueue.await_args.args[0]
    else:
        session = CodexSession(config)
        await session._enqueue_wake()
        text = session._message_queue.get_nowait()[0]
    req = get_probe_request("test-agent")
    assert req.get("current") is True
    assert req["launch_id"] in text and req["nonce"] in text
    assert FAILURE_PROTOCOL in text


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sdk", "tmux"])
@pytest.mark.parametrize("connect_fails", [False, True])
async def test_recovery_callback_clears_codex_resume_state(
    harness, tmp_path, monkeypatch, transport, connect_fails
):
    from pinky_daemon.codex_session import CodexSession
    from pinky_daemon.codex_tmux_session import CodexTmuxSession
    from pinky_daemon.streaming_session import StreamingSessionConfig

    h = harness
    h.add()
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(tmp_path),
        provider_url="codex_cli",
        resume_handle="previous-thread",
    )
    session = CodexTmuxSession(config) if transport == "tmux" else CodexSession(config)
    session.resume_handle = "previous-thread"
    if transport == "sdk":
        session.codex_session_id = "previous-thread"
    h.app.state.agents.set_streaming_session_id("test-agent", "previous-thread")
    h.app.state.broker._streaming["test-agent"] = {"main": session}
    configs = []

    async def replacement(*, configure, **kwargs):
        configure()
        configs.append(True)
        # No process is launched. Exercise the actual transport command builder.
        if transport == "tmux":
            monkeypatch.setattr(session, "_has_prior_transcript", lambda: True)
            assert "resume" not in session._build_claude_cmd()
        else:
            assert "resume" not in session._build_codex_cmd()
        if connect_fails:
            raise RuntimeError(SENTINEL)

    monkeypatch.setattr(session, "restart_transport", replacement)
    if connect_fails:
        with pytest.raises(RuntimeError):
            await h.real_recover("test-agent", "main", "test attach recovery")
    else:
        await h.real_recover("test-agent", "main", "test attach recovery")
    assert configs == [True]
    assert session.resume_handle == config.resume_handle == ""
    assert config.force_fresh_context_once is True
    assert h.app.state.agents.get_streaming_session_id("test-agent") == ""
    if transport == "sdk":
        assert session.codex_session_id == ""


@pytest.mark.real_transport
@pytest.mark.asyncio
async def test_loopback_refusal_smoke(harness, tmp_path):
    """An isolated installed client fails attach; the daemon announces and retries once."""
    from aiohttp import web

    from pinky_daemon.codex_app_server import CodexAppServerClient
    from pinky_daemon.codex_mcp_env import mcp_cli_config

    binary = shutil.which("codex")
    if not binary:
        pytest.skip("Codex CLI is not installed")
    h = harness
    h.add()
    requests = []
    client_logs = []

    async def refuse(request):
        requests.append(request.method)
        return web.Response(status=503, text="test gateway unavailable")

    server = web.Application()
    server.router.add_route("*", "/{path:.*}", refuse)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    args, header_env = mcp_cli_config(
        {
            "test-mcp": {
                "url": f"http://127.0.0.1:{port}/mcp/self/http",
                "headers": {"Authorization": f"Bearer {SENTINEL}"},
            }
        }
    )
    isolated_home = tmp_path / "client-home"
    isolated_home.mkdir()
    (isolated_home / "codex").mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(isolated_home),
        "CODEX_HOME": str(isolated_home / "codex"),
        **header_env,
    }

    async def failed_attach():
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            "app-server",
            cwd=isolated_home,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        client = CodexAppServerClient(
            proc.stdout,
            proc.stdin,
            stderr=proc.stderr,
            log=client_logs.append,
        )
        client.start()
        try:
            await client.initialize(name="attach-test", version="1")
            await client.notify("initialized")
            result = await client.request("mcpServerStatus/list", {}, timeout=30)
            rows = result.get("data", [])
            assert any(row.get("name") == "test-mcp" and row.get("toolsError") for row in rows)
        finally:
            await client.close()
            if proc.returncode is None:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10)
            assert_outputs_clean(*client_logs)
            for line in client_logs:
                logging.getLogger(__name__).info("isolated client: %s", line)

    try:
        h.launch()  # real builder and ledger; no synthetic probe injection
        await failed_attach()
        assert requests

        async def recover(*args):
            h.recovered.append(args)
            h.launch()
            await failed_attach()

        h.watchdog._mcp_recover_fn = recover
        for _ in range(4):
            h.clock.advance(DEFAULT_MCP_PROBE_DEADLINE)
            await h.watchdog._sweep()
        assert len(h.recovered) == 1
        assert len(h.alerts) == len(h.notices) == 2
        assert_outputs_clean(*client_logs)
        h.check_outputs()
    finally:
        await runner.cleanup()
