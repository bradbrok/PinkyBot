"""Receipt diagnostics must not repeatedly page an owner about healthy Codex work."""

from __future__ import annotations

import asyncio
import time

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.scheduler import AgentScheduler


@pytest.fixture
def registry(tmp_path):
    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    yield registry
    registry.close()


async def flush_alerts(scheduler):
    await asyncio.gather(*list(scheduler._owner_alert_tasks))


@pytest.mark.parametrize(
    ("runtime", "transport", "pages"),
    [
        ("codex_cli", "sdk", 0),
        ("codex_cli", "tmux", 0),
        ("claude_sdk", "sdk", 1),
        ("claude_sdk", "tmux", 1),
    ],
)
@pytest.mark.asyncio
async def test_receipt_expiry_pages_only_receipt_capable_runtime(
    registry,
    capsys,
    runtime,
    transport,
    pages,
):
    # A GPT model through a Claude runtime still has Claude receipt semantics.
    registry.register("worker", runtime=runtime, transport=transport, model="gpt-5.6-luna")
    schedule = registry.add_schedule("worker", "* * * * *", name="receipt", prompt="work")
    schedule.last_run = time.time()
    registry.update_schedule_last_run(schedule.id, schedule.last_run)
    receipt = asyncio.get_running_loop().create_future()
    alerts = []

    async def wake(*args):
        return receipt

    async def notify(agent, message):
        alerts.append((agent, message))
        return True

    scheduler = AgentScheduler(
        registry,
        wake_callback=wake,
        owner_notify_callback=notify,
        delivery_inflight_fn=lambda *args: True,
        schedule_delivery_timeout=0.005,
        receipt_extension_attempt_cap=1,
    )
    try:
        await scheduler._deliver_schedule(schedule)
        await flush_alerts(scheduler)
        row = registry.get_schedule_wake_by_fire(schedule.id, schedule.last_run)
        assert row.ledger_state == "abandoned"
        assert row.accepted_at == 0
        assert not receipt.cancelled()
        assert len(alerts) == pages
        logs = capsys.readouterr().err
        assert "RECEIPT_EXTENSION_EXPIRED" in logs
        if runtime == "codex_cli":
            assert "OWNER_NOTIFY_DEMOTED" in logs
            assert "codex_cli" in logs
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_recurring_receipt_pages_once_per_schedule_per_hour(registry, monkeypatch, capsys):
    registry.register("worker", runtime="claude_sdk", transport="tmux")
    schedule = registry.add_schedule("worker", "*/15 * * * *", name="recurring", prompt="work")
    other = registry.add_schedule("worker", "*/15 * * * *", name="independent", prompt="other")
    clock = [1_800_000_000.0]
    monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: clock[0])
    alerts = []

    async def notify(agent, message):
        alerts.append(message)
        return True

    scheduler = AgentScheduler(registry, owner_notify_callback=notify)

    def expire(item, offset):
        clock[0] = 1_800_000_000.0 + offset
        item.last_run = clock[0] - 1
        registry.persist_schedule_wake(
            item.id,
            agent_name="worker",
            schedule_name=item.name,
            prompt=item.prompt,
            fired_at=item.last_run,
        )
        scheduler._mark_abandoned_receipt(item, age=1800, wait_attempts=3)
        assert registry.get_schedule_wake_by_fire(item.id, item.last_run).abandoned_at > 0

    try:
        for offset in (0, 900, 1800, 3599):
            expire(schedule, offset)
        await flush_alerts(scheduler)
        assert len(alerts) == 1, "recurring fires must share one hourly page budget"
        assert capsys.readouterr().err.count("RECEIPT_ABANDONED schedule") == 4
        expire(other, 3599)
        await flush_alerts(scheduler)
        assert len(alerts) == 2, "one noisy schedule must not silence another"
        expire(schedule, 3600)
        await flush_alerts(scheduler)
        assert len(alerts) == 3, "the exact one-hour boundary rearms the page"
        clock[0] += 3600
        scheduler._alert_receipt_extension_expired(
            schedule,
            age=5400,
            bound_reason="wall-clock cap",
            wait_attempts=3,
        )
        await flush_alerts(scheduler)
        assert len(alerts) == 3, "the same exact fire never earns another page"
    finally:
        await scheduler.stop()


