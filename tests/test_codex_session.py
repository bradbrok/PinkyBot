"""Tests for CodexSession — Codex CLI agent provider."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from pinky_daemon.codex_session import CodexSession, CodexTurnResult
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.streaming_session import StreamingSessionConfig


class TestCodexTurnResult:
    def test_defaults(self):
        r = CodexTurnResult()
        assert r.thread_id == ""
        assert r.text_parts == []
        assert r.tool_uses == []
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert not r.failed


class TestCodexSessionInterface:
    """Verify CodexSession exposes the same public interface as StreamingSession."""

    def _make_session(self, **overrides):
        config = StreamingSessionConfig(
            agent_name="test-agent",
            label="main",
            model="",
            working_dir="/tmp",
            provider_url="codex_cli",
            provider_key="test-key",
            **overrides,
        )
        return CodexSession(config)

    def test_properties(self):
        from pinky_daemon.transport_state import SessionState
        s = self._make_session()
        assert s.agent_name == "test-agent"
        assert s.id == "test-agent-main"
        # Fresh session never tried to connect → UNINITIALIZED, not DEAD.
        # Per @pushok PR #492 Nit 1: distinguishes "never tried" from
        # "tried and disconnected".
        assert s.state == SessionState.UNINITIALIZED
        assert isinstance(s.stats, dict)
        assert s.stats["connected"] is False
        assert s.stats["idle_sleeping"] is False
        assert s.stats["account"]["apiProvider"] == "codex_cli"

    def test_stats_shape(self):
        s = self._make_session()
        stats = s.stats
        for key in ("turns", "messages_sent", "errors", "connected",
                     "current_activity", "activity_log", "cost_usd", "account"):
            assert key in stats, f"missing key: {key}"

    def test_resume_handle_defaults_empty(self):
        s = self._make_session()
        assert s.resume_handle == ""
        assert s.codex_session_id == ""

    def test_context_info_estimates_from_store_and_internal_prompts(self):
        tmpdir = tempfile.mkdtemp()
        store = ConversationStore(os.path.join(tmpdir, "conversations.db"))
        store.append("test-agent-main", "user", "hello " * 80)
        store.append("test-agent-main", "assistant", "world " * 40)

        config = StreamingSessionConfig(
            agent_name="test-agent",
            label="main",
            model="",
            working_dir="/tmp",
            provider_url="codex_cli",
            provider_key="test-key",
        )
        s = CodexSession(config, conversation_store=store)
        s._record_internal_context_text("internal wake prompt " * 30)

        info = s.get_context_info()

        assert info["total_tokens"] > 0
        assert info["max_tokens"] == 200_000
        assert info["percentage"] > 0


class TestCodexJSONLParsing:
    """Test JSONL event parsing via CodexSession._handle_event."""

    async def _parse_events(self, events: list[dict]) -> CodexTurnResult:
        """Use the real _handle_event method for parsing."""
        config = StreamingSessionConfig(
            agent_name="test", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config)
        result = CodexTurnResult()
        for event in events:
            await session._handle_event(event, result)
        return result

    @pytest.mark.asyncio
    async def test_simple_text_response(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "0", "type": "agent_message", "text": "hello"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
        ]
        r = await self._parse_events(events)
        assert r.thread_id == "abc-123"
        assert r.text_parts == ["hello"]
        assert r.input_tokens == 100
        assert r.output_tokens == 10
        assert not r.failed

    @pytest.mark.asyncio
    async def test_command_execution(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {
                "id": "0", "type": "command_execution",
                "command": "ls -la", "exit_code": 0, "aggregated_output": "total 8\n",
            }},
            {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 20}},
        ]
        r = await self._parse_events(events)
        assert len(r.tool_uses) == 1
        assert r.tool_uses[0]["tool"] == "Bash"
        assert r.tool_uses[0]["input"]["command"] == "ls -la"
        assert r.tool_uses[0]["exit_code"] == 0
        assert r.text_parts == ["done"]

    @pytest.mark.asyncio
    async def test_turn_failed(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "rate limited"}},
        ]
        r = await self._parse_events(events)
        assert r.failed
        assert "rate limited" in r.errors[0]

    @pytest.mark.asyncio
    async def test_multiple_text_parts(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "0", "type": "agent_message", "text": "part 1"}},
            {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "part 2"}},
            {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 20}},
        ]
        r = await self._parse_events(events)
        assert r.text_parts == ["part 1", "part 2"]

    @pytest.mark.asyncio
    async def test_error_item(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "item.completed", "item": {"id": "0", "type": "error", "message": "bad model"}},
            {"type": "turn.completed", "usage": {}},
        ]
        r = await self._parse_events(events)
        assert "bad model" in r.errors

    @pytest.mark.asyncio
    async def test_error_event_stamps_last_seen(self):
        """Transport/runtime errors (event_type=error) should stamp last_seen —
        it's a real liveness signal even when no turn.failed fires."""
        stamps: list[str] = []

        class _MockRegistry:
            def stamp_last_seen(self, name: str, ts: float | None = None) -> None:
                stamps.append(name)

        config = StreamingSessionConfig(
            agent_name="test-agent", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config, registry=_MockRegistry())
        result = CodexTurnResult()
        await session._handle_event(
            {"type": "error", "message": "transport closed"}, result,
        )
        assert stamps == ["test-agent"]
        assert "transport closed" in result.errors[0]

    @pytest.mark.asyncio
    async def test_turn_completed_stamps_last_seen(self):
        """Regression guard: turn.completed continues to stamp."""
        stamps: list[str] = []

        class _MockRegistry:
            def stamp_last_seen(self, name: str, ts: float | None = None) -> None:
                stamps.append(name)

        config = StreamingSessionConfig(
            agent_name="test-agent", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config, registry=_MockRegistry())
        result = CodexTurnResult()
        await session._handle_event(
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
            result,
        )
        assert stamps == ["test-agent"]

    @pytest.mark.asyncio
    async def test_turn_failed_stamps_last_seen(self):
        """Regression guard: turn.failed continues to stamp."""
        stamps: list[str] = []

        class _MockRegistry:
            def stamp_last_seen(self, name: str, ts: float | None = None) -> None:
                stamps.append(name)

        config = StreamingSessionConfig(
            agent_name="test-agent", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config, registry=_MockRegistry())
        result = CodexTurnResult()
        await session._handle_event(
            {"type": "turn.failed", "error": {"message": "rate limited"}}, result,
        )
        assert stamps == ["test-agent"]


