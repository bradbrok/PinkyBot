"""Recovery decisions remain bound to their session across lifecycle-lock waits."""

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.shared_mcp import bump_gateway_epoch, get_probe_request
from tests.test_codex_mcp_attach_loud import SENTINEL
from tests.test_codex_mcp_attach_loud import harness as _harness

harness = _harness


async def connected_session(h, monkeypatch, tmp_path, restart):
    from pinky_daemon.codex_session import CodexSession
    from pinky_daemon.streaming_session import StreamingSessionConfig
    from pinky_daemon.transport_state import SessionState, Trigger

    session = CodexSession(
        StreamingSessionConfig(
            agent_name="test-agent", working_dir=str(tmp_path), provider_url="codex_cli"
        )
    )
    boot = await session._state_machine.request_transition(SessionState.BOOTING, Trigger.BOOT)
    await session._state_machine.transition_complete(
        boot.owner_token, SessionState.CONNECTED, trigger=Trigger.BOOT_COMPLETE
    )
    monkeypatch.setattr(session, "restart_transport", restart)
    return session


def lifecycle_lock(h):
    lifecycle = inspect.getclosurevars(h.real_recover).nonlocals["_lifecycle"]
    lock_for = inspect.getclosurevars(lifecycle.__wrapped__).nonlocals["_container_lifecycle_lock"]
    return lock_for("test-agent")


async def prepare_recovery(h, monkeypatch, tmp_path, mode, restart):
    h.add(mcp_recover=mode == "legacy")
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    session = await connected_session(h, monkeypatch, tmp_path, restart)
    h.app.state.broker._streaming["test-agent"] = {"main": session}
    h.launch()
    if mode == "legacy":
        h.prove()
        await h.watchdog._sweep()
        h.clock.advance(10)
        bump_gateway_epoch()
        await h.watchdog._sweep()
        h.clock.advance(240)
    else:
        h.clock.advance(120)
        await h.watchdog._sweep()
        h.clock.advance(120)
    h.watchdog._mcp_recover_fn = h.real_recover
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["launch", "legacy"])
@pytest.mark.parametrize("change", ["bound", "new_launch", "replacement", "epoch", "unchanged"])
@pytest.mark.parametrize("guard", ["PINKY_MODEL_RUNTIME_GUARD", "PINKY_RESUME_FAILSAFE"])
async def test_recovery_rechecks_after_lifecycle_lock(
    harness, monkeypatch, tmp_path, change, guard, mode
):
    h = harness
    restart = AsyncMock()
    original = await prepare_recovery(h, monkeypatch, tmp_path, mode, restart)
    lock = lifecycle_lock(h)
    monkeypatch.setenv(guard, "1")
    await lock.acquire()
    sweep = asyncio.create_task(h.watchdog._sweep())
    try:
        await asyncio.sleep(0)
        assert lock._waiters and len(lock._waiters) == 1
        if change == "replacement":
            replacement = await connected_session(h, monkeypatch, tmp_path, restart)
            h.app.state.broker._streaming["test-agent"] = {"main": replacement}
        elif change == "bound":
            h.prove(generic=True)
        elif change == "new_launch":
            h.launch()
        elif change == "epoch":
            bump_gateway_epoch()
    finally:
        lock.release()
        await sweep
    assert restart.await_count == int(change == "unchanged")
    if change != "unchanged":
        assert h.watchdog._last_mcp_recover_at == 0
        assert original._config.force_fresh_context_once is False
        assert h.watchdog._mcp_recovery_pending is None


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["PINKY_MODEL_RUNTIME_GUARD", "PINKY_RESUME_FAILSAFE"])
async def test_under_lock_skip_releases_reservation_for_same_launch(
    harness, monkeypatch, tmp_path, guard
):
    h = harness
    restart = AsyncMock()
    await prepare_recovery(h, monkeypatch, tmp_path, "launch", restart)
    probe = get_probe_request("test-agent")
    lock = lifecycle_lock(h)
    monkeypatch.setenv(guard, "1")
    await lock.acquire()
    sweep = asyncio.create_task(h.watchdog._sweep())
    original_lookup = h.app.state.agents.get_latest_agent_heartbeat

    def unavailable(*args, **kwargs):
        raise RuntimeError(SENTINEL)

    try:
        await asyncio.sleep(0)
        assert lock._waiters and len(lock._waiters) == 1
        # Status is temporarily unverifiable only inside the locked callback.
        monkeypatch.setattr(h.app.state.agents, "get_latest_agent_heartbeat", unavailable)
    finally:
        lock.release()
        await sweep
        monkeypatch.setattr(h.app.state.agents, "get_latest_agent_heartbeat", original_lookup)
    assert restart.await_count == 0
    assert not h.watchdog._codex_mcp_states["test-agent"].retry_used
    assert h.watchdog._last_mcp_recover_at == 0
    assert h.watchdog._mcp_recovery_pending is None
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert get_probe_request("test-agent")["launch_id"] == probe["launch_id"]
    assert restart.await_count == 1


