"""Tests for pinky_daemon sessions and API."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import DEFAULT_HEARTBEAT_PROMPT
from pinky_daemon.broker import BrokerMessage
from pinky_daemon.claude_runner import RunResult
from pinky_daemon.sessions import (
    Checkpoint,
    ContextStatus,
    Session,
    SessionManager,
    SessionMessage,
    SessionState,
)

# ── SessionMessage ───────────────────────────────────────────


class TestSessionMessage:
    def test_create(self):
        msg = SessionMessage(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.duration_ms == 0
        assert msg.error == ""

    def test_timestamp_auto(self):
        msg = SessionMessage(role="assistant", content="Hi")
        assert msg.timestamp > 0


# ── Session ──────────────────────────────────────────────────


class TestSession:
    def test_create_default(self):
        session = Session()
        assert session.id.startswith("pinky-")
        assert session.state == SessionState.idle
        assert session.message_count == 0

    def test_create_custom_id(self):
        session = Session(session_id="my-session")
        assert session.id == "my-session"

    def test_create_with_model(self):
        session = Session(model="opus")
        assert session.model == "opus"

    def test_info(self):
        session = Session(session_id="test", model="sonnet")
        info = session.info
        assert info.id == "test"
        assert info.model == "sonnet"
        assert info.state == SessionState.idle
        assert info.message_count == 0

    def test_info_to_dict(self):
        session = Session(session_id="test")
        d = session.info.to_dict()
        assert d["id"] == "test"
        assert d["state"] == "idle"
        assert isinstance(d["created_at"], float)

    @pytest.mark.asyncio
    async def test_send_message(self):
        session = Session(session_id="test")

        # Mock the runner
        session._runner.run = AsyncMock(
            return_value=RunResult(output="Hello back!", exit_code=0)
        )

        msg = await session.send("Hello")
        assert msg.role == "assistant"
        assert msg.content == "Hello back!"
        assert msg.duration_ms >= 0
        assert session.message_count == 2  # user + assistant
        assert session.state == SessionState.idle

    @pytest.mark.asyncio
    async def test_send_resumes_after_first(self):
        session = Session(session_id="test")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="ok", exit_code=0)
        )

        await session.send("First message")
        await session.send("Second message")

        # First call should not resume, second should
        calls = session._runner.run.call_args_list
        assert calls[0][1]["resume"] is False
        assert calls[1][1]["resume"] is True

    @pytest.mark.asyncio
    async def test_send_system_prompt_first_only(self):
        session = Session(session_id="test", system_prompt="Be helpful")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="ok", exit_code=0)
        )

        await session.send("First")
        await session.send("Second")

        calls = session._runner.run.call_args_list
        assert calls[0][1]["system_prompt"] == "Be helpful"
        assert calls[1][1]["system_prompt"] == ""

    @pytest.mark.asyncio
    async def test_send_error(self):
        session = Session(session_id="test")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="", exit_code=1, error="crash")
        )

        msg = await session.send("Hello")
        assert msg.error == "crash"
        assert session.state == SessionState.error

    def test_get_history_empty(self):
        session = Session()
        assert session.get_history() == []

    @pytest.mark.asyncio
    async def test_get_history_with_messages(self):
        session = Session(session_id="test")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="response", exit_code=0)
        )

        await session.send("Hello")
        history = session.get_history()

        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Hello"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == "response"

    def test_close(self):
        session = Session()
        session.close()
        assert session.state == SessionState.closed


class TestStreamingSession:
    def test_sub_session_id_uses_label(self):
        from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig

        session = StreamingSession(
            StreamingSessionConfig(agent_name="test-agent", label="worker")
        )

        assert session.id == "test-agent-worker"

    @pytest.mark.asyncio
    async def test_failed_send_clears_pending_route(self):
        from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig

        session = StreamingSession(StreamingSessionConfig(agent_name="test-agent"))
        session._connected = True

        class FailingClient:
            async def query(self, prompt):
                raise RuntimeError("boom")

        session._client = FailingClient()
        session._try_reconnect = AsyncMock()

        await session.send("hello", platform="telegram", chat_id="chat-1")

        assert session._pending_chats == []
        session._try_reconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reader_loop_reports_outreach_tool_only_turn(self):
        from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig

        fake_types = ModuleType("claude_agent_sdk.types")

        class TextBlock:
            def __init__(self, text):
                self.text = text

        class ToolUseBlock:
            def __init__(self, name, input):
                self.name = name
                self.input = input

        class ToolResultBlock:
            def __init__(self, content="", is_error=False):
                self.content = content
                self.is_error = is_error

        class AssistantMessage:
            def __init__(self, content, usage=None, session_id="sdk-session", error=""):
                self.content = content
                self.usage = usage or {}
                self.session_id = session_id
                self.error = error
                self.stop_reason = None

        class ResultMessage:
            def __init__(self):
                self.num_turns = 1
                self.total_cost_usd = 0.01
                self.model_usage = {"sonnet": {"output_tokens": 10}}
                self.usage = {"input_tokens": 5, "output_tokens": 10}
                self.is_error = False
                self.stop_reason = None
                self.errors = []

        class ThinkingBlock:
            def __init__(self, thinking=""):
                self.thinking = thinking

        fake_types.TextBlock = TextBlock
        fake_types.ThinkingBlock = ThinkingBlock
        fake_types.ToolUseBlock = ToolUseBlock
        fake_types.ToolResultBlock = ToolResultBlock
        fake_types.AssistantMessage = AssistantMessage
        fake_types.ResultMessage = ResultMessage

        old_sdk_types = sys.modules.get("claude_agent_sdk.types")
        sys.modules["claude_agent_sdk.types"] = fake_types

        callback = AsyncMock()
        session = StreamingSession(
            StreamingSessionConfig(agent_name="test-agent"),
            response_callback=callback,
        )
        session._connected = True
        session._pending_chats.append(("telegram", "chat-1", "msg-1"))

        class FakeClient:
            async def receive_messages(self):
                yield AssistantMessage([
                    ToolUseBlock("thread", {"message_id": "msg-1", "text": "hi"}),
                    ToolResultBlock('{"sent": true}', False),
                ])
                yield ResultMessage()

        session._client = FakeClient()

        try:
            await session._reader_loop()
        finally:
            if old_sdk_types is not None:
                sys.modules["claude_agent_sdk.types"] = old_sdk_types
            else:
                sys.modules.pop("claude_agent_sdk.types", None)

        callback.assert_awaited_once()
        turn_result = callback.await_args.args[0]
        assert turn_result.platform == "telegram"
        assert turn_result.chat_id == "chat-1"
        assert turn_result.message_id == "msg-1"
        assert turn_result.response_text == ""
        assert turn_result.used_outreach_tools is True
        assert turn_result.tool_uses[0]["tool"] == "thread"

    @pytest.mark.asyncio
    async def test_force_restart_blocks_when_guard_fails(self):
        from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig

        client = SimpleNamespace(query=AsyncMock())
        session = StreamingSession(
            StreamingSessionConfig(
                agent_name="test-agent",
                restart_guard=lambda _session: {
                    "restart_safe": False,
                    "reason": "missing_explicit_save",
                    "message": "Restart blocked: call save_my_context() first.",
                },
            )
        )
        session._connected = True
        session._client = client
        session.disconnect = AsyncMock()
        session.connect = AsyncMock()

        restarted = await session.force_restart()

        assert restarted is False
        session.disconnect.assert_not_awaited()
        session.connect.assert_not_awaited()
        client.query.assert_awaited_once()


# ── SessionManager ───────────────────────────────────────────


class TestSessionManager:
    def test_create(self):
        mgr = SessionManager()
        session = mgr.create()
        assert session is not None
        assert mgr.count == 1

    def test_get(self):
        mgr = SessionManager()
        session = mgr.create(session_id="abc")
        got = mgr.get("abc")
        assert got is not None
        assert got.id == "abc"

    def test_get_missing(self):
        mgr = SessionManager()
        assert mgr.get("nope") is None

    def test_list(self):
        mgr = SessionManager()
        mgr.create(session_id="a")
        mgr.create(session_id="b")
        sessions = mgr.list()
        assert len(sessions) == 2

    def test_list_excludes_closed(self):
        mgr = SessionManager()
        s = mgr.create(session_id="a")
        mgr.create(session_id="b")
        s.close()
        sessions = mgr.list()
        assert len(sessions) == 1
        assert sessions[0].id == "b"

    def test_delete(self):
        mgr = SessionManager()
        mgr.create(session_id="a")
        assert mgr.delete("a") is True
        assert mgr.count == 0

    def test_delete_missing(self):
        mgr = SessionManager()
        assert mgr.delete("nope") is False

    def test_eviction(self):
        mgr = SessionManager(max_sessions=2)
        s1 = mgr.create(session_id="old")
        s1.last_active = time.time() - 1000  # Make it old
        mgr.create(session_id="new1")

        # This should evict "old"
        mgr.create(session_id="new2")
        assert mgr.get("old") is None
        assert mgr.count == 2

    def test_create_with_params(self):
        mgr = SessionManager()
        session = mgr.create(
            session_id="custom",
            model="opus",
            soul="# My AI",
            allowed_tools=["Read"],
        )
        assert session.id == "custom"
        assert session.model == "opus"
        assert session.soul == "# My AI"
        assert session.allowed_tools == ["Read"]


# ── API ──────────────────────────────────────────────────────


class TestAPI:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def _make_app(self, path: str):
        from pinky_daemon.api import create_api
        return create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)

    class _FakeContextClient:
        def __init__(self, total_tokens=0, max_tokens=200_000):
            self.total_tokens = total_tokens
            self.max_tokens = max_tokens

        async def get_context_usage(self):
            return {"totalTokens": self.total_tokens, "maxTokens": self.max_tokens}

    class _FakeStreamingSession:
        def __init__(self, agent_name: str, label: str = "main", *, connected: bool = True, total_tokens: int = 0, max_tokens: int = 200_000):
            self.agent_name = agent_name
            self.label = label
            self.session_id = f"{agent_name}-{label}-sdk"
            self.created_at = time.time()
            self.last_active = self.created_at
            self.is_connected = connected
            self._stats = {"messages_sent": 2, "turns": 3, "errors": 0, "reconnects": 0, "auto_restarts": 0}
            self._config = SimpleNamespace(model="sonnet", context_restart_pct=80, permission_mode="bypassPermissions")
            self.usage = SimpleNamespace(total_cost_usd=0.0, input_tokens=0, output_tokens=0)
            self._client = TestAPI._FakeContextClient(total_tokens=total_tokens, max_tokens=max_tokens) if connected else None
            self.sent: list[tuple[str, str, str]] = []
            self.disconnect_calls = 0
            self.connect_calls = 0

        @property
        def id(self) -> str:
            return f"{self.agent_name}-{self.label}"

        @property
        def stats(self) -> dict:
            return {**self._stats, "connected": self.is_connected, "pending_responses": 0, "cost_usd": 0.0, "account": {}}

        async def send(self, prompt: str, platform: str = "", chat_id: str = ""):
            self.sent.append((prompt, platform, chat_id))

        async def disconnect(self):
            self.disconnect_calls += 1
            self.is_connected = False

        async def connect(self):
            self.connect_calls += 1
            self.is_connected = True

    def test_root(self):
        client = self._make_client()
        resp = client.get("/api")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "pinky"
        assert data["sessions"] == 0

    def test_create_session(self):
        client = self._make_client()
        resp = client.post("/sessions", json={"model": "sonnet"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "idle"
        assert data["model"] == "sonnet"
        assert "id" in data

    def test_create_session_custom_id(self):
        client = self._make_client()
        resp = client.post("/sessions", json={"session_id": "my-session"})
        assert resp.status_code == 200
        assert resp.json()["id"] == "my-session"

    def test_get_heartbeat_settings_includes_prompt(self):
        client = self._make_client()
        resp = client.get("/settings/heartbeat")
        assert resp.status_code == 200
        data = resp.json()
        assert data["heartbeat_prompt"] == DEFAULT_HEARTBEAT_PROMPT

    def test_update_heartbeat_prompt(self):
        client = self._make_client()
        resp = client.put("/settings/heartbeat/prompt", json={
            "prompt": "Check for messages, otherwise reply HEARTBEAT_OK.",
        })
        assert resp.status_code == 200
        assert resp.json()["heartbeat_prompt"] == "Check for messages, otherwise reply HEARTBEAT_OK."

        settings = client.get("/settings/heartbeat")
        assert settings.status_code == 200
        assert settings.json()["heartbeat_prompt"] == "Check for messages, otherwise reply HEARTBEAT_OK."

    def test_update_heartbeat_prompt_rejects_blank(self):
        client = self._make_client()
        resp = client.put("/settings/heartbeat/prompt", json={"prompt": "   "})
        assert resp.status_code == 400

    def test_create_session_defaults(self):
        client = self._make_client()
        resp = client.post("/sessions", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "idle"
        assert data["id"].startswith("pinky-")

    def test_list_sessions(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "a"})
        client.post("/sessions", json={"session_id": "b"})

        resp = client.get("/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_list_sessions_excludes_streaming_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/sessions", json={"session_id": "adhoc"})
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming("test-agent", fake, label="main")

                resp = client.get("/sessions")
                assert resp.status_code == 200
                data = resp.json()
                assert len(data) == 1
                assert data[0]["id"] == "adhoc"

    def test_sleep_disconnects_streaming_main(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                app.state.agents.set_streaming_session_id("test-agent", "persisted-main", label="main")
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming("test-agent", fake, label="main")
                app.state.agents.set_context(
                    "test-agent",
                    task="Ready for sleep",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.session_id,
                )

                resp = client.post("/agents/test-agent/sleep")
                assert resp.status_code == 200
                assert resp.json()["status"] == "sleeping"
                assert fake.disconnect_calls == 1
                assert app.state.broker._streaming.get("test-agent") is None
                assert app.state.agents.get_streaming_session_id("test-agent", label="main") == ""

    def test_sleep_requires_recent_explicit_context_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming("test-agent", fake, label="main")

                resp = client.post("/agents/test-agent/sleep")
                assert resp.status_code == 409
                assert "save_my_context" in resp.text
                assert fake.disconnect_calls == 0

    def test_health_prefers_streaming_main(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                app.state.manager.create(session_id="test-agent-main", session_type="main", agent_name="test-agent")
                fake = self._FakeStreamingSession("test-agent", "main", total_tokens=180_000)
                app.state.broker.register_streaming("test-agent", fake, label="main")

                resp = client.get("/agents/test-agent/health")
                assert resp.status_code == 200
                data = resp.json()
                assert data["session"]["streaming"] is True
                assert data["session"]["id"] == "test-agent-main"
                assert data["session"]["needs_restart"] is True
                assert data["legacy_session"]["streaming"] is False

    def test_wake_creates_streaming_session_and_sends(self):
        sent_prompts = []

        async def fake_connect(self):
            self._connected = True
            if not self.session_id:
                self.session_id = f"{self.agent_name}-sdk"
            if self._on_session_id:
                await self._on_session_id(self.agent_name, self.session_id)

        async def fake_send(self, prompt: str, platform: str = "", chat_id: str = ""):
            sent_prompts.append((self.agent_name, prompt, platform, chat_id))

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.streaming_session.StreamingSession.connect", new=fake_connect), \
                patch("pinky_daemon.streaming_session.StreamingSession.send", new=fake_send):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})

                resp = client.post("/agents/test-agent/wake?prompt=Wake+up")
                assert resp.status_code == 200
                data = resp.json()
                assert data["sent"] is True
                assert data["connected"] is True
                assert "test-agent" in app.state.broker._streaming
                assert app.state.broker._streaming["test-agent"]["main"].is_connected is True
                assert sent_prompts[-1][1] == "Wake up"

    def test_streaming_restart_requires_explicit_current_session_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming("test-agent", fake, label="main")

                resp = client.post("/agents/test-agent/streaming/restart")
                assert resp.status_code == 409
                assert "save_my_context" in resp.text
                assert fake.disconnect_calls == 0

    def test_streaming_restart_blocks_when_save_is_too_old_for_activity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming("test-agent", fake, label="main")

                app.state.agents.set_context(
                    "test-agent",
                    task="Testing restart guard",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.session_id,
                )
                stale_ts = time.time() - 601
                app.state.agents._db.execute(
                    "UPDATE agent_contexts SET updated_at=? WHERE agent_name=?",
                    (stale_ts, "test-agent"),
                )
                app.state.agents._db.commit()
                fake.last_active = time.time()

                resp = client.post("/agents/test-agent/streaming/restart")
                assert resp.status_code == 409
                assert "5 minutes" in resp.text
                assert fake.disconnect_calls == 0

    def test_streaming_restart_allows_recent_current_session_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming("test-agent", fake, label="main")

                app.state.agents.set_context(
                    "test-agent",
                    task="Testing restart guard",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.session_id,
                )
                fake.last_active = time.time()

                resp = client.post("/agents/test-agent/streaming/restart")
                assert resp.status_code == 200
                assert resp.json()["restarted"] is True
                assert fake.disconnect_calls == 1
                assert fake.connect_calls == 1

    def test_wake_streaming_session_defaults_include_outreach_tools(self):
        async def fake_connect(self):
            self._connected = True

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.streaming_session.StreamingSession.connect", new=fake_connect):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})

                resp = client.post("/agents/barsik/wake?prompt=Wake")
                assert resp.status_code == 200

                session = app.state.broker._streaming["barsik"]["main"]
                assert "mcp__pinky-messaging__*" in session._config.allowed_tools
                assert "mcp__pinky-self__*" in session._config.allowed_tools
                assert "Read" in session._config.allowed_tools

    def test_wake_streaming_session_preserves_agent_allowed_tools(self):
        async def fake_connect(self):
            self._connected = True

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.streaming_session.StreamingSession.connect", new=fake_connect):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={
                    "name": "barsik",
                    "model": "sonnet",
                    "allowed_tools": ["Read", "mcp__pinky-outreach__*"],
                })

                resp = client.post("/agents/barsik/wake?prompt=Wake")
                assert resp.status_code == 200

                session = app.state.broker._streaming["barsik"]["main"]
                # Agent's configured tools are merged with defaults + skill patterns;
                # verify the agent-specific tools are present in the effective set.
                assert "Read" in session._config.allowed_tools
                assert "mcp__pinky-outreach__*" in session._config.allowed_tools

    def test_manual_streaming_session_persists_and_restores_labels(self):
        async def fake_connect(self):
            self._connected = True
            if not self.session_id:
                self.session_id = f"{self.agent_name}-sdk"
            if self._on_session_id:
                await self._on_session_id(self.agent_name, self.session_id)

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.streaming_session.StreamingSession.connect", new=fake_connect):
            db_path = os.path.join(tmpdir, "test.db")
            app1 = self._make_app(db_path)
            with TestClient(app1) as client1:
                client1.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                resp = client1.post("/agents/test-agent/streaming-sessions?label=worker")
                assert resp.status_code == 200
                assert app1.state.agents.get_streaming_session_id("test-agent", label="worker") == "test-agent-sdk"

            app2 = self._make_app(db_path)
            with TestClient(app2) as client2:
                resp = client2.get("/agents/test-agent/streaming-sessions")
                assert resp.status_code == 200
                labels = {item["label"] for item in resp.json()["sessions"]}
                # Only main restarts on boot — sub-sessions are on-demand
                assert "main" in labels
                assert "worker" not in labels

    def test_get_session(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "test"})

        resp = client.get("/sessions/test")
        assert resp.status_code == 200
        assert resp.json()["id"] == "test"

    def test_get_session_not_found(self):
        client = self._make_client()
        resp = client.get("/sessions/nope")
        assert resp.status_code == 404

    def test_delete_session(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "test"})

        resp = client.delete("/sessions/test")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Should be gone
        resp = client.get("/sessions/test")
        assert resp.status_code == 404

    def test_delete_not_found(self):
        client = self._make_client()
        resp = client.delete("/sessions/nope")
        assert resp.status_code == 404

    def test_get_history_empty(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "test"})

        resp = client.get("/sessions/test/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["messages"] == []

    def test_get_history_not_found(self):
        client = self._make_client()
        resp = client.get("/sessions/nope/history")
        assert resp.status_code == 404

    def test_send_message_not_found(self):
        client = self._make_client()
        resp = client.post(
            "/sessions/nope/message",
            json={"content": "Hello"},
        )
        assert resp.status_code == 404

    def test_session_count_updates(self):
        client = self._make_client()
        assert client.get("/api").json()["sessions"] == 0

        client.post("/sessions", json={})
        assert client.get("/api").json()["sessions"] == 1

        client.post("/sessions", json={})
        assert client.get("/api").json()["sessions"] == 2

    def test_get_context(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "test"})

        resp = client.get("/sessions/test/context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "test"
        assert data["estimated_tokens"] == 0
        assert data["max_tokens"] > 0
        assert data["context_used_pct"] == 0.0
        assert data["needs_restart"] is False
        assert data["checkpoints"] == 0
        assert data["last_checkpoint_at"] is None

    def test_get_context_not_found(self):
        client = self._make_client()
        resp = client.get("/sessions/nope/context")
        assert resp.status_code == 404

    def test_restart_session(self):
        client = self._make_client()
        client.post("/sessions", json={"session_id": "test"})

        resp = client.post("/sessions/test/restart")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "test"
        assert data["restart_number"] == 1

    def test_restart_not_found(self):
        client = self._make_client()
        resp = client.post("/sessions/nope/restart")
        assert resp.status_code == 404

    def test_create_with_auto_restart(self):
        client = self._make_client()
        resp = client.post("/sessions", json={
            "session_id": "test",
            "restart_threshold_pct": 70.0,
            "auto_restart": True,
        })
        assert resp.status_code == 200

    def test_context_used_pct_in_session_response(self):
        client = self._make_client()
        resp = client.post("/sessions", json={"session_id": "test"})
        data = resp.json()
        assert "context_used_pct" in data

    def test_register_agent_defaults_plain_text_fallback_disabled(self):
        client = self._make_client()
        resp = client.post("/agents", json={"name": "barsik", "model": "sonnet"})
        assert resp.status_code == 200
        assert resp.json()["plain_text_fallback"] is False

    def test_broker_thread_records_outbound_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                app.state.broker.remember_message_context(
                    BrokerMessage(
                        platform="telegram",
                        chat_id="6770805286",
                        sender_name="Brad",
                        sender_id="u1",
                        content="Hello",
                        agent_name="barsik",
                        message_id="101",
                    )
                )

                with patch("pinky_outreach.telegram.TelegramAdapter.send_message", return_value=SimpleNamespace(message_id="501")):
                    resp = client.post("/broker/thread", json={
                        "agent_name": "barsik",
                        "message_id": "101",
                        "content": "On it",
                    })

                assert resp.status_code == 200
                assert resp.json()["message_id"] == "501"

                history = app.state.conversation_store.get_history("barsik-main")
                assert history[-1].content == "On it"
                assert history[-1].metadata["tool"] == "thread"
                assert history[-1].metadata["source_message_id"] == "101"

    def test_broker_thread_voice_context_auto_uses_voice_reply(self):
        class _UrlResp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"audio-bytes"

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.register("barsik", voice_config={"voice_reply": True, "tts_provider": "openai", "tts_voice": "alloy"})
                app.state.agents.set_setting("OPENAI_API_KEY", "test-key")
                app.state.agents.set_token("barsik", "telegram", "bot123")
                app.state.broker.remember_message_context(
                    BrokerMessage(
                        platform="telegram",
                        chat_id="6770805286",
                        sender_name="Brad",
                        sender_id="u1",
                        content="voice",
                        agent_name="barsik",
                        message_id="202",
                        attachments=[{"type": "voice", "file_id": "voice-1"}],
                    ),
                    source_was_voice=True,
                )

                with patch("urllib.request.urlopen", return_value=_UrlResp()), \
                        patch("pinky_outreach.telegram.TelegramAdapter.send_voice", return_value=SimpleNamespace(message_id="voice-1")), \
                        patch("pinky_outreach.telegram.TelegramAdapter.send_message", return_value=SimpleNamespace(message_id="text-1")):
                    resp = client.post("/broker/thread", json={
                        "agent_name": "barsik",
                        "message_id": "202",
                        "content": "Auto voice reply",
                    })

                assert resp.status_code == 200
                data = resp.json()
                assert data["message_id"] == "voice-1"
                assert data["text_message_id"] == "text-1"

                history = app.state.conversation_store.get_history("barsik-main")
                assert history[-1].content == "Auto voice reply"
                assert history[-1].metadata["delivery_mode"] == "voice_auto_reply"

    def test_agent_chat_history_reads_persisted_transcripts_without_live_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "dreamer", "model": "sonnet"})
                app.state.conversation_store.append("dreamer-main", "user", "remember this")
                app.state.conversation_store.append("dreamer-main", "assistant", "stored reply")

                resp = client.get("/agents/dreamer/chat-history?limit=10")
                assert resp.status_code == 200
                data = resp.json()
                assert data["count"] == 2
                assert data["sessions_searched"] >= 1
                assert [m["content"] for m in data["messages"]] == ["stored reply", "remember this"]

    def test_manual_dream_uses_full_persisted_conversation_history(self):
        captured_prompts = []

        async def fake_run(self, prompt, **kwargs):
            captured_prompts.append(prompt)
            return RunResult(output="Dreamed successfully", exit_code=0)

        long_message = "A" * 620

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.dream_runner.SDKRunner._ensure_sdk", return_value=None), \
                patch("pinky_daemon.dream_runner.SDKRunner.run", new=fake_run):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "dreamer", "model": "sonnet"})
                app.state.conversation_store.append("dreamer-main", "user", long_message)
                app.state.conversation_store.append("dreamer-main", "assistant", "Noted.")

                resp = client.post("/agents/dreamer/dream")
                assert resp.status_code == 200
                assert resp.json()["summary"] == "Dreamed successfully"

        assert captured_prompts
        assert "<conversation_history>" in captured_prompts[0]
        assert long_message in captured_prompts[0]
        assert "Noted." in captured_prompts[0]

    def test_manual_dream_returns_no_new_history_when_transcript_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "dreamer", "model": "sonnet"})

                resp = client.post("/agents/dreamer/dream")
                assert resp.status_code == 200
                assert resp.json()["summary"] == "No new conversation history to process."


# ── Context Tracking ─────────────────────────────────────────


class TestContextTracking:
    def test_estimated_tokens_empty(self):
        session = Session(session_id="test")
        assert session.estimated_tokens == 0

    def test_estimated_tokens_with_system_prompt(self):
        session = Session(session_id="test", system_prompt="x" * 400)
        # 400 chars / 4 chars_per_token = 100
        assert session.estimated_tokens == 100

    @pytest.mark.asyncio
    async def test_estimated_tokens_after_messages(self):
        session = Session(session_id="test")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="y" * 200, exit_code=0)
        )
        await session.send("x" * 400)
        # user: 400/4=100, assistant: 200/4=50 = 150
        assert session.estimated_tokens == 150

    def test_context_used_pct(self):
        session = Session(session_id="test", system_prompt="x" * 40000)
        # 40000/4 = 10000 tokens out of 200000 = 5%
        assert 4.5 < session.context_used_pct < 5.5

    def test_needs_restart_false(self):
        session = Session(session_id="test")
        assert session.needs_restart is False

    def test_needs_restart_true(self):
        session = Session(session_id="test", restart_threshold_pct=0.001)
        session._system_prompt = "x" * 100
        assert session.needs_restart is True

    def test_max_tokens_default(self):
        session = Session(session_id="test")
        assert session.max_tokens == 200_000

    def test_get_context_status(self):
        session = Session(session_id="test")
        status = session.get_context_status()
        assert status.session_id == "test"
        assert status.estimated_tokens == 0
        assert status.checkpoints == 0
        assert status.last_checkpoint_at is None

    def test_context_status_to_dict(self):
        status = ContextStatus(
            session_id="test",
            estimated_tokens=5000,
            max_tokens=200000,
            context_used_pct=2.5,
            message_count=4,
            needs_restart=False,
            restart_threshold_pct=80.0,
            checkpoints=0,
            last_checkpoint_at=None,
        )
        d = status.to_dict()
        assert d["session_id"] == "test"
        assert d["context_used_pct"] == 2.5


# ── Checkpointing & Restart ─────────────────────────────────


class TestCheckpointing:
    @pytest.mark.asyncio
    async def test_manual_restart(self):
        session = Session(session_id="test")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="response", exit_code=0)
        )

        await session.send("Hello")
        checkpoint = await session.restart()

        assert checkpoint.message_count == 2
        assert len(session.checkpoints) == 1
        assert session.state == SessionState.idle

    @pytest.mark.asyncio
    async def test_checkpoint_summary_content(self):
        session = Session(session_id="test")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="I'm good!", exit_code=0)
        )

        await session.send("How are you?")
        checkpoint = await session.restart()

        assert "How are you?" in checkpoint.summary
        assert "I'm good!" in checkpoint.summary

    @pytest.mark.asyncio
    async def test_restart_resets_context_tracking(self):
        session = Session(session_id="test")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="x" * 1000, exit_code=0)
        )

        await session.send("x" * 1000)
        tokens_before = session.estimated_tokens
        assert tokens_before > 0

        await session.restart()

        # After restart, only the checkpoint summary counts
        # Active history should be empty
        active = session._active_history()
        assert len(active) == 0

    @pytest.mark.asyncio
    async def test_auto_restart_on_threshold(self):
        session = Session(
            session_id="test",
            restart_threshold_pct=0.001,  # Very low threshold
            auto_restart=True,
            system_prompt="x" * 100,
        )
        session._runner.run = AsyncMock(
            return_value=RunResult(output="ok", exit_code=0)
        )

        # First message triggers auto-restart since threshold is tiny
        await session.send("First")
        await session.send("Second")  # This should trigger auto-restart

        assert len(session.checkpoints) >= 1

    @pytest.mark.asyncio
    async def test_no_auto_restart_when_disabled(self):
        session = Session(
            session_id="test",
            restart_threshold_pct=0.001,
            auto_restart=False,
            system_prompt="x" * 100,
        )
        session._runner.run = AsyncMock(
            return_value=RunResult(output="ok", exit_code=0)
        )

        await session.send("First")
        await session.send("Second")

        assert len(session.checkpoints) == 0

    @pytest.mark.asyncio
    async def test_restart_preserves_session_id(self):
        session = Session(session_id="my-session")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="ok", exit_code=0)
        )

        await session.send("Hello")
        await session.restart()

        assert session.id == "my-session"

    @pytest.mark.asyncio
    async def test_multiple_restarts(self):
        session = Session(session_id="test")
        session._runner.run = AsyncMock(
            return_value=RunResult(output="ok", exit_code=0)
        )

        await session.send("msg1")
        await session.restart()
        await session.send("msg2")
        await session.restart()

        assert len(session.checkpoints) == 2
        assert session._restart_count == 2

    def test_checkpoint_dataclass(self):
        cp = Checkpoint(summary="test summary", message_count=5)
        assert cp.summary == "test summary"
        assert cp.message_count == 5
        assert cp.timestamp > 0


class TestOwnerProfileAPI:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_get_defaults(self):
        client = self._make_client()
        resp = client.get("/settings/owner-profile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == ""
        assert data["code_word"] == ""
        assert "timezone" in data

    def test_set_and_get(self):
        client = self._make_client()
        resp = client.put("/settings/owner-profile", json={
            "name": "Brad",
            "role": "dev",
            "code_word": "pineapple",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Brad"
        assert data["role"] == "dev"
        assert data["code_word"] == "pineapple"

        # Verify via GET
        resp2 = client.get("/settings/owner-profile")
        assert resp2.json()["name"] == "Brad"

    def test_partial_update(self):
        client = self._make_client()
        client.put("/settings/owner-profile", json={"name": "Brad"})
        client.put("/settings/owner-profile", json={"pronouns": "he/him"})
        data = client.get("/settings/owner-profile").json()
        assert data["name"] == "Brad"
        assert data["pronouns"] == "he/him"


# ── Agent CRUD ───────────────────────────────────────────────


class TestAgentCRUD:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_register_agent(self):
        client = self._make_client()
        resp = client.post("/agents", json={"name": "alice", "model": "sonnet"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "alice"
        assert data["model"] == "sonnet"

    def test_register_agent_with_soul(self):
        client = self._make_client()
        resp = client.post("/agents", json={
            "name": "bob",
            "model": "opus",
            "soul": "You are Bob, a helpful assistant.",
            "display_name": "Bob",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_name"] == "Bob"
        assert data["soul"] == "You are Bob, a helpful assistant."

    def test_list_agents_empty(self):
        client = self._make_client()
        resp = client.get("/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["agents"] == []

    def test_list_agents(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        client.post("/agents", json={"name": "bob", "model": "opus"})
        resp = client.get("/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        names = {a["name"] for a in data["agents"]}
        assert "alice" in names
        assert "bob" in names

    def test_get_agent(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.get("/agents/alice")
        assert resp.status_code == 200
        assert resp.json()["name"] == "alice"

    def test_get_agent_not_found(self):
        client = self._make_client()
        resp = client.get("/agents/nobody")
        assert resp.status_code == 404

    def test_update_agent(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.put("/agents/alice", json={"model": "opus", "display_name": "Alice Bot"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "opus"
        assert data["display_name"] == "Alice Bot"

    def test_update_agent_not_found(self):
        client = self._make_client()
        resp = client.put("/agents/nobody", json={"model": "opus"})
        assert resp.status_code == 404

    def test_delete_agent(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.delete("/agents/alice")
        assert resp.status_code == 200
        assert resp.json()["retired"] is True
        # Should no longer appear in active list
        list_resp = client.get("/agents")
        names = {a["name"] for a in list_resp.json()["agents"]}
        assert "alice" not in names

    def test_delete_agent_not_found(self):
        client = self._make_client()
        resp = client.delete("/agents/nobody")
        assert resp.status_code == 404

    def test_restore_retired_agent(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        client.delete("/agents/alice")
        resp = client.post("/agents/alice/restore")
        assert resp.status_code == 200
        assert resp.json()["restored"] is True
        # Should be back
        get_resp = client.get("/agents/alice")
        assert get_resp.status_code == 200

    def test_list_retired_agents(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        client.delete("/agents/alice")
        resp = client.get("/agents/retired")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["agents"][0]["name"] == "alice"

    def test_agent_working_status(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.post("/agents/alice/status", json={"status": "working"})
        assert resp.status_code == 200

    def test_agent_working_status_invalid(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.post("/agents/alice/status", json={"status": "invalid_status"})
        # 422 from Pydantic Literal validation (was 400 with manual dict check)
        assert resp.status_code in (400, 422)

    def test_agent_working_status_not_found(self):
        client = self._make_client()
        resp = client.post("/agents/nobody/status", json={"status": "idle"})
        assert resp.status_code == 404

    def test_agent_health(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.get("/agents/alice/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "session" in data
        assert "tasks" in data

    def test_agent_health_not_found(self):
        client = self._make_client()
        resp = client.get("/agents/nobody/health")
        assert resp.status_code == 404

    def test_agent_presence(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.get("/agents/alice/presence")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "streaming" in data

    def test_all_agents_presence(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.get("/agents/presence")
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data

    def test_agent_directives_crud(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        # Add directive
        resp = client.post("/agents/alice/directives", json={"directive": "Always be brief.", "priority": 1})
        assert resp.status_code == 200
        d = resp.json()
        assert d["directive"] == "Always be brief."
        directive_id = d["id"]

        # List directives
        resp = client.get("/agents/alice/directives")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

        # Toggle
        resp = client.post(f"/agents/alice/directives/{directive_id}/toggle?active=false")
        assert resp.status_code == 200

        # Delete
        resp = client.delete(f"/agents/alice/directives/{directive_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_add_directive_not_found(self):
        client = self._make_client()
        resp = client.post("/agents/nobody/directives", json={"directive": "test"})
        assert resp.status_code == 404

    def test_main_agent_setting(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.put("/settings/main-agent", json={"agent": "alice"})
        assert resp.status_code == 200
        assert resp.json()["agent"] == "alice"

        resp = client.get("/settings/main-agent")
        assert resp.status_code == 200
        assert resp.json()["agent"] == "alice"

    def test_set_main_agent_not_found(self):
        client = self._make_client()
        resp = client.put("/settings/main-agent", json={"agent": "nobody"})
        assert resp.status_code == 404


# ── Skills CRUD ──────────────────────────────────────────────


class TestSkillsCRUD:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_register_skill(self):
        client = self._make_client()
        resp = client.post("/skills", json={
            "name": "web-search",
            "description": "Search the web",
            "skill_type": "mcp_tool",
            "category": "research",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "web-search"
        assert data["category"] == "research"

    def test_list_skills(self):
        client = self._make_client()
        client.post("/skills", json={"name": "skill-a", "description": "A"})
        client.post("/skills", json={"name": "skill-b", "description": "B"})
        resp = client.get("/skills")
        assert resp.status_code == 200
        data = resp.json()
        # At least 2 custom skills + core skills seeded on startup
        assert data["count"] >= 2

    def test_get_skill(self):
        client = self._make_client()
        client.post("/skills", json={"name": "my-skill", "description": "Test skill"})
        resp = client.get("/skills/my-skill")
        assert resp.status_code == 200
        assert resp.json()["name"] == "my-skill"

    def test_get_skill_not_found(self):
        client = self._make_client()
        resp = client.get("/skills/nonexistent")
        assert resp.status_code == 404

    def test_update_skill(self):
        client = self._make_client()
        client.post("/skills", json={"name": "my-skill", "description": "Old desc"})
        resp = client.put("/skills/my-skill", json={"description": "New desc", "version": "0.2.0"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "New desc"
        assert data["version"] == "0.2.0"

    def test_update_skill_not_found(self):
        client = self._make_client()
        resp = client.put("/skills/nonexistent", json={"description": "Nope"})
        assert resp.status_code == 404

    def test_delete_skill(self):
        client = self._make_client()
        client.post("/skills", json={"name": "my-skill", "description": "Test"})
        resp = client.delete("/skills/my-skill")
        assert resp.status_code == 200
        # Should be gone
        get_resp = client.get("/skills/my-skill")
        assert get_resp.status_code == 404

    def test_skill_catalog(self):
        client = self._make_client()
        resp = client.get("/skills/catalog")
        assert resp.status_code == 200
        assert "skills" in resp.json()

    def test_skill_categories(self):
        client = self._make_client()
        resp = client.get("/skills/categories")
        assert resp.status_code == 200
        assert "categories" in resp.json()

    def test_assign_skill_to_agent(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        client.post("/skills", json={"name": "my-skill", "description": "Test", "self_assignable": True})
        resp = client.post("/agents/alice/skills/my-skill", json={"assigned_by": "user"})
        assert resp.status_code == 200

    def test_list_agent_skills(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.get("/agents/alice/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert "skills" in data

    def test_unassign_skill_from_agent(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        client.post("/skills", json={"name": "my-skill", "description": "Test"})
        client.post("/agents/alice/skills/my-skill", json={"assigned_by": "user"})
        resp = client.delete("/agents/alice/skills/my-skill")
        assert resp.status_code == 200

    def test_available_skills_for_agent(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.get("/agents/alice/skills/available")
        assert resp.status_code == 200
        data = resp.json()
        assert "skills" in data


# ── Tasks CRUD ───────────────────────────────────────────────


class TestTasksCRUD:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_create_task(self):
        client = self._make_client()
        resp = client.post("/tasks", json={"title": "Fix bug #42"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Fix bug #42"
        assert data["status"] == "pending"
        assert "id" in data

    def test_create_task_with_fields(self):
        client = self._make_client()
        resp = client.post("/tasks", json={
            "title": "Write tests",
            "description": "Add unit tests",
            "priority": "high",
            "assigned_agent": "alice",
            "created_by": "brad",
            "tags": ["testing", "ci"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["priority"] == "high"
        assert data["assigned_agent"] == "alice"

    def test_list_tasks(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "Task 1"})
        client.post("/tasks", json={"title": "Task 2"})
        resp = client.get("/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 2

    def test_list_tasks_filter_by_status(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "Task A", "status": "pending"})
        client.post("/tasks", json={"title": "Task B", "status": "in_progress"})
        resp = client.get("/tasks?status=pending")
        assert resp.status_code == 200
        for t in resp.json()["tasks"]:
            assert t["status"] == "pending"

    def test_get_task(self):
        client = self._make_client()
        create_resp = client.post("/tasks", json={"title": "My task"})
        task_id = create_resp.json()["id"]
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task"]["title"] == "My task"
        assert "subtasks" in data
        assert "comments" in data

    def test_get_task_not_found(self):
        client = self._make_client()
        resp = client.get("/tasks/99999")
        assert resp.status_code == 404

    def test_update_task(self):
        client = self._make_client()
        create_resp = client.post("/tasks", json={"title": "Old title"})
        task_id = create_resp.json()["id"]
        resp = client.put(f"/tasks/{task_id}", json={"title": "New title", "priority": "urgent"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "New title"
        assert data["priority"] == "urgent"

    def test_update_task_not_found(self):
        client = self._make_client()
        resp = client.put("/tasks/99999", json={"title": "Nope"})
        assert resp.status_code == 404

    def test_delete_task(self):
        client = self._make_client()
        create_resp = client.post("/tasks", json={"title": "Doomed task"})
        task_id = create_resp.json()["id"]
        resp = client.delete(f"/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        get_resp = client.get(f"/tasks/{task_id}")
        assert get_resp.status_code == 404

    def test_delete_task_not_found(self):
        client = self._make_client()
        resp = client.delete("/tasks/99999")
        assert resp.status_code == 404

    def test_task_stats(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "T1"})
        resp = client.get("/tasks/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_status" in data
        assert "by_agent" in data

    def test_claim_task(self):
        client = self._make_client()
        create_resp = client.post("/tasks", json={"title": "Claimable task"})
        task_id = create_resp.json()["id"]
        resp = client.post(f"/tasks/claim/{task_id}?agent_name=alice")
        assert resp.status_code == 200
        data = resp.json()
        assert data["assigned_agent"] == "alice"
        assert data["status"] == "in_progress"

    def test_claim_task_not_found(self):
        client = self._make_client()
        resp = client.post("/tasks/claim/99999?agent_name=alice")
        assert resp.status_code == 404

    def test_claim_task_already_assigned(self):
        client = self._make_client()
        create_resp = client.post("/tasks", json={"title": "Owned task", "assigned_agent": "bob"})
        task_id = create_resp.json()["id"]
        resp = client.post(f"/tasks/claim/{task_id}?agent_name=alice")
        assert resp.status_code == 409

    def test_complete_task(self):
        client = self._make_client()
        create_resp = client.post("/tasks", json={"title": "Completable"})
        task_id = create_resp.json()["id"]
        resp = client.post(f"/tasks/complete/{task_id}?agent_name=alice&summary=Done!")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    def test_complete_task_not_found(self):
        client = self._make_client()
        resp = client.post("/tasks/complete/99999")
        assert resp.status_code == 404

    def test_block_task(self):
        client = self._make_client()
        create_resp = client.post("/tasks", json={"title": "Blockable"})
        task_id = create_resp.json()["id"]
        resp = client.post(f"/tasks/block/{task_id}?agent_name=alice&reason=Waiting on dep")
        assert resp.status_code == 200
        assert resp.json()["status"] == "blocked"

    def test_block_task_not_found(self):
        client = self._make_client()
        resp = client.post("/tasks/block/99999")
        assert resp.status_code == 404

    def test_next_task(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "Available"})
        resp = client.get("/tasks/next")
        assert resp.status_code == 200
        data = resp.json()
        assert "task" in data
        assert "source" in data

    def test_next_task_for_agent(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "For alice", "assigned_agent": "alice", "status": "pending"})
        resp = client.get("/tasks/next?agent_name=alice")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task"] is not None

    def test_task_comments(self):
        client = self._make_client()
        create_resp = client.post("/tasks", json={"title": "Commentable"})
        task_id = create_resp.json()["id"]
        resp = client.post(f"/tasks/{task_id}/comments", json={"author": "alice", "content": "Looking into it"})
        assert resp.status_code == 200
        get_resp = client.get(f"/tasks/{task_id}/comments")
        assert get_resp.status_code == 200
        assert get_resp.json()["count"] >= 1


# ── Projects CRUD ────────────────────────────────────────────


class TestProjectsCRUD:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_create_project(self):
        client = self._make_client()
        resp = client.post("/projects", json={"name": "PinkyBot v2"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "PinkyBot v2"
        assert "id" in data

    def test_create_project_with_fields(self):
        client = self._make_client()
        resp = client.post("/projects", json={
            "name": "Alpha",
            "description": "First alpha",
            "repo_url": "https://github.com/example/alpha",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "First alpha"

    def test_list_projects(self):
        client = self._make_client()
        client.post("/projects", json={"name": "P1"})
        client.post("/projects", json={"name": "P2"})
        resp = client.get("/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 2

    def test_get_project(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "MyProject"})
        project_id = create_resp.json()["id"]
        resp = client.get(f"/projects/{project_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project"]["name"] == "MyProject"
        assert "tasks" in data

    def test_get_project_not_found(self):
        client = self._make_client()
        resp = client.get("/projects/99999")
        assert resp.status_code == 404

    def test_update_project(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "OldName"})
        project_id = create_resp.json()["id"]
        resp = client.put(f"/projects/{project_id}", json={"name": "NewName", "status": "active"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "NewName"

    def test_update_project_not_found(self):
        client = self._make_client()
        resp = client.put("/projects/99999", json={"name": "Nope"})
        assert resp.status_code == 404

    def test_delete_project(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "ToBeForgotten"})
        project_id = create_resp.json()["id"]
        resp = client.delete(f"/projects/{project_id}")
        assert resp.status_code == 200
        get_resp = client.get(f"/projects/{project_id}")
        assert get_resp.status_code == 404

    def test_project_hub(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "HubProject"})
        project_id = create_resp.json()["id"]
        resp = client.get(f"/projects/{project_id}/hub")
        assert resp.status_code == 200
        data = resp.json()
        assert "project" in data
        assert "milestones" in data
        assert "recent_tasks" in data

    def test_project_hub_not_found(self):
        client = self._make_client()
        resp = client.get("/projects/99999/hub")
        assert resp.status_code == 404

    def test_update_project_repo_url_and_fields(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "FieldsProject"})
        project_id = create_resp.json()["id"]

        # Update repo_url
        resp = client.put(
            f"/projects/{project_id}",
            json={"repo_url": "https://github.com/example/repo"},
        )
        assert resp.status_code == 200
        assert resp.json()["repo_url"] == "https://github.com/example/repo"

        # Update team_members
        resp = client.put(
            f"/projects/{project_id}",
            json={"team_members": [{"name": "Alice", "role": "dev", "contact": "alice@example.com"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["team_members"][0]["name"] == "Alice"

        # Update linked_assets
        resp = client.put(
            f"/projects/{project_id}",
            json={"linked_assets": [{"type": "url", "title": "Docs", "url": "https://docs.example.com",
                                     "description": ""}]},
        )
        assert resp.status_code == 200
        assert resp.json()["linked_assets"][0]["title"] == "Docs"

    def test_project_hub_sprint_progress(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "SprintProject"})
        project_id = create_resp.json()["id"]

        # Create and start a sprint
        sprint_resp = client.post(
            f"/projects/{project_id}/sprints",
            json={"name": "Sprint 1", "goal": "Ship it"},
        )
        sprint_id = sprint_resp.json()["id"]
        client.post(f"/sprints/{sprint_id}/start")

        # Create tasks assigned to the sprint
        t1 = client.post("/tasks", json={"title": "Task A", "project_id": project_id,
                                         "sprint_id": sprint_id}).json()
        client.post("/tasks", json={"title": "Task B", "project_id": project_id,
                                    "sprint_id": sprint_id})
        # Complete one task
        client.put(f"/tasks/{t1['id']}", json={"status": "completed"})

        hub_resp = client.get(f"/projects/{project_id}/hub")
        assert hub_resp.status_code == 200
        hub = hub_resp.json()
        assert hub["active_sprint"] is not None
        assert "progress_pct" in hub["active_sprint"]
        assert hub["active_sprint"]["tasks_total"] == 2
        assert hub["active_sprint"]["tasks_completed"] == 1
        assert hub["active_sprint"]["progress_pct"] == 50

    def test_project_hub_milestone_progress(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "MilestoneProject"})
        project_id = create_resp.json()["id"]

        ms_resp = client.post(
            f"/projects/{project_id}/milestones", json={"name": "M1"}
        )
        milestone_id = ms_resp.json()["id"]

        t1 = client.post("/tasks", json={"title": "T1", "project_id": project_id,
                                         "milestone_id": milestone_id}).json()
        client.post("/tasks", json={"title": "T2", "project_id": project_id,
                                    "milestone_id": milestone_id})
        client.put(f"/tasks/{t1['id']}", json={"status": "completed"})

        hub_resp = client.get(f"/projects/{project_id}/hub")
        assert hub_resp.status_code == 200
        hub = hub_resp.json()
        ms_data = next(m for m in hub["milestones"] if m["id"] == milestone_id)
        assert ms_data["task_count"] == 2
        assert ms_data["tasks_completed"] == 1
        assert ms_data["progress_pct"] == 50

    def test_project_hub_returns_team_members(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={
            "name": "TeamProject",
            "team_members": [{"name": "Bob", "role": "PM", "contact": "bob@example.com"}],
        })
        project_id = create_resp.json()["id"]

        hub_resp = client.get(f"/projects/{project_id}/hub")
        assert hub_resp.status_code == 200
        team = hub_resp.json()["project"]["team_members"]
        assert len(team) == 1
        assert team[0]["name"] == "Bob"

    def test_add_team_member(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "TM Project"})
        project_id = create_resp.json()["id"]

        resp = client.post(
            f"/projects/{project_id}/team",
            json={"name": "Alice", "role": "engineer", "contact": "alice@example.com"},
        )
        assert resp.status_code == 200
        members = resp.json()["team_members"]
        assert len(members) == 1
        assert members[0]["name"] == "Alice"

        # Add a second member
        resp2 = client.post(
            f"/projects/{project_id}/team",
            json={"name": "Bob", "role": "designer"},
        )
        assert resp2.status_code == 200
        assert len(resp2.json()["team_members"]) == 2

    def test_add_team_member_project_not_found(self):
        client = self._make_client()
        resp = client.post("/projects/99999/team", json={"name": "Ghost"})
        assert resp.status_code == 404

    def test_remove_team_member(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={
            "name": "RM Team Project",
            "team_members": [
                {"name": "Alice", "role": "dev", "contact": ""},
                {"name": "Bob", "role": "pm", "contact": ""},
            ],
        })
        project_id = create_resp.json()["id"]

        # Remove index 0 (Alice)
        resp = client.delete(f"/projects/{project_id}/team/0")
        assert resp.status_code == 200
        members = resp.json()["team_members"]
        assert len(members) == 1
        assert members[0]["name"] == "Bob"

    def test_remove_team_member_out_of_range(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "OOR Project"})
        project_id = create_resp.json()["id"]

        resp = client.delete(f"/projects/{project_id}/team/5")
        assert resp.status_code == 400

    def test_remove_team_member_project_not_found(self):
        client = self._make_client()
        resp = client.delete("/projects/99999/team/0")
        assert resp.status_code == 404

    def test_add_linked_asset(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "Asset Project"})
        project_id = create_resp.json()["id"]

        resp = client.post(
            f"/projects/{project_id}/assets",
            json={"type": "url", "title": "Docs", "url": "https://docs.example.com",
                  "description": "API docs"},
        )
        assert resp.status_code == 200
        assets = resp.json()["linked_assets"]
        assert len(assets) == 1
        assert assets[0]["title"] == "Docs"
        assert assets[0]["type"] == "url"

        # Add a second asset with an id reference
        resp2 = client.post(
            f"/projects/{project_id}/assets",
            json={"type": "research", "title": "Market Research", "id": 42},
        )
        assert resp2.status_code == 200
        assets2 = resp2.json()["linked_assets"]
        assert len(assets2) == 2
        assert assets2[1]["id"] == 42

    def test_add_linked_asset_project_not_found(self):
        client = self._make_client()
        resp = client.post("/projects/99999/assets", json={"type": "url", "title": "X"})
        assert resp.status_code == 404

    def test_remove_linked_asset(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={
            "name": "RM Asset Project",
            "linked_assets": [
                {"type": "url", "title": "Docs", "url": "https://docs.example.com",
                 "description": ""},
                {"type": "url", "title": "Design", "url": "https://figma.com",
                 "description": ""},
            ],
        })
        project_id = create_resp.json()["id"]

        resp = client.delete(f"/projects/{project_id}/assets/0")
        assert resp.status_code == 200
        assets = resp.json()["linked_assets"]
        assert len(assets) == 1
        assert assets[0]["title"] == "Design"

    def test_remove_linked_asset_out_of_range(self):
        client = self._make_client()
        create_resp = client.post("/projects", json={"name": "OOR Asset Project"})
        project_id = create_resp.json()["id"]

        resp = client.delete(f"/projects/{project_id}/assets/0")
        assert resp.status_code == 400

    def test_remove_linked_asset_project_not_found(self):
        client = self._make_client()
        resp = client.delete("/projects/99999/assets/0")
        assert resp.status_code == 404


# ── Milestones ────────────────────────────────────────────────


class TestMilestones:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def _create_project(self, client):
        resp = client.post("/projects", json={"name": "TestProject"})
        return resp.json()["id"]

    def test_create_milestone(self):
        client = self._make_client()
        project_id = self._create_project(client)
        resp = client.post(f"/projects/{project_id}/milestones", json={
            "name": "v1.0 Release",
            "description": "First release",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "v1.0 Release"

    def test_create_milestone_project_not_found(self):
        client = self._make_client()
        resp = client.post("/projects/99999/milestones", json={"name": "M"})
        assert resp.status_code == 404

    def test_list_milestones(self):
        client = self._make_client()
        project_id = self._create_project(client)
        client.post(f"/projects/{project_id}/milestones", json={"name": "M1"})
        client.post(f"/projects/{project_id}/milestones", json={"name": "M2"})
        resp = client.get(f"/projects/{project_id}/milestones")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2

    def test_update_milestone(self):
        client = self._make_client()
        project_id = self._create_project(client)
        create_resp = client.post(f"/projects/{project_id}/milestones", json={"name": "OldM"})
        milestone_id = create_resp.json()["id"]
        resp = client.put(f"/milestones/{milestone_id}", json={"name": "NewM", "status": "completed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "NewM"

    def test_update_milestone_not_found(self):
        client = self._make_client()
        resp = client.put("/milestones/99999", json={"name": "Nope"})
        assert resp.status_code == 404

    def test_delete_milestone(self):
        client = self._make_client()
        project_id = self._create_project(client)
        create_resp = client.post(f"/projects/{project_id}/milestones", json={"name": "Doomed"})
        milestone_id = create_resp.json()["id"]
        resp = client.delete(f"/milestones/{milestone_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_milestone_not_found(self):
        client = self._make_client()
        resp = client.delete("/milestones/99999")
        assert resp.status_code == 404


# ── Sprints ───────────────────────────────────────────────────


class TestSprints:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def _create_project(self, client):
        resp = client.post("/projects", json={"name": "SprintProject"})
        return resp.json()["id"]

    def test_create_sprint(self):
        client = self._make_client()
        project_id = self._create_project(client)
        resp = client.post(f"/projects/{project_id}/sprints", json={
            "name": "Sprint 1",
            "goal": "Ship the MVP",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Sprint 1"
        assert data["goal"] == "Ship the MVP"

    def test_create_sprint_project_not_found(self):
        client = self._make_client()
        resp = client.post("/projects/99999/sprints", json={"name": "S"})
        assert resp.status_code == 404

    def test_list_sprints(self):
        client = self._make_client()
        project_id = self._create_project(client)
        client.post(f"/projects/{project_id}/sprints", json={"name": "S1"})
        client.post(f"/projects/{project_id}/sprints", json={"name": "S2"})
        resp = client.get(f"/projects/{project_id}/sprints")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_get_sprint(self):
        client = self._make_client()
        project_id = self._create_project(client)
        create_resp = client.post(f"/projects/{project_id}/sprints", json={"name": "Sprint A"})
        sprint_id = create_resp.json()["id"]
        resp = client.get(f"/sprints/{sprint_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Sprint A"

    def test_update_sprint(self):
        client = self._make_client()
        project_id = self._create_project(client)
        create_resp = client.post(f"/projects/{project_id}/sprints", json={"name": "Old Sprint"})
        sprint_id = create_resp.json()["id"]
        resp = client.put(f"/sprints/{sprint_id}", json={"name": "Updated Sprint", "goal": "New goal"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Sprint"

    def test_delete_sprint(self):
        client = self._make_client()
        project_id = self._create_project(client)
        create_resp = client.post(f"/projects/{project_id}/sprints", json={"name": "Bye Sprint"})
        sprint_id = create_resp.json()["id"]
        resp = client.delete(f"/sprints/{sprint_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_start_sprint(self):
        client = self._make_client()
        project_id = self._create_project(client)
        create_resp = client.post(f"/projects/{project_id}/sprints", json={"name": "Sprint Go"})
        sprint_id = create_resp.json()["id"]
        resp = client.post(f"/sprints/{sprint_id}/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_complete_sprint(self):
        client = self._make_client()
        project_id = self._create_project(client)
        create_resp = client.post(f"/projects/{project_id}/sprints", json={"name": "Sprint Done"})
        sprint_id = create_resp.json()["id"]
        client.post(f"/sprints/{sprint_id}/start")
        resp = client.post(f"/sprints/{sprint_id}/complete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    def test_start_sprint_not_found(self):
        client = self._make_client()
        resp = client.post("/sprints/99999/start")
        assert resp.status_code == 404

    def test_complete_sprint_not_found(self):
        client = self._make_client()
        resp = client.post("/sprints/99999/complete")
        assert resp.status_code == 404


# ── Research Topics ───────────────────────────────────────────


class TestResearchTopics:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_create_research_topic(self):
        client = self._make_client()
        resp = client.post("/research", json={
            "title": "AI Safety Landscape",
            "description": "Overview of current AI safety research",
            "priority": "high",
            "submitted_by": "brad",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "AI Safety Landscape"
        assert data["priority"] == "high"
        assert "id" in data

    def test_list_research_topics(self):
        client = self._make_client()
        client.post("/research", json={"title": "Topic A"})
        client.post("/research", json={"title": "Topic B"})
        resp = client.get("/research")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 2

    def test_list_research_topics_by_status(self):
        client = self._make_client()
        client.post("/research", json={"title": "Pending Topic"})
        resp = client.get("/research?status=pending")
        assert resp.status_code == 200
        for t in resp.json()["topics"]:
            assert t["status"] == "pending"

    def test_get_research_topic(self):
        client = self._make_client()
        create_resp = client.post("/research", json={"title": "My Research"})
        topic_id = create_resp.json()["id"]
        resp = client.get(f"/research/{topic_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["topic"]["title"] == "My Research"

    def test_get_research_topic_not_found(self):
        client = self._make_client()
        resp = client.get("/research/99999")
        assert resp.status_code == 404

    def test_update_research_topic(self):
        client = self._make_client()
        create_resp = client.post("/research", json={"title": "Old Title"})
        topic_id = create_resp.json()["id"]
        resp = client.put(f"/research/{topic_id}", json={"title": "New Title", "priority": "urgent"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "New Title"

    def test_update_research_topic_not_found(self):
        client = self._make_client()
        resp = client.put("/research/99999", json={"title": "Nope"})
        assert resp.status_code == 404

    def test_assign_research_topic(self):
        client = self._make_client()
        create_resp = client.post("/research", json={"title": "Assignable"})
        topic_id = create_resp.json()["id"]
        resp = client.post(f"/research/{topic_id}/assign", json={"agent_name": "alice"})
        assert resp.status_code == 200
        assert resp.json()["assigned_agent"] == "alice"

    def test_submit_research_brief(self):
        client = self._make_client()
        create_resp = client.post("/research", json={"title": "Briefable"})
        topic_id = create_resp.json()["id"]
        resp = client.post(f"/research/{topic_id}/brief", json={
            "author_agent": "alice",
            "content": "Here is my research brief with findings.",
            "summary": "Key insights",
            "sources": ["https://example.com/paper1"],
            "key_findings": ["Finding 1", "Finding 2"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "Here is my research brief with findings."
        assert data["author_agent"] == "alice"

    def test_list_research_briefs(self):
        client = self._make_client()
        create_resp = client.post("/research", json={"title": "With Briefs"})
        topic_id = create_resp.json()["id"]
        client.post(f"/research/{topic_id}/brief", json={
            "author_agent": "alice",
            "content": "Brief content",
        })
        resp = client.get(f"/research/{topic_id}/briefs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1

    def test_submit_research_review(self):
        client = self._make_client()
        create_resp = client.post("/research", json={"title": "Reviewable"})
        topic_id = create_resp.json()["id"]
        brief_resp = client.post(f"/research/{topic_id}/brief", json={
            "author_agent": "alice",
            "content": "Some content",
        })
        brief_id = brief_resp.json()["id"]
        resp = client.post(f"/research/{topic_id}/reviews", json={
            "brief_id": brief_id,
            "reviewer_agent": "bob",
            "verdict": "approve",
            "comments": "Looks good",
            "confidence": 4,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "approve"

    def test_list_research_reviews(self):
        client = self._make_client()
        create_resp = client.post("/research", json={"title": "Topic"})
        topic_id = create_resp.json()["id"]
        resp = client.get(f"/research/{topic_id}/reviews")
        assert resp.status_code == 200
        assert "reviews" in resp.json()

    def test_publish_research(self):
        client = self._make_client()
        create_resp = client.post("/research", json={"title": "Publishable"})
        topic_id = create_resp.json()["id"]
        resp = client.post(f"/research/{topic_id}/publish")
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

    def test_publish_research_not_found(self):
        client = self._make_client()
        resp = client.post("/research/99999/publish")
        assert resp.status_code == 404

    def test_research_stats(self):
        client = self._make_client()
        client.post("/research", json={"title": "Topic"})
        resp = client.get("/research/stats")
        assert resp.status_code == 200


# ── Presentations ─────────────────────────────────────────────


class TestPresentations:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def _sample_html(self):
        return "<html><body><h1>Slide 1</h1></body></html>"

    def test_create_presentation(self):
        client = self._make_client()
        resp = client.post("/presentations", json={
            "title": "My Deck",
            "html_content": self._sample_html(),
            "description": "A test presentation",
            "created_by": "brad",
            "tags": ["test"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "My Deck"
        assert "id" in data
        assert "share_token" in data

    def test_list_presentations(self):
        client = self._make_client()
        client.post("/presentations", json={"title": "P1", "html_content": self._sample_html()})
        client.post("/presentations", json={"title": "P2", "html_content": self._sample_html()})
        resp = client.get("/presentations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 2

    def test_get_presentation(self):
        client = self._make_client()
        create_resp = client.post("/presentations", json={"title": "Get Me", "html_content": self._sample_html()})
        pres_id = create_resp.json()["id"]
        resp = client.get(f"/presentations/{pres_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Get Me"
        # Full content included
        assert "html_content" in data

    def test_get_presentation_not_found(self):
        client = self._make_client()
        resp = client.get("/presentations/99999")
        assert resp.status_code == 404

    def test_update_presentation(self):
        client = self._make_client()
        create_resp = client.post("/presentations", json={"title": "Old", "html_content": self._sample_html()})
        pres_id = create_resp.json()["id"]
        new_html = "<html><body><h1>Updated</h1></body></html>"
        resp = client.put(f"/presentations/{pres_id}", json={
            "html_content": new_html,
            "title": "New Title",
        })
        assert resp.status_code == 200
        assert resp.json()["title"] == "New Title"

    def test_update_presentation_not_found(self):
        client = self._make_client()
        resp = client.put("/presentations/99999", json={"html_content": "<html/>"})
        assert resp.status_code == 404

    def test_delete_presentation(self):
        client = self._make_client()
        create_resp = client.post("/presentations", json={"title": "Bye", "html_content": self._sample_html()})
        pres_id = create_resp.json()["id"]
        resp = client.delete(f"/presentations/{pres_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        get_resp = client.get(f"/presentations/{pres_id}")
        assert get_resp.status_code == 404

    def test_delete_presentation_not_found(self):
        client = self._make_client()
        resp = client.delete("/presentations/99999")
        assert resp.status_code == 404

    def test_presentation_versions(self):
        client = self._make_client()
        create_resp = client.post("/presentations", json={"title": "Versioned", "html_content": self._sample_html()})
        pres_id = create_resp.json()["id"]
        # Update to create a new version
        client.put(f"/presentations/{pres_id}", json={"html_content": "<html><body>v2</body></html>"})
        resp = client.get(f"/presentations/{pres_id}/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert "versions" in data
        assert "current_version" in data

    def test_presentation_share_link(self):
        client = self._make_client()
        create_resp = client.post("/presentations", json={"title": "Shared", "html_content": self._sample_html()})
        pres_id = create_resp.json()["id"]
        resp = client.get(f"/presentations/{pres_id}/share-link")
        assert resp.status_code == 200
        data = resp.json()
        assert "url" in data
        assert "share_token" in data

    def test_set_presentation_password(self):
        client = self._make_client()
        create_resp = client.post("/presentations", json={"title": "Locked", "html_content": self._sample_html()})
        pres_id = create_resp.json()["id"]
        resp = client.put(f"/presentations/{pres_id}/password", json={"password": "secret123"})
        assert resp.status_code == 200
        assert resp.json()["protected"] is True

    def test_remove_presentation_password(self):
        client = self._make_client()
        create_resp = client.post("/presentations", json={"title": "Unlocked", "html_content": self._sample_html()})
        pres_id = create_resp.json()["id"]
        client.put(f"/presentations/{pres_id}/password", json={"password": "secret123"})
        resp = client.put(f"/presentations/{pres_id}/password", json={"password": ""})
        assert resp.status_code == 200
        assert resp.json()["protected"] is False

    def test_presentation_stats(self):
        client = self._make_client()
        resp = client.get("/presentations/stats")
        assert resp.status_code == 200


# ── Activity ──────────────────────────────────────────────────


class TestActivity:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_list_activity_empty(self):
        client = self._make_client()
        resp = client.get("/activity")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "count" in data

    def test_list_activity_after_task_creation(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "Activity task"})
        resp = client.get("/activity")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "count" in data

    def test_activity_stats(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "Stats task"})
        resp = client.get("/activity/stats")
        assert resp.status_code == 200

    def test_activity_filter_by_agent(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "For alice", "assigned_agent": "alice"})
        resp = client.get("/activity?agent_name=alice")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data

    def test_activity_filter_by_event_type(self):
        client = self._make_client()
        client.post("/tasks", json={"title": "Filter test"})
        resp = client.get("/activity?event_type=task_created")
        assert resp.status_code == 200
        data = resp.json()
        for event in data["events"]:
            assert event["event_type"] == "task_created"


# ── Auth Endpoints ────────────────────────────────────────────


class TestAuthEndpoints:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_auth_status_unauthenticated(self):
        client = self._make_client()
        resp = client.get("/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "authenticated" in data

    def test_auth_logout(self):
        client = self._make_client()
        resp = client.post("/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["logged_out"] is True

    def test_auth_setup_requires_session_secret(self):
        client = self._make_client()
        # Without PINKY_SESSION_SECRET set, setup should fail with 503
        import os as _os
        old_secret = _os.environ.pop("PINKY_SESSION_SECRET", None)
        try:
            resp = client.post("/auth/setup", json={"password": "testpassword123"})
            assert resp.status_code == 503
        finally:
            if old_secret is not None:
                _os.environ["PINKY_SESSION_SECRET"] = old_secret

    def test_auth_login_requires_session_secret(self):
        client = self._make_client()
        import os as _os
        old_secret = _os.environ.pop("PINKY_SESSION_SECRET", None)
        try:
            resp = client.post("/auth/login", json={"password": "testpassword123"})
            assert resp.status_code == 503
        finally:
            if old_secret is not None:
                _os.environ["PINKY_SESSION_SECRET"] = old_secret

    def test_auth_password_update_requires_session(self):
        client = self._make_client()
        resp = client.put("/auth/password", json={"password": "newpassword123"})
        assert resp.status_code == 401


# ── Settings Endpoints ────────────────────────────────────────


class TestSettingsEndpoints:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_get_heartbeat_settings(self):
        client = self._make_client()
        resp = client.get("/settings/heartbeat")
        assert resp.status_code == 200
        data = resp.json()
        assert "heartbeat_prompt" in data

    def test_update_heartbeat_prompt(self):
        client = self._make_client()
        resp = client.put("/settings/heartbeat/prompt", json={"prompt": "Check inbox. Reply HEARTBEAT_OK if idle."})
        assert resp.status_code == 200
        assert "heartbeat_prompt" in resp.json()

    def test_heartbeat_prompt_rejects_blank(self):
        client = self._make_client()
        resp = client.put("/settings/heartbeat/prompt", json={"prompt": "   "})
        assert resp.status_code == 400

    def test_get_owner_profile(self):
        client = self._make_client()
        resp = client.get("/settings/owner-profile")
        assert resp.status_code == 200
        data = resp.json()
        assert "name" in data

    def test_set_owner_profile(self):
        client = self._make_client()
        resp = client.put("/settings/owner-profile", json={"name": "Alice", "timezone": "America/New_York"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Alice"

    def test_empty_owner_profile_update_returns_current(self):
        client = self._make_client()
        client.put("/settings/owner-profile", json={"name": "Alice"})
        resp = client.put("/settings/owner-profile", json={})
        assert resp.status_code == 200
        # Returns current profile unchanged
        assert resp.json()["name"] == "Alice"

    def test_get_main_agent_default(self):
        client = self._make_client()
        resp = client.get("/settings/main-agent")
        assert resp.status_code == 200
        assert "agent" in resp.json()

    def test_onboarding_status(self):
        client = self._make_client()
        resp = client.get("/system/onboarding-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "onboarding_completed" in data
        assert "has_agents" in data

    def test_mark_onboarding_complete(self):
        client = self._make_client()
        resp = client.post("/system/onboarding-complete")
        assert resp.status_code == 200
        assert resp.json()["completed"] is True

    def test_reset_onboarding(self):
        client = self._make_client()
        client.post("/system/onboarding-complete")
        resp = client.post("/system/onboarding-reset")
        assert resp.status_code == 200


# ── Triggers ──────────────────────────────────────────────────


class TestTriggers:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_list_all_triggers_empty(self):
        client = self._make_client()
        resp = client.get("/triggers")
        assert resp.status_code == 200
        data = resp.json()
        assert "triggers" in data
        assert data["count"] == 0

    def test_create_trigger_for_agent(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        resp = client.post("/agents/alice/triggers", json={
            "name": "webhook-test",
            "trigger_type": "webhook",
            "prompt_template": "Webhook fired: {{trigger_name}}",
            "enabled": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "webhook-test"
        assert data["trigger_type"] == "webhook"

    def test_list_agent_triggers(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        client.post("/agents/alice/triggers", json={
            "trigger_type": "webhook", "name": "t1",
        })
        resp = client.get("/agents/alice/triggers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1

    def test_get_trigger(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        create_resp = client.post("/agents/alice/triggers", json={
            "trigger_type": "webhook", "name": "mytrigger",
        })
        trigger_id = create_resp.json()["id"]
        resp = client.get(f"/agents/alice/triggers/{trigger_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "mytrigger"

    def test_update_trigger(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        create_resp = client.post("/agents/alice/triggers", json={
            "trigger_type": "webhook", "name": "orig",
        })
        trigger_id = create_resp.json()["id"]
        resp = client.put(f"/agents/alice/triggers/{trigger_id}", json={"name": "updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated"

    def test_delete_trigger(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        create_resp = client.post("/agents/alice/triggers", json={
            "trigger_type": "webhook", "name": "bye",
        })
        trigger_id = create_resp.json()["id"]
        resp = client.delete(f"/agents/alice/triggers/{trigger_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── Groups ────────────────────────────────────────────────────


class TestGroups:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_create_group(self):
        client = self._make_client()
        resp = client.post("/groups", json={"name": "team-alpha", "members": ["alice", "bob"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "team-alpha"

    def test_list_groups(self):
        client = self._make_client()
        client.post("/groups", json={"name": "team-a", "members": []})
        resp = client.get("/groups")
        assert resp.status_code == 200
        assert "groups" in resp.json()

    def test_get_group(self):
        client = self._make_client()
        client.post("/groups", json={"name": "my-group", "members": ["alice"]})
        resp = client.get("/groups/my-group")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-group"


# ── Providers ─────────────────────────────────────────────────


class TestProviders:
    def _make_client(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    def test_list_providers_empty(self):
        client = self._make_client()
        resp = client.get("/providers")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_create_provider(self):
        client = self._make_client()
        resp = client.post("/providers", json={
            "name": "my-ollama",
            "provider_url": "http://localhost:11434/v1",
            "provider_model": "llama3",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-ollama"

    def test_create_provider_requires_name(self):
        client = self._make_client()
        resp = client.post("/providers", json={"provider_url": "http://localhost:11434/v1"})
        assert resp.status_code == 400

    def test_create_provider_requires_url(self):
        client = self._make_client()
        resp = client.post("/providers", json={"name": "no-url"})
        assert resp.status_code == 400
