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
async def test_ensure_binds_both_callbacks_before_startup(
    lifecycle_harness, monkeypatch, mode, failed,
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
    await h.app.state.broker._ensure_session_callback("sample")
    assert seen == ["during-start", "during-start", ""]
    assert h.app.state.agents.get_streaming_session_id("sample") == "current"
    assert ss.state == SessionState.CONNECTED
    assert getattr(ss, "_replacement_connect_owner", None) is None
