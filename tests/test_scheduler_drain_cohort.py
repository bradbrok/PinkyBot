"""A spared fresh wake keeps its own drain budget across subsequent cycles."""

import pytest

from pinky_daemon.scheduler import AgentScheduler, _OutboxDrainExtensionState
from tests.test_scheduler_busy_bound import NOW, fire
from tests.test_scheduler_busy_bound import clock as clock
from tests.test_scheduler_busy_bound import registry as registry

CAP_AGE = 1800.0


@pytest.mark.parametrize("inherited", [29, 5])
def test_spared_fresh_row_is_not_parked_by_inherited_attempts(registry, clock, inherited):
    stale = fire(registry, name="stale")
    engine = AgentScheduler(registry)
    engine._outbox_drain_extensions["worker"] = _OutboxDrainExtensionState(NOW, attempts=inherited)
    clock.now = NOW + CAP_AGE
    fresh = fire(registry, name="fresh", at=clock.now)  # created at the parking boundary

    summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
    assert engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
    assert registry.get_schedule_wake_by_fire(stale.schedule_id, NOW).drain_parked_at > 0
    assert registry.get_schedule_wake_by_fire(fresh.schedule_id, fresh.fired_at).drain_parked_at == 0

    # Every later 60 s drain cycle while the fresh row is younger than its own budget.
    for k in range(1, 30):
        clock.now = fresh.fired_at + 60 * k
        summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
        engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
        row = registry.get_schedule_wake_by_fire(fresh.schedule_id, fresh.fired_at)
        assert row.drain_parked_at == 0, (
            f"fresh row parked {60 * k}s after creation (inherited attempts={inherited}, "
            f"state={engine._outbox_drain_extensions.get('worker')})"
        )


def test_relist_failure_does_not_adopt_spared_fresh_row(registry, clock, monkeypatch):
    stale = fire(registry, name="stale")
    engine = AgentScheduler(registry)
    engine._outbox_drain_extensions["worker"] = _OutboxDrainExtensionState(NOW, attempts=29)
    clock.now = NOW + CAP_AGE
    fresh = fire(registry, name="fresh", at=clock.now)
    real = registry.list_pending_schedule_wakes
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # the post-park durable re-list
            raise RuntimeError("transient re-list failure")
        return real(*a, **kw)

    monkeypatch.setattr(registry, "list_pending_schedule_wakes", flaky)
    summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
    assert engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
    assert calls["n"] == 2
    assert registry.get_schedule_wake_by_fire(stale.schedule_id, NOW).drain_parked_at > 0
    for k in range(1, 30):
        clock.now = fresh.fired_at + 60 * k
        summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
        engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
        row = registry.get_schedule_wake_by_fire(fresh.schedule_id, fresh.fired_at)
        assert row.drain_parked_at == 0, f"re-list failure adopted the fresh row; parked at {60 * k}s"


def test_spared_row_parks_on_its_own_budget_without_repage(registry, clock, monkeypatch):
    """Guard pin: the spared row is bounded by ITS OWN budget, and its later park stays under
    the cohort page already sent (no per-row page storm on a continuously busy transport)."""
    fire(registry, name="stale")
    engine = AgentScheduler(registry)
    pages = []
    monkeypatch.setattr(engine, "_queue_receipt_extension_owner_alert",
                        lambda agent, message: pages.append(message) or True)
    engine._outbox_drain_extensions["worker"] = _OutboxDrainExtensionState(NOW, attempts=29)
    clock.now = NOW + CAP_AGE
    fresh = fire(registry, name="fresh", at=clock.now)
    summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
    engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
    assert len(pages) == 1
    parked_at_k = None
    for k in range(1, 32):
        clock.now = fresh.fired_at + 60 * k
        rows = registry.get_pending_schedule_wake_health("worker", now=clock.now)
        if not rows or not int(rows[0]["count"]):
            break
        engine._record_outbox_drain_extension("worker", summary=rows[0], now=clock.now)
        if registry.get_schedule_wake_by_fire(fresh.schedule_id, fresh.fired_at).drain_parked_at:
            parked_at_k = k
            break
    assert parked_at_k is not None and parked_at_k <= 30, "spared row never bounded"
    assert len(pages) == 1, f"spared row re-paged the same cohort (pages={len(pages)})"


@pytest.mark.parametrize("relist_failure", [False, True])
def test_targeted_failed_write_retains_episode_history(registry, clock, monkeypatch, relist_failure):
    oldest = fire(registry, name="oldest")
    failed = fire(registry, name="targeted failure", at=NOW + 5)
    clock.now = NOW + CAP_AGE + 60
    fresh = fire(registry, name="fresh", at=clock.now)
    engine = AgentScheduler(registry)
    engine._outbox_drain_extensions["worker"] = _OutboxDrainExtensionState(NOW, attempts=29)
    original = engine._outbox_drain_extensions["worker"]
    attempted, pages = [], []
    real_park = registry.drain_park_pending_schedule_wake
    wedged = [True]

    def park(row_id, **kwargs):
        attempted.append(row_id)
        if row_id == failed.id and wedged[0]:
            raise RuntimeError("synthetic targeted UPDATE failure")
        return real_park(row_id, **kwargs)

    real_list = registry.list_pending_schedule_wakes
    list_calls = []

    def listing(*args, **kwargs):
        list_calls.append(1)
        if relist_failure and len(list_calls) == 2:
            raise RuntimeError("synthetic post-park read failure")
        return real_list(*args, **kwargs)

    monkeypatch.setattr(registry, "drain_park_pending_schedule_wake", park)
    monkeypatch.setattr(registry, "list_pending_schedule_wakes", listing)
    monkeypatch.setattr(engine, "_queue_receipt_extension_owner_alert",
                        lambda agent, message: pages.append(message) or True)
    summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
    assert engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
    assert attempted == [oldest.id, failed.id], "the aged failed row must actually be targeted"
    assert engine._outbox_drain_extensions["worker"] is original
    assert original.attempts == 30 and original.alerted
    assert registry.get_schedule_wake_by_fire(failed.schedule_id, failed.fired_at).drain_parked_at == 0
    assert registry.get_schedule_wake_by_fire(fresh.schedule_id, fresh.fired_at).drain_parked_at == 0
    assert len(pages) == 1
    wedged[0] = False
    clock.now += 60
    summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
    assert engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
    assert attempted == [oldest.id, failed.id, failed.id]
    assert original.attempts == 31, "a failed target must retain its existing attempt history"
    assert registry.get_schedule_wake_by_fire(failed.schedule_id, failed.fired_at).drain_parked_at > 0
    assert registry.get_schedule_wake_by_fire(fresh.schedule_id, fresh.fired_at).drain_parked_at == 0
    assert len(pages) == 1
