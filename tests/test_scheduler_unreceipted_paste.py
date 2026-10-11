"""A completed unrelated turn must not strand later serialized wakes."""

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import scheduler, tmux_session
from pinky_daemon.agent_registry import AgentRegistry
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

    for module in (scheduler, tmux_session):
        monkeypatch.setattr(module, "time", SimpleNamespace(
            **{**vars(module.time), "time": lambda: clock.now},
        ))
        monkeypatch.setattr(module, "asyncio", SimpleNamespace(
            **{**vars(asyncio), "sleep": sleep},
        ))
    return clock


async def _make_harness(tmp_path, clock):
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "test-receipt"
    ok = TmuxCommandResult(returncode=0, stdout="", stderr="")
    control.paste_text = AsyncMock(return_value=ok)
    control.kill_session = AsyncMock(return_value=ok)
    control.capture_pane = AsyncMock(return_value=ok)
    config = StreamingSessionConfig(
        agent_name="worker", working_dir=str(tmp_path),
        live_status_fn=lambda: {"status": clock.status, "last_updated": clock.updated},
    )
    session = TmuxSession(config, tmux_control=control)
    transition = await session._state_machine.request_transition(SessionState.BOOTING, Trigger.BOOT)
    await session._state_machine.transition_complete(
        transition.owner_token, SessionState.CONNECTED, trigger=Trigger.BOOT_COMPLETE,
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    # Attach a local tailer without starting a process or discovery loop.
    session._tailer = TmuxTranscriptTailer(
        transcript, session._handle_turn_complete, on_entry=session._on_transcript_entry,
    )
    registry = AgentRegistry(db_path=str(tmp_path / "registry.db"))
    registry.register("worker")
    return SimpleNamespace(
        session=session, control=control, transcript=transcript, registry=registry,
        clock=clock, tasks=[],
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


async def _first_paste(harness, *, owned=False):
    callback = None
    durable = None
    if owned:
        schedule = harness.registry.add_schedule(
            "worker", "0 * * * *", name="periodic", prompt=FIRST,
        )
        harness.registry.persist_schedule_wake(
            schedule.id, agent_name="worker", schedule_name=schedule.name,
            prompt=FIRST, fired_at=NOW,
        )
        durable = ScheduleWakeReceipt(harness.registry, schedule.id, NOW)
        callback = MagicMock(side_effect=durable.accept)
    receipt = await harness.session.send_scheduler_prompt(FIRST, on_accept=callback)
    await asyncio.wait_for(
        asyncio.gather(*list(harness.session._scheduler_delivery_tasks)), timeout=5,
    )
    assert [call.args[0] for call in harness.control.paste_text.await_args_list] == [FIRST]
    entry = harness.session._inflight_metas[0]
    assert entry.turn.pane_delivery_started and entry.turn.pane_delivery_recorded
    assert not receipt.done()
    return SimpleNamespace(receipt=receipt, entry=entry, callback=callback, durable=durable)


async def _unrelated_stop(harness, first):
    harness.clock.now += 1
    harness.clock.status = "working"
    harness.clock.updated = harness.clock.now
    await _rows(harness, {
        "type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "unrelated work"}],
        },
    })
    harness.clock.now = NOW + 12
    harness.clock.status = "idle"
    harness.clock.updated = harness.clock.now
    await _rows(harness, {"type": "system", "subtype": "stop_hook_summary"})
    assert not harness.session._inflight_metas
    assert harness.session._tailer.stats["turns_fired"] == 1
    assert not first.receipt.done()
    if first.callback is not None:
        first.callback.assert_not_called()
    assert harness.clock.updated > first.entry.paste_succeeded_at


async def _second_paste(harness, *, expired=False):
    callback = MagicMock(return_value=True)
    receipt = await harness.session.send_scheduler_prompt(
        SECOND, on_accept=callback,
        busy_deliver_at=harness.clock.now + (-1 if expired else 600),
    )
    await _settle()
    return receipt


async def _advance(harness, horizon):
    for offset in (*range(60, horizon, 60), horizon):
        harness.clock.now = NOW + offset
        harness.clock.updated = harness.clock.now
        await _settle()


@pytest.mark.parametrize("horizon", [660, 1_800, HORIZON, 14_400])
async def test_r1_ownerless_lost_paste_releases_later_wake(harness, horizon):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
    await _second_paste(harness)
    await _advance(harness, horizon)
    assert [call.args[0] for call in harness.control.paste_text.await_args_list] == [FIRST, SECOND]


@pytest.mark.parametrize("horizon", [660, 1_800, HORIZON, 14_400])
async def test_r2_owned_lost_paste_releases_later_wake(harness, horizon):
    first = await _first_paste(harness, owned=True)
    await _unrelated_stop(harness, first)
    await _second_paste(harness)
    await _advance(harness, horizon)
    first.callback.assert_not_called()
    assert [call.args[0] for call in harness.control.paste_text.await_args_list] == [FIRST, SECOND]


async def test_r3_real_receipt_wait_confirms_later_fire(harness, monkeypatch):
    first = await _first_paste(harness)
    await _unrelated_stop(harness, first)
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
        harness.clock.now += timeout
        harness.clock.updated = harness.clock.now
        await _settle()
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
