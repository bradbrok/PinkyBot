"""Tests for the real-time (mid-turn) context gauge on tmux sessions.

Before this feature, ``usage.last_usage`` — the source for
``context_used_pct``, ``get_context_info()`` and the ``context_usage``
SSE — was written only by ``_record_turn_usage`` at turn end
(``stop_hook_summary``). A long tool-loop turn therefore showed the
PREVIOUS turn's context% for its entire duration.

The fix: the tailer's ``on_usage`` hook fires per assistant entry;
``TmuxSession._on_transcript_usage`` folds the block into
``usage.last_usage`` synchronously (pull-based readers see it at once)
and schedules a coalesced ``_emit_context_usage_event`` (push side:
SSE + nudges).

Mirrors test_tmux_context_autorestart.py's mock strategy — never shells
out to tmux.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession, _TmuxControl

_MODEL_1M = "claude-opus-4-8"
_MODEL_200K = "claude-haiku-4-5"


def _make_mock_tmux() -> MagicMock:
    tmux = MagicMock(spec=_TmuxControl)
    tmux.session_name = "pinky-test"
    tmux.has_session = AsyncMock(return_value=False)
    tmux.new_session = AsyncMock()
    tmux.kill_session = AsyncMock()
    tmux.send_keys = AsyncMock()
    tmux.paste_text = AsyncMock()
    tmux.capture_pane = AsyncMock()
    return tmux


def _make_session(*, model: str = _MODEL_200K) -> TmuxSession:
    cfg = StreamingSessionConfig(
        agent_name="dreamer",
        working_dir="/tmp/tmux-ctx-realtime-test",
        model=model,
    )
    ss = TmuxSession(cfg, tmux_control=_make_mock_tmux())
    ss._skip_wake_prompt_for_tests = True
    ss._emit_stream_event = AsyncMock()
    ss._enqueue_internal_prompt = AsyncMock()
    return ss


async def _drain_emit_task(ss: TmuxSession) -> None:
    """Await the fire-and-forget emit task if one was scheduled."""
    if ss._ctx_usage_emit_task is not None:
        await ss._ctx_usage_emit_task


# ──────────────────────────────────────────────────────────────────────────
# State update — pull-based readers see mid-turn usage immediately
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mid_turn_usage_updates_gauge_immediately() -> None:
    ss = _make_session()
    assert ss.context_used_pct == 0.0

    ss._on_transcript_usage({"input_tokens": 50_000, "output_tokens": 500})

    # No turn completed, yet the pull-based gauge already moved.
    assert ss.usage.last_usage == {"input_tokens": 50_000, "output_tokens": 500}
    assert ss.context_used_pct > 0.0
    info = ss.get_context_info()
    assert info["totalTokens"] == 50_500
    await _drain_emit_task(ss)


@pytest.mark.asyncio
async def test_mid_turn_usage_counts_all_four_token_kinds() -> None:
    ss = _make_session()
    ss._on_transcript_usage({
        "input_tokens": 1_000,
        "output_tokens": 200,
        "cache_read_input_tokens": 30_000,
        "cache_creation_input_tokens": 4_000,
    })
    assert ss.get_context_info()["totalTokens"] == 35_200
    await _drain_emit_task(ss)


@pytest.mark.asyncio
async def test_empty_or_malformed_usage_is_ignored() -> None:
    ss = _make_session()
    ss.usage.last_usage = {"input_tokens": 123}

    ss._on_transcript_usage({})
    ss._on_transcript_usage(None)  # type: ignore[arg-type]
    ss._on_transcript_usage("nope")  # type: ignore[arg-type]

    assert ss.usage.last_usage == {"input_tokens": 123}
    assert ss._ctx_usage_emit_task is None


# ──────────────────────────────────────────────────────────────────────────
# Push side — coalesced context_usage SSE emit
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mid_turn_usage_emits_context_usage_sse() -> None:
    ss = _make_session()
    ss._on_transcript_usage({"input_tokens": 10_000, "output_tokens": 100})
    await _drain_emit_task(ss)

    ctx_events = [
        c.args[0]
        for c in ss._emit_stream_event.call_args_list
        if c.args and c.args[0].get("type") == "context_usage"
    ]
    assert len(ctx_events) == 1
    assert ctx_events[0]["totalTokens"] == 10_100


@pytest.mark.asyncio
async def test_emit_tasks_coalesce_while_one_is_in_flight() -> None:
    ss = _make_session()
    gate = asyncio.Event()

    async def _blocked_emit(event: dict) -> None:
        await gate.wait()

    ss._emit_stream_event = AsyncMock(side_effect=_blocked_emit)

    ss._on_transcript_usage({"input_tokens": 1_000})
    first_task = ss._ctx_usage_emit_task
    assert first_task is not None
    await asyncio.sleep(0)  # let the task reach the blocked emit

    # Burst of further entries while the emit is parked: no new task.
    ss._on_transcript_usage({"input_tokens": 2_000})
    ss._on_transcript_usage({"input_tokens": 3_000})
    assert ss._ctx_usage_emit_task is first_task

    # State still tracked the newest value even though the emit coalesced.
    assert ss.usage.last_usage == {"input_tokens": 3_000}

    gate.set()
    await first_task
    # Next usage block schedules a fresh emit.
    ss._on_transcript_usage({"input_tokens": 4_000})
    assert ss._ctx_usage_emit_task is not first_task
    await _drain_emit_task(ss)


@pytest.mark.asyncio
async def test_emit_exception_is_fenced() -> None:
    """A raising nudge path must not surface as an unobserved task error."""
    ss = _make_session(model=_MODEL_1M)
    ss._enqueue_internal_prompt = AsyncMock(side_effect=RuntimeError("queue down"))
    # Above the ~41% 1M cap → nudge path runs and raises.
    ss._on_transcript_usage({"input_tokens": 450_000})
    await _drain_emit_task(ss)  # must not raise
    assert ss._ctx_usage_emit_task.done()
    assert ss._ctx_usage_emit_task.exception() is None


# ──────────────────────────────────────────────────────────────────────────
# Nudges now fire mid-turn (not just at turn end)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threshold_crossing_mid_turn_fires_autorestart_nudge() -> None:
    ss = _make_session(model=_MODEL_1M)
    ss._on_transcript_usage({"input_tokens": 450_000})  # past the 400k cap
    await _drain_emit_task(ss)

    assert ss._enqueue_internal_prompt.await_count == 1
    _, kwargs = ss._enqueue_internal_prompt.call_args
    assert kwargs.get("reason") == "context_autorestart_nudge"

    # Latch holds across subsequent mid-turn blocks.
    ss._on_transcript_usage({"input_tokens": 460_000})
    await _drain_emit_task(ss)
    assert ss._enqueue_internal_prompt.await_count == 1


# ──────────────────────────────────────────────────────────────────────────
# Wiring + lifecycle
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_tailer_wires_on_usage_hook() -> None:
    ss = _make_session()
    try:
        await ss._start_tailer()
        assert ss._tailer is not None
        assert ss._tailer._on_usage == ss._on_transcript_usage
    finally:
        await ss._stop_tailer()


@pytest.mark.asyncio
async def test_stop_tailer_cancels_pending_emit_task() -> None:
    ss = _make_session()
    gate = asyncio.Event()

    async def _blocked_emit(event: dict) -> None:
        await gate.wait()

    ss._emit_stream_event = AsyncMock(side_effect=_blocked_emit)
    ss._on_transcript_usage({"input_tokens": 1_000})
    task = ss._ctx_usage_emit_task
    await asyncio.sleep(0)

    await ss._stop_tailer()

    assert ss._ctx_usage_emit_task is None
    with pytest.raises(asyncio.CancelledError):
        await task
