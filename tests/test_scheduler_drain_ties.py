"""Same-time fires retain counted checks; later fires start a new attempt budget."""

import pytest

from pinky_daemon.scheduler import AgentScheduler, _OutboxDrainExtensionState
from tests.test_scheduler_busy_bound import NOW, fire
from tests.test_scheduler_busy_bound import clock as clock
from tests.test_scheduler_busy_bound import registry as registry


@pytest.mark.parametrize("relist_failure", [False, True])
def test_equal_fire_sibling_inherits_counted_attempts(registry, clock, monkeypatch, relist_failure):
    oldest = fire(registry, name="first")
    spared = fire(registry, name="same-time-spared")
    clock.now = NOW + 60
    engine = AgentScheduler(registry)
    engine._outbox_drain_extensions["worker"] = _OutboxDrainExtensionState(NOW, attempts=29)
    pages, writes = [], []
    real_list = registry.list_pending_schedule_wakes
    real_park = registry.drain_park_pending_schedule_wake
    count = 0

    def listing(*args, **kwargs):
        nonlocal count
        count += 1
        if relist_failure and count == 2:
            raise RuntimeError("post-park read unavailable")
        return real_list(*args, **kwargs)

    def parking(row_id, **kwargs):
        writes.append(row_id)
        return real_park(row_id, **kwargs)

    monkeypatch.setattr(registry, "list_pending_schedule_wakes", listing)
    monkeypatch.setattr(registry, "drain_park_pending_schedule_wake", parking)
    monkeypatch.setattr(
        engine, "_queue_receipt_extension_owner_alert", lambda *args: pages.append(args) or True
    )
    summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
    assert engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
    assert writes == [oldest.id]
    assert (
        registry.get_schedule_wake_by_fire(spared.schedule_id, spared.fired_at).drain_parked_at == 0
    )
    clock.now += 60
    summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
    engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
    assert (
        registry.get_schedule_wake_by_fire(spared.schedule_id, spared.fired_at).drain_parked_at > 0
    ), "same-time sibling must retain the counted checks"
    assert writes == [oldest.id, spared.id]
    assert len(pages) == 1


@pytest.mark.parametrize("same_time", [False, True])
@pytest.mark.parametrize("relist_failure", [False, True])
def test_configured_cap_preserves_ties_and_spares_later_fires(
    registry, clock, monkeypatch, same_time, relist_failure
):
    first = fire(registry, name="first")
    second = fire(registry, name="second", at=NOW if same_time else NOW + 1)
    engine = AgentScheduler(registry, outbox_drain_extension_attempt_cap=3)
    pages, writes = [], []
    real_list = registry.list_pending_schedule_wakes
    real_park = registry.drain_park_pending_schedule_wake
    count = 0

    def listing(*args, **kwargs):
        nonlocal count
        count += 1
        if relist_failure and count == 2:
            raise RuntimeError("post-park read unavailable")
        return real_list(*args, **kwargs)

    def parking(row_id, **kwargs):
        writes.append(row_id)
        return real_park(row_id, **kwargs)

    monkeypatch.setattr(registry, "list_pending_schedule_wakes", listing)
    monkeypatch.setattr(registry, "drain_park_pending_schedule_wake", parking)
    monkeypatch.setattr(
        engine, "_queue_receipt_extension_owner_alert", lambda *args: pages.append(args) or True
    )
    final_cycle = 4 if same_time else 6
    episode = None
    for cycle in range(1, final_cycle + 1):
        clock.now = NOW + cycle * 60
        summary = registry.get_pending_schedule_wake_health("worker", now=clock.now)[0]
        engine._record_outbox_drain_extension("worker", summary=summary, now=clock.now)
        if cycle == 2:
            episode = engine._outbox_drain_extensions["worker"]
        expected = [] if cycle < 3 else [first.id] if cycle < final_cycle else [first.id, second.id]
        assert writes == expected, f"cycle {cycle}: only later fires receive a new attempt budget"
        row = registry.get_schedule_wake_by_fire(second.schedule_id, second.fired_at)
        assert bool(row.drain_parked_at) == (cycle == final_cycle)
        if same_time and cycle >= 3:
            assert episode.attempts == cycle
    assert len(pages) == 1
