"""Tests for pinky_daemon sessions and API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import DEFAULT_HEARTBEAT_PROMPT
from pinky_daemon.broker import BrokerMessage
from pinky_daemon.claude_runner import RunResult
from pinky_daemon.message_context_store import MessageContextStore
from pinky_daemon.sessions import (
    Checkpoint,
    ContextStatus,
    Session,
    SessionManager,
    SessionMessage,
    SessionState,
)

# ── SessionMessage ───────────────────────────────────────────


class TestRedactEnvSecrets:
    """#623 pre-cutover hardening: /mcp-servers must not echo secret env values."""

    def test_redacts_pinky_agent_key(self):
        from pinky_daemon.api import _redact_env_secrets

        out = _redact_env_secrets({"PINKY_AGENT_KEY": "supersecret", "FOO": "bar"})
        assert out["PINKY_AGENT_KEY"] == "***redacted***"
        assert out["FOO"] == "bar"  # non-sensitive value preserved
        assert "PINKY_AGENT_KEY" in out  # key stays visible, only value masked

    def test_redacts_common_secret_patterns(self):
        from pinky_daemon.api import _redact_env_secrets

        env = {
            "API_TOKEN": "t", "DB_PASSWORD": "p", "X_SECRET": "s",
            "AUTH_HEADER": "a", "MY_CREDENTIAL": "c", "PLAIN": "ok",
        }
        out = _redact_env_secrets(env)
        for k in ("API_TOKEN", "DB_PASSWORD", "X_SECRET", "AUTH_HEADER", "MY_CREDENTIAL"):
            assert out[k] == "***redacted***"
        assert out["PLAIN"] == "ok"

    def test_non_dict_passthrough(self):
        from pinky_daemon.api import _redact_env_secrets

        assert _redact_env_secrets(None) is None


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

    def test_sdk_runner_receives_requested_permission_mode(self, monkeypatch, tmp_path):
        from pinky_daemon import sdk_runner

        monkeypatch.setattr(sdk_runner, "sdk_available", lambda: True)
        session = Session(
            session_id="permission-boundary",
            working_dir=str(tmp_path),
            permission_mode="plan",
        )

        assert session._runner_type == "sdk"
        assert session._runner._config.permission_mode == "plan"

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
        from pinky_daemon.transport_state import SessionState

        session = StreamingSession(StreamingSessionConfig(agent_name="test-agent"))
        # Drive state machine to CONNECTED to mimic real connect() landing.
        session._state_machine._state = SessionState.CONNECTED

        class FailingClient:
            async def query(self, prompt):
                raise RuntimeError("boom")

        session._client = FailingClient()
        session.attempt_reconnect = AsyncMock()

        await session.send("hello", platform="telegram", chat_id="chat-1")

        assert session._pending_chats == []
        session.attempt_reconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_returns_per_call_handoff(self):
        """#853 P1: StreamingSession.send() reports THIS call's handoff —
        True when client.query() succeeded, False when the exception path was
        taken (swallow + reconnect preserved, no raise). The broker ANDs this
        with the transport capability to report confirmed consumption for
        observability."""
        from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
        from pinky_daemon.transport_state import SessionState

        session = StreamingSession(StreamingSessionConfig(agent_name="test-agent"))
        session._state_machine._state = SessionState.CONNECTED

        class OkClient:
            async def query(self, prompt):
                pass

        session._client = OkClient()
        assert await session.send("hello", platform="web", chat_id="c1") is True

        class FailingClient:
            async def query(self, prompt):
                raise RuntimeError("boom")

        session._client = FailingClient()
        session.attempt_reconnect = AsyncMock()
        assert await session.send("hello2", platform="web", chat_id="c1") is False
        session.attempt_reconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_video_is_classified_as_outreach_tool(self):
        """send_video must be in the outreach set so a video-send turn is marked
        used_outreach_tools=True — otherwise plain_text_fallback could deliver the
        assistant text a second time after the inline video. Covers bare + MCP
        namespaced forms (parity with send_document)."""
        from pinky_daemon.streaming_session import _is_outreach_tool

        assert _is_outreach_tool("send_video") is True
        assert _is_outreach_tool("mcp__pinky-messaging__send_video") is True

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
        # Stub for the SDK Literal — reader_loop's import-time invariant
        # check (PR #404) reads __args__ to defend against SDK rename.
        from typing import Literal as _Literal
        fake_types.AssistantMessageError = _Literal[
            "authentication_failed",
            "billing_error",
            "rate_limit",
            "invalid_request",
            "server_error",
            "unknown",
        ]

        old_sdk_types = sys.modules.get("claude_agent_sdk.types")
        sys.modules["claude_agent_sdk.types"] = fake_types

        callback = AsyncMock()
        session = StreamingSession(
            StreamingSessionConfig(agent_name="test-agent"),
            response_callback=callback,
        )
        from pinky_daemon.transport_state import SessionState
        session._state_machine._state = SessionState.CONNECTED
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
        from pinky_daemon.transport_state import SessionState
        session._state_machine._state = SessionState.CONNECTED
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
        _session = mgr.create(session_id="abc")
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

    @pytest.mark.asyncio
    async def test_tmux_liveness_uses_configured_owner_destination(self, tmp_path):
        """#902 + #865: the Billie shape pages the configured owner route once."""
        from pinky_daemon.transport_state import SessionState as TransportState

        app = self._make_app(str(tmp_path / "test.db"))
        app.state.agents.register(
            "billie",
            model="sonnet",
            transport="tmux",
            working_dir=str(tmp_path / "billie"),
        )
        app.state.agents.register(
            "sdk-sibling",
            model="sonnet",
            transport="sdk",
            working_dir=str(tmp_path / "sdk-sibling"),
        )
        app.state.agents.register(
            "retired-tmux",
            model="sonnet",
            transport="tmux",
            working_dir=str(tmp_path / "retired-tmux"),
        )
        app.state.agents.retire("retired-tmux")
        app.state.agents.set_owner_notification_destinations([
            {
                "platform": "slack",
                "account_id": "T_OWNER",
                "conversation_id": "D_OWNER",
                "principal_id": "U_OWNER",
            }
        ])

        deliveries = []

        async def _capture(
            agent_name, platform, chat_id, content, *, account_id="", **kwargs,
        ):
            deliveries.append((agent_name, platform, account_id, chat_id, content))
            return {"sent": True}

        app.state.broker._send_callback = _capture
        def _fake_session(session_name):
            return SimpleNamespace(
                state=TransportState.CONNECTED,
                stats={"state": TransportState.CONNECTED.value},
                _tmux=SimpleNamespace(
                    session_name=session_name,
                    has_session=AsyncMock(return_value=False),
                ),
            )

        session = _fake_session("pinky-billie")
        sdk_session = _fake_session("pinky-sdk-sibling")
        retired_session = _fake_session("pinky-retired-tmux")
        app.state.broker.register_streaming("billie", session, label="main")
        app.state.broker.register_streaming("sdk-sibling", sdk_session, label="main")
        app.state.broker.register_streaming("retired-tmux", retired_session, label="main")

        await app.state.watchdog._sweep()
        await app.state.watchdog._sweep()

        assert len(deliveries) == 1
        assert deliveries[0][:4] == (
            "billie", "slack", "T_OWNER", "D_OWNER",
        )
        assert "pinky-billie" in deliveries[0][4]
        assert session._tmux.has_session.await_count == 2
        assert sdk_session._tmux.has_session.await_count == 0
        assert retired_session._tmux.has_session.await_count == 0

    @pytest.mark.asyncio
    async def test_tmux_liveness_retries_failed_codex_owner_page(self, tmp_path):
        """#902 review: failed pages retry and name the actual Codex resource."""
        from pinky_daemon.session_watchdog import (
            DEFAULT_TMUX_LIVENESS_NOTIFY_RETRY_AFTER,
        )
        from pinky_daemon.transport_state import SessionState as TransportState

        app = self._make_app(str(tmp_path / "test.db"))
        app.state.agents.register(
            "murzik",
            model="gpt-5.6-sol",
            runtime="codex_cli",
            transport="tmux",
            working_dir=str(tmp_path / "murzik"),
        )
        app.state.agents.set_owner_notification_destinations([
            {
                "platform": "telegram",
                "account_id": "owner-bot",
                "conversation_id": "owner-chat",
                "principal_id": "owner",
            }
        ])

        attempts = []

        async def _flaky(
            agent_name, platform, chat_id, content, *, account_id="", **kwargs,
        ):
            attempts.append((agent_name, platform, account_id, chat_id, content))
            if len(attempts) == 1:
                raise RuntimeError("temporary owner route failure")
            return {"sent": True}

        app.state.broker._send_callback = _flaky
        session = SimpleNamespace(
            state=TransportState.CONNECTED,
            stats={"state": TransportState.CONNECTED.value},
            _tmux=SimpleNamespace(
                session_name="pinky-codex-murzik",
                has_session=AsyncMock(return_value=False),
            ),
        )
        app.state.broker.register_streaming("murzik", session, label="main")

        await app.state.watchdog._sweep()
        state = app.state.watchdog._tmux_liveness_states["murzik"]
        assert len(attempts) == 1
        assert state.mismatch_logged is True
        assert state.notified is False

        # Advance only the retry clock; the underlying mismatch stays active.
        state.last_notify_attempt_at -= DEFAULT_TMUX_LIVENESS_NOTIFY_RETRY_AFTER
        await app.state.watchdog._sweep()
        await app.state.watchdog._sweep()

        assert len(attempts) == 2
        assert state.notified is True
        assert state.notification_attempts == 2
        assert "pinky-codex-murzik" in attempts[1][4]
        assert "tmux session 'pinky-murzik'" not in attempts[1][4]

    @pytest.mark.asyncio
    async def test_login_wall_uses_deployment_owner_destination(self, tmp_path):
        """#916 routes the held login link to this deployment's main user."""
        from pinky_daemon.session_watchdog import (
            DEFAULT_LOGIN_WALL_DEBOUNCE_AFTER,
        )
        from pinky_daemon.tmux_session import TmuxCommandResult
        from pinky_daemon.transport_state import SessionState as TransportState

        app = self._make_app(str(tmp_path / "test.db"))
        for agent_name in ("barsik", "murzik"):
            app.state.agents.register(
                agent_name,
                model="sonnet",
                transport="tmux",
                working_dir=str(tmp_path / agent_name),
            )
        app.state.agents.set_owner_notification_destinations([
            {
                "platform": "telegram",
                "account_id": "deployment-main-bot",
                "conversation_id": "deployment-main-chat",
                "principal_id": "deployment-main-user",
            }
        ])
        capture = (
            Path(__file__).parent / "fixtures" / "login_wall_paste_code.txt"
        ).read_text()
        ok_capture = TmuxCommandResult(0, capture, "")
        ok_command = TmuxCommandResult(0, "", "")
        sessions = {}
        for agent_name in ("barsik", "murzik"):
            sessions[agent_name] = SimpleNamespace(
                state=TransportState.CONNECTED,
                stats={"state": TransportState.CONNECTED.value},
                _tmux=SimpleNamespace(
                    session_name=f"pinky-{agent_name}",
                    has_session=AsyncMock(return_value=True),
                    capture_pane=AsyncMock(return_value=ok_capture),
                    rename_session=AsyncMock(return_value=ok_command),
                ),
            )
            app.state.broker.register_streaming(
                agent_name, sessions[agent_name], label="main",
            )

        deliveries = []

        async def _capture(
            agent_name, platform, chat_id, content, *, account_id="", **kwargs,
        ):
            deliveries.append((agent_name, platform, account_id, chat_id, content))
            return {"sent": True}

        app.state.broker._send_callback = _capture
        await app.state.watchdog._sweep()
        for state in app.state.watchdog._login_wall_states.values():
            state.first_seen_at -= DEFAULT_LOGIN_WALL_DEBOUNCE_AFTER
        await app.state.watchdog._sweep()

        assert len(deliveries) == 1
        assert deliveries[0][1:4] == (
            "telegram",
            "deployment-main-bot",
            "deployment-main-chat",
        )
        assert "claude.com/cai/oauth/authorize" in deliveries[0][4]
        sessions["barsik"]._tmux.rename_session.assert_awaited_once_with(
            "login-hold-barsik",
        )

    @pytest.mark.asyncio
    async def test_codex_tmux_never_joins_or_freezes_claude_login_fleet(
        self, tmp_path,
    ):
        """A signature-positive Codex pane is ineligible for #916."""
        from pinky_daemon.session_watchdog import (
            DEFAULT_LOGIN_WALL_DEBOUNCE_AFTER,
        )
        from pinky_daemon.tmux_session import TmuxCommandResult
        from pinky_daemon.transport_state import SessionState as TransportState

        app = self._make_app(str(tmp_path / "test.db"))
        app.state.agents.register(
            "alpha-codex",
            model="gpt-5.6-sol",
            runtime="codex_cli",
            transport="tmux",
            working_dir=str(tmp_path / "alpha-codex"),
        )
        app.state.agents.register(
            "zeta-claude",
            model="sonnet",
            runtime="claude_sdk",
            transport="tmux",
            working_dir=str(tmp_path / "zeta-claude"),
        )
        app.state.agents.set_owner_notification_destinations([
            {
                "platform": "telegram",
                "account_id": "owner-bot",
                "conversation_id": "owner-chat",
                "principal_id": "owner",
            }
        ])
        capture = (
            Path(__file__).parent / "fixtures" / "login_wall_paste_code.txt"
        ).read_text()
        ok_capture = TmuxCommandResult(0, capture, "")
        ok_command = TmuxCommandResult(0, "", "")
        sessions = {}
        for agent_name in ("alpha-codex", "zeta-claude"):
            sessions[agent_name] = SimpleNamespace(
                state=TransportState.CONNECTED,
                stats={"state": TransportState.CONNECTED.value},
                _tmux=SimpleNamespace(
                    session_name=f"pinky-{agent_name}",
                    has_session=AsyncMock(return_value=True),
                    capture_pane=AsyncMock(return_value=ok_capture),
                    rename_session=AsyncMock(return_value=ok_command),
                ),
            )
            app.state.broker.register_streaming(
                agent_name, sessions[agent_name], label="main",
            )

        deliveries = []

        async def _capture(
            agent_name, platform, chat_id, content, *, account_id="", **kwargs,
        ):
            deliveries.append((agent_name, content))
            return {"sent": True}

        app.state.broker._send_callback = _capture
        await app.state.watchdog._sweep()
        app.state.watchdog._login_wall_states[
            "zeta-claude"
        ].first_seen_at -= DEFAULT_LOGIN_WALL_DEBOUNCE_AFTER
        await app.state.watchdog._sweep()

        assert "alpha-codex" not in app.state.watchdog._login_wall_states
        assert app.state.watchdog._login_wall_incident is None
        sessions["alpha-codex"]._tmux.capture_pane.assert_not_awaited()
        sessions["alpha-codex"]._tmux.rename_session.assert_not_awaited()
        sessions["zeta-claude"]._tmux.rename_session.assert_not_awaited()
        assert len(deliveries) == 1
        assert deliveries[0][0] == "zeta-claude"
        assert "No fleet de-auth is confirmed" in deliveries[0][1]

    @pytest.mark.asyncio
    async def test_per_agent_capture_timeout_cannot_open_shared_fleet(
        self, tmp_path, monkeypatch,
    ):
        """A known independent OAuth timeout remains outside the shared cohort."""
        from pinky_daemon.session_watchdog import (
            DEFAULT_LOGIN_WALL_DEBOUNCE_AFTER,
        )
        from pinky_daemon.tmux_session import TmuxCommandResult
        from pinky_daemon.transport_state import SessionState as TransportState

        monkeypatch.setenv("PINKY_CLAUDE_AUTH_MODE", "shared_refresh_file")
        monkeypatch.setenv(
            "PINKY_CLAUDE_AUTH_MODE_INDIE", "per_agent_oauth",
        )
        app = self._make_app(str(tmp_path / "test.db"))
        for agent_name in ("shared", "indie"):
            app.state.agents.register(
                agent_name,
                model="sonnet",
                runtime="claude_sdk",
                transport="tmux",
                working_dir=str(tmp_path / agent_name),
            )
        app.state.agents.set_owner_notification_destinations([
            {
                "platform": "telegram",
                "account_id": "owner-bot",
                "conversation_id": "owner-chat",
                "principal_id": "owner",
            }
        ])
        capture = (
            Path(__file__).parent / "fixtures" / "login_wall_paste_code.txt"
        ).read_text()
        ok_capture = TmuxCommandResult(0, capture, "")
        ok_command = TmuxCommandResult(0, "", "")
        shared_session = SimpleNamespace(
            state=TransportState.CONNECTED,
            stats={"state": TransportState.CONNECTED.value},
            _tmux=SimpleNamespace(
                session_name="pinky-shared",
                has_session=AsyncMock(return_value=True),
                capture_pane=AsyncMock(return_value=ok_capture),
                rename_session=AsyncMock(return_value=ok_command),
            ),
        )
        indie_session = SimpleNamespace(
            state=TransportState.CONNECTED,
            stats={"state": TransportState.CONNECTED.value},
            _tmux=SimpleNamespace(
                session_name="pinky-indie",
                has_session=AsyncMock(return_value=True),
                capture_pane=AsyncMock(side_effect=TimeoutError("capture timed out")),
                rename_session=AsyncMock(return_value=ok_command),
            ),
        )
        app.state.broker.register_streaming(
            "shared", shared_session, label="main",
        )
        app.state.broker.register_streaming(
            "indie", indie_session, label="main",
        )
        timeout_probe = await app.state.watchdog._login_wall_probe_fn(
            "indie", "main", indie_session,
        )
        assert timeout_probe is not None
        assert timeout_probe.wall is None
        assert timeout_probe.shared_credentials is False
        assert "TimeoutError" in timeout_probe.error
        deliveries = []

        async def _capture(
            agent_name, platform, chat_id, content, *, account_id="", **kwargs,
        ):
            deliveries.append((agent_name, content))
            return {"sent": True}

        app.state.broker._send_callback = _capture
        await app.state.watchdog._sweep()
        app.state.watchdog._login_wall_states[
            "shared"
        ].first_seen_at -= DEFAULT_LOGIN_WALL_DEBOUNCE_AFTER
        await app.state.watchdog._sweep()

        assert app.state.watchdog._login_wall_incident is None
        shared_session._tmux.rename_session.assert_not_awaited()
        indie_session._tmux.rename_session.assert_not_awaited()
        assert len(deliveries) == 1
        assert deliveries[0][0] == "shared"
        assert "No fleet de-auth is confirmed" in deliveries[0][1]

    @pytest.mark.asyncio
    async def test_login_wall_no_owner_destination_freezes_and_fails_loud(
        self, tmp_path, caplog, capsys,
    ):
        """#916 must preserve/log the URL even when owner-notify is misconfigured."""
        from pinky_daemon.session_watchdog import (
            DEFAULT_LOGIN_WALL_DEBOUNCE_AFTER,
        )
        from pinky_daemon.tmux_session import TmuxCommandResult
        from pinky_daemon.transport_state import SessionState as TransportState

        app = self._make_app(str(tmp_path / "test.db"))
        capture = (
            Path(__file__).parent / "fixtures" / "login_wall_paste_code.txt"
        ).read_text()
        ok_capture = TmuxCommandResult(0, capture, "")
        ok_command = TmuxCommandResult(0, "", "")
        sessions = {}
        for agent_name in ("barsik", "murzik"):
            app.state.agents.register(
                agent_name,
                model="sonnet",
                transport="tmux",
                working_dir=str(tmp_path / agent_name),
            )
            sessions[agent_name] = SimpleNamespace(
                state=TransportState.CONNECTED,
                stats={"state": TransportState.CONNECTED.value},
                _tmux=SimpleNamespace(
                    session_name=f"pinky-{agent_name}",
                    has_session=AsyncMock(return_value=True),
                    capture_pane=AsyncMock(return_value=ok_capture),
                    rename_session=AsyncMock(return_value=ok_command),
                ),
            )
            app.state.broker.register_streaming(
                agent_name, sessions[agent_name], label="main",
            )

        send_attempts = []

        async def _unexpected_send(*args, **kwargs):
            send_attempts.append((args, kwargs))
            return {"sent": True}

        app.state.broker._send_callback = _unexpected_send
        await app.state.watchdog._sweep()
        for state in app.state.watchdog._login_wall_states.values():
            state.first_seen_at -= DEFAULT_LOGIN_WALL_DEBOUNCE_AFTER
        with caplog.at_level(logging.CRITICAL, logger="pinky.watchdog"):
            await app.state.watchdog._sweep()

        assert send_attempts == []
        assert app.state.watchdog.is_login_hold("barsik") is True
        sessions["barsik"]._tmux.rename_session.assert_awaited_once_with(
            "login-hold-barsik",
        )
        assert "claude.com/cai/oauth/authorize" in caplog.text
        assert (
            "ERROR watchdog: owner alert for barsik not delivered"
            in capsys.readouterr().err
        )

    class _FakeContextClient:
        def __init__(self, total_tokens=0, max_tokens=200_000):
            self.total_tokens = total_tokens
            self.max_tokens = max_tokens
            self.queries: list[str] = []

        async def get_context_usage(self):
            return {"totalTokens": self.total_tokens, "maxTokens": self.max_tokens}

        async def query(self, prompt: str):
            self.queries.append(prompt)

    class _FakeStreamingSession:
        def __init__(self, agent_name: str, label: str = "main", *, connected: bool = True, total_tokens: int = 0, max_tokens: int = 200_000):
            from pinky_daemon.transport_state import SessionState
            self._TS = SessionState
            self.agent_name = agent_name
            self.label = label
            self.resume_handle = f"{agent_name}-{label}-sdk"
            self.created_at = time.time()
            self.last_active = self.created_at
            self._state = SessionState.CONNECTED if connected else SessionState.DEAD
            self._stats = {"messages_sent": 2, "turns": 3, "errors": 0, "reconnects": 0, "auto_restarts": 0}
            self._config = SimpleNamespace(model="sonnet", context_restart_pct=80, permission_mode="bypassPermissions")
            self.usage = SimpleNamespace(total_cost_usd=0.0, input_tokens=0, output_tokens=0)
            self._client = TestAPI._FakeContextClient(total_tokens=total_tokens, max_tokens=max_tokens) if connected else None
            self.sent: list[tuple[str, str, str]] = []
            self.disconnect_calls = 0
            self.connect_calls = 0

        @property
        def state(self):
            return self._state

        @property
        def id(self) -> str:
            return f"{self.agent_name}-{self.label}"

        @property
        def stats(self) -> dict:
            return {**self._stats, "connected": self._state == self._TS.CONNECTED, "pending_responses": 0, "cost_usd": 0.0, "account": {}}

        async def send(self, prompt: str, platform: str = "", chat_id: str = ""):
            self.sent.append((prompt, platform, chat_id))
            # Transport send contract (#853 P1): per-call handoff bool.
            return True

        async def disconnect(self):
            self.disconnect_calls += 1
            self._state = self._TS.DEAD

        async def connect(self):
            self.connect_calls += 1
            self._state = self._TS.CONNECTED

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

    def test_create_session_threads_tool_denials_to_sdk_runner(self, monkeypatch):
        from pinky_daemon import sdk_runner

        monkeypatch.setattr(sdk_runner, "sdk_available", lambda: True)
        client = self._make_client()
        resp = client.post(
            "/sessions",
            json={
                "session_id": "policy-boundary",
                "permission_mode": "acceptEdits",
                "disallowed_tools": ["Bash", "Write"],
            },
        )

        assert resp.status_code == 200
        session = client.app.state.manager.get("policy-boundary")
        assert session is not None
        assert session.disallowed_tools == ["Bash", "Write"]
        assert session._runner._config.permission_mode == "acceptEdits"
        assert session._runner._config.disallowed_tools == ["Bash", "Write"]

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

    def test_agent_message_tmux_is_live_only_with_zero_unread_or_nags(self):
        """A live tmux handoff creates audit bookkeeping but no inbox delivery."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "murzik", "model": "sonnet"})

                session = self._FakeStreamingSession("murzik", "main")
                session.injection_confirms_consumption = False
                nudges: list[tuple[str, str]] = []

                async def _enqueue_internal_prompt(prompt, *, reason, **kw):
                    nudges.append((prompt, reason))

                session._enqueue_internal_prompt = _enqueue_internal_prompt
                app.state.broker.register_streaming("murzik", session, label="main")

                resp = client.post("/agents/murzik/message", json={
                    "from_agent": "barsik", "message": "review chez#104 please",
                })
                assert resp.status_code == 200
                body = resp.json()
                assert body["delivered"] is True
                assert body["confirmed"] is False
                assert body["queued"] is False
                assert len(session.sent) == 1
                assert "review chez#104" in session.sent[0][0]

                assert app.state.comms.unread_count("murzik") == 0
                unread_rows = app.state.comms._conn.execute(
                    "SELECT COUNT(*) FROM inbox WHERE session_id = ? AND read = 0",
                    ("murzik",),
                ).fetchone()[0]
                assert unread_rows == 0
                inbox = client.get("/sessions/murzik/inbox?unread_only=true").json()
                assert inbox["messages"] == []
                assert inbox["unread"] == 0
                assert inbox["deprecated"] is True
                assert nudges == []

    def test_agent_message_sdk_confirmation_is_observability_only(self):
        """SDK confirmation remains visible without creating unread state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "gemma", "model": "sonnet"})

                session = self._FakeStreamingSession("gemma", "main")
                session.injection_confirms_consumption = True
                app.state.broker.register_streaming("gemma", session, label="main")

                resp = client.post("/agents/gemma/message", json={
                    "from_agent": "barsik", "message": "fyi heads up",
                })
                assert resp.status_code == 200
                body = resp.json()
                assert body["delivered"] is True
                assert body["confirmed"] is True
                assert body["queued"] is False
                assert app.state.comms.unread_count("gemma") == 0

    def test_agent_message_sdk_failed_handoff_is_not_queued(self):
        """A failed live handoff remains audited but is not inbox-queued."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "gemma", "model": "sonnet"})

                session = self._FakeStreamingSession("gemma", "main")
                session.injection_confirms_consumption = True

                async def _failing_send(prompt, platform="", chat_id=""):
                    # SDK-fidelity: client.query() raised; StreamingSession
                    # swallowed it, scheduled reconnect, and reports the
                    # failed per-call handoff.
                    return False

                session.send = _failing_send
                app.state.broker.register_streaming("gemma", session, label="main")

                resp = client.post("/agents/gemma/message", json={
                    "from_agent": "barsik", "message": "important verdict",
                })
                assert resp.status_code == 200
                body = resp.json()
                assert body["delivered"] is False
                assert body["confirmed"] is False
                assert body["queued"] is False
                assert app.state.comms.unread_count("gemma") == 0
                inbox = client.get("/sessions/gemma/inbox?unread_only=true").json()
                assert inbox["messages"] == []
                assert inbox["deprecated"] is True
                assert app.state.broker._stats["nudged"] == 0

    def test_register_isolation_mode_round_trips(self):
        """#149 phase-3: POST /agents carries isolation_mode through to the
        stored agent; default is 'local'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                # Default.
                r = client.post("/agents", json={"name": "plain", "model": "sonnet"})
                assert r.status_code == 200
                assert r.json()["isolation_mode"] == "local"

                # Explicit unix_user persists.
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolated": True, "isolation_mode": "unix_user",
                })
                assert r.status_code == 200
                assert r.json()["isolation_mode"] == "unix_user"
                # Confirm it survives a re-fetch.
                assert client.get("/agents/tenant").json()["isolation_mode"] == "unix_user"

    def test_container_image_round_trips(self, monkeypatch):
        """Container isolation: POST/PUT /agents carry the operator-supplied
        container_image through to the stored agent; default is empty."""
        # Hermetic: with the gate env set on the dev host this would invoke
        # the REAL get_provisioner('container') (SystemContainerOps → podman).
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                # Default is empty.
                r = client.post("/agents", json={"name": "plain", "model": "sonnet"})
                assert r.status_code == 200
                assert r.json()["container_image"] == ""

                # Registering a container agent persists its image.
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolated": True, "isolation_mode": "container",
                    "container_image": "myco/agent:1.4",
                })
                assert r.status_code == 200
                assert r.json()["container_image"] == "myco/agent:1.4"
                assert client.get("/agents/tenant").json()["container_image"] == "myco/agent:1.4"

                # PUT updates the image; an unrelated update leaves it intact.
                up = client.put("/agents/tenant", json={"container_image": "myco/agent:2.0"})
                assert up.status_code == 200
                assert up.json()["container_image"] == "myco/agent:2.0"
                client.put("/agents/tenant", json={"display_name": "Tenant"})
                assert client.get("/agents/tenant").json()["container_image"] == "myco/agent:2.0"

    def test_register_rejects_unknown_isolation_mode(self):
        """The api_models validator rejects modes outside the known set (422)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "bad", "model": "sonnet", "isolation_mode": "qemu_vm",
                })
                assert r.status_code == 422

    def test_update_isolation_mode_round_trips(self):
        """#149 phase-3 (Murzik #642 P2): PUT /agents/{name} can change
        isolation_mode; the validator rejects unknown values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "tenant", "model": "sonnet"})
                assert client.get("/agents/tenant").json()["isolation_mode"] == "local"

                r = client.put("/agents/tenant", json={"isolation_mode": "unix_user"})
                assert r.status_code == 200
                assert r.json()["isolation_mode"] == "unix_user"
                assert client.get("/agents/tenant").json()["isolation_mode"] == "unix_user"

                # A known mode (container) is accepted by the validator —
                # with an image, per the #638 final-state check (the
                # imageless flip is pinned to 422 in
                # test_update_container_mode_requires_image_on_file).
                ok = client.put("/agents/tenant", json={
                    "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                })
                assert ok.status_code == 200
                assert ok.json()["isolation_mode"] == "container"
                # ...while a truly-unknown value is still rejected.
                bad = client.put("/agents/tenant", json={"isolation_mode": "qemu_vm"})
                assert bad.status_code == 422
                # Unchanged after the rejected update.
                assert client.get("/agents/tenant").json()["isolation_mode"] == "container"

    def test_update_heartbeat_interval_round_trips(self):
        """PUT /agents/{name} can change heartbeat_interval. The field was
        missing from UpdateAgentRequest, so a passive worker couldn't be flipped
        to demand-woken (0) without a clobbering re-register — it just churned a
        full force_restart every heartbeat. The registry merge already supports
        the field by key-presence; this exposes it on the update model."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post(
                    "/agents",
                    json={"name": "worker", "model": "sonnet", "heartbeat_interval": 300},
                )
                assert client.get("/agents/worker").json()["heartbeat_interval"] == 300

                # Flip to demand-woken (0) — now editable via the update model.
                r = client.put("/agents/worker", json={"heartbeat_interval": 0})
                assert r.status_code == 200
                assert r.json()["heartbeat_interval"] == 0
                assert client.get("/agents/worker").json()["heartbeat_interval"] == 0

                # Merge-by-key-presence: an unrelated update must NOT resurrect
                # the old cadence (heartbeat_interval absent => unchanged).
                client.put("/agents/worker", json={"display_name": "Worker"})
                assert client.get("/agents/worker").json()["heartbeat_interval"] == 0

    def test_container_agent_cannot_start_before_activation(self, monkeypatch):
        """Container isolation is opt-in but DORMANT by default: with the runtime
        gate OFF, a container agent registers fine yet REFUSES to start (501) —
        same fail-closed guarantee as unix_user, so it never silently runs under
        the daemon uid with no container isolation."""
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                # transport=tmux so we exercise the gate (not the tmux guard).
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet", "transport": "tmux",
                    "isolated": True, "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                })
                assert r.status_code == 200  # registers (provision skipped, gate off)
                resp = client.post("/agents/tenant/wake?prompt=Wake")
                assert resp.status_code == 501
                assert "not runnable yet" in resp.text
                assert "container" in resp.text

    def test_container_requires_tmux_transport(self, monkeypatch):
        """A container agent on a non-tmux transport is blocked at start with a
        clear 400 — container exec only works through the tmux CommandRunner."""
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",  # default transport=sdk
                    "isolated": True, "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                })
                resp = client.post("/agents/tenant/wake?prompt=Wake")
                assert resp.status_code == 400
                assert "transport='tmux'" in resp.text

    def test_register_provisions_and_retire_deprovisions(self, monkeypatch):
        """Lifecycle wiring: register calls provisioner.provision and retire
        calls deprovision (best-effort). Uses a fake provisioner so no real
        podman is needed."""
        from pinky_daemon import provisioning

        calls = []

        class _FakeProv:
            def provision(self, agent):
                calls.append(("provision", agent.name))
                return provisioning.ProvisionResult(ok=True, mode="container")

            def deprovision(self, agent, **kw):
                calls.append(("deprovision", agent.name))
                return provisioning.ProvisionResult(ok=True, mode="container")

        monkeypatch.setattr(provisioning, "get_provisioner", lambda mode, **kw: _FakeProv())
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={"name": "tenant", "model": "sonnet"})
                assert r.status_code == 200
                assert ("provision", "tenant") in calls
                d = client.delete("/agents/tenant")
                assert d.status_code == 200
                assert ("deprovision", "tenant") in calls

    def test_register_rolls_back_on_provision_failure(self, monkeypatch):
        """A failed provision rolls back the just-registered agent (hard delete)
        and surfaces a 500 — no half-provisioned tenant is left behind."""
        from pinky_daemon import provisioning

        class _FailProv:
            def provision(self, agent):
                return provisioning.ProvisionResult(ok=False, mode="container", message="boom")

        monkeypatch.setattr(provisioning, "get_provisioner", lambda mode, **kw: _FailProv())
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={"name": "tenant", "model": "sonnet"})
                assert r.status_code == 500
                assert "boom" in r.text
                assert client.get("/agents/tenant").status_code == 404  # rolled back

    def test_register_upsert_keeps_existing_agent_on_provision_failure(self, monkeypatch):
        """#638: POST /agents is an UPSERT — a provision failure must only roll
        back a NEWLY created agent. A pre-existing agent re-POSTed through a
        failing provisioner is KEPT (500 says so explicitly): hard-deleting it
        over a transient podman error would destroy a live tenant."""
        from pinky_daemon import provisioning

        fail = {"on": False}

        class _TogglableProv:
            def provision(self, agent):
                if fail["on"]:
                    return provisioning.ProvisionResult(
                        ok=False, mode="container", message="podman flaked"
                    )
                return provisioning.ProvisionResult(ok=True, mode="container")

            def deprovision(self, agent, **kw):
                return provisioning.ProvisionResult(ok=True, mode="container")

        monkeypatch.setattr(
            provisioning, "get_provisioner", lambda mode, **kw: _TogglableProv()
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                # Case A: brand-new agent + failing provision -> 500, rolled back.
                fail["on"] = True
                r = client.post("/agents", json={"name": "newbie", "model": "sonnet"})
                assert r.status_code == 500
                assert "podman flaked" in r.text
                assert "existing agent kept" not in r.text  # new-agent branch
                assert client.get("/agents/newbie").status_code == 404

                # Case B: agent exists (provision succeeded), then a re-POST
                # hits a failing provisioner -> 500 but the agent SURVIVES.
                fail["on"] = False
                r = client.post("/agents", json={"name": "tenant", "model": "sonnet"})
                assert r.status_code == 200

                fail["on"] = True
                r = client.post("/agents", json={"name": "tenant", "model": "sonnet"})
                assert r.status_code == 500
                assert "podman flaked" in r.text
                assert "existing agent kept" in r.text
                assert client.get("/agents/tenant").status_code == 200  # NOT deleted

    def test_container_agent_mcp_json_carries_bearer_auth(self, monkeypatch):
        """#638/#623: a container agent's .mcp.json points every pinky-* server
        at the shared MCP via host.containers.internal and carries the DERIVED
        bearer of its per-agent signing key (required for non-loopback peers;
        the raw key never travels as a header) plus X-Agent-Name."""
        from pathlib import Path

        from pinky_daemon.shared_mcp import SHARED_MCP_PORT, derive_mcp_bearer

        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            work_dir = os.path.join(tmpdir, "agents", "tenant")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet", "transport": "tmux",
                    "isolated": True, "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                    "working_dir": work_dir,
                })
                assert r.status_code == 200

                key = app.state.agents.get_signing_key("tenant")
                assert key  # minted at registration

                mcp = json.loads(
                    (Path(work_dir) / ".mcp.json").read_text()
                )["mcpServers"]
                for server in ("pinky-memory", "pinky-self", "pinky-messaging"):
                    cfg = mcp[server]
                    assert cfg["type"] == "sse"
                    # Reaches the daemon over the rootless network, not loopback.
                    assert cfg["url"].startswith(
                        f"http://host.containers.internal:{SHARED_MCP_PORT}/mcp/"
                    )
                    assert cfg["headers"]["X-Agent-Name"] == "tenant"
                    assert cfg["headers"]["Authorization"] == (
                        f"Bearer {derive_mcp_bearer(key)}"
                    )

    def test_register_container_mode_coerces_isolated(self, monkeypatch):
        """#638: a non-local isolation_mode IS isolation — POST with
        isolation_mode='container' and `isolated` OMITTED still yields
        isolated=true, so a container tenant can never receive the forgeable
        global secret just because a caller forgot the bool."""
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                    # isolated deliberately omitted (defaults False)
                })
                assert r.status_code == 200
                assert r.json()["isolated"] is True
                assert client.get("/agents/tenant").json()["isolated"] is True

    def test_update_container_mode_coerces_isolated(self, monkeypatch):
        """#638: the PUT path enforces the same coupling — flipping an existing
        isolated=false agent to isolation_mode='container' implies
        isolated=true (without it the updated tenant would keep the fleet-wide
        PINKY_SESSION_SECRET inside its sandbox)."""
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "tenant", "model": "sonnet"})
                assert client.get("/agents/tenant").json()["isolated"] is False

                r = client.put("/agents/tenant", json={
                    "isolation_mode": "container", "container_image": "myco/agent:1",
                })
                assert r.status_code == 200
                assert r.json()["isolated"] is True
                assert client.get("/agents/tenant").json()["isolated"] is True

    def test_register_container_mode_requires_image(self):
        """#638: container mode is unrunnable without an image — POST with
        isolation_mode='container' and no container_image fails at registration
        (422) instead of at first provision."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolation_mode": "container",
                })
                assert r.status_code == 422
                assert "container_image" in r.text

    def test_update_container_mode_requires_image_on_file(self, monkeypatch):
        """#638 review fix: PUT validates the MERGED record — flipping to
        isolation_mode='container' with no image on file is rejected (422)
        instead of producing an unrunnable tenant at first provision."""
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "tenant", "model": "sonnet"})

                r = client.put("/agents/tenant", json={"isolation_mode": "container"})
                assert r.status_code == 422
                assert "container_image" in r.text
                # The record is unchanged by the rejected update.
                assert client.get("/agents/tenant").json()["isolation_mode"] == "local"

    def test_update_cannot_clear_image_of_container_agent(self, monkeypatch):
        """#638 review fix: the other half of the merged-record check — PUT
        {container_image: ""} on a container agent would leave the final state
        container-mode-without-an-image, so it is rejected (422)."""
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolated": True, "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                })
                assert r.status_code == 200

                r = client.put("/agents/tenant", json={"container_image": ""})
                assert r.status_code == 422
                assert "container_image" in r.text
                # Image (and mode) survive the rejected update.
                got = client.get("/agents/tenant").json()
                assert got["container_image"] == "myco/agent:1"
                assert got["isolation_mode"] == "container"

    def test_update_to_container_rewrites_mcp_json_to_sse(self, monkeypatch):
        """#638 review fix: the documented flow is "flip via PUT, then restart
        the agent" and the restart path does NOT rewrite .mcp.json — so the
        PUT itself must regenerate it. Before: stdio (host-python "command"
        servers). After: SSE via host.containers.internal."""
        from pathlib import Path

        import pinky_daemon.api as api_mod

        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        # Hermetic: stdio is the local-agent shape only while shared mode is off.
        monkeypatch.setattr(api_mod, "SHARED_MCP_ENABLED", False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            work_dir = os.path.join(tmpdir, "agents", "tenant")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet", "working_dir": work_dir,
                })
                assert r.status_code == 200
                before = (Path(work_dir) / ".mcp.json").read_text()
                assert "command" in before  # stdio servers (host python)
                assert "host.containers.internal" not in before

                r = client.put("/agents/tenant", json={
                    "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                })
                assert r.status_code == 200

                after = json.loads(
                    (Path(work_dir) / ".mcp.json").read_text()
                )["mcpServers"]
                for server in ("pinky-memory", "pinky-self", "pinky-messaging"):
                    assert after[server]["type"] == "sse"
                    assert after[server]["url"].startswith(
                        "http://host.containers.internal:"
                    )
                    assert "command" not in after[server]

    def test_update_downgrade_container_to_local_deprovisions(self, monkeypatch):
        """#638 review fix: PUT flipping container -> local tears down the
        container + key secret (deprovision) so they don't orphan forever —
        retire was previously the only deprovision site."""
        from pinky_daemon import provisioning

        calls = []

        class _FakeProv:
            def provision(self, agent):
                calls.append(("provision", agent.name))
                return provisioning.ProvisionResult(ok=True, mode="container")

            def deprovision(self, agent, **kw):
                calls.append(("deprovision", agent.name))
                return provisioning.ProvisionResult(ok=True, mode="container")

        monkeypatch.setattr(
            provisioning, "get_provisioner", lambda mode, **kw: _FakeProv()
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolated": True, "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                })
                assert r.status_code == 200

                r = client.put("/agents/tenant", json={"isolation_mode": "local"})
                assert r.status_code == 200
                assert r.json()["isolation_mode"] == "local"
                # Deprovision fired exactly once, for this tenant.
                assert [c for c in calls if c[0] == "deprovision"] == [
                    ("deprovision", "tenant")
                ]
                assert client.get("/agents/tenant").json()["isolation_mode"] == "local"

    def test_update_isolated_false_clears_flag_on_local_agent(self):
        """UpdateAgentRequest.isolated (#638): settable so a container->local
        round trip can explicitly lift the auto-coerced isolation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={
                    "name": "tenant", "model": "sonnet", "isolated": True,
                })
                assert client.get("/agents/tenant").json()["isolated"] is True

                r = client.put("/agents/tenant", json={"isolated": False})
                assert r.status_code == 200
                assert r.json()["isolated"] is False
                assert client.get("/agents/tenant").json()["isolated"] is False

    def test_update_isolated_false_recoerced_for_container_mode(self, monkeypatch):
        """#638: the handler re-coerces isolated=True for any non-local mode —
        an explicit {"isolated": false} alongside a container flip must NOT
        hand the tenant the fleet-wide forgeable secret."""
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "tenant", "model": "sonnet"})

                r = client.put("/agents/tenant", json={
                    "isolated": False,
                    "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                })
                assert r.status_code == 200
                assert r.json()["isolated"] is True
                assert client.get("/agents/tenant").json()["isolated"] is True

    def test_transcript_path_allows_container_config_root(self, monkeypatch):
        """#638: a container agent's claude runs with CLAUDE_CONFIG_DIR=
        <working_dir>/.claude-container, so its SessionStart hook legitimately
        reports a transcript under <wd>/.claude-container/projects/... — the
        endpoint must accept that root (without it the report 403s and the
        tailer never repoints off its cold-start guess)."""
        from pathlib import Path

        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            work_dir = os.path.join(tmpdir, "agents", "tenant")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "tenant", "model": "sonnet", "transport": "tmux",
                    "isolated": True, "isolation_mode": "container",
                    "container_image": "myco/agent:1",
                    "working_dir": work_dir,
                })
                assert r.status_code == 200

                candidate = (
                    Path(work_dir) / ".claude-container" / "projects"
                    / "-srv-agents-tenant" / "session.jsonl"
                )
                resp = client.post(
                    "/agents/tenant/transport/transcript-path",
                    json={"transcript_path": str(candidate)},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["ok"] is True
                # No live session registered — the path VALIDATION passing is
                # what this test pins.
                assert body.get("session") is None

                # The perimeter still holds for the same container agent.
                resp = client.post(
                    "/agents/tenant/transport/transcript-path",
                    json={"transcript_path": "/etc/passwd"},
                )
                assert resp.status_code == 403

    def test_transcript_path_container_root_denied_for_local_agent(self, monkeypatch):
        """The container-config root exists ONLY for isolation_mode='container'
        rows — a local agent reporting the container-style path is still
        403'd (its only legitimate root is ~/.claude/projects/)."""
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            work_dir = os.path.join(tmpdir, "agents", "plain")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                r = client.post("/agents", json={
                    "name": "plain", "model": "sonnet", "working_dir": work_dir,
                })
                assert r.status_code == 200

                candidate = os.path.join(
                    work_dir, ".claude-container", "projects", "x", "s.jsonl"
                )
                resp = client.post(
                    "/agents/plain/transport/transcript-path",
                    json={"transcript_path": candidate},
                )
                assert resp.status_code == 403
                assert "projects" in resp.json()["detail"].lower()

    def test_unix_user_agent_cannot_start_before_provisioner(self):
        """#149 phase-3 (Murzik #642 P1): an agent labeled isolation_mode=
        'unix_user' is accepted at registration but REFUSES to start (501)
        until inc3c wires the provisioner — it must never silently run under
        the local runner with no OS isolation. Covers the COLD-start path
        (_start_streaming_session)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolated": True, "isolation_mode": "unix_user",
                })
                # Wake triggers _start_streaming_session → isolation preflight.
                resp = client.post("/agents/tenant/wake?prompt=Wake")
                assert resp.status_code == 501
                assert "not runnable yet" in resp.text
                assert "unix_user" in resp.text

    def test_unix_user_reconnect_refused(self):
        """#149 P1 re-review (Murzik): the RECONNECT path
        (_ensure_streaming_session) must also hit the guard. An existing
        local session relabeled unix_user must not relaunch via connect()
        under the daemon uid. /chat auto-wakes a non-connected session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolated": True, "isolation_mode": "unix_user",
                })
                # Existing session object in a non-connected (DEAD) state.
                fake = self._FakeStreamingSession("tenant", "main", connected=False)
                app.state.broker.register_streaming("tenant", fake, label="main")

                resp = client.post("/agents/tenant/chat", json={"content": "hi"})
                assert resp.status_code == 501
                assert "not runnable yet" in resp.text
                assert fake.connect_calls == 0  # never relaunched

    def test_unix_user_restart_refused_without_teardown(self):
        """#149 P1 re-review (Murzik): the RESTART endpoint must hit the guard
        BEFORE disconnecting — a relabeled unix_user agent is refused (501)
        and not torn down then left down."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={
                    "name": "tenant", "model": "sonnet",
                    "isolated": True, "isolation_mode": "unix_user",
                })
                fake = self._FakeStreamingSession("tenant", "main")  # CONNECTED
                app.state.broker.register_streaming("tenant", fake, label="main")

                resp = client.post("/agents/tenant/streaming/restart")
                assert resp.status_code == 501
                assert "not runnable yet" in resp.text
                assert fake.disconnect_calls == 0  # guard fired before teardown

    def test_local_agent_unaffected_by_isolation_guard(self):
        """Control: a normal local agent passes the guard on every path —
        the guard only blocks unimplemented modes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "plain", "model": "sonnet"})
                fake = self._FakeStreamingSession("plain", "main")  # CONNECTED
                app.state.broker.register_streaming("plain", fake, label="main")
                # Restart reaches the save-safety guard (409), NOT a 501 —
                # proving the isolation guard let a local agent through.
                resp = client.post("/agents/plain/streaming/restart")
                assert resp.status_code != 501

    # Note: `test_sleep_disconnects_streaming_main` and
    # `test_sleep_requires_recent_explicit_context_save` were removed
    # in #552 along with the `POST /agents/{name}/sleep` endpoint
    # (Pulse v2 carry-over). Agent-initiated deep sleep fully closed
    # the session, breaking broker auto-wake from inbound platform
    # messages (Telegram in particular — web chat masked the bug via
    # its own cold-start path). Sleep is now exclusively watchdog-
    # driven idle-sleep, which preserves the resume handle so any
    # platform's inbound message warm-wakes the agent. See module
    # docstring in pinky_self/server.py.

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
            # Drive state machine to CONNECTED to mimic real connect() landing.
            from pinky_daemon.transport_state import SessionState
            self._state_machine._state = SessionState.CONNECTED
            if not self.resume_handle:
                self.resume_handle = f"{self.agent_name}-sdk"
            if self._on_resume_handle:
                await self._on_resume_handle(self.agent_name, self.resume_handle)

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
                from pinky_daemon.transport_state import SessionState
                assert app.state.broker._streaming["test-agent"]["main"].state == SessionState.CONNECTED
                assert sent_prompts[-1][1] == "Wake up"

    def test_wake_uses_streaming_session_for_claude_runtime(self):
        async def fake_connect(self):
            # Drive state machine to CONNECTED to mimic real connect() landing.
            from pinky_daemon.transport_state import SessionState
            self._state_machine._state = SessionState.CONNECTED

        async def fake_send(self, prompt: str, platform: str = "", chat_id: str = ""):
            del prompt, platform, chat_id

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.streaming_session.StreamingSession.connect", new=fake_connect), \
                patch("pinky_daemon.streaming_session.StreamingSession.send", new=fake_send):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "claude-agent", "model": "sonnet", "runtime": "claude_sdk"})

                resp = client.post("/agents/claude-agent/wake?prompt=Wake")
                assert resp.status_code == 200

                session = app.state.broker._streaming["claude-agent"]["main"]
                assert session.__class__.__name__ == "StreamingSession"

    def test_wake_uses_codex_session_for_codex_runtime(self):
        async def fake_connect(self):
            # CodexSession (not StreamingSession) — still uses the plain
            # _connected bool. PR3's state-machine routing is scoped to
            # StreamingSession; CodexSession adoption is a separate PR.
            self._connected = True
            self.resume_handle = self.resume_handle or f"{self.agent_name}-codex"
            if self._on_resume_handle:
                await self._on_resume_handle(self.agent_name, self.resume_handle)

        async def fake_send(self, prompt: str, platform: str = "", chat_id: str = "", message_id: str = "", agent_hint: str = ""):
            del prompt, platform, chat_id, message_id, agent_hint

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.codex_session.CodexSession.connect", new=fake_connect), \
                patch("pinky_daemon.codex_session.CodexSession.send", new=fake_send):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                now = time.time()
                app.state.agents._db.execute(
                    "INSERT INTO providers "
                    "(id, name, preset, provider_url, provider_key, provider_model, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "codex-openai",
                        "Codex OpenAI",
                        "",
                        "https://api.openai.com/v1",
                        "global-openai-key",
                        "gpt-5-codex",
                        now,
                        now,
                    ),
                )
                app.state.agents._db.commit()
                client.post("/agents", json={
                    "name": "codex-agent",
                    "model": "fallback-model",
                    "runtime": "codex_cli",
                    "provider_ref": "codex-openai",
                })

                resp = client.post("/agents/codex-agent/wake?prompt=Wake")
                assert resp.status_code == 200

                session = app.state.broker._streaming["codex-agent"]["main"]
                assert session.__class__.__name__ == "CodexSession"
                assert session._config.provider_url == "codex_cli"
                assert session._config.provider_key == "global-openai-key"
                assert session._config.model == "gpt-5-codex"

    def test_wake_uses_tmux_session_for_tmux_transport(self):
        async def fake_connect(self):
            from pinky_daemon.transport_state import SessionState
            self._state_machine._state = SessionState.CONNECTED
            if self._on_resume_handle:
                await self._on_resume_handle(self.agent_name, self.resume_handle)

        async def fake_send(
            self,
            prompt: str,
            *,
            platform: str = "",
            chat_id: str = "",
            message_id: str = "",
            agent_hint: str = "",
        ):
            del prompt, platform, chat_id, message_id, agent_hint

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.tmux_session.TmuxSession.connect", new=fake_connect), \
                patch("pinky_daemon.tmux_session.TmuxSession.send", new=fake_send):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={
                    "name": "tmux-agent",
                    "model": "sonnet",
                    "runtime": "claude_sdk",
                    "transport": "tmux",
                })

                resp = client.post("/agents/tmux-agent/wake?prompt=Wake")
                assert resp.status_code == 200

                session = app.state.broker._streaming["tmux-agent"]["main"]
                assert session.__class__.__name__ == "TmuxSession"
                assert session._config.model == "sonnet"
                assert session.resume_handle == "pinky-tmux-agent"

    def test_wake_uses_codex_tmux_session_for_codex_tmux_transport(self):
        # #215 PR2: codex_cli + transport=tmux is now a VALID combo (it was
        # rejected before this PR). It must build a CodexTmuxSession, not error.
        async def fake_connect(self):
            from pinky_daemon.transport_state import SessionState
            self._state_machine._state = SessionState.CONNECTED
            if self._on_resume_handle:
                await self._on_resume_handle(self.agent_name, self.resume_handle)

        async def fake_send(
            self,
            prompt: str,
            *,
            platform: str = "",
            chat_id: str = "",
            message_id: str = "",
            agent_hint: str = "",
        ):
            del prompt, platform, chat_id, message_id, agent_hint

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.tmux_session.TmuxSession.connect", new=fake_connect), \
                patch("pinky_daemon.tmux_session.TmuxSession.send", new=fake_send):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={
                    "name": "codextmux-agent",
                    "model": "gpt-5-codex",
                    "runtime": "codex_cli",
                    "transport": "tmux",
                })

                resp = client.post("/agents/codextmux-agent/wake?prompt=Wake")
                assert resp.status_code == 200

                session = app.state.broker._streaming["codextmux-agent"]["main"]
                assert session.__class__.__name__ == "CodexTmuxSession"
                assert session._config.provider_url == "codex_cli"
                assert session._config.model == "gpt-5-codex"
                # codex namespace — distinct from claude's pinky-<agent>
                assert session.resume_handle == "pinky-codex-codextmux-agent"

    def test_wake_rejects_opencode_runtime_until_session_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "opencode-agent", "model": "deepseek-v4", "runtime": "opencode"})

                resp = client.post("/agents/opencode-agent/wake?prompt=Wake")
                assert resp.status_code == 503
                assert "opencode runtime is disabled" in resp.text

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
                    updated_by=fake.resume_handle,
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
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                resp = client.post("/agents/test-agent/streaming/restart")
                assert resp.status_code == 200
                assert resp.json()["restart_scheduled"] is True
                assert fake.disconnect_calls == 1
                assert fake.connect_calls == 1

    def test_streaming_restart_acks_before_teardown(self):
        """Disconnect waits for a delayed outermost ASGI send to return.

        ``context_restart`` rides the MCP connection owned by the session being
        replaced.  Inline teardown closes that connection and turns every
        otherwise-successful tool call into MCP -32000.  A delay outside the
        complete FastAPI/BaseHTTPMiddleware stack reproduces Murzik's
        body-queued-but-not-delivered adversarial case.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            response_complete = False
            delay_outer_send = False

            async def observed_app(scope, receive, send):
                if scope["type"] != "http":
                    return await app(scope, receive, send)

                async def observed_send(message):
                    nonlocal response_complete, delay_outer_send
                    is_final_body = (
                        message["type"] == "http.response.body"
                        and not message.get("more_body", False)
                    )
                    if is_final_body and delay_outer_send:
                        await asyncio.sleep(0.75)
                    await send(message)
                    if is_final_body:
                        response_complete = True

                return await app(scope, receive, observed_send)

            with TestClient(observed_app) as client:
                client.post(
                    "/agents",
                    json={"name": "test-agent", "model": "sonnet"},
                )
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming(
                    "test-agent", fake, label="main"
                )
                app.state.agents.set_context(
                    "test-agent",
                    task="Testing post-response restart",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                disconnect_observations = []
                disconnect_started = threading.Event()
                original_disconnect = fake.disconnect

                async def observed_disconnect():
                    disconnect_observations.append(response_complete)
                    disconnect_started.set()
                    await original_disconnect()

                fake.disconnect = observed_disconnect
                response_complete = False
                delay_outer_send = True

                resp = client.post("/agents/test-agent/streaming/restart")

                assert resp.status_code == 200
                assert resp.json()["restart_scheduled"] is True
                assert disconnect_started.wait(timeout=2.0)
                assert disconnect_observations == [True]

    def test_streaming_restart_cancellation_releases_overlap_guard(self):
        """Cancellation anywhere in the helper cannot poison later restarts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post(
                    "/agents",
                    json={"name": "test-agent", "model": "sonnet"},
                )
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming(
                    "test-agent", fake, label="main"
                )
                app.state.agents.set_context(
                    "test-agent",
                    task="Testing cancellation-safe restart",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                disconnect_entered = threading.Event()
                original_disconnect = fake.disconnect

                async def blocking_disconnect():
                    fake.disconnect_calls += 1
                    disconnect_entered.set()
                    await asyncio.Event().wait()

                fake.disconnect = blocking_disconnect
                resp = client.post("/agents/test-agent/streaming/restart")

                assert resp.status_code == 200
                assert disconnect_entered.wait(timeout=2.0)
                task_box = []
                client.portal.call(
                    lambda: task_box.append(
                        next(iter(app.state._context_restart_tasks))
                    )
                )
                task = task_box[0]
                task_done = threading.Event()
                client.portal.call(
                    lambda: task.add_done_callback(lambda _: task_done.set())
                )
                client.portal.call(task.cancel)
                assert task_done.wait(timeout=2.0)
                assert client.portal.call(
                    lambda: "test-agent"
                    not in app.state._context_restarts_inflight
                )

                # Restore a healthy transport/save binding and prove the next
                # valid request is accepted rather than permanently 409'd.
                fake.disconnect = original_disconnect
                fake.resume_handle = "test-agent-main-sdk"
                app.state.agents.set_context(
                    "test-agent",
                    task="Testing cancellation-safe retry",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                retry = client.post("/agents/test-agent/streaming/restart")
                assert retry.status_code == 200
                assert retry.json()["restart_scheduled"] is True

    def test_streaming_restart_late_done_callback_keeps_next_generation_guard(self):
        """A's late done callback cannot release B's overlap reservation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post(
                    "/agents",
                    json={"name": "test-agent", "model": "sonnet"},
                )
                fake = self._FakeStreamingSession("test-agent", "main")
                app.state.broker.register_streaming(
                    "test-agent", fake, label="main"
                )
                app.state.agents.set_context(
                    "test-agent",
                    task="Testing restart-generation ownership",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                restart_endpoint = next(
                    route.endpoint
                    for route in app.routes
                    if getattr(route, "path", None)
                    == "/agents/{name}/streaming/restart"
                )
                b_accepted = threading.Event()
                b_helper_blocked = threading.Event()
                b_result = []
                gates = []

                async def run_queued_b():
                    await gates[0].wait()
                    scope = {"type": "http"}
                    result = await restart_endpoint(
                        name="test-agent",
                        request=SimpleNamespace(scope=scope),
                    )
                    handoff = scope["pinky.response_sent_handoff"]
                    handoff["schedule"]()
                    b_result.append(result)
                    b_accepted.set()

                async def prime_queued_b():
                    gates.extend((asyncio.Event(), asyncio.Event()))
                    asyncio.create_task(run_queued_b())

                client.portal.call(prime_queued_b)

                async def interleaved_disconnect():
                    fake.disconnect_calls += 1
                    fake._state = fake._TS.DEAD
                    if fake.disconnect_calls == 2:
                        b_helper_blocked.set()
                        await gates[1].wait()

                async def interleaved_connect():
                    fake.connect_calls += 1
                    fake._state = fake._TS.CONNECTED
                    if fake.connect_calls == 1:
                        # Wake the already-queued B endpoint task.  B's wakeup
                        # is queued before helper A completes and queues its
                        # task-done callback.
                        gates[0].set()

                fake.disconnect = interleaved_disconnect
                fake.connect = interleaved_connect

                try:
                    first = client.post(
                        "/agents/test-agent/streaming/restart"
                    )
                    assert first.status_code == 200
                    assert b_accepted.wait(timeout=2.0)
                    assert b_result[0]["accepted"] is True
                    assert b_result[0]["old_session_id"] == ""
                    assert b_helper_blocked.wait(timeout=2.0)

                    guard_for_b_present = client.portal.call(
                        lambda: "test-agent"
                        in app.state._context_restarts_inflight
                    )
                    third = client.post(
                        "/agents/test-agent/streaming/restart"
                    )

                    assert guard_for_b_present is True
                    assert third.status_code == 409
                    assert "already in progress" in third.text
                    assert fake.disconnect_calls == 2
                finally:
                    async def release_helpers():
                        for gate in gates:
                            gate.set()
                        while app.state._context_restart_tasks:
                            await asyncio.sleep(0)

                    client.portal.call(release_helpers)

    def test_streaming_restart_clears_codex_session_id_on_codex_sessions(self):
        """Codex sessions track thread_id in `codex_session_id`; restart must clear it
        or the next turn will run `codex exec resume <stale-id>` and fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                fake = self._FakeStreamingSession("test-agent", "main")
                # Simulate a codex-backed session with a stale thread id pinned.
                fake.codex_session_id = "019dc43b-99cd-7b81-884d-eb09a93f9144"
                app.state.broker.register_streaming("test-agent", fake, label="main")

                app.state.agents.set_context(
                    "test-agent",
                    task="Testing codex restart clears thread id",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                resp = client.post("/agents/test-agent/streaming/restart")
                assert resp.status_code == 200
                assert resp.json()["restart_scheduled"] is True
                assert fake.codex_session_id == "", (
                    "codex_session_id must be cleared so next turn does not "
                    "issue `codex exec resume <stale-id>`"
                )
                assert fake.resume_handle == ""

    # ──────────────────────────────────────────────────────────────────
    # Task #103 — /admin/force-restart-agent/{name}
    # Wedged-agent recovery escape hatch. Bumps agent_contexts.updated_at
    # to satisfy the streaming/restart guard's within_buffer check, then
    # restarts. Anti-abuse: requires stale heartbeat (>10min by default).
    # ──────────────────────────────────────────────────────────────────

    def _seed_heartbeat(
        self, app, agent_name: str, *, age_sec: float,
        metadata: dict | None = None,
        status: str = "alive",
    ):
        """Insert a heartbeat row with a fudged timestamp so the
        force-restart heartbeat-staleness check can be exercised
        deterministically without sleeping. ``metadata`` defaults to
        an empty dict (= agent-origin). Pass
        ``metadata={"source":"server_presence"}`` to simulate the
        synthetic scheduler reconciliation row, or
        ``status="dead"`` / ``status="stale"`` (with no source) to
        simulate scheduler stale-out / dead-out rows (Murzik #573
        round-2 review test reproducer)."""
        import json as _json
        ts = time.time() - age_sec
        meta_json = _json.dumps(metadata or {})
        app.state.agents._db.execute(
            """INSERT INTO agent_heartbeats
               (agent_name, session_id, timestamp, status, context_pct,
                message_count, metadata, notes, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, "test-session", ts, status, 0.0, 0, meta_json, "", 0),
        )
        app.state.agents._db.commit()

    def _seed_wedged_agent(self, app, agent_name: str = "wedged"):
        """Create a fake streaming session + saved context whose
        updated_at is too old to satisfy the normal restart guard.
        Returns the fake session.
        """
        fake = self._FakeStreamingSession(agent_name, "main")
        app.state.broker.register_streaming(agent_name, fake, label="main")
        app.state.agents.set_context(
            agent_name,
            task="Wedged work to recover",
            metadata={"source": "save_my_context"},
            updated_by=fake.resume_handle,
        )
        # Force the save timestamp into the past — beyond the 5-min
        # within_buffer window the normal restart endpoint requires.
        app.state.agents._db.execute(
            "UPDATE agent_contexts SET updated_at=? WHERE agent_name=?",
            (time.time() - 3600, agent_name),
        )
        app.state.agents._db.commit()
        return fake

    def test_force_restart_succeeds_on_wedged_agent(self):
        """Happy path: wedged agent with stale save + stale heartbeat
        gets force-restarted. Saved context is preserved (we wake into
        it); only updated_at is bumped to satisfy the gate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                fake = self._seed_wedged_agent(app)
                self._seed_heartbeat(app, "wedged", age_sec=3600)  # 1hr stale

                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"reason": "Sasha-style wedge — kevin's questions ignored"},
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["restarted"] is True
                assert body["forced"] is True
                assert body["agent"] == "wedged"
                assert body["heartbeat_age_sec"] >= 3600
                assert body["reason"].startswith("Sasha-style")
                assert fake.disconnect_calls == 1
                assert fake.connect_calls == 1
                assert fake.resume_handle == ""
                # Saved context survives the force-restart (only the
                # timestamp was touched); next session wakes into it.
                ctx = app.state.agents.get_context("wedged")
                assert ctx is not None
                assert ctx.task == "Wedged work to recover"

    def test_force_restart_rejects_when_heartbeat_is_recent(self):
        """Anti-abuse contract: a recently-alive agent is "busy," not
        "wedged." Refuse force-restart so an operator can't accidentally
        nuke an active session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "busy", "model": "sonnet"})
                fake = self._seed_wedged_agent(app, "busy")
                self._seed_heartbeat(app, "busy", age_sec=30)  # 30s ago — alive

                resp = client.post(
                    "/admin/force-restart-agent/busy",
                    json={"reason": "I think it's wedged"},
                )
                assert resp.status_code == 409
                assert "not\n                    wedged" in resp.text.replace(
                    " ", " "
                ) or "not " in resp.text  # message phrasing tolerance
                assert "heartbeat" in resp.text.lower()
                # No disconnect occurred — session left intact.
                assert fake.disconnect_calls == 0

    def test_force_restart_min_heartbeat_age_sec_can_be_overridden(self):
        """Caller may lower the threshold for a deliberate override
        (e.g. integration test or known-bad agent with frequent
        heartbeat hook firing despite being wedged on real work)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                self._seed_wedged_agent(app, "wedged")
                self._seed_heartbeat(app, "wedged", age_sec=120)  # 2min ago

                # Default threshold (600s) would reject — this one succeeds.
                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"min_heartbeat_age_sec": 60, "reason": "override"},
                )
                assert resp.status_code == 200, resp.text

    def test_force_restart_allows_when_agent_never_heartbeated(self):
        """An agent with no heartbeat row at all is allowed through
        — interpreted as "genuinely never alive" rather than "alive
        and busy." The recovery use-case includes never-booted
        agents whose first wake hung."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                self._seed_wedged_agent(app)
                # NO heartbeat seeded.

                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"reason": "never booted"},
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["heartbeat_age_sec"] is None

    def test_force_restart_rejects_when_no_saved_context(self):
        """Refuse if there's nothing to wake into. A fresh session
        without saved context would have empty wake_context and
        the agent would boot disoriented — better to surface this
        as a 412 than to silently restart into a void."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "blank", "model": "sonnet"})
                fake = self._FakeStreamingSession("blank", "main")
                app.state.broker.register_streaming("blank", fake, label="main")
                self._seed_heartbeat(app, "blank", age_sec=3600)
                # NO set_context call.

                resp = client.post(
                    "/admin/force-restart-agent/blank",
                    json={"reason": "trying anyway"},
                )
                assert resp.status_code == 412
                assert "saved context" in resp.text.lower()
                assert fake.disconnect_calls == 0

    def test_force_restart_rejects_when_no_streaming_session(self):
        """404 if there's no session to restart. Different failure mode
        from 412 (no context): nothing to even disconnect."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                resp = client.post(
                    "/admin/force-restart-agent/ghost",
                    json={"reason": "trying"},
                )
                assert resp.status_code == 404
                assert "ghost" in resp.text.lower()

    def test_force_restart_emits_audit_log(self):
        """Audit contract: activity.log + session_event_store.log must
        both fire with event_type='force_restart' and the caller's
        reason in metadata. This is the breadcrumb operators follow
        when reviewing 'who killed my agent.'"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                self._seed_wedged_agent(app)
                self._seed_heartbeat(app, "wedged", age_sec=3600)

                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"reason": "audit-test reason string"},
                )
                assert resp.status_code == 200, resp.text

                # Check the activity_log row landed.
                activity_rows = app.state.activity.list(agent_name="wedged", limit=10)
                force_rows = [r for r in activity_rows if r["event_type"] == "force_restart"]
                assert len(force_rows) == 1, (
                    f"expected one force_restart activity row; "
                    f"got: {[r['event_type'] for r in activity_rows]}"
                )
                assert force_rows[0]["metadata"]["reason"] == "audit-test reason string"
                assert force_rows[0]["metadata"]["source"] == "force_restart_endpoint"
                assert force_rows[0]["metadata"]["heartbeat_age_sec"] >= 3600

                # Check the session_events row landed.
                session_rows = app.state.session_event_store.get_for_agent("wedged")
                force_session_rows = [
                    r for r in session_rows if r["event_type"] == "force_restart"
                ]
                assert len(force_session_rows) == 1
                assert force_session_rows[0]["metadata"]["reason"] == (
                    "audit-test reason string"
                )

    def test_force_restart_clears_codex_session_id(self):
        """Sibling of the /streaming/restart codex-thread fix:
        codex_session_id must be cleared on force-restart too or the
        next turn would `codex exec resume <stale-id>`. Same bug class
        as test_streaming_restart_clears_codex_session_id_on_codex_sessions
        — pin the parity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                fake = self._seed_wedged_agent(app)
                fake.codex_session_id = "019dc43b-99cd-7b81-884d-eb09a93f9144"
                self._seed_heartbeat(app, "wedged", age_sec=3600)

                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"reason": "codex wedge"},
                )
                assert resp.status_code == 200, resp.text
                assert fake.codex_session_id == ""
                assert fake.resume_handle == ""

    def test_force_restart_ignores_synthetic_server_presence_heartbeat(self):
        """Murzik #573 review regression. The scheduler synthesizes
        ``alive`` heartbeats from server presence whenever the streaming
        session is CONNECTED (``source='server_presence'``). For the
        exact failure mode this endpoint targets — transport CONNECTED
        but reader loop wedged on an LLM call — those synthetic rows
        keep landing fresh and would mask the wedge if we used the
        latest heartbeat row blindly. Endpoint must look only at
        agent-origin heartbeats (empty metadata, or
        ``source != 'server_presence'``).

        Setup: stale agent-origin heartbeat (1h old) + FRESH synthetic
        ``server_presence`` heartbeat (30s old). With the pre-fix
        ``get_latest_heartbeat`` call, the 30s row would 409. With the
        fixed ``get_latest_agent_heartbeat``, the gate uses the 1h
        agent-origin row and permits the restart.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                self._seed_wedged_agent(app)
                # Stale agent-origin heartbeat (genuine).
                self._seed_heartbeat(app, "wedged", age_sec=3600, metadata={})
                # Fresh synthetic row — newer than the genuine one. Would
                # mask the wedge if we naively took the latest row.
                self._seed_heartbeat(
                    app, "wedged", age_sec=30,
                    metadata={
                        "source": "server_presence",
                        "reason": "connected_streaming_session",
                        "label": "main",
                    },
                )

                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"reason": "synthetic-row regression"},
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                # heartbeat_age_sec MUST come from the genuine row (3600s),
                # not the synthetic row (30s). Allow loose lower bound for
                # the few ms of test setup overhead.
                assert body["heartbeat_age_sec"] >= 3600, (
                    f"heartbeat_age_sec must reflect the agent-origin "
                    f"heartbeat (~3600s), not the synthetic 30s row; "
                    f"got {body['heartbeat_age_sec']}"
                )
                # Audit metadata must also reflect the genuine row.
                activity_rows = app.state.activity.list(
                    agent_name="wedged", limit=10,
                )
                force_rows = [
                    r for r in activity_rows if r["event_type"] == "force_restart"
                ]
                assert len(force_rows) == 1
                assert force_rows[0]["metadata"]["heartbeat_age_sec"] >= 3600

    def test_force_restart_ignores_scheduler_stale_dead_heartbeat(self):
        """Murzik #573 round-2 regression. Distinct from the
        server_presence case above: when the scheduler observes an
        agent missing heartbeat windows, it writes a synthetic row
        with ``status='stale'`` or ``status='dead'`` and metadata
        like ``{"reason": "no heartbeat for 600s"}`` — no
        ``source`` field. The previous filter (source-exclusion
        only) would let those fresh dead rows through and produce
        the wrong 'agent-origin heartbeat is N seconds old; not
        wedged' conclusion, defeating the endpoint in the exact
        target failure mode (scheduler has just marked the agent
        dead and we're trying to force-restart it).

        The fixed filter also rejects ``status IN ('stale', 'dead')``.

        Setup: stale agent-origin ``status='ok'`` heartbeat (1h old,
        no metadata) + FRESH synthetic scheduler ``status='dead'``
        row (30s old, ``{"reason": ...}``). Force-restart must use
        the 1h alive row and permit the restart.

        Also covers the agent-origin status namespace: the
        ``pinky-self`` MCP ``send_heartbeat()`` writes ``ok`` / ``busy``
        / ``finishing`` — these are NOT ``alive`` and must still
        count as "agent said it's alive recently."
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                self._seed_wedged_agent(app)
                # Stale genuine agent-origin "ok" heartbeat (1h).
                # This is the status pinky-self.send_heartbeat actually
                # writes — proves the filter doesn't accidentally
                # require status='alive'.
                self._seed_heartbeat(
                    app, "wedged", age_sec=3600, status="ok", metadata={},
                )
                # FRESH scheduler dead-out row. Newer than the genuine
                # one. No 'source' field — only 'reason'. The old
                # source-only filter let this through.
                self._seed_heartbeat(
                    app, "wedged", age_sec=30, status="dead",
                    metadata={"reason": "no heartbeat for 1200s"},
                )

                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"reason": "scheduler-dead-row regression"},
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                # heartbeat_age_sec MUST come from the genuine 1h 'ok'
                # row, not the synthetic 30s 'dead' row.
                assert body["heartbeat_age_sec"] >= 3600, (
                    f"heartbeat_age_sec must reflect the agent-origin "
                    f"heartbeat (~3600s), not the scheduler-dead row "
                    f"(30s); got {body['heartbeat_age_sec']}"
                )
                # And the audit row.
                activity_rows = app.state.activity.list(
                    agent_name="wedged", limit=10,
                )
                force_rows = [
                    r for r in activity_rows if r["event_type"] == "force_restart"
                ]
                assert len(force_rows) == 1
                assert force_rows[0]["metadata"]["heartbeat_age_sec"] >= 3600

    def test_force_restart_captures_prior_context_age_before_bump(self):
        """Murzik #573 review: the updated_at bump erases the 12h-stale
        diagnostic. Capture ``prior_context_updated_at`` and
        ``prior_context_age_sec`` in the audit metadata + response
        BEFORE the bump so operators reviewing 'why force-restart?'
        can see the headline 'wedged' signal (saved-context was N
        hours stale at the time)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                # _seed_wedged_agent sets updated_at to NOW - 3600s.
                self._seed_wedged_agent(app)
                self._seed_heartbeat(app, "wedged", age_sec=3600)

                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"reason": "prior-context audit test"},
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["prior_context_age_sec"] is not None
                assert body["prior_context_age_sec"] >= 3600, (
                    f"prior_context_age_sec must capture pre-bump age "
                    f"(~3600s); got {body['prior_context_age_sec']}"
                )
                # Response contract also exposes the absolute
                # pre-bump updated_at timestamp (matches audit row).
                assert body["prior_context_updated_at"] > 0
                assert (
                    time.time() - body["prior_context_updated_at"]
                ) >= 3600

                # Same on the audit row.
                activity_rows = app.state.activity.list(
                    agent_name="wedged", limit=10,
                )
                force_meta = activity_rows[0]["metadata"]
                assert force_meta["prior_context_age_sec"] >= 3600
                assert force_meta["prior_context_updated_at"] > 0

                # And post-bump the actual saved-context updated_at IS
                # recent (the bypass worked, that's what we wanted).
                ctx = app.state.agents.get_context("wedged")
                assert (time.time() - ctx.updated_at) < 60

    def test_force_restart_rejects_negative_min_heartbeat_age_sec(self):
        """Murzik #573 review: ``Field(ge=0)`` on the Pydantic model
        prevents a typo from silently disabling the gate by passing a
        negative value (negative would always satisfy the freshness
        check). Pydantic returns 422 on validation error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                self._seed_wedged_agent(app)
                self._seed_heartbeat(app, "wedged", age_sec=3600)

                resp = client.post(
                    "/admin/force-restart-agent/wedged",
                    json={"min_heartbeat_age_sec": -1, "reason": "typo"},
                )
                assert resp.status_code == 422

    def test_force_restart_without_body_works(self):
        """The reason field is optional. Calling with no body at all
        must still succeed (reason defaults to empty string). Useful
        for the eventual frontend "Force Restart" button which may
        not collect a reason for first-cut UX."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "wedged", "model": "sonnet"})
                self._seed_wedged_agent(app)
                self._seed_heartbeat(app, "wedged", age_sec=3600)

                resp = client.post("/admin/force-restart-agent/wedged")
                assert resp.status_code == 200, resp.text
                assert resp.json()["reason"] == ""

    def test_streaming_model_change_clears_codex_session_id(self):
        """When /streaming/model triggers a context-window restart, codex_session_id
        must also be cleared (sibling of the /streaming/restart bug fix)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                # Default fake has max_tokens=200_000 (not a 1M model). Switching to
                # a 1M model triggers needs_restart=True path inside set_streaming_model.
                fake = self._FakeStreamingSession("test-agent", "main", max_tokens=200_000)
                fake.codex_session_id = "019dc43b-99cd-7b81-884d-eb09a93f9144"
                app.state.broker.register_streaming("test-agent", fake, label="main")

                app.state.agents.set_context(
                    "test-agent",
                    task="Testing /streaming/model clears codex thread",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                resp = client.post(
                    "/agents/test-agent/streaming/model",
                    json={"model": "claude-opus-4-7"},  # in _1M_MODELS → triggers restart
                )
                assert resp.status_code == 200, resp.text
                assert fake.codex_session_id == ""
                assert fake.resume_handle == ""

    def test_streaming_model_restart_refreshes_retained_launch_config(self):
        """#856: a context-window rebuild uses current registry defaults.

        A retained Codex object can predate durable provider/effort changes.
        The model endpoint must refresh both StreamingSessionConfig and the
        Codex launch caches before reconnecting it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                # Cross window classes so the change forces the rebuild path.
                # Start from a genuine 1M class and switch to 200k-class Sol;
                # #356 removed Sol's stale inferred-1M designation.
                app.state.agents.register(
                    "murzik",
                    model="claude-opus-4-8",
                    runtime="codex_cli",
                    transport="tmux",
                    provider_url="codex_cli",
                    provider_key="old-key",
                    thinking_effort="low",
                    working_dir=os.path.join(tmpdir, "murzik"),
                )
                fake = self._FakeStreamingSession(
                    "murzik", "main", max_tokens=1_000_000
                )
                fake._config.model = "claude-opus-4-8"
                fake._config.provider_url = "codex_cli"
                fake._config.provider_key = "old-key"
                fake._config.thinking_effort = "low"
                fake._config.strict_effort_enforcement = False
                fake._codex_model = "claude-opus-4-8"
                fake._openai_api_key = "old-key"
                fake._reasoning_effort = "low"
                fake._effort_override = None
                app.state.broker.register_streaming("murzik", fake, label="main")

                app.state.agents.set_context(
                    "murzik",
                    task="Testing registry-backed context-window rebuild",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                updated = client.put(
                    "/agents/murzik",
                    json={"provider_key": "new-key", "thinking_effort": "xhigh"},
                )
                assert updated.status_code == 200, updated.text
                # The retained object is intentionally stale until a rebuild.
                assert fake._openai_api_key == "old-key"
                assert fake._reasoning_effort == "low"

                observed = {}

                async def connect_with_snapshot():
                    observed.update(
                        config_model=fake._config.model,
                        config_provider_url=fake._config.provider_url,
                        config_provider_key=fake._config.provider_key,
                        config_effort=fake._config.thinking_effort,
                        codex_model=fake._codex_model,
                        openai_api_key=fake._openai_api_key,
                        reasoning_effort=fake._reasoning_effort,
                    )
                    fake.connect_calls += 1
                    fake._state = fake._TS.CONNECTED

                fake.connect = connect_with_snapshot
                response = client.post(
                    "/agents/murzik/streaming/model",
                    json={"model": "gpt-5.6-sol"},
                )

                assert response.status_code == 200, response.text
                assert response.json()["restarted"] is True
                assert observed == {
                    "config_model": "gpt-5.6-sol",
                    "config_provider_url": "codex_cli",
                    "config_provider_key": "new-key",
                    "config_effort": "xhigh",
                    "codex_model": "gpt-5.6-sol",
                    "openai_api_key": "new-key",
                    "reasoning_effort": "xhigh",
                }
                durable = app.state.agents.get("murzik")
                assert durable.model == "gpt-5.6-sol"
                assert durable.provider_key == "new-key"
                assert durable.thinking_effort == "xhigh"

    def test_streaming_archive_clears_codex_session_id(self):
        """/streaming/archive must clear codex_session_id alongside session_id —
        same bug pattern as /streaming/restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                fake = self._FakeStreamingSession("test-agent", "main")
                fake.codex_session_id = "019dc43b-99cd-7b81-884d-eb09a93f9144"
                app.state.broker.register_streaming("test-agent", fake, label="main")

                app.state.agents.set_context(
                    "test-agent",
                    task="Testing /streaming/archive clears codex thread",
                    metadata={"source": "save_my_context"},
                    updated_by=fake.resume_handle,
                )
                fake.last_active = time.time()

                resp = client.post("/agents/test-agent/streaming/archive")
                assert resp.status_code == 200, resp.text
                assert resp.json()["archived"] is True
                assert fake.codex_session_id == ""
                assert fake.resume_handle == ""
                # Sanity: archive prompted the agent to save state before resetting.
                assert any(
                    "archived" in q.lower() or "save" in q.lower()
                    for q in fake._client.queries
                )

    def test_chat_does_not_double_connect_during_reconnecting(self):
        """Regression for @murzik PR #492 blocker 2.

        Pre-fix _ensure_streaming_session called ss.connect() for ANY
        non-CONNECTED state, including RECONNECTING. The chat endpoint
        delegates to _ensure_streaming_session when the session isn't
        already connected, so an inbound web/admin message during an
        in-flight reconnect would race the existing reconnect with a
        second connect() call. Post-fix _ensure_streaming_session
        branches by explicit state: RECONNECTING waits bounded for the
        in-flight to land instead of calling connect().
        """
        from pinky_daemon.transport_state import SessionState

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                fake = self._FakeStreamingSession("test-agent", "main")
                # Drop into RECONNECTING mid-flight.
                fake._state = SessionState.RECONNECTING
                app.state.broker.register_streaming("test-agent", fake, label="main")

                # Settle the in-flight reconnect on a background thread so the
                # bounded wait inside _ensure_streaming_session sees CONNECTED.
                import threading
                def _settle():
                    time.sleep(0.05)
                    fake._state = SessionState.CONNECTED
                threading.Thread(target=_settle, daemon=True).start()

                # Patch _INBOUND_RECONNECT_WAIT_SEC for fast test execution.
                import pinky_daemon.broker as broker_mod
                old_wait = broker_mod._INBOUND_RECONNECT_WAIT_SEC
                old_poll = broker_mod._INBOUND_RECONNECT_POLL_SEC
                broker_mod._INBOUND_RECONNECT_WAIT_SEC = 1.0
                broker_mod._INBOUND_RECONNECT_POLL_SEC = 0.01
                try:
                    resp = client.post(
                        "/agents/test-agent/chat?session=main",
                        json={"content": "hello during reconnect"},
                    )
                finally:
                    broker_mod._INBOUND_RECONNECT_WAIT_SEC = old_wait
                    broker_mod._INBOUND_RECONNECT_POLL_SEC = old_poll

                assert resp.status_code in (200, 202), resp.text
                # The load-bearing assertion: _ensure_streaming_session
                # MUST NOT have called connect() — the in-flight reconnect
                # (the _settle thread) is what lands the session in CONNECTED.
                assert fake.connect_calls == 0, (
                    f"chat endpoint called connect() {fake.connect_calls}x during "
                    f"RECONNECTING — _ensure_streaming_session must not race the "
                    f"in-flight reconnect. Pre-fix this was the double-connect."
                )

    def test_wake_streaming_session_defaults_include_outreach_tools(self):
        async def fake_connect(self):
            # Drive state machine to CONNECTED to mimic real connect() landing.
            from pinky_daemon.transport_state import SessionState
            self._state_machine._state = SessionState.CONNECTED

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
            # Drive state machine to CONNECTED to mimic real connect() landing.
            from pinky_daemon.transport_state import SessionState
            self._state_machine._state = SessionState.CONNECTED

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
            # Drive state machine to CONNECTED to mimic real connect() landing.
            from pinky_daemon.transport_state import SessionState
            self._state_machine._state = SessionState.CONNECTED
            if not self.resume_handle:
                self.resume_handle = f"{self.agent_name}-sdk"
            if self._on_resume_handle:
                await self._on_resume_handle(self.agent_name, self.resume_handle)

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.streaming_session.StreamingSession.connect", new=fake_connect):
            db_path = os.path.join(tmpdir, "test.db")
            app1 = self._make_app(db_path)
            with TestClient(app1) as client1:
                client1.post("/agents", json={"name": "test-agent", "model": "sonnet"})
                # Boot policy (2026-05-11): only the main agent auto-resumes
                # its streaming session on restart. Mark this agent as main so
                # the cross-boot restore behavior under test still applies.
                app1.state.agents.set_main_agent("test-agent")
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

    def test_restart_manifest_reconciles_live_sibling_with_registry_config(self):
        """#856: daemon restart restores previously-live sibling panes only.

        The rebuilt Codex/tmux session must take model + effort from the
        durable registry, while a dormant sibling with only a historical
        session id remains on-demand. One failed sibling may not abort startup.
        """
        from datetime import datetime, timezone
        from pathlib import Path

        from pinky_daemon.transport_state import SessionState as TransportState

        async def fake_connect(self):
            if self.agent_name == "broken":
                raise RuntimeError("disposable startup failure")
            self._state_machine._state = TransportState.CONNECTED

        async def fake_disconnect(self):
            self._state_machine._state = TransportState.DEAD

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.codex_tmux_session.CodexTmuxSession.connect", new=fake_connect), \
                patch("pinky_daemon.codex_tmux_session.CodexTmuxSession.disconnect", new=fake_disconnect), \
                patch("pinky_daemon.api.SHARED_MCP_ENABLED", False):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            for name, model, effort in (
                ("main", "gpt-5.5", "medium"),
                ("murzik", "gpt-5.6-sol", "xhigh"),
                ("dormant", "gpt-5.5", "high"),
                ("broken", "gpt-5.5", "high"),
            ):
                app.state.agents.register(
                    name,
                    model=model,
                    provider_model="gpt-5.5" if name == "murzik" else "",
                    thinking_effort=effort,
                    runtime="codex_cli",
                    transport="tmux",
                    working_dir=os.path.join(tmpdir, name),
                )
                app.state.agents.set_streaming_session_id(
                    name, f"resume-{name}", label="main"
                )
            app.state.agents.set_main_agent("main")
            Path(tmpdir, "restart_manifest.json").write_text(json.dumps({
                "restart_time": datetime.now(timezone.utc).isoformat(),
                "planned": True,
                "agents": {
                    "murzik": {"label": "main"},
                    "broken": {"label": "main"},
                },
            }))

            with TestClient(app) as client:
                assert client.get("/admin/watchdog").status_code == 200
                assert "main" in app.state.broker._streaming
                assert "murzik" in app.state.broker._streaming
                assert "dormant" not in app.state.broker._streaming
                assert "broken" not in app.state.broker._streaming

                rebuilt = app.state.broker._streaming["murzik"]["main"]
                assert rebuilt._config.model == "gpt-5.6-sol"
                assert rebuilt._config.thinking_effort == "xhigh"
                assert rebuilt._codex_model == "gpt-5.6-sol"
                assert rebuilt._reasoning_effort == "xhigh"

    def test_retained_rebuild_refreshes_model_and_effort_from_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register(
                "murzik",
                model="gpt-5.6-sol",
                provider_model="gpt-5.5",
                thinking_effort="xhigh",
                runtime="codex_cli",
                transport="tmux",
                working_dir=os.path.join(tmpdir, "murzik"),
            )
            config = SimpleNamespace(
                model="gpt-5.5",
                provider_url="",
                provider_key="",
                thinking_effort="high",
                strict_effort_enforcement=False,
            )
            retained = SimpleNamespace(
                _config=config,
                _codex_model="gpt-5.5",
                _reasoning_effort="high",
                _openai_api_key="",
                _effort_override=None,
            )

            app.state._refresh_streaming_launch_config("murzik", retained)

            assert config.model == "gpt-5.6-sol"
            assert config.thinking_effort == "xhigh"
            assert retained._codex_model == "gpt-5.6-sol"
            assert retained._reasoning_effort == "xhigh"

    @pytest.mark.asyncio
    async def test_inbound_idle_wake_refreshes_retained_launch_config(self):
        """#856: broker idle auto-wake must route through the API ensurer."""
        from pinky_daemon.transport_state import SessionState as TransportState

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register(
                "murzik",
                model="gpt-5.6-sol",
                provider_model="gpt-5.5",
                thinking_effort="xhigh",
                runtime="codex_cli",
                transport="tmux",
                working_dir=os.path.join(tmpdir, "murzik"),
            )

            class _RetainedSleepingSession:
                def __init__(self):
                    self.state = TransportState.IDLE_SLEEPING
                    self.resume_handle = "codex-resume"
                    self._config = SimpleNamespace(
                        model="gpt-5.5",
                        provider_url="codex_cli",
                        provider_key="",
                        thinking_effort="high",
                        strict_effort_enforcement=False,
                    )
                    self._codex_model = "gpt-5.5"
                    self._reasoning_effort = "high"
                    self._openai_api_key = ""
                    self._effort_override = None
                    self.sent = []

                async def connect(self):
                    self.state = TransportState.CONNECTED

                async def send(self, prompt, **kwargs):
                    self.sent.append((prompt, kwargs))

            retained = _RetainedSleepingSession()
            app.state.broker.register_streaming("murzik", retained, label="main")
            message = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="wake with current defaults",
                agent_name="murzik",
            )

            await app.state.broker._route_streaming("murzik", message)

            assert retained.state == TransportState.CONNECTED
            assert retained.sent
            assert retained._config.model == "gpt-5.6-sol"
            assert retained._config.thinking_effort == "xhigh"
            assert retained._codex_model == "gpt-5.6-sol"
            assert retained._reasoning_effort == "xhigh"

    def test_shutdown_manifest_excludes_idle_sleeping_siblings(self):
        from pathlib import Path

        from pinky_daemon.transport_state import SessionState as TransportState

        class _ManifestSession:
            def __init__(self, state):
                self.state = state
                self.stats = {
                    "current_activity": "",
                    "activity_log": [],
                    "pending_responses": 0,
                }

            async def disconnect(self):
                self.state = TransportState.DEAD

        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.api.SHARED_MCP_ENABLED", False):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app):
                app.state.agents.register(
                    "idle", working_dir=os.path.join(tmpdir, "idle")
                )
                app.state.agents.register(
                    "active", working_dir=os.path.join(tmpdir, "active")
                )
                app.state.broker.register_streaming(
                    "idle",
                    _ManifestSession(TransportState.IDLE_SLEEPING),
                    label="main",
                )
                app.state.broker.register_streaming(
                    "active",
                    _ManifestSession(TransportState.CONNECTED),
                    label="main",
                )

            manifest = json.loads(
                Path(tmpdir, "restart_manifest.json").read_text()
            )
            assert set(manifest["agents"]) == {"active"}

    def test_put_effort_is_partial_and_durable(self):
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch("pinky_daemon.api.SHARED_MCP_ENABLED", False):
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                app.state.agents.register(
                    "murzik",
                    display_name="Murzik",
                    model="gpt-5.6-sol",
                    thinking_effort="high",
                    runtime="codex_cli",
                    transport="tmux",
                    groups=["reviewers"],
                    working_dir=os.path.join(tmpdir, "murzik"),
                )

                response = client.put(
                    "/agents/murzik/effort", json={"effort": "xhigh"}
                )
                assert response.status_code == 200
                assert response.json() == {"agent": "murzik", "default": "xhigh"}
                current = client.get("/agents/murzik/effort").json()
                assert current["default"] == "xhigh"

                unchanged = app.state.agents.get("murzik")
                assert unchanged.display_name == "Murzik"
                assert unchanged.model == "gpt-5.6-sol"
                assert unchanged.runtime == "codex_cli"
                assert unchanged.transport == "tmux"
                assert unchanged.groups == ["reviewers"]
                registry_path = app.state.agents._db.execute(
                    "PRAGMA database_list"
                ).fetchone()[2]

            with sqlite3.connect(registry_path) as reopened:
                row = reopened.execute(
                    "SELECT thinking_effort FROM agents WHERE name=?", ("murzik",)
                ).fetchone()
            assert row == ("xhigh",)

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

    def test_broker_send_registers_context_for_outbound_self_thread(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                with patch(
                    "pinky_outreach.telegram.TelegramAdapter.send_message",
                    side_effect=[
                        SimpleNamespace(message_id="501"),
                        SimpleNamespace(message_id="502"),
                    ],
                ) as send:
                    first = client.post(
                        "/broker/send",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "content": "First outbound",
                        },
                    )
                    second = client.post(
                        "/broker/thread",
                        json={
                            "agent_name": "barsik",
                            "message_id": "501",
                            "content": "Reply to myself",
                        },
                    )

                assert first.status_code == 200, first.text
                assert second.status_code == 200, second.text
                context = app.state.broker.get_message_context("barsik", "501")
                assert context is not None
                assert context.platform == "telegram"
                assert context.chat_id == "6770805286"
                assert context.metadata == {"direction": "outbound"}
                stored = app.state.message_context_store.get("barsik", "501")
                assert stored is not None
                assert stored["platform"] == "telegram"
                assert stored["chat_id"] == "6770805286"
                assert stored["metadata"] == {"direction": "outbound"}
                assert send.call_args_list[1].kwargs["reply_to_message_id"] == 501

    def test_broker_thread_fails_closed_on_chat_scoped_message_id_collision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            context_db_path = db_path.replace(".db", "_message_context.db")
            durable = MessageContextStore(context_db_path)
            durable.put({
                "agent_name": "barsik",
                "message_id": "1",
                "platform": "telegram",
                "chat_id": "CHAT_A",
                "timestamp": time.time(),
            })
            durable.close()

            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                assert app.state.broker._message_contexts == {}
                app.state.broker.remember_outbound_message_context(
                    "barsik", "1", platform="telegram", chat_id="CHAT_B"
                )
                assert len(app.state.broker._message_contexts) == 1
                assert app.state.message_context_store.get("barsik", "1") is None

                with patch(
                    "pinky_outreach.telegram.TelegramAdapter.send_message"
                ) as send:
                    response = client.post(
                        "/broker/thread",
                        json={
                            "agent_name": "barsik",
                            "message_id": "1",
                            "content": "must not route to either chat",
                        },
                    )

                assert response.status_code == 404
                assert "Message context '1' not found" in response.json()["detail"]
                send.assert_not_called()

    def test_broker_thread_child_reply_uses_stored_root_ts(self):
        """A reply to a Slack child stays on the original thread root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "slack", "xoxb-test")
                app.state.broker.remember_message_context(
                    BrokerMessage(
                        platform="slack", chat_id="C123", sender_name="Alex",
                        sender_id="U123", content="child reply", agent_name="barsik",
                        message_id="1711584001.000200",
                        reply_to="1711584000.000100",
                        is_group=True,
                    )
                )

                with patch(
                    "pinky_outreach.slack.SlackAdapter.send_message",
                    return_value=SimpleNamespace(message_id="1711584002.000300"),
                ) as mock:
                    resp = client.post("/broker/thread", json={
                        "agent_name": "barsik",
                        "message_id": "1711584001.000200",
                        "content": "on the same thread",
                    })

                assert resp.status_code == 200, resp.text
                assert mock.call_args.kwargs["thread_ts"] == "1711584000.000100"

    @pytest.mark.asyncio
    async def test_broker_send_binds_account_before_delivery_and_dedupe(self):
        """#863: requested account must select the token and dedupe identity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            agents = app.state.agents
            agents.register("barsik", model="sonnet", working_dir=tmpdir)
            callback = app.state.broker.send_callback

            with patch(
                "pinky_outreach.slack.SlackAdapter.send_message",
                return_value=SimpleNamespace(message_id="1711584000.000100"),
            ) as send:
                # Missing binding: fail before adapter construction/delivery.
                agents.set_token("barsik", "slack", "xoxb-test", settings={})
                with pytest.raises(Exception) as missing:
                    await callback(
                        "barsik", "slack", "D_OWNER", "approve?",
                        account_id="T_OWNER_A",
                    )
                assert missing.value.status_code == 409
                assert send.call_count == 0

                # Mismatched binding: also zero delivery.
                agents.set_token(
                    "barsik", "slack", "xoxb-test",
                    settings={"team_id": "T_OTHER"},
                )
                with pytest.raises(Exception) as mismatch:
                    await callback(
                        "barsik", "slack", "D_OWNER", "approve?",
                        account_id="T_OWNER_A",
                    )
                assert mismatch.value.status_code == 409
                assert send.call_count == 0

                # Exact account A sends. Rotating the explicit binding to B and
                # repeating the same payload sends again (account is in dedupe).
                agents.set_token(
                    "barsik", "slack", "xoxb-test",
                    settings={"team_id": "T_OWNER_A"},
                )
                first = await callback(
                    "barsik", "slack", "D_OWNER", "approve?",
                    account_id="T_OWNER_A",
                )
                agents.set_token(
                    "barsik", "slack", "xoxb-test",
                    settings={"team_id": "T_OWNER_B"},
                )
                second = await callback(
                    "barsik", "slack", "D_OWNER", "approve?",
                    account_id="T_OWNER_B",
                )
                duplicate = await callback(
                    "barsik", "slack", "D_OWNER", "approve?",
                    account_id="T_OWNER_B",
                )

                assert first["account_id"] == "T_OWNER_A"
                assert second["account_id"] == "T_OWNER_B"
                assert duplicate["deduped"] is True
                assert send.call_count == 2

    def test_broker_thread_quote_builds_reply_parameters(self):
        """thread(quote=...) builds ReplyParameters quoting the given passage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                app.state.broker.remember_message_context(
                    BrokerMessage(
                        platform="telegram", chat_id="6770805286", sender_name="Brad",
                        sender_id="u1", content="The deploy failed at step 3",
                        agent_name="barsik", message_id="101",
                    )
                )
                with patch(
                    "pinky_outreach.telegram.TelegramAdapter.send_message",
                    return_value=SimpleNamespace(message_id="501"),
                ) as mock:
                    resp = client.post("/broker/thread", json={
                        "agent_name": "barsik", "message_id": "101",
                        "content": "looking now", "quote": "step 3",
                    })
                assert resp.status_code == 200, resp.text
                assert mock.call_args.kwargs["reply_parameters"] == {
                    "message_id": 101, "quote": "step 3"
                }

    def test_broker_thread_without_quote_has_no_reply_parameters(self):
        """A plain thread() reply keeps reply_parameters=None (legacy path)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                app.state.broker.remember_message_context(
                    BrokerMessage(
                        platform="telegram", chat_id="6770805286", sender_name="Brad",
                        sender_id="u1", content="hi", agent_name="barsik", message_id="101",
                    )
                )
                with patch(
                    "pinky_outreach.telegram.TelegramAdapter.send_message",
                    return_value=SimpleNamespace(message_id="501"),
                ) as mock:
                    resp = client.post("/broker/thread", json={
                        "agent_name": "barsik", "message_id": "101", "content": "ok",
                    })
                assert resp.status_code == 200, resp.text
                assert mock.call_args.kwargs["reply_parameters"] is None
                assert mock.call_args.kwargs["reply_to_message_id"] == 101

    def test_broker_thread_invalid_quote_degrades_to_plain_reply(self):
        """An invalid quote (rejected by Telegram) degrades to a normal reply
        instead of dropping the whole message (PR2 review high finding)."""
        from pinky_outreach.telegram import TelegramError

        calls = []

        def _send(*args, **kwargs):
            calls.append(kwargs.get("reply_parameters"))
            if kwargs.get("reply_parameters") is not None:
                raise TelegramError("Bad Request: QUOTE_TEXT_INVALID", 400)
            return SimpleNamespace(message_id="777")

        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                app.state.broker.remember_message_context(
                    BrokerMessage(
                        platform="telegram", chat_id="6770805286", sender_name="Brad",
                        sender_id="u1", content="hello world", agent_name="barsik",
                        message_id="101",
                    )
                )
                with patch("pinky_outreach.telegram.TelegramAdapter.send_message", side_effect=_send):
                    resp = client.post("/broker/thread", json={
                        "agent_name": "barsik", "message_id": "101",
                        "content": "looking", "quote": "not a substring",
                    })
                # The reply still lands (no 500), and the degrade dropped the quote.
                assert resp.status_code == 200, resp.text
                assert resp.json()["message_id"] == "777"
                assert calls[0] == {"message_id": 101, "quote": "not a substring"}
                assert calls[-1] is None

    def test_broker_thread_whitespace_quote_is_dropped(self):
        """A whitespace-only quote is stripped to empty → no reply_parameters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                app.state.broker.remember_message_context(
                    BrokerMessage(
                        platform="telegram", chat_id="6770805286", sender_name="Brad",
                        sender_id="u1", content="hi there", agent_name="barsik",
                        message_id="101",
                    )
                )
                with patch(
                    "pinky_outreach.telegram.TelegramAdapter.send_message",
                    return_value=SimpleNamespace(message_id="501"),
                ) as mock:
                    resp = client.post("/broker/thread", json={
                        "agent_name": "barsik", "message_id": "101",
                        "content": "ok", "quote": "   ",
                    })
                assert resp.status_code == 200, resp.text
                assert mock.call_args.kwargs["reply_parameters"] is None

    def test_broker_thread_overlong_quote_is_capped(self):
        """A quote longer than Telegram's 1024-char limit is capped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                app.state.broker.remember_message_context(
                    BrokerMessage(
                        platform="telegram", chat_id="6770805286", sender_name="Brad",
                        sender_id="u1", content="x" * 4000, agent_name="barsik",
                        message_id="101",
                    )
                )
                with patch(
                    "pinky_outreach.telegram.TelegramAdapter.send_message",
                    return_value=SimpleNamespace(message_id="501"),
                ) as mock:
                    resp = client.post("/broker/thread", json={
                        "agent_name": "barsik", "message_id": "101",
                        "content": "ok", "quote": "x" * 2000,
                    })
                assert resp.status_code == 200, resp.text
                assert len(mock.call_args.kwargs["reply_parameters"]["quote"]) == 1024

    def test_broker_media_endpoints_scrub_file_path_from_metadata(self):
        """Regression: /broker/send-photo, /send-document, /send-animation must not
        persist the raw file_path to conversation metadata. PR #244 scrubbed the
        codex/claude analytics path to arg_keys-only; Task #69 extends that
        guarantee to the outbound-message metadata written by these endpoints.
        """
        from pinky_outreach.telegram import TelegramAdapter

        cases = [
            ("/broker/send-photo", "send_photo", "send_photo", "/tmp/brads-private.png", "[photo]"),
            ("/broker/send-document", "send_document", "send_document", "/home/brad/secret-doc.pdf", "[document] secret-doc.pdf"),
            ("/broker/send-animation", "send_animation", "send_animation", "/tmp/brads-gif.gif", "[animation] brads-gif.gif"),
            ("/broker/send-video", "send_video", "send_video", "/home/brad/private-clip.mp4", "[video] private-clip.mp4"),
        ]

        for url, adapter_method, tool_name, file_path, expected_content in cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = os.path.join(tmpdir, "test.db")
                app = self._make_app(db_path)
                with TestClient(app) as client:
                    client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                    app.state.agents.set_token("barsik", "telegram", "bot123")

                    with patch.object(
                        TelegramAdapter,
                        adapter_method,
                        return_value=SimpleNamespace(message_id="999"),
                    ):
                        resp = client.post(
                            url,
                            json={
                                "agent_name": "barsik",
                                "platform": "telegram",
                                "chat_id": "6770805286",
                                "file_path": file_path,
                            },
                        )

                    assert resp.status_code == 200, f"{url} returned {resp.status_code}: {resp.text}"

                    history = app.state.conversation_store.get_history("barsik-main")
                    assert history, f"{url} recorded no outbound message"
                    entry = history[-1]
                    assert entry.content == expected_content
                    assert entry.metadata["tool"] == tool_name
                    # PII-safe: only the arg_keys name, never the raw path.
                    assert entry.metadata.get("arg_keys") == ["file_path"]
                    assert "file_path" not in entry.metadata, (
                        f"{url} leaked raw file_path into metadata: {entry.metadata}"
                    )
                    # Defense-in-depth: the raw value must not appear anywhere
                    # in the serialized metadata blob.
                    import json as _json
                    assert file_path not in _json.dumps(entry.metadata), (
                        f"{url} leaked raw file_path value into metadata payload"
                    )
                    context = app.state.broker.get_message_context("barsik", "999")
                    assert context is not None, f"{url} did not register its outbound message"
                    assert context.platform == "telegram"
                    assert context.chat_id == "6770805286"

    def test_broker_send_photo_threads_caption_format_and_spoiler(self):
        """/broker/send-photo formats the caption (MarkdownV2) and forwards
        has_spoiler / show_caption_above_media to the adapter."""
        from datetime import datetime, timezone

        from pinky_outreach.telegram import TelegramAdapter
        from pinky_outreach.types import Message, Platform

        sent = Message(
            platform=Platform.telegram, chat_id="6770805286", sender="bot",
            content="x", timestamp=datetime(2026, 6, 20, tzinfo=timezone.utc),
            message_id="42", is_outbound=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                with patch.object(TelegramAdapter, "send_photo", return_value=sent) as mock_photo:
                    resp = client.post(
                        "/broker/send-photo",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "file_path": "/tmp/chart.png",
                            "caption": "**bold** and ||secret||",
                            "has_spoiler": True,
                            "show_caption_above_media": True,
                        },
                    )
                assert resp.status_code == 200, resp.text
                kwargs = mock_photo.call_args.kwargs
                assert kwargs["caption_parse_mode"] == "MarkdownV2"
                assert kwargs["has_spoiler"] is True
                assert kwargs["show_caption_above_media"] is True
                # Caption was run through the MarkdownV2 converter, not sent raw.
                assert "*bold*" in kwargs["caption"]
                assert "||secret||" in kwargs["caption"]
                assert "**bold**" not in kwargs["caption"]

    def test_broker_send_threads_link_preview_options(self):
        """/broker/send forwards link_preview_options to the adapter."""
        from datetime import datetime, timezone

        from pinky_outreach.telegram import TelegramAdapter
        from pinky_outreach.types import Message, Platform

        sent = Message(
            platform=Platform.telegram, chat_id="6770805286", sender="bot",
            content="x", timestamp=datetime(2026, 6, 20, tzinfo=timezone.utc),
            message_id="43", is_outbound=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                with patch.object(TelegramAdapter, "send_message", return_value=sent) as mock_send:
                    resp = client.post(
                        "/broker/send",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "content": "see https://example.com",
                            "link_preview_options": {"is_disabled": True},
                        },
                    )
                assert resp.status_code == 200, resp.text
                assert mock_send.call_args.kwargs["link_preview_options"] == {"is_disabled": True}

    def test_broker_send_photo_strict_bool_rejects_string_false(self):
        """A JSON string "false" must NOT enable has_spoiler — bool("false") is
        True, so the route must use strict identity (json-gate/bool-is-int)."""
        from datetime import datetime, timezone

        from pinky_outreach.telegram import TelegramAdapter
        from pinky_outreach.types import Message, Platform

        sent = Message(
            platform=Platform.telegram, chat_id="6770805286", sender="bot",
            content="x", timestamp=datetime(2026, 6, 20, tzinfo=timezone.utc),
            message_id="44", is_outbound=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                with patch.object(TelegramAdapter, "send_photo", return_value=sent) as mock_photo:
                    resp = client.post(
                        "/broker/send-photo",
                        json={
                            "agent_name": "barsik", "platform": "telegram",
                            "chat_id": "6770805286", "file_path": "/tmp/x.png",
                            "has_spoiler": "false", "show_caption_above_media": "false",
                        },
                    )
                assert resp.status_code == 200, resp.text
                kwargs = mock_photo.call_args.kwargs
                assert kwargs["has_spoiler"] is False
                assert kwargs["show_caption_above_media"] is False

    def test_broker_send_drops_link_preview_url_injection(self):
        """The model-controlled url override is stripped; only the bool
        preview-shape flags reach Telegram (defense-in-depth)."""
        from datetime import datetime, timezone

        from pinky_outreach.telegram import TelegramAdapter
        from pinky_outreach.types import Message, Platform

        sent = Message(
            platform=Platform.telegram, chat_id="6770805286", sender="bot",
            content="x", timestamp=datetime(2026, 6, 20, tzinfo=timezone.utc),
            message_id="45", is_outbound=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")
                with patch.object(TelegramAdapter, "send_message", return_value=sent) as mock_send:
                    resp = client.post(
                        "/broker/send",
                        json={
                            "agent_name": "barsik", "platform": "telegram",
                            "chat_id": "6770805286", "content": "hi",
                            "link_preview_options": {
                                "url": "https://attacker.example",
                                "is_disabled": True,
                                "show_above_text": False,
                            },
                        },
                    )
                assert resp.status_code == 200, resp.text
                # url dropped (injection vector); False flag dropped; only the
                # explicitly-True bool survives.
                assert mock_send.call_args.kwargs["link_preview_options"] == {"is_disabled": True}

    def test_broker_send_document_404_on_telegram_error_returns_structured_502(self):
        """Issue #395 regression: when the Telegram client raises (e.g. 400 Bad Request
        on sendDocument), the broker route must translate it to a structured 502 instead
        of letting the exception bubble as an unhandled ASGI error.

        Also asserts the typing indicator is torn down even on the failure path so a
        failed send doesn't leave the chat showing "typing…" forever.
        """
        from pinky_outreach.telegram import TelegramAdapter, TelegramError

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                stop_typing_calls = []
                orig_stop_typing = app.state.broker._stop_typing
                def _spy_stop_typing(agent, chat):
                    stop_typing_calls.append((agent, chat))
                    return orig_stop_typing(agent, chat)
                app.state.broker._stop_typing = _spy_stop_typing

                with patch.object(
                    TelegramAdapter,
                    "send_document",
                    side_effect=TelegramError("Bad Request: file must be non-empty", 400),
                ):
                    resp = client.post(
                        "/broker/send-document",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "file_path": "/tmp/empty.pdf",
                        },
                    )

                assert resp.status_code == 502, f"expected 502, got {resp.status_code}: {resp.text}"
                body = resp.json()
                assert "send_document" in body.get("detail", ""), body
                assert "TelegramError" in body.get("detail", ""), body

                # Typing indicator must be cleaned up even on failure.
                assert ("barsik", "6770805286") in stop_typing_calls, (
                    f"stop_typing not called on failure path: {stop_typing_calls}"
                )

                # No outbound message should be recorded for a failed send.
                history = app.state.conversation_store.get_history("barsik-main")
                assert not any(
                    e.metadata.get("tool") == "send_document" for e in history
                ), f"failed send leaked into conversation history: {history}"

    def test_broker_send_photo_400_on_telegram_error_returns_structured_502(self):
        """Issue #395 regression: same as send_document but for the send_photo path."""
        from pinky_outreach.telegram import TelegramAdapter, TelegramError

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                stop_typing_calls = []
                orig_stop_typing = app.state.broker._stop_typing
                def _spy_stop_typing(agent, chat):
                    stop_typing_calls.append((agent, chat))
                    return orig_stop_typing(agent, chat)
                app.state.broker._stop_typing = _spy_stop_typing

                with patch.object(
                    TelegramAdapter,
                    "send_photo",
                    side_effect=TelegramError("Bad Request: PHOTO_INVALID_DIMENSIONS", 400),
                ):
                    resp = client.post(
                        "/broker/send-photo",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "file_path": "/tmp/bad.png",
                        },
                    )

                assert resp.status_code == 502, f"expected 502, got {resp.status_code}: {resp.text}"
                assert ("barsik", "6770805286") in stop_typing_calls

    def test_broker_send_document_filenotfound_returns_structured_400(self):
        """Issue #395 regression + #408 follow-up: missing file path raises
        FileNotFoundError inside the Telegram adapter (`open(file_path, 'rb')`).
        Must surface as a structured response (not unhandled ASGI 500). Status
        is 400 (bad caller input) since no upstream call has happened — this
        was 502 before the #408 outcome-bucket split carved FileNotFoundError
        out as `rejected`."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                resp = client.post(
                    "/broker/send-document",
                    json={
                        "agent_name": "barsik",
                        "platform": "telegram",
                        "chat_id": "6770805286",
                        "file_path": "/tmp/does-not-exist-xyz.pdf",
                    },
                )

                # The adapter will try open() before any HTTP call — that's a real
                # FileNotFoundError, which the route must wrap as a 400 (rejected).
                assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
                assert "FileNotFoundError" in resp.json().get("detail", "")

    def test_broker_send_animation_telegram_error_returns_structured_502(self):
        """Issue #395 follow-up: /broker/send-animation must translate adapter
        exceptions to a structured 502 and tear down the typing indicator on
        failure (previously had no try/except — exceptions bubbled as ASGI 500
        and typing got stuck).
        """
        from pinky_outreach.telegram import TelegramAdapter, TelegramError

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                stop_typing_calls = []
                orig_stop_typing = app.state.broker._stop_typing
                def _spy_stop_typing(agent, chat):
                    stop_typing_calls.append((agent, chat))
                    return orig_stop_typing(agent, chat)
                app.state.broker._stop_typing = _spy_stop_typing

                with patch.object(
                    TelegramAdapter,
                    "send_animation",
                    side_effect=TelegramError("Bad Request: ANIMATION_INVALID", 400),
                ):
                    resp = client.post(
                        "/broker/send-animation",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "file_path": "/tmp/bad.gif",
                        },
                    )

                assert resp.status_code == 502, f"expected 502, got {resp.status_code}: {resp.text}"
                body = resp.json()
                assert "send_animation" in body.get("detail", ""), body
                assert "TelegramError" in body.get("detail", ""), body

                # Typing indicator must be cleaned up even on failure.
                assert ("barsik", "6770805286") in stop_typing_calls, (
                    f"stop_typing not called on failure path: {stop_typing_calls}"
                )

                # No outbound message should be recorded for a failed send.
                history = app.state.conversation_store.get_history("barsik-main")
                assert not any(
                    e.metadata.get("tool") == "send_animation" for e in history
                ), f"failed send leaked into conversation history: {history}"

    def test_broker_send_animation_filenotfound_returns_structured_400(self):
        """Issue #395 follow-up + #408 outcome-bucket follow-up: missing
        animation file must surface as a structured response (not unhandled
        ASGI 500) and still tear down the typing indicator (phantom typing
        dots after a missing file was one of the original #395 symptoms).
        Status is 400 (bad caller input — `rejected` bucket) since no
        upstream call has happened yet; was 502 before the FileNotFoundError
        carve-out in `_broker_send_file_route`.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                stop_typing_calls = []
                orig_stop_typing = app.state.broker._stop_typing
                def _spy_stop_typing(agent, chat):
                    stop_typing_calls.append((agent, chat))
                    return orig_stop_typing(agent, chat)
                app.state.broker._stop_typing = _spy_stop_typing

                resp = client.post(
                    "/broker/send-animation",
                    json={
                        "agent_name": "barsik",
                        "platform": "telegram",
                        "chat_id": "6770805286",
                        "file_path": "/tmp/does-not-exist-xyz.gif",
                    },
                )

                assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
                assert "FileNotFoundError" in resp.json().get("detail", "")
                assert ("barsik", "6770805286") in stop_typing_calls, (
                    f"stop_typing not called on failure path: {stop_typing_calls}"
                )

    def test_broker_send_gif_telegram_error_returns_structured_502(self):
        """Issue #395 follow-up: /broker/send-gif must translate adapter
        exceptions to a structured 502 and tear down the typing indicator on
        failure (previously returned bare 500 and typing got stuck on failure).
        """
        from pinky_outreach.telegram import TelegramAdapter, TelegramError

        class _GiphyResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return json.dumps({
                    "data": [{"images": {"original": {"url": "https://media.giphy.com/test.gif?cid=abc"}}}]
                }).encode()

        class _GifBlobResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return b"GIF89a-bytes"

        def _urlopen_side_effect(req_or_url, *args, **kwargs):
            url = req_or_url if isinstance(req_or_url, str) else getattr(req_or_url, "full_url", "")
            if "giphy.com/v1/gifs/search" in url:
                return _GiphyResp()
            return _GifBlobResp()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                stop_typing_calls = []
                orig_stop_typing = app.state.broker._stop_typing
                def _spy_stop_typing(agent, chat):
                    stop_typing_calls.append((agent, chat))
                    return orig_stop_typing(agent, chat)
                app.state.broker._stop_typing = _spy_stop_typing

                with patch("urllib.request.urlopen", side_effect=_urlopen_side_effect), \
                        patch.object(
                            TelegramAdapter,
                            "send_animation",
                            side_effect=TelegramError("Bad Request: ANIMATION_INVALID", 400),
                        ):
                    resp = client.post(
                        "/broker/send-gif",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "query": "cat typing",
                        },
                    )

                assert resp.status_code == 502, f"expected 502, got {resp.status_code}: {resp.text}"
                body = resp.json()
                assert "send_gif" in body.get("detail", ""), body
                assert "TelegramError" in body.get("detail", ""), body

                assert ("barsik", "6770805286") in stop_typing_calls, (
                    f"stop_typing not called on failure path: {stop_typing_calls}"
                )

                history = app.state.conversation_store.get_history("barsik-main")
                assert not any(
                    e.metadata.get("tool") == "send_gif" for e in history
                ), f"failed send leaked into conversation history: {history}"

    def test_broker_send_gif_search_failure_stops_typing(self):
        """Issue #397 follow-up (Murzik P1): when the Giphy search call itself
        raises (e.g. urlopen timeout/connection error), the handler must still
        tear down the typing indicator. Previously the search HTTPException was
        raised before the try/finally was entered, leaving typing dots stuck —
        same bug class as the original incident #395.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                stop_typing_calls = []
                orig_stop_typing = app.state.broker._stop_typing
                def _spy_stop_typing(agent, chat):
                    stop_typing_calls.append((agent, chat))
                    return orig_stop_typing(agent, chat)
                app.state.broker._stop_typing = _spy_stop_typing

                # Patch urlopen to raise during the Giphy search call.
                with patch(
                    "urllib.request.urlopen",
                    side_effect=TimeoutError("giphy timeout"),
                ):
                    resp = client.post(
                        "/broker/send-gif",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "query": "cat typing",
                        },
                    )

                assert resp.status_code == 502, f"expected 502, got {resp.status_code}: {resp.text}"
                body = resp.json()
                assert "Giphy search failed" in body.get("detail", ""), body

                # Typing indicator must be cleaned up even when the search
                # itself fails before download+send is attempted.
                assert stop_typing_calls.count(("barsik", "6770805286")) == 1, (
                    f"stop_typing not called exactly once on search-failure path: {stop_typing_calls}"
                )

                # Failed send must not leak into history.
                history = app.state.conversation_store.get_history("barsik-main")
                assert not any(
                    e.metadata.get("tool") == "send_gif" for e in history
                ), f"failed send leaked into conversation history: {history}"

    def test_broker_send_gif_no_results_stops_typing(self):
        """Issue #397 follow-up (Murzik P1): the no-results 404 path must also
        tear down the typing indicator. Previously this raise happened before
        the try/finally was entered.
        """
        class _EmptyGiphyResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return json.dumps({"data": []}).encode()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                stop_typing_calls = []
                orig_stop_typing = app.state.broker._stop_typing
                def _spy_stop_typing(agent, chat):
                    stop_typing_calls.append((agent, chat))
                    return orig_stop_typing(agent, chat)
                app.state.broker._stop_typing = _spy_stop_typing

                with patch(
                    "urllib.request.urlopen",
                    return_value=_EmptyGiphyResp(),
                ):
                    resp = client.post(
                        "/broker/send-gif",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "query": "asdfghjklqwerty-no-such-thing",
                        },
                    )

                # Current behavior: empty results still surfaces as 404.
                assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
                body = resp.json()
                assert "No GIFs found" in body.get("detail", ""), body

                # Typing indicator must still be cleaned up.
                assert stop_typing_calls.count(("barsik", "6770805286")) == 1, (
                    f"stop_typing not called exactly once on no-results path: {stop_typing_calls}"
                )

    def test_broker_send_voice_telegram_error_returns_structured_502(self):
        """Issue #395 follow-up: /broker/send-voice must translate adapter
        exceptions to a structured 502 and tear down the typing indicator on
        failure (previously returned bare 500 and typing got stuck on failure).
        """
        from pinky_outreach.telegram import TelegramAdapter, TelegramError

        class _TtsResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return b"opus-bytes"

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_setting("OPENAI_API_KEY", "test-key")
                app.state.agents.set_token("barsik", "telegram", "bot123")

                stop_typing_calls = []
                orig_stop_typing = app.state.broker._stop_typing
                def _spy_stop_typing(agent, chat):
                    stop_typing_calls.append((agent, chat))
                    return orig_stop_typing(agent, chat)
                app.state.broker._stop_typing = _spy_stop_typing

                with patch("urllib.request.urlopen", return_value=_TtsResp()), \
                        patch.object(
                            TelegramAdapter,
                            "send_voice",
                            side_effect=TelegramError("Bad Request: VOICE_TOO_LONG", 400),
                        ):
                    resp = client.post(
                        "/broker/send-voice",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "text": "hello world",
                            "provider": "openai",
                        },
                    )

                assert resp.status_code == 502, f"expected 502, got {resp.status_code}: {resp.text}"
                body = resp.json()
                assert "send_voice" in body.get("detail", ""), body
                assert "TelegramError" in body.get("detail", ""), body

                assert ("barsik", "6770805286") in stop_typing_calls, (
                    f"stop_typing not called on failure path: {stop_typing_calls}"
                )

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


# ── Outreach Outcome Buckets (task #81 / issue #395 follow-up) ───────


class TestOutreachOutcomeBuckets:
    """Verify the 4-bucket OutreachOutcome enum is wired correctly into the
    broker handlers and emitted on `outreach-attempt` log lines.

    Buckets:
      - success         — adapter returned a message_id
      - rejected        — caller-side validation failed (HTTPException raised
                          by our code due to bad input / no results / missing
                          API key)
      - error_upstream  — Telegram / OpenAI / Giphy returned a real error
      - error_internal  — generic catch-all for genuinely unexpected exceptions
                          (defined for completeness; no organic call site today)
    """

    def _make_app(self, path: str):
        from pinky_daemon.api import create_api
        return create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)

    def _capture_outreach_logs(self):
        """Patch `pinky_daemon.api._log` to capture every log line emitted.

        Returns (mock, getter) where getter() yields only the structured
        `outreach-attempt:` lines from the captured log stream.
        """
        captured: list[str] = []

        def _fake_log(msg: str) -> None:
            captured.append(msg)

        return captured, _fake_log

    def test_outreach_outcome_logs_success_on_happy_path(self):
        """Happy-path send_gif must log `outcome=success` and no `error=` field."""
        from pinky_outreach.telegram import TelegramAdapter

        class _GiphyResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({
                    "data": [{"images": {"original": {"url": "https://media.giphy.com/test.gif?cid=abc"}}}]
                }).encode()

        class _GifBlobResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"GIF89a-bytes"

        def _urlopen_side_effect(req_or_url, *args, **kwargs):
            url = req_or_url if isinstance(req_or_url, str) else getattr(req_or_url, "full_url", "")
            if "giphy.com/v1/gifs/search" in url:
                return _GiphyResp()
            return _GifBlobResp()

        captured, fake_log = self._capture_outreach_logs()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                with patch("pinky_daemon.api._log", side_effect=fake_log), \
                        patch("urllib.request.urlopen", side_effect=_urlopen_side_effect), \
                        patch.object(
                            TelegramAdapter,
                            "send_animation",
                            return_value=SimpleNamespace(message_id="msg-42"),
                        ):
                    resp = client.post(
                        "/broker/send-gif",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "query": "happy cat",
                        },
                    )

                assert resp.status_code == 200, resp.text
                outreach_lines = [m for m in captured if m.startswith("outreach-attempt:")]
                assert any("outcome=success" in m and "method=send_gif" in m for m in outreach_lines), (
                    f"expected success outcome in outreach logs, got: {outreach_lines}"
                )
                # Success path must not append an `error=` field.
                success_lines = [m for m in outreach_lines if "outcome=success" in m]
                assert all(" error=" not in m for m in success_lines), success_lines

    def test_outreach_outcome_logs_rejected_on_no_giphy_results(self):
        """Empty Giphy results → `outcome=rejected error=no_results` (treating
        the empty-search case as caller-side: their query didn't match)."""
        class _EmptyGiphyResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"data": []}).encode()

        captured, fake_log = self._capture_outreach_logs()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                with patch("pinky_daemon.api._log", side_effect=fake_log), \
                        patch("urllib.request.urlopen", return_value=_EmptyGiphyResp()):
                    resp = client.post(
                        "/broker/send-gif",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "query": "asdfghjklqwerty-no-such-thing",
                        },
                    )

                assert resp.status_code == 404, resp.text
                outreach_lines = [m for m in captured if m.startswith("outreach-attempt:")]
                assert any(
                    "outcome=rejected" in m and "error=no_results" in m and "method=send_gif" in m
                    for m in outreach_lines
                ), f"expected rejected/no_results in outreach logs, got: {outreach_lines}"

    def test_outreach_outcome_logs_error_upstream_on_telegram_failure(self):
        """TelegramError from adapter.send_animation → `outcome=error_upstream`."""
        from pinky_outreach.telegram import TelegramAdapter, TelegramError

        class _GiphyResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({
                    "data": [{"images": {"original": {"url": "https://media.giphy.com/test.gif?cid=abc"}}}]
                }).encode()

        class _GifBlobResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"GIF89a-bytes"

        def _urlopen_side_effect(req_or_url, *args, **kwargs):
            url = req_or_url if isinstance(req_or_url, str) else getattr(req_or_url, "full_url", "")
            if "giphy.com/v1/gifs/search" in url:
                return _GiphyResp()
            return _GifBlobResp()

        captured, fake_log = self._capture_outreach_logs()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                with patch("pinky_daemon.api._log", side_effect=fake_log), \
                        patch("urllib.request.urlopen", side_effect=_urlopen_side_effect), \
                        patch.object(
                            TelegramAdapter,
                            "send_animation",
                            side_effect=TelegramError("Bad Request: ANIMATION_INVALID", 400),
                        ):
                    resp = client.post(
                        "/broker/send-gif",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "query": "cat typing",
                        },
                    )

                assert resp.status_code == 502, resp.text
                outreach_lines = [m for m in captured if m.startswith("outreach-attempt:")]
                assert any(
                    "outcome=error_upstream" in m and "TelegramError" in m and "method=send_gif" in m
                    for m in outreach_lines
                ), f"expected error_upstream/TelegramError in outreach logs, got: {outreach_lines}"

    def test_outreach_outcome_logs_rejected_on_send_document_filenotfound(self):
        """PR #408 follow-up (Murzik P2): `_broker_send_file_route` previously
        bucketed FileNotFoundError under the bare `except Exception` →
        `error_upstream`, but the open(file_path) failure happens BEFORE any
        Telegram/OpenAI/Giphy upstream call. Caller handed us a path we can't
        read, so it's a caller-side validation failure → `rejected`. Status
        code is 400 (bad input), not 502 (upstream-flavored)."""
        captured, fake_log = self._capture_outreach_logs()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            with TestClient(app) as client:
                client.post("/agents", json={"name": "barsik", "model": "sonnet"})
                app.state.agents.set_token("barsik", "telegram", "bot123")

                with patch("pinky_daemon.api._log", side_effect=fake_log):
                    resp = client.post(
                        "/broker/send-document",
                        json={
                            "agent_name": "barsik",
                            "platform": "telegram",
                            "chat_id": "6770805286",
                            "file_path": "/tmp/does-not-exist-xyz.pdf",
                        },
                    )

                assert resp.status_code == 400, resp.text
                assert "FileNotFoundError" in resp.json().get("detail", "")
                outreach_lines = [m for m in captured if m.startswith("outreach-attempt:")]
                assert any(
                    "outcome=rejected" in m
                    and "error=FileNotFoundError:" in m
                    and "method=send_document" in m
                    for m in outreach_lines
                ), f"expected rejected/FileNotFoundError in outreach logs, got: {outreach_lines}"
                # Must NOT be bucketed as error_upstream — that was the bug.
                assert not any(
                    "outcome=error_upstream" in m and "method=send_document" in m
                    for m in outreach_lines
                ), f"FileNotFoundError must not bucket as error_upstream: {outreach_lines}"

    def test_outreach_outcome_type_alias_covers_all_four_buckets(self):
        """Verify the OutreachOutcome Literal type alias defines exactly the
        four expected buckets. Static-analysis tools (mypy/pyright) enforce
        call-site correctness; this guards against accidental edits to the
        type alias itself.

        Includes `error_internal` even though no current handler emits it —
        the bucket is reserved for future genuinely-unexpected catch-alls.
        """
        from pinky_daemon.api import OutreachOutcome
        assert set(OutreachOutcome.__args__) == {
            "success",
            "rejected",
            "error_upstream",
            "error_internal",
        }


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


