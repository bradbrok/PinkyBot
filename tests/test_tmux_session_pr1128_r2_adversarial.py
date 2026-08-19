"""Independent R2 adversarial probes for PR #1128 at 8d4a245d.

Run from the pinned checkout root:

    PYTHONPATH="$PWD/src" uv run pytest --rootdir="$PWD" \
        -c pyproject.toml -q \
        /Users/oleg/PinkyBot/data/agents/murzik/workspace/pr1128_r2_adversarial_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from pathlib import Path
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

PINNED_HEAD = "8d4a245d73d014c9f84b555a4b63d954b1437ba4"
MAX_EXPECTED_SCAN = 4 * 1024 * 1024


def _ok() -> TmuxCommandResult:
    return TmuxCommandResult(returncode=0, stdout="", stderr="")


def _session(transcript: Path) -> TmuxSession:
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "pinky-pr1128-r2-review"
    control.kill_session = AsyncMock(return_value=_ok())
    config = StreamingSessionConfig(
        agent_name="review",
        working_dir="/tmp/pr1128-r2-review",
    )
    session = TmuxSession(config, tmux_control=control)
    session._state_machine._state = SessionState.CONNECTED
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    return session


def _entry(
    prompt: str,
    *,
    path: Path | None,
    identity: tuple[int, int] | None,
    offset: int | None,
) -> _InflightMeta:
    turn = _QueuedTurn(prompt=prompt)
    turn.pane_delivery_started = True
    turn.pane_delivery_recorded = True
    return _InflightMeta(
        meta={},
        completion_event=None,
        internal=False,
        dispatched_at=1.0,
        turn=turn,
        transcript_path_at_paste=path,
        transcript_file_identity_at_paste=identity,
        transcript_offset_at_paste=offset,
    )


def _user_row(prompt: str) -> bytes:
    return (
        json.dumps({"type": "user", "message": {"content": prompt}})
        + "\n"
    ).encode()


def test_missing_paste_boundary_is_unavailable_not_definitively_unconsumed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A transient pre-paste stat failure cannot justify a duplicate replay."""
    prompt = "side effect already accepted while stat was unavailable"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b'{"type":"system"}\n')
    session = _session(transcript)
    original_stat = Path.stat

    def failing_stat(path: Path, *args, **kwargs):
        if path == transcript:
            raise PermissionError("transient pre-paste stat failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    path, identity, offset = session._capture_transcript_occurrence_ticket()
    monkeypatch.setattr(Path, "stat", original_stat)
    assert (path, identity, offset) == (transcript, None, None)

    with transcript.open("ab") as handle:
        handle.write(_user_row(prompt))
    entry = _entry(prompt, path=path, identity=identity, offset=offset)

    assert session._phantom_consumption_verdicts([entry]) == [None]


def test_accepted_duplicate_without_ticket_still_reserves_its_user_row(
    tmp_path: Path,
) -> None:
    """An accepted occurrence cannot donate its row after ticket capture failed."""
    prompt = "equal prompt pasted twice before the first user row"
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()
    stat = transcript.stat()
    first = _entry(prompt, path=transcript, identity=None, offset=None)
    first.turn.transport_accepted = True
    second = _entry(
        prompt,
        path=transcript,
        identity=(stat.st_dev, stat.st_ino),
        offset=0,
    )
    session = _session(transcript)

    transcript.write_bytes(_user_row(prompt))

    assert session._phantom_consumption_verdicts([first, second]) == [
        True,
        False,
    ]


def test_same_inode_truncate_and_regrow_does_not_hide_current_occurrence(
    tmp_path: Path,
) -> None:
    """Net file size cannot detect copy-truncate once the file regrows."""
    prompt = "current occurrence after in-place transcript reset"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"x" * 1024)
    before = transcript.stat()
    entry = _entry(
        prompt,
        path=transcript,
        identity=(before.st_dev, before.st_ino),
        offset=before.st_size,
    )
    session = _session(transcript)

    # Opening with wb truncates in place, preserving device+inode. Regrow past
    # the old EOF so a final-size-only test cannot see that the offset is stale.
    transcript.write_bytes(_user_row(prompt) + b" " * 2048)
    after = transcript.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after.st_size > before.st_size

    assert session._phantom_consumption_verdicts([entry]) == [True]