class TestCodexSessionSendDrop:
    """Verify send() drops messages when not connected."""

    @pytest.mark.asyncio
    async def test_send_drops_when_disconnected(self):
        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        # Should not raise — just log and return
        await s.send("hello", platform="telegram", chat_id="123")
        assert s._stats["messages_sent"] == 0  # Not connected, message dropped


class TestCodexSessionSendSignature:
    """The broker calls .send() with `agent_hint=...` for both StreamingSession
    and CodexSession (broker.py:804-810). CodexSession.send must accept the
    kwarg or every inbound message to a Codex agent crashes with TypeError —
    the regression that masked the #351 fix on production.
    """

    def _make(self, **overrides):
        kwargs = {
            "agent_name": "test",
            "working_dir": "/tmp",
            "provider_url": "codex_cli",
        }
        kwargs.update(overrides)
        return CodexSession(StreamingSessionConfig(**kwargs))

    @pytest.mark.asyncio
    async def test_send_accepts_agent_hint_kwarg(self):
        """Regression: broker passes agent_hint; signature must accept it."""
        s = self._make()
        # Even disconnected — the signature mismatch raised before reaching
        # the connected check, so this would TypeError pre-fix.
        await s.send(
            "hello",
            platform="telegram",
            chat_id="123",
            message_id="msg-1",
            agent_hint="\n💬 reply hint",
        )
        # Disconnected → message dropped, but call must not raise.
        assert s._stats["messages_sent"] == 0

    @pytest.mark.asyncio
    async def test_send_appends_agent_hint_to_queued_prompt(self):
        """Hint should be appended to the queued prompt (matches Streaming-
        Session.send behavior) but NOT stored in the conversation log."""
        s = self._make()
        s._connected = True  # bypass the dropped-when-disconnected branch

        await s.send(
            "actual user text",
            platform="telegram",
            chat_id="123",
            agent_hint="\n💬 routing hint",
        )

        # Queued prompt has the hint appended
        queued = await s._message_queue.get()
        queued_prompt = queued[0]
        assert queued_prompt == "actual user text\n💬 routing hint"

    @pytest.mark.asyncio
    async def test_send_without_agent_hint_unchanged(self):
        """No-hint path is the previous behavior — prompt queued verbatim."""
        s = self._make()
        s._connected = True

        await s.send("plain prompt", platform="telegram", chat_id="123")

        queued = await s._message_queue.get()
        assert queued[0] == "plain prompt"


class TestCodexSessionDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        from pinky_daemon.transport_state import SessionState
        s = CodexSession(config)
        # Should be safe to call multiple times
        await s.disconnect()
        await s.disconnect()
        assert s.state != SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_alone_does_not_set_idle_sleeping(self):
        from pinky_daemon.transport_state import SessionState
        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)

        await s.disconnect()

        assert s.state != SessionState.CONNECTED
        assert s.state != SessionState.IDLE_SLEEPING

    @pytest.mark.asyncio
    async def test_idle_sleep_sets_idle_sleeping(self):
        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        s._connected = True

        async def fake_exec(prompt: str) -> CodexTurnResult:
            return CodexTurnResult()

        s._exec_codex = fake_exec  # type: ignore[assignment]

        slept = await s.idle_sleep()

        from pinky_daemon.transport_state import SessionState
        assert slept is True
        assert s.state == SessionState.IDLE_SLEEPING
        assert s.stats["idle_sleeping"] is True

    @pytest.mark.asyncio
    async def test_idle_sleep_does_not_flicker_through_dead(self):
        """Regression for @pushok PR #492 Nit 2.

        Pre-fix idle_sleep() called disconnect() BEFORE setting
        _idle_sleeping=True, so the derived state property reported DEAD
        for the entire teardown window between disconnect() landing
        _connected=False and the next line setting _idle_sleeping=True.
        A concurrent heartbeat-watchdog tick observing state during that
        window would call _heartbeat_resurrect on a session about to be
        IDLE_SLEEPING — same bug class as PR3 Bug 1 on StreamingSession.

        Post-fix _idle_sleeping=True is set BEFORE disconnect() so the
        derived state never visits DEAD. We pin the contract by
        instrumenting disconnect() to capture state at entry — must
        already be IDLE_SLEEPING.
        """
        from pinky_daemon.transport_state import SessionState

        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        s._connected = True
        s._connect_attempted = True  # mirror post-connect()

        async def fake_exec(prompt: str) -> CodexTurnResult:
            return CodexTurnResult()

        s._exec_codex = fake_exec  # type: ignore[assignment]

        states_at_disconnect_entry: list = []

        original_disconnect = s.disconnect

        async def instrumented_disconnect():
            states_at_disconnect_entry.append(s.state)
            await original_disconnect()

        s.disconnect = instrumented_disconnect  # type: ignore[method-assign]

        await s.idle_sleep()

        # The load-bearing assertion: when disconnect() is invoked from
        # idle_sleep(), the macro state must already be IDLE_SLEEPING.
        # Pre-fix this would be CONNECTED (so disconnect()'s side effect
        # would land us in DEAD until _idle_sleeping=True was set after).
        assert states_at_disconnect_entry == [SessionState.IDLE_SLEEPING], (
            f"idle_sleep() must declare IDLE_SLEEPING intent before "
            f"calling disconnect(); observed states at disconnect entry: "
            f"{states_at_disconnect_entry}. Pre-fix this would be "
            f"[CONNECTED], producing a DEAD-flicker window."
        )
        assert s.state == SessionState.IDLE_SLEEPING

    @pytest.mark.asyncio
    async def test_connect_clears_idle_sleeping(self):
        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        s._idle_sleeping = True

        async def fake_worker() -> None:
            await asyncio.sleep(60)

        s._message_worker = fake_worker  # type: ignore[assignment]

        await s.connect()

        from pinky_daemon.transport_state import SessionState
        assert s.state == SessionState.CONNECTED
        await s.disconnect()

    @pytest.mark.asyncio
    async def test_attempt_reconnect_uses_connect_and_preserves_codex_session_id(self):
        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        s.codex_session_id = "thread-123"
        s.resume_handle = "thread-123"
        s._RECONNECT_BACKOFF = (0,)
        calls = []

        async def fake_disconnect() -> None:
            calls.append("disconnect")
            s._connected = False

        async def fake_connect() -> None:
            calls.append("connect")
            s._connected = True
            s._idle_sleeping = False

        s.disconnect = fake_disconnect  # type: ignore[method-assign]
        s.connect = fake_connect  # type: ignore[method-assign]

        await s.attempt_reconnect()

        from pinky_daemon.transport_state import SessionState
        assert calls == ["disconnect", "connect"]
        assert s.state == SessionState.CONNECTED
        assert s.codex_session_id == "thread-123"
        assert s.resume_handle == "thread-123"
        assert s.stats["reconnects"] == 1


