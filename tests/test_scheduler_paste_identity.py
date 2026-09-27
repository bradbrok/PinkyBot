"""Probe: the marked-paste fence must survive agent-wide stale-drop notice changes."""

import asyncio
import inspect

import pytest

from pinky_daemon.scheduler import AgentScheduler, ScheduleWakeReceipt
from tests.test_scheduler_busy_bound import NOW, fire
from tests.test_scheduler_busy_bound import clock as clock
from tests.test_scheduler_busy_bound import registry as registry


@pytest.mark.parametrize("abandoned", [False, True])
@pytest.mark.parametrize("change", ["no-change-control", "notice-added-after-paste", "notice-cleared-after-paste"])
async def test_marked_paste_fence_survives_notice_change(registry, clock, change, abandoned):
    older = fire(registry, at=NOW - 1000, name="alpha")
    other = registry.add_schedule("worker", "0 * * * *", name="beta", prompt="execute beta")
    if change == "notice-cleared-after-paste":
        registry.record_recurring_schedule_stale_drop(
            other.id, agent_name="worker", schedule_name="beta",
            dropped_at=NOW - 2000, row_age_s=4000,
        )
    calls = []

    async def wake(_agent, _session, prompt):
        calls.append(prompt)
        return True

    pasted = {}
    engine = AgentScheduler(
        registry, wake_callback=wake,
        delivery_session_fn=lambda _: "current",
        delivery_inflight_fn=lambda _agent, prompt: prompt == pasted["text"],
    )
    # The transport holds exactly the text the delivery path built at paste time.
    pasted["text"], notices = engine._wake_prompt_with_recurring_stale_drops(older)
    kwargs = {}
    if "pasted_prompt" in inspect.signature(registry.mark_schedule_wake_pasted).parameters:
        kwargs["pasted_prompt"] = pasted["text"]
    assert registry.mark_schedule_wake_pasted(
        older.schedule_id, older.fired_at, pasted_at=NOW - 10, session_id="current", **kwargs,
    )
    if abandoned:
        registry.abandon_pending_schedule_wake(older.id, reason="RECEIPT_ABANDONED: fixture")
    newer, _ = registry.persist_schedule_wake(
        older.schedule_id, agent_name="worker", schedule_name="alpha",
        prompt="newer work", fired_at=NOW - 1,
    )
    # Between the paste and the next pass, the agent-wide notice set changes.
    if change == "notice-added-after-paste":
        registry.record_recurring_schedule_stale_drop(
            other.id, agent_name="worker", schedule_name="beta",
            dropped_at=NOW - 5, row_age_s=4000,
        )
    elif change == "notice-cleared-after-paste":
        registry.acknowledge_recurring_schedule_stale_drops("worker", notices)
    await engine._replay_pending_locked("worker")
    assert calls == [], "an unresolved older paste must still fence the next fire"
    assert ScheduleWakeReceipt(registry, older.schedule_id, older.fired_at).accept()
    row = registry.get_schedule_wake_by_fire(newer.schedule_id, newer.fired_at)
    assert row.attempts == row.accepted_at == 0
    await engine._replay_pending_locked("worker")
    assert len(calls) == 1
    assert registry.get_schedule_wake_by_fire(newer.schedule_id, newer.fired_at).accepted_at > 0


@pytest.mark.parametrize("release", ["ceiling", "session-change"])
async def test_legacy_empty_paste_identity_has_bounded_fence(registry, clock, monkeypatch, release):
    from pinky_daemon import scheduler

    older = fire(registry, at=NOW - 3595)
    assert registry.mark_schedule_wake_pasted(
        older.schedule_id, older.fired_at, pasted_at=NOW - 5, session_id="current",
    )
    newer, _ = registry.persist_schedule_wake(
        older.schedule_id, agent_name="worker", schedule_name=older.name,
        prompt="newer work", fired_at=NOW - 1,
    )
    logs, alerts, calls = [], [], []
    current = ["current"]
    monkeypatch.setattr(scheduler, "_log", logs.append)

    async def wake(_agent, _session, prompt):
        calls.append(prompt)
        return True

    async def notify(_agent, message):
        alerts.append(message)

    engine = AgentScheduler(
        registry, wake_callback=wake, delivery_session_fn=lambda _: current[0],
        delivery_inflight_fn=lambda *_: False, owner_notify_callback=notify,
    )
    await engine._replay_pending_locked("worker")
    assert calls == [], "missing physical identity must fence inside the live-session ceiling"
    row = registry.get_schedule_wake_by_fire(older.schedule_id, older.fired_at)
    assert row.attempts == row.accepted_at == row.abandoned_at == 0
    assert row.pasted_prompt == ""
    if release == "ceiling":
        clock.now += 6
        reason = "PASTED_PROMPT_UNKNOWN_RECEIPT_CEILING"
    else:
        current[0] = "replacement"
        reason = "PASTED_UNCONFIRMED_SESSION_LOST"
    await engine._replay_pending_locked("worker")
    await engine._replay_pending_locked("worker")
    if engine._owner_alert_tasks:
        await asyncio.gather(*engine._owner_alert_tasks)
    assert calls == [newer.prompt]
    row = registry.get_schedule_wake_by_fire(older.schedule_id, older.fired_at)
    assert row.abandoned_at > 0 and row.last_error.startswith(reason)
    assert row.accepted_at == row.attempts == 0
    assert sum(reason in line for line in logs) == 1
    assert sum(reason in line for line in alerts) == 1


@pytest.mark.parametrize("kind_name", ["TmuxSession", "CodexTmuxSession"])
async def test_exact_paste_identity_is_durable_before_handoff(registry, clock, tmp_path, monkeypatch, kind_name):
    from pinky_daemon.agent_registry import AgentRegistry
    from pinky_daemon.tmux_session import TmuxCommandResult
    from tests.test_scheduler_busy_bound import CodexTmuxSession, TmuxSession, session

    older = fire(registry)
    ss, tmux = session(tmp_path, {"TmuxSession": TmuxSession, "CodexTmuxSession": CodexTmuxSession}[kind_name])
    durable = ScheduleWakeReceipt(registry, older.schedule_id, older.fired_at)
    monkeypatch.setattr(ss, "_scheduler_pane_busy", lambda candidate=None: False)
    physical = "notice snapshot\n\n" + older.prompt
    seen = []

    async def paste(prompt, **_kwargs):
        # A second connection must see the entire marker before the transport runs.
        with_registry = AgentRegistry(db_path=str(tmp_path / "registry.db"))
        try:
            row = with_registry.get_schedule_wake_by_fire(older.schedule_id, older.fired_at)
            seen.append((row.pasted_prompt, row.pasted_at, row.pasted_session_id, row.accepted_at, row.attempts))
        finally:
            with_registry.close()
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    tmux.paste_text.side_effect = paste
    try:
        receipt = await ss.send_scheduler_prompt(physical, on_accept=durable.accept)
        await asyncio.gather(*ss._scheduler_delivery_tasks)
        assert seen == [(physical, NOW, ss._scheduler_paste_session_id, 0, 0)]
        assert not receipt.done()
        assert not registry.mark_schedule_wake_pasted(
            older.schedule_id, older.fired_at, pasted_at=NOW + 1,
            session_id="different", pasted_prompt="replacement text",
        )
        row = registry.get_schedule_wake_by_fire(older.schedule_id, older.fired_at)
        assert row.pasted_prompt == physical and row.pasted_at == NOW
    finally:
        await ss.disconnect()
