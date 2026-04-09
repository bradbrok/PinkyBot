"""Tests for CodexSession — Codex CLI agent provider."""

from __future__ import annotations

import pytest

from pinky_daemon.codex_session import CodexSession, CodexTurnResult
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
        s = self._make_session()
        assert s.agent_name == "test-agent"
        assert s.id == "test-agent-main"
        assert s.is_connected is False
        assert isinstance(s.stats, dict)
        assert s.stats["connected"] is False
        assert s.stats["account"]["apiProvider"] == "codex_cli"

    def test_stats_shape(self):
        s = self._make_session()
        stats = s.stats
        for key in ("turns", "messages_sent", "errors", "connected",
                     "current_activity", "activity_log", "cost_usd", "account"):
            assert key in stats, f"missing key: {key}"

    def test_session_id_defaults_empty(self):
        s = self._make_session()
        assert s.session_id == ""
        assert s.codex_session_id == ""


class TestCodexJSONLParsing:
    """Test JSONL event parsing via CodexSession._handle_event."""

    def _parse_events(self, events: list[dict]) -> CodexTurnResult:
        """Use the real _handle_event method for parsing."""
        config = StreamingSessionConfig(
            agent_name="test", working_dir="/tmp", provider_url="codex_cli",
        )
        session = CodexSession(config)
        result = CodexTurnResult()
        for event in events:
            session._handle_event(event, result)
        return result

    def test_simple_text_response(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "0", "type": "agent_message", "text": "hello"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
        ]
        r = self._parse_events(events)
        assert r.thread_id == "abc-123"
        assert r.text_parts == ["hello"]
        assert r.input_tokens == 100
        assert r.output_tokens == 10
        assert not r.failed

    def test_command_execution(self):
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
        r = self._parse_events(events)
        assert len(r.tool_uses) == 1
        assert r.tool_uses[0]["tool"] == "Bash"
        assert r.tool_uses[0]["input"]["command"] == "ls -la"
        assert r.tool_uses[0]["exit_code"] == 0
        assert r.text_parts == ["done"]

    def test_turn_failed(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "rate limited"}},
        ]
        r = self._parse_events(events)
        assert r.failed
        assert "rate limited" in r.errors[0]

    def test_multiple_text_parts(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "0", "type": "agent_message", "text": "part 1"}},
            {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "part 2"}},
            {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 20}},
        ]
        r = self._parse_events(events)
        assert r.text_parts == ["part 1", "part 2"]

    def test_error_item(self):
        events = [
            {"type": "thread.started", "thread_id": "abc-123"},
            {"type": "item.completed", "item": {"id": "0", "type": "error", "message": "bad model"}},
            {"type": "turn.completed", "usage": {}},
        ]
        r = self._parse_events(events)
        assert "bad model" in r.errors


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


class TestCodexSessionDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        config = StreamingSessionConfig(
            agent_name="test",
            working_dir="/tmp",
            provider_url="codex_cli",
        )
        s = CodexSession(config)
        # Should be safe to call multiple times
        await s.disconnect()
        await s.disconnect()
        assert not s.is_connected
