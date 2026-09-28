"""Background tool hooks retain telemetry without claiming the main pane."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import create_api
from pinky_daemon.scheduler import AgentScheduler
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession, _InflightMeta, _QueuedTurn
from pinky_daemon.transport import SessionState


@pytest.fixture
def hook_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def run_hook(path, raw, url):
    # Only synthetic credentials and the local stub are visible to the child.
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR") if key in os.environ}
    env.update(PINKY_AGENT_KEY="synthetic-test-key", PINKY_DAEMON_URL=url)
    result = subprocess.run(
        [sys.executable, str(path)],
        input=raw,
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "raw,posts",
    [
        ('{"agent_id":"a1","agent_type":"general-purpose","tool_name":"Bash"}', False),
        ('{"tool_name":"Bash"}', True),
        ("", True),
        ("invalid json", True),
        ("null", True),
        ("[]", True),
        ('{"agent_id":""}', True),
        ('{"agent_id":null}', True),
        ('{"agent_id":42}', True),
        ('{"agent_type":"general-purpose"}', True),
    ],
)
def test_working_hook_main_status_only(tmp_path, hook_server, raw, posts):
    url, received = hook_server
    AgentRegistry._setup_hooks(tmp_path, "sample")
    run_hook(tmp_path / ".claude/hook_working.py", raw, url)
    assert received == ([("/agents/sample/status", {"status": "working"})] if posts else [])


@pytest.mark.parametrize("phase,endpoint", [("pre", "tool-use"), ("post", "tool-result")])
def test_tool_hooks_forward_subagent_identity(tmp_path, hook_server, phase, endpoint):
    url, received = hook_server
    AgentRegistry._setup_hooks(tmp_path, "sample")
    payload = dict(
        agent_id="a1",
        agent_type="general-purpose",
        tool_name="Bash",
        tool_use_id="child-call",
        tool_input={},
        tool_response="done",
    )
    run_hook(tmp_path / f".claude/hook_tmux_{phase}_tool.py", json.dumps(payload), url)
    assert len(received) == 1
    path, body = received[0]
    assert path == f"/agents/sample/transport/{endpoint}"
    assert body.get("agent_id") == "a1"
    assert body.get("agent_type") == "general-purpose"


def test_old_working_hook_upgrades_and_suppresses_subagent(tmp_path, hook_server):
    url, received = hook_server
    old = (Path(__file__).parent / "fixtures/generated_hooks/hook_working.py.txt").read_bytes()
    directory = tmp_path / ".claude"
    directory.mkdir()
    hook = directory / "hook_working.py"
    hook.write_bytes(old)
    AgentRegistry._setup_hooks(tmp_path, "sample")
    assert hook.read_bytes() != old
    run_hook(hook, '{"agent_id":"child"}', url)
    assert received == []
    before = hook.stat().st_mtime_ns
    AgentRegistry._setup_hooks(tmp_path, "sample")
    assert hook.stat().st_mtime_ns == before


@pytest.fixture
def transport_api(tmp_path, monkeypatch):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "state.db"))
    app.state.agents.register("sample", working_dir=str(tmp_path / "sample"), transport="tmux")
    events = []
    analytics = MagicMock()
    config = StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path))
    idle_stamp = time.time()
    config.live_status_fn = lambda: {"status": "idle", "last_updated": idle_stamp}
    session = TmuxSession(
        config,
        tmux_control=MagicMock(),
        analytics_store=analytics,
        stream_event_callback=events.append,
    )
    session._state_machine._state = SessionState.CONNECTED
    # A prior accepted paste is older than the authoritative idle receipt.
    session._inflight_metas.append(
        _InflightMeta(
            meta={},
            completion_event=None,
            internal=False,
            dispatched_at=time.time() - 10,
            turn=_QueuedTurn(prompt="earlier turn", transport_accepted=True),
        )
    )
    monkeypatch.setattr(app.state.broker, "get_streaming_session", lambda *a, **kw: session)
    client = TestClient(app)
    try:
        yield client, session, analytics, events, app.state.agents
    finally:
        client.close()
        app.state.agents.close()


def post_start(client, **extra):
    payload = dict(tool_use_id="call-1", tool_name="Bash", tool_input={"command": "true"})
    payload.update(extra)
    response = client.post("/agents/sample/transport/tool-use", json=payload)
    assert response.status_code == 200, response.text


def post_finish(client, **extra):
    payload = dict(tool_use_id="call-1", tool_name="Bash", tool_response="done")
    payload.update(extra)
    response = client.post("/agents/sample/transport/tool-result", json=payload)
    assert response.status_code == 200, response.text


def test_subagent_start_preserves_main_inflight(transport_api):
    client, session, analytics, events, _ = transport_api
    session._inflight_tool_calls["main-call"] = 123.0
    post_start(client, agent_id="child", agent_type="general-purpose")
    assert session._inflight_tool_calls == {"main-call": 123.0}
    assert len(session._activity_log) == 1
    assert "child" in session._activity_log[-1]
    meta = analytics.start_tool_call.call_args.kwargs["metadata"]
    assert meta["agent_id"] == "child"
    assert meta["agent_type"] == "general-purpose"
    assert events[-1]["agent_id"] == "child"
    assert events[-1]["agent_type"] == "general-purpose"


def test_subagent_start_preserves_current_activity_and_effort(transport_api):
    client, session, _, _, _ = transport_api
    session._current_activity = "main is waiting"
    session.last_reported_effort = "high"
    post_start(client, agent_id="child", agent_type="general-purpose", effort="low")
    assert session._current_activity == "main is waiting"
    assert session.last_reported_effort == "high"


def test_subagent_finish_cannot_clear_main_call(transport_api):
    client, session, analytics, events, _ = transport_api
    post_start(client, tool_name="Agent")
    before = dict(session._inflight_tool_calls)
    post_finish(client, agent_id="child", agent_type="general-purpose")
    assert session._inflight_tool_calls == before
    meta = analytics.finish_tool_call.call_args.kwargs["metadata"]
    assert meta["agent_id"] == "child"
    assert meta["agent_type"] == "general-purpose"
    assert events[-1]["agent_id"] == "child"
    assert events[-1]["agent_type"] == "general-purpose"
    assert session.scheduler_drain_busy() is True


@pytest.mark.parametrize("tool_name", ["Bash", "Agent"])
def test_main_tool_stays_busy_until_finish(transport_api, tool_name):
    client, session, analytics, events, _ = transport_api
    assert session.scheduler_drain_busy() is False
    post_start(client, tool_name=tool_name, effort="high")
    assert "call-1" in session._inflight_tool_calls
    assert session._current_activity
    assert session.last_reported_effort == "high"
    assert session.scheduler_drain_busy() is True
    post_finish(client, tool_name=tool_name)
    assert session._inflight_tool_calls == {}
    assert session.scheduler_drain_busy() is False
    analytics.start_tool_call.assert_called_once()
    analytics.finish_tool_call.assert_called_once()
    assert [e["type"] for e in events] == ["tool_use_start", "tool_use_finish"]


@pytest.mark.asyncio
async def test_subagent_tool_allows_persisted_wake_replay(transport_api):
    client, session, _, _, registry = transport_api
    schedule = registry.add_schedule("sample", "* * * * *", name="pending", prompt="wake")
    registry.persist_schedule_wake(
        schedule.id,
        agent_name="sample",
        schedule_name="pending",
        prompt="wake",
        fired_at=time.time() - 5,
    )
    wake = AsyncMock(return_value=True)
    scheduler = AgentScheduler(
        registry,
        wake_callback=wake,
        delivery_drain_busy_fn=lambda *a: session.scheduler_drain_busy(),
    )
    try:
        assert session.scheduler_drain_busy() is False
        post_start(client, agent_id="child", agent_type="general-purpose")
        assert session.scheduler_drain_busy() is False
        scheduler.replay_pending_for_agent("sample", drain_recheck=True)
        await scheduler._pending_replay_tasks["sample"]
        wake.assert_awaited_once()
        assert registry.list_pending_schedule_wakes("sample") == []
    finally:
        await scheduler.stop()
