"""Suspended restart boundaries and retained resume callback authority."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.transport_state import SessionState
from tests.recovery_test_support import lifecycle_harness as lifecycle_harness
from tests.recovery_test_support import set_flags


@pytest.mark.parametrize("mode", ["a", "b", "both"])
@pytest.mark.parametrize("source,transport", [
    ("claude_sdk", "sdk"), ("codex_cli", "sdk"),
    ("claude_sdk", "tmux"), ("codex_cli", "tmux"),
])
@pytest.mark.parametrize("phase", ["cleanup", "spawn"])
async def test_retirement_awaits_suspended_internal_restart(
    lifecycle_harness, monkeypatch, mode, source, transport, phase,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    ss = h.seed((source, transport))
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def boundary(*args):
        calls.append(asyncio.current_task())
        if len(calls) == 1:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                # A substrate can acknowledge cancellation only after finishing
                # its own cleanup. Retirement must wait even if it suppresses it.
                await release.wait()
        return SimpleNamespace(ok=True)

    if phase == "spawn":
        async def spawn(owner):
            if owner is ss:
                await boundary()
        h.control.start_hook = spawn
    elif transport == "tmux":
        ss._tmux.kill_session.side_effect = boundary
    elif source == "claude_sdk":
        ss._client.disconnect.side_effect = boundary
    else:
        ss._app_client = SimpleNamespace(close=AsyncMock(side_effect=boundary))

    caller = asyncio.create_task(ss.force_restart())
    stop = waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        owner = calls[0]
        stop = asyncio.create_task(h.client.post("/agents/sample/stop"))
        waiter = asyncio.create_task(cancelled.wait())
        await asyncio.wait({stop, waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert cancelled.is_set(), "Terminal path ignored the suspended restart"
        assert owner is not caller, "Restart borrowed an arbitrary caller task"
        assert not stop.done(), "Terminal publication preceded cancellation settlement"
        assert h.app.state.broker._streaming["sample"]["main"] is ss
        before = len(h.clients), len(h.trace)
        with pytest.raises(RuntimeError):
            await ss.force_restart()
        assert (len(h.clients), len(h.trace)) == before
        release.set()
        assert (await stop).status_code == 200
        assert await caller is False
        assert not caller.cancelled()
        assert ss.state == SessionState.DEAD
        assert ss._state_machine._in_flight is None
        if phase == "cleanup":
            assert not any(event == "substrate" for event, _ in h.trace)
            assert len(h.clients) == (1 if source == "claude_sdk" and transport == "sdk" else 0)
        assert (await h.client.post("/agents/sample/streaming-sessions")).status_code == 200
        current = h.app.state.broker._streaming["sample"]["main"]
        after = len(calls), len(h.trace)
        await asyncio.sleep(0)
        assert (len(calls), len(h.trace)) == after
        assert current is not ss and current.state == SessionState.CONNECTED
        assert ss.state == SessionState.DEAD
    finally:
        release.set()
        if waiter:
            waiter.cancel()
        await asyncio.gather(*(t for t in (caller, stop, waiter) if t), return_exceptions=True)


@pytest.mark.parametrize("mode", ["a", "b", "both"])
async def test_real_sdk_context_restart_is_owned(lifecycle_harness, monkeypatch, mode):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    ss = h.seed()
    ss._context_warned = True
    ss._client.get_context_usage = AsyncMock(return_value={"totalTokens": 990, "maxTokens": 1000})
    entered, release = asyncio.Event(), asyncio.Event()
    operations = []

    async def spawn(owner):
        if owner is ss:
            operations.append(asyncio.current_task())
            entered.set()
            await release.wait()

    h.control.start_hook = spawn
    trigger = asyncio.create_task(ss._check_context())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await h.client.post("/agents/sample/stop")).status_code == 200
        release.set()
        await trigger
        assert operations[0] is not trigger
        assert not trigger.cancelled()
        assert ss.state == SessionState.DEAD
        assert ss._state_machine._in_flight is None
        assert ss._stats["auto_restarts"] == 0
    finally:
        release.set()
        await asyncio.gather(trigger, return_exceptions=True)


@pytest.mark.parametrize("mode", ["a", "b", "both"])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("entry", ["ensure", "force"])
async def test_ensure_binds_both_callbacks_before_startup(
    lifecycle_harness, monkeypatch, mode, failed, entry,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    h.seed()
    assert (await h.client.post("/agents/sample/stop")).status_code == 200
    assert (await h.client.post("/agents/sample/streaming-sessions")).status_code == 200
    ss = h.app.state.broker._streaming["sample"]["main"]
    stale_async, stale_sync = ss._on_resume_handle, ss._on_resume_handle_sync
    peer = ss._client
    if failed:
        peer.disconnect.side_effect = RuntimeError("cleanup unconfirmed")
        assert (await h.client.post("/admin/force-restart-agent/sample")).status_code >= 400
        peer.disconnect.side_effect = peer.close
    else:
        await ss.disconnect()

    seen = []

    async def during_start(owner):
        assert owner is ss
        await ss._on_resume_handle("sample", "during-start")
        seen.append(h.app.state.agents.get_streaming_session_id("sample"))
        await stale_async("sample", "stale")
        stale_sync("sample", "")
        seen.append(h.app.state.agents.get_streaming_session_id("sample"))
        ss._on_resume_handle_sync("sample", "")
        seen.append(h.app.state.agents.get_streaming_session_id("sample"))
        await ss._on_resume_handle("sample", "current")

    h.control.start_hook = during_start
    if entry == "ensure":
        await h.app.state.broker._ensure_session_callback("sample")
    else:
        assert (await h.client.post("/admin/force-restart-agent/sample")).status_code == 200
    assert seen == ["during-start", "during-start", ""]
    assert h.app.state.agents.get_streaming_session_id("sample") == "current"
    assert ss.state == SessionState.CONNECTED
    assert getattr(ss, "_replacement_connect_owner", None) is None


@pytest.mark.parametrize("source,transport", [
    ("claude_sdk", "sdk"), ("codex_cli", "sdk"),
    ("claude_sdk", "tmux"), ("codex_cli", "tmux"),
])
async def test_internal_restart_generation_loss_stops_publication(
    lifecycle_harness, source, transport,
):
    h = lifecycle_harness
    ss = h.seed((source, transport))
    entered, release = asyncio.Event(), asyncio.Event()
    publications = []
    analytics = getattr(ss, "_analytics_session_started", lambda: None)

    def observe_publication():
        publications.append(ss.state)
        analytics()

    ss._analytics_session_started = observe_publication

    async def spawn(owner):
        if owner is ss:
            entered.set()
            await release.wait()

    h.control.start_hook = spawn
    task = asyncio.create_task(ss.force_restart())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # Supersede authority without inhibition so the generation check itself
        # must reject this completion, independently of terminal entry guards.
        ss._recovery_generation = getattr(ss, "_recovery_generation", 0) + 1
        release.set()
        assert await task is False
        assert publications == [], "Stale startup published before final settlement"
        assert ss.state == SessionState.DEAD
        assert ss._state_machine._in_flight is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("source,transport", [
    ("claude_sdk", "sdk"), ("codex_cli", "sdk"),
    ("claude_sdk", "tmux"), ("codex_cli", "tmux"),
])
async def test_internal_restart_waiter_cancellation_does_not_cancel_owner(
    lifecycle_harness, source, transport,
):
    h = lifecycle_harness
    ss = h.seed((source, transport))
    entered, release = asyncio.Event(), asyncio.Event()
    owners = []

    async def spawn(owner):
        if owner is ss:
            owners.append(asyncio.current_task())
            entered.set()
            await release.wait()

    h.control.start_hook = spawn
    waiter = asyncio.create_task(ss.force_restart())
    observer = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert not owners[0].done(), "Cancelling a waiter cancelled transport recovery"
        observer = asyncio.create_task(ss.force_restart())
        await asyncio.sleep(0)
        assert len(owners) == 1
        release.set()
        assert await observer is True
        assert len(owners) == 1
        assert ss.state == SessionState.CONNECTED
        assert ss._state_machine._in_flight is None
    finally:
        release.set()
        await asyncio.gather(*(t for t in (waiter, observer) if t), return_exceptions=True)


@pytest.mark.parametrize("mode", ["a", "b", "both", "off"])
@pytest.mark.parametrize("source,transport", [
    ("claude_sdk", "sdk"), ("codex_cli", "sdk"),
    ("claude_sdk", "tmux"), ("codex_cli", "tmux"),
])
async def test_blocked_internal_restart_preserves_connected_owner(
    lifecycle_harness, monkeypatch, mode, source, transport,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    ss = h.seed((source, transport))
    ss._has_completed_turn = True
    ss._config.restart_guard = lambda _: {"restart_safe": False}
    before = len(h.clients), len(h.trace)
    assert await ss.force_restart() is False
    assert ss.state == SessionState.CONNECTED
    assert ss._state_machine._in_flight is None
    assert (len(h.clients), len(h.trace)) == before


@pytest.mark.parametrize("source,transport", [
    ("claude_sdk", "sdk"), ("codex_cli", "sdk"),
    ("claude_sdk", "tmux"), ("codex_cli", "tmux"),
])
async def test_terminal_refuses_internal_restart_that_has_not_settled(
    lifecycle_harness, monkeypatch, source, transport,
):
    h = lifecycle_harness
    ss = h.seed((source, transport))
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_wait = asyncio.wait

    async def bounded_wait(tasks, *, timeout=None, **kwargs):
        if timeout == 5:
            timeout = 0.02
        return await real_wait(tasks, timeout=timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait", bounded_wait)

    async def spawn(owner):
        if owner is ss:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

    h.control.start_hook = spawn
    task = asyncio.create_task(ss.force_restart())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        try:
            response = await h.client.post("/agents/sample/stop")
            assert response.status_code >= 400, "Terminal stop ignored unsettled restart"
        except TimeoutError:
            pass
        assert cancelled.is_set()
        assert h.app.state.broker._streaming["sample"]["main"] is ss
        assert not getattr(ss, "_recovery_retired", False)
        with pytest.raises(RuntimeError):
            await ss.connect()
        release.set()
        assert await task is False
        assert (await h.client.post("/agents/sample/stop")).status_code == 200
        assert ss.state == SessionState.DEAD
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("self_handle", [False, True])
async def test_terminal_never_cancels_unowned_internal_restart_handle(
    lifecycle_harness, self_handle,
):
    h = lifecycle_harness
    ss = h.seed()
    handle = asyncio.current_task() if self_handle else asyncio.create_task(asyncio.sleep(30))
    ss._force_restart_task = handle
    try:
        with pytest.raises(RuntimeError):
            await ss.retire_transport()
        assert not handle.cancelling()
        assert ss.state == SessionState.CONNECTED
        assert h.app.state.broker._streaming["sample"]["main"] is ss
    finally:
        ss._force_restart_task = None
        if not self_handle:
            handle.cancel()
            await asyncio.gather(handle, return_exceptions=True)


@pytest.mark.parametrize("source,transport", [
    ("claude_sdk", "sdk"), ("codex_cli", "sdk"),
    ("claude_sdk", "tmux"), ("codex_cli", "tmux"),
])
async def test_internal_restart_does_not_settle_another_transition(
    lifecycle_harness, source, transport,
):
    from pinky_daemon.transport_state import Trigger

    h = lifecycle_harness
    ss = h.seed((source, transport))
    result = await ss._state_machine.request_transition(
        SessionState.RECONNECTING, Trigger.USER_AGENT, reason="existing owner",
    )
    before = len(h.clients), len(h.trace)
    try:
        assert await ss.force_restart() is False
        assert ss._state_machine._in_flight.owner_token == result.owner_token
        assert (len(h.clients), len(h.trace)) == before
    finally:
        await ss._state_machine.transition_complete(result.owner_token, SessionState.DEAD)


@pytest.mark.parametrize("source,transport", [
    ("claude_sdk", "sdk"), ("codex_cli", "sdk"),
    ("claude_sdk", "tmux"), ("codex_cli", "tmux"),
])
async def test_reconnect_joins_active_internal_restart(lifecycle_harness, source, transport):
    h = lifecycle_harness
    ss = h.seed((source, transport))
    ss._RECONNECT_BACKOFF = (0,)
    entered, duplicate, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts = []

    async def spawn(owner):
        if owner is ss:
            starts.append(asyncio.current_task())
            if len(starts) > 1:
                duplicate.set()
            entered.set()
            await release.wait()

    h.control.start_hook = spawn
    force = asyncio.create_task(ss.force_restart())
    reconnect = observer = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        reconnect = asyncio.create_task(ss.attempt_reconnect())
        observer = asyncio.create_task(duplicate.wait())
        await asyncio.wait({reconnect, observer}, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
        assert not duplicate.is_set(), "Backoff reconnect started a second substrate"
        assert not reconnect.done(), "Reconnect did not await the existing owner"
        release.set()
        assert await force is True
        await reconnect
        assert len(starts) == 1
        assert ss.state == SessionState.CONNECTED
        assert ss._state_machine._in_flight is None
    finally:
        release.set()
        if observer:
            observer.cancel()
        await asyncio.gather(*(t for t in (force, reconnect, observer) if t), return_exceptions=True)


@pytest.mark.parametrize("source,transport", [
    ("claude_sdk", "sdk"), ("codex_cli", "sdk"),
    ("claude_sdk", "tmux"), ("codex_cli", "tmux"),
])
async def test_internal_restart_refuses_unconfirmed_cleanup(lifecycle_harness, source, transport):
    h = lifecycle_harness
    ss = h.seed((source, transport))

    def refuse():
        raise RuntimeError("cleanup unconfirmed")

    peer = getattr(ss, "_client", None)
    proc = None
    if transport == "tmux":
        ss._tmux.kill_session.side_effect = refuse
    elif source == "claude_sdk":
        peer.disconnect.side_effect = refuse
    else:
        proc = SimpleNamespace(returncode=None, kill=refuse, wait=AsyncMock())
        ss._app_proc = proc
    before = len(h.clients), sum(event == "substrate" for event, _ in h.trace)
    try:
        assert await ss.force_restart() is False
        assert (len(h.clients), sum(event == "substrate" for event, _ in h.trace)) == before
        assert ss.state == SessionState.DEAD
        assert ss._state_machine._in_flight is None
    finally:
        if transport == "tmux":
            ss._tmux.kill_session.side_effect = None
        elif peer:
            peer.disconnect.side_effect = peer.close
        if proc:
            proc.returncode = 0
