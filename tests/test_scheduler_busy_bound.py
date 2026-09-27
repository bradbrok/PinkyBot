"""Bounded scheduler delivery uses ordinary paste and exact receipt evidence."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import codex_tmux_session, scheduler, tmux_session
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.scheduler import AgentScheduler, ScheduleWakeReceipt, _OutboxDrainExtensionState
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _QueuedTurn, _TmuxControl
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from pinky_daemon.transport_state import SessionState

NOW = 1_800_000_000.0


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=NOW, polls=0)
    for module in (scheduler, tmux_session, codex_tmux_session):
        monkeypatch.setattr(module, "time", SimpleNamespace(
            **{**vars(module.time), "time": lambda: clock.now},
        ))
    return clock


@pytest.fixture
def registry(tmp_path):
    registry = AgentRegistry(db_path=str(tmp_path / "registry.db"))
    registry.register("worker")
    yield registry
    registry.close()


def fire(registry, *, at=NOW, name="periodic", cron="0 * * * *"):
    schedule = registry.add_schedule("worker", cron, name=name, prompt=f"execute {name}")
    pending, _ = registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=name,
        prompt=schedule.prompt, fired_at=at,
    )
    return pending


def session(tmp_path, kind=TmuxSession):
    tmux = MagicMock(spec=_TmuxControl)
    tmux.session_name = "test-bound"
    ok = TmuxCommandResult(returncode=0, stdout="", stderr="")
    tmux.paste_text = AsyncMock(return_value=ok)
    tmux.kill_session = AsyncMock(return_value=ok)
    tmux.capture_pane = AsyncMock(return_value=ok)
    config = StreamingSessionConfig(agent_name="worker", working_dir=str(tmp_path))
    config.live_status_fn = lambda: {"status": "working", "last_updated": NOW}
    result = kind(config, tmux_control=tmux)
    result._state_machine._state = SessionState.CONNECTED
    result._tailer = TmuxTranscriptTailer(
        tmux_session._PLACEHOLDER_TRANSCRIPT_PATH, result._handle_turn_complete,
    )
    result._inflight_tool_calls = {"tool": {}}
    return result, tmux


@pytest.mark.parametrize("kind", [TmuxSession, CodexTmuxSession])
async def test_scheduler_turn_pastes_after_busy_starvation_bound(tmp_path, monkeypatch, clock, kind):
    ss, tmux = session(tmp_path, kind)
    turn = _QueuedTurn(prompt="owed", queued_at=NOW, scheduler_serialized=True)
    turn.scheduler_busy_deliver_at = NOW + 600

    async def advance(delay):
        assert tmux.paste_text.await_count == 0
        clock.polls += 1
        assert clock.polls <= 2, "busy scheduler must paste at its deadline"
        clock.now = NOW + (599 if clock.polls == 1 else 600)

    for module in (tmux_session, codex_tmux_session):
        monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": advance}))
    try:
        await ss._deliver_turn(turn)
        tmux.paste_text.assert_awaited_once_with("owed", enter=True)
        assert clock.now == NOW + 600
    finally:
        await ss.disconnect()


@pytest.mark.parametrize("kind", [TmuxSession, CodexTmuxSession])
def test_past_deadline_keeps_unconfirmed_paste_veto(tmp_path, monkeypatch, clock, kind):
    ss, _ = session(tmp_path, kind)
    turn = _QueuedTurn(prompt="owed", queued_at=NOW - 700, scheduler_serialized=True)
    turn.scheduler_busy_deliver_at = NOW - 100
    monkeypatch.setattr(ss, "_has_unresolved_pasted_acceptance", lambda: True)
    assert ss._scheduler_pane_busy(turn) is True
    monkeypatch.setattr(ss, "_has_unresolved_pasted_acceptance", lambda: False)
    assert ss._scheduler_pane_busy(turn) is False


async def test_never_idle_agent_recurring_fire_delivered_before_ceiling(registry, clock):
    pending = fire(registry)
    calls = []

    async def wake(_agent, _session, prompt, **kwargs):
        calls.append((clock.now, prompt, kwargs))
        return True

    engine = AgentScheduler(registry, wake_callback=wake, delivery_busy_fn=lambda _: True)
    clock.now = NOW + 599
    await engine._replay_pending_locked("worker")
    assert calls == []
    clock.now = NOW + 600
    await engine._replay_pending_locked("worker")
    assert [(at, prompt) for at, prompt, _ in calls] == [(NOW + 600, pending.prompt)]
    row = registry.get_schedule_wake_by_fire(pending.schedule_id, NOW)
    assert row.accepted_at == NOW + 600
    assert row.attempts == 1


@pytest.mark.parametrize("reason,age", [("wall-clock cap", 1800), ("attempt cap", 1500)])
def test_drain_park_spares_rows_younger_than_budget(registry, clock, reason, age):
    older = fire(registry, name="older")
    younger = fire(registry, name="younger", at=NOW + 1440)
    engine = AgentScheduler(registry)
    clock.now = NOW + age
    result = engine._drain_park_outbox_rows(
        "worker", bound_reason=reason,
        state=_OutboxDrainExtensionState(NOW, attempts=30), oldest_age=age,
    )
    assert result == (1, 1)
    assert registry.get_schedule_wake_by_fire(older.schedule_id, NOW).drain_parked_at > 0
    assert registry.get_schedule_wake_by_fire(younger.schedule_id, younger.fired_at).drain_parked_at == 0


async def test_released_row_gets_fresh_receipt_window(registry, monkeypatch, clock):
    pending = fire(registry)
    registry.drain_park_pending_schedule_wake(pending.id)
    clock.now = NOW + 3121
    registry.release_drain_parked_schedule_wakes("worker")
    observed = []

    async def confirmation(_pending, *, fresh_receipt_budget=False):
        observed.append(fresh_receipt_budget)
        return False

    engine = AgentScheduler(registry)
    monkeypatch.setattr(engine, "_wait_for_wake_confirmation", confirmation)
    await engine._replay_pending_locked("worker")
    assert observed == [True]


@pytest.mark.parametrize("commit", [False, True])
def test_restart_context_lists_owed_wakes_without_consuming(tmp_path, commit):
    from pinky_daemon.api import create_api

    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    registry = app.state.agents
    registry.register("worker")
    pending = fire(registry)
    before = registry.get_schedule_wake_by_fire(pending.schedule_id, NOW).to_dict()
    text = app.state._build_streaming_wake_context("worker", commit=commit)
    assert "periodic" in text
    assert "2027-01-15" in text or str(NOW) in text
    assert pending.prompt not in text
    assert registry.get_schedule_wake_by_fire(pending.schedule_id, NOW).to_dict() == before


async def test_pasted_then_restart_before_consumption_never_replays(registry, tmp_path, clock, monkeypatch):
    pending = fire(registry, at=NOW - 700)
    ss, _ = session(tmp_path)
    durable = ScheduleWakeReceipt(registry, pending.schedule_id, pending.fired_at)
    turn = _QueuedTurn(
        prompt=pending.prompt, queued_at=NOW - 700, scheduler_serialized=True,
        scheduler_accept=durable.accept,
        scheduler_delivery=asyncio.get_running_loop().create_future(),
    )
    # Isolate restart durability from the independently tested busy gate.
    monkeypatch.setattr(ss, "_scheduler_pane_busy", lambda candidate=None: False)
    await ss._deliver_turn(turn)
    assert not turn.scheduler_delivery.done()
    await ss.disconnect()
    calls = []

    async def wake(*args):
        calls.append(args)
        return True

    alerts = []
    restarted = AgentScheduler(
        registry, wake_callback=wake, owner_notify_callback=lambda *args: alerts.append(args),
    )
    await restarted._replay_pending_locked("worker")
    assert calls == []
    row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
    assert row.accepted_at == 0
    assert "PASTED_UNCONFIRMED_SESSION_LOST" in row.last_error
    if restarted._owner_alert_tasks:
        await asyncio.gather(*restarted._owner_alert_tasks)
    assert len(alerts) == 1
    assert "PASTED_UNCONFIRMED_SESSION_LOST" in alerts[0][1]


async def test_midturn_queue_consumed_at_boundary_receipted_once(registry, tmp_path, monkeypatch, clock):
    pending = fire(registry, at=NOW - 700)
    ss, _ = session(tmp_path)
    durable = ScheduleWakeReceipt(registry, pending.schedule_id, pending.fired_at)
    accepted = []
    real_confirm = registry.confirm_pending_schedule_wake_by_fire

    def confirm(*args, **kwargs):
        accepted.append(args)
        return real_confirm(*args, **kwargs)

    monkeypatch.setattr(registry, "confirm_pending_schedule_wake_by_fire", confirm)
    monkeypatch.setattr(ss, "_scheduler_pane_busy", lambda candidate=None: False)
    receipt = await ss.send_scheduler_prompt(pending.prompt, on_accept=durable.accept)
    await asyncio.gather(*ss._scheduler_delivery_tasks)
    try:
        row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
        assert row.accepted_at == 0, "a paste marker cannot grant acceptance"
        assert row.attempts == 0, "paste bookkeeping cannot burn replay attempts"
        assert not receipt.done()
        ss._on_transcript_entry({"type": "queue-operation", "operation": "enqueue", "content": pending.prompt})
        assert not receipt.done()
        ss._on_transcript_entry({"type": "queue-operation", "operation": "dequeue"})
        assert await receipt is True
        ss._on_transcript_entry({"type": "user", "message": {"role": "user", "content": pending.prompt}})
        assert len(accepted) == 1
        row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
        assert row.accepted_at > 0 and row.abandoned_at == 0
    finally:
        await ss.disconnect()


async def test_paste_marker_is_write_ahead(registry, tmp_path, monkeypatch, clock):
    pending = fire(registry)
    ss, tmux = session(tmp_path)
    durable = ScheduleWakeReceipt(registry, pending.schedule_id, pending.fired_at)
    monkeypatch.setattr(ss, "_scheduler_pane_busy", lambda candidate=None: False)
    seen = []

    async def paste(*args, **kwargs):
        row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
        seen.append((getattr(row, "pasted_at", 0), row.accepted_at, row.attempts))
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    tmux.paste_text.side_effect = paste
    receipt = await ss.send_scheduler_prompt(pending.prompt, on_accept=durable.accept)
    await asyncio.gather(*ss._scheduler_delivery_tasks)
    try:
        assert seen == [(NOW, 0, 0)]
        assert not receipt.done()
    finally:
        await ss.disconnect()


async def test_past_deadline_receipt_budget_starts_from_paste(registry, tmp_path, monkeypatch, clock):
    pending = fire(registry, at=NOW - 3121)
    ss, _ = session(tmp_path)
    monkeypatch.setattr(ss, "_scheduler_pane_busy", lambda candidate=None: False)
    pasted = asyncio.Event()
    budgets = []

    async def wake(_agent, _session, prompt, *, schedule_receipt, busy_deliver_at=None):
        # A pane lock delayed the actual paste by another 100 seconds.
        clock.now += 100
        receipt = await ss.send_scheduler_prompt(prompt, on_accept=schedule_receipt.accept)
        await asyncio.gather(*ss._scheduler_delivery_tasks)
        pasted.set()
        return receipt

    async def wait_for(awaitable, timeout):
        await pasted.wait()
        budgets.append(timeout)
        if len(budgets) == 1:
            # The first timer started before paste. Its timeout must re-read
            # the durable paste timestamp before declaring abandonment.
            clock.now = NOW + timeout
            raise asyncio.TimeoutError
        assert len(budgets) <= 2
        assert timeout >= NOW + 100 + 500 - clock.now, "full fresh receipt window lost"
        clock.now = NOW + 600
        ss._on_transcript_entry({"type": "user", "message": {"role": "user", "content": pending.prompt}})
        return await awaitable

    monkeypatch.setattr(scheduler, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": wait_for}))
    engine = AgentScheduler(registry, wake_callback=wake, delivery_inflight_fn=lambda *_: True)
    try:
        try:
            result = await engine._wait_for_wake_confirmation(pending, fresh_receipt_budget=True)
        except scheduler._ReceiptAbandonedError:
            result = "abandoned"
        assert result is True, "past-deadline paste lost its fresh receipt budget"
        row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
        assert row.accepted_at == NOW + 600 and row.abandoned_at == 0
        assert len(budgets) == 2
    finally:
        await ss.disconnect()
        await engine.stop()
