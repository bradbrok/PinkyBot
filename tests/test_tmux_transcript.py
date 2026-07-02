"""Tests for ``tmux_transcript`` — the response capture pipeline.

PR8b of the #486 sequence. Tests are pure-Python (no tmux, no claude,
no asyncio scheduler tricks) — the tailer is a function of (file
content, wake events) → callbacks, and we test exactly that.

Coverage shape:
- ``_TurnBuffer``: per-entry accumulation, drain shape, multi-block
  turns, missing/malformed blocks.
- ``TmuxTranscriptTailer``: read-once semantics, partial-line
  tolerance, offset replay, file rotation/truncation, callback errors,
  cold-start replay where stop_hook_summary has no preceding buffer.
- Active-poll mode toggling.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from pinky_daemon.tmux_transcript import (
    TmuxTranscriptTailer,
    TurnResponse,
    _TurnBuffer,
)

# ──────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ──────────────────────────────────────────────────────────────────────────


def _assistant(
    text: str = "",
    *,
    thinking: str = "",
    tool_use: dict | None = None,
    stop_reason: str = "end_turn",
    usage: dict | None = None,
    ts: str = "2026-05-14T05:00:00.000Z",
) -> dict:
    """Build a synthetic ``assistant`` transcript entry."""
    content: list[dict] = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    if tool_use is not None:
        content.append({
            "type": "tool_use",
            "name": tool_use.get("name", "Bash"),
            "input": tool_use.get("input", {}),
            "id": tool_use.get("id", "tool_1"),
        })
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-opus-test",
            "content": content,
            "stop_reason": stop_reason,
            "usage": usage or {"input_tokens": 100, "output_tokens": 50},
        },
    }


def _user(text: str = "hi", ts: str = "2026-05-14T04:59:59.000Z") -> dict:
    """Build a synthetic ``user`` transcript entry."""
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _stop_hook_summary(
    *,
    prevented: bool = False,
    ts: str = "2026-05-14T05:00:01.500Z",
) -> dict:
    """Build a synthetic ``stop_hook_summary`` entry."""
    return {
        "type": "system",
        "subtype": "stop_hook_summary",
        "timestamp": ts,
        "preventedContinuation": prevented,
        "hookCount": 1,
        "stopReason": "",
        "hasOutput": False,
        "level": "suggestion",
    }


def _write_jsonl(path: Path, entries: list[dict], *, trailing_newline: bool = True) -> int:
    """Append entries to a JSONL file. Returns total bytes written."""
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
# _TurnBuffer tests
# ──────────────────────────────────────────────────────────────────────────


class TestTurnBuffer:
    """Pure-data tests for the per-turn accumulator."""

    def test_single_assistant_then_stop(self):
        buf = _TurnBuffer()
        assert buf.feed(_assistant(text="hello world")) is False
        assert buf.feed(_stop_hook_summary()) is True

        resp = buf.drain()
        assert resp.text == "hello world"
        assert resp.thinking == ""
        assert resp.tool_uses == []
        assert resp.stop_reason == "end_turn"
        assert resp.assistant_entry_count == 1
        assert resp.usage == {"input_tokens": 100, "output_tokens": 50}

    def test_multi_block_turn(self):
        """Tool-use loop: thinking → text → tool_use → text → end_turn."""
        buf = _TurnBuffer()
        buf.feed(_assistant(
            thinking="planning the call",
            text="let me check that",
            tool_use={"name": "Bash", "input": {"cmd": "ls"}, "id": "tu_1"},
            stop_reason="tool_use",
        ))
        # second assistant entry after tool_result comes in
        buf.feed(_assistant(
            text="here's the answer",
            stop_reason="end_turn",
        ))
        closed = buf.feed(_stop_hook_summary())
        assert closed is True

        resp = buf.drain()
        assert resp.text == "let me check that\nhere's the answer"
        assert resp.thinking == "planning the call"
        assert resp.tool_uses == [
            {"name": "Bash", "input": {"cmd": "ls"}, "id": "tu_1"},
        ]
        # Last assistant entry's stop_reason wins
        assert resp.stop_reason == "end_turn"
        assert resp.assistant_entry_count == 2

    def test_drain_resets_buffer(self):
        """A drained buffer accepts the next turn cleanly."""
        buf = _TurnBuffer()
        buf.feed(_assistant(text="first"))
        buf.feed(_stop_hook_summary())
        buf.drain()
        assert buf.is_empty

        buf.feed(_assistant(text="second"))
        buf.feed(_stop_hook_summary())
        resp = buf.drain()
        assert resp.text == "second"

    def test_user_and_unknown_entries_ignored(self):
        """user / attachment / queue-operation / unknown types don't pollute."""
        buf = _TurnBuffer()
        buf.feed(_user())
        buf.feed({"type": "attachment", "attachment": {"foo": "bar"}})
        buf.feed({"type": "queue-operation", "operation": "enqueue"})
        buf.feed({"type": "ai-title", "aiTitle": "Test Session"})
        buf.feed({"type": "last-prompt", "lastPrompt": "..."})
        buf.feed({"type": "future-type-we-do-not-know-about", "foo": "bar"})
        assert buf.is_empty

        # Now a real assistant + stop closes a clean turn.
        buf.feed(_assistant(text="real response"))
        buf.feed(_stop_hook_summary())
        resp = buf.drain()
        assert resp.text == "real response"

    def test_malformed_assistant_entry(self):
        """Defensive against schema drift — bad shapes are skipped silently."""
        buf = _TurnBuffer()
        # message field missing
        buf.feed({"type": "assistant", "timestamp": "2026-05-14T05:00:00Z"})
        # message field not a dict
        buf.feed({"type": "assistant", "message": "not a dict"})
        # content not a list
        buf.feed({"type": "assistant", "message": {"content": "not a list"}})
        # content block not a dict
        buf.feed({"type": "assistant", "message": {"content": [None, 42, "str"]}})
        # block with unknown type
        buf.feed({
            "type": "assistant",
            "message": {"content": [{"type": "future_block_type", "data": "x"}]},
        })
        # Buffer must not raise on any of these shapes. Count is 4 because
        # the "message is a string" shape early-returns before incrementing
        # (the early-return defends against AttributeError on .get); the
        # other 4 are dict-shaped (some via the ``or {}`` fallback) and
        # do increment. Either count is defensible; the invariant we care
        # about is "no raise + no spurious text/tool_uses".
        assert buf._assistant_count == 4
        assert buf._text_blocks == []
        assert buf._tool_uses == []

        # A real assistant after malformed entries still works.
        buf.feed(_assistant(text="recovered"))
        buf.feed(_stop_hook_summary())
        resp = buf.drain()
        assert resp.text == "recovered"

    def test_empty_text_blocks_skipped(self):
        """Empty string text/thinking blocks don't pollute the output."""
        buf = _TurnBuffer()
        buf.feed({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": ""},
                {"type": "text", "text": "real"},
                {"type": "thinking", "thinking": ""},
            ], "stop_reason": "end_turn"},
        })
        buf.feed(_stop_hook_summary())
        resp = buf.drain()
        assert resp.text == "real"
        assert resp.thinking == ""

    def test_duration_ms_from_timestamps(self):
        buf = _TurnBuffer()
        buf.feed(_assistant(text="x", ts="2026-05-14T05:00:00.000Z"))
        buf.feed(_stop_hook_summary(ts="2026-05-14T05:00:01.500Z"))
        resp = buf.drain()
        assert resp.duration_ms == 1500

    def test_duration_ms_zero_when_timestamps_missing(self):
        buf = _TurnBuffer()
        buf.feed({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x"}], "stop_reason": "end_turn"}})
        buf.feed({"type": "system", "subtype": "stop_hook_summary"})
        resp = buf.drain()
        assert resp.duration_ms == 0

    def test_prevented_continuation_propagates(self):
        buf = _TurnBuffer()
        buf.feed(_assistant(text="x"))
        buf.feed(_stop_hook_summary(prevented=True))
        resp = buf.drain(prevented_continuation=True)
        assert resp.prevented_continuation is True

    def test_is_empty_after_drain(self):
        buf = _TurnBuffer()
        buf.feed(_assistant(text="x"))
        assert buf.is_empty is False
        buf.feed(_stop_hook_summary())
        buf.drain()
        assert buf.is_empty is True


# ──────────────────────────────────────────────────────────────────────────
# TmuxTranscriptTailer tests
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def transcript(tmp_path) -> Path:
    """A fresh empty transcript file in a tmp dir."""
    p = tmp_path / "session.jsonl"
    p.touch()
    return p


class _Captor:
    """Captures callback invocations for assertions."""

    def __init__(self) -> None:
        self.responses: list[TurnResponse] = []
        self.raise_on_call: Exception | None = None

    async def __call__(self, response: TurnResponse) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.responses.append(response)


class TestTailerReadOnce:
    """Synchronous semantics — drive read_once() directly, no background task."""

    @pytest.mark.asyncio
    async def test_empty_file_no_callbacks(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        assert cb.responses == []
        assert tailer.offset == 0

    @pytest.mark.asyncio
    async def test_nonexistent_file_no_error(self, tmp_path):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(tmp_path / "does-not-exist.jsonl", cb)
        consumed = await tailer.read_once()
        assert consumed == 0
        assert cb.responses == []

    @pytest.mark.asyncio
    async def test_single_turn_fires_callback(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _user(text="hi"),
            _assistant(text="hello back"),
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "hello back"
        assert tailer.offset == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_multi_turn_fires_callback_per_turn(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _user(text="prompt 1"),
            _assistant(text="reply 1"),
            _stop_hook_summary(ts="2026-05-14T05:00:01Z"),
            _user(text="prompt 2"),
            _assistant(text="reply 2"),
            _stop_hook_summary(ts="2026-05-14T05:00:03Z"),
        ])
        await tailer.read_once()
        assert [r.text for r in cb.responses] == ["reply 1", "reply 2"]

    @pytest.mark.asyncio
    async def test_offset_advances_only_on_complete_lines(self, transcript):
        """Partial trailing line (no \\n) is not consumed."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)

        # First write: one complete entry + a half-written entry (no newline).
        complete = json.dumps(_user(text="hi")) + "\n"
        partial = json.dumps(_assistant(text="par"))[:30]  # truncated mid-write
        transcript.write_text(complete + partial, encoding="utf-8")

        await tailer.read_once()
        assert cb.responses == []  # no stop_hook_summary yet
        # Offset advanced past the complete line only.
        assert tailer.offset == len(complete.encode("utf-8"))

        # Now finish the assistant entry and add stop_hook_summary.
        finish = json.dumps(_assistant(text="hello")) + "\n"
        finish += json.dumps(_stop_hook_summary()) + "\n"
        # Replace the partial chunk with the finished entries.
        transcript.write_text(complete + finish, encoding="utf-8")

        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "hello"

    @pytest.mark.asyncio
    async def test_replay_from_persisted_offset(self, transcript):
        """Two turns written, tailer set to offset after turn 1 — only turn 2 fires."""
        cb = _Captor()
        # Pre-populate with turn 1.
        _write_jsonl(transcript, [
            _user(text="p1"),
            _assistant(text="r1"),
            _stop_hook_summary(),
        ])
        turn1_size = transcript.stat().st_size

        # Append turn 2.
        _append_jsonl(transcript, [
            _user(text="p2"),
            _assistant(text="r2"),
            _stop_hook_summary(),
        ])

        # Construct tailer at offset = turn1_size; only turn 2 should fire.
        tailer = TmuxTranscriptTailer(transcript, cb)
        tailer.set_offset(turn1_size)
        await tailer.read_once()

        assert len(cb.responses) == 1
        assert cb.responses[0].text == "r2"

    @pytest.mark.asyncio
    async def test_truncation_resets_offset(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _user(text="p1"),
            _assistant(text="r1"),
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert len(cb.responses) == 1
        assert tailer.offset > 0

        # Simulate file replacement (smaller content). Tailer should detect
        # and reset.
        transcript.write_text("", encoding="utf-8")
        await tailer.read_once()
        assert tailer.offset == 0

        # New content after truncation reads cleanly.
        _write_jsonl(transcript, [
            _assistant(text="post-truncate"),
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert cb.responses[-1].text == "post-truncate"

    @pytest.mark.asyncio
    async def test_cold_start_replay_skips_empty_buffer(self, transcript):
        """Mid-file resume past previous turns: stop_hook_summary fires for
        an empty buffer (we entered after the assistant entries). Tailer
        should NOT fire an empty callback."""
        cb = _Captor()
        # File contains only a stop_hook_summary (simulating mid-file resume
        # where we missed the earlier entries).
        _write_jsonl(transcript, [_stop_hook_summary()])

        tailer = TmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        assert cb.responses == []  # no spurious empty turn

    @pytest.mark.asyncio
    async def test_callback_exception_is_swallowed(self, transcript):
        """A misbehaving callback doesn't strand the tailer."""
        cb = _Captor()
        cb.raise_on_call = RuntimeError("downstream blew up")

        tailer = TmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _assistant(text="r1"),
            _stop_hook_summary(),
        ])
        await tailer.read_once()  # must not raise
        assert tailer.stats["callback_errors"] == 1
        assert tailer.stats["turns_fired"] == 1
        # Offset still advanced — we don't get stuck retrying a bad callback.
        assert tailer.offset == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_malformed_json_line_skipped(self, transcript):
        cb = _Captor()
        good_line = json.dumps(_assistant(text="good")) + "\n"
        bad_line = "{this is not valid json\n"
        stop_line = json.dumps(_stop_hook_summary()) + "\n"
        transcript.write_text(good_line + bad_line + stop_line, encoding="utf-8")

        tailer = TmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        assert tailer.stats["parse_errors"] == 1
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "good"

    @pytest.mark.asyncio
    async def test_set_transcript_path_seeks_to_eof_by_default(self, transcript, tmp_path):
        """Pushok's PR #496 round-1 Case 3 fix: rotating to a transcript
        that already has entries must NOT replay them. The default
        offset on swap is EOF, not 0.

        This protects against compact-resume, daemon-restart re-fire,
        and misconfigured test fixture cases where SessionStart fires
        with a path that already contains stop_hook_summary entries.
        Without seek-to-EOF, those would re-fire response_callback for
        every historical turn → reply-spam.
        """
        cb = _Captor()
        _write_jsonl(transcript, [
            _assistant(text="old"),
            _stop_hook_summary(),
        ])
        tailer = TmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        assert tailer.offset > 0

        # Rotate to a new transcript that already contains entries
        # (e.g. compact-resume case where Claude Code resumed an existing
        # session). Default offset is EOF, so existing entries are NOT
        # re-fired.
        new_path = tmp_path / "session2.jsonl"
        _write_jsonl(new_path, [
            _assistant(text="historical — must not replay"),
            _stop_hook_summary(),
        ])
        responses_before = len(cb.responses)
        tailer.set_transcript_path(new_path)
        new_size = new_path.stat().st_size
        assert tailer.offset == new_size, "swap must default to EOF, not 0"
        assert tailer.stats["rotations"] == 1

        await tailer.read_once()
        # Critical assertion: existing entries in the swapped-in file are
        # NOT re-fired. Replay would have produced a callback for "historical".
        assert len(cb.responses) == responses_before

        # New entries appended AFTER the swap fire correctly.
        _append_jsonl(new_path, [
            _assistant(text="new"),
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert cb.responses[-1].text == "new"

    @pytest.mark.asyncio
    async def test_set_transcript_path_fresh_file_offset_is_zero(self, transcript, tmp_path):
        """Swap-to-fresh-file (size==0) yields offset==0 by construction —
        no behavior change vs. pre-fix on the contract'd path."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)

        fresh = tmp_path / "fresh.jsonl"
        fresh.touch()  # exists but empty
        tailer.set_transcript_path(fresh)
        assert tailer.offset == 0

    @pytest.mark.asyncio
    async def test_set_offset_zero_allows_explicit_backfill(self, transcript, tmp_path):
        """The seek-to-EOF default doesn't preclude backfill: callers
        who want to re-read the whole file can call set_offset(0)
        explicitly after the swap. Pinned because the contract is in
        the docstring and the migration note depends on this path
        working for ops use cases (re-process a session's history)."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)

        target = tmp_path / "session.jsonl"
        _write_jsonl(target, [
            _assistant(text="t1"), _stop_hook_summary(),
            _assistant(text="t2"), _stop_hook_summary(),
        ])
        tailer.set_transcript_path(target)
        # Default would skip both; explicit backfill re-reads them.
        tailer.set_offset(0)
        await tailer.read_once()
        assert [r.text for r in cb.responses] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_set_transcript_path_drains_buffer_across_swap(
        self, transcript, tmp_path,
    ):
        """Pushok's PR #496 round-2 Case 2': swapping the transcript
        path must also drain the in-memory turn buffer.

        Failure mode without the drain:
        1. Session X is mid-turn — tailer has accumulated assistant text
           in its buffer but no ``stop_hook_summary`` has landed yet.
        2. Session X gets killed (force_restart). Session Y spawns with
           a fresh transcript path. SessionStart hook fires
           ``set_transcript_path(new_path)``.
        3. Without the drain, ``_buffer`` still holds X's partial text.
           When Y produces its first complete turn, the callback gets
           ``X_partial + "\\n" + Y_text`` — dead-session content leaking
           into Y's first reply.

        Pre-fix: this test would fail with the callback firing on text
        like ``"partial from X\\nresponse from Y"``. With the drain:
        callback fires with exactly ``"response from Y"``.
        """
        cb = _Captor()

        # File A: partial turn (assistant entry, NO stop_hook_summary).
        # This is the in-flight state when a session dies.
        _write_jsonl(transcript, [
            _assistant(text="partial from X"),
        ])
        tailer = TmuxTranscriptTailer(transcript, cb)
        await tailer.read_once()
        # Buffer should now hold "partial from X" but no callback fired
        # (no stop_hook_summary yet — turn is incomplete).
        assert len(cb.responses) == 0
        assert not tailer._buffer.is_empty, (
            "buffer should have accumulated assistant text from session X"
        )

        # File B: fresh transcript for the new (post-restart) session.
        new_path = tmp_path / "session_y.jsonl"
        new_path.touch()  # exists but empty

        # Swap. The fix: this should drain X's partial text out of the
        # buffer, preventing leak into Y's first turn.
        tailer.set_transcript_path(new_path)
        assert tailer._buffer.is_empty, (
            "set_transcript_path must drain the buffer to prevent "
            "cross-session text leak (Pushok Case 2')"
        )

        # Y produces a complete turn.
        _append_jsonl(new_path, [
            _assistant(text="response from Y"),
            _stop_hook_summary(),
        ])
        await tailer.read_once()

        # Callback fires with ONLY Y's text — no "partial from X" prefix.
        assert len(cb.responses) == 1, (
            "exactly one turn should have fired"
        )
        assert cb.responses[0].text == "response from Y", (
            f"expected clean Y response, got {cb.responses[0].text!r} — "
            f"if this contains 'partial from X', the buffer-drain "
            f"regression has reopened"
        )

    @pytest.mark.asyncio
    async def test_swap_during_turn_callback_aborts_old_chunk(
        self, transcript, tmp_path,
    ):
        """A path-changing ``set_transcript_path`` can land from another
        task while ``_read_and_dispatch`` is parked in an awaited turn
        callback (late SessionStart hook, #565 first-bind recovery).

        Pre-fix, the read loop kept feeding the REST of the old file's
        chunk into the buffer the swap just drained (dead-session text
        leaking into the new session) and then added the old chunk's
        byte length to the offset the swap just set for the NEW file
        (offset corruption: skipped bytes or a bogus shrank-branch
        replay). The swap-generation check discards the remainder of
        the chunk and leaves the swapped-in offset untouched.
        """
        # File A (dying session): two complete turns in one chunk.
        _write_jsonl(transcript, [
            _assistant(text="A1"),
            _stop_hook_summary(),
            _assistant(text="A2 dead-session text"),
            _stop_hook_summary(),
        ])
        # File B (new session): one turn, to be read from byte 0.
        new_path = tmp_path / "session_b.jsonl"
        _write_jsonl(new_path, [
            _assistant(text="B1"),
            _stop_hook_summary(),
        ])

        responses: list[str] = []
        box: dict = {}

        async def cb(response: TurnResponse) -> None:
            responses.append(response.text)
            if len(responses) == 1:
                # Simulate the concurrent swap landing mid-callback.
                box["tailer"].set_transcript_path(new_path, seek_to_start=True)

        tailer = TmuxTranscriptTailer(transcript, cb)
        box["tailer"] = tailer

        await tailer.read_once()
        # Turn A2 belongs to the dead session and must NOT fire; the
        # offset must stay where the swap put it for file B.
        assert responses == ["A1"]
        assert tailer.offset == 0

        await tailer.read_once()
        assert responses == ["A1", "B1"]
        assert tailer.offset == new_path.stat().st_size


class TestTailerBackgroundLoop:
    """Drive the actual asyncio loop end-to-end (with shortened cadences)."""

    @pytest.mark.asyncio
    async def test_wake_picks_up_new_entries(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(
            transcript, cb,
            fallback_poll_sec=0.05,
            active_poll_sec=0.01,
        )
        await tailer.start()
        try:
            _write_jsonl(transcript, [
                _assistant(text="wake test"),
                _stop_hook_summary(),
            ])
            tailer.wake()
            # Give the loop a tick.
            for _ in range(20):
                await asyncio.sleep(0.02)
                if cb.responses:
                    break
            assert len(cb.responses) == 1
            assert cb.responses[0].text == "wake test"
        finally:
            await tailer.stop()

    @pytest.mark.asyncio
    async def test_fallback_poll_makes_progress_without_wake(self, transcript):
        """With no wake() calls, the poll-timeout path still reads new data."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(
            transcript, cb,
            fallback_poll_sec=0.02,
            active_poll_sec=0.01,
        )
        await tailer.start()
        try:
            _write_jsonl(transcript, [
                _assistant(text="poll test"),
                _stop_hook_summary(),
            ])
            # Wait up to ~0.5s without calling wake().
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
        tailer = TmuxTranscriptTailer(transcript, cb)
        await tailer.start()
        await tailer.stop()
        await tailer.stop()  # second call must not raise
        assert tailer.stats["running"] is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        await tailer.start()
        await tailer.start()  # second call must not spawn a second task
        await tailer.stop()

    @pytest.mark.asyncio
    async def test_mark_active_switches_cadence(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(
            transcript, cb,
            fallback_poll_sec=10.0,  # huge — would never fire in test window
            active_poll_sec=0.02,
        )
        await tailer.start()
        try:
            tailer.mark_active()  # switch to active cadence
            _write_jsonl(transcript, [
                _assistant(text="active poll"),
                _stop_hook_summary(),
            ])
            # The active cadence (20ms) should reach the file well within 0.5s.
            for _ in range(30):
                await asyncio.sleep(0.02)
                if cb.responses:
                    break
            assert len(cb.responses) == 1
            # And active flips back to False after the turn fires.
            assert tailer.stats["active"] is False
        finally:
            await tailer.stop()


class TestTailerStats:
    @pytest.mark.asyncio
    async def test_stats_shape(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        stats = tailer.stats
        assert {"turns_fired", "lines_read", "parse_errors", "callback_errors",
                "rotations", "offset", "buffer_empty", "active", "running"} <= set(stats)

    @pytest.mark.asyncio
    async def test_turns_fired_counter(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _assistant(text="a"), _stop_hook_summary(),
            _assistant(text="b"), _stop_hook_summary(),
            _assistant(text="c"), _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert tailer.stats["turns_fired"] == 3


# ──────────────────────────────────────────────────────────────────────────
# Pushok's PR #496 round-1 hardening — size cap, UTF-8, defense-in-depth
# ──────────────────────────────────────────────────────────────────────────


class TestSizeCap:
    """Bounded single-read memory (Pushok's Case 4a defense-in-depth)."""

    @pytest.mark.asyncio
    async def test_chunk_read_is_bounded(self, transcript, monkeypatch):
        """A single ``read_once`` should not pull more than
        ``_MAX_READ_CHUNK_BYTES``. Remaining data is consumed by the
        next iteration via the re-armed wake_event.

        Uses many small turns so each individual line fits within the
        shrunken test cap — verifies the cap bounds aggregate read
        without exercising the (degenerate) single-huge-line case.
        Production cap (10 MiB) is large enough that any realistic
        single JSONL line fits.
        """
        from pinky_daemon import tmux_transcript

        # Shrink cap to 2KB so multiple small turns exceed it.
        monkeypatch.setattr(tmux_transcript, "_MAX_READ_CHUNK_BYTES", 2048)

        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        # Write ~30 small turns. Each turn is roughly 400 bytes; 30 turns
        # ≈ 12 KB → well over the 2 KB cap.
        entries = []
        for i in range(30):
            entries.append(_assistant(text=f"turn {i}"))
            entries.append(_stop_hook_summary(
                ts=f"2026-05-14T05:00:{i:02d}.000Z",
            ))
        _write_jsonl(transcript, entries)
        first_size = transcript.stat().st_size
        assert first_size > 2048, "test fixture must exceed the cap"

        # First read consumes at most the cap; some turns fire, more pending.
        await tailer.read_once()
        assert tailer.offset < first_size, "first read must be cap-bounded"
        # wake_event is re-armed so the next loop tick picks up immediately.
        assert tailer._wake_event.is_set()
        turns_after_first = len(cb.responses)
        assert 0 < turns_after_first < 30, (
            f"first read should have fired some but not all turns: "
            f"{turns_after_first}/30"
        )

        # Drain the rest via successive reads.
        for _ in range(20):
            if tailer.offset >= first_size:
                break
            await tailer.read_once()
        assert tailer.offset == first_size
        assert len(cb.responses) == 30
        # No turn was lost or duplicated.
        assert [r.text for r in cb.responses] == [f"turn {i}" for i in range(30)]


class TestUtf8MultiByte:
    """UTF-8 byte-counting correctness for non-ASCII transcript content."""

    @pytest.mark.asyncio
    async def test_offset_advances_correctly_for_multi_byte_chars(self, transcript):
        """Cyrillic + CJK characters: bytes != chars. ``len(line.encode())``
        is the right unit for offset accounting, not ``len(line)``. Pins
        the existing implementation against a future regression."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        # Russian + Japanese + emoji — guarantees multi-byte UTF-8 sequences.
        text = "Привет, мир! こんにちは 🐈"
        _write_jsonl(transcript, [
            _assistant(text=text),
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == text
        # Offset must equal exact file byte size (not char count).
        assert tailer.offset == transcript.stat().st_size


# ──────────────────────────────────────────────────────────────────────────
# Self-heal discovery — #515.
#
# The tailer is correct without the SessionStart hook firing. On each
# poll tick, if the configured path doesn't exist and a discovery
# callback is set, the tailer scans for the real transcript and rebinds.
# This removes the brittle "hook MUST fire OR the daemon never sees
# the response" coupling that caused #515.
# ──────────────────────────────────────────────────────────────────────────


class TestSetTranscriptPathSeekToStart:
    """The ``seek_to_start`` kwarg on ``set_transcript_path`` — supports
    the placeholder→real transition where the discovered file was
    created fresh and we want byte 0 onward (not EOF, which would skip
    the response we're trying to capture)."""

    @pytest.mark.asyncio
    async def test_seek_to_start_overrides_eof_default(
        self, transcript, tmp_path,
    ):
        """``seek_to_start=True`` forces offset 0 even when the new
        file has content. Mirrors the default-EOF test
        (``test_set_transcript_path_seeks_to_eof_by_default``) with the
        opposite expectation."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)

        new_path = tmp_path / "fresh-session.jsonl"
        _write_jsonl(new_path, [
            _assistant(text="cold-start response"),
            _stop_hook_summary(),
        ])
        tailer.set_transcript_path(new_path, seek_to_start=True)
        assert tailer.offset == 0, "seek_to_start must override default-EOF"

        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "cold-start response"

    @pytest.mark.asyncio
    async def test_default_kwarg_is_seek_to_eof(self, transcript, tmp_path):
        """Default ``seek_to_start=False`` preserves the existing
        EOF-seek behavior. Belt + suspenders against the new kwarg
        accidentally flipping the default."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)

        new_path = tmp_path / "session-with-history.jsonl"
        _write_jsonl(new_path, [
            _assistant(text="historical — must not replay"),
            _stop_hook_summary(),
        ])
        tailer.set_transcript_path(new_path)
        assert tailer.offset == new_path.stat().st_size


class TestSelfHealDiscovery:
    """The ``path_discovery`` callback on the tailer — mtime-scan
    fallback when the configured path doesn't exist (e.g. SessionStart
    hook never fired). Bug #515 root cause + structural fix."""

    @pytest.mark.asyncio
    async def test_no_discovery_callback_is_no_op(self, tmp_path):
        """Backwards compat: tailer constructed without ``path_discovery``
        works exactly as before. No discovery attempt, no crash, no
        spurious rebinds."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(
            tmp_path / "does-not-exist.jsonl", cb,
        )
        tailer._try_self_heal_repoint()  # explicit call (no asyncio needed)
        assert tailer.stats["self_heal_repoints"] == 0
        assert tailer.transcript_path == tmp_path / "does-not-exist.jsonl"

    @pytest.mark.asyncio
    async def test_discovery_called_when_path_missing(self, tmp_path):
        """When the path doesn't exist on a poll tick, the discovery
        callback is invoked. This is the load-bearing assertion for
        #515 — the daemon must self-heal even when the SessionStart
        hook never fires."""
        cb = _Captor()
        call_count = {"n": 0}

        def discover() -> Path | None:
            call_count["n"] += 1
            return None  # no transcript yet — still cold-starting

        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=discover,
        )
        tailer._try_self_heal_repoint()
        assert call_count["n"] == 1
        # No rebind because discovery returned None.
        assert tailer.stats["self_heal_repoints"] == 0
        assert tailer.transcript_path == tmp_path / "placeholder.jsonl"

    @pytest.mark.asyncio
    async def test_discovery_not_called_when_path_exists(
        self, transcript, tmp_path,
    ):
        """When the path DOES exist, discovery must NOT run — cheap
        early-return. We don't want to pay the project_dir glob on
        every tick of a healthy session."""
        cb = _Captor()
        call_count = {"n": 0}

        def discover() -> Path | None:
            call_count["n"] += 1
            return tmp_path / "newer.jsonl"

        tailer = TmuxTranscriptTailer(
            transcript, cb,  # path exists (touch'd by fixture)
            path_discovery=discover,
        )
        tailer._try_self_heal_repoint()
        assert call_count["n"] == 0, (
            "discovery must not run when the watched path exists"
        )

    @pytest.mark.asyncio
    async def test_discovery_returning_path_rebinds_with_seek_to_start(
        self, tmp_path,
    ):
        """When discovery returns a fresh transcript path, the tailer
        rebinds and seeks to byte 0 — the response written between
        cold-start and now must be readable from the start of the file.

        Without ``seek_to_start=True``, set_transcript_path's default
        EOF-seek would skip the in-flight response entirely."""
        cb = _Captor()

        real_path = tmp_path / "real-session-after-cold-start.jsonl"
        _write_jsonl(real_path, [
            _assistant(text="the response we'd lose without seek_to_start"),
            _stop_hook_summary(),
        ])

        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=lambda: real_path,
        )
        tailer._try_self_heal_repoint()
        assert tailer.transcript_path == real_path
        assert tailer.offset == 0
        assert tailer.stats["self_heal_repoints"] == 1

        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].text == (
            "the response we'd lose without seek_to_start"
        )

    @pytest.mark.asyncio
    async def test_discovery_returning_same_path_is_no_op(self, tmp_path):
        """Discovery returning the path we already watch is fine — no
        rebind, no stats bump, no log spam. Defends against an over-
        eager mtime-scan flapping when the path exists at glob time
        but `_path.exists()` raced False (filesystem hiccup)."""
        cb = _Captor()
        target = tmp_path / "same.jsonl"
        # Note: target doesn't exist yet, so discovery fires; but it
        # returns the same path we already watch → no rebind.
        tailer = TmuxTranscriptTailer(
            target, cb,
            path_discovery=lambda: target,
        )
        tailer._try_self_heal_repoint()
        assert tailer.stats["self_heal_repoints"] == 0
        assert tailer.transcript_path == target

    @pytest.mark.asyncio
    async def test_discovery_exception_swallowed_and_logged(self, tmp_path):
        """A raised discovery callback must NOT crash the tail loop.
        The transient filesystem error gets logged; the next poll tick
        retries."""
        cb = _Captor()

        def discover() -> Path | None:
            raise OSError("simulated filesystem hiccup")

        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=discover,
        )
        # The critical assertion: no raise propagates out.
        tailer._try_self_heal_repoint()
        assert tailer.stats["self_heal_repoints"] == 0

    @pytest.mark.asyncio
    async def test_self_heal_integration_via_background_loop(self, tmp_path):
        """End-to-end: start the tailer with a non-existent placeholder
        path, then have discovery return a real path with a complete
        turn in it. The background loop discovers the path, rebinds,
        reads from byte 0, fires the callback.

        This is the #515 fix in its entirety, exercised at the asyncio
        layer the production tailer runs at."""
        cb = _Captor()
        real_path = tmp_path / "real.jsonl"
        # File doesn't exist yet — simulate cold-start state where
        # claude hasn't written anything.
        discovery_state = {"file_ready": False}

        def discover() -> Path | None:
            if discovery_state["file_ready"]:
                return real_path
            return None  # claude still cold-starting

        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            fallback_poll_sec=0.02,
            active_poll_sec=0.01,
            path_discovery=discover,
        )
        await tailer.start()
        try:
            # First few ticks: discovery returns None (no file yet).
            await asyncio.sleep(0.05)
            assert tailer.stats["self_heal_repoints"] == 0
            assert cb.responses == []

            # Now claude finishes the splash and writes a response.
            _write_jsonl(real_path, [
                _assistant(text="discovered cold-start response"),
                _stop_hook_summary(),
            ])
            discovery_state["file_ready"] = True

            # Within a few poll ticks, discovery should fire + rebind
            # + read the file from byte 0 + fire the callback.
            for _ in range(50):
                await asyncio.sleep(0.02)
                if cb.responses:
                    break
            assert len(cb.responses) == 1
            assert cb.responses[0].text == "discovered cold-start response"
            assert tailer.stats["self_heal_repoints"] == 1
            assert tailer.transcript_path == real_path
        finally:
            await tailer.stop()

    # ── #291: self-heal mtime-floor (stale-clobber guard) ────────────────

    @pytest.mark.asyncio
    async def test_self_heal_skips_discovery_older_than_bind(self, tmp_path):
        """#291: once a real path is bound (SessionStart hook), the self-heal
        must NEVER repoint to a transcript whose mtime predates that bind — the
        stale-previous-session clobber that wedged the tailer on a frozen file
        (frozen ``transcript_mtime`` → watchdog false-wedge → force_restart)."""
        cb = _Captor()
        old = tmp_path / "old-session.jsonl"
        _write_jsonl(old, [_assistant(text="prev session"), _stop_hook_summary()])
        os.utime(old, (time.time() - 3600, time.time() - 3600))  # clearly stale
        fresh = tmp_path / "fresh-session.jsonl"  # hook announced it; not on disk yet

        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=lambda: old,
        )
        # SessionStart hook binds the fresh (not-yet-written) path → arms the floor.
        tailer.set_transcript_path(fresh, seek_to_start=True)
        assert tailer.transcript_path == fresh

        # Next poll: fresh is missing → self-heal → discovery returns the OLD file.
        tailer._try_self_heal_repoint()

        assert tailer.transcript_path == fresh, "must not clobber to the stale file"
        assert tailer.stats["self_heal_repoints"] == 0
        assert tailer.stats["self_heal_stale_skips"] == 1

    @pytest.mark.asyncio
    async def test_self_heal_strict_floor_blocks_recent_previous_session(self, tmp_path):
        """#291: the floor is STRICT (no slack). The clobber target is often the
        *immediately* preceding session, whose last write can be only seconds
        before the new bind — a positive slack window would re-admit it and the
        fix would silently regress. A file only 5s older than the bind must
        still be refused. (This test fails under a 60s slack and passes under a
        strict ``<`` comparison.)"""
        cb = _Captor()
        recent = tmp_path / "recent-prev.jsonl"
        _write_jsonl(recent, [_assistant(text="x"), _stop_hook_summary()])
        os.utime(recent, (time.time() - 5, time.time() - 5))  # only 5s stale
        fresh = tmp_path / "fresh.jsonl"

        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=lambda: recent,
        )
        tailer.set_transcript_path(fresh)  # arms the floor at ~now
        tailer._try_self_heal_repoint()

        assert tailer.transcript_path == fresh
        assert tailer.stats["self_heal_stale_skips"] == 1

    @pytest.mark.asyncio
    async def test_self_heal_unrestricted_before_first_real_bind(self, tmp_path):
        """#291: the floor must NOT block cold-start (#515) discovery. Before any
        explicit ``set_transcript_path``, ``_path_bound_at`` is the 0.0 sentinel,
        so even a discovery whose mtime predates tailer construction heals — at
        genuine cold start there is no live session to clobber to."""
        cb = _Captor()
        real = tmp_path / "real.jsonl"
        _write_jsonl(real, [_assistant(text="cold-start"), _stop_hook_summary()])
        os.utime(real, (time.time() - 3600, time.time() - 3600))  # old, yet valid here

        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=lambda: real,
        )
        assert tailer._path_bound_at == 0.0
        tailer._try_self_heal_repoint()

        assert tailer.transcript_path == real, "#515 cold-start heal must still fire"
        assert tailer.stats["self_heal_repoints"] == 1
        assert tailer.stats["self_heal_stale_skips"] == 0

    @pytest.mark.asyncio
    async def test_self_heal_failsafe_on_unstattable_discovery(self, tmp_path):
        """#291: if the discovered candidate can't be stat()'d (vanished between
        glob and stat), fail SAFE — skip the repoint, never risk the clobber,
        never raise."""
        cb = _Captor()
        ghost = tmp_path / "ghost.jsonl"  # never created
        fresh = tmp_path / "fresh.jsonl"

        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=lambda: ghost,
        )
        tailer.set_transcript_path(fresh)  # arms the floor
        tailer._try_self_heal_repoint()  # ghost.stat() → OSError → skip, no crash

        assert tailer.transcript_path == fresh
        assert tailer.stats["self_heal_repoints"] == 0

    @pytest.mark.asyncio
    async def test_self_heal_forward_heal_with_carried_over_floor(self, tmp_path):
        """#291: the tailer instance is retained across ``force_restart``, so a
        respawn carries the PRIOR session's bind time in ``_path_bound_at``. A
        genuinely NEW session transcript (created after the respawn, so newer
        than the carried floor) must still heal FORWARD — the carried-over floor
        only blocks true previous-session files, never the live one."""
        cb = _Captor()
        new = tmp_path / "new-session.jsonl"
        tailer = TmuxTranscriptTailer(
            tmp_path / "placeholder.jsonl", cb,
            path_discovery=lambda: new,
        )
        # Prior session: a real bind stamps _path_bound_at (the value that
        # survives a force_restart respawn of the retained instance).
        prior = tmp_path / "prior-session.jsonl"
        prior.write_text("{}\n")
        tailer.set_transcript_path(prior)
        carried_floor = tailer._path_bound_at
        assert carried_floor > 0.0

        # Respawn: prior file is gone, the NEW session's JSONL appears, newer
        # than the carried floor.
        prior.unlink()
        _write_jsonl(new, [_assistant(text="new session"), _stop_hook_summary()])
        os.utime(new, (carried_floor + 10, carried_floor + 10))

        # _path (prior) is now missing → self-heal fires → discovery returns the
        # NEW (newer) file → floor ALLOWS the forward heal.
        tailer._try_self_heal_repoint()

        assert tailer.transcript_path == new, "must heal forward to the live file"
        assert tailer.stats["self_heal_repoints"] == 1
        assert tailer.stats["self_heal_stale_skips"] == 0


# ──────────────────────────────────────────────────────────────────────────
# Mid-turn usage callback (real-time context gauge)
# ──────────────────────────────────────────────────────────────────────────


class TestMidTurnUsageCallback:
    """``on_usage`` fires per assistant entry that carries a fresh usage
    block — the hook that keeps context% live during long tool-loop
    turns instead of freezing at the previous turn's value."""

    @pytest.mark.asyncio
    async def test_fires_per_assistant_entry_mid_turn(self, transcript):
        cb = _Captor()
        seen: list[dict] = []
        tailer = TmuxTranscriptTailer(transcript, cb, on_usage=seen.append)
        # A turn IN FLIGHT: three API calls, no stop_hook_summary yet.
        _write_jsonl(transcript, [
            _user(text="do a big thing"),
            _assistant(text="", tool_use={"name": "Bash"},
                       usage={"input_tokens": 1000, "output_tokens": 50}),
            _assistant(text="", tool_use={"name": "Read"},
                       usage={"input_tokens": 2000, "output_tokens": 60}),
            _assistant(text="done",
                       usage={"input_tokens": 3000, "output_tokens": 70}),
        ])
        await tailer.read_once()
        # Turn hasn't closed — no turn callback yet — but usage surfaced
        # three times, tracking the live window per API call.
        assert cb.responses == []
        assert [u["input_tokens"] for u in seen] == [1000, 2000, 3000]
        assert tailer.stats["usage_events"] == 3

    @pytest.mark.asyncio
    async def test_not_fired_for_entries_without_usage(self, transcript):
        cb = _Captor()
        seen: list[dict] = []
        tailer = TmuxTranscriptTailer(transcript, cb, on_usage=seen.append)
        no_usage = _assistant(text="synthetic")
        no_usage["message"]["usage"] = {}
        del_usage = _assistant(text="missing")
        del del_usage["message"]["usage"]
        _write_jsonl(transcript, [_user(), no_usage, del_usage])
        await tailer.read_once()
        assert seen == []
        assert tailer.stats["usage_events"] == 0

    @pytest.mark.asyncio
    async def test_empty_usage_does_not_clobber_last_real_snapshot(self, transcript):
        """A trailing ``"usage": {}`` row must not erase the last real
        usage block from the TurnResponse."""
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        empty = _assistant(text="")
        empty["message"]["usage"] = {}
        _write_jsonl(transcript, [
            _user(),
            _assistant(text="real", usage={"input_tokens": 500, "output_tokens": 5}),
            empty,
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert len(cb.responses) == 1
        assert cb.responses[0].usage == {"input_tokens": 500, "output_tokens": 5}

    @pytest.mark.asyncio
    async def test_on_usage_exception_swallowed_and_turn_still_fires(self, transcript):
        cb = _Captor()

        def boom(usage: dict) -> None:
            raise RuntimeError("gauge exploded")

        tailer = TmuxTranscriptTailer(transcript, cb, on_usage=boom)
        _write_jsonl(transcript, [
            _user(),
            _assistant(text="reply", usage={"input_tokens": 100, "output_tokens": 10}),
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        # Callback error is counted, tailing continues, turn completes.
        assert len(cb.responses) == 1
        assert cb.responses[0].text == "reply"
        assert tailer.stats["callback_errors"] == 1

    @pytest.mark.asyncio
    async def test_callback_receives_copy_not_buffer_state(self, transcript):
        cb = _Captor()
        seen: list[dict] = []

        def mutate(usage: dict) -> None:
            seen.append(dict(usage))
            usage["input_tokens"] = -999  # must not corrupt the buffer

        tailer = TmuxTranscriptTailer(transcript, cb, on_usage=mutate)
        _write_jsonl(transcript, [
            _user(),
            _assistant(text="reply", usage={"input_tokens": 100, "output_tokens": 10}),
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert seen == [{"input_tokens": 100, "output_tokens": 10}]
        assert cb.responses[0].usage["input_tokens"] == 100

    @pytest.mark.asyncio
    async def test_no_callback_configured_is_no_op(self, transcript):
        cb = _Captor()
        tailer = TmuxTranscriptTailer(transcript, cb)
        _write_jsonl(transcript, [
            _user(),
            _assistant(text="reply", usage={"input_tokens": 100, "output_tokens": 10}),
            _stop_hook_summary(),
        ])
        await tailer.read_once()
        assert len(cb.responses) == 1
        # Stat counts CALLBACKS fired, not usage sightings — stays 0.
        assert tailer.stats["usage_events"] == 0