class TestCodexCommandConstruction:
    """Pin down `_build_codex_cmd()` against #351 regression: `--sandbox=...`
    is not accepted by `codex exec resume`, so the resume path used to fail
    silently with `error: unexpected argument '--sandbox' found`. The fix
    swaps to `--dangerously-bypass-approvals-and-sandbox`, which is accepted
    on BOTH `codex exec` and `codex exec resume` and bypasses both gates.
    """

    def _make(self, **overrides):
        kwargs = {
            "agent_name": "test-agent",
            "label": "main",
            "working_dir": "/tmp",
            "provider_url": "codex_cli",
        }
        kwargs.update(overrides)
        return CodexSession(StreamingSessionConfig(**kwargs))

    def test_fresh_session_uses_yolo_flag_not_sandbox(self):
        """Fresh session: command must use the bypass flag, not --sandbox."""
        s = self._make()
        cmd = s._build_codex_cmd()

        assert cmd[:2] == ["codex", "exec"]
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        # The old flag must be gone — it's the very thing that broke resume
        assert not any(
            arg.startswith("--sandbox") or arg == "--sandbox" for arg in cmd
        ), f"--sandbox flag must not appear in cmd: {cmd}"
        assert "--full-auto" not in cmd, "must not combine with --full-auto"
        assert cmd[-1] == "-", "prompt must be passed via stdin"

    def test_resume_session_uses_yolo_flag_not_sandbox(self):
        """Resume session: this is the path that #351 broke. Same flags must
        apply, and the resume subcommand must come right after `exec`."""
        s = self._make()
        s.codex_session_id = "019de4d8-609a-7000-8000-000000000000"

        cmd = s._build_codex_cmd()

        # Subcommand layout: codex exec resume <id> ...
        assert cmd[:3] == ["codex", "exec", "resume"]
        assert cmd[3] == "019de4d8-609a-7000-8000-000000000000"
        # Bypass flag works on resume; --sandbox does NOT (the bug)
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert not any(
            arg.startswith("--sandbox") or arg == "--sandbox" for arg in cmd
        ), f"--sandbox is rejected by `codex exec resume`: {cmd}"
        # -C (working dir) is only valid for new sessions, not resume
        assert "-C" not in cmd, "-C must not be passed on resume"

    def test_resume_includes_mcp_server_config(self):
        """MCP servers must be injected on resume too — that's the whole
        point of why bypass-on-resume matters (otherwise MCP tool calls die)."""
        s = self._make()
        s.codex_session_id = "session-id"
        s._mcp_servers = {
            "pinky-self": {
                "url": "http://127.0.0.1:8890/mcp",
                "headers": {"X-Agent-Name": "test-agent"},
            }
        }

        cmd = s._build_codex_cmd()

        # MCP url + header overrides should be present
        joined = " ".join(cmd)
        assert "mcp_servers.pinky-self.url=" in joined
        assert "mcp_servers.pinky-self.http_headers.X-Agent-Name=" in joined

    def test_fresh_session_includes_working_dir(self):
        """Fresh session passes -C; the resume path skips it."""
        s = self._make(working_dir="/some/cwd")
        cmd = s._build_codex_cmd()
        assert "-C" in cmd
        assert cmd[cmd.index("-C") + 1] == "/some/cwd"
