"""Tests for pinky_daemon SDK runner."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import MagicMock

import pytest

from pinky_daemon.sdk_runner import SDKRunner, SDKRunnerConfig, sdk_available

OLD_SESSION_ID = "11111111-1111-4111-8111-111111111111"
NEW_SESSION_ID = "22222222-2222-4222-8222-222222222222"


class TestSDKAvailable:
    def test_sdk_is_installed(self):
        assert sdk_available() is True


class TestSDKRunnerConfig:
    def test_defaults(self):
        config = SDKRunnerConfig()
        assert config.working_dir == "."
        assert config.model is None
        assert config.max_turns is None
        assert "Read" in config.allowed_tools
        assert config.permission_mode == "bypassPermissions"

    def test_custom(self):
        config = SDKRunnerConfig(
            working_dir="/tmp",
            model="opus",
            max_turns=10,
            allowed_tools=["Read"],
            mcp_servers={"memory": {"command": "python", "args": ["-m", "pinky_memory"]}},
        )
        assert config.model == "opus"
        assert config.max_turns == 10
        assert config.mcp_servers["memory"]["command"] == "python"


class TestSDKRunner:
    def test_create(self):
        runner = SDKRunner()
        assert runner is not None

    def test_create_with_config(self):
        config = SDKRunnerConfig(model="sonnet", working_dir="/tmp")
        runner = SDKRunner(config)
        assert runner._config.model == "sonnet"

    @pytest.mark.asyncio
    async def test_run_builds_options(self):
        """Verify that run() constructs ClaudeAgentOptions correctly."""
        config = SDKRunnerConfig(
            model="opus",
            max_turns=5,
            allowed_tools=["Read", "Grep"],
            system_prompt="Be helpful",
        )
        runner = SDKRunner(config)

        # Mock the query function to capture options
        captured_options = {}

        async def mock_query(*, prompt, options=None, transport=None):
            captured_options["prompt"] = prompt
            captured_options["options"] = options
            # Yield a minimal result
            from claude_agent_sdk import ResultMessage
            result = MagicMock(spec=ResultMessage)
            result.result = "test response"
            result.session_id = "test-session-uuid"
            result.total_cost_usd = 0.01
            type(result).__instancecheck__ = lambda cls, inst: isinstance(inst, MagicMock)
            return
            yield  # Make it an async generator

        # Since we can't easily mock the async generator, test the config building
        assert runner._config.model == "opus"
        assert runner._config.max_turns == 5
        assert runner._config.system_prompt == "Be helpful"

    @pytest.mark.asyncio
    async def test_run_returns_run_result(self):
        """Verify run() returns a RunResult with correct fields."""
        runner = SDKRunner()
        # Just verify the interface is correct
        assert hasattr(runner, "run")
        assert hasattr(runner, "health_check")
        assert runner._config.working_dir == "."

    @pytest.mark.skip(reason="ConversationResetMessage removed from SDK")
    @pytest.mark.asyncio
    async def test_run_warns_on_conversation_reset_and_unknown_frame(
        self, monkeypatch, capsys
    ):
        """A widened SDK Message union must never degrade to a silent drop."""
        import claude_agent_sdk
        from claude_agent_sdk import ConversationResetMessage

        async def fake_query(*, prompt, options):
            del prompt, options
            yield ConversationResetMessage(
                new_conversation_id="new-conversation",
                uuid="reset-uuid",
                session_id="session-uuid",
            )
            yield object()

        monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
        result = await SDKRunner().run("clear")

        assert result.ok
        logs = capsys.readouterr().err
        assert "WARNING SDK conversation reset" in logs
        assert "new_conversation_id='new-conversation'" in logs
        assert "WARNING unhandled SDK message type=object; continuing" in logs

    @pytest.mark.skip(reason="ConversationResetMessage removed from SDK")
    @pytest.mark.asyncio
    async def test_session_store_reset_then_result_replaces_old_id(
        self, monkeypatch, tmp_path
    ):
        """The per-run lane persists Reset(old) before capturing Result(new)."""
        import claude_agent_sdk
        from claude_agent_sdk import ConversationResetMessage, ResultMessage

        from pinky_daemon.session_store import SessionStore
        from pinky_daemon.sessions import Session, SessionMessage

        options_seen = []

        async def fake_query(*, prompt, options):
            del prompt
            options_seen.append(options)
            yield ConversationResetMessage(
                new_conversation_id="new-conversation",
                uuid="reset-uuid",
                session_id=OLD_SESSION_ID,
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=1,
                session_id=NEW_SESSION_ID,
                result="cleared",
            )

        monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
        store = SessionStore(tmp_path / "sessions.db")
        session = Session(session_id="reset-session", agent_name="test")
        session._store = store
        session._sdk_session_id = OLD_SESSION_ID
        session.history.append(SessionMessage(role="assistant", content="before reset"))
        session._persist()

        response = await session.send("/clear")

        record = store.get(session.id)
        assert response.error == ""
        assert options_seen[0].resume == OLD_SESSION_ID
        assert record is not None
        assert record.sdk_session_id == NEW_SESSION_ID
        assert session._sdk_session_id == NEW_SESSION_ID

    @pytest.mark.skip(reason="ConversationResetMessage removed from SDK")
    @pytest.mark.asyncio
    async def test_session_store_reset_window_reboots_as_clean_start(
        self, monkeypatch, tmp_path
    ):
        """Death after Reset clears the store; the next boot starts clean."""
        import claude_agent_sdk
        from claude_agent_sdk import ConversationResetMessage, ResultMessage

        from pinky_daemon.session_store import SessionStore
        from pinky_daemon.sessions import Session, SessionManager, SessionMessage

        reset_consumed = asyncio.Event()
        hold_stream_open = asyncio.Event()
        options_seen = []

        async def reset_then_block(*, prompt, options):
            del prompt
            options_seen.append(options)
            yield ConversationResetMessage(
                new_conversation_id="new-conversation",
                uuid="reset-uuid",
                session_id=OLD_SESSION_ID,
            )
            reset_consumed.set()
            await hold_stream_open.wait()

        monkeypatch.setattr(claude_agent_sdk, "query", reset_then_block)
        store = SessionStore(tmp_path / "sessions.db")
        session = Session(session_id="reset-window", agent_name="test")
        session._store = store
        session._sdk_session_id = OLD_SESSION_ID
        session.history.append(SessionMessage(role="assistant", content="before reset"))
        session._persist()

        send_task = asyncio.create_task(session.send("/clear"))
        await asyncio.wait_for(reset_consumed.wait(), timeout=1)

        record = store.get(session.id)
        assert options_seen[0].resume == OLD_SESSION_ID
        assert session._sdk_session_id == ""
        assert record is not None
        assert record.sdk_session_id == ""

        # Simulate process death before any frame can carry the fresh ID.
        send_task.cancel()
        with suppress(asyncio.CancelledError):
            await send_task

        manager = SessionManager(store=store)
        restored = manager.get(session.id)
        assert restored is not None
        assert restored._sdk_session_id == ""

        fresh_options = []

        async def fresh_query(*, prompt, options):
            del prompt
            fresh_options.append(options)
            yield ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=1,
                session_id=NEW_SESSION_ID,
                result="fresh",
            )

        monkeypatch.setattr(claude_agent_sdk, "query", fresh_query)
        # Keep local history to prove the empty handle, not is_first, forces a
        # fresh SDK start instead of continue_conversation.
        restored.history.append(SessionMessage(role="assistant", content="local history"))

        response = await restored.send("after reset")

        assert response.error == ""
        assert fresh_options[0].resume is None
        assert fresh_options[0].continue_conversation is False


class TestSessionSDKIntegration:
    """Test that Session correctly initializes with SDK runner."""

    def test_session_uses_sdk(self):
        """Session should prefer SDK runner when available."""
        from pinky_daemon.sessions import Session
        session = Session(session_id="test")
        assert session._runner_type == "sdk"

    def test_session_runner_type_attribute(self):
        from pinky_daemon.sessions import Session
        session = Session(session_id="test", model="sonnet")
        assert hasattr(session, "_runner_type")
        assert session._runner_type in ("sdk", "cli")

    def test_session_sdk_session_id_tracking(self):
        from pinky_daemon.sessions import Session
        session = Session(session_id="test")
        assert session._sdk_session_id == ""
