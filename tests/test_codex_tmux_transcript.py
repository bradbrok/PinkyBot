"""Tests for ``codex_tmux_transcript`` — the codex response-capture pipeline.

PR1 of the codex-tmux transport (#215). Pure unit tests (no network, no
daemon, no tmux). The tailer is a function of (file content, wake events)
→ callbacks, and we test exactly that.

Fixture note: ``tests/fixtures/codex_rollout_sample.jsonl`` was built by
hand from the EXACT record structure observed in a real murzik rollout
(2026-06-13), but with synthetic placeholder content only — PinkyBot is a
public repo and we never check in real agent conversation text.

Coverage:
  - ``task_complete`` fires the callback once; text == last_agent_message;
    usage mapped from the preceding token_count; duration_ms correct.
  - Multi-turn file → N callbacks in FIFO order; offset advances fully.
  - Partial trailing line (no ``\\n``) not consumed; resumes on append.
  - ``token_count`` with ``info=null`` does not corrupt usage state.
  - ``turn_aborted`` closes the turn (callback fires; stop_reason == "turn_aborted").
  - Cold-start seek-to-EOF vs first-bind seek-to-0.
  - ``path_discovery``: cwd-filtered selection from a temp tree of fixtures.
  - Background loop: wake() short-circuits poll; fallback poll makes progress.
  - Raw rollout entries are forwarded to the session acceptance observer.
  - Standard stats shape; self_heal_repoints counter; callback errors.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from pinky_daemon.codex_tmux_transcript import (
    CodexTmuxTranscriptTailer,
    _CodexTurnBuffer,
    _discover_codex_rollout,
)
from pinky_daemon.turn_response import TurnResponse

# ──────────────────────────────────────────────────────────────────────────
# JSONL builder helpers — format-accurate, content-clean
# ──────────────────────────────────────────────────────────────────────────


def _session_meta(cwd: str = "/tmp/fake-agent-cwd", session_id: str = "test-uuid-0001") -> dict:
    return {
        "timestamp": "2026-06-17T10:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "timestamp": "2026-06-17T10:00:00.000Z",
            "cwd": cwd,
            "originator": "pinkybot",
            "cli_version": "0.125.0",
        },
    }


def _task_started(turn_id: str = "turn-001", started_at: int = 1750152001) -> dict:
    return {
        "timestamp": "2026-06-17T10:00:01.000Z",
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "turn_id": turn_id,
            "started_at": started_at,
            "model_context_window": 258400,
        },
    }


def _agent_message(text: str = "Hello") -> dict:
    return {
        "timestamp": "2026-06-17T10:00:02.000Z",
        "type": "event_msg",
        "payload": {
            "type": "agent_message",
            "message": text,
            "phase": "commentary",
            "memory_citation": None,
        },
    }


def _user_message(text: str = "queued wake") -> dict:
    return {
        "timestamp": "2026-06-17T10:00:01.500Z",
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": text,
            "images": [],
            "local_images": [],
            "text_elements": [],
        },
    }


def _token_count_null() -> dict:
    """token_count with info=null (rate-limit-only event, no usage)."""
    return {
        "timestamp": "2026-06-17T10:00:00.500Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": None,
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 5.0, "window_minutes": 300},
            },
        },
    }


def _token_count(
    *,
    input_tokens: int = 100,
    cached_input_tokens: int = 20,
    output_tokens: int = 10,
    reasoning_output_tokens: int = 5,
    total_tokens: int = 110,
) -> dict:
    """token_count with non-null info (carries actual usage)."""
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
    }
    return {
        "timestamp": "2026-06-17T10:00:03.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": usage,
                "last_token_usage": usage,
                "model_context_window": 258400,
            },
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 6.0, "window_minutes": 300},
            },
        },
    }


def _task_complete(
    turn_id: str = "turn-001",
    last_agent_message: str = "Done.",
    duration_ms: int = 3000,
) -> dict:
    return {
        "timestamp": "2026-06-17T10:00:03.500Z",
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": turn_id,
            "last_agent_message": last_agent_message,
            "completed_at": 1750152004,
            "duration_ms": duration_ms,
            "time_to_first_token_ms": 900,
        },
    }


def _turn_aborted(turn_id: str = "turn-aborted-001", duration_ms: int = 10000) -> dict:
    return {
        "timestamp": "2026-06-17T10:00:30.000Z",
        "type": "event_msg",
        "payload": {
            "type": "turn_aborted",
            "turn_id": turn_id,
            "reason": "interrupted",
            "completed_at": 1750152030,
            "duration_ms": duration_ms,
        },
    }


def _write_jsonl(path: Path, entries: list[dict], *, trailing_newline: bool = True) -> int:
    """Write entries to a JSONL file. Returns total bytes written."""
    text = "\n".join(json.dumps(e) for e in entries)
    if trailing_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _append_jsonl(path: Path, entries: list[dict], *, trailing_newline: bool = True) -> int:
    """Append entries to an existing JSONL file."""
    chunk = "\n".join(json.dumps(e) for e in entries)
    if trailing_newline:
        chunk += "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(chunk)
    return len(chunk.encode("utf-8"))


# ──────────────────────────────────────────────────────────────────────────
# Shared test helper
# ──────────────────────────────────────────────────────────────────────────


class _Captor:
    """Captures on_turn_complete invocations for assertions."""

    def __init__(self) -> None:
        self.responses: list[TurnResponse] = []
        self.raise_on_call: Exception | None = None

    async def __call__(self, response: TurnResponse) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.responses.append(response)


@pytest.fixture
def transcript(tmp_path) -> Path:
    """A fresh empty rollout file in a tmp dir."""
    p = tmp_path / "rollout-test.jsonl"
    p.touch()
    return p


# ──────────────────────────────────────────────────────────────────────────
# _CodexTurnBuffer unit tests
# ──────────────────────────────────────────────────────────────────────────


class TestCodexTurnBuffer:
    """Per-entry accumulation + drain shape."""

    def test_task_complete_fires_turn(self):
        buf = _CodexTurnBuffer()
        assert buf.feed(_task_started()) == (False, False)
        assert buf.feed(_agent_message("Hello")) == (False, False)
        assert buf.feed(_token_count()) == (False, False)
        closes, aborted = buf.feed(_task_complete(last_agent_message="Done."))
        assert closes is True
        assert aborted is False

        resp = buf.drain(model="gpt-5")
        assert resp.text == "Done."
        assert resp.model == "gpt-5"
        assert resp.usage["input_tokens"] == 100
        assert resp.usage["output_tokens"] == 10
        assert resp.duration_ms == 3000
        assert resp.stop_reason == "task_complete"

    def test_agent_message_fallback_when_lam_empty(self):
        """When last_agent_message is missing/empty, fall back to accumulated chunks."""
        buf = _CodexTurnBuffer()
        buf.feed(_agent_message("chunk1"))
        buf.feed(_agent_message("chunk2"))
        closes, aborted = buf.feed(_task_complete(last_agent_message=""))
        assert closes is True
        resp = buf.drain()
        assert resp.text == "chunk1chunk2"

    def test_lam_takes_precedence_over_chunks(self):
        """last_agent_message wins over accumulated agent_message chunks."""
        buf = _CodexTurnBuffer()
        buf.feed(_agent_message("streaming text"))
        closes, _ = buf.feed(_task_complete(last_agent_message="Authoritative final."))
        assert closes is True
        resp = buf.drain()
        assert resp.text == "Authoritative final."

    def test_token_count_null_does_not_corrupt_usage(self):
        """info=null token_count events must be a no-op for usage state."""
        buf = _CodexTurnBuffer()
        buf.feed(_token_count(input_tokens=50, output_tokens=5, total_tokens=55))
        buf.feed(_token_count_null())   # must NOT overwrite the valid snapshot
        buf.feed(_task_complete())
        resp = buf.drain()
        # Usage must come from the first (non-null) token_count, not be cleared.
        assert resp.usage["input_tokens"] == 50
        assert resp.usage["output_tokens"] == 5

    def test_token_count_null_when_no_prior_usage(self):
        """info=null with no preceding valid token_count → empty usage dict; no crash."""
        buf = _CodexTurnBuffer()
        buf.feed(_token_count_null())
        buf.feed(_task_complete())
        resp = buf.drain()
        assert resp.usage == {}

    def test_turn_aborted_closes_turn(self):
        buf = _CodexTurnBuffer()
        buf.feed(_task_started())
        buf.feed(_agent_message("Partial reply before abort."))
        closes, aborted = buf.feed(_turn_aborted(duration_ms=10000))
        assert closes is True
        assert aborted is True
        resp = buf.drain(aborted=True)
        assert resp.stop_reason == "turn_aborted"
        assert resp.duration_ms == 10000

    def test_drain_resets_for_next_turn(self):
        buf = _CodexTurnBuffer()
        buf.feed(_agent_message("first"))
        buf.feed(_task_complete(last_agent_message="First."))
        buf.drain()
        assert buf.is_empty

        buf.feed(_agent_message("second"))
        buf.feed(_task_complete(last_agent_message="Second."))
        resp = buf.drain()
        assert resp.text == "Second."

    def test_non_event_msg_types_ignored(self):
        """session_meta and any unknown top-level type don't affect the buffer."""
        buf = _CodexTurnBuffer()
        buf.feed(_session_meta())
        buf.feed({"type": "unknown_future_type", "payload": {"data": 1}})
        assert buf.is_empty

    def test_is_empty_true_initially(self):
        buf = _CodexTurnBuffer()
        assert buf.is_empty is True

    def test_is_empty_false_after_agent_message(self):
        buf = _CodexTurnBuffer()
        buf.feed(_agent_message("hi"))
        assert buf.is_empty is False

    def test_assistant_entry_count_reflects_agent_messages(self):
        buf = _CodexTurnBuffer()
        buf.feed(_agent_message("a"))
        buf.feed(_agent_message("b"))
        buf.feed(_agent_message("c"))
        buf.feed(_task_complete())
        resp = buf.drain()
        assert resp.assistant_entry_count == 3


