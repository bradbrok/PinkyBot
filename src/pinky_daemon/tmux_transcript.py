"""Tmux transcript tailer — response capture pipeline for ``TmuxSession``.

PR8b of the #486 sequence. Closes the response-capture gap left open by
PR8a (#495). Design proposal: ``docs/design/tmux-response-pipeline.md``.

## What this module does

Claude Code's interactive REPL appends one JSONL entry per event to
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``. Inside that
stream are:

- ``{"type": "user", ...}`` — inbound prompt or tool result
- ``{"type": "assistant", "message": {"content": [...]}, ...}`` — one
  model API call's output. Multiple of these can appear inside a single
  conversational turn (tool-use loops).
- ``{"type": "system", "subtype": "stop_hook_summary", ...}`` — written
  by Claude Code itself after the configured ``Stop`` hooks complete.
  This is the authoritative turn-end marker.

``TmuxTranscriptTailer`` watches the file from a byte offset, accumulates
each assistant entry's text / thinking / tool_use blocks into a
``_TurnBuffer``, and fires ``on_turn_complete(TurnResponse)`` when it
sees a ``stop_hook_summary`` entry.

## Hybrid wake model

The tailer normally polls at ``_FALLBACK_POLL_SEC`` so it makes progress
even without external signals. ``wake()`` short-circuits the poll —
callers wire that to a ``Stop`` hook that POSTs to the daemon, giving
near-zero latency on turn-end while keeping the file as single source of
truth. See module docstring in ``tmux_session.py`` and the design doc
for the full rationale.

## Why this is its own module (not folded into TmuxSession)

Two reasons:

1. **Reuse.** Context-budget watchdog and conversation-store backfill
   both need to read the same transcripts. A standalone tailer is the
   shared primitive; ``TmuxSession`` is one consumer.
2. **Testability.** The tailer is a pure function of (file content, wake
   events) → callback invocations. Unit-testable without any tmux /
   claude / asyncio session setup.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

from pinky_daemon.turn_response import TurnResponse


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# Fallback poll cadence when no external ``wake()`` arrives. Cheap — stat'ing
# a file is microseconds. Bounded so a missed Stop hook doesn't strand a
# response forever (worst case the user perceives this much latency).
_FALLBACK_POLL_SEC = 2.0

# Tight poll cadence used during an in-flight turn (between send_keys and the
# next stop_hook_summary). We want low latency here; the Stop-hook wake
# normally short-circuits this entirely, but if the hook is misconfigured we
# still want sub-second response. Bounded below by what stat()/read are happy
# to do on a hot file.
_ACTIVE_POLL_SEC = 0.2

# Max bytes read per ``_read_and_dispatch`` invocation. Bounds memory if the
# transcript path ever points at something pathologically large (per
# Pushok's PR #496 round-1 Case 4a: the daemon endpoint's docstring claims
# path validation that the code doesn't actually enforce, so an attacker
# with a valid HMAC could point the tailer at /var/log/system.log and OOM
# the daemon via a single uncapped fh.read()). 10 MiB is generous for one
# turn's worth of transcript (typical turn = a few KB to a few hundred KB)
# while keeping single-read memory bounded. Excess data stays on disk and
# is picked up by the next loop iteration.
_MAX_READ_CHUNK_BYTES = 10 * 1024 * 1024


# ──────────────────────────────────────────────────────────────────────────
# _TurnBuffer — accumulates assistant content between stop_hook_summary
# boundaries
# ──────────────────────────────────────────────────────────────────────────


class _TurnBuffer:
    """In-progress accumulator for one conversational turn.

    Fed one transcript entry at a time. ``feed`` returns True iff the
    entry was a ``stop_hook_summary`` (i.e. the turn is now complete and
    the caller should ``drain()``). Otherwise the entry is either
    accumulated (assistant) or ignored (everything else).

    Drain resets the buffer for the next turn.
    """

    def __init__(self) -> None:
        self._text_blocks: list[str] = []
        self._thinking_blocks: list[str] = []
        self._tool_uses: list[dict] = []
        self._last_stop_reason: str = ""
        self._last_usage: dict = {}
        self._last_model: str = ""
        self._assistant_count: int = 0
        self._turn_started_at: float | None = None  # epoch seconds
        self._turn_ended_at: float | None = None

    def feed(self, entry: dict) -> bool:
        """Process one transcript entry. Return True iff this closes a turn."""
        etype = entry.get("type", "")

        # Track wall-clock by harvesting the first usable timestamp.
        ts = _parse_ts(entry.get("timestamp", ""))
        if ts is not None and self._turn_started_at is None:
            self._turn_started_at = ts

        if etype == "assistant":
            self._consume_assistant(entry)
            return False
        if etype == "system" and entry.get("subtype") == "stop_hook_summary":
            self._turn_ended_at = ts
            return True
        # user / attachment / queue-operation / ai-title / last-prompt — ignore.
        # These either don't carry response data (queue-op, ai-title) or are
        # inputs we don't echo back through the response callback.
        return False

    def _consume_assistant(self, entry: dict) -> None:
        msg = entry.get("message") or {}
        if not isinstance(msg, dict):
            return
        self._assistant_count += 1
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text") or ""
                if txt:
                    self._text_blocks.append(txt)
            elif btype == "thinking":
                txt = block.get("thinking") or ""
                if txt:
                    self._thinking_blocks.append(txt)
            elif btype == "tool_use":
                self._tool_uses.append({
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                    "id": block.get("id", ""),
                })
            # tool_result blocks appear on user-role entries; we don't see
            # them here. Unknown block types are silently skipped — schema
            # drift defense per design doc Risk #1.
        # Last assistant entry wins for stop_reason / usage. Per turn,
        # the *final* assistant entry carries the user-visible stop_reason
        # (the in-loop ones tend to be tool_use); usage is cumulative
        # so the last one is the right snapshot.
        sr = msg.get("stop_reason")
        if sr:
            self._last_stop_reason = sr
        usage = msg.get("usage")
        if isinstance(usage, dict):
            self._last_usage = usage
        # Capture the model that produced this turn. The configured model
        # (``config.model``) can be empty when the agent relies on Claude
        # Code's default, so the transcript's own ``model`` field is the
        # authoritative per-turn source for cost pricing (#648). Synthetic
        # placeholder rows ("<synthetic>") carry no real usage, so prefer
        # the last entry that actually reported usage.
        model = msg.get("model")
        if model and model != "<synthetic>":
            self._last_model = model

    def drain(self, prevented_continuation: bool = False) -> TurnResponse:
        """Snapshot + reset for the next turn."""
        duration_ms = 0
        if self._turn_started_at is not None and self._turn_ended_at is not None:
            duration_ms = max(0, int((self._turn_ended_at - self._turn_started_at) * 1000))

        resp = TurnResponse(
            text="\n".join(self._text_blocks),
            thinking="\n".join(self._thinking_blocks),
            tool_uses=list(self._tool_uses),
            stop_reason=self._last_stop_reason,
            usage=dict(self._last_usage),
            model=self._last_model,
            prevented_continuation=prevented_continuation,
            duration_ms=duration_ms,
            assistant_entry_count=self._assistant_count,
        )

        self._text_blocks.clear()
        self._thinking_blocks.clear()
        self._tool_uses.clear()
        self._last_stop_reason = ""
        self._last_usage = {}
        self._last_model = ""
        self._assistant_count = 0
        self._turn_started_at = None
        self._turn_ended_at = None

        return resp

    @property
    def is_empty(self) -> bool:
        """True iff nothing has been fed since the last drain.

        Useful for the tailer's catch-up path: if a stop_hook_summary
        appears and the buffer is empty (e.g. cold-start replay of an
        already-fired turn), the tailer can skip firing the callback to
        avoid emitting empty TurnResponses to the broker.
        """
        return self._assistant_count == 0 and not self._text_blocks


def _parse_ts(raw: str) -> float | None:
    """Parse an ISO8601 timestamp into epoch seconds. ``None`` on failure."""
    if not raw:
        return None
    # Claude Code uses Z-suffixed UTC ISO8601 (e.g. "2026-05-14T05:03:16.161Z").
    # Strip the trailing Z; the rest is fromisoformat-compatible on 3.11+.
    s = raw[:-1] if raw.endswith("Z") else raw
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────────────────────────────────────
# TmuxTranscriptTailer — file watcher + parser + dispatcher
# ──────────────────────────────────────────────────────────────────────────


# Callback type. Async so consumers (e.g. TmuxSession._handle_turn_complete)
# can do async work like awaiting the response_callback / cost_callback /
# conversation_store writes without forcing a sync bridge.
TurnCallback = Callable[[TurnResponse], Awaitable[None]]


class TmuxTranscriptTailer:
    """Per-session tailer for a Claude Code JSONL transcript.

    Lifecycle:
    1. Constructor: configured with path + callback. No I/O yet.
    2. ``start()`` opens the file at the configured offset and spawns
       the background tail task. Idempotent.
    3. ``wake()`` is called from external Stop-hook handlers (or tests)
       to signal "new data available, read now."
    4. Background task loops: wait for wake_event OR poll timeout,
       read until EOF, feed entries to the buffer, fire callback on
       turn-complete.
    5. ``stop()`` cancels the task and closes the file handle.

    Offset can be persisted across daemon restarts to replay missed
    turns (or skipped, if the consumer prefers to re-resume from EOF).
    ``set_offset(0)`` re-reads the whole file from scratch — useful for
    backfill.
    """

    def __init__(
        self,
        transcript_path: Path,
        on_turn_complete: TurnCallback,
        *,
        agent_name: str = "",
        fallback_poll_sec: float = _FALLBACK_POLL_SEC,
        active_poll_sec: float = _ACTIVE_POLL_SEC,
        path_discovery: Callable[[], Path | None] | None = None,
    ) -> None:
        self._path = Path(transcript_path)
        # #291: wall-clock when ``_path`` was last bound via an explicit
        # ``set_transcript_path`` call. Starts at 0.0 — the "no real bind yet"
        # sentinel — so the self-heal mtime-floor (``_try_self_heal_repoint``)
        # is UNRESTRICTED at cold start: the placeholder→fresh discovery is
        # exactly what #515 is for, and at genuine cold start there is no live
        # session yet to clobber to (any pre-existing JSONL is bound directly by
        # ``_start_tailer`` and so already exists → self-heal never fires). The
        # floor engages the moment a real path is bound (below + the hook path):
        # from then on the self-heal refuses to repoint to any transcript OLDER
        # than the current bind — a stale previous-session file surfacing
        # because the freshly-bound JSONL hasn't hit disk yet. Re-stamped on
        # every real path change in ``set_transcript_path``.
        self._path_bound_at = 0.0
        self._on_turn_complete = on_turn_complete
        self._agent_name = agent_name or self._path.stem[:12]
        self._fallback_poll_sec = fallback_poll_sec
        self._active_poll_sec = active_poll_sec
        # #515 self-heal: when ``_path`` doesn't exist on a poll (e.g.
        # tailer still pinned to the placeholder because SessionStart
        # hook never fired), call ``path_discovery()``. If it returns a
        # Path, rebind via ``set_transcript_path(path, seek_to_start=True)``.
        # This makes the tailer correct without dependence on the
        # SessionStart hook firing at all. See ``TmuxSession._discover_transcript_path``.
        self._path_discovery = path_discovery

        self._offset: int = 0
        # Bumped by every path-changing ``set_transcript_path``. Lets
        # ``_read_and_dispatch`` detect a concurrent swap that landed
        # while it was parked in an awaited turn callback, so it can
        # discard the rest of the old file's chunk instead of feeding it
        # into the freshly-drained buffer and adding the old chunk's
        # byte length to the NEW file's offset.
        self._swap_generation: int = 0
        self._buffer = _TurnBuffer()
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._stats = {
            "turns_fired": 0,
            "lines_read": 0,
            "parse_errors": 0,
            "callback_errors": 0,
            "rotations": 0,
            # #515 self-heal: count successful discoveries so we can
            # surface "the tailer fell back to mtime-scan N times" in
            # diagnostics.
            "self_heal_repoints": 0,
            # #291: count self-heal discoveries we REFUSED because the
            # candidate predated the current bind (the stale-clobber guard).
            "self_heal_stale_skips": 0,
        }
        # ``_active`` flips True after we see a user entry but before we see
        # the closing stop_hook_summary. Drives the tighter poll cadence so
        # we minimise the perceived round-trip latency.
        self._active: bool = False

    # ── External API ────────────────────────────────────────────────────

    @property
    def offset(self) -> int:
        """Current byte offset. Persist this across restarts to resume."""
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
        """Override the offset (e.g. for backfill or post-restart resume).

        Safe to call before ``start()``. After start, the next read uses
        the new offset.
        """
        self._offset = max(0, offset)

    def set_transcript_path(
        self, path: Path, *, seek_to_start: bool = False,
    ) -> None:
        """Swap the watched file. Used by the SessionStart hook to
        repoint the tailer at the canonical transcript Claude Code is
        writing to, and by the #515 self-heal mtime-scan when the
        SessionStart hook never fires.

        Seeks to end-of-file by default — Pushok's PR #496 round-1
        Case 3 fix. The previous design unconditionally reset offset to
        0, which was safe ONLY under the contract that SessionStart
        fires before any turns. If that contract is ever violated
        (compact-resume swap, daemon-restart mid-session re-fire,
        misconfigured test fixture, late-arriving hook on a session
        that already produced turns), reading from 0 would re-fire
        every historical turn's ``response_callback`` — reply-spam to
        every chat that ever talked to this agent.

        Seek-to-EOF gives the same offset==0 for a fresh file
        (size == 0) and bounds the replay risk for non-fresh.

        ``seek_to_start=True`` overrides the default and seeks to byte
        0. The self-heal discovery path uses this when transitioning
        from a placeholder (non-existent file) to a freshly-discovered
        transcript: in that case the daemon has never read any bytes
        for this session, the JSONL was created seconds ago specifically
        for this cold-start, and we want every entry from byte 0
        forward. The reply-spam risk applies to long-lived transcripts
        with prior turns, not to fresh-discovery files.

        Callers that want backfill semantics (re-read the whole file)
        can call ``set_offset(0)`` after this method — that's what the
        backfill code path is for and it's an explicit choice rather
        than an accidental side effect. ``seek_to_start`` is the
        in-method shorthand for the discovery case.

        Pushok's PR #496 round-2 Case 2': also drain the in-memory turn
        buffer. The buffer accumulates assistant entries between
        ``stop_hook_summary`` markers, so a session that was killed
        mid-turn (e.g. force_restart) can leave partial text in the
        buffer. If we swap to a new session's transcript without
        draining, the next ``stop_hook_summary`` we read would surface
        ``old_session_text + new_session_text`` as a single response,
        leaking dead-session content into the new session's first reply.
        Silent drain — no callback (we're not at a turn boundary, just
        discarding partial state, symmetric with the truncation/rotation
        path in ``_read_and_dispatch``).
        """
        if Path(path) != self._path:
            self._path = Path(path)
            # #291: re-stamp the bind clock on every real path change so the
            # self-heal floor measures staleness against THIS bind, not a
            # stale construction-time value — the tailer instance is retained
            # across ``force_restart`` respawns, so a fresh launch rebinds
            # through here and must reset the floor.
            self._path_bound_at = time.time()
            self._swap_generation += 1
            if seek_to_start:
                self._offset = 0
            else:
                try:
                    self._offset = self._path.stat().st_size if self._path.exists() else 0
                except OSError:
                    self._offset = 0
            self._buffer.drain()
            self._stats["rotations"] += 1
            self._wake_event.set()

    def wake(self) -> None:
        """Signal the tail loop that new data is available now.

        Idempotent — multiple wakes between reads coalesce into one
        read. Safe to call before ``start()`` (the next read will pick
        up the latched event).
        """
        self._wake_event.set()

    def drain_buffer(self) -> None:
        """Discard any in-progress turn state.

        Public counterpart of ``self._buffer.drain()`` for lifecycle
        callers (notably ``TmuxSession._stop_tailer``). Murzik's PR #496
        round-3 finding (Case 2''): the round-2 drain inside
        ``set_transcript_path`` only fires when the path actually
        changes. ``claude --continue`` after ``force_restart`` resumes
        the same JSONL path, so the path-equality guard skips the drain
        and partial assistant text from the killed session survives
        across the lifecycle restart.

        ``_stop_tailer`` is the single semantic "session ended"
        boundary that handles both the new-path and same-path cases.
        Silent drain — no callback (we're not at a turn boundary,
        just discarding stale state).
        """
        self._buffer.drain()

    async def start(self) -> None:
        """Begin tailing in the background. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(
            self._tail_loop(), name=f"tmux_tailer:{self._agent_name}"
        )

    async def stop(self) -> None:
        """Cancel the background task. Idempotent."""
        self._stopped = True
        self._wake_event.set()  # wake the loop so it can see _stopped
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                # Cancellation is expected and any other failure during
                # shutdown is logged-and-swallowed — we're tearing down,
                # not a programmable failure mode for callers to handle.
                pass
            self._task = None

    async def read_once(self) -> int:
        """Read up to EOF, feed entries, fire callbacks. Returns number
        of new bytes consumed.

        Exposed for tests and for callers that want synchronous control
        (e.g. force a flush before recording state).
        """
        return await self._read_and_dispatch()

    def mark_active(self) -> None:
        """Hint to the tailer that a turn is in flight. Switches to the
        tighter poll cadence until the next stop_hook_summary fires.

        TmuxSession calls this from ``_deliver_turn`` after ``send-keys``
        returns.

        Note (Pushok's PR #496 round-1 Case 4b): there's a benign
        ordering race between ``mark_active()`` (sets True, called by
        the worker) and the False-flip inside ``_read_and_dispatch``
        (called by the tail loop on stop_hook_summary). If the worker
        dispatches turn N+1 before the tailer has flipped False for
        turn N, the True-set may race a False-flip and end up at the
        fallback cadence during an in-flight turn. Latency-only, not
        correctness — CPython bool stores are atomic so no torn state.
        Stop-hook wake() short-circuits the slower cadence anyway.
        Not worth a lock.
        """
        self._active = True
        self._wake_event.set()

    # ── Internals ───────────────────────────────────────────────────────

    async def _tail_loop(self) -> None:
        """Main loop: wait for wake OR poll timeout, then read+dispatch."""
        while not self._stopped:
            cadence = self._active_poll_sec if self._active else self._fallback_poll_sec
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=cadence)
            except asyncio.TimeoutError:
                # Timeout is the normal "no wake fired — proceed to poll"
                # path; not an error. Used as control flow.
                pass
            self._wake_event.clear()
            if self._stopped:
                return
            # #515 self-heal: if the path doesn't exist and a discovery
            # callback is configured, try to find the real transcript
            # via mtime-scan and rebind. Removes the SessionStart-hook
            # dependency from the correctness path — even if the hook
            # never fires (e.g. PINKY_SESSION_SECRET stripped by tmux,
            # hook script removed, claude-code internals changed), the
            # tailer reaches the real transcript on its own.
            self._try_self_heal_repoint()
            try:
                await self._read_and_dispatch()
            except Exception as e:  # defensive — never crash the loop
                self._stats["parse_errors"] += 1
                _log(
                    f"tmux_tailer[{self._agent_name}]: read loop error "
                    f"({type(e).__name__}: {e}); continuing"
                )

    def _try_self_heal_repoint(self) -> None:
        """If the watched path doesn't exist and we have a discovery
        callback, scan for the real transcript and rebind.

        Called from ``_tail_loop`` once per poll tick. Cheap when there
        is no discovery callback (early return) and when the path
        exists (single stat). Discovery itself (an ``os.scandir`` over
        the project dir) is the dominant cost, only paid when the path
        is missing.

        Errors are swallowed and logged — a transient filesystem hiccup
        during discovery must never crash the tail loop. The next poll
        tick retries.
        """
        if self._path_discovery is None:
            return
        if self._path.exists():
            return
        try:
            discovered = self._path_discovery()
        except Exception as e:
            _log(
                f"tmux_tailer[{self._agent_name}]: path_discovery raised "
                f"({type(e).__name__}: {e}); will retry next tick"
            )
            return
        if discovered is None:
            return
        if Path(discovered) == self._path:
            return
        # #291: never repoint to a transcript whose mtime PREDATES the current
        # bind. On a fresh launch (context_restart / new session) the
        # SessionStart hook binds the new JSONL the instant Claude Code
        # announces the session — before CC has flushed the file to disk. The
        # next poll sees ``_path`` missing and lands here; ``path_discovery``
        # (newest-existing-by-mtime) then returns the PREVIOUS session's file,
        # and the pre-#291 code clobbered the correct bind with it. That wedges
        # the tailer on a frozen file forever: the stale path EXISTS so this
        # self-heal never re-fires, and the first-bind flag was already consumed
        # so #565 won't either — leaving the watchdog blind (frozen
        # ``transcript_mtime`` reads "quiet" + undetected stop hooks pile the
        # inflight deque) until it force_restarts a perfectly healthy agent.
        # The freshly-bound session's own JSONL, once it materialises, has
        # mtime >= the bind; a strictly-older file is always a previous session
        # and never a valid heal target. Strict ``<`` (no slack): the stale
        # candidate is often the *immediately* preceding session, whose last
        # write can be only seconds before the new bind — any positive slack
        # would re-admit it. Fail-safe: if the candidate's mtime can't be read,
        # skip rather than risk the clobber.
        try:
            discovered_mtime = Path(discovered).stat().st_mtime
        except OSError:
            return
        if discovered_mtime < self._path_bound_at:
            _log(
                f"tmux_tailer[{self._agent_name}]: self-heal SKIP stale "
                f"discovery {discovered} (mtime {discovered_mtime:.0f} predates "
                f"bind {self._path_bound_at:.0f}) — awaiting bound path to "
                f"materialise (#291)"
            )
            self._stats["self_heal_stale_skips"] += 1
            return
        _log(
            f"tmux_tailer[{self._agent_name}]: self-heal repointing "
            f"{self._path} → {discovered} (placeholder/missing path "
            f"resolved via mtime-scan)"
        )
        # ``seek_to_start=True``: this is the placeholder→real
        # transition. The discovered file was just created for this
        # cold-start session; we want every entry from byte 0. The
        # reply-spam risk that motivated default-seek-to-EOF applies
        # to long-lived transcripts with prior turns, not to fresh
        # discovery on cold-start where no bytes have been read yet.
        self.set_transcript_path(Path(discovered), seek_to_start=True)
        self._stats["self_heal_repoints"] += 1

    async def _read_and_dispatch(self) -> int:
        """Read from ``_offset`` to EOF, feed each JSONL entry, fire
        ``on_turn_complete`` per stop_hook_summary.

        Returns number of bytes consumed. Zero if file doesn't exist yet
        (cold start before claude has written anything).
        """
        if not self._path.exists():
            return 0

        # Snapshot for the mid-chunk swap check below. The only awaits in
        # this method are the turn callbacks; everything else is sync, so
        # a concurrent ``set_transcript_path`` can only land while a
        # callback is in flight.
        generation = self._swap_generation

        size = self._path.stat().st_size
        if size < self._offset:
            # File truncated or rotated underneath us. Reset to 0 and
            # replay; downstream consumers should be idempotent against
            # repeat turns, but in practice the buffer is empty on
            # rotation so this just rebuilds state from scratch.
            _log(
                f"tmux_tailer[{self._agent_name}]: file shrank "
                f"({size} < {self._offset}); resetting to 0"
            )
            self._offset = 0
            self._buffer.drain()  # discard partial state
            self._stats["rotations"] += 1

        if size == self._offset:
            return 0

        bytes_read = 0
        # Read in text mode — Claude Code transcripts are UTF-8. Buffer
        # may contain a partial trailing line if Claude Code is mid-write;
        # we only advance offset by complete lines.
        #
        # ``_MAX_READ_CHUNK_BYTES`` caps single-read memory so a pathological
        # transcript path (or attacker-pointed file via the path-update
        # endpoint) can't OOM the daemon. If more data remains after the
        # cap, the wake_event is re-armed below so the next loop iteration
        # picks it up — slower but bounded.
        with self._path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self._offset)
            chunk = fh.read(_MAX_READ_CHUNK_BYTES)
            more_pending = (size - self._offset) > _MAX_READ_CHUNK_BYTES
            # Split into lines manually so we can detect a partial trailing
            # line (no trailing newline → don't consume).
            if not chunk:
                return 0
            if chunk.endswith("\n"):
                complete = chunk
                partial = ""
            else:
                last_nl = chunk.rfind("\n")
                if last_nl == -1:
                    # No complete line yet. Don't advance offset.
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
                        f"tmux_tailer[{self._agent_name}]: skipping malformed "
                        f"JSON at offset {self._offset}+{bytes_read}"
                    )
                    bytes_read += len(line) + 1
                    continue

                closes_turn = False
                prevented = False
                try:
                    closes_turn = self._buffer.feed(entry)
                    if closes_turn:
                        prevented = bool(entry.get("preventedContinuation", False))
                except Exception as e:
                    self._stats["parse_errors"] += 1
                    _log(
                        f"tmux_tailer[{self._agent_name}]: feed raised "
                        f"({type(e).__name__}: {e}); skipping entry"
                    )

                bytes_read += len(line.encode("utf-8")) + 1  # +1 for the \n

                if closes_turn and not self._buffer.is_empty:
                    response = self._buffer.drain(prevented_continuation=prevented)
                    self._active = False
                    self._stats["turns_fired"] += 1
                    await self._safe_callback(response)
                    if self._swap_generation != generation:
                        # ``set_transcript_path`` swapped the watched file
                        # while the callback was awaited. The rest of this
                        # chunk belongs to the OLD file and ``_offset`` now
                        # refers to the NEW one: feeding more lines would
                        # repollute the drained buffer (#496 Case 2 leak)
                        # and the offset advance below would corrupt the
                        # new file's position. Discard and return; the
                        # swap already armed the wake event.
                        return bytes_read
                elif closes_turn:
                    # Cold-start replay: stop_hook_summary appeared but the
                    # buffer is empty (we entered mid-transcript). Drain
                    # silently to clear ts state; don't fire.
                    self._buffer.drain(prevented_continuation=prevented)
                    self._active = False

            # Advance past complete lines only. Partial line stays in the
            # file and we'll re-read it next loop.
            self._offset += len(complete.encode("utf-8"))
            # ``partial`` is dropped intentionally — next read picks it up.
            _ = partial

        # If the size cap forced us to stop short of EOF, re-arm the wake
        # event so the next loop iteration picks up the remainder
        # immediately rather than waiting for the poll cadence.
        if more_pending:
            self._wake_event.set()

        return bytes_read

    async def _safe_callback(self, response: TurnResponse) -> None:
        """Invoke ``on_turn_complete`` swallowing any callback exception.

        A misbehaving callback should not strand the tailer. We log and
        increment a stat so behavior is visible.
        """
        try:
            result = self._on_turn_complete(response)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self._stats["callback_errors"] += 1
            _log(
                f"tmux_tailer[{self._agent_name}]: on_turn_complete raised "
                f"({type(e).__name__}: {e}); continuing"
            )


__all__ = [
    "TmuxTranscriptTailer",
    "TurnResponse",
]
