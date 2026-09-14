"""Exercise the remote-MCP gate through the real application startup lifecycle."""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import api, codex_home
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState


@pytest.fixture
def boot_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    monkeypatch.setenv("PINKY_MCP_READINESS_CAP_SEC", "0.03")
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    app = api.create_api(db_path=str(tmp_path / "test.db"), default_working_dir=str(tmp_path))
    trace = []

    async def connect(session):
        trace.append(("launch", session.agent_name))
        session._state_machine._state = SessionState.CONNECTED

    async def disconnect(session):
        session._state_machine._state = SessionState.DEAD

    for cls in (TmuxSession, CodexTmuxSession, CodexSession):
        monkeypatch.setattr(cls, "connect", connect)
        monkeypatch.setattr(cls, "disconnect", disconnect)
    return app, trace


def _agent(app, tmp_path, name="primary", *, runtime="claude_sdk", transport="tmux"):
    work = tmp_path / name
    work.mkdir()
    app.state.agents.register(name, runtime=runtime, transport=transport, working_dir=str(work))
    if name == "primary":
        app.state.agents.set_main_agent(name)
    return work


def _remote(app, name="primary"):
    app.state.agents.add_mcp_server(
        name, "remote-bridge", server_type="http", url="https://bridge.example/mcp",
    )


@pytest.mark.asyncio
async def test_boot_waits_for_remote_before_launch(boot_app, tmp_path, monkeypatch, capsys):
    app, trace = boot_app
    _agent(app, tmp_path)
    _remote(app)
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())

    async def connect(host, port, **kwargs):
        trace.append(("probe", host, port))
        return None, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    async with app.router.lifespan_context(app):
        assert trace == [("probe", "bridge.example", 443), ("launch", "primary")]
    assert re.search(
        r"startup: mcp readiness — 1 host\(s\) up, 0 unreachable, waited [\d.]+s",
        capsys.readouterr().err,
    )


@pytest.mark.asyncio
async def test_boot_cap_launches_with_exact_log_and_event(boot_app, tmp_path, monkeypatch, capsys):
    app, trace = boot_app
    _agent(app, tmp_path)
    _remote(app)
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=OSError("offline")))
    async with app.router.lifespan_context(app):
        assert ("launch", "primary") in trace
        events = app.state.session_event_store.get_for_agent("primary", limit=50)
        unreachable = [e for e in events if e["event_type"] == "mcp_host_unreachable"]
        assert len(unreachable) == 1
        metadata = unreachable[0]["metadata"]
        assert metadata["host"] == "bridge.example" and metadata["port"] == 443
        assert 0 <= metadata["waited_sec"] < 0.2
    log = capsys.readouterr().err
    assert re.search(
        r"startup: remote MCP host bridge\.example:443 unreachable after [\d.]+s "
        r"\(1 attempts\); launching primary without waiting", log,
    )
    assert re.search(
        r"startup: mcp readiness — 0 host\(s\) up, 1 unreachable, waited [\d.]+s", log,
    )


