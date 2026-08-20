"""Additional exact-contract probes for PR #1128 R3.

Run from the detached exact-head checkout with its ``src`` directory on
``PYTHONPATH``.  These cases intentionally sit outside the committed suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxSession,
    _InflightMeta,
    _QueuedTurn,
    _TmuxControl,
)

MAX_SCAN = 4 * 1024 * 1024


def _session(transcript: Path) -> TmuxSession:
    control = MagicMock(spec=_TmuxControl)
    control.session_name = "pinky-pr1128-r3-review"
    session = TmuxSession(
        StreamingSessionConfig(
            agent_name="review",
            working_dir="/tmp/pr1128-r3-review",
        ),
        tmux_control=control,
    )
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    return session


def _entry(prompt: str, transcript: Path) -> _InflightMeta:
    stat = transcript.stat()
    return _InflightMeta(
        meta={},
        completion_event=None,
        internal=False,
        dispatched_at=1.0,
        turn=_QueuedTurn(prompt=prompt),
        transcript_path_at_paste=transcript,
        transcript_file_identity_at_paste=(stat.st_dev, stat.st_ino),
        transcript_offset_at_paste=stat.st_size,
    )


def _user_row(prompt: str) -> bytes:
    return (
        json.dumps({"type": "user", "message": {"content": prompt}}) + "\n"
    ).encode()


def test_changed_overlap_of_long_row_is_positive_same_inode_proof(
    tmp_path: Path,
) -> None:
    """A changed captured-suffix overlap proves a same-inode row mutation."""
    transcript = tmp_path / "transcript.jsonl"
    old_prompt = "a" * 5000
    current_prompt = "b" * 5000
    old_row = _user_row(old_prompt)
    current_row = _user_row(current_prompt)
    assert len(old_row) == len(current_row) > 4096
    transcript.write_bytes(old_row)
    entry = _entry(current_prompt, transcript)
    session = _session(transcript)

    transcript.write_bytes(current_row)
    assert transcript.stat().st_size == entry.transcript_offset_at_paste
    assert entry.transcript_anchor_start_at_paste is not None
    assert entry.transcript_anchor_start_at_paste > 0
    assert current_row[entry.transcript_anchor_start_at_paste :] != (
        entry.transcript_anchor_at_paste
    )

    # Commitment 3 permits positive proof when the row bytes differ from the
    # captured exact suffix over their pre-send-boundary overlap.
    assert session._phantom_consumption_verdicts([entry]) == [True]


def test_scan_budget_is_cumulative_across_physical_sources(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """One reconciliation must not synchronously read 4 MiB per old file."""
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    first_path.touch()
    second_path.touch()
    first = _entry("missing first prompt", first_path)
    second = _entry("missing second prompt", second_path)
    session = _session(first_path)
    first_path.write_bytes(b"x" * (MAX_SCAN + 1024))
    second_path.write_bytes(b"y" * (MAX_SCAN + 1024))

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

        def fileno(self):
            return self._wrapped.fileno()

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
        if path in {first_path, second_path} and mode == "rb":
            return CountingReader(wrapped)
        return wrapped

    monkeypatch.setattr(Path, "open", counting_open)

    session._phantom_consumption_verdicts([first, second])
    assert observed["bytes"] <= MAX_SCAN