# ──────────────────────────────────────────────────────────────────────────
# CodexTmuxTranscriptTailer — read_once() driven tests
# ──────────────────────────────────────────────────────────────────────────


class TestTailerReadOnce:
    """Synchronous semantics — drive read_once() directly, no background task."""

    @pytest.mark.asyncio
    async def test_empty_file_no_callbacks(self, transcript):
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        assert cb.responses == []
        assert tailer.offset == 0

    @pytest.mark.asyncio
    async def test_nonexistent_file_no_error(self, tmp_path):
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(tmp_path / "does-not-exist.jsonl", cb)
        consumed = await tailer.read_once()
        assert consumed == 0

    @pytest.mark.asyncio
    async def test_forwards_raw_user_message_to_entry_observer(self, transcript):
        cb = _Captor()
        observed: list[dict] = []
        entry = _user_message("scheduled codex wake")
        tailer = CodexTmuxTranscriptTailer(
            transcript,
            cb,
            on_entry=lambda raw: observed.append(raw),
        )
        _write_jsonl(transcript, [entry])

        await tailer.read_once()

        assert observed == [entry]
        assert cb.responses == []

    @pytest.mark.asyncio
    async def test_single_turn_fires_callback_once(self, transcript):
        """task_complete fires exactly one callback; text==last_agent_message."""
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb, model="gpt-5")
        _write_jsonl(transcript, [
            _session_meta(),
            _task_started("t1"),
            _agent_message("streamed text"),
            _token_count(input_tokens=100, output_tokens=10, total_tokens=110),
            _task_complete("t1", last_agent_message="Done.", duration_ms=3000),
        ])
        await tailer.read_once()
        assert len(cb.responses) == 1
        r = cb.responses[0]
        assert r.text == "Done."
        assert r.model == "gpt-5"
        assert r.usage["input_tokens"] == 100
        assert r.usage["output_tokens"] == 10
        assert r.duration_ms == 3000
        assert tailer.offset == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_multi_turn_fifo_order(self, transcript):
        """Two turns produce two callbacks in order; offset reaches EOF."""
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _session_meta(),
            # Turn 1
            _task_started("t1"),
            _token_count(input_tokens=100, output_tokens=10, total_tokens=110),
            _task_complete("t1", last_agent_message="Reply one.", duration_ms=1000),
            # Turn 2
            _task_started("t2"),
            _token_count(input_tokens=200, output_tokens=20, total_tokens=220),
            _task_complete("t2", last_agent_message="Reply two.", duration_ms=2000),
        ])
        await tailer.read_once()
        assert [r.text for r in cb.responses] == ["Reply one.", "Reply two."]
        assert tailer.offset == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_partial_trailing_line_not_consumed(self, transcript):
        """Partial trailing line (no \\n) is not consumed; resumes on append."""
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)

        # Write a complete line + a partial (mid-write) line with no \\n.
        complete = json.dumps(_session_meta()) + "\n"
        partial = json.dumps(_task_started())[:30]   # truncated
        transcript.write_text(complete + partial, encoding="utf-8")

        await tailer.read_once()
        assert cb.responses == []
        # Offset advanced only past the complete line.
        assert tailer.offset == len(complete.encode("utf-8"))

        # Now write the full content so the partial becomes complete.
        rest = json.dumps(_task_started()) + "\n"
        rest += json.dumps(_token_count()) + "\n"
        rest += json.dumps(_task_complete(last_agent_message="Partial resolved.")) + "\n"
        transcript.write_text(complete + rest, encoding="utf-8")

        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "Partial resolved."

    @pytest.mark.asyncio
    async def test_token_count_null_does_not_crash(self, transcript):
        """info=null token_count in the wild doesn't corrupt state or raise."""
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _token_count_null(),
            _task_started(),
            _token_count_null(),
            _task_complete(last_agent_message="Safe."),
        ])
        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "Safe."
        # Usage should be empty (no valid token_count preceded this turn).
        assert cb.responses[0].usage == {}

    @pytest.mark.asyncio
    async def test_turn_aborted_fires_callback_with_aborted_flag(self, transcript):
        """turn_aborted closes the turn; callback fires; stop_reason=='turn_aborted'."""
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _task_started("t1"),
            _agent_message("Partial."),
            _turn_aborted("t1", duration_ms=10000),
        ])
        await tailer.read_once()
        assert len(cb.responses) == 1
        r = cb.responses[0]
        assert r.stop_reason == "turn_aborted"
        assert r.duration_ms == 10000

    @pytest.mark.asyncio
    async def test_cold_start_seek_to_eof_by_default(self, transcript, tmp_path):
        """Default set_transcript_path seeks to EOF — historical turns not re-fired."""
        cb = _Captor()
        _write_jsonl(transcript, [
            _task_started("old"),
            _task_complete("old", last_agent_message="Historical."),
        ])
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        historical_responses = len(cb.responses)

        # Swap to a file that already has content.
        new_path = tmp_path / "rollout-new.jsonl"
        _write_jsonl(new_path, [
            _task_started("hist"),
            _task_complete("hist", last_agent_message="Must not replay."),
        ])
        tailer.set_transcript_path(new_path)
        # Default offset == EOF of the new file.
        assert tailer.offset == new_path.stat().st_size
        await tailer.read_once()
        # No additional callbacks from the historical content.
        assert len(cb.responses) == historical_responses

        # New content appended after the swap fires correctly.
        _append_jsonl(new_path, [
            _task_started("new"),
            _task_complete("new", last_agent_message="Fresh reply."),
        ])
        await tailer.read_once()
        assert cb.responses[-1].text == "Fresh reply."

    @pytest.mark.asyncio
    async def test_first_bind_seek_to_start(self, transcript, tmp_path):
        """seek_to_start=True reads from byte 0 — used by self-heal discovery."""
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(tmp_path / "placeholder.jsonl", cb)

        real_path = tmp_path / "rollout-real.jsonl"
        _write_jsonl(real_path, [
            _task_started(),
            _task_complete(last_agent_message="Cold start response."),
        ])
        tailer.set_transcript_path(real_path, seek_to_start=True)
        assert tailer.offset == 0
        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "Cold start response."

    @pytest.mark.asyncio
    async def test_offset_advances_per_turn(self, transcript):
        """After turn 1 completes, offset matches the byte position after it."""
        cb = _Captor()
        _write_jsonl(transcript, [
            _task_complete("t1", last_agent_message="Turn 1."),
        ])
        turn1_size = transcript.stat().st_size

        _append_jsonl(transcript, [
            _task_complete("t2", last_agent_message="Turn 2."),
        ])

        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        tailer.set_offset(turn1_size)
        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "Turn 2."
        assert tailer.offset == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_cold_start_replay_skips_empty_buffer(self, transcript):
        """task_complete with no preceding agent_message must NOT fire an empty callback."""
        cb = _Captor()
        # File only has a task_complete with no task_started/agent_message before it
        # (simulating mid-file entry where we started after agent_message lines).
        _write_jsonl(transcript, [
            _task_complete(last_agent_message="Old historical message."),
        ])
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        # Seek to 0 to replay the file from the start.
        # The buffer is empty when task_complete fires → callback must not fire.
        await tailer.read_once()
        # This fires because last_agent_message is non-empty and the buffer
        # picks it up during task_complete handling.
        # Cold-start guard: only suppress when buffer.is_empty (no chunks AND
        # no lam text). With lam="Old...", the buffer is populated.
        # So actually one callback fires here (the lam text). This is correct:
        # an observer restarting from 0 *will* see prior turns. The "cold-start
        # skip" only applies when we enter after the agent_message lines but
        # the task_complete carries no text (lam empty AND no chunks).
        # Test the actual "no text" path:
        _write_jsonl(transcript, [
            _task_complete(last_agent_message=""),  # empty lam, no chunks
        ])
        cb2 = _Captor()
        tailer2 = CodexTmuxTranscriptTailer(transcript, cb2)
        await tailer2.read_once()
        assert cb2.responses == []  # empty buffer → no spurious callback

    @pytest.mark.asyncio
    async def test_initial_bind_and_self_heal_snapshot_historical_high_water(
        self, tmp_path
    ):
        """Initial history is frozen, but a self-heal consumes unseen closes.

        The bare close after the historical scan pins the silent-close drain reset:
        an observed historical ``task_started`` must not leak into the next marker.
        The explicit ``drain_buffer`` case pins the other lifecycle reset path.
        """
        rollout = tmp_path / "rollout-initial.jsonl"
        _write_jsonl(
            rollout,
            [
                _task_started("historical-text"),
                _task_complete(
                    "historical-text", last_agent_message="Historical reply."
                ),
                # Keep the silently-drained turn last so the next bare marker
                # proves that branch reset its observed task_started state.
                _task_started("historical-empty"),
                _agent_message(""),
                _task_complete("historical-empty", last_agent_message=""),
            ],
        )
        cb = _Captor()
        # Constructor bind: snapshot the replay boundary before reading bytes.
        tailer = CodexTmuxTranscriptTailer(rollout, cb)

        await tailer.read_once()
        assert [response.text for response in cb.responses] == [
            "Historical reply."
        ]

        # No task_started after the prior silent drain: this remains replay
        # noise even though the marker itself is beyond the bind boundary.
        _append_jsonl(
            rollout,
            [_task_complete("bare-after-history", last_agent_message="")],
        )
        await tailer.read_once()
        assert [response.text for response in cb.responses] == [
            "Historical reply."
        ]

        _append_jsonl(
            rollout,
            [
                _task_started("live-empty"),
                _agent_message(""),
                _token_count(total_tokens=75_591),
                _task_complete("live-empty", last_agent_message=""),
            ],
        )
        await tailer.read_once()
        assert [response.text for response in cb.responses] == [
            "Historical reply.",
            "",
        ]
        assert cb.responses[-1].usage["total_tokens"] == 75_591
        assert cb.responses[-1].usage["model_context_window"] == 258_400

        # A lifecycle drain after a start marker must clear the observed-live
        # bit; the following bare marker cannot inherit it and fire.
        _append_jsonl(rollout, [_task_started("discarded-live-start")])
        await tailer.read_once()
        tailer.drain_buffer()
        _append_jsonl(
            rollout,
            [_task_complete("discarded-live-start", last_agent_message="")],
        )
        await tailer.read_once()
        assert len(cb.responses) == 2

        # Self-heal sees bytes this daemon process has never consumed. A
        # complete textless turn already present on the rollout is live.
        healed = tmp_path / "rollout-self-heal.jsonl"
        _write_jsonl(
            healed,
            [
                _task_started("healed-empty"),
                _agent_message(""),
                _token_count(total_tokens=75_591),
                _task_complete("healed-empty", last_agent_message=""),
            ],
        )
        healed_cb = _Captor()
        inflight = ["head"]

        async def consume_healed(response: TurnResponse) -> None:
            await healed_cb(response)
            inflight.pop(0)

        healed_tailer = CodexTmuxTranscriptTailer(
            tmp_path / "placeholder-self-heal.jsonl", consume_healed
        )
        healed_tailer.set_transcript_path(healed, seek_to_start=True)
        await healed_tailer.read_once()

        assert healed_tailer.stats["turns_fired"] == 1
        assert inflight == []
        assert [response.text for response in healed_cb.responses] == [""]
        assert healed_cb.responses[0].usage["total_tokens"] == 75_591
        assert healed_tailer.stats["buffer_empty"] is True

        # The close marker, not the start marker, owns the replay boundary.  A
        # turn that began below the bind high-water but completes above it is
        # live and must fire.
        straddle = tmp_path / "rollout-straddled-boundary.jsonl"
        _write_jsonl(straddle, [_task_started("straddled")])
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(straddle, cb)
        await tailer.read_once()
        _append_jsonl(
            straddle,
            [_task_complete("straddled", last_agent_message="")],
        )
        await tailer.read_once()
        assert [response.text for response in cb.responses] == [""]

    @pytest.mark.parametrize("bind_kind", ("constructor", "eof-swap", "resume"))
    @pytest.mark.asyncio
    async def test_frozen_replay_boundaries_keep_textless_history_silent(
        self, tmp_path, bind_kind
    ):
        """Constructor, default EOF swap, and resume keep frozen history silent."""
        rollout = tmp_path / f"rollout-{bind_kind}.jsonl"
        _write_jsonl(
            rollout,
            [
                _task_started("historical-empty"),
                _task_complete("historical-empty", last_agent_message=""),
            ],
        )
        cb = _Captor()

        if bind_kind == "eof-swap":
            tailer = CodexTmuxTranscriptTailer(
                tmp_path / "placeholder-eof-swap.jsonl", cb
            )
            tailer.set_transcript_path(rollout)
        else:
            tailer = CodexTmuxTranscriptTailer(rollout, cb)
            if bind_kind == "resume":
                tailer.set_offset(0)

        await tailer.read_once()

        assert tailer.stats["turns_fired"] == 0
        assert cb.responses == []

    @pytest.mark.asyncio
    async def test_truncation_replacement_delivers_unconsumed_textless_close(
        self, tmp_path
    ):
        """A replacement already containing a complete textless turn is live."""
        rollout = tmp_path / "rollout-truncated.jsonl"
        _write_jsonl(
            rollout,
            [_task_started("old"), _agent_message("x" * 2_000)],
        )
        old_size = rollout.stat().st_size
        cb = _Captor()
        inflight = ["head"]

        async def consume_replacement(response: TurnResponse) -> None:
            await cb(response)
            inflight.pop(0)

        tailer = CodexTmuxTranscriptTailer(rollout, consume_replacement)
        tailer.set_offset(old_size)
        _write_jsonl(
            rollout,
            [
                _task_started("replacement-empty"),
                _task_complete("replacement-empty", last_agent_message=""),
            ],
        )
        assert rollout.stat().st_size < old_size

        await tailer.read_once()

        assert tailer.stats["turns_fired"] == 1
        assert inflight == []
        assert [response.text for response in cb.responses] == [""]
        assert tailer.stats["buffer_empty"] is True

    @pytest.mark.asyncio
    async def test_self_heal_repoint_without_complete_turn_does_not_pop_head(
        self, tmp_path
    ):
        """A repoint with no complete turn cannot over-pop the inflight head."""
        rollout = tmp_path / "rollout-incomplete-self-heal.jsonl"
        _write_jsonl(rollout, [_task_started("still-running")])
        cb = _Captor()
        inflight = ["head"]

        async def consume_incomplete(response: TurnResponse) -> None:
            await cb(response)
            inflight.pop(0)

        tailer = CodexTmuxTranscriptTailer(
            tmp_path / "placeholder-incomplete.jsonl", consume_incomplete
        )
        tailer.set_transcript_path(rollout, seek_to_start=True)
        await tailer.read_once()

        assert tailer.stats["turns_fired"] == 0
        assert inflight == ["head"]
        assert cb.responses == []

    @pytest.mark.asyncio
    async def test_callback_exception_swallowed(self, transcript):
        """A misbehaving callback does not strand the tailer."""
        cb = _Captor()
        cb.raise_on_call = RuntimeError("downstream blew up")
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _agent_message("hi"),
            _task_complete(last_agent_message="Response."),
        ])
        await tailer.read_once()  # must not raise
        assert tailer.stats["callback_errors"] == 1
        assert tailer.stats["turns_fired"] == 1
        assert tailer.offset == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_malformed_json_line_skipped(self, transcript):
        cb = _Captor()
        good = json.dumps(_agent_message("good")) + "\n"
        bad = "{not valid json\n"
        complete = json.dumps(_task_complete(last_agent_message="Good.")) + "\n"
        transcript.write_text(good + bad + complete, encoding="utf-8")
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        assert tailer.stats["parse_errors"] == 1
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "Good."

    @pytest.mark.asyncio
    async def test_session_meta_fields_captured(self, transcript):
        """session_id and session_cwd are populated from line 1."""
        cb = _Captor()
        _write_jsonl(transcript, [
            _session_meta(cwd="/tmp/my-agent", session_id="my-session-uuid"),
        ])
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        assert tailer.session_id == "my-session-uuid"
        assert tailer.session_cwd == "/tmp/my-agent"

    @pytest.mark.asyncio
    async def test_stats_shape(self, transcript):
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        stats = tailer.stats
        assert {
            "turns_fired", "lines_read", "parse_errors", "callback_errors",
            "rotations", "self_heal_repoints", "offset", "buffer_empty",
            "active", "running",
        } <= set(stats)

    @pytest.mark.asyncio
    async def test_turns_fired_counter(self, transcript):
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _task_complete("t1", last_agent_message="A."),
            _task_complete("t2", last_agent_message="B."),
            _task_complete("t3", last_agent_message="C."),
        ])
        await tailer.read_once()
        assert tailer.stats["turns_fired"] == 3

    @pytest.mark.asyncio
    async def test_fixture_file_reads_correctly(self):
        """Smoke-test the hand-built fixture file; two turns complete, one abort."""
        fixture = Path(__file__).parent / "fixtures" / "codex_rollout_sample.jsonl"
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(fixture, cb, model="gpt-5")
        tailer.set_offset(0)
        await tailer.read_once()
        # Fixture has: 1 turn_complete "Done.", 1 turn_complete "Second done.", 1 turn_aborted.
        assert len(cb.responses) == 3
        assert cb.responses[0].text == "Done."
        assert cb.responses[1].text == "Second done."
        assert cb.responses[2].stop_reason == "turn_aborted"
        assert tailer.session_id == "01234567-0000-7000-8000-000000000001"
        assert tailer.session_cwd == "/tmp/fake-agent-cwd"


