"""Protocol and daemon boundary tests for the optional ACP stdio connector."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from pinky_cli.acp import (
    ACP_DEFAULT_ALLOWED_TOOLS,
    DaemonClient,
    PinkyAcpAgent,
    _read_session_secret,
)

acp = pytest.importorskip("acp")


class RecordingClient(acp.Client):
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        del kwargs
        self.updates.append((session_id, update))


def _agent_config() -> dict[str, Any]:
    return {
        "name": "tester",
        "model": "sonnet",
        "allowed_tools": ["Read", "Bash"],
        "disallowed_tools": ["WebSearch", "Write"],
        "permission_mode": "plan",
        "max_turns": 17,
        "timeout": 901.0,
        "restart_threshold_pct": 73.0,
        "auto_restart": False,
    }


def _response(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _client(
    handler: Any,
    *,
    busy_retries: int = 2,
    busy_retry_delay: float = 0.0,
) -> DaemonClient:
    return DaemonClient(
        daemon_url="http://daemon.test",
        agent_name="tester",
        secret="signed-test-secret",
        busy_retries=busy_retries,
        busy_retry_delay=busy_retry_delay,
        transport=httpx.MockTransport(handler),
    )


async def _new_agent(
    tmp_path: Path,
    handler: Any,
    *,
    keepalive_interval: float = 60.0,
    busy_retries: int = 2,
) -> tuple[PinkyAcpAgent, RecordingClient, str]:
    daemon = _client(handler, busy_retries=busy_retries)
    agent = PinkyAcpAgent(
        agent_name="tester",
        daemon=daemon,
        project_root=tmp_path,
        keepalive_interval=keepalive_interval,
    )
    recorder = RecordingClient()
    agent.on_connect(recorder)
    session = await agent.new_session(cwd="/client/cwd")
    return agent, recorder, session.session_id


@pytest.mark.asyncio
async def test_subprocess_handshake_env_fallbacks_and_stdout_hygiene(tmp_path):
    recorder = RecordingClient()
    env = {
        **os.environ,
        "PINKY_AGENT": "tester",
        "PINKY_DAEMON_URL": "http://daemon.invalid",
        "PINKY_PROJECT_ROOT": str(tmp_path),
        "PINKY_SESSION_SECRET": "signed-test-secret",
    }

    async with acp.spawn_agent_process(
        recorder,
        sys.executable,
        "-m",
        "pinky_cli",
        "acp",
        env=env,
    ) as (connection, process):
        response = await connection.initialize(protocol_version=2)

        assert response.protocol_version == 1
        assert response.auth_methods == []
        capabilities = response.agent_capabilities
        assert capabilities.load_session is False
        assert capabilities.mcp_capabilities.http is False
        assert capabilities.mcp_capabilities.sse is False
        assert capabilities.prompt_capabilities.embedded_context is True
        assert process.returncode is None

    assert process.stderr is not None
    assert await process.stderr.read() == b""


@pytest.mark.asyncio
async def test_new_and_prompt_happy_path_uses_agent_identity_and_policy(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PINKY_ACP_PERMISSION_MODE", raising=False)
    requests: list[tuple[str, str, dict[str, Any], httpx.Headers]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body, request.headers))
        if request.method == "GET":
            return _response(200, _agent_config())
        if request.url.path == "/sessions":
            return _response(200, {"id": "daemon-session"})
        return _response(
            200,
            {"content": "daemon answer", "error": "", "role": "assistant"},
        )

    agent, recorder, session_id = await _new_agent(tmp_path, handler)
    embedded = acp.schema.EmbeddedResourceContentBlock(
        type="resource",
        resource=acp.schema.TextResourceContents(
            uri="file:///context.txt",
            text="embedded context",
        ),
    )
    try:
        result = await agent.prompt(
            session_id,
            [acp.text_block("user prompt"), embedded],
        )
    finally:
        await agent.close()

    assert result.stop_reason == "end_turn"
    assert [(update.session_update, update.content.text) for _, update in recorder.updates] == [
        ("agent_message_chunk", "daemon answer")
    ]
    create = requests[1]
    assert create[:2] == ("POST", "/sessions")
    create_body = create[2]
    assert create_body["working_dir"] == str(tmp_path / "data" / "agents" / "tester")
    assert create_body["working_dir"] != "/client/cwd"
    assert create_body["model"] == "sonnet"
    assert create_body["allowed_tools"] == ["Read", "Bash"]
    assert create_body["disallowed_tools"] == ["WebSearch", "Write"]
    assert create_body["permission_mode"] == "plan"
    assert create_body["permission_mode"] != "bypassPermissions"
    assert create_body["max_turns"] == 17
    assert create_body["timeout"] == 901.0
    assert create_body["restart_threshold_pct"] == 73.0
    assert create_body["auto_restart"] is False
    message = requests[2]
    assert message[:3] == (
        "POST",
        "/sessions/daemon-session/message",
        {"content": "user prompt\nembedded context"},
    )
    for _, _, _, headers in requests:
        assert headers["x-pinky-agent"] == "tester"
        assert headers["x-pinky-signature"]
        assert headers["x-pinky-timestamp"]


@pytest.mark.asyncio
async def test_bypass_agent_defaults_acp_surface_to_dont_ask(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("PINKY_ACP_PERMISSION_MODE", raising=False)
    create_bodies: list[dict[str, Any]] = []
    config = {
        **_agent_config(),
        "allowed_tools": ["Read"],
        "permission_mode": "bypassPermissions",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(200, config)
        create_bodies.append(json.loads(request.content))
        return _response(200, {"id": "daemon-session"})

    agent, _, _ = await _new_agent(tmp_path, handler)
    await agent.close()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert create_bodies[0]["permission_mode"] == "dontAsk"
    assert create_bodies[0]["allowed_tools"] == ["Read"]
    assert "configured bypassPermissions" in captured.err
    assert "ACP surface downgrades to dontAsk" in captured.err
    assert "agent-config allowed_tools: Read" in captured.err
    assert "ACP allowlist lacks Bash" in captured.err


@pytest.mark.asyncio
async def test_empty_agent_mode_uses_dont_ask_and_acp_default_allowlist(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("PINKY_ACP_PERMISSION_MODE", raising=False)
    create_bodies: list[dict[str, Any]] = []
    config = {**_agent_config(), "allowed_tools": [], "permission_mode": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(200, config)
        create_bodies.append(json.loads(request.content))
        return _response(200, {"id": "daemon-session"})

    agent, _, _ = await _new_agent(tmp_path, handler)
    await agent.close()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert create_bodies[0]["permission_mode"] == "dontAsk"
    assert create_bodies[0]["allowed_tools"] == ACP_DEFAULT_ALLOWED_TOOLS
    assert "has no configured permission mode" in captured.err
    assert "ACP surface uses dontAsk" in captured.err
    assert "acp-default allowed_tools" in captured.err
    assert "ACP allowlist lacks Bash" not in captured.err


@pytest.mark.asyncio
async def test_acp_permission_env_override_allows_deliberate_bypass(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PINKY_ACP_PERMISSION_MODE", "bypassPermissions")
    create_bodies: list[dict[str, Any]] = []
    config = {**_agent_config(), "permission_mode": "bypassPermissions"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(200, config)
        create_bodies.append(json.loads(request.content))
        return _response(200, {"id": "daemon-session"})

    agent, _, _ = await _new_agent(tmp_path, handler)
    await agent.close()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert create_bodies[0]["permission_mode"] == "bypassPermissions"
    assert "PINKY_ACP_PERMISSION_MODE" in captured.err
    assert "downgrades to dontAsk" not in captured.err


@pytest.mark.asyncio
async def test_prompt_emits_keepalive_while_daemon_turn_is_running(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(200, _agent_config())
        if request.url.path == "/sessions":
            return _response(200, {"id": "daemon-session"})
        started.set()
        await release.wait()
        return _response(200, {"content": "finished", "error": ""})

    agent, recorder, session_id = await _new_agent(
        tmp_path,
        handler,
        keepalive_interval=0.01,
    )
    prompt_task = asyncio.create_task(agent.prompt(session_id, [acp.text_block("slow")]))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        while not recorder.updates:
            await asyncio.sleep(0.005)
        release.set()
        result = await asyncio.wait_for(prompt_task, timeout=1)
    finally:
        release.set()
        await agent.close()

    assert result.stop_reason == "end_turn"
    assert recorder.updates[0][1].session_update == "agent_thought_chunk"
    assert recorder.updates[0][1].content.text == "Still working…"
    assert recorder.updates[-1][1].session_update == "agent_message_chunk"
    assert recorder.updates[-1][1].content.text == "finished"


@pytest.mark.asyncio
async def test_cancel_detaches_inflight_daemon_turn_and_returns_cancelled(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(200, _agent_config())
        if request.url.path == "/sessions":
            return _response(200, {"id": "daemon-session"})
        started.set()
        await release.wait()
        return _response(200, {"content": "late answer", "error": ""})

    agent, recorder, session_id = await _new_agent(tmp_path, handler)
    prompt_task = asyncio.create_task(agent.prompt(session_id, [acp.text_block("cancel me")]))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await agent.cancel(session_id)
        result = await asyncio.wait_for(prompt_task, timeout=1)
        assert agent._detached_tasks
        release.set()
        while agent._detached_tasks:
            await asyncio.sleep(0)
    finally:
        release.set()
        await agent.close()

    assert result.stop_reason == "cancelled"
    assert recorder.updates == []


@pytest.mark.asyncio
async def test_busy_response_retries_boundedly_without_protocol_error(tmp_path):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "GET":
            return _response(200, _agent_config())
        if request.url.path == "/sessions":
            return _response(200, {"id": "daemon-session"})
        attempts += 1
        if attempts < 3:
            return _response(409, {"detail": "busy"})
        return _response(200, {"content": "after busy", "error": ""})

    agent, recorder, session_id = await _new_agent(tmp_path, handler, busy_retries=2)
    try:
        result = await agent.prompt(session_id, [acp.text_block("overlap")])
    finally:
        await agent.close()

    assert attempts == 3
    assert result.stop_reason == "end_turn"
    assert recorder.updates[0][1].content.text == "after busy"


@pytest.mark.asyncio
async def test_busy_retry_exhaustion_returns_protocol_refusal(tmp_path):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.method == "GET":
            return _response(200, _agent_config())
        if request.url.path == "/sessions":
            return _response(200, {"id": "daemon-session"})
        attempts += 1
        return _response(409, {"detail": "busy"})

    agent, recorder, session_id = await _new_agent(tmp_path, handler, busy_retries=2)
    try:
        result = await agent.prompt(session_id, [acp.text_block("overlap")])
    finally:
        await agent.close()

    assert attempts == 3
    assert result.stop_reason == "refusal"
    assert len(recorder.updates) == 1
    assert recorder.updates[0][1].session_update == "agent_message_chunk"
    assert "remained busy after 3 attempts" in recorder.updates[0][1].content.text


@pytest.mark.asyncio
async def test_mcp_servers_warn_to_stderr_and_are_ignored(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(200, _agent_config())
        return _response(200, {"id": "daemon-session"})

    daemon = _client(handler)
    agent = PinkyAcpAgent(
        agent_name="tester",
        daemon=daemon,
        project_root=tmp_path,
    )
    agent.on_connect(RecordingClient())
    try:
        await agent.new_session(cwd="/client/cwd", mcp_servers=[object()])
    finally:
        await agent.close()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ignoring 1 client MCP server" in captured.err


def test_session_secret_environment_then_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("PINKY_SESSION_SECRET", raising=False)
    (tmp_path / ".env").write_text("OTHER=value\nPINKY_SESSION_SECRET='from-dotenv'\n")
    assert _read_session_secret(tmp_path) == "from-dotenv"

    monkeypatch.setenv("PINKY_SESSION_SECRET", "from-environment")
    assert _read_session_secret(tmp_path) == "from-environment"