@pytest.mark.asyncio
async def test_exception_after_destructive_boundary_consumes_attempt(
    harness, monkeypatch, tmp_path
):
    h = harness
    boundary = []

    async def fail_after_start(**kwargs):
        boundary.append(
            (
                h.watchdog._codex_mcp_states["test-agent"].retry_used,
                h.watchdog._mcp_recovery_pending.started,
            )
        )
        raise RuntimeError(SENTINEL)

    restart = AsyncMock(side_effect=fail_after_start)
    original = await prepare_recovery(h, monkeypatch, tmp_path, "launch", restart)
    await h.watchdog._sweep()
    assert restart.await_count == 1
    assert boundary == [(True, True)]
    assert h.watchdog._codex_mcp_states["test-agent"].retry_used
    assert h.watchdog._last_mcp_recover_at == h.clock.now
    # The inert failed replacement cannot create a new allowance, even when
    # it remains available for a later sweep after the rate-limit expires.
    h.app.state.broker._streaming["test-agent"] = {"main": original}
    h.clock.advance(1000)
    await h.watchdog._sweep()
    assert restart.await_count == 1


@pytest.mark.asyncio
async def test_pending_reservation_blocks_concurrent_sweep(harness, monkeypatch, tmp_path):
    h = harness
    restart = AsyncMock()
    await prepare_recovery(h, monkeypatch, tmp_path, "launch", restart)
    lock = lifecycle_lock(h)
    monkeypatch.setenv("PINKY_MODEL_RUNTIME_GUARD", "1")
    await lock.acquire()
    first = asyncio.create_task(h.watchdog._sweep())
    try:
        await asyncio.sleep(0)
        assert lock._waiters and len(lock._waiters) == 1
        await h.watchdog._sweep()
        assert len(lock._waiters) == 1
        assert restart.await_count == 0
    finally:
        lock.release()
        await first
    assert restart.await_count == 1


@pytest.mark.asyncio
async def test_new_launch_does_not_inherit_prelaunch_legacy_deadline(harness):
    h = harness
    h.add(mcp_recover=True)
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    h.launch()
    h.prove()
    await h.watchdog._sweep()
    h.clock.advance(10)
    bump_gateway_epoch()
    await h.watchdog._sweep()  # legacy outage anchor at +10
    h.clock.advance(230)
    # Replace the broker object under the same agent/label between sweeps.
    h.add(mcp_recover=True)
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    h.launch()  # external replacement finishes between sweeps at +240
    h.clock.advance(10)
    await h.watchdog._sweep()  # new probe age 10, old legacy outage age 240
    assert h.recovered == [], "legacy clock restarted new launch before either notice window"


@pytest.mark.asyncio
async def test_notice_delivery_delay_gets_full_grace(harness):
    h = harness
    session = h.add()
    h.launch()
    send = session.send

    async def delayed(text):
        h.clock.advance(75)
        return await send(text)

    session.send = delayed
    h.clock.advance(120)
    await h.watchdog._sweep()
    h.clock.advance(119)
    await h.watchdog._sweep()
    assert h.recovered == []
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fulfilled", "new_launch", "epoch"])
async def test_final_check_after_alert_await(harness, change):
    h = harness
    h.add()
    h.launch()
    calls = 0

    async def alert(name, message):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "fulfilled":
                h.prove()
            elif change == "new_launch":
                h.launch()
            else:
                bump_gateway_epoch()
        return False

    h.watchdog._alert_fn = alert
    h.clock.advance(120)
    await h.watchdog._sweep()
    h.clock.advance(120)
    await h.watchdog._sweep()
    assert calls == 2
    assert h.recovered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["session_only", "probe_only"])
async def test_either_external_identity_change_resets_legacy_anchor(harness, change):
    h = harness
    h.add(mcp_recover=True)
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    h.launch()
    h.prove()
    await h.watchdog._sweep()
    h.clock.advance(10)
    bump_gateway_epoch()
    await h.watchdog._sweep()
    h.clock.advance(230)
    if change == "session_only":
        h.add(mcp_recover=True)
        h.app.state.agents.register("test-agent", heartbeat_interval=60)
    else:
        h.launch()
    h.clock.advance(10)
    await h.watchdog._sweep()
    assert h.recovered == []


@pytest.mark.asyncio
async def test_epoch_change_without_replacement_keeps_legacy_outage_clock(harness):
    h = harness
    h.add(mcp_recover=True)
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    h.launch()
    h.prove()
    await h.watchdog._sweep()
    h.clock.advance(10)
    bump_gateway_epoch()
    await h.watchdog._sweep()
    h.clock.advance(120)
    bump_gateway_epoch()
    await h.watchdog._sweep()
    h.clock.advance(119)
    await h.watchdog._sweep()
    assert h.recovered == []
    h.clock.advance(1)
    await h.watchdog._sweep()
    assert len(h.recovered) == 1
