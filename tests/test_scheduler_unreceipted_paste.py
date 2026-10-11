"""A completed unrelated turn must not strand later serialized wakes."""

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import codex_tmux_session, scheduler, tmux_session
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.codex_tmux_transcript import CodexTmuxTranscriptTailer
from pinky_daemon.scheduler import AgentScheduler, ScheduleWakeReceipt
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _TmuxControl
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from pinky_daemon.transport_state import SessionState, Trigger

# The fix specification sets the real bound; this horizon is not its design.
HORIZON = 4_200
NOW = 1_800_000_000.0
FIRST = "perform the first synthetic task"
SECOND = "perform the second synthetic task"


async def _settle():
    for _ in range(8):
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=NOW, status="idle", updated=NOW, polls=0)

    async def sleep(_delay):
        clock.polls += 1
        await asyncio.sleep(0)

    for module in (scheduler, tmux_session, codex_tmux_session):
        monkeypatch.setattr(
            module,
            "time",
            SimpleNamespace(
                **{**vars(module.time), "time": lambda: clock.now},
            ),
        )
        monkeypatch.setattr(
            module,
            "asyncio",
            SimpleNamespace(
                **{**vars(asyncio), "sleep": sleep},
            ),
        )
    return clock


async def _make_harness(tmp_path, clock, *, kind=TmuxSession):
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "test-receipt"
    ok = TmuxCommandResult(returncode=0, stdout="", stderr="")
    control.paste_text = AsyncMock(return_value=ok)
    control.kill_session = AsyncMock(return_value=ok)
    control.capture_pane = AsyncMock(return_value=ok)
    config = StreamingSessionConfig(
        agent_name="worker",
        working_dir=str(tmp_path),
        live_status_fn=lambda: {"status": clock.status, "last_updated": clock.updated},
    )
    session = kind(config, tmux_control=control)
    transition = await session._state_machine.request_transition(SessionState.BOOTING, Trigger.BOOT)
    await session._state_machine.transition_complete(
        transition.owner_token,
        SessionState.CONNECTED,
        trigger=Trigger.BOOT_COMPLETE,
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    # Attach a local tailer without starting a process or discovery loop.
    tailer_kind = CodexTmuxTranscriptTailer if kind is CodexTmuxSession else TmuxTranscriptTailer
    session._tailer = tailer_kind(
        transcript,
        session._handle_turn_complete,
        on_entry=session._on_transcript_entry,
    )
    registry = AgentRegistry(db_path=str(tmp_path / "registry.db"))
    registry.register("worker", working_dir=str(tmp_path / "agent"))
    return SimpleNamespace(
        session=session,
        control=control,
        transcript=transcript,
        registry=registry,
        clock=clock,
        tasks=[],
    )


@pytest.fixture
async def harness(tmp_path, clock):
    harness = await _make_harness(tmp_path, clock)
    try:
        yield harness
    finally:
        for task in harness.tasks:
            task.cancel()
        for task in harness.tasks:
            with suppress(asyncio.CancelledError):
                await task
        await harness.session.disconnect()
        harness.registry.close()


async def _rows(harness, *entries):
    with harness.transcript.open("a") as stream:
        for entry in entries:
            stream.write(json.dumps(entry) + "\n")
    assert await harness.session._tailer.read_once() > 0
    assert harness.session._tailer.stats["callback_errors"] == 0


def _user(prompt):
    return {"type": "user", "message": {"role": "user", "content": prompt}}


class _ReceiptOwner:
    def __init__(self, receipt):
        self.callback = MagicMock(side_effect=receipt.accept)
        self.mark_pasted = MagicMock(side_effect=receipt.mark_pasted)

    def accept(self):
        return self.callback()


async def _first_paste(harness, *, owned=False):
    callback = None
    durable = None
    if owned:
        schedule = harness.registry.add_schedule(
            "worker",
            "0 * * * *",
            name="periodic",
            prompt=FIRST,
        )
        harness.registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=FIRST,
            fired_at=NOW,
        )
        durable = ScheduleWakeReceipt(harness.registry, schedule.id, NOW)
        owner = _ReceiptOwner(durable)
        callback = owner.callback
    receipt = await harness.session.send_scheduler_prompt(
        FIRST,
        on_accept=owner.accept if owned else None,
    )
    await asyncio.wait_for(
        asyncio.gather(*list(harness.session._scheduler_delivery_tasks)),
        timeout=5,
    )
    assert [call.args[0] for call in harness.control.paste_text.await_args_list] == [FIRST]
    entry = harness.session._inflight_metas[0]
    assert entry.turn.pane_delivery_started and entry.turn.pane_delivery_recorded
    assert not receipt.done()
    return SimpleNamespace(
        receipt=receipt,
        entry=entry,
        callback=callback,
        durable=durable,
        mark_pasted=owner.mark_pasted if owned else None,
    )


