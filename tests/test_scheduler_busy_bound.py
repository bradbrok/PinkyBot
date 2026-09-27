"""Bounded scheduler delivery uses ordinary paste and exact receipt evidence."""

import asyncio
import inspect
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
    entered = asyncio.Event()
    receipt = asyncio.get_running_loop().create_future()
    durable = ScheduleWakeReceipt(registry, pending.schedule_id, pending.fired_at)
    budgets = []

    async def wake(*args):
        entered.set()
        return receipt

    async def wait_for(awaitable, timeout):
        await entered.wait()
        budgets.append(timeout)
        assert timeout >= 500, "released first delivery needs a complete fresh receipt window"
        clock.now += 500
        assert durable.accept()
        receipt.set_result(True)
        return await awaitable

    monkeypatch.setattr(scheduler, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": wait_for}))
    engine = AgentScheduler(registry, wake_callback=wake)
    await engine._replay_pending_locked("worker")
    row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
    assert row.accepted_at == NOW + 3621 and row.abandoned_at == 0
    assert budgets == [600]


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


@pytest.mark.parametrize("released", [False, True])
async def test_past_deadline_receipt_budget_starts_from_paste(registry, tmp_path, monkeypatch, clock, released):
    pending = fire(registry, at=NOW - 3121)
    if released:
        registry.drain_park_pending_schedule_wake(pending.id)
        registry.release_drain_parked_schedule_wakes("worker")
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
            if released:
                await engine._replay_pending_locked("worker")
                result = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at).accepted_at > 0
            else:
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


@pytest.mark.parametrize("configured,cron,ceiling,expected", [
    (600, "0 * * * *", 3600, 600),
    (10000, "0 * * * *", 1200, 600),
    (600, "* * * * *", 3600, 30),
    (17, "0 * * * *", 3600, 17),
])
async def test_scheduler_handoff_carries_clamped_absolute_deadline(registry, clock, configured, cron, ceiling, expected):
    pending = fire(registry, cron=cron)
    captured = []

    async def wake(_agent, _session, _prompt, *, busy_deliver_at=None):
        captured.append(busy_deliver_at)
        return True

    registry.set_setting("SCHEDULER_BUSY_DELIVER_AFTER_S", str(configured))
    engine = AgentScheduler(registry, wake_callback=wake, receipt_extension_max_age_sec=ceiling)
    assert await engine._wake_and_confirm(pending) is True
    assert captured == [NOW + expected]


@pytest.mark.parametrize("value,expected", [("25", 25), ("nan", 600), ("inf", 600), ("-1", 600), ("0", 600), ("bad", 600)])
async def test_busy_delay_environment_is_finite_positive(registry, monkeypatch, value, expected):
    monkeypatch.setenv("SCHEDULER_BUSY_DELIVER_AFTER_S", value)
    pending = fire(registry)
    captured = []

    async def wake(_agent, _session, _prompt, *, busy_deliver_at=None):
        captured.append(busy_deliver_at)
        return True

    engine = AgentScheduler(registry, wake_callback=wake)
    assert await engine._wake_and_confirm(pending) is True
    assert captured == [NOW + expected]


@pytest.mark.parametrize("kind", [TmuxSession, CodexTmuxSession])
async def test_in_lock_recheck_retains_unconfirmed_paste_veto(tmp_path, monkeypatch, clock, kind):
    ss, tmux = session(tmp_path, kind)
    turn = _QueuedTurn(prompt="owed", queued_at=NOW - 700, scheduler_serialized=True)
    turn.scheduler_busy_deliver_at = NOW - 100
    blocked = [False]
    waits = []
    monkeypatch.setattr(ss, "_has_unresolved_pasted_acceptance", lambda: blocked[0])

    async def gate(candidate):
        waits.append(candidate)
        if len(waits) == 1:
            blocked[0] = True  # A different turn won the lock after the outer gate.
        else:
            assert len(waits) == 2
            assert tmux.paste_text.await_count == 0
            blocked[0] = False

    monkeypatch.setattr(ss, "_wait_for_scheduler_delivery_slot", gate)
    try:
        await ss._deliver_turn(turn)
        assert len(waits) == 2
        tmux.paste_text.assert_awaited_once()
    finally:
        await ss.disconnect()