@pytest.mark.asyncio
async def test_local_agent_and_poller_start_while_remote_probe_pending(
    boot_app, tmp_path, monkeypatch,
):
    app, trace = boot_app
    _agent(app, tmp_path)
    _remote(app)
    _agent(app, tmp_path, "local")
    (tmp_path / "restart_manifest.json").write_text(json.dumps({
        "restart_time": datetime.now(timezone.utc).isoformat(),
        "agents": {"local": {"label": "main"}},
    }))
    app.state.agents.set_token("local", "telegram", "fake-test-token")
    from pinky_daemon.pollers import BrokerTelegramPoller

    async def start_poller(self):
        trace.append(("poller", "local"))

    monkeypatch.setattr(BrokerTelegramPoller, "start", start_poller)
    monkeypatch.setattr(BrokerTelegramPoller, "stop", Mock())
    probe_entered = asyncio.Event()
    release = asyncio.Event()
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())

    async def connect(host, port, **kwargs):
        trace.append(("probe", host, port))
        probe_entered.set()
        await release.wait()
        return None, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setenv("PINKY_MCP_READINESS_CAP_SEC", "2")
    lifespan = app.router.lifespan_context(app)
    startup = asyncio.create_task(lifespan.__aenter__())
    try:
        await asyncio.wait_for(probe_entered.wait(), timeout=1)
        for _ in range(100):
            if ("launch", "local") in trace and ("poller", "local") in trace:
                break
            await asyncio.sleep(0.001)
        assert ("launch", "primary") not in trace
        assert ("launch", "local") in trace
        assert ("poller", "local") in trace
    finally:
        release.set()
        await asyncio.wait_for(startup, timeout=3)
        await lifespan.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_stdio_or_disabled_gate_launches_without_probe(
    boot_app, tmp_path, monkeypatch, capsys, disabled,
):
    app, trace = boot_app
    _agent(app, tmp_path)
    if disabled:
        _remote(app)
        monkeypatch.setenv("PINKY_MCP_READINESS_CAP_SEC", "0")
    probe = AsyncMock(side_effect=AssertionError("no remote probe expected"))
    monkeypatch.setattr(asyncio, "open_connection", probe)
    async with app.router.lifespan_context(app):
        assert ("launch", "primary") in trace
    probe.assert_not_awaited()
    log = capsys.readouterr().err
    assert "startup: mcp readiness — 0 host(s) up, 0 unreachable, waited 0s" in log
    if disabled:
        assert "startup: mcp readiness gate disabled" in log


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["tmux", "sdk"])
async def test_codex_gate_reads_resolved_home_not_claude_json(
    boot_app, tmp_path, monkeypatch, transport,
):
    app, trace = boot_app
    work = _agent(app, tmp_path, runtime="codex_cli", transport=transport)
    home = tmp_path / "codex-config"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    contents = '[mcp_servers.remote]\nurl = "https://codex-bridge.example:9443/mcp"\n'
    (home / "config.toml").write_text(contents)
    (work / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"unrelated": {"url": "https://claude-only.example/mcp"}},
    }))
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())

    async def connect(host, port, **kwargs):
        trace.append(("probe", host, port))
        return None, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    async with app.router.lifespan_context(app):
        assert trace == [("probe", "codex-bridge.example", 9443), ("launch", "primary")]
        assert (home / "config.toml").read_text() == contents


def test_codex_effective_config_seam_is_read_only_and_overlays_runtime(tmp_path, monkeypatch):
    home = tmp_path / "scope"
    home.mkdir()
    # Use the exported switch name, not assumptions about deployment defaults.
    monkeypatch.setenv(codex_home.PER_AGENT_CODEX_HOME_ENV, "1")
    config_path = home / "config.toml"
    text = '[mcp_servers.bridge]\nurl = "https://old.example/mcp"\n'
    config_path.write_text(text)
    before = config_path.stat()
    agent = SimpleNamespace(working_dir=str(tmp_path), codex_home=str(home))
    result = codex_home.effective_codex_mcp_config(
        agent, {"bridge": {"url": "https://override.example:9443/mcp"}},
    )
    assert result["mcp_servers"]["bridge"]["url"] == "https://override.example:9443/mcp"
    assert config_path.read_text() == text
    assert config_path.stat().st_mtime_ns == before.st_mtime_ns
    assert list(home.iterdir()) == [config_path]


@pytest.mark.asyncio
async def test_boot_wires_terminal_wake_alert_to_owner_path(boot_app, tmp_path, monkeypatch):
    app, _ = boot_app
    _agent(app, tmp_path)
    app.state.agents.set_owner_notification_destinations([{
        "platform": "slack", "account_id": "T_TEST", "conversation_id": "D_TEST",
        "principal_id": "U_TEST",
    }])
    async with app.router.lifespan_context(app):
        session = app.state.broker._streaming["primary"]["main"]
        callback = session._config.wake_failure_callback
        assert callable(callback)
        send = AsyncMock(return_value={"sent": True})
        monkeypatch.setattr(app.state.broker, "_send_callback", send)
        monkeypatch.setattr(app.state.agents, "get_raw_token_for_account", lambda *args: "test")
        message = (
            "Wake submission unverified: agent=primary reason=wake_resume "
            "submit_attempts=1 latency_ms=20"
        )
        assert await callback("primary", message) is True
        send.assert_awaited_once()
        assert send.await_args.args == ("primary", "slack", "D_TEST", message)


