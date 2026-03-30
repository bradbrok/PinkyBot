"""Tests for pinky_daemon sessions and API."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.claude_runner import RunResult
from pinky_daemon.agent_registry import DEFAULT_HEARTBEAT_PROMPT
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

                resp = client.post("/agents/test-agent/sleep")
                assert resp.status_code == 200
                assert resp.json()["status"] == "sleeping"
                assert fake.disconnect_calls == 1
                assert app.state.broker._streaming.get("test-agent") is None
                assert app.state.agents.get_streaming_session_id("test-agent", label="main") == ""

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
                assert "main" in labels
                assert "worker" in labels

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
