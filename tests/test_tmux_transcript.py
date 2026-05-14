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
