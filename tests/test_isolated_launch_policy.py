"""Actual session/config construction must carry tenant policy."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api
from tests.isolated_policy_support import (
    closure,
    names,
    protocol,
    shared_service,
    signed,
)
from tests.isolated_policy_support import (
    daemon as daemon,
)

pytestmark = pytest.mark.real_auth
RUNTIMES = ["claude-tmux", "codex-exec", "codex-appserver", "codex-tmux", "claude-sdk"]
DENIED = {"mcp__pinky-self__add_skill", "mcp__pinky-self__restart_daemon"}


async def prepare(d, monkeypatch, runtime, name="tenant"):
    codex = runtime.startswith("codex")
    d.agents.update(
        name,
        runtime="codex_cli" if codex else "claude_sdk",
        transport="tmux" if runtime.endswith("tmux") else "sdk",
    )
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1" if runtime == "codex-appserver" else "0")
    work = Path(d.agents.get(name).working_dir)
    api._write_mcp_json(work, name, agent_registry=d.agents, skill_store=d.skills)
    session = await closure(d.app, "_prepare_streaming_session")(name)
    assert session is not None
    if codex:
        # Exercise the real command/config consumer without executing its CLI.
        if runtime == "codex-appserver":
            emitted = session._appserver_config()
            headers = emitted["mcp_servers"]["pinky-self"]["http_headers"]
        else:
            if runtime == "codex-tmux":
                monkeypatch.setattr(session, "_has_prior_transcript", lambda: False)
                session._build_claude_cmd()
            else:
                session._build_codex_cmd()
            headers = session._config.mcp_servers["pinky-self"]["headers"]
        transport = "http"
    else:
        if runtime == "claude-tmux":
            monkeypatch.setattr(session, "_has_prior_transcript", lambda: False)
            session._build_claude_cmd()
        headers = json.loads((work / ".mcp.json").read_text())["mcpServers"]["pinky-self"][
            "headers"
        ]
        transport = "sse"
    return session, transport, headers


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_real_runtime_tool_availability(daemon, monkeypatch, runtime):
    d = daemon()
    session, transport, headers = await prepare(d, monkeypatch, runtime)
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, headers) as client:
            advertised = await names(client)
            result = await client.call_tool("restart_daemon", {})
    assert not {"add_skill", "restart_daemon"} & advertised, runtime
    assert result.isError and not service.outgoing, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", RUNTIMES)
async def test_real_runtime_launch_denies_defense_in_depth(daemon, monkeypatch, runtime):
    d = daemon()
    session, _, _ = await prepare(d, monkeypatch, runtime)
    assert DENIED <= set(session._config.disallowed_tools), runtime


def test_stdio_config_strips_privileged_registration(daemon, monkeypatch):
    from pinky_self.server import create_server

    d = daemon()
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    work = d.root / "tenant"
    api._write_mcp_json(work, "tenant", agent_registry=d.agents, skill_store=d.skills)
    config = json.loads((work / ".mcp.json").read_text())["mcpServers"]["pinky-self"]
    args = config["args"]
    gates = args[args.index("--tool-gates") + 1].split(",") if "--tool-gates" in args else []
    server = create_server(agent_name="tenant", tool_gates=gates)
    advertised = {tool.name for tool in server._tool_manager.list_tools()}
    assert not {"add_skill", "restart_daemon"} & advertised


def test_real_legacy_creation_uses_effective_policy(daemon):
    d = daemon("shadow")  # Route denial must not conceal a missing launch gate.
    path = "/agents/tenant/sessions"
    client = TestClient(d.app)
    response = client.post(
        path, headers=signed(d, "POST", path), json={"session_id": "policy-created"}
    )
    client.close()
    assert response.status_code == 200, response.text
    session = d.app.state.manager.get("policy-created")
    assert session is not None
    assert DENIED <= set(session.disallowed_tools), session.disallowed_tools


def test_creation_control_preserves_nonisolated_behavior(daemon):
    d = daemon()
    path = "/agents/normal/sessions"
    client = TestClient(d.app)
    response = client.post(
        path, headers=signed(d, "POST", path, "normal"), json={"session_id": "normal-created"}
    )
    client.close()
    assert response.status_code == 200, response.text
    assert d.app.state.manager.get("normal-created") is not None


@pytest.mark.parametrize("actor", ["tenant", "normal"])
def test_clone_worker_runs_after_fork(daemon, monkeypatch, actor):
    """Resolve metadata through the manager before forking a live session."""
    from types import SimpleNamespace

    import claude_agent_sdk

    from pinky_daemon.sessions import Session

    d = daemon("shadow")
    manager = d.app.state.manager
    main = manager.create(
        session_id=f"{actor}-main",
        agent_name=actor,
        session_type="main",
        working_dir=str(d.root / actor),
    )
    main._sdk_session_id = "fixture-main-sdk"
    assert isinstance(manager.list()[0].session_type, str)
    forked = []
    sent = []

    def fork(sdk_id, **kwargs):
        forked.append(sdk_id)
        return SimpleNamespace(session_id="fixture-fork-sdk")

    async def send(session, content):
        sent.append((session.agent_name, content, session._sdk_session_id))

    monkeypatch.setattr(claude_agent_sdk, "fork_session", fork)
    monkeypatch.setattr(Session, "send", send)
    path = f"/agents/{actor}/clone-worker"
    client = TestClient(d.app)
    response = client.post(
        path, headers=signed(d, "POST", path, actor), json={"task": "fixture task"}
    )
    client.close()
    assert response.status_code == 200, response.text
    assert (forked, len(manager.list())) == (["fixture-main-sdk"], 2)
    assert sent == [(actor, "fixture task", "fixture-fork-sdk")]