async def _unrelated_stop(harness, first, *, notification=False):
    completed = harness.session._tailer.stats["turns_fired"]
    harness.clock.now += 1
    harness.clock.status = "working"
    harness.clock.updated = harness.clock.now
    if notification:
        await _rows(
            harness,
            {"type": "queue-operation", "operation": "enqueue", "content": "harness notification"},
            {"type": "queue-operation", "operation": "dequeue"},
            _user("harness notification"),
        )
    await _rows(
        harness,
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "unrelated work"}],
            },
        },
    )
    harness.clock.now += 11
    harness.clock.status = "idle"
    harness.clock.updated = harness.clock.now
    await _rows(harness, {"type": "system", "subtype": "stop_hook_summary"})
    assert not harness.session._inflight_metas
    assert harness.session._tailer.stats["turns_fired"] == completed + 1
    assert not first.receipt.done()
    if first.callback is not None:
        first.callback.assert_not_called()
    assert harness.clock.updated > first.entry.paste_succeeded_at


async def _second_paste(harness, *, expired=False):
    callback = MagicMock(return_value=True)
    receipt = await harness.session.send_scheduler_prompt(
        SECOND,
        on_accept=callback,
        busy_deliver_at=harness.clock.now + (-1 if expired else 600),
    )
    await _settle()
    return receipt


async def _advance(harness, horizon):
    end = NOW + horizon
    while harness.clock.now + tmux_session._WATCHDOG_TICK_SEC <= end:
        await _tick(harness)
    harness.clock.now = end
    harness.clock.updated = end
    await _settle()


async def _start_watchdog(harness, monkeypatch):
    gate = asyncio.Queue()
    harness.clock.watchdog_waits = 0

    async def sleep(delay):
        if delay == tmux_session._WATCHDOG_TICK_SEC:
            harness.clock.watchdog_waits += 1
            await gate.get()
        else:
            await asyncio.sleep(0)

    monkeypatch.setattr(
        tmux_session, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep})
    )
    harness.tick_gate = gate
    task = asyncio.create_task(harness.session._inflight_watchdog())
    harness.tasks.append(task)
    await _settle()
    assert harness.clock.watchdog_waits == 1 and not task.done()


async def _tick(harness, *, at=None, status=None, updated=None):
    harness.clock.now = (
        at if at is not None else harness.clock.now + tmux_session._WATCHDOG_TICK_SEC
    )
    if status is not None:
        harness.clock.status = status
    harness.clock.updated = harness.clock.now if updated is None else updated
    target = harness.clock.watchdog_waits + 1
    harness.tick_gate.put_nowait(None)
    for _ in range(100):
        await asyncio.sleep(0)
        if harness.clock.watchdog_waits == target:
            break
    assert harness.clock.watchdog_waits == target, "the real watchdog must finish this tick"
    await _settle()


def _pastes(harness):
    return [call.args[0] for call in harness.control.paste_text.await_args_list]


def _unreceipted(harness):
    return getattr(harness.session, "_unreceipted_scheduler_pastes", [])


def _lines(capsys, token):
    return [line for line in capsys.readouterr().err.splitlines() if token in line]


def _confirm_following_pastes(harness):
    async def paste(prompt, *, enter=True):
        await _rows(harness, _user(prompt))
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    harness.control.paste_text.side_effect = paste