@pytest.mark.asyncio
async def test_terminal_wake_session_produces_owner_notification(boot_app, tmp_path, monkeypatch):
    from pinky_daemon import tmux_session
    from pinky_daemon.tmux_session import TmuxCommandResult, _QueuedTurn

    app, _ = boot_app
    _agent(app, tmp_path)
    app.state.agents.set_owner_notification_destinations([{
        "platform": "slack", "account_id": "T_TEST", "conversation_id": "D_TEST",
        "principal_id": "U_TEST",
    }])
    monkeypatch.setattr(tmux_session, "_WAKE_SUBMISSION_RECEIPT_TIMEOUT_SEC", 0.001)
    async with app.router.lifespan_context(app):
        session = app.state.broker._streaming["primary"]["main"]
        monkeypatch.setattr(session, "_wake_receipt_timeout_sec", lambda: 0.001, raising=False)
        session._session_ready_event.set()
        session._tmux.paste_text = AsyncMock(return_value=TmuxCommandResult(0, "", ""))
        session._tmux.capture_pane = AsyncMock(return_value=TmuxCommandResult(0, "> empty", ""))
        send = AsyncMock(return_value={"sent": True})
        monkeypatch.setattr(app.state.broker, "_send_callback", send)
        monkeypatch.setattr(app.state.agents, "get_raw_token_for_account", lambda *args: "test")
        events = []

        async def event(data):
            events.append(data)

        session._stream_event_callback = event
        turn = _QueuedTurn(
            prompt="private wake payload must not enter the owner alert",
            internal=True, reason="wake_new_session",
            submission_receipt=asyncio.get_running_loop().create_future(),
        )
        with pytest.raises(RuntimeError, match="not confirmed"):
            await session._deliver_turn(turn)
        await session._notify_delivery_failure(turn)
        send.assert_awaited_once()
        terminal = [e for e in events if e["type"] == "wake_prompt_submission_unverified"]
        assert len(terminal) == 1 and terminal[0]["terminal"] is True
        assert terminal[0]["submit_attempts"] == 1
        expected = (
            "Wake submission unverified: agent=primary reason=wake_new_session "
            f"submit_attempts=1 latency_ms={terminal[0]['latency_ms']}"
        )
        assert send.await_args.args == ("primary", "slack", "D_TEST", expected)
        assert "\n" not in expected and turn.prompt not in expected


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["tmux", "sdk"])
async def test_codex_boot_uses_single_read_only_config_seam(
    boot_app, tmp_path, monkeypatch, transport,
):
    app, trace = boot_app
    _agent(app, tmp_path, runtime="codex_cli", transport=transport)
    original = codex_home.effective_codex_mcp_config
    home = tmp_path / "codex-config"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    path = home / "config.toml"
    path.write_text('[mcp_servers.remote]\nurl = "https://seam.example/mcp"\n')
    in_seam = False
    real_open, real_load, real_loads = Path.open, tomllib.load, tomllib.loads

    def guarded_open(self, *args, **kwargs):
        assert self != path or in_seam, "Codex TOML opened outside extraction seam"
        return real_open(self, *args, **kwargs)

    def guarded_load(*args, **kwargs):
        assert in_seam, "ad hoc TOML parse outside extraction seam"
        return real_load(*args, **kwargs)

    def guarded_loads(*args, **kwargs):
        assert in_seam, "ad hoc TOML parse outside extraction seam"
        return real_loads(*args, **kwargs)

    def extract(*args, **kwargs):
        nonlocal in_seam
        in_seam = True
        try:
            return original(*args, **kwargs)
        finally:
            in_seam = False

    spy = Mock(side_effect=extract)
    monkeypatch.setattr(codex_home, "effective_codex_mcp_config", spy)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(tomllib, "load", guarded_load)
    monkeypatch.setattr(tomllib, "loads", guarded_loads)
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    probe = AsyncMock(return_value=(None, writer))
    monkeypatch.setattr(asyncio, "open_connection", probe)
    async with app.router.lifespan_context(app):
        spy.assert_called_once()
        probe.assert_awaited_once_with("seam.example", 443)
        assert ("launch", "primary") in trace


def test_container_shared_alias_from_real_writer_is_not_remote(boot_app, tmp_path, monkeypatch):
    from importlib import import_module

    app, _ = boot_app
    work = _agent(app, tmp_path)
    app.state.agents.update("primary", isolation_mode="container")
    api._write_mcp_json(work, "primary", agent_registry=app.state.agents)
    config = json.loads((work / ".mcp.json").read_text())
    assert "host.containers.internal" in config["mcpServers"]["pinky-self"]["url"]
    monkeypatch.setattr(api, "SHARED_MCP_HOST", "daemon.example")
    exclusions = api._shared_mcp_endpoints()
    assert ("daemon.example", api.SHARED_MCP_PORT) in exclusions
    assert ("host.containers.internal", api.SHARED_MCP_PORT) in exclusions
    assert import_module("pinky_daemon.mcp_readiness").remote_mcp_endpoints(
        config, excluded_endpoints=exclusions,
    ) == set()