# Agent schedules


class TestAgentScheduleEndpoints:
    def _make_client(self):
        from pinky_daemon.api import create_api

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app)

    @staticmethod
    def _register(client, name):
        response = client.post("/agents", json={"name": name, "model": "sonnet"})
        assert response.status_code == 200

    @staticmethod
    def _create_schedule(client, agent_name="alice", **overrides):
        body = {
            "name": "morning",
            "cron": "0 8 * * *",
            "prompt": "Original",
            "timezone": "America/Los_Angeles",
            "direct_send": True,
            "target_channel": "123",
            "one_shot": True,
        }
        body.update(overrides)
        return client.post(f"/agents/{agent_name}/schedules", json=body)

    def test_patch_schedule_partial_update_preserves_id_and_omitted_fields(self):
        client = self._make_client()
        self._register(client, "alice")
        created = self._create_schedule(client).json()

        response = client.patch(
            f"/agents/alice/schedules/{created['id']}",
            json={
                "cron": "30 8 * * 1-5",
                "prompt": "Updated",
                "direct_send": False,
                "target_channel": "",
            },
        )

        assert response.status_code == 200
        updated = response.json()
        assert updated["id"] == created["id"]
        assert updated["name"] == "morning"
        assert updated["cron"] == "30 8 * * 1-5"
        assert updated["prompt"] == "Updated"
        assert updated["timezone"] == "America/Los_Angeles"
        assert updated["direct_send"] is False
        assert updated["target_channel"] == ""
        assert updated["one_shot"] is True

    def test_patch_schedule_unknown_id_returns_404(self):
        client = self._make_client()
        self._register(client, "alice")

        response = client.patch(
            "/agents/alice/schedules/999",
            json={"prompt": "Updated"},
        )

        assert response.status_code == 404

    def test_patch_schedule_refuses_cross_agent_edit(self):
        client = self._make_client()
        self._register(client, "alice")
        self._register(client, "bob")
        created = self._create_schedule(client).json()

        response = client.patch(
            f"/agents/bob/schedules/{created['id']}",
            json={"prompt": "Cross-agent edit"},
        )

        assert response.status_code == 404
        schedules = client.get(
            "/agents/alice/schedules",
            params={"enabled_only": False},
        ).json()["schedules"]
        assert schedules[0]["prompt"] == "Original"

    def test_patch_schedule_refuses_empty_update(self):
        client = self._make_client()
        self._register(client, "alice")
        created = self._create_schedule(client).json()

        response = client.patch(
            f"/agents/alice/schedules/{created['id']}",
            json={},
        )

        assert response.status_code == 400
        assert "at least one field" in response.json()["detail"]

    def test_patch_schedule_rejects_invalid_cron_without_mutating(self):
        client = self._make_client()
        self._register(client, "alice")
        created = self._create_schedule(client).json()

        response = client.patch(
            f"/agents/alice/schedules/{created['id']}",
            json={"cron": "99 * * * *"},
        )

        assert response.status_code == 400
        schedules = client.get(
            "/agents/alice/schedules",
            params={"enabled_only": False},
        ).json()["schedules"]
        assert schedules[0]["cron"] == "0 8 * * *"

    def test_create_schedule_rejects_enabled_duplicate_name(self):
        client = self._make_client()
        self._register(client, "alice")
        created = self._create_schedule(client).json()

        response = self._create_schedule(client, cron="0 9 * * *")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "distinct name" in detail
        assert f"update_wake_schedule with ID {created['id']}" in detail
        assert client.get("/agents/alice/schedules").json()["count"] == 1

    def test_patch_schedule_rejects_enabled_name_collision(self):
        client = self._make_client()
        self._register(client, "alice")
        existing = self._create_schedule(client).json()
        renamed = self._create_schedule(
            client,
            name="evening",
            cron="0 21 * * *",
        ).json()

        response = client.patch(
            f"/agents/alice/schedules/{renamed['id']}",
            json={"name": "morning"},
        )

        assert response.status_code == 409
        assert f"ID {existing['id']}" in response.json()["detail"]
        schedules = client.get("/agents/alice/schedules").json()["schedules"]
        assert [(row["id"], row["name"]) for row in schedules] == [
            (existing["id"], "morning"),
            (renamed["id"], "evening"),
        ]

    def test_toggle_schedule_rejects_reenable_name_collision(self):
        client = self._make_client()
        self._register(client, "alice")
        old = self._create_schedule(client).json()
        disabled = client.post(
            f"/agents/alice/schedules/{old['id']}/toggle",
            params={"enabled": False},
        )
        assert disabled.status_code == 200
        replacement = self._create_schedule(client, cron="0 9 * * *").json()

        response = client.post(
            f"/agents/alice/schedules/{old['id']}/toggle",
            params={"enabled": True},
        )

        assert response.status_code == 409
        assert f"ID {replacement['id']}" in response.json()["detail"]
        schedules = client.get(
            "/agents/alice/schedules",
            params={"enabled_only": False},
        ).json()["schedules"]
        assert [(row["id"], row["enabled"]) for row in schedules] == [
            (old["id"], False),
            (replacement["id"], True),
        ]

    @pytest.mark.parametrize("one_shot", [False, True])
    def test_create_schedule_reuses_disabled_name(self, one_shot):
        client = self._make_client()
        self._register(client, "alice")
        created = self._create_schedule(client, one_shot=one_shot).json()
        toggle = client.post(
            f"/agents/alice/schedules/{created['id']}/toggle",
            params={"enabled": False},
        )
        assert toggle.status_code == 200

        response = self._create_schedule(
            client,
            cron="0 9 * * *",
            one_shot=False,
        )

        assert response.status_code == 200
        assert response.json()["id"] != created["id"]

    def test_create_schedule_rejects_invalid_cron(self):
        client = self._make_client()
        self._register(client, "alice")

        response = self._create_schedule(client, cron="not a cron")

        assert response.status_code == 400


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
        resp = client.post("/agents", json={
            "name": "alice",
            "model": "sonnet",
            "runtime": "codex_cli",
            "provider_url": "codex_cli",
            "provider_model": "gpt-5-codex",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "alice"
        assert data["model"] == "sonnet"
        assert data["runtime"] == "codex_cli"
        assert data["transport"] == "sdk"
        assert data["provider_url"] == "codex_cli"
        assert data["provider_model"] == "gpt-5-codex"

    def test_update_agent_runtime(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})

        resp = client.put("/agents/alice", json={"runtime": "codex_cli"})
        assert resp.status_code == 200
        assert resp.json()["runtime"] == "codex_cli"

    def test_update_agent_transport(self):
        client = self._make_client()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})

        resp = client.put("/agents/alice", json={"transport": "tmux"})
        assert resp.status_code == 200
        assert resp.json()["transport"] == "tmux"

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

    def test_agent_health_surfaces_failed_owner_notification(self):
        client, registry = self._make_client_with_registry()
        client.post("/agents", json={"name": "alice", "model": "sonnet"})
        request = registry.record_approval_hold(
            "alice", "C_GATED", target_name="C_GATED", is_channel=True,
        )
        registry.record_approval_notification_failure(
            request["id"],
            error="RuntimeError: channel_not_found",
            next_retry_at=0,
            failed=True,
            fallback_path=[],
        )

        resp = client.get("/agents/alice/health")

        assert resp.status_code == 200
        check = resp.json()["checks"]["approval_notifications"]
        assert check["healthy"] is False
        assert check["failed"] == 1
        assert check["requests"][0]["notification_state"] == "failed"
        assert "channel_not_found" in check["requests"][0]["last_error"]
        assert resp.json()["recommendation"] == "needs_attention"

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

    def _make_client_with_registry(self):
        """Return (client, registry) both pointing at the same in-api agents.db."""
        from pinky_daemon.agent_registry import AgentRegistry
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        # create_api splits storage: agents live in {db_path}_agents.db
        registry = AgentRegistry(db_path=path.replace(".db", "_agents.db"))
        return TestClient(app), registry

    def test_agent_presence_server_stamped_online(self):
        """Non-heartbeat agent with fresh last_seen_at should be online, not unknown."""
        client, registry = self._make_client_with_registry()
        client.post("/agents", json={"name": "codex-a", "model": "opus"})
        registry.stamp_last_seen("codex-a", ts=time.time())
        resp = client.get("/agents/codex-a/presence")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "online", f"expected online, got {data['status']}"
        assert data["last_seen"] > 0
        assert data["streaming"] is False

    def test_agent_presence_server_stamped_idle(self):
        """Non-heartbeat agent stamped 10min ago should be idle."""
        client, registry = self._make_client_with_registry()
        client.post("/agents", json={"name": "codex-b", "model": "opus"})
        registry.stamp_last_seen("codex-b", ts=time.time() - 600)
        resp = client.get("/agents/codex-b/presence")
        data = resp.json()
        assert data["status"] == "idle", f"expected idle, got {data['status']}"

    def test_agent_presence_server_stamped_offline(self):
        """Non-heartbeat agent stamped 1hr ago should be offline."""
        client, registry = self._make_client_with_registry()
        client.post("/agents", json={"name": "codex-c", "model": "opus"})
        registry.stamp_last_seen("codex-c", ts=time.time() - 3600)
        resp = client.get("/agents/codex-c/presence")
        data = resp.json()
        assert data["status"] == "offline", f"expected offline, got {data['status']}"

    def test_agent_presence_no_stamp_no_heartbeat_is_unknown(self):
        """Preserve existing behavior: no server stamp and no heartbeat → unknown."""
        client = self._make_client()
        client.post("/agents", json={"name": "ghost", "model": "sonnet"})
        resp = client.get("/agents/ghost/presence")
        data = resp.json()
        assert data["status"] == "unknown"

    def test_agent_presence_heartbeat_wins_when_fresher(self):
        """If heartbeat is fresher than server stamp, heartbeat status logic applies."""
        client, registry = self._make_client_with_registry()
        client.post("/agents", json={"name": "cc-agent", "model": "sonnet"})
        # Old server stamp
        registry.stamp_last_seen("cc-agent", ts=time.time() - 3600)
        # Fresh heartbeat — should override server stamp
        registry.record_heartbeat("cc-agent", status="alive")
        resp = client.get("/agents/cc-agent/presence")
        data = resp.json()
        assert data["status"] == "online"

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