@pytest.mark.parametrize("horizon", [660, 1_800, HORIZON, 14_400])
async def test_r1_ownerless_lost_paste_releases_later_wake(harness, monkeypatch, horizon):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first, notification=True)
    _confirm_following_pastes(harness)
    second = await _second_paste(harness)
    await _start_watchdog(harness, monkeypatch)
    await _advance(harness, horizon)
    assert len(_pastes(harness)) == 3 and _pastes(harness).count(FIRST) == 2
    assert _pastes(harness).count(SECOND) == 1
    assert first.receipt.done() and first.receipt.result() is True
    assert second.done() and second.result() is True


@pytest.mark.parametrize("horizon", [660, 1_800, HORIZON, 14_400])
async def test_r2_owned_lost_paste_releases_later_wake(harness, monkeypatch, horizon):
    first = await _first_paste(harness, owned=True)
    await _unrelated_stop(harness, first)
    _confirm_following_pastes(harness)
    second = await _second_paste(harness)
    await _start_watchdog(harness, monkeypatch)
    await _advance(harness, horizon)
    first.callback.assert_not_called()
    first.mark_pasted.assert_called_once()
    assert _pastes(harness) == [FIRST, SECOND]
    assert first.receipt.done() and first.receipt.result() is False
    assert second.done() and second.result() is True


async def test_r3_real_receipt_wait_confirms_later_fire(harness, monkeypatch):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    await _start_watchdog(harness, monkeypatch)
    schedule = harness.registry.add_schedule(
        "worker", "0 * * * *", name="later-periodic", prompt=SECOND,
    )
    fired_at = harness.clock.now
    pending, _ = harness.registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=schedule.name,
        prompt=SECOND, fired_at=fired_at,
    )
    budgets = []

    async def paste(prompt, *, enter=True):
        if prompt == SECOND:
            await _rows(harness, _user(SECOND))
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    harness.control.paste_text.side_effect = paste

    async def wake(_agent, _session, prompt, *, schedule_receipt, busy_deliver_at=None):
        return await harness.session.send_scheduler_prompt(
            prompt, on_accept=schedule_receipt.accept, busy_deliver_at=busy_deliver_at,
        )

    async def wait_for(awaitable, timeout):
        await _settle()
        if awaitable.done():
            return await awaitable
        budgets.append(timeout)
        deadline = harness.clock.now + timeout
        while harness.clock.now < deadline:
            await _tick(harness, at=min(harness.clock.now + tmux_session._WATCHDOG_TICK_SEC, deadline))
            if awaitable.done():
                return await awaitable
        if awaitable.done():
            return await awaitable
        awaitable.cancel()
        raise asyncio.TimeoutError

    monkeypatch.setattr(scheduler, "asyncio", SimpleNamespace(
        **{**vars(scheduler.asyncio), "wait_for": wait_for},
    ))
    engine = AgentScheduler(
        harness.registry, wake_callback=wake,
        delivery_session_fn=lambda _agent: harness.session._scheduler_paste_session_id,
        delivery_inflight_fn=lambda _agent, prompt: harness.session.scheduler_wake_inflight(prompt),
        delivery_queued_fn=lambda _agent, prompt: harness.session.scheduler_wake_queued(prompt),
        delivery_cancel_queued_fn=lambda _agent, prompt: harness.session.cancel_scheduler_wake(prompt),
        schedule_delivery_timeout=600, receipt_extension_attempt_cap=3,
    )
    try:
        await engine._replay_pending_locked("worker")
        row = harness.registry.get_schedule_wake_by_fire(pending.schedule_id, fired_at)
        assert row.accepted_at > 0 and row.abandoned_at == 0, (
            row.ledger_state, row.last_error, budgets,
        )
    finally:
        await engine.stop()


async def test_k1_unresolved_paste_vetoes_an_expired_later_wake(harness):
    await _first_paste(harness)
    await _second_paste(harness, expired=True)
    assert [call.args[0] for call in harness.control.paste_text.await_args_list] == [FIRST]


async def test_k2_late_exact_user_row_releases_later_wake(harness):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    await _second_paste(harness, expired=True)
    assert harness.control.paste_text.await_count == 1
    await _rows(harness, _user(FIRST))
    await _settle()
    assert first.receipt.done() and first.receipt.result() is True
    assert [call.args[0] for call in harness.control.paste_text.await_args_list] == [FIRST, SECOND]