# ──────────────────────────────────────────────────────────────────────────
# Background loop tests
# ──────────────────────────────────────────────────────────────────────────


class TestTailerBackgroundLoop:
    @pytest.mark.asyncio
    async def test_wake_picks_up_new_entries(self, transcript):
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(
            transcript, cb,
            fallback_poll_sec=0.05,
            active_poll_sec=0.01,
        )
        await tailer.start()
        try:
            _write_jsonl(transcript, [
                _agent_message("wake test"),
                _task_complete(last_agent_message="Wake response."),
            ])
            tailer.wake()
            for _ in range(20):
                await asyncio.sleep(0.02)
                if cb.responses:
                    break
            assert len(cb.responses) == 1
            assert cb.responses[0].text == "Wake response."
        finally:
            await tailer.stop()

    @pytest.mark.asyncio
    async def test_fallback_poll_makes_progress_without_wake(self, transcript):
        """Fallback poll fires even without wake() calls."""
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(
            transcript, cb,
            fallback_poll_sec=0.02,
            active_poll_sec=0.01,
        )
        await tailer.start()
        try:
            _write_jsonl(transcript, [
                _task_complete(last_agent_message="Poll response."),
            ])
            for _ in range(30):
                await asyncio.sleep(0.02)
                if cb.responses:
                    break
            assert len(cb.responses) == 1
        finally:
            await tailer.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, transcript):
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        await tailer.start()
        await tailer.stop()
        await tailer.stop()
        assert tailer.stats["running"] is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, transcript):
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(transcript, cb)
        await tailer.start()
        await tailer.start()
        await tailer.stop()