@pytest.mark.parametrize("rebuild", [False, True])
async def test_api_routes_preserve_busy_deadline(tmp_path, monkeypatch, clock, rebuild):
    from pinky_daemon.api import create_api

    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", str(int(rebuild)))
    monkeypatch.setenv("PINKY_STREAMING_TRANSPORT", "tmux")
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    registry = app.state.agents
    registry.register("worker", working_dir=str(tmp_path), transport="tmux")
    ss, _ = session(tmp_path)
    received = []

    async def sender(prompt, *, on_accept=None, busy_deliver_at=None):
        received.append((prompt, busy_deliver_at, on_accept))
        return True

    ss.send_scheduler_prompt = sender
    app.state.broker._streaming["worker"] = {"main": ss}
    pending = fire(registry)
    durable = ScheduleWakeReceipt(registry, pending.schedule_id, pending.fired_at)
    try:
        callback = app.state.scheduler._wake_callback
        kwargs = {"schedule_receipt": durable}
        if "busy_deliver_at" in inspect.signature(callback).parameters:
            kwargs["busy_deliver_at"] = NOW + 600
        assert await callback("worker", "worker-main", pending.prompt, **kwargs) is True
        assert received == [(pending.prompt, NOW + 600, durable.accept)]
    finally:
        await ss.disconnect()
        registry.close()


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("settled", [False, True])
async def test_restart_context_warns_unconsumed_paste_read_only(tmp_path, clock, commit, settled):
    from pinky_daemon.api import create_api

    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    registry = app.state.agents
    registry.register("worker")
    pending = fire(registry)
    assert callable(getattr(registry, "mark_schedule_wake_pasted", None)), "missing write-ahead paste marker"
    assert registry.mark_schedule_wake_pasted(
        pending.schedule_id, pending.fired_at, pasted_at=NOW, session_id="old-session",
    )
    if settled:
        await app.state.scheduler._replay_pending_locked("worker")
    before = registry.get_schedule_wake_by_fire(pending.schedule_id, NOW).to_dict()
    text = app.state._build_streaming_wake_context("worker", commit=commit)
    assert "periodic" in text and str(NOW) in text
    assert "pasted before the restart, may not have run; check before redoing" in text
    assert pending.prompt not in text
    assert registry.get_schedule_wake_by_fire(pending.schedule_id, NOW).to_dict() == before
    await app.state.scheduler.stop()
    registry.close()


async def test_live_paste_survives_reaper_and_replay(registry, clock):
    pending = fire(registry, at=NOW - 3500)
    assert callable(getattr(registry, "mark_schedule_wake_pasted", None)), "missing write-ahead paste marker"
    assert registry.mark_schedule_wake_pasted(
        pending.schedule_id, pending.fired_at, pasted_at=NOW, session_id="live-session",
    )
    calls = []

    async def wake(*args):
        calls.append(args)
        return True

    engine = AgentScheduler(registry, wake_callback=wake, delivery_session_fn=lambda _: "live-session")
    clock.now += 200
    engine._run_outbox_reaper_if_due(clock.now)
    await engine._replay_pending_locked("worker")
    row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
    assert row.accepted_at == row.abandoned_at == row.parked_at == 0
    assert row.attempts == 0
    assert calls == []