async def test_k3_ordinary_send_bypasses_unresolved_scheduler_paste(harness):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    await _second_paste(harness, expired=True)
    worker = asyncio.create_task(harness.session._message_worker())
    harness.tasks.append(worker)
    assert await harness.session.send("ordinary steering")
    await _settle()
    assert not first.receipt.done()
    assert [call.args[0] for call in harness.control.paste_text.await_args_list] == [
        FIRST, "ordinary steering",
    ]


async def test_k4_native_enqueue_without_dequeue_keeps_later_wake_waiting(harness):
    first = await _first_paste(harness)
    await _rows(harness, {"type": "queue-operation", "operation": "enqueue", "content": FIRST})
    await _unrelated_stop(harness, first)
    await _second_paste(harness, expired=True)
    assert not first.receipt.done()
    assert len(harness.session._pane_queue_operations) == 1
    assert [call.args[0] for call in harness.control.paste_text.await_args_list] == [FIRST]


async def test_k5_retained_meta_requeues_lost_paste_and_releases_later_wake(harness):
    first = await _first_paste(harness)
    second_receipt = await _second_paste(harness)
    worker = asyncio.create_task(harness.session._message_worker())
    watchdog = asyncio.create_task(harness.session._inflight_watchdog())
    harness.tasks.extend([watchdog, worker])
    harness.clock.now = NOW + tmux_session._TURN_DONE_TIMEOUT_SEC + 1
    harness.clock.updated = harness.clock.now
    await _settle()
    assert first.entry.turn.replay_count == 1
    assert SECOND in [call.args[0] for call in harness.control.paste_text.await_args_list]
    assert not first.receipt.done()
    await _rows(harness, _user(SECOND))
    await _settle()
    assert second_receipt.result() is True
    pasted = [call.args[0] for call in harness.control.paste_text.await_args_list]
    assert len(pasted) == 3 and pasted.count(FIRST) == 2 and pasted.count(SECOND) == 1


async def test_t1_ownerless_paste_redelivers_after_two_idle_ticks(harness, monkeypatch, capsys):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first, notification=True)
    assert harness.session._stats.get("pane_entries_popped_unaccepted") == 1
    diagnostics = capsys.readouterr().err
    assert diagnostics.count("SCHEDULER_PASTE_UNRECEIPTED") == 1
    assert "PANE_ENTRY_POPPED_UNACCEPTED" not in diagnostics
    second = await _second_paste(harness, expired=True)
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness)
    assert _pastes(harness) == [FIRST] and not first.receipt.done()
    assert first.entry.turn.replay_count == 0
    await _tick(harness)
    assert first.entry.turn.replay_count == 1
    assert sum(turn is first.entry.turn for turn in harness.session._scheduler_pending_turns) == 1
    assert _pastes(harness).count(SECOND) == 1
    assert not first.receipt.done() and not _unreceipted(harness)
    await _rows(harness, _user(SECOND))
    await _settle()
    assert len(_pastes(harness)) == 3 and _pastes(harness).count(FIRST) == 2
    await _rows(harness, _user(FIRST))
    await _settle()
    assert first.receipt.result() is True and second.result() is True
    outcomes = _lines(capsys, "SCHEDULER_PASTE_SETTLED")
    assert len(outcomes) == 1 and "outcome=redelivered" in outcomes[0]


async def test_t2_marked_fire_is_undelivered_without_another_paste(harness, monkeypatch, capsys):
    first = await _first_paste(harness, owned=True)
    await _unrelated_stop(harness, first)
    await _second_paste(harness, expired=True)
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness)
    assert not first.receipt.done() and _pastes(harness) == [FIRST]
    await _tick(harness)
    assert first.receipt.done() and first.receipt.result() is False
    first.callback.assert_not_called()
    first.mark_pasted.assert_called_once()
    assert _pastes(harness) == [FIRST, SECOND] and not _unreceipted(harness)
    outcomes = _lines(capsys, "SCHEDULER_PASTE_SETTLED")
    assert len(outcomes) == 1 and "reason=marked_fire" in outcomes[0]
    await _rows(harness, _user(FIRST))
    first.callback.assert_not_called()
    await _rows(harness, _user(SECOND))
    accept = MagicMock(return_value=True)
    later = await harness.session.send_scheduler_prompt(FIRST, on_accept=accept)
    await _settle()
    await _rows(harness, _user(FIRST))
    await _settle()
    assert later.done() and later.result() is True
    accept.assert_called_once()
    first.callback.assert_not_called()


