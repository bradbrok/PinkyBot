import asyncio

import pytest

from pinky_daemon.transport_state import SessionState
from tests.recovery_test_support import lifecycle_harness as lifecycle_harness
from tests.recovery_test_support import set_flags


@pytest.mark.parametrize("mode", ["a", "b", "both", "off"])
async def test_ensure_restores_resume_authority_after_failed_restart(
    lifecycle_harness, monkeypatch, mode
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    h.seed()
    assert (await h.client.post("/agents/sample/stop")).status_code == 200
    assert (await h.client.post("/agents/sample/streaming-sessions")).status_code == 200
    ss = h.app.state.broker._streaming["sample"]["main"]
    await ss._on_resume_handle("sample", "before")
    retained = ss._client
    retained.disconnect.side_effect = RuntimeError("cleanup temporarily unconfirmed")
    response = await h.client.post("/admin/force-restart-agent/sample")
    if mode != "off":
        assert response.status_code >= 400
    retained.disconnect.side_effect = retained.close
    await h.app.state.broker._ensure_session_callback("sample")
    assert ss.state == SessionState.CONNECTED
    await ss._on_resume_handle("sample", "after")
    assert h.app.state.agents.get_streaming_session_id("sample") == "after"


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("transport", ["sdk", "tmux"])
async def test_retired_force_restart_cannot_spawn(lifecycle_harness, source, transport):
    h = lifecycle_harness
    ss = h.seed((source, transport))
    assert (await h.client.post("/agents/sample/stop")).status_code == 200
    before = len(h.clients), sum(event == "substrate" for event, _ in h.trace)
    try:
        await ss.force_restart()
    except RuntimeError:
        pass
    assert (len(h.clients), sum(event == "substrate" for event, _ in h.trace)) == before
    assert ss.state == SessionState.DEAD


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("transport", ["sdk", "tmux"])
async def test_terminal_stop_joins_internal_force_restart(lifecycle_harness, source, transport):
    h = lifecycle_harness
    ss = h.seed((source, transport))
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_start(owner):
        if owner is ss:
            entered.set()
            await release.wait()

    h.control.start_hook = pause_start
    task = asyncio.create_task(ss.force_restart())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await h.client.post("/agents/sample/stop")
        assert response.status_code == 200
        assert (await h.client.post("/agents/sample/streaming-sessions")).status_code == 200
        current = h.app.state.broker._streaming["sample"]["main"]
        assert current is not ss
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert ss.state == SessionState.DEAD, "Internal restart revived a terminally retired object"
    assert current.state == SessionState.CONNECTED


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
async def test_terminal_stop_joins_real_tmux_watchdog_restart(
    lifecycle_harness, monkeypatch, source
):
    from pinky_daemon import tmux_session
    from tests.test_tmux_session import _seed_inflight

    h = lifecycle_harness
    ss = h.seed((source, "tmux"))
    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.01)
    monkeypatch.setenv("PINKY_WATCHDOG_PANE_LIVENESS", "0")
    entered, release = asyncio.Event(), asyncio.Event()
    spawned = []

    async def pause_start(owner):
        if owner is ss:
            spawned.append(asyncio.current_task())
            entered.set()
            await release.wait()

    h.control.start_hook = pause_start
    _seed_inflight(ss, prompt="already accepted turn", meta={"chat_id": "test"})
    ss._inflight_metas[0].turn.transport_accepted = True
    ss._head_started_at = 1.0
    ss._watchdog_task = asyncio.create_task(ss._inflight_watchdog())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert ss._stats["turn_timeouts"] == 1
        assert (await h.client.post("/agents/sample/stop")).status_code == 200
        assert (await h.client.post("/agents/sample/streaming-sessions")).status_code == 200
        current = h.app.state.broker._streaming["sample"]["main"]
    finally:
        release.set()
        await asyncio.gather(*spawned, return_exceptions=True)
    assert ss.state == SessionState.DEAD, "Detached watchdog restart escaped terminal retirement"
    assert current is not ss
    assert current.state == SessionState.CONNECTED
