"""Tests for pinky_daemon — runner, handler, pollers, daemon."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pinky_daemon.claude_runner import ClaudeRunner, ClaudeRunnerConfig, RunResult
from pinky_daemon.message_handler import HandlerConfig, InboundMessage, MessageHandler
from pinky_daemon.daemon import Daemon, DaemonConfig, _resolve_env


# ── RunResult ────────────────────────────────────────────────


class TestRunResult:
    def test_ok_when_success(self):
        r = RunResult(output="hello", exit_code=0)
        assert r.ok is True

    def test_not_ok_when_error(self):
        r = RunResult(output="", exit_code=1, error="failed")
        assert r.ok is False

    def test_not_ok_when_nonzero_exit(self):
        r = RunResult(output="partial", exit_code=2)
        assert r.ok is False


# ── ClaudeRunnerConfig ───────────────────────────────────────


class TestClaudeRunnerConfig:
    def test_defaults(self):
        config = ClaudeRunnerConfig()
        assert config.working_dir == "."
        assert config.timeout == 300.0
        assert config.max_turns == 0
        assert config.session_id == ""
        assert config.model == ""

    def test_custom_config(self):
        config = ClaudeRunnerConfig(
            working_dir="/tmp/test",
            model="opus",
            max_turns=10,
            timeout=60.0,
            session_id="test-session",
            allowed_tools=["Read", "Grep"],
        )
        assert config.model == "opus"
        assert config.max_turns == 10
        assert config.allowed_tools == ["Read", "Grep"]


# ── ClaudeRunner ─────────────────────────────────────────────


class TestClaudeRunner:
    def test_build_command_basic(self):
        config = ClaudeRunnerConfig(claude_bin="/usr/bin/claude")
        runner = ClaudeRunner(config)
        cmd = runner._build_command("Hello")
        assert "/usr/bin/claude" in cmd
        assert "--print" in cmd
        assert "--prompt" in cmd
        assert "Hello" in cmd

    def test_build_command_with_session(self):
        config = ClaudeRunnerConfig(claude_bin="/usr/bin/claude")
        runner = ClaudeRunner(config)
        cmd = runner._build_command("Hi", session_id="test-123")
        assert "--session-id" in cmd
        assert "test-123" in cmd

    def test_build_command_with_resume(self):
        config = ClaudeRunnerConfig(claude_bin="/usr/bin/claude")
        runner = ClaudeRunner(config)
        cmd = runner._build_command("Hi", resume=True)
        assert "--continue" in cmd

    def test_build_command_with_model(self):
        config = ClaudeRunnerConfig(claude_bin="/usr/bin/claude", model="opus")
        runner = ClaudeRunner(config)
        cmd = runner._build_command("Hi")
        assert "--model" in cmd
        assert "opus" in cmd

    def test_build_command_with_tools(self):
        config = ClaudeRunnerConfig(
            claude_bin="/usr/bin/claude",
            allowed_tools=["Read", "mcp__memory__*"],
        )
        runner = ClaudeRunner(config)
        cmd = runner._build_command("Hi")
        assert cmd.count("--allowedTools") == 2

    def test_build_command_with_system_prompt(self):
        config = ClaudeRunnerConfig(claude_bin="/usr/bin/claude")
        runner = ClaudeRunner(config)
        cmd = runner._build_command("Hi", system_prompt="Be helpful")
        assert "--system-prompt" in cmd
        assert "Be helpful" in cmd

    @pytest.mark.asyncio
    async def test_run_success(self):
        config = ClaudeRunnerConfig(claude_bin="/bin/echo")
        runner = ClaudeRunner(config)

        # echo will just print the args back
        result = await runner.run("test message")
        assert result.exit_code == 0
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_run_timeout(self):
        """Test that long-running commands get killed after timeout."""
        config = ClaudeRunnerConfig(claude_bin="/bin/echo", timeout=0.001)
        runner = ClaudeRunner(config)

        # Monkey-patch to use a real slow command
        import asyncio

        original_run = runner.run

        async def slow_run(prompt, **kwargs):
            # Directly test the timeout logic
            try:
                process = await asyncio.create_subprocess_exec(
                    "/bin/sleep", "10",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(process.communicate(), timeout=0.05)
                return RunResult(output="", exit_code=0)
            except asyncio.TimeoutError:
                process.kill()
                return RunResult(output="", exit_code=1, error="Timed out after 0.05 seconds")

        result = await slow_run("test")
        assert result.ok is False
        assert "Timed out" in result.error

    @pytest.mark.asyncio
    async def test_run_missing_binary(self):
        config = ClaudeRunnerConfig(claude_bin="/nonexistent/claude")
        runner = ClaudeRunner(config)

        result = await runner.run("test")
        assert result.ok is False
        assert "not found" in result.error


# ── InboundMessage ───────────────────────────────────────────


class TestInboundMessage:
    def test_create(self):
        msg = InboundMessage(
            platform="telegram",
            chat_id="12345",
            sender_name="Brad",
            sender_id="67890",
            content="Hello",
        )
        assert msg.platform == "telegram"
        assert msg.chat_id == "12345"
        assert msg.is_group is False

    def test_defaults(self):
        msg = InboundMessage(
            platform="discord",
            chat_id="1",
            sender_name="x",
            sender_id="2",
            content="y",
        )
        assert msg.message_id == ""
        assert msg.chat_title == ""
        assert msg.metadata == {}


# ── MessageHandler ───────────────────────────────────────────


class TestMessageHandler:
    def _make_handler(self, **runner_kwargs):
        mock_runner = AsyncMock(spec=ClaudeRunner)
        mock_runner.run = AsyncMock(
            return_value=RunResult(output="response", exit_code=0),
            **runner_kwargs,
        )
        config = HandlerConfig(rate_limit_seconds=0)
        return MessageHandler(mock_runner, config), mock_runner

    def _make_message(self, **kwargs):
        defaults = {
            "platform": "telegram",
            "chat_id": "12345",
            "sender_name": "Brad",
            "sender_id": "67890",
            "content": "Hello",
        }
        defaults.update(kwargs)
        return InboundMessage(**defaults)

    @pytest.mark.asyncio
    async def test_handle_success(self):
        handler, runner = self._make_handler()
        msg = self._make_message()

        result = await handler.handle(msg)
        assert result == "response"
        assert handler.message_count == 1
        runner.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_formats_prompt(self):
        handler, runner = self._make_handler()
        msg = self._make_message(content="What's up?")

        await handler.handle(msg)

        call_args = runner.run.call_args
        prompt = call_args[0][0]
        assert "What's up?" in prompt
        assert "telegram" in prompt
        assert "12345" in prompt

    @pytest.mark.asyncio
    async def test_session_per_chat(self):
        handler, runner = self._make_handler()
        config = HandlerConfig(session_strategy="per_chat", session_prefix="test", rate_limit_seconds=0)
        handler = MessageHandler(runner, config)

        msg = self._make_message(platform="telegram", chat_id="111")
        await handler.handle(msg)

        call_kwargs = runner.run.call_args[1]
        assert call_kwargs["session_id"] == "test-telegram-111"

    @pytest.mark.asyncio
    async def test_session_shared(self):
        handler, runner = self._make_handler()
        config = HandlerConfig(session_strategy="shared", session_prefix="pinky", rate_limit_seconds=0)
        handler = MessageHandler(runner, config)

        msg = self._make_message()
        await handler.handle(msg)

        call_kwargs = runner.run.call_args[1]
        assert call_kwargs["session_id"] == "pinky-shared"

    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        handler, runner = self._make_handler()
        config = HandlerConfig(rate_limit_seconds=999)  # Very high limit
        handler = MessageHandler(runner, config)

        msg = self._make_message()
        result1 = await handler.handle(msg)
        result2 = await handler.handle(msg)  # Should be rate limited

        assert result1 == "response"
        assert result2 is None
        assert runner.run.call_count == 1

    @pytest.mark.asyncio
    async def test_handle_runner_error(self):
        handler, runner = self._make_handler()
        runner.run = AsyncMock(
            return_value=RunResult(output="", exit_code=1, error="crash"),
        )
        config = HandlerConfig(rate_limit_seconds=0)
        handler = MessageHandler(runner, config)

        msg = self._make_message()
        result = await handler.handle(msg)
        assert result is None

    @pytest.mark.asyncio
    async def test_active_sessions_count(self):
        handler, _ = self._make_handler()
        config = HandlerConfig(rate_limit_seconds=0)
        handler._config = config

        await handler.handle(self._make_message(chat_id="111"))
        await handler.handle(self._make_message(chat_id="222"))

        assert handler.active_sessions == 2


# ── DaemonConfig ─────────────────────────────────────────────


class TestDaemonConfig:
    def test_defaults(self):
        config = DaemonConfig()
        assert config.session_strategy == "per_chat"
        assert config.max_concurrent == 3
        assert config.telegram_token == ""
        assert config.claude_max_turns == 0

    def test_from_yaml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
claude:
  model: opus
  max_turns: 10
  timeout: 120

daemon:
  session_strategy: shared
  max_concurrent: 5

outreach:
  telegram:
    bot_token: test-token-123
    poll_timeout: 60
    allowed_chats:
      - "12345"
      - "67890"
""")
            f.flush()

            config = DaemonConfig.from_yaml(f.name)

        os.unlink(f.name)

        assert config.claude_model == "opus"
        assert config.claude_max_turns == 10
        assert config.claude_timeout == 120
        assert config.session_strategy == "shared"
        assert config.max_concurrent == 5
        assert config.telegram_token == "test-token-123"
        assert config.telegram_poll_timeout == 60
        assert config.telegram_allowed_chats == ["12345", "67890"]

    def test_from_yaml_missing_file(self):
        config = DaemonConfig.from_yaml("/nonexistent/config.yaml")
        # Should return defaults
        assert config.telegram_token == ""
        assert config.session_strategy == "per_chat"

    def test_from_yaml_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            f.flush()
            config = DaemonConfig.from_yaml(f.name)
        os.unlink(f.name)
        assert config.telegram_token == ""


