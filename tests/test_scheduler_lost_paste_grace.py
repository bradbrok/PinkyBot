"""Lost paste notices tolerate short offline gaps across every transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import scheduler
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSession
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState
from pinky_daemon.wake_context import WakeContextDelivery
from tests.test_scheduler_lost_paste_alerts import FIRED, NOW
from tests.test_scheduler_lost_paste_alerts import api_session as api_session
from tests.test_scheduler_lost_paste_alerts import rig as rig
from tests.test_scheduler_lost_paste_recovery import api_paste, drain_notices


@pytest.mark.parametrize("kind", [StreamingSession, CodexSession, TmuxSession, CodexTmuxSession])
async def test_only_tmux_family_can_cover_a_lost_paste(api_session, kind):
    app, original, _, _ = api_session
    ss = kind(original._config)
    ss._state_machine._state = SessionState.CONNECTED
    # Even a stale claimed receipt on another transport cannot cover a paste.
    ss._wake_context_delivery = WakeContextDelivery()
    snapshot = ss._wake_context_delivery.snapshot()
    snapshot.rendered_at = NOW + 1
    snapshot.delivered()
    app.state.broker._streaming["worker"]["main"] = ss
    row = api_paste(app, pasted_at=NOW)
    engine = app.state.scheduler
    owner, notice = AsyncMock(return_value=True), AsyncMock(return_value=True)
    engine._owner_notify_callback, engine._agent_notify_callback = owner, notice
    is_tmux = issubclass(kind, TmuxSession)
    assert engine._delivery_context_rendered_at_fn("worker") == (NOW + 1 if is_tmux else 0)
    engine._settle_lost_pastes("worker")
    await drain_notices(engine)
    saved = app.state.agents.get_schedule_wake_by_fire(row.schedule_id, FIRED)
    assert saved.abandoned_at and not saved.accepted_at
    owner.assert_not_awaited()
    assert notice.await_count == (0 if is_tmux else 1)


@pytest.fixture
def offline(rig, monkeypatch):
    clock = SimpleNamespace(now=10_000.0)
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(
        **{**vars(scheduler.time), "monotonic": lambda: clock.now},
    ))
    row = rig.pasted()
    engine, wake = rig.engine(live="", rendered=0)
    live = SimpleNamespace(session="")
    engine._delivery_session_fn = lambda _: live.session
    return SimpleNamespace(rig=rig, clock=clock, row=row, engine=engine, live=live, wake=wake)


def saved_row(case):
    return case.rig.registry.get_schedule_wake_by_fire(case.row.schedule_id, FIRED)


async def observe(case, path="direct"):
    if path == "direct":
        case.engine._settle_lost_pastes("worker")
    elif path == "pending":
        await case.engine._replay_pending_locked("worker")
    else:
        assert case.engine._run_outbox_reaper_if_due(case.rig.clock.now)
    await drain_notices(case.engine)


@pytest.mark.parametrize("path", ["direct", "pending", "reaper"])
async def test_offline_grace_defers_all_settlement_paths(offline, path):
    case = offline
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.clock.now += 299.999
    await observe(case, path)
    assert not saved_row(case).abandoned_at
    assert case.rig.owners == case.rig.notices == []
    case.wake.assert_not_awaited()


async def test_offline_grace_expires_at_exactly_300_seconds(offline):
    case = offline
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.clock.now += 299.999
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.clock.now += 0.001
    await observe(case)
    await observe(case)
    assert saved_row(case).abandoned_at and not saved_row(case).accepted_at
    assert len(case.rig.owners) == 1 and case.rig.notices == []
    assert "went offline before it confirmed the scheduled prompt" in case.rig.owners[0][1]
    case.wake.assert_not_awaited()


@pytest.mark.parametrize("covered", [False, True])
async def test_short_planned_gap_uses_live_audience_after_recovery(offline, covered):
    case = offline
    case.engine._delivery_context_rendered_at_fn = lambda _: NOW if covered else 0
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.clock.now += 299
    case.live.session = "replacement"
    await observe(case)
    assert saved_row(case).abandoned_at
    assert case.rig.owners == []
    assert len(case.rig.notices) == (0 if covered else 1)
    case.wake.assert_not_awaited()


async def test_live_observation_restarts_the_offline_grace(offline):
    case = offline
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.clock.now += 299
    case.live.session = "old-session"
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.live.session = ""
    case.clock.now += 1
    await observe(case)
    case.clock.now += 299.999
    await observe(case)
    assert not saved_row(case).abandoned_at
    assert case.rig.owners == case.rig.notices == []
    case.clock.now += 0.001
    await observe(case)
    assert saved_row(case).abandoned_at and len(case.rig.owners) == 1


async def test_offline_grace_uses_monotonic_observations(offline):
    case = offline
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.rig.clock.now += 86_400
    case.clock.now += 299
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.rig.clock.now -= 172_800
    case.clock.now += 1
    await observe(case)
    assert saved_row(case).abandoned_at and len(case.rig.owners) == 1


async def test_daily_reaper_uses_an_existing_offline_observation(offline):
    case = offline
    await observe(case)
    assert not saved_row(case).abandoned_at
    case.clock.now += 300
    case.rig.clock.now += 86_400
    await observe(case, "reaper")
    assert saved_row(case).abandoned_at and len(case.rig.owners) == 1
    assert case.rig.notices == []
    case.wake.assert_not_awaited()


async def test_transport_owned_restart_gap_uses_the_same_grace(api_session, monkeypatch):
    app, ss, _, _ = api_session
    clock = SimpleNamespace(now=10_000.0)
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(
        **{**vars(scheduler.time), "monotonic": lambda: clock.now},
    ))
    row = api_paste(app, pasted_at=NOW + 1)
    engine = app.state.scheduler
    owner, notice = AsyncMock(return_value=True), AsyncMock(return_value=True)
    engine._owner_notify_callback, engine._agent_notify_callback = owner, notice
    # The transport's replacement stand-in never enters an API endpoint.
    ss._state_machine._state = SessionState.DEAD
    engine._settle_lost_pastes("worker")
    clock.now += 299
    engine._settle_lost_pastes("worker")
    await drain_notices(engine)
    assert not app.state.agents.get_schedule_wake_by_fire(row.schedule_id, FIRED).abandoned_at
    owner.assert_not_awaited()
    notice.assert_not_awaited()
    ss._state_machine._state = SessionState.CONNECTED
    engine._settle_lost_pastes("worker")
    await drain_notices(engine)
    owner.assert_not_awaited()
    notice.assert_awaited_once()
