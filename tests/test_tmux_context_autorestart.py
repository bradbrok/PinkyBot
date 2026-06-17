"""Tests for the tmux context-autorestart nudge (task #144).

Covers the two halves of the feature:

1. ``_effective_restart_threshold_pct`` — the percentage threshold with
   the absolute ``_RESTART_TOKENS_CAP_1M`` (400k) ceiling folded in.
   Bites on 1M-context models (drops 80%→~41%), no-ops on 200k.
2. ``_emit_context_usage_event`` — on crossing it must now ALSO drive the
   agent via ``_enqueue_internal_prompt`` (the "dead wire" the SSE-only
   ``restart_nudge`` left disconnected), one-shot per crossing, re-arming
   after the total drops, and honouring the
   ``PINKY_CONTEXT_AUTORESTART_NUDGE`` kill-switch.

Mirrors test_tmux_session.py's mock strategy — never shells out to tmux.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession, _TmuxControl

# A 1M-context model and a 200k one (membership in streaming_session._1M_MODELS).
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


def _make_session(*, model: str) -> TmuxSession:
    cfg = StreamingSessionConfig(
        agent_name="dreamer",
        working_dir="/tmp/tmux-autorestart-test",
        model=model,
    )
    ss = TmuxSession(cfg, tmux_control=_make_mock_tmux())
    ss._skip_wake_prompt_for_tests = True
    # Isolate the unit: patch the two awaited side-effect sinks so the
    # crossing logic runs without an SSE bus or a live turn queue.
    ss._emit_stream_event = AsyncMock()
    ss._enqueue_internal_prompt = AsyncMock()
    return ss


def _set_total_tokens(ss: TmuxSession, total: int) -> None:
    """Pin the session's current-window total via last_usage."""
    ss.usage.last_usage = {"input_tokens": total}


# ──────────────────────────────────────────────────────────────────────────
# _effective_restart_threshold_pct
# ──────────────────────────────────────────────────────────────────────────


def test_effective_threshold_caps_at_400k_on_1m_model(monkeypatch) -> None:
    # Clear the env var so the default 33k-token buffer is used, not any
    # ambient CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (e.g. set by Claude Code).
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)
    ss = _make_session(model=_MODEL_1M)
    # Effective max = 1_000_000 - 33_000 buffer = 967_000.
    # cap_pct = 400_000 / 967_000 * 100 ≈ 41.36, below the 80% default.
    assert ss._effective_restart_threshold_pct() == pytest.approx(
        400_000 / 967_000 * 100.0, rel=1e-3
    )


def test_effective_threshold_unchanged_on_200k_model() -> None:
    ss = _make_session(model=_MODEL_200K)
    # 400k cap exceeds the whole 200k window, so min() yields the
    # configured 80% default — behaviour identical to pre-feature.
    assert ss._effective_restart_threshold_pct() == pytest.approx(80.0)


def test_400k_cap_fires_well_before_the_old_80pct_point() -> None:
    """Regression intent: the cap must trigger earlier than 80% on 1M."""
    ss = _make_session(model=_MODEL_1M)
    assert ss._effective_restart_threshold_pct() < 80.0


# ──────────────────────────────────────────────────────────────────────────
# _emit_context_usage_event → autorestart nudge
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_crossing_enqueues_nudge_once() -> None:
    ss = _make_session(model=_MODEL_1M)
    # 450k tokens ≈ 46.5% on a 967k window — past the ~41% cap.
    _set_total_tokens(ss, 450_000)

    await ss._emit_context_usage_event()
    await ss._emit_context_usage_event()  # still above; latch must hold

    assert ss._enqueue_internal_prompt.await_count == 1
    _, kwargs = ss._enqueue_internal_prompt.call_args
    assert kwargs.get("reason") == "context_autorestart_nudge"


@pytest.mark.asyncio
async def test_nudge_is_tail_enqueued_not_front() -> None:
    """Lock the tail-vs-front decision (Dymok #618 review, Q1).

    Front-enqueue would jump the restart ahead of queued user turns,
    making them depend on replay surviving an agent-initiated
    context_restart. Tail-enqueue answers users first, then restarts.
    """
    ss = _make_session(model=_MODEL_1M)
    _set_total_tokens(ss, 450_000)

    await ss._emit_context_usage_event()

    ss._enqueue_internal_prompt.assert_awaited_once()
    _, kwargs = ss._enqueue_internal_prompt.call_args
    assert kwargs.get("front", False) is False


@pytest.mark.asyncio
async def test_no_nudge_below_threshold() -> None:
    ss = _make_session(model=_MODEL_1M)
    _set_total_tokens(ss, 100_000)  # ~10% — well below

    await ss._emit_context_usage_event()

    ss._enqueue_internal_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_latch_rearms_after_total_drops() -> None:
    ss = _make_session(model=_MODEL_1M)

    _set_total_tokens(ss, 450_000)
    await ss._emit_context_usage_event()  # fire #1

    _set_total_tokens(ss, 80_000)
    await ss._emit_context_usage_event()  # drop below → re-arm
    assert ss._restart_nudge_fired is False

    _set_total_tokens(ss, 450_000)
    await ss._emit_context_usage_event()  # fire #2

    assert ss._enqueue_internal_prompt.await_count == 2


@pytest.mark.asyncio
async def test_kill_switch_keeps_sse_but_suppresses_nudge(monkeypatch) -> None:
    monkeypatch.setenv("PINKY_CONTEXT_AUTORESTART_NUDGE", "0")
    ss = _make_session(model=_MODEL_1M)
    _set_total_tokens(ss, 450_000)

    await ss._emit_context_usage_event()

    # Notify-only: the restart_nudge SSE still fires for the UI...
    nudge_events = [
        c.args[0]
        for c in ss._emit_stream_event.call_args_list
        if c.args and c.args[0].get("type") == "restart_nudge"
    ]
    assert len(nudge_events) == 1
    # ...but the agent-facing directive is suppressed.
    ss._enqueue_internal_prompt.assert_not_called()