@pytest.mark.real_auth
class TestAuthEndpoints:
    """Auth-flow tests — opt out of conftest's auto-cookie injection so
    these exercise the real unauthenticated → authenticated transitions
    that the auth endpoints exist to serve.
    """
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

    def test_default_provider_setting_redacts_key(self):
        client = self._make_client()
        create = client.post("/providers", json={
            "name": "shared",
            "preset": "openrouter",
            "provider_url": "https://openrouter.ai/api",
            "provider_key": "super-secret",
            "provider_model": "anthropic/claude-sonnet-4-5",
        })
        assert create.status_code == 200
        provider_id = create.json()["id"]

        set_resp = client.put("/settings/default-provider", json={"provider_id": provider_id})
        assert set_resp.status_code == 200

        get_resp = client.get("/settings/default-provider")
        assert get_resp.status_code == 200
        payload = get_resp.json()
        assert payload["provider_id"] == provider_id
        assert payload["provider"]["id"] == provider_id
        assert "provider_key" not in payload["provider"]
        assert "provider_url" not in payload["provider"]

    def test_resolver_uses_default_provider_when_agent_unconfigured(self):
        from pinky_daemon.api import resolve_provider_config
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE providers (id TEXT PRIMARY KEY, provider_url TEXT, provider_key TEXT, provider_model TEXT)")
        db.execute(
            "INSERT INTO providers (id, provider_url, provider_key, provider_model) VALUES (?, ?, ?, ?)",
            ("default", "http://localhost:11434", "ollama", "llama3.2"),
        )
        db.commit()
        url, key, model = resolve_provider_config(
            agent_provider_url="",
            agent_provider_key="",
            agent_provider_model="",
            agent_provider_ref="",
            default_provider_ref="default",
            db=db,
        )
        assert (url, key, model) == ("http://localhost:11434", "ollama", "llama3.2")

    def test_resolver_agent_ref_overrides_system_default(self):
        from pinky_daemon.api import resolve_provider_config
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE providers (id TEXT PRIMARY KEY, provider_url TEXT, provider_key TEXT, provider_model TEXT)")
        db.execute(
            "INSERT INTO providers (id, provider_url, provider_key, provider_model) VALUES (?, ?, ?, ?)",
            ("default", "https://openrouter.ai/api", "k1", "anthropic/claude-sonnet-4-5"),
        )
        db.execute(
            "INSERT INTO providers (id, provider_url, provider_key, provider_model) VALUES (?, ?, ?, ?)",
            ("agent", "https://api.deepseek.com/anthropic", "k2", "deepseek-chat"),
        )
        db.commit()
        url, key, model = resolve_provider_config(
            agent_provider_url="",
            agent_provider_key="",
            agent_provider_model="",
            agent_provider_ref="agent",
            default_provider_ref="default",
            db=db,
        )
        assert (url, key, model) == ("https://api.deepseek.com/anthropic", "k2", "deepseek-chat")

    def test_resolver_agent_explicit_fields_override_refs(self):
        from pinky_daemon.api import resolve_provider_config
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE providers (id TEXT PRIMARY KEY, provider_url TEXT, provider_key TEXT, provider_model TEXT)")
        db.execute(
            "INSERT INTO providers (id, provider_url, provider_key, provider_model) VALUES (?, ?, ?, ?)",
            ("agent", "https://openrouter.ai/api", "k1", "anthropic/claude-sonnet-4-5"),
        )
        db.commit()
        url, key, model = resolve_provider_config(
            agent_provider_url="https://api.deepseek.com/anthropic",
            agent_provider_key="",
            agent_provider_model="deepseek-chat",
            agent_provider_ref="agent",
            default_provider_ref="",
            db=db,
        )
        assert (url, key, model) == ("https://api.deepseek.com/anthropic", "", "deepseek-chat")


