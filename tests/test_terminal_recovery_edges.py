"""Ordering and recovery after a refused terminal or retained transition."""

import asyncio

import pytest

from pinky_daemon.transport_state import SessionState
from tests.recovery_test_support import closure_value, set_flags
from tests.recovery_test_support import lifecycle_harness as lifecycle_harness


async def test_b_only_ensure_restores_recovery_after_failed_restart(lifecycle_harness, monkeypatch):
    h = lifecycle_harness
    set_flags(monkeypatch, "b")
    ss = h.seed()
    client = ss._client
    client.disconnect.side_effect = RuntimeError("unconfirmed cleanup")
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code >= 400
    assert ss._client is client
    client.disconnect.side_effect = client.close
    await h.app.state.broker._ensure_session_callback("sample")
    ss._RECONNECT_BACKOFF = (0,)
    await ss.attempt_reconnect()
    assert ss.state == SessionState.CONNECTED


async def test_b_only_rename_handler_waits_for_lifecycle_owner(lifecycle_harness, monkeypatch):
    h = lifecycle_harness
    set_flags(monkeypatch, "b")
    ss = h.seed(label="secondary")
    endpoint = next(r.endpoint for r in h.app.routes if
                    getattr(r, "path", "") == "/agents/{name}/streaming-sessions/{label}"
                    and "PATCH" in getattr(r, "methods", set()))
    async with closure_value(h.app, "_lifecycle")("sample"):
        task = asyncio.create_task(endpoint("sample", "secondary", {"label": "renamed"}))
        await asyncio.sleep(0)
        waiting = not task.done()
        label = ss._config.label
    await task
    assert waiting, "Handler bypassed the lifecycle lock"
    assert label == "secondary"
    assert ss._config.label == "renamed"


async def test_direct_connect_refuses_while_terminal_owner_is_quiescing(
    lifecycle_harness, monkeypatch,
):
    h = lifecycle_harness
    ss = h.seed()
    # A prior retained restart must release its task-scoped connect permit.
    await ss.restart_transport()
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
    stop = asyncio.create_task(h.client.post("/agents/sample/stop"))
    waiter = asyncio.create_task(cancelled.wait())
    try:
        await asyncio.wait({stop, waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert cancelled.is_set(), "Stop did not quiesce recovery"
        before = len(h.clients)
        try:
            await ss.connect()
        except RuntimeError:
            pass
        assert len(h.clients) == before, "Direct startup raced terminal quiescence"
    finally:
        release.set()
        task.cancel()
        waiter.cancel()
        await asyncio.gather(task, stop, waiter, return_exceptions=True)


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("transport", ["sdk", "tmux"])
async def test_terminal_retirement_also_refuses_stale_retained_restart(
    lifecycle_harness, source, transport,
):
    h = lifecycle_harness
    ss = h.seed((source, transport))
    response = await h.client.post("/agents/sample/stop")
    assert response.status_code == 200
    before = len(h.clients), sum(event == "substrate" for event, _ in h.trace)
    try:
        await ss.restart_transport()
    except RuntimeError:
        pass
    assert ss.state == SessionState.DEAD
    assert (len(h.clients), sum(event == "substrate" for event, _ in h.trace)) == before


async def test_retained_cleanup_obeys_the_existing_startup_deadline(lifecycle_harness, monkeypatch):
    h = lifecycle_harness
    set_flags(monkeypatch, "b")
    ss = h.seed()
    client = ss._client

    async def stuck_cleanup():
        await asyncio.Event().wait()

    client.disconnect.side_effect = stuck_cleanup
    ss._startup_deadline = asyncio.get_running_loop().time() + 0.02
    connect = asyncio.create_task(ss.connect())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(connect, 1)
        assert not connect.cancelled(), "Outer test timeout stopped an unbounded cleanup"
        assert ss._client is client
        assert len(h.clients) == 1
    finally:
        client.disconnect.side_effect = client.close


@pytest.mark.parametrize("mode", ["a", "b", "both"])
async def test_rename_refuses_destination_with_unconfirmed_startup_cleanup(
    lifecycle_harness, monkeypatch, mode,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    h.seed()
    assert (await h.client.post("/agents/sample/streaming-sessions?label=secondary")).status_code == 200
    ss = h.app.state.broker._streaming["sample"]["secondary"]
    h.control.start_error = RuntimeError("initialize failed")
    h.control.cleanup_error = RuntimeError("cleanup unconfirmed")
    response = await h.client.post("/agents/sample/streaming-sessions?label=destination")
    assert response.status_code >= 400
    debt_client = h.clients[-1]
    h.control.start_error = h.control.cleanup_error = None
    try:
        response = await h.client.patch(
            "/agents/sample/streaming-sessions/secondary", json={"label": "destination"},
        )
        assert response.status_code >= 400, "Rename published over destination cleanup debt"
    except RuntimeError:
        pass
    assert h.app.state.broker._streaming["sample"]["secondary"] is ss
    assert "destination" not in h.app.state.broker._streaming["sample"]
    assert not debt_client.closed.is_set()
    debt_client.disconnect.side_effect = debt_client.close
    response = await h.client.patch(
        "/agents/sample/streaming-sessions/secondary", json={"label": "destination"},
    )
    assert response.status_code == 200
    assert debt_client.closed.is_set()
    await ss._on_resume_handle("sample", "new-handle")
    assert h.app.state.agents.get_streaming_session_id("sample", label="destination") == "new-handle"