@pytest.mark.parametrize("owned", [False, True])
async def test_t3_unread_exact_row_accepts_on_the_first_tick(harness, monkeypatch, owned, capsys):
    first = await _first_paste(harness, owned=owned)
    await _unrelated_stop(harness, first)
    with harness.transcript.open("a") as stream:
        stream.write(json.dumps(_user(FIRST)) + "\n")
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness)
    assert first.receipt.done() and first.receipt.result() is True
    if owned:
        first.callback.assert_called_once()
        first.mark_pasted.assert_called_once()
    assert _pastes(harness) == [FIRST] and not _unreceipted(harness)
    assert len(_lines(capsys, "SCHEDULER_PASTE_SETTLED")) == 1


async def test_t4_unavailable_probe_is_undelivered_after_two_ticks(harness, monkeypatch, capsys):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    harness.transcript.unlink()
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness)
    assert not first.receipt.done()
    await _tick(harness)
    assert first.receipt.done() and first.receipt.result() is False
    assert _pastes(harness) == [FIRST] and not _unreceipted(harness)
    outcomes = _lines(capsys, "SCHEDULER_PASTE_SETTLED")
    assert len(outcomes) == 1 and "reason=probe_unavailable" in outcomes[0]


async def test_t5_disabled_watchdog_settles_without_redelivery(harness, monkeypatch, capsys):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    harness.session._config.watchdog_enabled_fn = lambda: False
    await _second_paste(harness, expired=True)
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness)
    assert not first.receipt.done()
    await _tick(harness)
    assert first.receipt.done() and first.receipt.result() is False
    assert _pastes(harness) == [FIRST, SECOND] and not _unreceipted(harness)
    outcomes = _lines(capsys, "SCHEDULER_PASTE_SETTLED")
    assert len(outcomes) == 1 and "reason=watchdog_disabled" in outcomes[0]


async def test_t6_redelivery_stops_at_the_replay_cap(harness, monkeypatch, capsys):
    cap = 2
    monkeypatch.setenv("PINKY_INFLIGHT_REPLAY_CAP", str(cap))
    first = await _first_paste(harness)
    await _start_watchdog(harness, monkeypatch)
    for attempt in range(cap + 1):
        await _unrelated_stop(harness, first)
        await _tick(harness)
        assert not first.receipt.done()
        await _tick(harness)
        assert not _unreceipted(harness)
        if attempt < cap:
            assert not first.receipt.done()
            assert _pastes(harness) == [FIRST] * (attempt + 2)
    assert first.receipt.done() and first.receipt.result() is False
    assert _pastes(harness) == [FIRST] * (cap + 1)
    assert first.entry.turn.replay_count == cap
    outcomes = _lines(capsys, "SCHEDULER_PASTE_SETTLED")
    assert len(outcomes) == cap + 1 and "reason=replay_cap" in outcomes[-1]


@pytest.mark.parametrize("nonqualifying", ["working", "stale", "nonnumeric", "boolean"])
async def test_t7_only_consecutive_fresh_idle_ticks_settle(
    harness, monkeypatch, nonqualifying, capsys
):
    if nonqualifying == "boolean":
        harness.clock.now = 0.5
        harness.clock.updated = 0.5
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    await _start_watchdog(harness, monkeypatch)

    async def disqualify():
        if nonqualifying == "working":
            await _tick(harness, status="working")
        else:
            updated = {"stale": NOW - 1, "nonnumeric": str(NOW), "boolean": True}[nonqualifying]
            await _tick(harness, status="idle", updated=updated)
        assert not _lines(capsys, "SCHEDULER_PASTE_SETTLE_ERROR")

    for _ in range(3):
        await disqualify()
    assert not first.receipt.done() and _pastes(harness) == [FIRST]
    await _tick(harness, status="idle")
    assert _pastes(harness) == [FIRST]
    await disqualify()
    await _tick(harness, status="idle")
    assert _pastes(harness) == [FIRST]
    await _tick(harness, status="idle")
    assert _pastes(harness) == [FIRST, FIRST] and not _unreceipted(harness)