# ── Google OAuth CSRF state validation (#287) ────────────────────


@pytest.mark.real_auth
class TestGoogleOAuthStateValidation:
    """Regression for #287: the legacy /calendar/google/callback endpoint must
    validate a previously-issued state nonce before exchanging the code.

    Marked ``real_auth`` so the conftest auto-cookie isn't injected — real
    OAuth redirects from Google arrive cross-site without our session
    cookie (SameSite=strict). The tests must mirror that production
    posture; otherwise they'd silently pass on a session-cookie-protected
    route, masking bugs in the unauth path (caught in PR #504 round 2).
    """

    def _make_app(self):
        from pinky_daemon.api import create_api
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)

    def _seed_credentials(self, app):
        """Seed client credentials so the callback can't early-return on them."""
        agents = app.state.agents
        # TokenStore uses these exact setting keys (see pinky_calendar.store).
        agents.set_setting("GOOGLE_CALENDAR_CLIENT_ID", "test-client-id")
        agents.set_setting("GOOGLE_CALENDAR_CLIENT_SECRET", "test-client-secret")

    def test_callback_rejects_missing_state(self):
        app = self._make_app()
        self._seed_credentials(app)
        with TestClient(app) as client:
            resp = client.get(
                "/calendar/google/callback",
                params={"code": "auth-code", "state": ""},
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert "missing state" in resp.text.lower()

    def test_callback_rejects_unknown_state(self):
        app = self._make_app()
        self._seed_credentials(app)
        with TestClient(app) as client:
            resp = client.get(
                "/calendar/google/callback",
                params={"code": "auth-code", "state": "attacker-forged-nonce"},
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert "unknown or replayed" in resp.text.lower()

    def test_callback_rejects_expired_state(self):
        from datetime import datetime, timedelta, timezone
        app = self._make_app()
        self._seed_credentials(app)
        # Manually insert a state key with an issued timestamp in the distant past.
        stale = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
        app.state.agents.set_setting("GOOGLE_OAUTH_STATE_stale-nonce", stale)
        with TestClient(app) as client:
            resp = client.get(
                "/calendar/google/callback",
                params={"code": "auth-code", "state": "stale-nonce"},
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert "expired" in resp.text.lower()
        # Expired state must be purged so it can't be retried.
        assert app.state.agents.get_setting("GOOGLE_OAUTH_STATE_stale-nonce") == ""

    def test_callback_rejects_replayed_state(self):
        """A state nonce must be single-use. After one consume, a second hit
        with the same nonce must be treated as unknown."""
        from datetime import datetime, timezone
        from unittest.mock import patch as _patch

        app = self._make_app()
        self._seed_credentials(app)
        fresh = datetime.now(tz=timezone.utc).isoformat()
        app.state.agents.set_setting("GOOGLE_OAUTH_STATE_one-shot", fresh)

        # First use consumes the nonce. We don't care about the token-exchange
        # outcome here (which will fail because we're not mocking Google), we
        # only need to confirm the state was accepted + deleted.
        fake_tokens = {"access_token": "a", "refresh_token": "r", "expiry": None}
        with _patch("pinky_calendar.oauth.exchange_code", return_value=fake_tokens):
            with TestClient(app) as client:
                first = client.get(
                    "/calendar/google/callback",
                    params={"code": "auth-code", "state": "one-shot"},
                    follow_redirects=False,
                )
                assert first.status_code == 200  # happy path
                assert app.state.agents.get_setting("GOOGLE_OAUTH_STATE_one-shot") == ""
                # Replay with the same nonce must now be rejected.
                replay = client.get(
                    "/calendar/google/callback",
                    params={"code": "auth-code", "state": "one-shot"},
                    follow_redirects=False,
                )
        assert replay.status_code == 400
        assert "unknown or replayed" in replay.text.lower()

    def test_callback_deletes_state_even_on_exchange_failure(self):
        """If exchange_code raises, the state nonce must still have been
        consumed — otherwise an attacker could race a valid flow."""
        from datetime import datetime, timezone
        from unittest.mock import patch as _patch

        app = self._make_app()
        self._seed_credentials(app)
        fresh = datetime.now(tz=timezone.utc).isoformat()
        app.state.agents.set_setting("GOOGLE_OAUTH_STATE_single-use", fresh)

        with _patch(
            "pinky_calendar.oauth.exchange_code",
            side_effect=RuntimeError("invalid code"),
        ):
            with TestClient(app) as client:
                resp = client.get(
                    "/calendar/google/callback",
                    params={"code": "bad-code", "state": "single-use"},
                    follow_redirects=False,
                )
        assert resp.status_code == 400
        assert "oauth error" in resp.text.lower()
        assert app.state.agents.get_setting("GOOGLE_OAUTH_STATE_single-use") == ""

    def test_direct_auth_url_persists_state(self):
        """The direct-auth-url endpoint must persist a single-use state nonce
        that the callback can later validate against."""
        from unittest.mock import patch as _patch

        app = self._make_app()
        self._seed_credentials(app)
        from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
        with _patch(
            "pinky_calendar.oauth.get_auth_url",
            return_value=("https://accounts.google.com/o/oauth2/auth?state=xyz", "xyz"),
        ):
            with TestClient(app) as client:
                # /calendar is auth-gated. direct-auth-url is an owner-initiated
                # action (not Google's cross-site callback), so it requires a
                # session — unlike the callback tests in this real_auth class.
                # Mint one.
                client.cookies.set(
                    SESSION_COOKIE_NAME,
                    create_session_cookie(os.environ["PINKY_SESSION_SECRET"]),
                )
                resp = client.get("/calendar/google/direct-auth-url")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "xyz"
        assert "accounts.google.com" in body["auth_url"]
        # State key must be present and non-empty (timestamp).
        stored = app.state.agents.get_setting("GOOGLE_OAUTH_STATE_xyz")
        assert stored != ""

    def test_direct_auth_url_requires_credentials(self):
        """Without stored client credentials, direct-auth must 400 — there's
        nothing to build a direct-Google auth URL for."""
        from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
        app = self._make_app()
        # Intentionally skip _seed_credentials().
        with TestClient(app) as client:
            # /calendar is auth-gated; this owner endpoint needs a session.
            client.cookies.set(
                SESSION_COOKIE_NAME,
                create_session_cookie(os.environ["PINKY_SESSION_SECRET"]),
            )
            resp = client.get("/calendar/google/direct-auth-url")
        assert resp.status_code == 400

    def test_consume_fails_closed_when_delete_returns_false(self):
        """Regression for Murzik's review: if delete_setting() returns False
        (because a concurrent consumer already deleted the row), we must NOT
        proceed to token exchange. Treat the loser of the race as replayed."""
        from datetime import datetime, timezone
        from unittest.mock import patch as _patch

        app = self._make_app()
        self._seed_credentials(app)
        fresh = datetime.now(tz=timezone.utc).isoformat()
        app.state.agents.set_setting("GOOGLE_OAUTH_STATE_race-nonce", fresh)

        # Simulate the race: get_setting sees the value, but by the time we
        # try to delete it, a concurrent consumer has beaten us to it.
        real_delete = app.state.agents.delete_setting

        def fake_delete(key):
            if key == "GOOGLE_OAUTH_STATE_race-nonce":
                return False  # Another consumer won the race.
            return real_delete(key)

        exchange_called = {"count": 0}

        def fake_exchange(*args, **kwargs):
            exchange_called["count"] += 1
            return {"access_token": "a", "refresh_token": "r", "expiry": None}

        with _patch.object(app.state.agents, "delete_setting", fake_delete), \
             _patch("pinky_calendar.oauth.exchange_code", fake_exchange):
            with TestClient(app) as client:
                resp = client.get(
                    "/calendar/google/callback",
                    params={"code": "auth-code", "state": "race-nonce"},
                    follow_redirects=False,
                )
        assert resp.status_code == 400
        assert "unknown or replayed" in resp.text.lower()
        # Critical: exchange must not have been invoked.
        assert exchange_called["count"] == 0

    def test_consume_fails_closed_when_delete_raises(self):
        """Regression for Murzik's review: if delete_setting() raises (DB
        error, disk full, etc.), fail closed — don't exchange the code."""
        from datetime import datetime, timezone
        from unittest.mock import patch as _patch

        app = self._make_app()
        self._seed_credentials(app)
        fresh = datetime.now(tz=timezone.utc).isoformat()
        app.state.agents.set_setting("GOOGLE_OAUTH_STATE_boom", fresh)

        def raising_delete(key):
            raise sqlite3.OperationalError("database is locked")

        exchange_called = {"count": 0}

        def fake_exchange(*args, **kwargs):
            exchange_called["count"] += 1
            return {"access_token": "a", "refresh_token": "r", "expiry": None}

        with _patch.object(app.state.agents, "delete_setting", raising_delete), \
             _patch("pinky_calendar.oauth.exchange_code", fake_exchange):
            with TestClient(app) as client:
                resp = client.get(
                    "/calendar/google/callback",
                    params={"code": "auth-code", "state": "boom"},
                    follow_redirects=False,
                )
        assert resp.status_code == 400
        assert "could not consume state" in resp.text.lower()
        assert exchange_called["count"] == 0

    def test_callback_rejects_naive_timestamp_state(self):
        """Regression for Murzik's review: datetime.fromisoformat() can return
        a naive datetime for valid ISO strings without a tz suffix, and
        subtracting naive from aware raises TypeError → 500. Reject naive
        records as corrupt so the callback still returns a clean 400."""
        app = self._make_app()
        self._seed_credentials(app)
        # No tz suffix — fromisoformat() will parse this as naive.
        app.state.agents.set_setting(
            "GOOGLE_OAUTH_STATE_naive-nonce", "2026-04-21T10:00:00",
        )
        with TestClient(app) as client:
            resp = client.get(
                "/calendar/google/callback",
                params={"code": "auth-code", "state": "naive-nonce"},
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert "corrupt state" in resp.text.lower()
        # Still purged, even though it was malformed — can't be retried.
        assert app.state.agents.get_setting("GOOGLE_OAUTH_STATE_naive-nonce") == ""


class TestBuildStreamingWakeContextReasonGating:
    """#591 — ``_build_streaming_wake_context`` gates the saved-context
    manifest by wake reason.

    On ``WakeReason.RESUME`` (warm ``claude --continue``) the prior
    conversation is already in context, so the bulk manifest is dropped.
    Only a *fresh-this-cycle* ``wake_action`` survives — gated by
    comparison against the previous ``agent_wake`` event's timestamp.

    On any other reason (CONTEXT_RESTART / AUTO_RESTART / NEW_SESSION /
    IDLE_WAKE) the full manifest is emitted as before.

    Transient context (tasks, channels, dreams, restart manifest)
    is fresh per-wake and continues to fire on RESUME — only the saved
    manifest is gated.
    """

    def _make_app(self, path: str):
        from pinky_daemon.api import create_api
        return create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)

    def _seed_previous_wake(self, app, agent_name: str, when: float) -> None:
        """Insert an ``agent_wake`` activity event with a specific
        created_at so the cycle-bound freshness check has a baseline.
        Uses raw SQL because ``activity.log`` stamps NOW unconditionally.
        """
        with app.state.activity._db:
            app.state.activity._db.execute(
                "INSERT INTO activity_log (agent_name, event_type, title, "
                "description, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (agent_name, "agent_wake", "prev wake", "", "{}", when),
            )

    def _seed_manifest_at(
        self, app, agent_name: str, *, task: str, wake_action: str, when: float
    ) -> None:
        app.state.agents.set_context(
            agent_name,
            task=task,
            wake_action=wake_action,
            metadata={"source": "save_my_context"},
        )
        with app.state.agents._db:
            app.state.agents._db.execute(
                "UPDATE agent_contexts SET updated_at=? WHERE agent_name=?",
                (when, agent_name),
            )

    def test_resume_with_fresh_wake_action_emits_directive_only(self):
        """RESUME + manifest written AFTER previous wake → only
        wake_action renders. Bulk fields stay out of the wake prompt."""
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            prev_wake = time.time() - 3600
            self._seed_previous_wake(app, "dymok", prev_wake)
            self._seed_manifest_at(
                app, "dymok",
                task="Phase 2 of tmux watchdog fix",
                wake_action="Grep daemon log for verdict_wedged_inputs",
                when=prev_wake + 1800,  # 30 min after previous wake — fresh this cycle
            )

            out = app.state._build_streaming_wake_context("dymok", WakeReason.RESUME)
            assert "## ⚡ Wake Action (do this FIRST)" in out
            assert "Grep daemon log for verdict_wedged_inputs" in out
            # Bulk fields must NOT appear.
            assert "## Continuation" not in out
            assert "Phase 2 of tmux watchdog fix" not in out

    def test_resume_with_stale_wake_action_drops_directive(self):
        """RESUME + manifest written BEFORE previous wake → cycle-bound
        gate rejects, no manifest contribution. Pins the #591 repro:
        14h-old directive must not replay on a new wake."""
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            prev_wake = time.time() - 3600
            # Manifest written 14h before the previous wake — stale.
            self._seed_manifest_at(
                app, "dymok",
                task="Old task from prior cycle",
                wake_action="Old directive from prior cycle",
                when=prev_wake - 14 * 3600,
            )
            self._seed_previous_wake(app, "dymok", prev_wake)

            out = app.state._build_streaming_wake_context("dymok", WakeReason.RESUME)
            assert "Old directive from prior cycle" not in out
            assert "Old task from prior cycle" not in out
            # Bulk fields ALSO not emitted on RESUME regardless of staleness.
            assert "## Continuation" not in out

    def test_resume_with_no_wake_action_drops_manifest(self):
        """RESUME + fresh manifest but wake_action empty → no manifest
        contribution. The bulk-only manifest is redundant on warm
        resume."""
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            prev_wake = time.time() - 3600
            self._seed_previous_wake(app, "dymok", prev_wake)
            self._seed_manifest_at(
                app, "dymok",
                task="Working on something",
                wake_action="",  # No directive
                when=prev_wake + 1800,
            )

            out = app.state._build_streaming_wake_context("dymok", WakeReason.RESUME)
            assert "## ⚡ Wake Action" not in out
            assert "Working on something" not in out
            assert "## Continuation" not in out

    def test_resume_with_no_previous_wake_emits_directive(self):
        """RESUME with no prior ``agent_wake`` event → fall through to
        emit (first-ever resume edge case). The manifest must be fresh
        in some absolute sense too, but if no prior wake exists we
        treat the directive as fresh-by-default."""
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            self._seed_manifest_at(
                app, "dymok",
                task="First-ever task",
                wake_action="Do the first-ever thing",
                when=time.time() - 60,
            )

            out = app.state._build_streaming_wake_context("dymok", WakeReason.RESUME)
            assert "Do the first-ever thing" in out
            # Still drops the bulk on RESUME.
            assert "## Continuation" not in out

    def test_context_restart_emits_full_manifest(self):
        """CONTEXT_RESTART = fresh ``claude`` launch (no --continue).
        The bulk manifest is needed because the new session has no
        prior conversation to anchor against. Regression guard."""
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            self._seed_manifest_at(
                app, "dymok",
                task="Phase 2 of tmux watchdog fix",
                wake_action="Grep daemon log",
                when=time.time() - 600,
            )

            out = app.state._build_streaming_wake_context(
                "dymok", WakeReason.CONTEXT_RESTART
            )
            assert "## ⚡ Wake Action (do this FIRST)" in out
            assert "Grep daemon log" in out
            assert "## Continuation" in out
            assert "Phase 2 of tmux watchdog fix" in out

    def test_default_reason_emits_full_manifest(self):
        """Backwards compat: legacy 1-arg callers (``builder(name)``)
        get the default ``WakeReason.NEW_SESSION`` which emits the full
        manifest — same as pre-#591 behavior. Protects external callers
        that haven't been updated to pass a reason."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            self._seed_manifest_at(
                app, "dymok",
                task="Legacy task",
                wake_action="Legacy directive",
                when=time.time() - 600,
            )

            # One-arg call (no reason) — pre-#591 caller shape.
            out = app.state._build_streaming_wake_context("dymok")
            assert "Legacy directive" in out
            assert "## Continuation" in out
            assert "Legacy task" in out

    def test_resume_with_fresh_directive_but_absolute_age_no_warning(self):
        """RESUME + cycle-fresh wake_action that is ALSO >12h old in
        absolute terms (no agent_wake between save and now) → no stale
        warning. The stale-warning is about the BULK manifest's
        absolute age; this branch already validated cycle freshness
        via the tighter previous-wake comparison, and we never emit
        the bulk on RESUME. Firing the warning here would contradict
        the cycle-bound gate that just certified the directive.

        Repro shape (Barsik branch review): agent awake continuously
        with the only prior agent_wake event >12h old, fresh save
        within the current awake period, current RESUME wake.
        """
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            # Previous agent_wake fired 15h ago (e.g., daemon-restart
            # wake at the start of a long awake period).
            prev_wake = time.time() - 15 * 3600
            self._seed_previous_wake(app, "dymok", prev_wake)
            # Manifest saved 13h ago — AFTER the prior wake (cycle-
            # fresh) but >12h absolute (would trip the stale-warning
            # constant if we let it).
            self._seed_manifest_at(
                app, "dymok",
                task="Working through a long shift",
                wake_action="Ping Barsik with status when you wake",
                when=time.time() - 13 * 3600,
            )

            out = app.state._build_streaming_wake_context(
                "dymok", WakeReason.RESUME
            )
            # Directive survives (cycle-fresh).
            assert "Ping Barsik with status when you wake" in out
            # The stale-continuation warning MUST NOT appear — the
            # cycle-bound gate already validated freshness and we
            # didn't emit the bulk to which the warning refers.
            assert "WARNING: Saved continuation context" not in out

    def test_resume_preserves_channel_context(self):
        """Transient context (channel preamble, tasks, dreams,
        restart manifest) is fresh per wake and continues to fire on
        RESUME — only the saved-context manifest is gated by reason.
        The model wouldn't otherwise know about active channels.
        """
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            # No saved manifest. Broker still emits default channel
            # context — the transient layer must not be gated by reason.

            out = app.state._build_streaming_wake_context("dymok", WakeReason.RESUME)
            assert "## Active Channels" in out
            assert "## Messaging Tools" in out
            # Manifest sections must NOT appear.
            assert "## ⚡ Wake Action" not in out
            assert "## Continuation" not in out

    def test_wake_context_never_replays_agent_audit_messages(self):
        """Audit history is never treated as a wake-time delivery inbox."""
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            app.state.agents.register("barsik", model="sonnet")
            app.state.comms.send(
                from_session="barsik",
                to_session="dymok",
                content="Phase 2 plan looks right — ship it",
            )
            assert app.state.comms.get_inbox("dymok", unread_only=True) == []

            out_preview = app.state._build_streaming_wake_context(
                "dymok", WakeReason.NEW_SESSION, commit=False
            )
            assert "Phase 2 plan looks right" not in out_preview

            out_delivered = app.state._build_streaming_wake_context(
                "dymok", WakeReason.NEW_SESSION, commit=True
            )
            assert "Phase 2 plan looks right" not in out_delivered
            assert app.state.comms.unread_count("dymok") == 0

    def test_commit_false_does_not_consume_restart_manifest(self):
        """#591 P1#1 (Murzik): the eager pre-build must NOT delete
        the agent's entry from restart_manifest.json — the delivered
        rebuild would then find nothing and the wake prompt would
        miss the restart manifest content.
        """
        import json
        from datetime import datetime
        from datetime import timezone as _utc
        from pathlib import Path

        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")
            manifest_path = Path(db_path).parent / "restart_manifest.json"
            manifest = {
                "restart_time": datetime.now(_utc.utc).isoformat(),
                "agents": {
                    "dymok": {
                        "in_progress": "Phase 2 of tmux watchdog",
                        "activity_log": ["build", "test"],
                        "pending_responses": 0,
                    },
                },
            }
            manifest_path.write_text(json.dumps(manifest))

            # Eager pre-build (commit=False): renders content, no delete.
            out_preview = app.state._build_streaming_wake_context(
                "dymok", WakeReason.NEW_SESSION, commit=False
            )
            assert "Phase 2 of tmux watchdog" in out_preview
            assert manifest_path.exists()  # NOT deleted
            persisted = json.loads(manifest_path.read_text())
            assert "dymok" in persisted["agents"]  # entry still present

            # Delivered build (commit=True): deletes the entry.
            out_delivered = app.state._build_streaming_wake_context(
                "dymok", WakeReason.NEW_SESSION, commit=True
            )
            assert "Phase 2 of tmux watchdog" in out_delivered
            # Last entry was for dymok — manifest file should now be gone.
            assert not manifest_path.exists()

    def test_central_wake_log_advances_cycle_gate_on_warm_wakes(self):
        """#591 P1#2 (Murzik): the cycle-bound gate's source-of-truth is
        the most-recent ``agent_wake`` event. Before the centralized
        callback, warm-wake paths (broker auto-wake, reconnect) didn't
        log ``agent_wake``, so a directive set once would replay on
        every subsequent warm wake forever. With the central callback,
        every successful delivery advances the boundary.

        Concrete replay shape:
          T0 cold-start log → save wake_action T1 → warm-wake T2 emits
          directive + LOGS T2 → T3 warm-wake reads prev_wake=T2 →
          manifest.updated_at(T1) < T2 → directive does NOT replay.
        """
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")

            # T0: seed prior cold-start wake event.
            t0 = time.time() - 7200
            self._seed_previous_wake(app, "dymok", t0)
            # T1: save_my_context — wake_action set just before sleep.
            t1 = t0 + 3600
            self._seed_manifest_at(
                app, "dymok",
                task="Working on tmux watchdog",
                wake_action="Grep daemon log for verdict_wedged_inputs",
                when=t1,
            )

            # T2: warm wake fires. Directive should emit (manifest.t1 > t0).
            out_t2 = app.state._build_streaming_wake_context(
                "dymok", WakeReason.RESUME
            )
            assert "Grep daemon log for verdict_wedged_inputs" in out_t2

            # Central callback advances the boundary to T2.
            app.state._log_agent_wake_event("dymok", WakeReason.RESUME)

            # T3: another warm wake. prev_wake is now T2, AFTER manifest.
            # The cycle gate must REJECT — directive must not replay.
            out_t3 = app.state._build_streaming_wake_context(
                "dymok", WakeReason.RESUME
            )
            assert "Grep daemon log for verdict_wedged_inputs" not in out_t3

    def test_central_wake_triggers_persisted_scheduler_catch_up(self):
        """#949: every proven session boot wakes the durable scheduler outbox."""
        from pinky_daemon.wake_prompt import WakeReason

        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            replayed: list[str] = []
            app.state.scheduler.replay_pending_for_agent = replayed.append

            app.state._log_agent_wake_event("dymok", WakeReason.CONTEXT_RESTART)

            assert replayed == ["dymok"]

    @pytest.mark.asyncio
    async def test_scheduler_undelivered_alert_uses_owner_destination(self):
        """#949 alerting uses the durable #863 owner-notify configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "test.db"))
            app.state.agents.set_owner_notification_destinations([
                {
                    "platform": "telegram",
                    "account_id": "acct-1",
                    "conversation_id": "owner-dm",
                    "principal_id": "owner-user",
                }
            ])
            send = AsyncMock(return_value={"sent": True})
            app.state.broker._send_callback = send

            delivered = await app.state.scheduler._owner_notify_callback(
                "dymok", "FIRED BUT UNDELIVERED test"
            )

            assert delivered is True
            send.assert_awaited_once_with(
                "dymok",
                "telegram",
                "owner-dm",
                "FIRED BUT UNDELIVERED test",
                account_id="acct-1",
            )

    def test_central_wake_log_failure_does_not_advance_gate(self):
        """#591 P1#2 (Barsik refinement): the callback fires ONLY on
        successful delivery. If the wake prompt's paste/query fails,
        the boundary must NOT advance — otherwise a wedged delivery
        would eat the directive permanently (next attempt finds the
        boundary already past the manifest and skips emission).

        We can't directly simulate a paste failure in this unit test,
        but we CAN verify the invariant: without firing the callback,
        the manifest stays fresh-this-cycle on the retry.
        """
        from pinky_daemon.wake_prompt import WakeReason
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            app = self._make_app(db_path)
            app.state.agents.register("dymok", model="sonnet")

            t0 = time.time() - 7200
            self._seed_previous_wake(app, "dymok", t0)
            t1 = t0 + 3600
            self._seed_manifest_at(
                app, "dymok",
                task="Working on tmux watchdog",
                wake_action="Grep daemon log for verdict_wedged_inputs",
                when=t1,
            )

            # First warm-wake attempt emits the directive.
            out_first = app.state._build_streaming_wake_context(
                "dymok", WakeReason.RESUME
            )
            assert "Grep daemon log for verdict_wedged_inputs" in out_first

            # Delivery fails: callback NOT fired. Boundary stays at T0.

            # Retry attempt MUST still emit — manifest is still fresh
            # against T0. (Contrast with the prior test where the
            # callback fired and advanced the boundary.)
            out_retry = app.state._build_streaming_wake_context(
                "dymok", WakeReason.RESUME
            )
            assert "Grep daemon log for verdict_wedged_inputs" in out_retry


class TestWakeContextMcpProbeDirective:
    """#663 phase-2 — the launch-time mcp_probe directive injection (2a)."""

    def _make_app(self, path: str):
        from pinky_daemon.api import create_api
        return create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)

    def _extract_nonce(self, text: str) -> str:
        import re
        m = re.search(r"nonce='([0-9a-f]+)'", text)
        return m.group(1) if m else ""

    def test_commit_true_injects_directive_and_records_request(self):
        from pinky_daemon.shared_mcp import bump_gateway_epoch, get_probe_request
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "t.db"))
            app.state.agents.register(
                "dymok", model="sonnet", watchdog_config={"mcp_recover": True}
            )
            bump_gateway_epoch()  # establish a live generation
            out = app.state._build_streaming_wake_context("dymok", commit=True)
            assert "MCP BIND CHECK" in out
            assert out.lstrip().startswith("⚠️ MCP BIND CHECK")  # at the very top
            nonce = self._extract_nonce(out)
            assert nonce
            req = get_probe_request("dymok")
            assert req and req["current"] is True and req["fulfilled"] is False
            assert req["nonce"] == nonce  # delivered directive nonce == registered

    def test_commit_false_preview_injects_nothing_and_records_no_request(self):
        from pinky_daemon.shared_mcp import bump_gateway_epoch, get_probe_request
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "t.db"))
            app.state.agents.register(
                "dymok", model="sonnet", watchdog_config={"mcp_recover": True}
            )
            bump_gateway_epoch()
            out = app.state._build_streaming_wake_context("dymok", commit=False)
            assert "MCP BIND CHECK" not in out  # no throwaway-nonce directive
            assert get_probe_request("dymok") == {}  # ledger untouched

    def test_no_directive_when_mcp_recover_off(self):
        from pinky_daemon.shared_mcp import bump_gateway_epoch, get_probe_request
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "t.db"))
            app.state.agents.register("dymok", model="sonnet")  # default: no mcp_recover
            bump_gateway_epoch()
            out = app.state._build_streaming_wake_context("dymok", commit=True)
            assert "MCP BIND CHECK" not in out
            assert get_probe_request("dymok") == {}

    def test_record_failure_falls_back_to_normal_text_no_request(self, monkeypatch):
        # A failure inside the injection must never corrupt the wake text or
        # block the launch — fall back to the normal text with no directive.
        from pinky_daemon.shared_mcp import bump_gateway_epoch, get_probe_request
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(os.path.join(tmpdir, "t.db"))
            app.state.agents.register(
                "dymok", model="sonnet", watchdog_config={"mcp_recover": True}
            )
            bump_gateway_epoch()

            def boom(*_a, **_k):
                raise RuntimeError("ledger down")

            monkeypatch.setattr("pinky_daemon.api.record_probe_request", boom)
            out = app.state._build_streaming_wake_context("dymok", commit=True)
            assert "MCP BIND CHECK" not in out  # record failed before prepend
            assert get_probe_request("dymok") == {}  # nothing recorded