@pytest.mark.asyncio
async def test_replacement_file_cannot_advance_current_scheduler_from_old_row(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Identity reset must not promote a stale duplicate to durable acceptance."""
    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.01)
    prompt = "recurring scheduler prompt"
    transcript = tmp_path / "transcript.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(_user_row(prompt))
    transcript.write_bytes(b'{"type":"system"}\n')
    before = transcript.stat()
    entry = _entry(
        prompt,
        path=transcript,
        identity=(before.st_dev, before.st_ino),
        offset=before.st_size,
    )
    session = _session(transcript)
    session._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": time.time() + 1.0,
    }
    session._transcript_recently_grew = lambda *_args: False
    delivery = asyncio.get_running_loop().create_future()
    durable_accept = MagicMock(return_value=True)
    entry.turn.scheduler_delivery = delivery
    entry.turn.scheduler_accept = durable_accept
    entry.turn.scheduler_serialized = True
    session._scheduler_pending_turns.append(entry.turn)
    session._inflight_metas.append(entry)
    session._head_started_at = time.time() - 1.0

    # The duplicate row predates the paste ticket, but is moved under the bound
    # pathname afterward. Resetting the scan offset to zero must not accept it.
    os.replace(replacement, transcript)

    watchdog = asyncio.create_task(session._inflight_watchdog())
    try:
        for _ in range(200):
            if not session._inflight_metas:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("idle watchdog did not reconcile the seeded meta")
    finally:
        watchdog.cancel()
        with suppress(asyncio.CancelledError):
            await watchdog

    durable_accept.assert_not_called()
    assert not delivery.done() or delivery.result() is False
    assert entry.turn in list(session._message_queue._queue)


def test_replacement_between_stat_and_open_loses_positive_provenance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A newly opened inode cannot inherit an old offset or certify its rows."""
    prompt = "current occurrence in file installed during the read race"
    transcript = tmp_path / "transcript.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    transcript.write_bytes(b"x" * 1024)
    before = transcript.stat()
    entry = _entry(
        prompt,
        path=transcript,
        identity=(before.st_dev, before.st_ino),
        offset=before.st_size,
    )
    session = _session(transcript)
    replacement.write_bytes(_user_row(prompt))

    original_open = Path.open
    original_fstat = os.fstat
    swapped = False
    fstat_calls = 0

    def racing_open(path: Path, *args, **kwargs):
        nonlocal swapped
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == transcript and mode == "rb" and not swapped:
            os.replace(replacement, transcript)
            swapped = True
        return original_open(path, *args, **kwargs)

    def tracking_fstat(fd: int):
        nonlocal fstat_calls
        fstat_calls += 1
        return original_fstat(fd)

    monkeypatch.setattr(Path, "open", racing_open)
    monkeypatch.setattr(os, "fstat", tracking_fstat)

    # Although this fixture creates the replacement after the ticket, that
    # ordering is not observable by production code.  Once fstat reveals a
    # different inode, its row is indistinguishable from the stale replacement
    # in the preceding forgery case and is therefore allocation-only, never
    # positive proof for this unaccepted candidate.
    assert session._phantom_consumption_verdicts([entry]) == [False]
    assert fstat_calls >= 1


def test_one_physical_row_cannot_be_allocated_twice_through_path_aliases(
    tmp_path: Path,
) -> None:
    """Occurrence identity is device+inode+offset, not pathname+offset."""
    prompt = "duplicate prompt with one physical transcript row"
    transcript = tmp_path / "transcript.jsonl"
    alias = tmp_path / "transcript-alias.jsonl"
    transcript.touch()
    os.link(transcript, alias)
    stat = transcript.stat()
    identity = (stat.st_dev, stat.st_ino)
    first = _entry(prompt, path=transcript, identity=identity, offset=0)
    second = _entry(prompt, path=alias, identity=identity, offset=0)
    session = _session(transcript)

    transcript.write_bytes(_user_row(prompt))

    assert session._phantom_consumption_verdicts([first, second]) == [
        True,
        False,
    ]


def test_occurrence_probe_does_not_scan_unbounded_output_after_early_match(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The watchdog must not synchronously scan arbitrary transcript growth."""
    prompt = "prompt row appears immediately after the paste boundary"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b'{"type":"system"}\n')
    before = transcript.stat()
    entry = _entry(
        prompt,
        path=transcript,
        identity=(before.st_dev, before.st_ino),
        offset=before.st_size,
    )
    session = _session(transcript)
    assistant_row = b'{"type":"assistant","message":{"content":"x"}}\n'
    with transcript.open("ab") as handle:
        handle.write(_user_row(prompt))
        repetitions = (MAX_EXPECTED_SCAN * 2) // len(assistant_row) + 100
        handle.write(assistant_row * repetitions)

    original_open = Path.open
    observed = {"bytes": 0}

    class CountingReader:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __enter__(self):
            self._wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self._wrapped.__exit__(*args)

        def seek(self, *args):
            return self._wrapped.seek(*args)

        def tell(self):
            return self._wrapped.tell()

        def readline(self, *args):
            raw = self._wrapped.readline(*args)
            observed["bytes"] += len(raw)
            return raw

    def counting_open(path: Path, *args, **kwargs):
        wrapped = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == transcript and mode == "rb":
            return CountingReader(wrapped)
        return wrapped

    monkeypatch.setattr(Path, "open", counting_open)

    assert session._phantom_consumption_verdicts([entry]) == [True]
    assert observed["bytes"] <= MAX_EXPECTED_SCAN