async def test_t8_late_evidence_retires_the_streak_silently(harness, monkeypatch, capsys):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    assert len(_lines(capsys, "SCHEDULER_PASTE_UNRECEIPTED")) == 1
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness)
    await _rows(harness, _user(FIRST))
    await _tick(harness)
    assert first.receipt.result() is True and _pastes(harness) == [FIRST]
    assert not _unreceipted(harness)
    assert not _lines(capsys, "SCHEDULER_PASTE_SETTLED")


async def test_terminal_negative_receipt_retires_the_entry_silently(harness, monkeypatch, capsys):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    assert len(_lines(capsys, "SCHEDULER_PASTE_UNRECEIPTED")) == 1
    await _start_watchdog(harness, monkeypatch)
    first.receipt.cancel()
    await _tick(harness)
    assert not _unreceipted(harness) and _pastes(harness) == [FIRST]
    assert not _lines(capsys, "SCHEDULER_PASTE_SETTLED")


async def test_t9_native_queue_waits_for_its_dequeue(harness, monkeypatch, capsys):
    first = await _first_paste(harness)
    await _rows(harness, {"type": "queue-operation", "operation": "enqueue", "content": FIRST})
    await _unrelated_stop(harness, first)
    await _second_paste(harness, expired=True)
    await _start_watchdog(harness, monkeypatch)
    for _ in range(4):
        await _tick(harness)
    assert not first.receipt.done() and _pastes(harness) == [FIRST]
    assert not _lines(capsys, "SCHEDULER_PASTE_SETTLED")
    await _rows(harness, {"type": "queue-operation", "operation": "dequeue"})
    await _tick(harness)
    assert first.receipt.result() is True and _pastes(harness) == [FIRST, SECOND]
    assert not _unreceipted(harness)


async def test_native_queue_evidence_resets_the_qualifying_streak(harness, monkeypatch, capsys):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    assert len(_lines(capsys, "SCHEDULER_PASTE_UNRECEIPTED")) == 1
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness)
    assert _unreceipted(harness)[0].qualifying_ticks == 1
    await _rows(harness, {"type": "queue-operation", "operation": "enqueue", "content": FIRST})
    await _tick(harness)
    assert _unreceipted(harness)[0].qualifying_ticks == 0
    assert not first.receipt.done() and _pastes(harness) == [FIRST]


@pytest.mark.parametrize("kind", [TmuxSession, CodexTmuxSession])
async def test_t10_expired_wait_names_its_holder_every_300_seconds(
    tmp_path, clock, monkeypatch, capsys, kind
):
    harness = await _make_harness(tmp_path, clock, kind=kind)
    try:
        await _first_paste(harness)
        capsys.readouterr()
        await _second_paste(harness)
        assert not _lines(capsys, "SCHEDULER_PASTE_HELD")
        clock.now += 600
        await _settle()
        first = _lines(capsys, "SCHEDULER_PASTE_HELD")
        assert len(first) == 1 and "holder=owner-less" in first[0]
        assert "inflight=True" in first[0] and "unreceipted=False" in first[0]
        clock.now += 299
        await _settle()
        assert not _lines(capsys, "SCHEDULER_PASTE_HELD")
        clock.now += 1
        await _settle()
        assert len(_lines(capsys, "SCHEDULER_PASTE_HELD")) == 1
    finally:
        await harness.session.disconnect()
        harness.registry.close()