# ── Cross-agent memory authorizer (#145) ─────────────────────


class TestAgentIsDreamer:
    """The cross-agent memory boundary is role-only — a single privileged
    identity, not a class. Group membership must NOT confer it (#624 review)."""

    def test_role_dreamer_authorized(self):
        from pinky_daemon.api import _agent_is_dreamer

        assert _agent_is_dreamer(SimpleNamespace(role="dreamer", groups=[])) is True

    def test_other_roles_denied(self):
        from pinky_daemon.api import _agent_is_dreamer

        for role in ("", "sidekick", "lead", "worker", "specialist", "Dreamer"):
            assert _agent_is_dreamer(SimpleNamespace(role=role, groups=[])) is False

    def test_group_membership_does_not_authorize(self):
        from pinky_daemon.api import _agent_is_dreamer

        # The tightening: being in a "dreamer" group is NOT enough.
        agent = SimpleNamespace(role="worker", groups=["dreamer", "ops"])
        assert _agent_is_dreamer(agent) is False

    def test_none_agent_default_deny(self):
        from pinky_daemon.api import _agent_is_dreamer

        assert _agent_is_dreamer(None) is False

    def test_missing_role_attr_default_deny(self):
        from pinky_daemon.api import _agent_is_dreamer

        assert _agent_is_dreamer(SimpleNamespace(groups=["dreamer"])) is False