@pytest.mark.parametrize(("runtime", "pages"), [("codex_cli", 0), ("claude_sdk", 1)])
@pytest.mark.asyncio
async def test_drain_extension_pages_only_receipt_capable_runtime(
    registry,
    monkeypatch,
    capsys,
    runtime,
    pages,
):
    registry.register("worker", runtime=runtime, transport="tmux")
    schedule = registry.add_schedule("worker", "* * * * *", name="backlog", prompt="work")
    clock = [1_800_000_000.0]
    monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: clock[0])
    alerts = []

    async def notify(agent, message):
        alerts.append(message)
        return True

    def pending(fired_at):
        return registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=schedule.name,
            prompt=schedule.prompt,
            fired_at=fired_at,
        )[0]

    rows = [pending(clock[0] - 600), pending(clock[0] - 500)]
    scheduler = AgentScheduler(
        registry,
        owner_notify_callback=notify,
        delivery_drain_busy_fn=lambda _: True,
        outbox_drain_extension_attempt_cap=1,
    )
    try:
        await scheduler._replay_pending_locked("worker", drain_recheck=True)
        await flush_alerts(scheduler)
        assert len(alerts) == pages
        assert all(
            registry.get_schedule_wake_by_fire(schedule.id, r.fired_at).drain_parked_at > 0
            for r in rows
        )

        # A still-busy follow-up retains the actual parked state and never
        # repeats the first notification for this same parked episode.
        clock[0] += 60
        await scheduler._replay_pending_locked("worker", drain_recheck=True)
        await flush_alerts(scheduler)
        assert len(alerts) == pages
        assert all(
            registry.get_schedule_wake_by_fire(schedule.id, r.fired_at).drain_parked_at > 0
            for r in rows
        )
        if runtime == "codex_cli":
            # The known park/unpark churn remains unchanged. Even when a
            # confirmed delivery releases the cohort, its next expiry is
            # still an operator-only diagnostic on this runtime.
            confirmation = pending(clock[0])
            assert registry.confirm_pending_schedule_wake_by_fire(
                schedule.id, confirmation.fired_at
            )
            assert all(
                registry.get_schedule_wake_by_fire(schedule.id, r.fired_at).drain_parked_at == 0
                for r in rows
            )
            clock[0] += 60
            await scheduler._replay_pending_locked("worker", drain_recheck=True)
            await flush_alerts(scheduler)
            assert alerts == []
            assert all(
                registry.get_schedule_wake_by_fire(schedule.id, r.fired_at).drain_parked_at > 0
                for r in rows
            )
        logs = capsys.readouterr().err
        assert "OUTBOX_DRAIN_EXTENSION_EXPIRED" in logs
        if runtime == "codex_cli":
            assert "OWNER_NOTIFY_DEMOTED" in logs
    finally:
        await scheduler.stop()