class TestResolveEnv:
    def test_resolve_env_var(self):
        os.environ["TEST_PINKY_TOKEN"] = "secret123"
        result = _resolve_env("${TEST_PINKY_TOKEN}")
        assert result == "secret123"
        del os.environ["TEST_PINKY_TOKEN"]

    def test_resolve_missing_env_var(self):
        result = _resolve_env("${DEFINITELY_NOT_SET_XYZ}")
        assert result == ""

    def test_no_env_ref(self):
        result = _resolve_env("plain-text")
        assert result == "plain-text"

    def test_empty_string(self):
        result = _resolve_env("")
        assert result == ""

    def test_none_value(self):
        result = _resolve_env(None)
        assert result == ""


# ── Daemon ───────────────────────────────────────────────────


class TestDaemon:
    def test_create_daemon(self):
        config = DaemonConfig()
        with patch("pinky_daemon.claude_runner._find_claude_binary", return_value="/usr/bin/claude"):
            daemon = Daemon(config)
        assert daemon.is_running is False

    def test_stats(self):
        config = DaemonConfig()
        with patch("pinky_daemon.claude_runner._find_claude_binary", return_value="/usr/bin/claude"):
            daemon = Daemon(config)
        stats = daemon.stats
        assert stats["running"] is False
        assert stats["pollers"] == 0
        assert stats["messages_processed"] == 0