async def test_t11_only_unaccepted_ownerless_receipts_log_an_ending(tmp_path, clock, capsys):
    for label in ("pending", "accepted", "owned", "unpasted"):
        subdir = tmp_path / label
        subdir.mkdir()
        harness = await _make_harness(subdir, clock)
        try:
            capsys.readouterr()
            if label == "unpasted":
                clock.status = "working"
                receipt = await harness.session.send_scheduler_prompt(FIRST)
                await _settle()
                assert not receipt.done() and not _pastes(harness)
            else:
                await _first_paste(harness, owned=label == "owned")
                if label == "accepted":
                    await _rows(harness, _user(FIRST))
                    await _settle()
            await harness.session.disconnect()
            await _settle()
            endings = _lines(capsys, "SCHEDULER_UNOWNED_TURN_NOT_ACCEPTED")
            assert len(endings) == (1 if label in ("pending", "unpasted") else 0)
            if endings:
                assert f"pasted={label != 'unpasted'}" in endings[0]
                assert FIRST in endings[0] and "state=" in endings[0]
            clock.status = "idle"
        finally:
            await harness.session.disconnect()
            harness.registry.close()


async def test_t12_ordinary_sends_pass_while_redelivery_waits(harness, monkeypatch):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first, notification=True)
    await _second_paste(harness, expired=True)
    harness.tasks.append(asyncio.create_task(harness.session._message_worker()))
    await _start_watchdog(harness, monkeypatch)
    for index in range(4):
        if index in (1, 2):
            await _tick(harness)
        text = f"ordinary steering {index}"
        assert await harness.session.send(text)
        await _settle()
        assert _pastes(harness).count(text) == 1
        await _rows(harness, _user(text))
    assert not first.receipt.done()
    if SECOND in _pastes(harness):
        await _rows(harness, _user(SECOND))
        await _settle()
        await _rows(harness, _user(FIRST))


async def test_t13_disconnect_clears_the_recorded_entry(harness, capsys):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    assert len(_lines(capsys, "SCHEDULER_PASTE_UNRECEIPTED")) == 1
    assert len(_unreceipted(harness)) == 1
    await harness.session.disconnect()
    assert not _unreceipted(harness) and first.receipt.result() is False


async def test_t14_codex_completion_keeps_its_existing_receipt_rule(
    tmp_path, clock, monkeypatch, capsys
):
    harness = await _make_harness(tmp_path, clock, kind=CodexTmuxSession)
    try:
        first = await _first_paste(harness)
        clock.now += 12
        clock.updated = clock.now
        await _rows(
            harness,
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "foreign"}},
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "unrelated work"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "done"},
            },
        )
        await _second_paste(harness)
        await _start_watchdog(harness, monkeypatch)
        for _ in range(10):
            await _tick(harness)
        assert _pastes(harness) == [FIRST, SECOND]
        assert not first.receipt.done() and not _unreceipted(harness)
        assert not _lines(capsys, "SCHEDULER_PASTE_SETTLED")
        with capsys.disabled():
            print("CODEX_PASTE_LIST=" + json.dumps(_pastes(harness)))
    finally:
        for task in harness.tasks:
            task.cancel()
        await asyncio.gather(*harness.tasks, return_exceptions=True)
        await harness.session.disconnect()
        harness.registry.close()


async def test_t15_abandonment_alert_uses_the_ledger_paste_marker(harness):
    never = "The prompt was never pasted: the agent did not get this wake."
    uncertain = "The prompt was pasted and not confirmed: the agent may have received it."
    for label in ("unpasted", "pasted", "missing"):
        schedule = harness.registry.add_schedule("worker", "0 * * * *", name=label, prompt=FIRST)
        schedule.last_run = NOW
        if label != "missing":
            harness.registry.persist_schedule_wake(
                schedule.id,
                agent_name="worker",
                schedule_name=label,
                prompt=FIRST,
                fired_at=NOW,
            )
            if label == "pasted":
                assert ScheduleWakeReceipt(harness.registry, schedule.id, NOW).mark_pasted(
                    "session", FIRST
                )
        notify = AsyncMock(return_value=True)
        engine = AgentScheduler(harness.registry, owner_notify_callback=notify)
        try:
            engine._mark_abandoned_receipt(
                schedule, age=1800, bound_reason="attempt cap", wait_attempts=3
            )
            await asyncio.gather(*list(engine._owner_alert_tasks))
            notify.assert_awaited_once()
            text = notify.await_args.args[1]
            assert (never in text) == (label == "unpasted")
            assert (uncertain in text) == (label == "pasted")
        finally:
            await engine.stop()