@pytest.mark.parametrize("lookup", ["missing", "failure"])
@pytest.mark.asyncio
async def test_unknown_runtime_does_not_silence_receipt_expiry(registry, monkeypatch, lookup):
    registry.register("worker")
    schedule = registry.add_schedule("worker", "* * * * *", name="unknown", prompt="work")
    schedule.last_run = time.time()
    alerts = []

    async def notify(agent, message):
        alerts.append(message)
        return True

    def get(name):
        if lookup == "failure":
            raise RuntimeError("registry lookup failed")
        return None

    monkeypatch.setattr(registry, "get", get)
    scheduler = AgentScheduler(registry, owner_notify_callback=notify)
    try:
        scheduler._alert_receipt_extension_expired(
            schedule,
            age=1800,
            bound_reason="attempt cap",
            wait_attempts=3,
        )
        await flush_alerts(scheduler)
        assert len(alerts) == 1
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_codex_real_stale_one_shot_and_dead_letter_still_page(registry):
    registry.register("worker", runtime="codex_cli")
    schedule = registry.add_schedule("worker", "* * * * *", name="owed", prompt="work")
    pending, _ = registry.persist_schedule_wake(
        schedule.id,
        agent_name="worker",
        schedule_name=schedule.name,
        prompt=schedule.prompt,
        fired_at=time.time(),
    )
    alerts = []

    async def notify(agent, message):
        alerts.append(message)
        return True

    scheduler = AgentScheduler(registry, owner_notify_callback=notify)
    try:
        scheduler._alert_stale_one_shot_drop(
            agent_name="worker",
            schedule_id=schedule.id,
            schedule_name=schedule.name,
            pending_id=pending.id,
            row_age=3601,
            replay_max_age=3600,
        )
        assert scheduler._park_pending_wake_if_capped(pending, scheduler.PERSISTED_WAKE_ATTEMPT_CAP)
        await flush_alerts(scheduler)
        assert len(alerts) == 2
        assert "STALE ONE-SHOT WAKE DROPPED" in alerts[0]
        assert "PERSISTED WAKE PARKED" in alerts[1]
        assert registry.get_schedule_wake_by_fire(schedule.id, pending.fired_at).parked_at > 0
    finally:
        await scheduler.stop()


@pytest.mark.parametrize("release", ["confirmed-delivery", "verified-idle"])
@pytest.mark.asyncio
async def test_claude_drain_cohort_repark_does_not_page_again(
    registry, monkeypatch, capsys, release
):
    registry.register("worker", runtime="claude_sdk", transport="tmux")
    clock = [1_800_000_000.0]
    monkeypatch.setattr("pinky_daemon.scheduler.time.time", lambda: clock[0])
    busy = [True]
    alerts = []

    def pending(name, fired_at):
        schedule = registry.add_schedule("worker", "* * * * *", name=name, prompt="work")
        return registry.persist_schedule_wake(
            schedule.id,
            agent_name="worker",
            schedule_name=name,
            prompt="work",
            fired_at=fired_at,
        )[0]

    async def wake(*args):
        return False

    async def notify(agent, message):
        alerts.append(message)
        return True

    rows = [pending("first", clock[0] - 30), pending("second", clock[0] - 20)]
    scheduler = AgentScheduler(
        registry,
        wake_callback=wake,
        delivery_drain_busy_fn=lambda _: busy[0],
        owner_notify_callback=notify,
        outbox_drain_extension_attempt_cap=1,
    )

    def row_state(row):
        return registry.get_schedule_wake_by_fire(row.schedule_id, row.fired_at)

    try:
        await scheduler._replay_pending_locked("worker", drain_recheck=True)
        await flush_alerts(scheduler)
        assert len(alerts) == 1, "a genuine Claude drain expiry must still page"
        assert all(row_state(row).drain_parked_at > 0 for row in rows)

        if release == "confirmed-delivery":
            # Settling the oldest fire releases the remaining member of the
            # already-notified cohort, whose new oldest timestamp differs.
            first = rows.pop(0)
            assert registry.confirm_pending_schedule_wake_by_fire(first.schedule_id, first.fired_at)
        else:
            busy[0] = False
            await scheduler._replay_pending_locked("worker", drain_recheck=True)
        assert all(row_state(row).drain_parked_at == 0 for row in rows)

        busy[0] = True
        clock[0] += 60
        await scheduler._replay_pending_locked("worker", drain_recheck=True)
        await flush_alerts(scheduler)
        assert all(row_state(row).drain_parked_at > 0 for row in rows)
        assert len(alerts) == 1, "re-parking the notified cohort must not page again"
        assert "OUTBOX_DRAIN_EXTENSION_PAGE_DEDUP" in capsys.readouterr().err

        for row in rows:
            assert registry.confirm_pending_schedule_wake_by_fire(row.schedule_id, row.fired_at)
        newer = pending("new-cohort", clock[0])
        clock[0] += 60
        await scheduler._replay_pending_locked("worker", drain_recheck=True)
        await flush_alerts(scheduler)
        assert row_state(newer).drain_parked_at > 0
        assert len(alerts) == 2, "a genuinely newer cohort must earn its first page"
    finally:
        await scheduler.stop()