# ──────────────────────────────────────────────────────────────────────────
# Self-heal discovery tests
# ──────────────────────────────────────────────────────────────────────────


class TestSelfHealDiscovery:
    @pytest.mark.asyncio
    async def test_no_discovery_callback_is_no_op(self, tmp_path):
        cb = _Captor()
        tailer = CodexTmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
        )
        tailer._try_self_heal_repoint()
        assert tailer.stats["self_heal_repoints"] == 0

    @pytest.mark.asyncio
    async def test_discovery_called_when_path_missing(self, tmp_path):
        cb = _Captor()
        call_count = {"n": 0}

        def discover() -> Path | None:
            call_count["n"] += 1
            return None

        tailer = CodexTmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=discover,
        )
        tailer._try_self_heal_repoint()
        assert call_count["n"] == 1
        assert tailer.stats["self_heal_repoints"] == 0

    @pytest.mark.asyncio
    async def test_discovery_runs_every_tick_even_when_path_exists(
        self, transcript, tmp_path
    ):
        """#846: discovery now runs on EVERY tick, not only when the bound path
        is missing — ``codex resume --last`` writes a BRAND-NEW rollout while
        the old (bound) one still exists on disk. But a discovered path that is
        not strictly newer (here it doesn't exist → unreadable) must NOT
        repoint."""
        cb = _Captor()
        call_count = {"n": 0}

        def discover() -> Path | None:
            call_count["n"] += 1
            return tmp_path / "other.jsonl"  # never created → stat fails

        tailer = CodexTmuxTranscriptTailer(
            transcript, cb,  # bound path exists (touched by fixture)
            path_discovery=discover,
        )
        tailer._try_self_heal_repoint()
        assert call_count["n"] == 1, "discovery is consulted every tick now (#846)"
        assert tailer.stats["self_heal_repoints"] == 0
        assert tailer.transcript_path == transcript

    @pytest.mark.asyncio
    async def test_repoint_to_strictly_newer_rollout(self, tmp_path):
        """#846 PRIMARY: tailer bound to an OLDER file A; a NEWER rollout B
        (session_meta + task_complete) appears — the file ``codex resume
        --last`` wrote. A tick must repoint A→B, reset the offset to 0
        (seek_to_start), and drain the accumulated task_complete through the
        callback so the wedged inflight head resolves."""
        cb = _Captor()
        cwd = str(tmp_path)
        old = tmp_path / "rollout-old.jsonl"
        _write_jsonl(old, [
            _session_meta(cwd=cwd, session_id="old-uuid"),
            _agent_message("stale"),
            _task_complete(last_agent_message="Old reply."),
        ])
        new = tmp_path / "rollout-new.jsonl"
        _write_jsonl(new, [
            _session_meta(cwd=cwd, session_id="new-uuid"),
            _agent_message("fresh"),
            _task_complete(last_agent_message="Resumed reply."),
        ])
        # B strictly newer than A.
        old_t = time.time() - 100
        new_t = time.time()
        os.utime(old, (old_t, old_t))
        os.utime(new, (new_t, new_t))

        tailer = CodexTmuxTranscriptTailer(
            old, cb,
            path_discovery=lambda: new,
        )
        # Bind offset to A's EOF (as _start_tailer does on a warm resume) so we
        # prove seek_to_start=True resets it to 0.
        tailer.set_offset(old.stat().st_size)
        assert tailer.offset > 0

        tailer._try_self_heal_repoint()
        assert tailer.transcript_path == new
        assert tailer.offset == 0
        assert tailer.stats["self_heal_repoints"] == 1

        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "Resumed reply."

    @pytest.mark.asyncio
    async def test_no_repoint_when_bound_is_newest(self, tmp_path):
        """#846 regression: when the bound file IS the newest rollout (normal
        operation — the active rollout is the one being written), discovery
        returns it and we no-op. No flap."""
        cb = _Captor()
        bound = tmp_path / "rollout-bound.jsonl"
        _write_jsonl(bound, [_session_meta(cwd=str(tmp_path))])
        older = tmp_path / "rollout-older.jsonl"
        _write_jsonl(older, [_session_meta(cwd=str(tmp_path))])
        now = time.time()
        os.utime(older, (now - 50, now - 50))
        os.utime(bound, (now, now))

        # Discovery returns the OLDER file — the bound one is strictly newer.
        tailer = CodexTmuxTranscriptTailer(
            bound, cb,
            path_discovery=lambda: older,
        )
        tailer._try_self_heal_repoint()
        assert tailer.transcript_path == bound
        assert tailer.stats["self_heal_repoints"] == 0

    @pytest.mark.asyncio
    async def test_no_repoint_when_discovered_not_strictly_newer(self, tmp_path):
        """#846 edge: discovered file EXISTS and differs from the bound path but
        its mtime is EQUAL (or older) — do NOT repoint (strictly-newer guard,
        the inverted #291 never-repoint-backward rule)."""
        cb = _Captor()
        bound = tmp_path / "rollout-a.jsonl"
        _write_jsonl(bound, [_session_meta(cwd=str(tmp_path))])
        other = tmp_path / "rollout-b.jsonl"
        _write_jsonl(other, [_session_meta(cwd=str(tmp_path))])
        # Identical mtimes → not strictly newer.
        stamp = time.time() - 10
        os.utime(bound, (stamp, stamp))
        os.utime(other, (stamp, stamp))

        tailer = CodexTmuxTranscriptTailer(
            bound, cb,
            path_discovery=lambda: other,
        )
        tailer._try_self_heal_repoint()
        assert tailer.transcript_path == bound
        assert tailer.stats["self_heal_repoints"] == 0

    @pytest.mark.asyncio
    async def test_discovery_rebinds_with_seek_to_start(self, tmp_path):
        """Discovery returning a fresh path rebinds at byte 0 (seek_to_start)."""
        cb = _Captor()
        real_path = tmp_path / "real.jsonl"
        _write_jsonl(real_path, [
            _agent_message("discoverable"),
            _task_complete(last_agent_message="Found response."),
        ])
        tailer = CodexTmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=lambda: real_path,
        )
        tailer._try_self_heal_repoint()
        assert tailer.transcript_path == real_path
        assert tailer.offset == 0
        assert tailer.stats["self_heal_repoints"] == 1

        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "Found response."

    @pytest.mark.asyncio
    async def test_discovery_same_path_is_no_op(self, tmp_path):
        cb = _Captor()
        target = tmp_path / "same.jsonl"
        tailer = CodexTmuxTranscriptTailer(
            target, cb,
            path_discovery=lambda: target,
        )
        tailer._try_self_heal_repoint()
        assert tailer.stats["self_heal_repoints"] == 0

    @pytest.mark.asyncio
    async def test_discovery_exception_swallowed(self, tmp_path):
        cb = _Captor()

        def discover() -> Path | None:
            raise OSError("simulated hiccup")

        tailer = CodexTmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=discover,
        )
        tailer._try_self_heal_repoint()  # must not raise
        assert tailer.stats["self_heal_repoints"] == 0

    @pytest.mark.asyncio
    async def test_self_heal_integration_via_background_loop(self, tmp_path):
        """End-to-end: non-existent placeholder → discovery returns real path → callback."""
        cb = _Captor()
        real_path = tmp_path / "real.jsonl"
        state = {"ready": False}

        def discover() -> Path | None:
            return real_path if state["ready"] else None

        tailer = CodexTmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            fallback_poll_sec=0.02,
            active_poll_sec=0.01,
            path_discovery=discover,
        )
        await tailer.start()
        try:
            await asyncio.sleep(0.06)
            assert cb.responses == []

            _write_jsonl(real_path, [
                _agent_message("discovered"),
                _task_complete(last_agent_message="Discovered response."),
            ])
            state["ready"] = True

            for _ in range(50):
                await asyncio.sleep(0.02)
                if cb.responses:
                    break
            assert len(cb.responses) == 1
            assert cb.responses[0].text == "Discovered response."
            assert tailer.stats["self_heal_repoints"] == 1
        finally:
            await tailer.stop()


