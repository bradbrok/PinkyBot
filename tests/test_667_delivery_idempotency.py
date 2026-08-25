"""#667 — durable inbound-delivery idempotency.

A message delivered to an agent's session exactly once must never be
re-delivered after a bounce. The in-memory ``transport_accepted`` fence
guarantees that within one process life, but a re-entry through the single
inbound entry point (``send``/``_queue_external_turn``) — a poller re-fetch on
an uncommitted offset, a broker re-route, an escalation re-feed — carries the
same durable platform id but not the in-memory fence, so without a durable
ledger it is re-pasted as a duplicate. These tests pin the durable guard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession, _QueuedTurn, _TmuxControl
from pinky_daemon.transport_state import SessionState


def _session(registry: AgentRegistry) -> TmuxSession:
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "pinky-review"
    control.kill_session = AsyncMock()
    cfg = StreamingSessionConfig(agent_name="review", working_dir="/tmp/667-review")
    session = TmuxSession(cfg, tmux_control=control, registry=registry)
    session._state_machine._state = SessionState.CONNECTED
    return session


def _queue_depth(session: TmuxSession) -> int:
    return len(session._message_queue._queue)  # type: ignore[attr-defined]


# ── Registry primitive ────────────────────────────────────────────────


def test_registry_mark_and_check_roundtrip(tmp_path):
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    assert reg.is_turn_delivered("review", "telegram", "123", "42") is False
    assert reg.mark_turn_delivered("review", "telegram", "123", "42") is True
    assert reg.is_turn_delivered("review", "telegram", "123", "42") is True
    # Idempotent second mark is a no-op.
    assert reg.mark_turn_delivered("review", "telegram", "123", "42") is False


def test_registry_empty_message_id_is_never_recorded(tmp_path):
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    assert reg.mark_turn_delivered("review", "telegram", "123", "") is False
    assert reg.is_turn_delivered("review", "telegram", "123", "") is False


def test_registry_scopes_by_agent_and_identity(tmp_path):
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    reg.mark_turn_delivered("review", "telegram", "123", "42")
    # Different agent, different chat, different message id are all distinct.
    assert reg.is_turn_delivered("other", "telegram", "123", "42") is False
    assert reg.is_turn_delivered("review", "telegram", "999", "42") is False
    assert reg.is_turn_delivered("review", "telegram", "123", "43") is False


def test_registry_prune_respects_retention(tmp_path):
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    reg.mark_turn_delivered("review", "telegram", "123", "42")
    # Nothing old enough yet.
    assert reg.prune_delivered_turns(retention_sec=3600) == 0
    assert reg.is_turn_delivered("review", "telegram", "123", "42") is True
    # A future 'now' pushes the row past retention.
    import time as _t

    removed = reg.prune_delivered_turns(retention_sec=0, now=_t.time() + 1)
    assert removed == 1
    assert reg.is_turn_delivered("review", "telegram", "123", "42") is False


# ── Session entry-point behavior ──────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_external_message_dropped_after_delivery(tmp_path):
    """The core guard: a re-delivered external message is not re-enqueued."""
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    session = _session(reg)
    msg = dict(platform="telegram", chat_id="123", message_id="42")

    # First delivery enqueues one turn.
    assert await session._queue_external_turn("hello", **msg) is True
    assert _queue_depth(session) == 1
    turn = session._message_queue._queue[-1]  # type: ignore[attr-defined]

    # Positive delivery evidence lands: the turn is durably recorded.
    session._mark_transport_accepted(turn)
    assert reg.is_turn_delivered("review", "telegram", "123", "42") is True

    # A bounce re-feeds the SAME message (same durable id). It must not
    # re-enqueue, and must report idempotent success (it WAS delivered) so the
    # caller commits its offset instead of retrying.
    depth_before = _queue_depth(session)
    stats_before = session._stats["messages_sent"]
    result = await session._queue_external_turn("hello", **msg)
    assert result is True
    assert _queue_depth(session) == depth_before
    # Suppression happens before any side effect: the stats counter must not
    # advance for a dropped duplicate.
    assert session._stats["messages_sent"] == stats_before


def test_scheduler_turn_with_message_id_is_not_recorded(tmp_path):
    """A scheduler-serialized turn is never written to the ledger, even if it
    somehow carries a message_id — the mark site guards on scheduler_serialized."""
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    session = _session(reg)
    turn = _QueuedTurn(
        prompt="wake",
        platform="telegram",
        chat_id="123",
        message_id="900",
        scheduler_serialized=True,
    )
    session._mark_transport_accepted(turn)
    assert turn.transport_accepted is True
    assert reg.is_turn_delivered("review", "telegram", "123", "900") is False


def test_mark_failure_does_not_block_acceptance(tmp_path):
    """A ledger write error must never fail an otherwise-accepted delivery."""

    class _BoomRegistry:
        def mark_turn_delivered(self, *a, **k):
            raise RuntimeError("ledger down")

    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    session = _session(reg)
    session._registry = _BoomRegistry()  # type: ignore[assignment]
    turn = _QueuedTurn(
        prompt="hi", platform="telegram", chat_id="123", message_id="42"
    )
    assert session._mark_transport_accepted(turn) is True
    assert turn.transport_accepted is True


@pytest.mark.asyncio
async def test_distinct_message_is_still_delivered(tmp_path):
    """A different message id must never be suppressed by a prior delivery."""
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    session = _session(reg)
    reg.mark_turn_delivered("review", "telegram", "123", "42")

    assert (
        await session._queue_external_turn(
            "second", platform="telegram", chat_id="123", message_id="43"
        )
        is True
    )
    assert _queue_depth(session) == 1


@pytest.mark.asyncio
async def test_same_text_distinct_message_ids_both_delivered(tmp_path):
    """Two genuine sends of identical text must both land.

    Dedup keys on the platform message id, never on content — a user who
    sends "yes" twice in succession produces two distinct platform message
    ids, so both are delivered. Only the SAME message id arriving twice (a
    real redelivery after a bounce) is suppressed.
    """
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    session = _session(reg)

    assert (
        await session._queue_external_turn(
            "yes", platform="telegram", chat_id="123", message_id="100"
        )
        is True
    )
    first = session._message_queue._queue[-1]  # type: ignore[attr-defined]
    session._mark_transport_accepted(first)

    # Same text, next message — a different platform message id.
    assert (
        await session._queue_external_turn(
            "yes", platform="telegram", chat_id="123", message_id="101"
        )
        is True
    )
    assert _queue_depth(session) == 2


@pytest.mark.asyncio
async def test_empty_message_id_is_never_suppressed(tmp_path):
    """Internal/wake/scheduler/approval-drain turns have no id and always pass."""
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    session = _session(reg)

    # Two internal-style sends with empty message_id both enqueue.
    assert await session._queue_external_turn("wake-a", platform="", chat_id="") is True
    assert await session._queue_external_turn("wake-b", platform="", chat_id="") is True
    assert _queue_depth(session) == 2


@pytest.mark.asyncio
async def test_no_registry_falls_back_to_legacy_behavior(tmp_path):
    """With no registry wired, the entry point must not raise — just enqueue."""
    cfg = StreamingSessionConfig(agent_name="review", working_dir="/tmp/667-review")
    control = MagicMock(spec=_TmuxControl)
    control.kill_session = AsyncMock()
    session = TmuxSession(cfg, tmux_control=control)  # registry=None
    session._state_machine._state = SessionState.CONNECTED

    assert (
        await session._queue_external_turn(
            "hello", platform="telegram", chat_id="123", message_id="42"
        )
        is True
    )
    assert _queue_depth(session) == 1
