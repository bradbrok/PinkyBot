"""Codex transcript tailer — response capture pipeline for ``CodexTmuxSession``.

PR1 of the codex-tmux transport (task #215). Mirrors ``tmux_transcript.py``
(the claude ``TmuxTranscriptTailer``) in structure, public API, and
wake/poll hybrid model. Only the per-entry parsing and turn-boundary
detection differ: codex emits ``event_msg``-wrapped payloads with
explicit ``task_complete`` / ``turn_aborted`` markers and front-loads
token usage in a separate ``token_count`` event.

## Record shapes (codex-cli 0.125.0, verified from real rollout)

Line 1 (always): ``{"type":"session_meta","payload":{"id":"<uuid>","cwd":"...","cli_version":...}}``
Turn events: ``{"type":"event_msg","payload":{"type":"<subtype>",...}}``

Subtype routing:
  - ``task_started``   → mark turn active; capture turn_id, started_at.
  - ``agent_message``  → accumulate payload.message chunks.
  - ``token_count``    → if payload.info is non-null, snapshot last_token_usage.
  - ``task_complete``  → TURN END. text = payload.last_agent_message (fallback to
                         accumulated chunks); usage from last non-null token_count.
  - ``turn_aborted``   → also closes the turn (empty/partial + aborted flag),
                         so inflight heads resolve rather than stranding.
  - Everything else    → ignored for response purposes.

## Session file discovery

Codex does NOT slug the cwd into the path (unlike claude). Discovery:
glob ``~/.codex/sessions/**/rollout-*.jsonl``, sort by mtime desc, read
line 1 of each candidate (bounded to newest K files), pick the newest
whose ``session_meta.payload.cwd == os.path.realpath(working_dir)``.

The primary path is via explicit ``set_transcript_path()`` — same as
claude. The mtime+cwd glob is the self-heal fallback (mirrors claude's
``path_discovery`` / ``_discover_transcript_path`` pattern from #515).

## Relationship to tmux_transcript.py

Public API is intentionally identical so ``CodexTmuxSession`` (PR2) can
hold a ``CodexTmuxTranscriptTailer`` in the same slot ``TmuxSession``
uses for ``TmuxTranscriptTailer``.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
from pathlib import Path
from typing import Awaitable, Callable

from pinky_daemon.turn_response import TurnResponse


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# Fallback poll cadence when no external notify-hook ``wake()`` arrives.
# Bounded so a missed notify event doesn't strand a response forever.
_FALLBACK_POLL_SEC = 2.0

# Tight cadence during an in-flight turn. The notify hook short-circuits
# this entirely under normal operation; fallback if the hook is missing.
_ACTIVE_POLL_SEC = 0.2

# Max bytes per ``_read_and_dispatch`` call. Mirrors tmux_transcript.py's
# cap — bounds single-read memory if the transcript path ever points at
# something pathologically large. Remaining data is picked up on the
# next iteration.
_MAX_READ_CHUNK_BYTES = 10 * 1024 * 1024

# How many rollout files to scan during cwd-based discovery (mtime desc).
# Prevents unbounded glob cost on machines with thousands of sessions.
_DISCOVERY_SCAN_LIMIT = 50


# ──────────────────────────────────────────────────────────────────────────
# _CodexTurnBuffer — accumulates codex event_msg payloads between
# task_complete / turn_aborted boundaries.
# ──────────────────────────────────────────────────────────────────────────


class _CodexTurnBuffer:
    """In-progress accumulator for one codex conversational turn.

    Fed one parsed ``entry`` dict at a time via ``feed()``. Returns a
    ``(_closes_turn, _aborted)`` pair: ``_closes_turn=True`` means the
    caller should call ``drain()``. Codex streams text via ``agent_message``
    chunks and delivers the authoritative final text in ``task_complete``'s
    ``last_agent_message`` field; accumulated chunks serve as a fallback.

    Token usage arrives in a separate ``token_count`` event just before
    ``task_complete``. We snapshot the last non-null ``info`` so usage
    is available at drain time.
    """

    def __init__(self) -> None:
        self._message_chunks: list[str] = []
        self._last_usage: dict = {}          # from token_count.info.last_token_usage
        self._turn_id: str = ""
        self._started_at: float | None = None  # epoch seconds from task_started
        self._duration_ms: int = 0            # from task_complete / turn_aborted
        self._agent_message_count: int = 0    # rough analog of assistant_entry_count

    def feed(self, entry: dict) -> tuple[bool, bool]:
        """Process one transcript entry.

        Returns ``(closes_turn, aborted)`` where ``closes_turn=True``
        means the caller should call ``drain()`` now, and ``aborted``
        indicates a ``turn_aborted`` marker (no final text from the model).
        """
        if entry.get("type") != "event_msg":
            # session_meta and any other top-level types — ignored.
            return False, False

        payload = entry.get("payload") or {}
        ptype = payload.get("type", "")

        if ptype == "task_started":
            self._turn_id = payload.get("turn_id", "")
            raw_started = payload.get("started_at")
            # started_at is an integer epoch (seconds) in the real format.
            if isinstance(raw_started, (int, float)):
                self._started_at = float(raw_started)
            return False, False

        if ptype == "agent_message":
            msg = payload.get("message") or ""
            if msg and isinstance(msg, str):
                self._message_chunks.append(msg)
                self._agent_message_count += 1
            return False, False

        if ptype == "token_count":
            info = payload.get("info")
            # info is sometimes null (rate-limit-only token_count events).
            # Null-guard: only snapshot when info is a non-null dict.
            if isinstance(info, dict):
                usage = info.get("last_token_usage")
                if isinstance(usage, dict):
                    self._last_usage = dict(usage)
            return False, False

        if ptype == "task_complete":
            # last_agent_message is the authoritative final text; fall back
            # to accumulated chunks if it's absent or empty.
            lam = payload.get("last_agent_message") or ""
            if not lam and self._message_chunks:
                lam = "".join(self._message_chunks)
            self._message_chunks = [lam] if lam else []
            raw_dur = payload.get("duration_ms")
            if isinstance(raw_dur, (int, float)):
                self._duration_ms = int(raw_dur)
            return True, False

        if ptype == "turn_aborted":
            # Close the turn with whatever partial text is available.
            # This lets inflight heads resolve rather than strand.
            raw_dur = payload.get("duration_ms")
            if isinstance(raw_dur, (int, float)):
                self._duration_ms = int(raw_dur)
            return True, True

        # response_item/*, turn_context, compacted, reasoning,
        # user_message, and anything else — skip.
        return False, False

    def drain(self, *, model: str = "", aborted: bool = False) -> TurnResponse:
        """Snapshot accumulated state into a ``TurnResponse`` and reset."""
        text = "".join(self._message_chunks)

        resp = TurnResponse(
            text=text,
            usage=dict(self._last_usage),
            model=model,
            duration_ms=self._duration_ms,
            assistant_entry_count=self._agent_message_count,
            # Codex does not surface stop_reason / tool_uses / thinking in
            # the rollout format we consume; leave at defaults.
            stop_reason="turn_aborted" if aborted else "task_complete",
        )

        # Reset for the next turn.
        self._message_chunks.clear()
        self._last_usage = {}
        self._turn_id = ""
        self._started_at = None
        self._duration_ms = 0
        self._agent_message_count = 0

        return resp

    @property
    def is_empty(self) -> bool:
        """True iff nothing has been accumulated since the last drain.

        Mirrors ``_TurnBuffer.is_empty`` — used by the tailer's cold-start
        replay path to avoid firing empty callbacks.
        """
        return self._agent_message_count == 0 and not self._message_chunks


# ──────────────────────────────────────────────────────────────────────────
# Discovery helper — cwd-filtered mtime scan over ~/.codex/sessions/
# ──────────────────────────────────────────────────────────────────────────


def _discover_codex_rollout(working_dir: str | Path) -> Path | None:
    """Scan ``~/.codex/sessions/**/rollout-*.jsonl`` and return the
    newest file whose ``session_meta.payload.cwd`` equals
    ``os.path.realpath(working_dir)``.

    Scans at most ``_DISCOVERY_SCAN_LIMIT`` files (sorted by mtime desc)
    to bound cost on machines with thousands of sessions.

    Returns ``None`` when no matching file is found. Errors reading
    individual candidates are swallowed — a single unreadable file
    should not block discovery of others.
    """
    target_cwd = os.path.realpath(str(working_dir))

    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.exists():
        return None

    # glob returns unordered; sort by mtime descending and take the top N.
    candidates = sorted(
        sessions_root.glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )[:_DISCOVERY_SCAN_LIMIT]

    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as fh:
                first_line = fh.readline()
            obj = json.loads(first_line)
            if obj.get("type") == "session_meta":
                cwd = obj.get("payload", {}).get("cwd", "")
                if os.path.realpath(cwd) == target_cwd:
                    return candidate
        except (OSError, json.JSONDecodeError, ValueError):
            continue

    return None


# ──────────────────────────────────────────────────────────────────────────
# CodexTmuxTranscriptTailer — file watcher + parser + dispatcher
# ──────────────────────────────────────────────────────────────────────────


# Async callback type — matches TmuxTranscriptTailer's TurnCallback.
TurnCallback = Callable[[TurnResponse], Awaitable[None]]


class CodexTmuxTranscriptTailer:
    """Per-session tailer for a codex JSONL rollout transcript.

    Public API mirrors ``TmuxTranscriptTailer`` exactly so ``CodexTmuxSession``
    (PR2) can hold either tailer in the same slot without changes to the
    surrounding machinery.

    Lifecycle:
    1. Constructor: configured with path + callback + model. No I/O yet.
    2. ``start()`` spawns the background tail task. Idempotent.
    3. ``wake()`` is called from the codex notify-hook handler (or tests)
       to signal "new data available, read now."
    4. Background task loops: wait for wake OR poll timeout, read until
       EOF, feed entries to the buffer, fire callback on turn-complete.
    5. ``stop()`` cancels the task. Idempotent.

    ``set_transcript_path(path)`` / ``set_offset(n)`` work identically to
    the claude tailer. The notify hook delivers the session UUID which maps
    directly to a rollout filename, allowing exact binding without a glob.
    The cwd-based glob is the self-heal fallback when that notification
    never arrives (mirrors PR #515's ``path_discovery`` pattern).
    """

    def __init__(
        self,
        transcript_path: Path,
        on_turn_complete: TurnCallback,
        *,
        agent_name: str = "",
        model: str = "",
        fallback_poll_sec: float = _FALLBACK_POLL_SEC,
        active_poll_sec: float = _ACTIVE_POLL_SEC,
        path_discovery: Callable[[], Path | None] | None = None,
    ) -> None:
        self._path = Path(transcript_path)
        self._on_turn_complete = on_turn_complete
        self._model = model
        self._agent_name = agent_name or self._path.stem[:12]
        self._fallback_poll_sec = fallback_poll_sec
        self._active_poll_sec = active_poll_sec
        # Self-heal: when the watched path doesn't exist, call this to
        # scan for the real rollout by cwd match (mirrors #515).
        self._path_discovery = path_discovery

        self._offset: int = 0
        # Bumped on every path-changing ``set_transcript_path``. Lets
        # ``_read_and_dispatch`` detect a concurrent swap that landed
        # while it was parked in a turn callback (mirrors #496 gen-check).
        self._swap_generation: int = 0
        self._buffer = _CodexTurnBuffer()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._stats = {
            "turns_fired": 0,
            "lines_read": 0,
            "parse_errors": 0,
            "callback_errors": 0,
            "rotations": 0,
            "self_heal_repoints": 0,
        }
        # Flips True on task_started; back to False on task_complete /
        # turn_aborted. Drives the tighter active poll cadence.
        self._active: bool = False

        # Exposed attributes populated from session_meta (line 1).
        # Set when the first read encounters the session_meta record.
        # ``CodexTmuxSession`` (PR2) reads these to capture the resume UUID.
        self.session_id: str = ""
        self.session_cwd: str = ""

    # ── External API ──────────────────────────────────────────────────────

    @property
    def offset(self) -> int:
        """Current byte offset. Persist across daemon restarts to resume."""
        return self._offset

    @property
    def transcript_path(self) -> Path:
        return self._path

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "offset": self._offset,
            "buffer_empty": self._buffer.is_empty,
            "active": self._active,
            "running": self._task is not None and not self._task.done(),
        }

    def set_offset(self, offset: int) -> None:
        """Override the byte offset (backfill or post-restart resume).

        Safe to call before ``start()``.
        """
        self._offset = max(0, offset)

    def set_transcript_path(
        self, path: Path, *, seek_to_start: bool = False,
    ) -> None:
        """Swap the watched rollout file.

        Default behaviour: seek to EOF on swap (mirrors the claude tailer's
        #496 fix — prevents re-firing historical turns as reply-spam when
        a path binding arrives late or redundantly).

        ``seek_to_start=True``: seek to byte 0. Used by the self-heal
        discovery path when transitioning from a placeholder to a freshly-
        discovered rollout (created seconds ago; daemon has never read it).

        Also drains the in-memory buffer (mirrors #496 Case 2' fix) to
        prevent partial text from a killed session leaking into a fresh one.
        """
        if Path(path) != self._path:
            self._path = Path(path)
            self._swap_generation += 1
            if seek_to_start:
                self._offset = 0
            else:
                try:
                    self._offset = self._path.stat().st_size if self._path.exists() else 0
                except OSError:
                    self._offset = 0
            self._buffer.drain()    # silent drain; we're not at a boundary
            self._stats["rotations"] += 1
            self._wake_event.set()

    def wake(self) -> None:
        """Signal the tail loop that new data is available.

        Idempotent — multiple wakes coalesce. Safe to call before
        ``start()`` (latched event fires on first loop tick).
        """
        self._wake_event.set()

    def drain_buffer(self) -> None:
        """Discard any in-progress turn state.

        Lifecycle counterpart of ``_buffer.drain()`` for callers that
        need to reset state at session boundaries without an awaitable
        (e.g. force_restart). Mirrors ``TmuxTranscriptTailer.drain_buffer``.
        """
        self._buffer.drain()

    def mark_active(self) -> None:
        """Hint that a turn is in flight; switch to the tighter poll cadence.

        Called after ``send-keys`` delivers the prompt to codex. The
        notify hook's ``wake()`` normally short-circuits the cadence
        entirely; this is the fallback.
        """
        self._active = True
        self._wake_event.set()

    async def start(self) -> None:
        """Begin tailing in the background. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(
            self._tail_loop(), name=f"codex_tailer:{self._agent_name}"
        )

    async def stop(self) -> None:
        """Cancel the background task. Idempotent."""
        self._stopped = True
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def read_once(self) -> int:
        """Read up to EOF, feed entries, fire callbacks.

        Exposed for tests and for callers that want synchronous control.
        Returns number of bytes consumed.
        """
        return await self._read_and_dispatch()

    # ── Internals ─────────────────────────────────────────────────────────

    async def _tail_loop(self) -> None:
        """Main loop: wait for wake OR poll timeout, then read+dispatch."""
        while not self._stopped:
            cadence = self._active_poll_sec if self._active else self._fallback_poll_sec
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=cadence)
            except asyncio.TimeoutError:
                pass
            self._wake_event.clear()
            if self._stopped:
                return
            self._try_self_heal_repoint()
            try:
                await self._read_and_dispatch()
            except Exception as e:
                self._stats["parse_errors"] += 1
                _log(
                    f"codex_tailer[{self._agent_name}]: read loop error "
                    f"({type(e).__name__}: {e}); continuing"
                )

    def _try_self_heal_repoint(self) -> None:
        """If the watched path is missing and a discovery callback is
        configured, scan for the real rollout and rebind.

        Called once per poll tick. Cheap when the path exists (single
        stat). Discovery is only paid when the path is missing.
        Errors are swallowed; the next tick retries.
        """
        if self._path_discovery is None:
            return
        if self._path.exists():
            return
        try:
            discovered = self._path_discovery()
        except Exception as e:
            _log(
                f"codex_tailer[{self._agent_name}]: path_discovery raised "
                f"({type(e).__name__}: {e}); will retry next tick"
            )
            return
        if discovered is None:
            return
        if Path(discovered) == self._path:
            return
        _log(
            f"codex_tailer[{self._agent_name}]: self-heal repointing "
            f"{self._path} → {discovered}"
        )
        self.set_transcript_path(Path(discovered), seek_to_start=True)
        self._stats["self_heal_repoints"] += 1

    async def _read_and_dispatch(self) -> int:
        """Read from ``_offset`` to EOF, feed JSONL entries, fire callbacks.

        Returns bytes consumed. Zero if the file doesn't exist yet (cold
        start before codex has written anything).
        """
        if not self._path.exists():
            return 0

        generation = self._swap_generation

        size = self._path.stat().st_size
        if size < self._offset:
            # File truncated / rotated. Reset and replay from 0.
            _log(
                f"codex_tailer[{self._agent_name}]: file shrank "
                f"({size} < {self._offset}); resetting to 0"
            )
            self._offset = 0
            self._buffer.drain()
            self._stats["rotations"] += 1

        if size == self._offset:
            return 0

        bytes_read = 0

        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self._offset)
            chunk = fh.read(_MAX_READ_CHUNK_BYTES)
            more_pending = (size - self._offset) > _MAX_READ_CHUNK_BYTES

            if not chunk:
                return 0

            if chunk.endswith("\n"):
                complete = chunk
                partial = ""
            else:
                last_nl = chunk.rfind("\n")
                if last_nl == -1:
                    # No complete line yet — don't advance offset.
                    return 0
                complete = chunk[: last_nl + 1]
                partial = chunk[last_nl + 1:]

            for line in complete.splitlines():
                if not line.strip():
                    continue
                self._stats["lines_read"] += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    self._stats["parse_errors"] += 1
                    _log(
                        f"codex_tailer[{self._agent_name}]: skipping malformed "
                        f"JSON at offset {self._offset}+{bytes_read}"
                    )
                    bytes_read += len(line.encode("utf-8")) + 1
                    continue

                # Capture session_meta fields on line 1.
                if entry.get("type") == "session_meta":
                    payload = entry.get("payload") or {}
                    if not self.session_id:
                        self.session_id = payload.get("id", "")
                    if not self.session_cwd:
                        self.session_cwd = payload.get("cwd", "")

                closes_turn = False
                aborted = False
                try:
                    closes_turn, aborted = self._buffer.feed(entry)
                    if closes_turn:
                        # Sync active flag before the await below so a
                        # concurrent mark_active doesn't race the flip.
                        self._active = False
                except Exception as e:
                    self._stats["parse_errors"] += 1
                    _log(
                        f"codex_tailer[{self._agent_name}]: feed raised "
                        f"({type(e).__name__}: {e}); skipping entry"
                    )

                bytes_read += len(line.encode("utf-8")) + 1  # +1 for \n

                if closes_turn and not self._buffer.is_empty:
                    response = self._buffer.drain(model=self._model, aborted=aborted)
                    self._stats["turns_fired"] += 1
                    await self._safe_callback(response)
                    if self._swap_generation != generation:
                        # Path swapped mid-callback. Discard remainder of
                        # old file's chunk (mirrors #496 gen-check).
                        return bytes_read
                elif closes_turn:
                    # Cold-start replay: turn marker with empty buffer.
                    # Drain silently; don't fire.
                    self._buffer.drain()

            # Advance past complete lines only. Partial line stays on disk.
            self._offset += len(complete.encode("utf-8"))
            _ = partial  # intentionally not consumed

        if more_pending:
            self._wake_event.set()

        return bytes_read

    async def _safe_callback(self, response: TurnResponse) -> None:
        """Invoke ``on_turn_complete`` swallowing callback exceptions.

        A misbehaving consumer should not strand the tailer.
        """
        try:
            result = self._on_turn_complete(response)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self._stats["callback_errors"] += 1
            _log(
                f"codex_tailer[{self._agent_name}]: on_turn_complete raised "
                f"({type(e).__name__}: {e}); continuing"
            )


__all__ = [
    "CodexTmuxTranscriptTailer",
    "_discover_codex_rollout",
    "TurnCallback",
]
