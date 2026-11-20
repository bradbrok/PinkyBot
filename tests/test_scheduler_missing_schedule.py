"""Orphaned schedule receipts retain acceptance and emit a diagnostic."""

import pytest

from tests.test_schedule_fire_trace import fire
from tests.test_schedule_fire_trace import registry as registry


@pytest.mark.parametrize("by_fire", [False, True])
def test_missing_schedule_row_keeps_positive_receipt(registry, capsys, by_fire):
    pending, _ = fire(registry)
    assert registry.remove_schedule(pending.schedule_id)
    assert registry.get_schedule_wake_by_fire(pending.schedule_id, pending.fired_at) is not None
    delivered_at = pending.fired_at + 30
    if by_fire:
        result = registry.confirm_pending_schedule_wake_by_fire(
            pending.schedule_id, pending.fired_at, delivered_at=delivered_at,
        )
    else:
        result = registry.confirm_pending_schedule_wake(pending.id, delivered_at=delivered_at)
    assert result is True
    assert registry.get_schedule_wake_by_fire(
        pending.schedule_id, pending.fired_at,
    ).accepted_at == delivered_at
    assert not registry._db.in_transaction
    expected = (
        "agent_registry: SCHEDULE_ROW_MISSING "
        f"pending #{pending.id}, schedule #{pending.schedule_id}, fired_at={pending.fired_at}"
    )
    assert capsys.readouterr().err.splitlines().count(expected) == 1