async def test_write_ahead_crash_cannot_replay_or_accept(registry, clock):
    pending = fire(registry)
    assert callable(getattr(registry, "mark_schedule_wake_pasted", None)), "missing write-ahead paste marker"
    assert registry.mark_schedule_wake_pasted(
        pending.schedule_id, pending.fired_at, pasted_at=NOW, session_id="lost-before-handoff",
    )
    assert not registry.mark_schedule_wake_pasted(
        pending.schedule_id, pending.fired_at, pasted_at=NOW + 1, session_id="replacement",
    )
    calls = []
    alerts = []

    async def wake(*args):
        calls.append(args)
        return True

    engine = AgentScheduler(registry, wake_callback=wake, owner_notify_callback=lambda *args: alerts.append(args))
    await engine._replay_pending_locked("worker")
    await engine._replay_pending_locked("worker")
    if engine._owner_alert_tasks:
        await asyncio.gather(*engine._owner_alert_tasks)
    row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
    assert row.accepted_at == row.attempts == 0
    assert row.last_error.startswith("PASTED_UNCONFIRMED_SESSION_LOST")
    assert len(alerts) == 1 and calls == []


async def test_short_cadence_busy_fire_drains_before_staleness(registry, clock):
    pending = fire(registry, cron="* * * * *")
    calls = []

    async def wake(*args):
        calls.append(args)
        return True

    engine = AgentScheduler(registry, wake_callback=wake, delivery_busy_fn=lambda _: True)
    engine._check_pending_wake_liveness(NOW)
    clock.now = NOW + 31
    engine._check_pending_wake_liveness(clock.now)
    tasks = list(engine._pending_replay_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)
    assert len(calls) == 1, "minute-cadence fires must drain before their 60-second stale limit"
    assert registry.get_schedule_wake_by_fire(pending.schedule_id, NOW).accepted_at > 0


async def test_failed_write_ahead_marker_prevents_paste(registry, tmp_path, monkeypatch, clock):
    pending = fire(registry)
    ss, tmux = session(tmp_path)
    durable = ScheduleWakeReceipt(registry, pending.schedule_id, pending.fired_at)
    monkeypatch.setattr(ss, "_scheduler_pane_busy", lambda candidate=None: False)

    def failed_write(*args, **kwargs):
        raise OSError("synthetic ledger write failure")

    monkeypatch.setattr(registry, "mark_schedule_wake_pasted", failed_write, raising=False)
    receipt = await ss.send_scheduler_prompt(pending.prompt, on_accept=durable.accept)
    await asyncio.gather(*ss._scheduler_delivery_tasks)
    try:
        assert receipt.done() and receipt.result() is False
        assert tmux.paste_text.await_count == 0
        row = registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at)
        assert row.accepted_at == row.attempts == getattr(row, "pasted_at", 0) == 0
    finally:
        await ss.disconnect()


def test_attempt_cap_parks_one_oldest_row_when_fire_times_tie(registry, clock):
    first = fire(registry, name='first')
    second = fire(registry, name='second')
    assert first.id < second.id
    clock.now = NOW + 1
    engine = AgentScheduler(registry)
    result = engine._drain_park_outbox_rows(
        'worker', bound_reason='attempt cap',
        state=_OutboxDrainExtensionState(NOW, attempts=30), oldest_age=1,
    )
    assert result == (1, 1)
    assert registry.get_schedule_wake_by_fire(first.schedule_id, NOW).drain_parked_at > 0
    assert registry.get_schedule_wake_by_fire(second.schedule_id, NOW).drain_parked_at == 0


@pytest.mark.parametrize('abandoned', [False, True])
async def test_marked_inflight_fire_fences_newer_until_independent_acceptance(registry, clock, abandoned):
    older = fire(registry, at=NOW - 1000)
    assert callable(getattr(registry, 'mark_schedule_wake_pasted', None))
    assert registry.mark_schedule_wake_pasted(
        older.schedule_id, older.fired_at, pasted_at=NOW - 10, session_id='current',
    )
    if abandoned:
        registry.abandon_pending_schedule_wake(older.id, reason='RECEIPT_ABANDONED: fixture')
    newer, _ = registry.persist_schedule_wake(
        older.schedule_id, agent_name='worker', schedule_name=older.name,
        prompt='newer work', fired_at=NOW - 1,
    )
    calls = []

    async def wake(_agent, _session, prompt):
        calls.append(prompt)
        return True

    engine = AgentScheduler(
        registry, wake_callback=wake,
        delivery_session_fn=lambda _: 'current',
        delivery_inflight_fn=lambda _agent, prompt: prompt == older.prompt,
    )
    await engine._replay_pending_locked('worker')
    assert calls == [], 'an unresolved older paste must fence the next fire before handoff'
    assert registry.get_schedule_wake_by_fire(newer.schedule_id, newer.fired_at).attempts == 0
    assert registry.get_schedule_wake_by_fire(older.schedule_id, older.fired_at).accepted_at == 0
    assert ScheduleWakeReceipt(registry, older.schedule_id, older.fired_at).accept()
    await engine._replay_pending_locked('worker')
    assert calls == [newer.prompt]
    row = registry.get_schedule_wake_by_fire(newer.schedule_id, newer.fired_at)
    assert row.accepted_at > 0 and row.attempts == 1


