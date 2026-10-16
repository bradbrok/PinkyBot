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
    # A settle queued by the fixture's wake can run before the fake clock is installed.
    app.state.scheduler._lost_paste_offline_since.pop("worker", None)
    row = api_paste(app, pasted_at=NOW + 1)
    engine = app.state.scheduler
    owner, notice = AsyncMock(return_value=True), AsyncMock(return_value=True)
    engine._owner_notify_callback, engine._agent_notify_callback = owner, notice
    # The transport's replacement stand-in never enters an API endpoint.
    ss._state_machine._state = SessionState.DEAD
    engine._settle_lost_pastes("worker")
    assert engine._lost_paste_offline_since["worker"] == 10_000.0
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


@pytest.fixture
def offline_agents(rig, tmp_path):
    rows = {"worker": rig.pasted()}
    rig.registry.register("peer", working_dir=str(tmp_path / "peer"))
    schedule = rig.registry.add_schedule(
        "peer", "0 * * * *", name="peer check", prompt="check more work",
    )
    row, created = rig.registry.persist_schedule_wake(
        schedule.id, agent_name="peer", schedule_name=schedule.name,
        prompt=schedule.prompt, fired_at=FIRED,
    )
    assert created
    assert rig.registry.mark_schedule_wake_pasted(
        row.schedule_id, row.fired_at, pasted_at=NOW, session_id="old-session",
        pasted_prompt=row.prompt,
    )
    rows["peer"] = row
    engine, wake = rig.engine(live="")
    live = {"worker": "", "peer": ""}
    engine._delivery_session_fn = live.__getitem__
    return SimpleNamespace(rig=rig, rows=rows, engine=engine, live=live, wake=wake)


def agent_row(case, name):
    row = case.rows[name]
    return case.rig.registry.get_schedule_wake_by_fire(row.schedule_id, row.fired_at)


async def test_live_agent_does_not_clear_another_agents_offline_grace(offline_agents):
    case = offline_agents
    case.engine._settle_lost_pastes("worker")
    case.rig.clock.monotonic += 200
    case.live["peer"] = "old-session"
    case.engine._settle_lost_pastes("peer")
    await drain_notices(case.engine)
    assert not agent_row(case, "worker").abandoned_at
    assert not agent_row(case, "peer").abandoned_at
    assert case.rig.owners == case.rig.notices == []

    case.rig.clock.monotonic += 100
    case.engine._settle_lost_pastes("worker")
    case.engine._settle_lost_pastes("worker")
    await drain_notices(case.engine)
    assert agent_row(case, "worker").abandoned_at
    assert not agent_row(case, "worker").accepted_at
    assert not agent_row(case, "peer").abandoned_at
    assert case.rig.owners == [(
        "worker", "worker went offline before it confirmed the scheduled prompt 'periodic' "
        "(fired 2023-11-14 22:13 UTC); it may not have run.",
    )]
    assert case.rig.notices == []
    case.wake.assert_not_awaited()


async def test_offline_grace_starts_at_each_agents_first_observation(offline_agents):
    case = offline_agents
    case.engine._settle_lost_pastes("worker")
    case.rig.clock.monotonic += 200
    case.engine._settle_lost_pastes("peer")
    await drain_notices(case.engine)
    assert not agent_row(case, "worker").abandoned_at
    assert not agent_row(case, "peer").abandoned_at
    assert case.rig.owners == case.rig.notices == []

    case.rig.clock.monotonic += 100
    for name in ("worker", "peer"):
        case.engine._settle_lost_pastes(name)
    await drain_notices(case.engine)
    assert agent_row(case, "worker").abandoned_at
    assert not agent_row(case, "peer").abandoned_at
    assert [name for name, _ in case.rig.owners] == ["worker"]
    assert case.rig.notices == []

    case.rig.clock.monotonic += 200
    case.engine._settle_lost_pastes("peer")
    case.engine._settle_lost_pastes("peer")
    await drain_notices(case.engine)
    assert agent_row(case, "peer").abandoned_at
    assert not agent_row(case, "peer").accepted_at
    assert case.rig.owners == [
        ("worker", "worker went offline before it confirmed the scheduled prompt 'periodic' "
         "(fired 2023-11-14 22:13 UTC); it may not have run."),
        ("peer", "peer went offline before it confirmed the scheduled prompt 'peer check' "
         "(fired 2023-11-14 22:13 UTC); it may not have run."),
    ]
    assert case.rig.notices == []
    case.wake.assert_not_awaited()


@pytest.mark.parametrize("main_kind", [StreamingSession, TmuxSession])
async def test_sibling_context_cannot_cover_the_main_sessions_lost_paste(api_session, main_kind):
    app, original, _, _ = api_session

    def session_with_receipt(kind, rendered_at):
        session = kind(original._config)
        session._state_machine._state = SessionState.CONNECTED
        session._wake_context_delivery = WakeContextDelivery()
        snapshot = session._wake_context_delivery.snapshot()
        snapshot.rendered_at = rendered_at
        snapshot.delivered()
        return session

    main = session_with_receipt(main_kind, NOW - 1)
    sibling = session_with_receipt(TmuxSession, NOW + 1)
    # Insertion order must not let another label supply main's context.
    app.state.broker._streaming["worker"] = {"background": sibling, "main": main}
    row = api_paste(app, pasted_at=NOW)
    engine = app.state.scheduler
    owner, notice = AsyncMock(return_value=True), AsyncMock(return_value=True)
    engine._owner_notify_callback, engine._agent_notify_callback = owner, notice
    engine._settle_lost_pastes("worker")
    engine._settle_lost_pastes("worker")
    await drain_notices(engine)
    saved = app.state.agents.get_schedule_wake_by_fire(row.schedule_id, FIRED)
    assert saved.abandoned_at and not saved.accepted_at
    owner.assert_not_awaited()
    notice.assert_awaited_once()
    assert notice.await_args.args[0] == "worker"
    assert notice.await_args.args[1].startswith("Scheduled prompt 'periodic'")
    assert "was pasted into your previous session and may not have run" in notice.await_args.args[1]


async def test_failed_session_lookup_does_not_start_an_offline_grace(offline):
    case = offline

    def unavailable(_):
        raise RuntimeError("session lookup unavailable")

    case.engine._delivery_session_fn = unavailable
    case.engine._settle_lost_pastes("worker")
    case.clock.now += 301
    case.engine._settle_lost_pastes("worker")
    await drain_notices(case.engine)
    assert not saved_row(case).abandoned_at
    assert not saved_row(case).accepted_at
    assert case.rig.owners == case.rig.notices == []
    assert "worker" not in case.engine._lost_paste_offline_since
    case.wake.assert_not_awaited()
