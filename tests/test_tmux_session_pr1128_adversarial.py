"""Independent adversarial probes for PR #1128 at 3bb8bcc2.

Run from the pinned checkout root:

    PYTHONPATH="$PWD/src" uv run pytest --rootdir="$PWD" \
        -c pyproject.toml -q ../../pr1128_adversarial_test.py
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    TmuxSession,
    _InflightMeta,
    _QueuedTurn,
    _TmuxControl,
)
from pinky_daemon.transport_state import SessionState

PINNED_HEAD = "3bb8bcc2f22a3164df870c133ebc777a4f643125"
TAIL_BYTES = 4 * 1024 * 1024


def _ok() -> TmuxCommandResult:
    return TmuxCommandResult(returncode=0, stdout="", stderr="")


def _session() -> TmuxSession:
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "pinky-review"
    control.kill_session = AsyncMock(return_value=_ok())
    cfg = StreamingSessionConfig(
        agent_name="review",
        working_dir="/tmp/pr1128-review",
    )
    session = TmuxSession(cfg, tmux_control=control)
    session._state_machine._state = SessionState.CONNECTED
    return session


def _seed(
    session: TmuxSession,
    turn: _QueuedTurn,
    *,
    completion: asyncio.Event | None = None,
) -> _InflightMeta:
    now = time.time()
    turn.pane_delivery_started = True
    turn.pane_delivery_recorded = True
    entry = _InflightMeta(
        meta={},
        completion_event=completion,
        internal=turn.internal,
        dispatched_at=now,
        turn=turn,
        transcript_mtime_at_paste=now,
        paste_succeeded_at=now,
    )
    session._inflight_metas.append(entry)
    session._head_started_at = now - 1.0
    return entry


async def _reconcile(session: TmuxSession) -> None:
    task = asyncio.create_task(session._inflight_watchdog())
    try:
        for _ in range(200):
            if not session._inflight_metas:
                return
            await asyncio.sleep(0.01)
        pytest.fail("idle watchdog did not reconcile the seeded meta")
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _arm_idle_probe(
    monkeypatch: pytest.MonkeyPatch,
    session: TmuxSession,
    transcript,
) -> None:
    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.01)
    session._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": time.time() + 1.0,
    }
    session._transcript_recently_grew = lambda *_args: False
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    session._tailer.stop = AsyncMock()


@pytest.mark.asyncio
async def test_same_second_agent_header_does_not_prove_a_different_body_consumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """One same-second agent turn must not provide proof for its sibling."""
    session = _session()
    header = "[agent | murzik | internal | 2026-08-19 06:00:00 UTC]"
    consumed_prompt = f"{header}\nfirst verdict"
    missing_prompt = f"{header}\nsecond verdict"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "user", "message": {"content": consumed_prompt}},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _arm_idle_probe(monkeypatch, session, transcript)

    completion = asyncio.Event()
    missing = _QueuedTurn(prompt=missing_prompt, completion_event=completion)
    _seed(session, missing, completion=completion)

    await _reconcile(session)

    assert not completion.is_set(), "unconsumed sibling must remain pending"
    assert session._message_queue.get_nowait() is missing


@pytest.mark.asyncio
async def test_old_recurring_prompt_cannot_advance_current_scheduler_fire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A retained prior occurrence is not acceptance of the current fire."""
    session = _session()
    recurring_prompt = "Scheduled wake: heartbeat\nrun the recurring check"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": recurring_prompt}})
        + "\n",
        encoding="utf-8",
    )
    _arm_idle_probe(monkeypatch, session, transcript)

    delivery = asyncio.get_running_loop().create_future()
    durable_accept = MagicMock(return_value=True)
    current = _QueuedTurn(
        prompt=recurring_prompt,
        scheduler_delivery=delivery,
        scheduler_accept=durable_accept,
        scheduler_serialized=True,
    )
    _seed(session, current)

    await _reconcile(session)

    durable_accept.assert_not_called()
    assert not delivery.done() or delivery.result() is False


@pytest.mark.asyncio
async def test_bounded_tail_miss_cannot_override_exact_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A tail-window miss is weaker than the turn's exact user-row receipt."""
    session = _session()
    header = "accepted prompt"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(
        header.encode()
        + b"\n"
        + b"x" * (TAIL_BYTES + 1)
    )
    _arm_idle_probe(monkeypatch, session, transcript)

    completion = asyncio.Event()
    accepted = _QueuedTurn(
        prompt=f"{header}\nside effect already started",
        completion_event=completion,
        transport_accepted=True,
    )
    _seed(session, accepted, completion=completion)

    await _reconcile(session)

    assert completion.is_set(), "exactly accepted phantom should reconcile"
    assert accepted not in list(session._message_queue._queue)


@pytest.mark.asyncio
async def test_disconnect_does_not_requeue_a_terminalized_wake_alias() -> None:
    """The meta and worker in-hand slots can reference the same wake turn."""
    session = _session()
    completion = asyncio.Event()
    submission = asyncio.get_running_loop().create_future()
    wake = _QueuedTurn(
        prompt="orientation wake",
        internal=True,
        reason="wake_resume",
        completion_event=completion,
        submission_receipt=submission,
    )
    _seed(session, wake, completion=completion)
    # Production keeps a verified wake in-hand while awaiting its exact
    # transcript receipt, so it simultaneously owns an inflight meta.
    session._inflight_turn = wake

    await session.disconnect()

    queued = list(session._message_queue._queue)
    assert wake not in queued or (
        not completion.is_set() and not submission.done()
    ), "disconnect must not enqueue a wake after terminalizing its receipts"


@pytest.mark.asyncio
async def test_disconnect_does_not_replay_a_verified_accepted_plain_turn() -> None:
    """#561 parity abandons an accepted head to avoid duplicate side effects."""
    session = _session()
    accepted = _QueuedTurn(prompt="perform a side effect", transport_accepted=True)
    _seed(session, accepted)

    await session.disconnect()

    assert accepted not in list(session._message_queue._queue)


@pytest.mark.asyncio
async def test_disconnect_does_not_leave_false_scheduler_replay_queued(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """An idle-requeued scheduler turn cannot outlive its False owner receipt."""
    session = _session()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"type":"system"}\n', encoding="utf-8")
    _arm_idle_probe(monkeypatch, session, transcript)

    receipt = asyncio.get_running_loop().create_future()
    scheduler = _QueuedTurn(
        prompt="scheduled current fire",
        scheduler_delivery=receipt,
        scheduler_accept=MagicMock(return_value=True),
        scheduler_serialized=True,
    )
    session._scheduler_pending_turns.append(scheduler)
    _seed(session, scheduler)

    await _reconcile(session)
    await session.disconnect()

    assert not (
        scheduler in list(session._message_queue._queue) and receipt.done()
    ), "a terminal-False scheduler turn must not remain queued for execution"