@pytest.mark.parametrize('runtime', ['codex_cli', 'claude_sdk'])
async def test_default_busy_policy_stale_drops_old_notification_backlog(registry, clock, runtime):
    registry.register('worker', runtime=runtime, transport='tmux')
    older = fire(registry, at=NOW - 600, cron='* * * * *')
    newer, _ = registry.persist_schedule_wake(
        older.schedule_id, agent_name='worker', schedule_name=older.name,
        prompt=older.prompt, fired_at=NOW - 500,
    )
    alerts = []
    engine = AgentScheduler(
        registry, owner_notify_callback=lambda *args: alerts.append(args),
        delivery_drain_busy_fn=lambda _: True, outbox_drain_extension_attempt_cap=1,
    )
    await engine._replay_pending_locked('worker', drain_recheck=True)
    if engine._owner_alert_tasks:
        await asyncio.gather(*engine._owner_alert_tasks)
    assert alerts == []
    assert registry.get_schedule_wake_by_fire(older.schedule_id, older.fired_at) is None
    assert registry.get_schedule_wake_by_fire(newer.schedule_id, newer.fired_at) is None
    notices = registry.list_recurring_schedule_stale_drops('worker')
    assert len(notices) == 1 and notices[0].drop_count == 2


@pytest.mark.parametrize('release', ['confirmed-delivery', 'verified-idle'])
async def test_default_busy_policy_advances_then_stale_drops_notification_cohort(registry, clock, release):
    registry.register('worker', runtime='claude_sdk', transport='tmux')
    first = fire(registry, at=NOW - 30, name='first', cron='* * * * *')
    second = fire(registry, at=NOW - 20, name='second', cron='* * * * *')
    rows = [first, second]
    busy = [True]
    alerts = []
    calls = []

    async def wake(*args):
        calls.append(args[-1])
        return False

    engine = AgentScheduler(
        registry, wake_callback=wake, delivery_drain_busy_fn=lambda _: busy[0],
        owner_notify_callback=lambda *args: alerts.append(args),
        outbox_drain_extension_attempt_cap=1,
    )
    await engine._replay_pending_locked('worker', drain_recheck=True)
    assert calls == [first.prompt]
    assert registry.get_schedule_wake_by_fire(first.schedule_id, first.fired_at).attempts == 1
    assert registry.get_schedule_wake_by_fire(second.schedule_id, second.fired_at).attempts == 0
    assert all(registry.get_schedule_wake_by_fire(row.schedule_id, row.fired_at).drain_parked_at == 0 for row in rows)
    if release == 'confirmed-delivery':
        assert registry.confirm_pending_schedule_wake_by_fire(first.schedule_id, first.fired_at)
        rows.pop(0)
    else:
        busy[0] = False
        await engine._replay_pending_locked('worker', drain_recheck=True)
    busy[0] = True
    clock.now += 60
    await engine._replay_pending_locked('worker', drain_recheck=True)
    if engine._owner_alert_tasks:
        await asyncio.gather(*engine._owner_alert_tasks)
    assert alerts == []
    assert all(registry.get_schedule_wake_by_fire(row.schedule_id, row.fired_at) is None for row in rows)
    assert sum(n.drop_count for n in registry.list_recurring_schedule_stale_drops('worker')) == len(rows)