# ──────────────────────────────────────────────────────────────────────────
# _discover_codex_rollout function — cwd-filtered mtime scan
# ──────────────────────────────────────────────────────────────────────────


class TestDiscoverCodexRollout:
    """Unit tests for the ``_discover_codex_rollout`` standalone helper.

    We don't touch ~/.codex — we monkey-patch the sessions_root glob by
    writing fake rollout files in tmp_path and passing a working_dir
    that matches one of them.
    """

    @pytest.fixture(autouse=True)
    def _clear_codex_home(self, monkeypatch):
        # Discovery now honors CODEX_HOME; clear it so these tests
        # deterministically exercise the ~/.codex (Path.home) fallback.
        monkeypatch.delenv("CODEX_HOME", raising=False)

    def _fake_rollout(
        self, parent: Path, name: str, cwd: str, *, mtime_offset: float = 0,
    ) -> Path:
        """Write a minimal rollout with a session_meta line for the given cwd."""
        p = parent / name
        meta = {
            "type": "session_meta",
            "payload": {"id": "test-uuid", "cwd": cwd, "cli_version": "0.125.0"},
        }
        p.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        if mtime_offset:
            t = time.time() + mtime_offset
            os.utime(p, (t, t))
        return p

    def test_returns_none_when_no_sessions_root(self, tmp_path, monkeypatch):
        """No ~/.codex/sessions → returns None, no crash."""
        monkeypatch.setattr(
            "pinky_daemon.codex_home.Path.home",
            lambda: tmp_path,
        )
        result = _discover_codex_rollout("/some/cwd")
        assert result is None

    def test_picks_matching_cwd(self, tmp_path, monkeypatch):
        """Returns the file whose session_meta.payload.cwd resolves to working_dir."""
        sessions = tmp_path / ".codex" / "sessions" / "2026" / "06" / "17"
        sessions.mkdir(parents=True)

        target_cwd = str(tmp_path / "my-agent")
        other_cwd = str(tmp_path / "other-agent")
        os.makedirs(target_cwd, exist_ok=True)

        self._fake_rollout(sessions, "rollout-old.jsonl", other_cwd, mtime_offset=-10)
        match = self._fake_rollout(sessions, "rollout-new.jsonl", target_cwd, mtime_offset=0)

        monkeypatch.setattr(
            "pinky_daemon.codex_home.Path.home",
            lambda: tmp_path,
        )
        result = _discover_codex_rollout(target_cwd)
        assert result == match

    def test_newest_matching_selected(self, tmp_path, monkeypatch):
        """When multiple files match the cwd, the newest (highest mtime) wins."""
        sessions = tmp_path / ".codex" / "sessions" / "2026" / "06" / "17"
        sessions.mkdir(parents=True)

        target_cwd = str(tmp_path / "agent")
        os.makedirs(target_cwd, exist_ok=True)

        self._fake_rollout(sessions, "rollout-old.jsonl", target_cwd, mtime_offset=-30)
        newer = self._fake_rollout(sessions, "rollout-newer.jsonl", target_cwd, mtime_offset=-5)

        monkeypatch.setattr(
            "pinky_daemon.codex_home.Path.home",
            lambda: tmp_path,
        )
        result = _discover_codex_rollout(target_cwd)
        assert result == newer

    def test_no_match_returns_none(self, tmp_path, monkeypatch):
        """No file matches the requested cwd → returns None."""
        sessions = tmp_path / ".codex" / "sessions" / "2026" / "06" / "17"
        sessions.mkdir(parents=True)
        self._fake_rollout(sessions, "rollout-other.jsonl", "/some/other/cwd")

        monkeypatch.setattr(
            "pinky_daemon.codex_home.Path.home",
            lambda: tmp_path,
        )
        result = _discover_codex_rollout("/requested/cwd")
        assert result is None

    def test_unreadable_candidate_skipped(self, tmp_path, monkeypatch):
        """Candidates that raise on open/parse are skipped; valid ones still found."""
        sessions = tmp_path / ".codex" / "sessions"
        sessions.mkdir(parents=True)

        target_cwd = str(tmp_path / "agent")
        os.makedirs(target_cwd, exist_ok=True)

        # Write a corrupt file (invalid JSON).
        bad = sessions / "rollout-bad.jsonl"
        bad.write_text("{not json\n", encoding="utf-8")
        os.utime(bad, times=(time.time() - 5, time.time() - 5))

        good = self._fake_rollout(sessions, "rollout-good.jsonl", target_cwd, mtime_offset=0)

        monkeypatch.setattr(
            "pinky_daemon.codex_home.Path.home",
            lambda: tmp_path,
        )
        result = _discover_codex_rollout(target_cwd)
        assert result == good

    def test_honors_codex_home_env(self, tmp_path, monkeypatch):
        """CODEX_HOME points discovery at the right session store; Path.home is NOT used."""
        codex_home = tmp_path / "custom-codex"
        sessions = codex_home / "sessions" / "2026" / "06" / "17"
        sessions.mkdir(parents=True)
        target_cwd = str(tmp_path / "agent")
        os.makedirs(target_cwd, exist_ok=True)
        match = self._fake_rollout(sessions, "rollout-x.jsonl", target_cwd)

        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        # Point Path.home at an EMPTY dir to prove it is never consulted.
        empty_home = tmp_path / "wrong-home"
        empty_home.mkdir()
        monkeypatch.setattr(
            "pinky_daemon.codex_home.Path.home",
            lambda: empty_home,
        )
        result = _discover_codex_rollout(target_cwd)
        assert result == match
