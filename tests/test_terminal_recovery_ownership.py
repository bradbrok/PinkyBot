"""Independent corrective review: real lifecycle, disposable client boundaries."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.transport_state import SessionState, Trigger
from tests.recovery_test_support import closure_value, set_flags
from tests.recovery_test_support import lifecycle_harness as lifecycle_harness


@pytest.mark.parametrize("mode", ["b", "both"])
async def test_ensure_cannot_overwrite_client_retained_after_cleanup_failure(
    lifecycle_harness, monkeypatch, mode,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    ss = h.seed()
    retained = ss._client
    retained.disconnect.side_effect = RuntimeError("original child remains alive")
    ss._RECONNECT_BACKOFF = (0,)
    await ss.attempt_reconnect()
    assert ss.state == SessionState.DEAD
    assert ss._client is retained
    before = len(h.clients)
    try:
        await h.app.state.broker._ensure_session_callback("sample")
    except RuntimeError:
        pass
    assert len(h.clients) == before, "Ensure spawned over an uncleaned retained SDK child"
    assert ss._client is retained
    assert not retained.closed.is_set()


@pytest.mark.parametrize("mode", ["b", "both", "off"])
async def test_rename_preserves_current_resume_callback_authority(
    lifecycle_harness, monkeypatch, mode,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    h.seed()
    response = await h.client.post("/agents/sample/streaming-sessions?label=secondary")
    assert response.status_code == 200, response.text
    ss = h.app.state.broker._streaming["sample"]["secondary"]
    await ss._on_resume_handle("sample", "before-rename")
    response = await h.client.patch(
        "/agents/sample/streaming-sessions/secondary", json={"label": "renamed"},
    )
    assert response.status_code == 200, response.text
    await ss._on_resume_handle("sample", "after-rename")
    assert h.app.state.agents.get_streaming_session_id("sample", label="renamed") == "after-rename"


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("entry", ["skills", "stop_create"])
async def test_teardown_quiesces_recovery_before_replacing(
    lifecycle_harness, monkeypatch, source, entry,
):
    h = lifecycle_harness
    old = h.seed((source, "sdk"))
    old._RECONNECT_BACKOFF = (137.0,)
    entered, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay):
        if delay == 137.0:
            entered.set()
            await release.wait()
        else:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(old.attempt_reconnect())
    await asyncio.wait_for(entered.wait(), 1)
    h.app.state.agents.register(
        "sample", runtime="codex_cli" if source == "claude_sdk" else "claude_sdk",
    )
    try:
        if entry == "skills":
            response = await h.client.post("/agents/sample/skills/apply")
            assert response.status_code == 200, response.text
            assert response.json()["session_restarted"]
        else:
            response = await h.client.post("/agents/sample/stop")
            assert response.status_code == 200, response.text
            response = await h.client.post("/agents/sample/streaming-sessions")
            assert response.status_code == 200, response.text
        current = h.app.state.broker._streaming["sample"]["main"]
        assert current is not old
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert old.state == SessionState.DEAD, "Unregistered recovery owner resumed after teardown/replacement"
    assert current.state == SessionState.CONNECTED


async def terminal_entry(h, entry, label):
    if entry == "delete":
        response = await h.client.delete(f"/agents/sample/streaming-sessions/{label}")
        assert response.status_code == 200, response.text
    elif entry == "stop":
        response = await h.client.post("/agents/sample/stop")
        assert response.status_code == 200, response.text
    elif entry == "shutdown":
        await h.app.router.on_shutdown[0]()
    elif entry == "watchdog":
        await h.app.state.watchdog._recover_fn("sample", label, "test")
    else:
        callback = getattr(h.app.state.broker, "_stop_callback" if entry == "stop_agent" else "_stop_all_callback")
        await callback(*(["sample"] if entry == "stop_agent" else []))


@pytest.mark.parametrize("mode", ["a", "b", "both"])
@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("entry", ["stop", "delete", "shutdown", "watchdog", "stop_agent", "stop_all"])
async def test_terminal_sinks_quiesce_owned_backoff(
    lifecycle_harness, monkeypatch, mode, source, entry,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    label = "secondary" if entry == "delete" else "main"
    old = h.seed((source, "sdk"), label=label)
    old._RECONNECT_BACKOFF = (137.0,)
    entered, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep
    owner = []

    async def pause(delay):
        if delay == 137.0:
            owner.append(asyncio.current_task())
            entered.set()
            await release.wait()
        else:
            await real_sleep(0 if delay == 2 else delay)

    monkeypatch.setattr(asyncio, "sleep", pause)
    task = asyncio.create_task(old.attempt_reconnect())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await terminal_entry(h, entry, label)
        assert owner[0].done(), "Terminal sink returned before its recovery owner stopped"
        assert old._state_machine._in_flight is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    # A-only watchdog retains the object; other terminal sinks retire it.
    if entry != "watchdog" or mode == "b":
        before = len(h.clients), len(h.trace)
        try:
            await old.attempt_reconnect()
        except RuntimeError:
            pass
        assert (len(h.clients), len(h.trace)) == before
        assert old.state == SessionState.DEAD


@pytest.mark.parametrize("mode", ["a", "b", "both"])
@pytest.mark.parametrize("entry", ["stop", "delete", "shutdown", "watchdog", "stop_agent", "stop_all"])
async def test_terminal_cleanup_failure_keeps_registered_owner(
    lifecycle_harness, monkeypatch, mode, entry,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    label = "secondary" if entry == "delete" else "main"
    ss = h.seed(label=label)
    retained = ss._client
    retained.disconnect.side_effect = RuntimeError("cleanup unconfirmed")
    try:
        await terminal_entry(h, entry, label)
    except RuntimeError:
        pass
    assert h.app.state.broker._streaming.get("sample", {}).get(label) is ss
    assert ss._client is retained
    assert len(h.clients) == 1
    assert ss._replacement_cleanup_strict is False


@pytest.mark.parametrize("entry", ["stop", "delete", "shutdown"])
async def test_terminal_sink_refuses_unowned_task(lifecycle_harness, entry):
    h = lifecycle_harness
    label = "secondary" if entry == "delete" else "main"
    ss = h.seed(label=label)
    task = asyncio.create_task(asyncio.Event().wait())
    ss._reconnect_task = task
    try:
        try:
            await terminal_entry(h, entry, label)
        except RuntimeError:
            pass
        assert not task.cancelling(), "Cancelled a task not minted by this transport"
        assert h.app.state.broker._streaming.get("sample", {}).get(label) is ss
        assert not ss._client.closed.is_set()
    finally:
        ss._reconnect_task = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("entry", ["connect", "ensure", "force"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_retained_cleanup_is_not_forgotten_across_startup_entries(
    lifecycle_harness, monkeypatch, entry, failure,
):
    h = lifecycle_harness
    set_flags(monkeypatch, "b")
    ss = h.seed()
    retained = ss._client
    retained.disconnect.side_effect = (
        RuntimeError("cleanup unconfirmed") if failure == "error" else asyncio.CancelledError()
    )
    try:
        await ss.attempt_reconnect()
    except asyncio.CancelledError:
        pass
    assert ss._client is retained
    # A later, ordinary disconnect must not silently forget the same debt.
    try:
        await ss.disconnect()
    except (RuntimeError, asyncio.CancelledError):
        pass
    try:
        if entry == "connect":
            await ss.connect()
        elif entry == "ensure":
            await h.app.state.broker._ensure_session_callback("sample")
        else:
            await h.client.post("/admin/force-restart-agent/sample")
    except (RuntimeError, asyncio.CancelledError):
        pass
    assert len(h.clients) == 1, "Startup discarded an unconfirmed client"
    assert ss._client is retained
    assert h.app.state.broker._streaming["sample"]["main"] is ss
    retained.disconnect.side_effect = retained.close
    await h.app.state.broker._ensure_session_callback("sample")
    assert retained.closed.is_set()
    assert ss.state == SessionState.CONNECTED


@pytest.mark.parametrize("mode", ["a", "b", "both"])
async def test_rename_invalidates_old_callback_and_serializes_with_startup(
    lifecycle_harness, monkeypatch, mode,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    h.seed()
    response = await h.client.post("/agents/sample/streaming-sessions?label=secondary")
    assert response.status_code == 200
    ss = h.app.state.broker._streaming["sample"]["secondary"]
    old_async, old_sync = ss._on_resume_handle, ss._on_resume_handle_sync
    await old_async("sample", "before")
    lifecycle = closure_value(h.app, "_lifecycle")
    async with lifecycle("sample"):
        rename = asyncio.create_task(h.client.patch(
            "/agents/sample/streaming-sessions/secondary", json={"label": "renamed"},
        ))
        await asyncio.sleep(0)
        assert not rename.done(), "Rename bypassed the lifecycle owner"
    assert (await rename).status_code == 200
    await old_async("sample", "stale-async")
    old_sync("sample", "stale-sync")
    assert not h.app.state.agents.get_streaming_session_id("sample", label="secondary")
    await ss._on_resume_handle("sample", "new-async")
    assert h.app.state.agents.get_streaming_session_id("sample", label="renamed") == "new-async"
    ss._on_resume_handle_sync("sample", "")
    assert not h.app.state.agents.get_streaming_session_id("sample", label="renamed")


@pytest.mark.parametrize("mode", ["a", "b", "both"])
async def test_registration_refuses_displacing_an_owned_transport(
    lifecycle_harness, monkeypatch, mode,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    ss = h.seed()
    from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig

    replacement = StreamingSession(StreamingSessionConfig(agent_name="sample"))
    try:
        h.app.state.broker.register_streaming("sample", replacement)
    except RuntimeError:
        pass
    assert h.app.state.broker._streaming["sample"]["main"] is ss
    assert not ss._client.closed.is_set()


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
async def test_terminal_stop_quiesces_tmux_recovery(lifecycle_harness, monkeypatch, source):
    h = lifecycle_harness
    old = h.seed((source, "tmux"))
    monkeypatch.setattr("pinky_daemon.tmux_session._RECONNECT_BACKOFF", (137.0,))
    entered, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep
    owner = []

    async def pause(delay):
        if delay == 137.0:
            owner.append(asyncio.current_task())
            entered.set()
            await release.wait()
        else:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", pause)
    task = asyncio.create_task(old.attempt_reconnect(trigger=Trigger.WATCHDOG))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await terminal_entry(h, "stop", "main")
        assert owner[0].done(), "Terminal stop left a tmux recovery owner alive"
        assert old._state_machine._in_flight is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert old.state == SessionState.DEAD


@pytest.mark.parametrize("entry", ["stop", "delete"])
async def test_terminal_teardown_waits_for_cancellation_ack(lifecycle_harness, monkeypatch, entry):
    h = lifecycle_harness
    ss = h.seed(label="secondary" if entry == "delete" else "main")
    ss._RECONNECT_BACKOFF = (137.0,)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep

    async def pause(delay):
        if delay == 137.0:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise
        else:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", pause)
    task = asyncio.create_task(ss.attempt_reconnect())
    await asyncio.wait_for(entered.wait(), 2)
    teardown = asyncio.create_task(terminal_entry(h, entry, ss._config.label))
    try:
        # Either cancellation starts or the terminal call wrongly returns.
        waiter = asyncio.create_task(cancelled.wait())
        await asyncio.wait({waiter, teardown}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert cancelled.is_set(), "Terminal sink never quiesced its recovery owner"
        assert not teardown.done(), "Teardown did not await cancellation acknowledgment"
        assert h.app.state.broker._streaming["sample"][ss._config.label] is ss
    finally:
        release.set()
        task.cancel()
        waiter.cancel()
        await asyncio.gather(task, waiter, teardown, return_exceptions=True)


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("transport", ["sdk", "tmux"])
async def test_terminal_retirement_refuses_late_direct_connect(
    lifecycle_harness, source, transport,
):
    h = lifecycle_harness
    ss = h.seed((source, transport))
    await terminal_entry(h, "stop", "main")
    before = len(h.clients), sum(event == "substrate" for event, _ in h.trace)
    try:
        await ss.connect()
    except RuntimeError:
        pass
    assert ss.state == SessionState.DEAD
    assert (len(h.clients), sum(event == "substrate" for event, _ in h.trace)) == before


@pytest.mark.parametrize("entry", ["force", "context", "model", "archive", "mcp"])
async def test_b_only_restart_failure_retains_registered_client(lifecycle_harness, monkeypatch, entry):
    h = lifecycle_harness
    set_flags(monkeypatch, "b")
    ss = h.seed()
    retained = ss._client
    retained.disconnect.side_effect = RuntimeError("cleanup unconfirmed")
    retained.get_context_usage = AsyncMock(return_value={"maxTokens": 200000})
    try:
        if entry == "force":
            await h.client.post("/admin/force-restart-agent/sample")
        elif entry == "context":
            await closure_value(h.app, "_restart_streaming_session_after_response")(
                "sample", ss, "saved", object(),
            )
        elif entry == "model":
            await h.client.post("/agents/sample/streaming/model", json={"model": "claude-sonnet-4-6"})
        elif entry == "archive":
            await h.client.post("/agents/sample/streaming/archive")
        else:
            await h.app.state.watchdog._mcp_recover_fn("sample", "main", "test")
    except RuntimeError:
        pass
    assert h.app.state.broker._streaming.get("sample", {}).get("main") is ss
    assert retained.disconnect.await_count > 0, "Restart did not exercise cleanup"
    assert ss._client is retained
    assert len(h.clients) == 1