async def test_probe_exception_is_logged_and_the_watchdog_keeps_ticking(
    harness, monkeypatch, capsys
):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    probe = harness.session._phantom_consumption_verdicts
    await _start_watchdog(harness, monkeypatch)
    monkeypatch.setattr(
        harness.session,
        "_phantom_consumption_verdicts",
        MagicMock(side_effect=RuntimeError("probe failed")),
    )
    await _tick(harness)
    assert len(_lines(capsys, "SCHEDULER_PASTE_SETTLE_ERROR")) == 1
    assert not first.receipt.done()
    monkeypatch.setattr(harness.session, "_phantom_consumption_verdicts", probe)
    await _tick(harness)
    await _tick(harness)
    assert _pastes(harness) == [FIRST, FIRST] and not _unreceipted(harness)


async def test_zero_replay_cap_keeps_ownerless_redelivery_available(harness, monkeypatch):
    monkeypatch.setenv("PINKY_INFLIGHT_REPLAY_CAP", "0")
    first = await _first_paste(harness)
    await _start_watchdog(harness, monkeypatch)
    for attempt in range(4):
        await _unrelated_stop(harness, first)
        await _tick(harness)
        await _tick(harness)
        assert _pastes(harness) == [FIRST] * (attempt + 2)
        assert not first.receipt.done() and not _unreceipted(harness)


async def test_idle_report_at_the_paste_time_qualifies(harness, monkeypatch):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness, updated=first.entry.dispatched_at)
    assert _pastes(harness) == [FIRST]
    await _tick(harness, updated=first.entry.dispatched_at)
    assert _pastes(harness) == [FIRST, FIRST] and not _unreceipted(harness)


async def test_unaccepted_pops_skip_accepted_and_native_input(harness, capsys):
    harness.tasks.append(asyncio.create_task(harness.session._message_worker()))
    for label in ("lost", "accepted", "queued", "internal"):
        text = f"ordinary {label} input"
        if label == "internal":
            await harness.session._enqueue_internal_prompt(text, reason="maintenance")
        else:
            assert await harness.session.send(text)
        await _settle()
        if label == "accepted":
            await _rows(harness, _user(text))
        elif label == "queued":
            await _rows(
                harness, {"type": "queue-operation", "operation": "enqueue", "content": text}
            )
        await _rows(
            harness,
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "unrelated work"}],
                },
            },
            {"type": "system", "subtype": "stop_hook_summary"},
        )
        assert harness.session._stats.get("pane_entries_popped_unaccepted") == (
            2 if label == "internal" else 1
        )
    lines = _lines(capsys, "PANE_ENTRY_POPPED_UNACCEPTED")
    assert len(lines) == 2 and "kind=ordinary" in lines[0] and "kind=internal" in lines[1]


async def test_holder_line_tracks_popped_and_native_queue_evidence(harness, capsys):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    await _second_paste(harness, expired=True)
    lines = _lines(capsys, "SCHEDULER_PASTE_HELD")
    assert len(lines) == 1
    assert "inflight=False" in lines[0] and "unreceipted=True" in lines[0]
    assert "native_queue=False" in lines[0]
    await _rows(harness, {"type": "queue-operation", "operation": "enqueue", "content": FIRST})
    harness.clock.now += 300
    await _settle()
    lines = _lines(capsys, "SCHEDULER_PASTE_HELD")
    assert len(lines) == 1 and "native_queue=True" in lines[0]


async def test_stop_failure_records_the_same_unreceipted_paste(harness, monkeypatch, capsys):
    first = await _first_paste(harness)
    harness.clock.now += 12
    harness.clock.updated = harness.clock.now
    assert await harness.session.handle_stop_failure("synthetic_failure")
    assert len(_lines(capsys, "SCHEDULER_PASTE_UNRECEIPTED")) == 1
    await _start_watchdog(harness, monkeypatch)
    await _tick(harness)
    await _tick(harness)
    assert _pastes(harness) == [FIRST, FIRST] and not _unreceipted(harness)
    assert not first.receipt.done()
