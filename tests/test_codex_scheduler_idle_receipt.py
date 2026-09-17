"""Correlated Codex completion must release exact scheduler receipts safely."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.codex_tmux_transcript import CodexTmuxTranscriptTailer
from pinky_daemon.scheduler import AgentScheduler, ScheduleWakeReceipt
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, _QueuedTurn, _TmuxControl
from pinky_daemon.tmux_transcript import TurnResponse
from pinky_daemon.transport_state import SessionState

IDLE = "Ready\nmodel · /tmp/worker\n"
BUSY = "Working (esc to interrupt)\n" + IDLE
NOW = 1_800_000_000.0


def _ok(text=""):
    return TmuxCommandResult(returncode=0, stdout=text, stderr="")


def _append(harness, *payloads):
    with harness.rollout.open("a") as stream:
        for payload in payloads:
            stream.write(json.dumps({"type": "event_msg", "payload": payload}) + "\n")


async def _read(harness):
    assert await harness.session._tailer.read_once() > 0
    assert harness.session._tailer._stats["callback_errors"] == 0


async def _drain_delivery(harness):
    tasks = list(harness.session._scheduler_delivery_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


async def _paste(harness, *, prompt="scheduled work", on_accept=None):
    receipt = await harness.session.send_scheduler_prompt(prompt, on_accept=on_accept)
    turn = harness.session._scheduler_pending_turns[-1]
    await _drain_delivery(harness)
    assert turn.pane_delivery_started and turn.pane_delivery_recorded
    assert not receipt.done(), "paste success alone must not accept the wake"
    return turn, receipt


async def _start(harness, turn_id="current-turn"):
    _append(harness, {"type": "task_started", "turn_id": turn_id})
    await _read(harness)


async def _complete(harness, turn_id="current-turn"):
    _append(harness, {
        "type": "task_complete", "turn_id": turn_id,
        "last_agent_message": "completed", "duration_ms": 10,
    })
    await _read(harness)


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: NOW)
    monkeypatch.setattr("pinky_daemon.codex_tmux_session._CODEX_IDLE_CONFIRM_SEC", 0)
    tmux = MagicMock(spec=_TmuxControl)
    tmux.session_name = "test-scheduler-pane"
    tmux.has_session = AsyncMock(return_value=False)
    tmux.kill_session = AsyncMock(return_value=_ok())
    tmux.paste_text = AsyncMock(return_value=_ok())
    tmux.capture_pane = AsyncMock(return_value=_ok(IDLE))
    config = StreamingSessionConfig(agent_name="worker", working_dir=str(tmp_path))
    session = CodexTmuxSession(config, tmux_control=tmux)
    session._state_machine._state = SessionState.CONNECTED
    monkeypatch.setattr(session, "_context_lock_path", lambda: tmp_path / "absent.lock")
    rollout = tmp_path / "rollout.jsonl"
    rollout.touch()
    session._tailer = CodexTmuxTranscriptTailer(
        rollout, session._handle_turn_complete, on_entry=session._on_transcript_entry,
    )
    registry = AgentRegistry(db_path=str(tmp_path / "registry.db"))
    registry.register("worker")
    state = SimpleNamespace(
        session=session, tmux=tmux, rollout=rollout, registry=registry, schedulers=[],
    )
    try:
        yield state
    finally:
        for scheduler in state.schedulers:
            await scheduler.stop()
        await session.disconnect()
        registry.close()


def _schedule_fire(harness, *, age=10, prompt="scheduled work"):
    schedule = harness.registry.add_schedule(
        "worker", "0 * * * *", name="periodic", prompt=prompt,
    )
    fired_at = NOW - age
    pending, _ = harness.registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=schedule.name,
        prompt=prompt, fired_at=fired_at,
    )
    return schedule, pending, ScheduleWakeReceipt(harness.registry, schedule.id, fired_at)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="#1159 deferred: missing receipt needs independent submission ownership",
)
async def test_post_paste_idle_accepts_exact_fire_before_idle_notification(harness):
    """Missing user_message must not strand a completed, explicitly idle wake."""
    schedule, pending, durable = _schedule_fire(harness)
    receipt_order = []
    turn = None

    def accept():
        receipt_order.append(turn.scheduler_delivery.done())
        return durable.accept()

    turn, receipt = await _paste(harness, on_accept=accept)
    idle_observations = []
    harness.session._config.on_turn_idle = lambda _name: idle_observations.append((
        receipt.done(),
        harness.registry.get_schedule_wake_by_fire(schedule.id, pending.fired_at).accepted_at > 0,
    ))
    await _start(harness)
    assert not receipt.done(), "an active turn must not be accepted by this fallback"
    await _complete(harness)

    row = harness.registry.get_schedule_wake_by_fire(schedule.id, pending.fired_at)
    assert {
        "positive_receipt": receipt.done() and receipt.result() is True,
        "accepted": turn.transport_accepted,
        "durable_state": row.ledger_state,
        "abandoned": row.abandoned_at > 0,
        "inflight": harness.session.scheduler_wake_inflight(turn.prompt),
        "durability_before_future": receipt_order,
        "idle_observations": idle_observations,
        "idle_captures": harness.tmux.capture_pane.await_count,
    } == {
        "positive_receipt": True,
        "accepted": True,
        "durable_state": "receipted-ran-once",
        "abandoned": False,
        "inflight": False,
        "durability_before_future": [False],
        "idle_observations": [(True, True)],
        "idle_captures": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("new_prompt", ["newer distinct work", "older work"])
@pytest.mark.xfail(
    strict=True,
    reason="#1159 deferred: missing receipt needs independent submission ownership",
)
async def test_completed_abandoned_wake_releases_newer_fire_at_idle_boundary(harness, new_prompt):
    """Real receipt ownership and real replay must release same-schedule debt."""
    schedule, older, durable = _schedule_fire(harness, age=3_601, prompt="older work")
    turn, receipt = await _paste(harness, prompt=older.prompt, on_accept=durable.accept)
    await _start(harness)
    submitted = []

    async def deliver_newer(agent_name, session_id, prompt, *, schedule_receipt):
        assert agent_name == "worker"
        submitted.append(prompt)
        next_receipt = await harness.session.send_scheduler_prompt(
            prompt, on_accept=schedule_receipt.accept,
        )
        await _drain_delivery(harness)
        harness.session._on_transcript_entry({
            "type": "event_msg", "payload": {"type": "user_message", "message": prompt},
        })
        return next_receipt

    scheduler = AgentScheduler(
        harness.registry, wake_callback=deliver_newer,
        delivery_inflight_fn=lambda _name, prompt: harness.session.scheduler_wake_inflight(prompt),
    )
    harness.schedulers.append(scheduler)
    await scheduler._replay_pending_locked("worker")
    abandoned = harness.registry.get_schedule_wake_by_fire(schedule.id, older.fired_at)
    assert abandoned.ledger_state == "abandoned"
    assert abandoned.last_error.startswith("RECEIPT_ABANDONED")
    assert not receipt.done()
    assert turn in harness.session._acceptance_candidates()
    assert turn.pane_delivery_started
    assert harness.session.scheduler_wake_inflight(turn.prompt)
    assert submitted == []

    newer, _ = harness.registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=schedule.name,
        prompt=new_prompt, fired_at=NOW - 1,
    )
    # Preserve the old exact-fire authority while its work really is active.
    await scheduler._replay_pending_locked("worker")
    assert submitted == []
    assert harness.registry.get_schedule_wake_by_fire(schedule.id, newer.fired_at).attempts == 0

    idle_notifications = []

    def first_idle_boundary(agent_name):
        idle_notifications.append(agent_name)
        scheduler.notify_agent_idle(agent_name)

    harness.session._config.on_turn_idle = first_idle_boundary
    await _complete(harness)
    assert idle_notifications == ["worker"]
    replay_tasks = list(scheduler._pending_replay_tasks.values())
    assert replay_tasks, "the actual completion must notify the scheduler"
    await asyncio.wait_for(asyncio.gather(*replay_tasks), timeout=5)
    assert idle_notifications == ["worker"], "no second idle boundary or age-out pass"
    row = harness.registry.get_schedule_wake_by_fire(schedule.id, newer.fired_at)
    assert {
        "old_inflight": harness.session.scheduler_wake_inflight(turn.prompt),
        "new_submissions": submitted,
        "new_state": row.ledger_state,
        "physical_pastes": [call.args[0] for call in harness.tmux.paste_text.await_args_list],
    } == {
        "old_inflight": False,
        "new_submissions": [newer.prompt],
        "new_state": "receipted-ran-once",
        "physical_pastes": [older.prompt, newer.prompt],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["active", "idle_without_completion", "historical_close"])
async def test_incidental_idle_does_not_accept_scheduler_wake(harness, evidence):
    if evidence == "historical_close":
        # These bytes predate the paste, but the real tailer reads them later.
        _append(harness,
                {"type": "task_started", "turn_id": "previous-turn"},
                {"type": "task_complete", "turn_id": "previous-turn",
                 "last_agent_message": "previous result"})
    turn, receipt = await _paste(harness)
    if evidence == "active":
        await _start(harness)
        harness.tmux.capture_pane.return_value = _ok(BUSY)
    elif evidence == "historical_close":
        assert turn.transcript_offset_at_paste == harness.rollout.stat().st_size
        await _read(harness)
    else:
        assert await harness.session._codex_capture_explicit_idle()
        assert await harness.session._codex_capture_explicit_idle()
    assert not receipt.done()
    assert harness.session.scheduler_wake_inflight(turn.prompt)


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [BUSY, "", "Ready without a footer"])
async def test_completed_turn_with_unconfirmed_pane_does_not_accept(harness, snapshot):
    turn, receipt = await _paste(harness)
    await _start(harness)
    harness.tmux.capture_pane.return_value = _ok(snapshot)
    await _complete(harness)
    assert not receipt.done()
    assert harness.session.scheduler_wake_inflight(turn.prompt)


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [_ok(BUSY), _ok(""), TmuxCommandResult(1, "", "capture failed")])
async def test_first_idle_read_without_confirmation_does_not_accept(harness, second):
    turn, receipt = await _paste(harness)
    await _start(harness)
    harness.tmux.capture_pane.side_effect = [_ok(IDLE), second]
    await _complete(harness)
    assert not receipt.done()
    assert harness.session.scheduler_wake_inflight(turn.prompt)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["in_hand", "queued", "other_meta", "tool_after_close"])
async def test_other_work_prevents_idle_fallback_acceptance(harness, blocker):
    turn, receipt = await _paste(harness)
    await _start(harness)
    other = _QueuedTurn(prompt="other work")
    if blocker == "in_hand":
        harness.session._inflight_turn = other
    elif blocker == "queued":
        harness.session._message_queue.put_nowait(other)
    elif blocker == "other_meta":
        other.pane_delivery_started = True
        harness.session._finish_turn_delivery(other)
    else:
        async def newly_active_tool(_response):
            harness.session._inflight_tool_calls["new-tool"] = {"tool": "test"}

        harness.session._response_callback = newly_active_tool
    await _complete(harness)
    if blocker == "tool_after_close":
        assert harness.session._inflight_tool_calls
    assert not receipt.done()
    assert harness.session.scheduler_wake_inflight(turn.prompt)


@pytest.mark.asyncio
async def test_unpasted_wake_is_not_accepted_by_unrelated_completion(harness):
    receipt = asyncio.get_running_loop().create_future()
    turn = _QueuedTurn(prompt="not pasted", scheduler_delivery=receipt, scheduler_serialized=True)
    harness.session._scheduler_pending_turns.append(turn)
    await harness.session._handle_turn_complete(TurnResponse(text="unrelated"))
    assert not receipt.done()
    assert not harness.session.scheduler_wake_inflight(turn.prompt)


@pytest.mark.asyncio
async def test_exact_transcript_receipt_retains_late_authority_after_abandonment(harness):
    schedule, pending, durable = _schedule_fire(harness, age=3_601)
    turn, receipt = await _paste(harness, on_accept=durable.accept)
    scheduler = AgentScheduler(
        harness.registry,
        delivery_inflight_fn=lambda _name, prompt: harness.session.scheduler_wake_inflight(prompt),
    )
    harness.schedulers.append(scheduler)
    await scheduler._replay_pending_locked("worker")
    assert harness.registry.get_schedule_wake_by_fire(
        schedule.id, pending.fired_at,
    ).ledger_state == "abandoned"
    _append(harness, {"type": "user_message", "message": turn.prompt})
    await _read(harness)
    assert receipt.done() and receipt.result() is True
    assert harness.registry.get_schedule_wake_by_fire(
        schedule.id, pending.fired_at,
    ).ledger_state == "receipted-ran-once"
    assert not harness.session.scheduler_wake_inflight(turn.prompt)
    harness.tmux.paste_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["accepted", "rejected", "cancelled"])
async def test_completion_does_not_reaccept_terminal_receipt(harness, terminal):
    accept = MagicMock(return_value=True)
    turn, receipt = await _paste(harness, on_accept=accept)
    await _start(harness)
    if terminal == "accepted":
        _append(harness, {"type": "user_message", "message": turn.prompt})
        await _read(harness)
        accept.assert_called_once_with()
    elif terminal == "rejected":
        receipt.set_result(False)
    else:
        receipt.cancel()
    before = (turn.transport_accepted, receipt.cancelled(), accept.call_count)
    await _complete(harness)
    assert (turn.transport_accepted, receipt.cancelled(), accept.call_count) == before
    assert not harness.session.scheduler_wake_inflight(turn.prompt)
    if terminal != "cancelled":
        assert receipt.result() is (terminal == "accepted")
