"""Tests for CodexSession — Codex CLI agent provider."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

from pinky_daemon.codex_session import CodexSession, CodexTurnResult
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.transport_state import SessionState, Trigger


async def _to_connected(s: CodexSession) -> None:
    """Drive a test session's state machine straight to CONNECTED — without
    spawning the worker or running codex (#206; replaces the old inert
    ``s._connected = True``)."""
    sm = s._state_machine
    res = await sm.request_transition(SessionState.BOOTING, Trigger.BOOT)
    await sm.transition_complete(
        res.owner_token, SessionState.CONNECTED, trigger=Trigger.BOOT_COMPLETE
    )


async def _to_idle(s: CodexSession) -> None:
    """Drive a test session to IDLE_SLEEPING."""
    await _to_connected(s)
    sm = s._state_machine
    res = await sm.request_transition(SessionState.IDLE_SLEEPING, Trigger.USER_AGENT)
    await sm.transition_complete(
        res.owner_token, SessionState.IDLE_SLEEPING, trigger=Trigger.USER_AGENT
    )


async def _to_dead(s: CodexSession) -> None:
    """Drive a test session to DEAD (via CONNECTED if fresh)."""
    if s.state != SessionState.CONNECTED:
        await _to_connected(s)
    sm = s._state_machine
    res = await sm.request_transition(SessionState.DEAD, Trigger.INTERNAL)
    if res.owner_token is not None:
        await sm.transition_complete(
            res.owner_token, SessionState.DEAD, trigger=Trigger.INTERNAL
        )


async def _noop(*_a, **_k) -> None:
    """Async no-op — patch over _enqueue_wake / side effects in state tests."""
    return None


class TestCodexTurnResult:
    def test_defaults(self):
        r = CodexTurnResult()
        assert r.thread_id == ""
        assert r.text_parts == []
        assert r.tool_uses == []
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert not r.failed

    def test_uncached_input_tokens_disjoint(self):
        # #206: Codex reports input_tokens INCLUSIVE of the cached prefix.
        # The billable/disjoint value (Anthropic convention) is input - cached,
        # so the analytics cost math doesn't double-bill the cached span.
        # Real Murzik sample: input=78121, cached=77696 -> uncached=425.
        r = CodexTurnResult(input_tokens=78121, cached_input_tokens=77696)
        assert r.uncached_input_tokens == 425
        # No caching: uncached == input.
        assert CodexTurnResult(input_tokens=100).uncached_input_tokens == 100
        # Anomalous (cached > input) clamps at 0, never negative.
        assert (
            CodexTurnResult(input_tokens=10, cached_input_tokens=50)
            .uncached_input_tokens
            == 0
        )


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
        await _to_connected(s)  # state-machine CONNECTED (bypass the drop branch)

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
        await _to_connected(s)

        await s.send("plain prompt", platform="telegram", chat_id="123")

        queued = await s._message_queue.get()
        assert queued[0] == "plain prompt"

    @pytest.mark.asyncio
    async def test_scheduler_receipt_waits_until_worker_starts_exact_turn(self):
        """#931: a queued scheduler prompt is not delivered at enqueue time."""
        s = self._make()
        await _to_connected(s)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls: list[str] = []

        async def fake_exec(prompt: str) -> CodexTurnResult:
            calls.append(prompt)
            if prompt == "first":
                first_started.set()
                await release_first.wait()
            return CodexTurnResult()

        s._exec_codex = fake_exec  # type: ignore[assignment]
        await s.send("first")
        worker = asyncio.create_task(s._message_worker())
        await asyncio.wait_for(first_started.wait(), timeout=1)

        second_receipt = await s.send_scheduler_prompt("second")
        await asyncio.sleep(0.02)
        assert not second_receipt.done()

        release_first.set()
        assert await asyncio.wait_for(second_receipt, timeout=1) is True
        assert calls == ["first", "second"]

        await _to_dead(s)
        s._message_queue.put_nowait(("noop", "", "", ""))
        await asyncio.wait_for(worker, timeout=1)


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
        await _to_connected(s)

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
        await _to_connected(s)

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
        await _to_idle(s)

        async def fake_worker() -> None:
            await asyncio.sleep(60)

        s._message_worker = fake_worker  # type: ignore[assignment]

        await s.connect()  # warm-wake: IDLE_SLEEPING → RECONNECTING → CONNECTED

        from pinky_daemon.transport_state import SessionState
        assert s.state == SessionState.CONNECTED
        await s.disconnect()

    @pytest.mark.asyncio
    async def test_attempt_reconnect_preserves_codex_session_id(self):
        # #206: attempt_reconnect now owns ONE RECONNECTING transition and
        # retries _bring_up_substrate under it (no nested connect()). It resumes
        # from DEAD, preserves the codex_session_id (only force_restart clears
        # it), and completes to CONNECTED.
        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        s.codex_session_id = "thread-123"
        s.resume_handle = "thread-123"
        s._RECONNECT_BACKOFF = (0,)
        await _to_dead(s)  # broker/heartbeat resurrects from DEAD

        calls = []

        async def fake_disconnect() -> None:
            calls.append("disconnect")

        async def fake_bring_up() -> None:
            calls.append("bring_up")

        async def fake_enqueue() -> None:
            calls.append("wake")

        s.disconnect = fake_disconnect  # type: ignore[method-assign]
        s._bring_up_substrate = fake_bring_up  # type: ignore[method-assign]
        s._start_worker = lambda: calls.append("worker")  # type: ignore[method-assign]
        s._enqueue_wake = fake_enqueue  # type: ignore[method-assign]
        s._analytics_session_started = lambda: None  # type: ignore[method-assign]

        await s.attempt_reconnect()

        assert s.state == SessionState.CONNECTED
        assert calls == ["disconnect", "bring_up", "worker", "wake"]
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


