"""Design-limit probe: idle notification cannot replace acceptance evidence."""

import asyncio

import pytest

from pinky_daemon.scheduler import AgentScheduler
from tests.test_codex_scheduler_idle_receipt import (
    NOW,
    _append,
    _complete,
    _drain_delivery,
    _paste,
    _read,
    _schedule_fire,
    _start,
)
from tests.test_codex_scheduler_idle_receipt import harness as harness


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_user_message", [False, True])
async def test_idle_replay_requires_independent_acceptance(
    harness, matching_user_message,
):
    # Exercise the real transport policy; no acceptance method is patched.
    schedule, older, durable = _schedule_fire(
        harness, age=3_601, prompt="older work",
    )
    turn, receipt = await _paste(
        harness, prompt=older.prompt, on_accept=durable.accept,
    )
    await _start(harness)
    submitted = []

    async def deliver_newer(agent_name, session_id, prompt, *, schedule_receipt):
        submitted.append(prompt)
        next_receipt = await harness.session.send_scheduler_prompt(
            prompt, on_accept=schedule_receipt.accept,
        )
        await _drain_delivery(harness)
        _append(harness, {"type": "user_message", "message": prompt})
        await _read(harness)
        return next_receipt

    scheduler = AgentScheduler(
        harness.registry, wake_callback=deliver_newer,
        delivery_inflight_fn=lambda _name, prompt: (
            harness.session.scheduler_wake_inflight(prompt)
        ),
    )
    harness.schedulers.append(scheduler)
    await scheduler._replay_pending_locked("worker")
    assert harness.registry.get_schedule_wake_by_fire(
        schedule.id, older.fired_at,
    ).ledger_state == "abandoned"
    newer, _ = harness.registry.persist_schedule_wake(
        schedule.id, agent_name="worker", schedule_name=schedule.name,
        prompt="newer distinct work", fired_at=NOW - 1,
    )
    if matching_user_message:
        # Genuine existing authority, arriving after abandonment.
        _append(harness, {"type": "user_message", "message": turn.prompt})
        await _read(harness)

    idle_notifications = []

    def on_idle(agent_name):
        idle_notifications.append(agent_name)
        scheduler.notify_agent_idle(agent_name)

    harness.session._config.on_turn_idle = on_idle
    await _complete(harness)
    replay_tasks = list(scheduler._pending_replay_tasks.values())
    assert replay_tasks
    await asyncio.wait_for(asyncio.gather(*replay_tasks), timeout=5)
    assert idle_notifications == ["worker"]
    assert receipt.done() is matching_user_message
    assert turn.transport_accepted is matching_user_message
    assert harness.session.scheduler_wake_inflight(turn.prompt) is (
        not matching_user_message
    )
    assert submitted == ([newer.prompt] if matching_user_message else [])
    row = harness.registry.get_schedule_wake_by_fire(schedule.id, newer.fired_at)
    if matching_user_message:
        assert receipt.result() is True
        assert row.ledger_state == "receipted-ran-once"
    else:
        assert row.attempts == 0
