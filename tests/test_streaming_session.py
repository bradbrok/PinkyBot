"""Tests for StreamingSession context-check behavior.

Focuses on _check_context() — the warn/restart logic that was buggy pre-fix:
  - warn flag was set before the query attempt (silent failure, no retry)
  - if/elif structure skipped warn on single-turn overshoot
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig


def _make_session(
    *,
    warn_pct: int = 40,
    restart_pct: int = 80,
) -> StreamingSession:
    cfg = StreamingSessionConfig(
        agent_name="test",
        context_warn_pct=warn_pct,
        context_restart_pct=restart_pct,
    )
    ss = StreamingSession(cfg)
    ss._connected = True
    ss._client = MagicMock()
    ss._client.query = AsyncMock()
    # Stub force_restart so tests don't need a real connect loop
    ss.force_restart = AsyncMock(return_value=True)
    return ss


def _stub_ctx(ss: StreamingSession, pct: int, *, max_tokens: int = 200_000) -> None:
    total = int(max_tokens * pct / 100)
    ss._client.get_context_usage = AsyncMock(
        return_value={"totalTokens": total, "maxTokens": max_tokens}
    )


@pytest.mark.asyncio
async def test_warn_fires_once_at_warn_threshold() -> None:
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=50)

    await ss._check_context()

    assert ss._client.query.await_count == 1
    assert ss._context_warned is True
    ss.force_restart.assert_not_awaited()

    # Second check at same pct: warn should NOT re-fire
    await ss._check_context()
    assert ss._client.query.await_count == 1


@pytest.mark.asyncio
async def test_warn_flag_only_set_on_successful_query() -> None:
    """If query() fails, the warn flag must stay False so next turn retries."""
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=50)
    ss._client.query = AsyncMock(side_effect=RuntimeError("transport down"))

    await ss._check_context()

    assert ss._context_warned is False, "Flag must not be set when query fails"
    assert ss._client.query.await_count == 1

    # Transport recovers: next check should retry warn and succeed
    ss._client.query = AsyncMock()
    await ss._check_context()

    assert ss._context_warned is True
    assert ss._client.query.await_count == 1


@pytest.mark.asyncio
async def test_single_turn_overshoot_fires_both_warn_and_restart() -> None:
    """When pct jumps past restart threshold without ever being between warn and restart
    (e.g. a big tool result), the old if/elif skipped warn entirely. Fixed: both fire.
    """
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=85)

    await ss._check_context()

    assert ss._client.query.await_count == 1, "Warn must still fire on overshoot"
    assert ss._context_warned is True
    ss.force_restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_below_warn_threshold_no_action() -> None:
    ss = _make_session(warn_pct=40, restart_pct=80)
    _stub_ctx(ss, pct=30)

    await ss._check_context()

    ss._client.query.assert_not_awaited()
    ss.force_restart.assert_not_awaited()
    assert ss._context_warned is False


@pytest.mark.asyncio
async def test_restart_alone_when_already_warned() -> None:
    """If we already warned earlier in this session, crossing restart threshold
    should just restart — no redundant warn query."""
    ss = _make_session(warn_pct=40, restart_pct=80)
    ss._context_warned = True
    _stub_ctx(ss, pct=85)

    await ss._check_context()

    ss._client.query.assert_not_awaited()
    ss.force_restart.assert_awaited_once()