# ──────────────────────────────────────────────────────────────────────────
# Wedge resilience — Tier 1 of the Codex integration spec fix-up.
# Covers: reasoning_output_tokens (codex-cli 0.125+), worker-done watchdog,
# and the is_healthy() diagnostic probe.
# ──────────────────────────────────────────────────────────────────────────


class TestCodexReasoningOutputTokens:
    """codex-cli 0.125+ added reasoning_output_tokens to turn.completed.usage."""

    @pytest.mark.asyncio
    async def test_parses_reasoning_output_tokens(self):
        """New field flows into CodexTurnResult + analytics + stream event."""
        config = StreamingSessionConfig(
            agent_name="test", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config)
        result = CodexTurnResult()
        await session._handle_event(
            {"type": "turn.completed", "usage": {
                "input_tokens": 100, "output_tokens": 20,
                "cached_input_tokens": 50, "reasoning_output_tokens": 4096,
            }},
            result,
        )
        assert result.reasoning_output_tokens == 4096
        # Backward-compat: existing fields still populated.
        assert result.input_tokens == 100
        assert result.output_tokens == 20
        assert result.cached_input_tokens == 50

    @pytest.mark.asyncio
    async def test_absent_field_defaults_to_zero(self):
        """Older codex versions don't emit this field — must not crash."""
        config = StreamingSessionConfig(
            agent_name="test", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config)
        result = CodexTurnResult()
        await session._handle_event(
            {"type": "turn.completed", "usage": {
                "input_tokens": 100, "output_tokens": 20,
            }},
            result,
        )
        assert result.reasoning_output_tokens == 0

    @pytest.mark.asyncio
    async def test_reasoning_gt_output_flagged_anomaly(self):
        """Guard (#206): reasoning is a subset of output, so reasoning > output
        is a usage-parse anomaly. It must surface as a queryable observability
        flag in the turn_completed metadata — not silently trusted."""
        config = StreamingSessionConfig(
            agent_name="test", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config)
        captured: list = []
        session._analytics_log_activity = (
            lambda event_type, *, metadata=None: captured.append((event_type, metadata))
        )
        result = CodexTurnResult()
        await session._handle_event(
            {"type": "turn.completed", "usage": {
                "input_tokens": 100, "output_tokens": 20,
                "cached_input_tokens": 50, "reasoning_output_tokens": 4096,
            }},
            result,
        )
        meta = next(m for e, m in captured if e == "turn_completed")
        assert meta["reasoning_gt_output"] is True
        assert meta["reasoning_output_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_reasoning_within_output_not_flagged(self):
        """Normal case: reasoning <= output → no anomaly flag."""
        config = StreamingSessionConfig(
            agent_name="test", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config)
        captured: list = []
        session._analytics_log_activity = (
            lambda event_type, *, metadata=None: captured.append((event_type, metadata))
        )
        result = CodexTurnResult()
        await session._handle_event(
            {"type": "turn.completed", "usage": {
                "input_tokens": 100, "output_tokens": 5000,
                "cached_input_tokens": 50, "reasoning_output_tokens": 4096,
            }},
            result,
        )
        meta = next(m for e, m in captured if e == "turn_completed")
        assert meta["reasoning_gt_output"] is False


class TestCodexWorkerDoneCallback:
    """Worker-task watchdog: surface silent worker death (#206).

    Pathological case: the worker exits while the session is still CONNECTED.
    Broker thinks the session is alive, the queue piles up, nothing processes.
    The callback drives the session straight to DEAD (no inline reconnect — the
    worker owns the queue) so the broker/heartbeat resurrects it via
    attempt_reconnect on the next inbound/recovery tick.
    """

    def _make_session(self):
        config = StreamingSessionConfig(
            agent_name="murzik-test", working_dir="/tmp",
            provider_url="codex_cli", provider_key="test",
        )
        return CodexSession(config)

    @pytest.mark.asyncio
    async def test_callback_marks_dead_on_silent_exit(self):
        """Worker finishes without exception while CONNECTED → session → DEAD."""
        s = self._make_session()
        await _to_connected(s)

        # Build a real completed task (graceful exit, no exception, no cancel).
        async def _no_op():
            return None
        task = asyncio.create_task(_no_op())
        await task

        s._worker_done_callback(task)
        await asyncio.sleep(0)  # let the scheduled _terminalize_dead run
        await asyncio.sleep(0)

        assert s.state == SessionState.DEAD, (
            "silent worker exit must drive DEAD so the broker can resurrect"
        )

    @pytest.mark.asyncio
    async def test_callback_marks_dead_on_exception_exit(self):
        """Worker that raised while CONNECTED also drives the session DEAD."""
        s = self._make_session()
        await _to_connected(s)

        async def _raises():
            raise RuntimeError("simulated worker crash")
        task = asyncio.create_task(_raises())
        # Drain the exception so asyncio doesn't warn on garbage collect.
        try:
            await task
        except RuntimeError:
            pass

        s._worker_done_callback(task)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert s.state == SessionState.DEAD

    @pytest.mark.asyncio
    async def test_callback_noop_when_cancelled(self):
        """Graceful disconnect cancels the worker — callback must NOT treat
        that as a wedge (disconnect already drove the state)."""
        s = self._make_session()
        await _to_connected(s)

        async def _block_forever():
            await asyncio.Event().wait()
        task = asyncio.create_task(_block_forever())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        s._worker_done_callback(task)
        await asyncio.sleep(0)
        # Cancelled → early return → state untouched (still CONNECTED).
        assert s.state == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_callback_noop_when_not_connected(self):
        """Worker exit when the session isn't CONNECTED (already torn down):
        callback is a no-op, no spurious transition."""
        s = self._make_session()
        await _to_dead(s)  # already DEAD

        async def _no_op():
            return None
        task = asyncio.create_task(_no_op())
        await task

        s._worker_done_callback(task)
        await asyncio.sleep(0)
        assert s.state == SessionState.DEAD  # unchanged


class TestCodexIsHealthy:
    """is_healthy() — synchronous diagnostic probe for wedge detection."""

    def _make_session(self):
        config = StreamingSessionConfig(
            agent_name="murzik-test", working_dir="/tmp",
            provider_url="codex_cli", provider_key="test",
        )
        return CodexSession(config)

    def test_shape(self):
        """Probe returns the documented keys, no extras drifting in."""
        s = self._make_session()
        h = s.is_healthy()
        assert set(h.keys()) == {
            "connected", "worker_alive", "processing",
            "queue_depth", "seconds_since_active", "wedged",
        }

    def test_fresh_session_not_wedged(self):
        """Just-constructed session: not connected, no worker, not wedged."""
        s = self._make_session()
        h = s.is_healthy()
        assert h["connected"] is False
        assert h["worker_alive"] is False
        assert h["wedged"] is False  # disconnected ≠ wedged

    @pytest.mark.asyncio
    async def test_detects_wedge_when_connected_but_worker_dead(self):
        """The exact pathological shape we're guarding: broker thinks
        we're connected (_connected=True), but the worker task is
        done. ``wedged`` must be True so broker / health endpoint can
        surface it."""
        s = self._make_session()
        await _to_connected(s)

        async def _exits_immediately():
            return None
        s._worker_task = asyncio.create_task(_exits_immediately())
        await s._worker_task  # let it finish

        h = s.is_healthy()
        assert h["worker_alive"] is False
        assert h["connected"] is True
        assert h["wedged"] is True

    @pytest.mark.asyncio
    async def test_detects_wedge_when_processing_stale(self):
        """_processing flag stuck True with last_active >900s ago —
        worker likely hung mid-turn (the proc.wait wedge before the
        Tier 1.A timeout caught it)."""
        s = self._make_session()
        await _to_connected(s)
        s._processing = True
        # Pretend last_active was an hour ago.
        s.last_active = s.last_active - 3600

        # Worker still alive (would normally hide the wedge from the
        # first check) — pin it to a never-resolving task.
        async def _block():
            await asyncio.Event().wait()
        s._worker_task = asyncio.create_task(_block())
        try:
            h = s.is_healthy()
            assert h["processing"] is True
            assert h["wedged"] is True
        finally:
            s._worker_task.cancel()
            try:
                await s._worker_task
            except asyncio.CancelledError:
                pass

    def test_queue_depth_reflects_pending_messages(self):
        """queue_depth is the pending-message backlog — useful signal
        on its own for the health endpoint."""
        s = self._make_session()
        # Push without going through send() (avoids the not-connected drop).
        s._message_queue.put_nowait(("p1", "tg", "1", "1"))
        s._message_queue.put_nowait(("p2", "tg", "1", "2"))
        h = s.is_healthy()
        assert h["queue_depth"] == 2


class TestCodexPendingWakeCallback:
    """#591 P1#2 (Murzik round-2): the codex worker fires the pending
    wake-callback AFTER _exec_codex succeeds, not at queue.put time.
    Failed execs leave the boundary intact so the next attempt re-emits
    the directive.
    """

    @pytest.mark.asyncio
    async def test_worker_fires_pending_callback_after_exec_success(self):
        """Happy path: exec succeeds → pending callback fires once, then
        is cleared so a subsequent non-wake turn doesn't re-fire it."""
        config = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        await _to_connected(s)

        fires: list[str] = []
        s._pending_wake_callback = lambda: fires.append("delivered")

        async def fake_exec(prompt: str) -> CodexTurnResult:
            return CodexTurnResult()  # failed=False by default

        s._exec_codex = fake_exec  # type: ignore[assignment]
        s._message_queue.put_nowait(("wake prompt body", "", "", ""))

        worker = asyncio.create_task(s._message_worker())
        # Let worker process one turn, then stop the loop.
        await asyncio.sleep(0.05)
        await _to_dead(s)  # flip off CONNECTED to stop the worker loop
        s._message_queue.put_nowait(("noop", "", "", ""))  # unblock get()
        try:
            await asyncio.wait_for(worker, timeout=2.0)
        except asyncio.TimeoutError:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

        assert fires == ["delivered"], (
            "pending_wake_callback must fire exactly once on exec-success"
        )
        # And cleared so it doesn't re-fire on a subsequent non-wake turn.
        assert s._pending_wake_callback is None

    @pytest.mark.asyncio
    async def test_worker_skips_pending_callback_on_exec_failure(self):
        """Failure path: exec fails → callback does NOT fire. Boundary
        stays put so the next attempt re-emits the directive. Pending
        callback is still cleared so a subsequent non-wake turn doesn't
        spuriously fire it once exec recovers.
        """
        config = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        await _to_connected(s)

        fires: list[str] = []
        s._pending_wake_callback = lambda: fires.append("delivered")

        async def fake_exec_fail(prompt: str) -> CodexTurnResult:
            result = CodexTurnResult()
            result.failed = True
            return result

        s._exec_codex = fake_exec_fail  # type: ignore[assignment]
        s._message_queue.put_nowait(("wake prompt body", "", "", ""))

        worker = asyncio.create_task(s._message_worker())
        await asyncio.sleep(0.05)
        await _to_dead(s)  # flip off CONNECTED to stop the worker loop
        s._message_queue.put_nowait(("noop", "", "", ""))
        try:
            await asyncio.wait_for(worker, timeout=2.0)
        except asyncio.TimeoutError:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

        assert fires == [], (
            "pending_wake_callback MUST NOT fire on exec-failure — "
            "boundary stays put so retry re-emits"
        )
        # But the field IS cleared so a recovery turn doesn't replay
        # the (now-stale) callback against a turn it wasn't paired with.
        assert s._pending_wake_callback is None


# ── #98 Tier 2: codex app-server path ────────────────────────────────────


def _appserver_session(monkeypatch=None, **overrides):
    """Build a CodexSession with the app-server flag forced on."""
    config = StreamingSessionConfig(
        agent_name="test-agent",
        label="main",
        model=overrides.pop("model", ""),
        working_dir=overrides.pop("working_dir", "/tmp"),
        provider_url="codex_cli",
        provider_key="test-key",
        **overrides,
    )
    s = CodexSession(config)
    s._use_app_server = True
    return s


class _FakeAppClient:
    """Stand-in for CodexAppServerClient driven from notifications.

    On ``turn/start`` it replays a scripted notification sequence through the
    session's notification handler (which is exactly how the real read loop
    feeds the session), so the full translate→_handle_event path is exercised.
    """

    def __init__(self, session, notifications, *, thread_id="thr-1", fire_on_start=None):
        self._session = session
        self._notifications = notifications
        self._thread_id = thread_id
        self._fire_on_start = fire_on_start or []
        self.requests = []
        self.closed = False

    async def initialize(self, **kw):
        return {}

    async def request(self, method, params=None, *, timeout=600.0):
        self.requests.append((method, params or {}))
        if method == "thread/start":
            for m, p in self._fire_on_start:
                await self._session._on_appserver_notification(m, p)
            return {"thread": {"id": self._thread_id}}
        if method == "thread/resume":
            return {"thread": {"id": (params or {}).get("threadId", self._thread_id)}}
        if method == "turn/start":
            for m, p in self._notifications:
                await self._session._on_appserver_notification(m, p)
            return {"turn": {"id": "t1", "status": "completed"}}
        return {}

    async def close(self):
        self.closed = True


def _patch_ensure(session, fake):
    async def _ensure():
        session._app_client = fake
    session._ensure_app_server = _ensure


class TestCodexAppServerFlag:
    def test_flag_off_by_default(self, monkeypatch):
        monkeypatch.delenv("PINKY_CODEX_APP_SERVER", raising=False)
        config = StreamingSessionConfig(
            agent_name="a", working_dir="/tmp", provider_url="codex_cli",
        )
        assert CodexSession(config)._use_app_server is False

    def test_flag_on_when_set(self, monkeypatch):
        monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
        config = StreamingSessionConfig(
            agent_name="a", working_dir="/tmp", provider_url="codex_cli",
        )
        assert CodexSession(config)._use_app_server is True

    @pytest.mark.asyncio
    async def test_exec_dispatches_to_app_server_when_flagged(self):
        s = _appserver_session()
        sentinel = CodexTurnResult(thread_id="sentinel")

        async def _fake_app(prompt):
            return sentinel

        s._exec_codex_app_server = _fake_app
        out = await s._exec_codex("hi")
        assert out is sentinel


class TestCodexAppServerTranslation:
    """Unit tests for the slash→dot notification/item shim."""

    def _s(self):
        return _appserver_session()

    def test_thread_started(self):
        ev = self._s()._appserver_to_event("thread/started", {"thread": {"id": "abc"}})
        assert ev == {"type": "thread.started", "thread_id": "abc"}

    def test_turn_started(self):
        assert self._s()._appserver_to_event("turn/started", {}) == {"type": "turn.started"}

    def test_item_completed_agent_message(self):
        ev = self._s()._appserver_to_event(
            "item/completed", {"item": {"id": "0", "type": "agentMessage", "text": "hi"}}
        )
        assert ev["type"] == "item.completed"
        assert ev["item"] == {"type": "agent_message", "text": "hi", "id": "0"}

    def test_item_command_execution(self):
        item = {"id": "1", "type": "commandExecution", "command": "ls",
                "exitCode": 0, "aggregatedOutput": "out"}
        ev = self._s()._appserver_to_event("item/completed", {"item": item})
        assert ev["item"] == {
            "type": "command_execution", "command": "ls",
            "exit_code": 0, "aggregated_output": "out", "id": "1",
        }

    def test_item_file_change(self):
        item = {"id": "2", "type": "fileChange",
                "changes": [{"path": "/x/y.py", "kind": "update", "diff": "..."}]}
        ev = self._s()._appserver_to_event("item/completed", {"item": item})
        assert ev["item"] == {"type": "file_edit", "filepath": "/x/y.py", "id": "2"}

    def test_item_mcp_tool_call(self):
        item = {"id": "3", "type": "mcpToolCall", "tool": "send", "arguments": {"text": "hi"}}
        ev = self._s()._appserver_to_event("item/completed", {"item": item})
        assert ev["item"] == {
            "type": "mcp_tool_call", "tool_name": "send",
            "input": {"text": "hi"}, "id": "3",
        }

    def test_item_dynamic_tool_call(self):
        item = {"id": "4", "type": "dynamicToolCall", "tool": "Grep", "arguments": {"q": "x"}}
        ev = self._s()._appserver_to_event("item/completed", {"item": item})
        assert ev["item"]["type"] == "function_call"
        assert ev["item"]["tool_name"] == "Grep"

    def test_turn_completed_maps_usage(self):
        s = self._s()
        s._appserver_last_usage = {
            "inputTokens": 12, "outputTokens": 3,
            "cachedInputTokens": 4, "reasoningOutputTokens": 1,
        }
        ev = s._appserver_to_event("turn/completed", {"turn": {"status": "completed"}})
        assert ev == {"type": "turn.completed", "usage": {
            "input_tokens": 12, "output_tokens": 3,
            "cached_input_tokens": 4, "reasoning_output_tokens": 1,
        }}

    def test_turn_completed_failed_maps_to_turn_failed(self):
        ev = self._s()._appserver_to_event(
            "turn/completed", {"turn": {"status": "failed", "error": {"message": "boom"}}}
        )
        assert ev == {"type": "turn.failed", "error": {"message": "boom"}}

    def test_error_notification(self):
        ev = self._s()._appserver_to_event("error", {"error": {"message": "rate limited"}})
        assert ev == {"type": "error", "message": "rate limited"}

    def test_unknown_method_returns_none(self):
        assert self._s()._appserver_to_event("thread/status/changed", {}) is None


class TestCodexAppServerApprovals:
    def _s(self):
        return _appserver_session()

    @pytest.mark.asyncio
    async def test_exec_command_approval(self):
        assert await self._s()._on_appserver_request("execCommandApproval", {}) == {
            "decision": "approved"}

    @pytest.mark.asyncio
    async def test_apply_patch_approval(self):
        assert await self._s()._on_appserver_request("applyPatchApproval", {}) == {
            "decision": "approved"}

    @pytest.mark.asyncio
    async def test_command_execution_request_approval(self):
        out = await self._s()._on_appserver_request(
            "item/commandExecution/requestApproval", {})
        assert out == {"decision": "accept"}

    @pytest.mark.asyncio
    async def test_file_change_request_approval(self):
        out = await self._s()._on_appserver_request("item/fileChange/requestApproval", {})
        assert out == {"decision": "accept"}

    @pytest.mark.asyncio
    async def test_permissions_request_approval(self):
        out = await self._s()._on_appserver_request("item/permissions/requestApproval", {})
        assert out == {"permissions": {}, "scope": "session"}

    @pytest.mark.asyncio
    async def test_unknown_request_returns_empty(self):
        assert await self._s()._on_appserver_request("mcpServer/elicitation/request", {}) == {}


class TestCodexAppServerEffortAndConfig:
    def test_effort_passthrough(self):
        s = _appserver_session()
        s._reasoning_effort = "high"
        assert s._appserver_effort() == "high"

    def test_effort_max_maps_to_high(self):
        s = _appserver_session()
        s._reasoning_effort = "max"
        assert s._appserver_effort() == "high"

    def test_effort_invalid_returns_none(self):
        s = _appserver_session()
        s._reasoning_effort = "bananas"
        assert s._appserver_effort() is None

    def test_config_builds_mcp_servers(self):
        s = _appserver_session()
        s._mcp_servers = {
            "pinky": {"url": "http://x/mcp", "headers": {"X-Agent-Name": "test-agent"}},
            "empty": {"url": ""},
        }
        cfg = s._appserver_config()
        assert cfg == {"mcp_servers": {
            "pinky": {"url": "http://x/mcp", "http_headers": {"X-Agent-Name": "test-agent"}},
        }}

    def test_config_empty_when_no_servers(self):
        s = _appserver_session()
        s._mcp_servers = {}
        assert s._appserver_config() == {}


class TestCodexAppServerTurn:
    """Full-turn integration via a fake app-server client."""

    @pytest.mark.asyncio
    async def test_simple_turn_accumulates_text_and_usage(self):
        s = _appserver_session()
        notifications = [
            ("thread/started", {"thread": {"id": "thr-1"}}),
            ("turn/started", {}),
            ("item/agentMessage/delta", {"delta": "hel"}),
            ("item/completed", {"item": {"id": "0", "type": "agentMessage", "text": "hello"}}),
            ("thread/tokenUsage/updated", {"tokenUsage": {"last": {
                "inputTokens": 100, "outputTokens": 10,
                "cachedInputTokens": 5, "reasoningOutputTokens": 2,
            }}}),
            ("turn/completed", {"threadId": "thr-1", "turn": {"id": "t1", "status": "completed"}}),
        ]
        fake = _FakeAppClient(s, notifications)
        _patch_ensure(s, fake)

        result = await s._exec_codex_app_server("hi there")

        assert not result.failed
        assert result.text_parts == ["hello"]
        assert result.input_tokens == 100
        assert result.output_tokens == 10
        assert result.cached_input_tokens == 5
        assert result.reasoning_output_tokens == 2
        # thread id captured + queued for resume-handle persistence
        assert s.codex_session_id == "thr-1"
        assert s.resume_handle == "thr-1"
        assert s._pending_resume_handle_update == "thr-1"
        # request sequence: fresh thread/start then turn/start
        methods = [m for m, _ in fake.requests]
        assert methods == ["thread/start", "turn/start"]

    @pytest.mark.asyncio
    async def test_thread_id_from_response_when_no_notification(self):
        s = _appserver_session()
        # No thread/started notification — rely on the thread/start response.
        notifications = [
            ("turn/started", {}),
            ("item/completed", {"item": {"id": "0", "type": "agentMessage", "text": "ok"}}),
            ("turn/completed", {"turn": {"status": "completed"}}),
        ]
        fake = _FakeAppClient(s, notifications, thread_id="resp-thread")
        _patch_ensure(s, fake)

        result = await s._exec_codex_app_server("hi")
        assert not result.failed
        assert s.codex_session_id == "resp-thread"

    @pytest.mark.asyncio
    async def test_resume_uses_existing_thread_id(self):
        s = _appserver_session()
        s.codex_session_id = "existing-thread"
        notifications = [
            ("item/completed", {"item": {"id": "0", "type": "agentMessage", "text": "hi"}}),
            ("turn/completed", {"turn": {"status": "completed"}}),
        ]
        fake = _FakeAppClient(s, notifications)
        _patch_ensure(s, fake)

        await s._exec_codex_app_server("again")
        methods = [m for m, _ in fake.requests]
        assert methods == ["thread/resume", "turn/start"]
        # turn/start targets the existing thread
        turn_params = dict(fake.requests)["turn/start"]
        assert turn_params["threadId"] == "existing-thread"
        assert turn_params["input"] == [{"type": "text", "text": "again"}]

    @pytest.mark.asyncio
    async def test_command_execution_recorded(self):
        s = _appserver_session()
        notifications = [
            ("thread/started", {"thread": {"id": "thr-1"}}),
            ("item/completed", {"item": {
                "id": "0", "type": "commandExecution",
                "command": "ls -la", "exitCode": 0, "aggregatedOutput": "total 8",
            }}),
            ("item/completed", {"item": {"id": "1", "type": "agentMessage", "text": "done"}}),
            ("turn/completed", {"turn": {"status": "completed"}}),
        ]
        fake = _FakeAppClient(s, notifications)
        _patch_ensure(s, fake)

        result = await s._exec_codex_app_server("run ls")
        assert len(result.tool_uses) == 1
        assert result.tool_uses[0]["tool"] == "Bash"
        assert result.tool_uses[0]["input"]["command"] == "ls -la"
        assert result.text_parts == ["done"]

    @pytest.mark.asyncio
    async def test_failed_turn(self):
        s = _appserver_session()
        notifications = [
            ("thread/started", {"thread": {"id": "thr-1"}}),
            ("turn/completed", {"turn": {"status": "failed", "error": {"message": "rate limited"}}}),
        ]
        fake = _FakeAppClient(s, notifications)
        _patch_ensure(s, fake)

        result = await s._exec_codex_app_server("boom")
        assert result.failed
        assert "rate limited" in result.errors[0]

    @pytest.mark.asyncio
    async def test_connect_failure_returns_failed_result(self):
        s = _appserver_session()

        async def _boom():
            raise RuntimeError("spawn failed")

        s._ensure_app_server = _boom
        result = await s._exec_codex_app_server("hi")
        assert result.failed
        assert "spawn failed" in result.errors[0]


# -- Exec serialization / stderr drain / delta dedupe regressions ---------


def _plain_session(**overrides):
    config = StreamingSessionConfig(
        agent_name="test-agent",
        label="main",
        model="",
        working_dir=overrides.pop("working_dir", "/tmp"),
        provider_url="codex_cli",
        provider_key="test-key",
        **overrides,
    )
    return CodexSession(config)


class TestExecSerialization:
    @pytest.mark.asyncio
    async def test_idle_sleep_save_exec_serializes_with_worker(self):
        """idle_sleep()'s save turn must not run concurrently with a worker
        turn -- two parallel execs would resume the same codex thread and
        clobber the shared kill handle / app-server turn state."""
        s = _plain_session()

        active = 0
        max_active = 0
        exec_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_exec(prompt: str) -> CodexTurnResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            exec_started.set()
            await release.wait()
            active -= 1
            return CodexTurnResult()

        s._exec_codex = fake_exec  # type: ignore[assignment]

        await s.connect()  # queues the wake prompt; worker starts executing it
        await asyncio.wait_for(exec_started.wait(), timeout=5)

        sleep_task = asyncio.create_task(s.idle_sleep())
        await asyncio.sleep(0.05)  # give idle_sleep a chance to start its exec
        release.set()

        assert await asyncio.wait_for(sleep_task, timeout=5) is True
        assert max_active == 1


class TestExecStderrDrain:
    @pytest.mark.asyncio
    async def test_large_stderr_does_not_wedge_exec(self, tmp_path):
        """A child writing more than the OS pipe buffer (~64KiB) to stderr
        mid-turn must not block the turn: stderr is drained concurrently
        rather than read only after stdout EOF."""
        s = _plain_session(working_dir=str(tmp_path))
        s._use_app_server = False  # exercise the legacy exec path
        script = tmp_path / "fake_codex.py"
        script.write_text(
            "import json, sys\n"
            "sys.stdin.read()\n"
            "sys.stderr.write('x' * (256 * 1024))\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({'type': 'item.completed',\n"
            "                  'item': {'type': 'agent_message', 'text': 'hi'}}))\n"
        )
        s._build_codex_cmd = lambda: [sys.executable, str(script)]  # type: ignore[assignment]

        result = await asyncio.wait_for(s._exec_codex("hello"), timeout=30)

        assert not result.failed
        assert result.text_parts == ["hi"]


class TestAssistantDeltaDedupe:
    @pytest.mark.asyncio
    async def test_app_server_streams_assistant_text_once(self):
        """item/completed must not re-emit text already streamed via
        item/agentMessage/delta notifications."""
        s = _appserver_session()
        events: list[dict] = []

        async def capture(ev: dict) -> None:
            events.append(ev)

        s._stream_event_callback = capture
        notifications = [
            ("thread/started", {"thread": {"id": "thr-1"}}),
            ("item/agentMessage/delta", {"delta": "hel"}),
            ("item/agentMessage/delta", {"delta": "lo"}),
            ("item/completed", {"item": {"id": "0", "type": "agentMessage", "text": "hello"}}),
            ("turn/completed", {"turn": {"status": "completed"}}),
        ]
        fake = _FakeAppClient(s, notifications)
        _patch_ensure(s, fake)

        result = await s._exec_codex_app_server("hi")

        assert result.text_parts == ["hello"]
        deltas = [e["delta"] for e in events if e["type"] == "assistant_delta"]
        assert deltas == ["hel", "lo"]

    @pytest.mark.asyncio
    async def test_legacy_agent_message_still_emits_delta(self, monkeypatch):
        """The legacy exec path has no incremental deltas -- the full text on
        item.completed is its only assistant_delta and must keep flowing."""
        monkeypatch.delenv("PINKY_CODEX_APP_SERVER", raising=False)
        s = _plain_session()
        events: list[dict] = []

        async def capture(ev: dict) -> None:
            events.append(ev)

        s._stream_event_callback = capture
        result = CodexTurnResult()
        await s._handle_event(
            {"type": "item.completed",
             "item": {"id": "0", "type": "agent_message", "text": "hello"}},
            result,
        )

        assert result.text_parts == ["hello"]
        deltas = [e["delta"] for e in events if e["type"] == "assistant_delta"]
        assert deltas == ["hello"]


# ── #206: CodexSession state-machine parity (BOOTING/RECONNECTING) ──────────
#
# Murzik-required coverage: cold-start BOOTING→CONNECTED (and →DEAD on substrate
# failure, no leaked owner token), warm reconnect RECONNECTING→CONNECTED/DEAD,
# worker-death terminalization, per-turn turn/failed staying CONNECTED, and the
# state_entered_at stamp surfaced in stats.


class TestCodexStateMachine:
    @pytest.mark.asyncio
    async def test_cold_connect_exposes_booting_then_connected(self):
        # App-server mode: BOOTING is held across spawn+initialize, then flips
        # CONNECTED. Block _ensure_app_server to observe BOOTING.
        s = _appserver_session()
        gate = asyncio.Event()

        async def blocked_ensure():
            await gate.wait()
            s._app_client = object()  # mark substrate "up"

        s._ensure_app_server = blocked_ensure  # type: ignore[assignment]
        s._start_worker = lambda: None  # type: ignore[assignment] — no real worker
        s._enqueue_wake = _noop  # type: ignore[assignment]
        s._analytics_session_started = lambda: None  # type: ignore[assignment]

        task = asyncio.create_task(s.connect())
        await asyncio.sleep(0.03)  # let connect reach the blocked substrate bring-up
        assert s.state == SessionState.BOOTING
        gate.set()
        await asyncio.wait_for(task, timeout=2)
        assert s.state == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_cold_connect_appserver_init_failure_dies_no_leak(self):
        # App-server initialize failure during BOOT → BOOTING completes to DEAD,
        # and the owner token is NOT leaked (a later resurrection can proceed).
        s = _appserver_session()

        async def failing_ensure():
            raise RuntimeError("app-server init boom")

        s._ensure_app_server = failing_ensure  # type: ignore[assignment]

        with pytest.raises(RuntimeError):
            await s.connect()
        assert s.state == SessionState.DEAD
        assert s._state_machine._in_flight is None, "owner token leaked on BOOT failure"

        # No leak → resurrection works: DEAD → RECONNECTING → CONNECTED.
        async def ok_ensure():
            s._app_client = object()

        s._ensure_app_server = ok_ensure  # type: ignore[assignment]
        s._start_worker = lambda: None  # type: ignore[assignment]
        s._enqueue_wake = _noop  # type: ignore[assignment]
        s._analytics_session_started = lambda: None  # type: ignore[assignment]
        await s.connect()
        assert s.state == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_ensure_app_server_init_failure_is_atomic(self, monkeypatch):
        # P1 (#206 P2 review, Murzik): a spawn that succeeds but whose
        # initialize() raises must NOT leave a half-initialized client/proc
        # cached. The early-return guard in _ensure_app_server() checks only
        # (client is not None and proc alive), so a cached-but-uninitialized
        # substrate would make the next reconnect skip initialization and flip
        # CONNECTED on a dead app-server.
        s = _appserver_session()

        class _FakeProc:
            def __init__(self):
                self.returncode = None  # "alive" — would satisfy the guard
                self.pid = 4242
                self.killed = False

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                return self.returncode

        class _InitBoomClient:
            def __init__(self):
                self.closed = False

            async def initialize(self, **kw):
                raise RuntimeError("initialize boom")

            async def close(self):
                self.closed = True

        boom_procs: list = []
        boom_clients: list = []

        async def fake_spawn_boom(**kw):
            proc, client = _FakeProc(), _InitBoomClient()
            boom_procs.append(proc)
            boom_clients.append(client)
            return client, proc

        monkeypatch.setattr(
            "pinky_daemon.codex_session.spawn_app_server", fake_spawn_boom
        )

        with pytest.raises(RuntimeError, match="initialize boom"):
            await s._ensure_app_server()

        # Half-initialized substrate must be torn down, not cached.
        assert s._app_client is None
        assert s._app_proc is None
        assert boom_clients[0].closed is True, "client not closed on init failure"
        assert boom_procs[0].killed is True, "proc not killed on init failure"

        # A later attempt must respawn AND re-run initialize — the guard must
        # not short-circuit on the cleared (None) fields.
        init_calls: list = []

        class _OkClient:
            async def initialize(self, **kw):
                init_calls.append(True)

            async def close(self):
                pass

        async def fake_spawn_ok(**kw):
            return _OkClient(), _FakeProc()

        monkeypatch.setattr(
            "pinky_daemon.codex_session.spawn_app_server", fake_spawn_ok
        )
        await s._ensure_app_server()
        assert init_calls == [True], "initialization was skipped on reconnect"
        assert s._app_client is not None
        assert s._app_proc is not None

    @pytest.mark.asyncio
    async def test_warm_reconnect_exposes_reconnecting_then_connected(self):
        s = _appserver_session()
        await _to_dead(s)
        s._RECONNECT_BACKOFF = (0,)
        gate = asyncio.Event()

        async def blocked_bring_up():
            await gate.wait()

        s._bring_up_substrate = blocked_bring_up  # type: ignore[assignment]
        s._start_worker = lambda: None  # type: ignore[assignment]
        s._enqueue_wake = _noop  # type: ignore[assignment]
        s._analytics_session_started = lambda: None  # type: ignore[assignment]

        task = asyncio.create_task(s.attempt_reconnect())
        await asyncio.sleep(0.03)  # grant RECONNECTING, then block in bring-up
        assert s.state == SessionState.RECONNECTING
        gate.set()
        await asyncio.wait_for(task, timeout=2)
        assert s.state == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_warm_reconnect_exhausts_budget_to_dead(self):
        s = _appserver_session()
        await _to_dead(s)
        s._RECONNECT_BACKOFF = (0, 0)  # two quick attempts, both fail

        async def always_fail():
            raise RuntimeError("substrate down")

        s._bring_up_substrate = always_fail  # type: ignore[assignment]

        await s.attempt_reconnect()
        assert s.state == SessionState.DEAD
        assert s._state_machine._in_flight is None
        assert s.stats["reconnects"] == 2

    @pytest.mark.asyncio
    async def test_worker_death_terminalizes_and_visible_in_stats(self):
        # Worker exits unexpectedly while CONNECTED → session DEAD, and the
        # watchdog (which reads stats) sees state=="dead"/connected=False.
        s = _plain_session()
        await _to_connected(s)

        async def _no_op():
            return None
        task = asyncio.create_task(_no_op())
        await task

        s._worker_done_callback(task)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert s.state == SessionState.DEAD
        assert s.stats["state"] == "dead"
        assert s.stats["connected"] is False

    @pytest.mark.asyncio
    async def test_app_server_turn_failed_leaves_connected(self):
        # A per-turn MODEL failure (turn.failed) does NOT tear down the
        # transport, so the session stays CONNECTED (vs. a structural transport
        # failure, which terminalizes — see codex_session app-server paths).
        s = _appserver_session()
        await _to_connected(s)
        result = CodexTurnResult()
        await s._handle_event(
            {"type": "turn.failed", "error": {"message": "model declined"}},
            result,
        )
        assert result.failed is True
        assert s.state == SessionState.CONNECTED

    @pytest.mark.asyncio
    async def test_state_entered_at_surfaced_in_stats(self):
        s = _plain_session()
        before = s.stats["state_entered_at"]  # UNINITIALIZED, stamped at construction
        await _to_connected(s)
        after = s.stats["state_entered_at"]
        assert after >= before
        assert s.stats["state_entered_at"] == s._state_machine.state_entered_at
        assert s.stats["state"] == "connected"
