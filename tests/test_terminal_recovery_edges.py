"""Ordering and recovery after a refused terminal or retained transition."""

import asyncio

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
