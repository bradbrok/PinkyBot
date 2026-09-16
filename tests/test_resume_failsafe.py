"""Failure attribution, cleanup and receipt contracts for resume recovery."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.codex_app_server import CodexAppServerError
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.transport_state import SessionState


@pytest.fixture(autouse=True)
def failsafe_enabled(monkeypatch):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")


def codex_session(tmp_path):
    session = CodexSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), model="gpt-5.6-sol",
        provider_url="codex_cli", resume_handle="saved-thread",
    ))
    session.codex_session_id = session.resume_handle = "saved-thread"
    session._state_machine._state = SessionState.CONNECTED
    session._emit_stream_event = AsyncMock()
    return session


@pytest.mark.parametrize("error", [
    CodexAppServerError("Internal error", code=-32603),
    CodexAppServerError("Invalid params", code=-32602),
    CodexAppServerError("transport EOF"),
    CodexAppServerError("Authentication required", code=-32000),
    CodexAppServerError("Rate limit exceeded", code=-32000),
    CodexAppServerError("Billing limit reached", code=-32000),
    CodexAppServerError("Permission denied", code=-32603),
    CodexAppServerError("Unable to read configuration", code=-32603),
    TimeoutError("resume timed out"),
], ids=["internal", "params", "eof", "auth", "rate", "billing", "permission", "config", "timeout"])
async def test_appserver_unknown_resume_failure_never_starts_fresh(tmp_path, error):
    ss = codex_session(tmp_path)
    client = SimpleNamespace(request=AsyncMock(side_effect=error), close=AsyncMock())
    ss._app_client = client
    ss._ensure_app_server = AsyncMock(return_value=True)
    receipt = asyncio.get_running_loop().create_future()
    accepted = MagicMock(return_value=True)
    result = await ss._exec_codex_app_server(
        "Continue", scheduler_delivery=receipt, scheduler_accept=accepted,
    )
    assert result.failed
    assert [call.args[0] for call in client.request.await_args_list] == ["thread/resume"]
    assert await receipt is False
    accepted.assert_not_called()
    assert ss.codex_session_id == "saved-thread"


@pytest.mark.parametrize("phase", ["turn_request", "accepted_turn", "ambiguous_tool"])
async def test_appserver_never_replays_after_resume_succeeds(tmp_path, phase):
    ss = codex_session(tmp_path)
    requests = []
    tools = []

    async def request(method, params):
        requests.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": "saved-thread"}}
        assert method == "turn/start"
        if phase == "ambiguous_tool":
            tools.append("tool executed")
            await ss._on_appserver_notification("item/started", {
                "item": {"id": "tool-1", "type": "commandExecution", "command": "true"},
            })
        if phase != "accepted_turn":
            raise CodexAppServerError("transport EOF after submission")
        await ss._on_appserver_notification("turn/completed", {
            "threadId": "saved-thread", "turn": {"id": "turn-1", "status": "failed",
            "error": {"message": "model failed after acceptance"}},
        })
        return {"turn": {"id": "turn-1"}}

    ss._app_client = SimpleNamespace(request=request, close=AsyncMock())
    ss._ensure_app_server = AsyncMock(return_value=True)
    receipt = asyncio.get_running_loop().create_future()
    accepted = MagicMock(return_value=True)
    result = await ss._exec_codex_app_server(
        "Do work once", scheduler_delivery=receipt, scheduler_accept=accepted,
    )
    assert result.failed
    assert [method for method, _ in requests] == ["thread/resume", "turn/start"]
    assert accepted.call_count == int(phase == "accepted_turn")
    assert len(tools) == int(phase == "ambiguous_tool")


@pytest.mark.parametrize("error", [
    RuntimeError("authentication required"), RuntimeError("rate limit"),
    RuntimeError("billing failure"), RuntimeError("exit code 1"),
    EOFError("closed"), TimeoutError("timeout"), PermissionError("denied"),
    FileNotFoundError("executable missing"), asyncio.CancelledError(),
    SystemExit(1), KeyboardInterrupt(),
    BaseExceptionGroup("cancelled", [asyncio.CancelledError(), RuntimeError("error")]),
], ids=["auth", "rate", "billing", "exit", "eof", "timeout", "permission", "exec", "cancel", "exit-system", "interrupt", "group-cancel"])
async def test_sdk_failure_is_not_a_resume_retry_and_closes_partial_client(
    tmp_path, monkeypatch, error,
):
    client = SimpleNamespace(connect=AsyncMock(side_effect=error), disconnect=AsyncMock())
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle="saved-handle",
    ))
    with pytest.raises(BaseException) as caught:
        await ss.connect()
    assert caught.value is error
    assert factory.call_count == 1
    client.disconnect.assert_awaited_once()
    assert ss.state == SessionState.DEAD


@pytest.mark.parametrize("diagnostic", [
    "authentication required", "rate limit exceeded", "billing limit reached",
    "permission denied", "configuration invalid", "unknown startup error",
])
async def test_exec_unknown_rejection_preserves_one_receipt_and_error_evidence(
    tmp_path, monkeypatch, diagnostic,
):
    ss = codex_session(tmp_path)
    ss._use_app_server = False
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_eof()
    stderr.feed_data(diagnostic.encode())
    stderr.feed_eof()
    proc = SimpleNamespace(
        stdin=SimpleNamespace(write=MagicMock(), drain=AsyncMock(), close=MagicMock(),
                              wait_closed=AsyncMock()),
        stdout=stdout, stderr=stderr, returncode=1, wait=AsyncMock(return_value=1),
        kill=MagicMock(),
    )
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("pinky_daemon.codex_session.asyncio.create_subprocess_exec", spawn)
    receipt = asyncio.get_running_loop().create_future()
    accepted = MagicMock(return_value=True)
    result = await ss._exec_codex(
        "Perform once", scheduler_delivery=receipt, scheduler_accept=accepted,
    )
    assert result.failed
    assert spawn.await_count == 1
    assert accepted.call_count == 1
    assert await receipt is True
    assert diagnostic in " ".join(result.errors)
    assert ss.codex_session_id == "saved-thread"


async def test_exec_tool_work_without_text_is_never_replayed(tmp_path, monkeypatch):
    ss = codex_session(tmp_path)
    ss._use_app_server = False
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    for event in [
        {"type": "thread.started", "thread_id": "saved-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "one", "type": "command_execution",
         "command": "true", "exit_code": 0, "aggregated_output": ""}},
        {"type": "turn.failed", "error": {"message": "transport lost"}},
    ]:
        stdout.feed_data(json.dumps(event).encode() + b"\n")
    stdout.feed_eof()
    stderr.feed_data(b"exit code 1")
    stderr.feed_eof()
    proc = SimpleNamespace(
        stdin=SimpleNamespace(write=MagicMock(), drain=AsyncMock(), close=MagicMock(),
                              wait_closed=AsyncMock()),
        stdout=stdout, stderr=stderr, returncode=1, wait=AsyncMock(return_value=1),
        kill=MagicMock(),
    )
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("pinky_daemon.codex_session.asyncio.create_subprocess_exec", spawn)
    result = await ss._exec_codex("Perform once")
    assert result.failed
    assert not result.text_parts
    assert result.tool_uses
    assert spawn.await_count == 1


MISSING_THREAD = "11111111-1111-4111-8111-111111111111"
FRESH_THREAD = "22222222-2222-4222-8222-222222222222"


def missing_rollout_error(thread_id=MISSING_THREAD):
    # Codex rust-v0.154.0, commit 6b9826e3aa83b1a5947db50f4332cb9c65f1b340:
    # app-server/src/request_processors/thread_processor.rs:4510,5819 and
    # app-server/tests/suite/v2/thread_resume.rs:443. Match the correlated ID,
    # exact missing-rollout reason, -32600 and absent data, never code alone.
    return CodexAppServerError(
        f"no rollout found for thread id {thread_id}", code=-32600, data=None,
    )


@pytest.mark.parametrize("fresh_fails", [False, True], ids=["fresh-success", "fresh-failure"])
async def test_missing_rollout_retries_once_before_turn_start(tmp_path, fresh_fails):
    ss = codex_session(tmp_path)
    ss.codex_session_id = ss.resume_handle = MISSING_THREAD
    ss._config.resume_handle = MISSING_THREAD
    ss._pending_resume_handle_update = MISSING_THREAD
    persisted = []

    async def persist(agent, handle):
        persisted.append(handle)

    ss._on_resume_handle = persist
    requests = []

    async def request(method, params):
        requests.append((method, params))
        if method == "thread/resume":
            assert params["threadId"] == MISSING_THREAD
            raise missing_rollout_error()
        if method == "thread/start":
            assert ss.codex_session_id == ""
            assert ss.resume_handle == ""
            assert ss._config.resume_handle == ""
            assert ss._pending_resume_handle_update == ""
            assert "" in persisted
            if fresh_fails:
                raise CodexAppServerError("Authentication required", code=-32000)
            return {"thread": {"id": FRESH_THREAD}}
        assert method == "turn/start"
        await ss._on_appserver_notification("turn/completed", {
            "threadId": FRESH_THREAD, "turn": {"id": "one", "status": "completed"},
        })
        return {"turn": {"id": "one"}}

    ss._app_client = SimpleNamespace(request=request, close=AsyncMock())
    ss._ensure_app_server = AsyncMock(return_value=True)
    receipt = asyncio.get_running_loop().create_future()
    accepted = MagicMock(return_value=True)
    result = await ss._exec_codex_app_server(
        "Perform once", scheduler_delivery=receipt, scheduler_accept=accepted,
    )
    expected = ["thread/resume", "thread/start"] + ([] if fresh_fails else ["turn/start"])
    assert [method for method, _ in requests] == expected
    assert result.failed is fresh_fails
    assert accepted.call_count == int(not fresh_fails)
    assert await receipt is (not fresh_fails)
    start = requests[1][1]
    assert start["cwd"] == str(tmp_path)
    assert start["model"] == "gpt-5.6-sol"
    assert start["approvalPolicy"] == ss._APPROVAL_POLICY
    assert start["sandbox"] == ss._SANDBOX_MODE
    assert "model_auto_compact_token_limit" not in start.get("config", {})
    events = [call.args[0] for call in ss._emit_stream_event.await_args_list]
    attempted = [e for e in events if e.get("type") == "resume_fallback_attempted"]
    assert len(attempted) == 1
    recovered = [e for e in events if e.get("type") == "resume_failed_restarted_fresh"]
    assert len(recovered) == int(not fresh_fails)
    if not fresh_fails:
        await ss._on_appserver_notification("thread/started", {"thread": {"id": MISSING_THREAD}})
        assert ss.codex_session_id == FRESH_THREAD
        assert ss._pending_resume_handle_update != MISSING_THREAD


@pytest.mark.parametrize("error", [
    missing_rollout_error(FRESH_THREAD),
    CodexAppServerError(f"no rollout found for thread id {MISSING_THREAD}", code=-32603),
    CodexAppServerError(f"no rollout found for thread id {MISSING_THREAD}", code=-32600,
                       data={"reason": "permission_denied"}),
    CodexAppServerError("no rollout found for thread id not-a-uuid", code=-32600),
], ids=["wrong-id", "wrong-code", "conflicting-data", "non-uuid"])
async def test_missing_rollout_lookalikes_are_not_positive(tmp_path, error):
    ss = codex_session(tmp_path)
    ss.codex_session_id = ss.resume_handle = MISSING_THREAD
    client = SimpleNamespace(request=AsyncMock(side_effect=error), close=AsyncMock())
    ss._app_client = client
    ss._ensure_app_server = AsyncMock(return_value=True)
    result = await ss._exec_codex_app_server("Perform once")
    assert result.failed
    assert [call.args[0] for call in client.request.await_args_list] == ["thread/resume"]


async def test_missing_rollout_message_after_turn_submission_is_not_retried(tmp_path):
    ss = codex_session(tmp_path)
    ss.codex_session_id = ss.resume_handle = MISSING_THREAD
    requests = []

    async def request(method, params):
        requests.append(method)
        if method == "thread/resume":
            return {"thread": {"id": MISSING_THREAD}}
        raise missing_rollout_error()

    ss._app_client = SimpleNamespace(request=request, close=AsyncMock())
    ss._ensure_app_server = AsyncMock(return_value=True)
    result = await ss._exec_codex_app_server("Perform once")
    assert result.failed
    assert requests == ["thread/resume", "turn/start"]


@pytest.mark.parametrize("fresh_fails", [False, True], ids=["fresh-success", "fresh-failure"])
async def test_sdk_verified_missing_target_one_owner_and_one_fresh_attempt(
    tmp_path, monkeypatch, fresh_fails,
):
    from claude_agent_sdk._errors import ProcessError

    # SDK 0.2.138 Query._read_messages propagates the CLI error result through
    # a pending initialize. Verified offline with its bundled CLI: this exact
    # requested-UUID message rejects connect before any user query is sent.
    rejection = ProcessError(
        f"Claude Code returned an error result: No conversation found with session ID: {MISSING_THREAD}",
        exit_code=1,
    )
    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=MISSING_THREAD,
    ))
    clients = []
    options_seen = []
    states = []
    cleared = []
    ss._on_resume_handle_sync = lambda name, handle: cleared.append(handle)
    ss._on_resume_handle = AsyncMock(side_effect=lambda name, handle: cleared.append(handle))
    ss._reader_loop = AsyncMock()
    ss._query_unrouted = AsyncMock()
    complete = AsyncMock(wraps=ss._state_machine.transition_complete)
    monkeypatch.setattr(ss._state_machine, "transition_complete", complete)

    def factory(options):
        index = len(clients)
        options_seen.append(options.resume)

        async def connect():
            states.append(ss.state)
            if index == 0:
                raise rejection
            assert ss.resume_handle == ""
            assert ss._config.resume_handle == ""
            assert "" in cleared
            if fresh_fails:
                raise RuntimeError("fresh startup unavailable")

        client = SimpleNamespace(
            connect=connect, disconnect=AsyncMock(), get_server_info=AsyncMock(return_value={}),
        )
        clients.append(client)
        return client

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    caught = None
    try:
        await ss.connect()
    except Exception as exc:
        caught = exc
    assert len(clients) == 2
    assert options_seen[0] == MISSING_THREAD
    assert not options_seen[1]
    clients[0].disconnect.assert_awaited_once()
    assert states == [SessionState.BOOTING, SessionState.BOOTING]
    assert complete.await_count == 1
    assert ss.state == (SessionState.DEAD if fresh_fails else SessionState.CONNECTED)
    assert (caught is not None) is fresh_fails
    for task in list(ss._background_tasks):
        await task
    if ss._reader_task:
        await ss._reader_task


async def test_sdk_reconnect_backoff_cannot_reset_fresh_budget(tmp_path, monkeypatch):
    from claude_agent_sdk._errors import ProcessError

    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=MISSING_THREAD,
    ))
    ss._state_machine._state = SessionState.RECONNECTING
    ss._RECONNECT_BACKOFF = (0, 0, 0)
    ss.disconnect = AsyncMock()
    options_seen = []

    def factory(options):
        options_seen.append(options.resume)
        error = ProcessError(
            f"Claude Code returned an error result: No conversation found with session ID: {MISSING_THREAD}",
            exit_code=1,
        ) if len(options_seen) == 1 else RuntimeError("fresh failed")
        return SimpleNamespace(connect=AsyncMock(side_effect=error), disconnect=AsyncMock())

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    await ss._reconnect_with_backoff()
    assert len(options_seen) == 2
    assert options_seen[0] == MISSING_THREAD
    assert not options_seen[1]
    assert ss.state == SessionState.DEAD


@pytest.mark.parametrize("flag", [None, "0"])
async def test_appserver_failsafe_default_off(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("PINKY_RESUME_FAILSAFE", raising=False)
    else:
        monkeypatch.setenv("PINKY_RESUME_FAILSAFE", flag)
    ss = codex_session(tmp_path)
    ss.codex_session_id = ss.resume_handle = MISSING_THREAD
    client = SimpleNamespace(request=AsyncMock(side_effect=missing_rollout_error()), close=AsyncMock())
    ss._app_client = client
    ss._ensure_app_server = AsyncMock(return_value=True)
    result = await ss._exec_codex_app_server("Perform once")
    assert result.failed
    assert [call.args[0] for call in client.request.await_args_list] == ["thread/resume"]


@pytest.mark.parametrize("suffix", [FRESH_THREAD, "not-a-uuid", ""])
async def test_sdk_missing_target_lookalike_cannot_retry(tmp_path, monkeypatch, suffix):
    from claude_agent_sdk._errors import ProcessError

    error = ProcessError(
        f"Claude Code returned an error result: No conversation found with session ID: {suffix}",
        exit_code=1,
    )
    client = SimpleNamespace(connect=AsyncMock(side_effect=error), disconnect=AsyncMock())
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=MISSING_THREAD,
    ))
    with pytest.raises(ProcessError):
        await ss.connect()
    assert factory.call_count == 1
    assert ss.resume_handle == MISSING_THREAD


async def test_sdk_two_attempts_share_outer_startup_timeout(tmp_path, monkeypatch):
    from claude_agent_sdk._errors import ProcessError

    from pinky_daemon.api import _bounded_cold_start_connect

    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=MISSING_THREAD,
    ))
    attempts = []

    def factory(options):
        index = len(attempts)
        attempts.append(options.resume)

        async def connect():
            if index == 0:
                raise ProcessError(
                    f"Claude Code returned an error result: No conversation found with session ID: {MISSING_THREAD}",
                    exit_code=1,
                )
            await asyncio.Event().wait()

        return SimpleNamespace(connect=connect, disconnect=AsyncMock())

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    ss.disconnect = AsyncMock()
    with pytest.raises(TimeoutError):
        await _bounded_cold_start_connect(ss, agent_name="sample", label="main", timeout=0.03)
    assert len(attempts) == 2
    assert not attempts[1]
    ss.disconnect.assert_awaited_once()
    assert ss.state == SessionState.DEAD


async def test_resume_rejection_after_early_turn_notification_cannot_retry(tmp_path):
    ss = codex_session(tmp_path)
    ss.codex_session_id = ss.resume_handle = MISSING_THREAD
    methods = []

    async def request(method, params):
        methods.append(method)
        await ss._on_appserver_notification("turn/started", {"threadId": MISSING_THREAD})
        raise missing_rollout_error()

    ss._app_client = SimpleNamespace(request=request, close=AsyncMock())
    ss._ensure_app_server = AsyncMock(return_value=True)
    result = await ss._exec_codex_app_server("Perform once")
    assert result.failed
    assert methods == ["thread/resume"]


async def test_missing_target_cleanup_failure_never_spawns_fresh(tmp_path, monkeypatch):
    from claude_agent_sdk._errors import ProcessError

    error = ProcessError(
        f"Claude Code returned an error result: No conversation found with session ID: {MISSING_THREAD}",
        exit_code=1,
    )
    client = SimpleNamespace(connect=AsyncMock(side_effect=error),
                             disconnect=AsyncMock(side_effect=RuntimeError("still live")))
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=MISSING_THREAD,
    ))
    with pytest.raises(ProcessError):
        await ss.connect()
    assert factory.call_count == 1
    assert ss._client is client
    assert ss.resume_handle == MISSING_THREAD
    assert ss.state == SessionState.DEAD


async def test_rejected_thread_notification_cannot_restore_cleared_id(tmp_path):
    ss = codex_session(tmp_path)
    ss.codex_session_id = ss.resume_handle = MISSING_THREAD
    ss._ensure_app_server = AsyncMock(return_value=True)

    async def request(method, params):
        if method == "thread/resume":
            raise missing_rollout_error()
        if method == "thread/start":
            await ss._on_appserver_notification("thread/started", {"thread": {"id": MISSING_THREAD}})
            assert ss.codex_session_id == ""
            assert ss._pending_resume_handle_update == ""
            return {"thread": {"id": FRESH_THREAD}}
        await ss._on_appserver_notification("turn/completed", {
            "threadId": FRESH_THREAD, "turn": {"id": "one", "status": "completed"},
        })
        return {"turn": {"id": "one"}}

    ss._app_client = SimpleNamespace(request=request, close=AsyncMock())
    result = await ss._exec_codex_app_server("Perform once")
    assert not result.failed
    assert ss.codex_session_id == FRESH_THREAD


async def test_sdk_replacement_cleanup_failure_keeps_child_handle(tmp_path):
    ss = StreamingSession(StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path)))
    ss._state_machine._state = SessionState.CONNECTED
    client = SimpleNamespace(disconnect=AsyncMock(side_effect=RuntimeError("still live")))
    ss._client = client
    spawn = AsyncMock()
    with pytest.raises(RuntimeError, match="still live"):
        await ss.restart_transport(target_preflight=lambda: None, bring_up=spawn)
    spawn.assert_not_awaited()
    assert ss._client is client


@pytest.mark.parametrize("error", [
    asyncio.CancelledError(), SystemExit(1), KeyboardInterrupt(),
    BaseExceptionGroup("cancelled", [asyncio.CancelledError(), RuntimeError("error")]),
])
async def test_appserver_control_exceptions_cleanup_without_retry(tmp_path, error):
    ss = codex_session(tmp_path)
    ss.codex_session_id = ss.resume_handle = MISSING_THREAD
    client = SimpleNamespace(request=AsyncMock(side_effect=error), close=AsyncMock())
    ss._app_client = client
    ss._ensure_app_server = AsyncMock(return_value=True)
    with pytest.raises(BaseException) as caught:
        await ss._exec_codex_app_server("Perform once")
    assert caught.value is error
    assert [call.args[0] for call in client.request.await_args_list] == ["thread/resume"]
    client.close.assert_awaited_once()
    assert ss.state == SessionState.DEAD


async def test_untyped_sdk_error_with_exact_missing_text_does_not_retry(tmp_path, monkeypatch):
    error = RuntimeError(
        f"Claude Code returned an error result: No conversation found with session ID: {MISSING_THREAD} (exit code: 1)"
    )
    error.exit_code = 1
    client = SimpleNamespace(connect=AsyncMock(side_effect=error), disconnect=AsyncMock())
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=MISSING_THREAD,
    ))
    with pytest.raises(RuntimeError) as caught:
        await ss.connect()
    assert caught.value is error
    assert factory.call_count == 1
    assert ss.resume_handle == MISSING_THREAD


async def test_reconnect_cleanup_uncertainty_cannot_spawn_another_child(tmp_path, monkeypatch):
    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=MISSING_THREAD,
    ))
    ss._state_machine._state = SessionState.RECONNECTING
    ss._RECONNECT_BACKOFF = (0, 0, 0)
    ss.disconnect = AsyncMock()
    client = SimpleNamespace(
        connect=AsyncMock(side_effect=RuntimeError("startup unavailable")),
        disconnect=AsyncMock(side_effect=RuntimeError("cleanup unconfirmed")),
    )
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    await ss._reconnect_with_backoff()
    assert factory.call_count == 1
    assert ss._client is client
    assert ss.state == SessionState.DEAD


async def test_sdk_cleanup_uses_operation_deadline_and_claims_budget_first(tmp_path, monkeypatch):
    import time

    from claude_agent_sdk._errors import ProcessError

    from pinky_daemon.resume_recovery import RecoveryOperation

    ss = StreamingSession(StreamingSessionConfig(
        agent_name="sample", working_dir=str(tmp_path), resume_handle=MISSING_THREAD,
    ))
    operation = RecoveryOperation(deadline=time.monotonic() + 0.03)
    ss._recovery_operation = operation
    error = ProcessError(
        f"Claude Code returned an error result: No conversation found with session ID: {MISSING_THREAD}",
        exit_code=1,
    )

    async def disconnect():
        assert operation.fresh_used
        await asyncio.Event().wait()

    client = SimpleNamespace(connect=AsyncMock(side_effect=error), disconnect=disconnect)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    with pytest.raises(ProcessError) as caught:
        await asyncio.wait_for(ss.connect(), timeout=1)
    assert caught.value is error
    assert isinstance(error.__cause__, TimeoutError)
    assert operation.cleanup_failed
    assert factory.call_count == 1
    assert ss._client is client
