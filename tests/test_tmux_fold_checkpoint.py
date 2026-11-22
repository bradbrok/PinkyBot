"""Bounded fold-history progress, validation, equivalence and cold-start coverage."""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from tests._fold_history_reference import _assert_reference, _probe_with_classes
from tests.test_tmux_fold_remove_consumption import (
    _append_entry,
    _append_fold_chain,
    _attachment_entry,
    _bind_ticket,
    _make_session,
    _queue_entry,
    _seed_inflight,
)

BUDGET = 4 * 1024 * 1024


def _bind(session, path):
    session._tailer = TmuxTranscriptTailer(
        transcript_path=path, on_turn_complete=lambda _response: None,
        agent_name=session.agent_name,
    )


def _fill(path, size, *, single=False):
    row = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "x" * (size if single else 2000)}]}}).encode() + b"\n"
    with path.open("ab") as handle:
        for _ in range(1 if single else math.ceil(size / len(row))):
            handle.write(row)


def _warm(session, path, *, maximum=100):
    for _ in range(maximum):
        session._warm_fold_history()
        state = session._fold_history._states.get(path)
        if state is not None and state.complete:
            return state
    pytest.fail("bounded warm-up did not complete")


@pytest.mark.parametrize("history_bytes", [0, BUDGET // 2, BUDGET + 1024, 2 * BUDGET])
@pytest.mark.parametrize("attachment_first", [False, True])
def test_checkpoint_proves_long_history_after_bounded_warmup(
    tmp_path, history_bytes, attachment_first,
):
    session = _make_session()
    path = tmp_path / "history.jsonl"
    path.write_text('{"type":"system"}\n')
    _fill(path, history_bytes)
    entry = _seed_inflight(session, prompt="queued prompt after history")
    _bind_ticket(entry, path)
    _append_fold_chain(path, entry.turn.prompt, attachment_first=attachment_first)
    _bind(session, path)
    _warm(session, path)
    verdicts, classes = _probe_with_classes(session, [entry])
    assert verdicts == [True]
    assert classes == ["a"]
    _assert_reference(session, [entry], verdicts, classes)


def test_existing_fold_scenarios_match_the_classified_reference(tmp_path):
    root = Path(__file__).resolve().parents[1]
    files = sorted(str(p.relative_to(root)) for p in (root / "tests").glob("test_tmux_fold_*.py")
                   if p.name != Path(__file__).name)
    assert len(files) == 4
    result = subprocess.run([
        sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-p", "tests._fold_history_reference", "--fold-history-differential",
        "--basetemp=" + str(tmp_path / "reference"), *files,
    ], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    assert result.returncode == 0, result.stdout


def test_seeded_incremental_fold_differential(tmp_path, monkeypatch):
    rng = random.Random(1327)
    observed = set()
    started = time.perf_counter()
    session = _make_session()
    for case in range(2000):
        session._inflight_metas.clear()
        session._head_started_at = None
        path = tmp_path / (str(case) + ".jsonl")
        path.write_text('{"type":"system"}\n')
        prompts = ["repeat", "repeat", "other", "cancel"]
        entries = [_seed_inflight(session, prompt=rng.choice(prompts)) for _ in range(rng.randint(1, 4))]
        for entry in entries:
            _bind_ticket(entry, path)
        _bind(session, path)
        rows = []
        for _ in range(rng.randint(8, 32)):
            prompt = rng.choice(prompts)
            kind = rng.randrange(9)
            if kind < 3:
                row = _queue_entry("enqueue", prompt)
            elif kind < 5:
                row = _queue_entry("remove", prompt)
            elif kind < 7:
                row = _attachment_entry(prompt)
            elif kind == 7:
                row = {"type": "assistant", "message": {"content": "ignored"}}
            else:
                row = None
            rows.append((json.dumps(row).encode() if row is not None else b"junk") + b"\n")
        data = b"".join(rows)
        passes = rng.randint(2, 5)
        cuts = sorted(rng.sample(range(1, len(data)), passes - 1)) + [len(data)]
        previous = 0
        for end in cuts:
            with path.open("ab") as handle:
                handle.write(data[previous:end])
            previous = end
            if case % 3 == 0:
                session._warm_fold_history()
            with monkeypatch.context() as patch:
                patch.setattr(tmux_session, "_PHANTOM_TRANSCRIPT_SCAN_BYTES", 64 if case % 7 == 0 else BUDGET)
                verdicts, classes = _probe_with_classes(session, entries)
                _assert_reference(session, entries, verdicts, classes)
            observed.update(classes)
        path.unlink()
    elapsed = time.perf_counter() - started
    assert observed == {"a", "b", "c"}
    assert elapsed < 60
    print("fold_differential=" + json.dumps({"cases": 2000, "classes": sorted(observed), "seconds": elapsed}))


@pytest.mark.parametrize("change", ["truncate", "replace", "anchor", "path", "exception"])
def test_checkpoint_validation_discards_stale_history(tmp_path, monkeypatch, change):
    session = _make_session()
    path = tmp_path / "old.jsonl"
    path.write_text('{"type":"system"}\n')
    entry = _seed_inflight(session, prompt="validation prompt")
    _bind_ticket(entry, path)
    _append_fold_chain(path, entry.turn.prompt, attachment_first=False)
    _bind(session, path)
    _warm(session, path)
    assert session._phantom_consumption_verdicts([entry]) == [True]
    if change == "truncate":
        path.write_text('{"type":"system"}\n')
    elif change == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_text('{"type":"system"}\n')
        replacement.replace(path)
    elif change == "anchor":
        text = path.read_text()
        assert text.count('"operation": "remove"') == 1
        path.write_text(text.replace('"operation": "remove"', '"operation": "cancel"'))
    elif change == "path":
        other = tmp_path / "other.jsonl"
        other.write_text('{"type":"system"}\n')
        _bind(session, other)
        entry = _seed_inflight(session, prompt="new path prompt")
        _bind_ticket(entry, other)
    else:
        def fail(*_args):
            raise OSError("injected validation failure")
        monkeypatch.setattr(os, "pread", fail)
    assert session._phantom_consumption_verdicts([entry]) == [False]
    if change == "replace":
        state = session._fold_history._states[path]
        current = path.stat()
        assert state.identity == (current.st_dev, current.st_ino)
    if change in ("path", "exception"):
        assert path not in session._fold_history._states


def test_partial_row_does_not_advance_checkpoint(tmp_path):
    session = _make_session()
    path = tmp_path / "partial.jsonl"
    path.write_text('{"type":"system"}\n')
    entry = _seed_inflight(session, prompt="partial suffix prompt")
    _bind_ticket(entry, path)
    _append_fold_chain(path, entry.turn.prompt, attachment_first=True)
    _bind(session, path)
    _warm(session, path)
    boundary = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b'{"type":"progress"')
    session._warm_fold_history()
    state = session._fold_history._states[path]
    assert state.offset == boundary
    assert state.cursor > state.offset
    assert not state.complete
    assert session._phantom_consumption_verdicts([entry]) == [False]
    with path.open("ab") as handle:
        handle.write(b"}\n")
    _warm(session, path)
    assert session._phantom_consumption_verdicts([entry]) == [True]


class _ReadCounter:
    def __init__(self, wrapped, counter):
        self._wrapped, self.counter = wrapped, counter

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def __enter__(self):
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args):
        return self._wrapped.__exit__(*args)

    def readline(self, limit):
        data = self._wrapped.readline(limit)
        self.counter[0] += len(data)
        return data


@pytest.mark.parametrize("single", [False, True])
def test_reconciliation_budget_and_mid_delta_progress(tmp_path, monkeypatch, single):
    session = _make_session()
    path = tmp_path / "large.jsonl"
    path.write_text('{"type":"system"}\n')
    _fill(path, BUDGET + 1024 * 1024 if single else 2 * BUDGET, single=single)
    entry = _seed_inflight(session, prompt="bounded progress prompt")
    _bind_ticket(entry, path)
    _append_fold_chain(path, entry.turn.prompt, attachment_first=True)
    _bind(session, path)
    counter = [0]
    opened, pread = session._open_transcript, os.pread
    monkeypatch.setattr(session, "_open_transcript", lambda *args: _ReadCounter(opened(*args), counter))

    def counted_pread(*args):
        data = pread(*args)
        counter[0] += len(data)
        return data

    monkeypatch.setattr(os, "pread", counted_pread)
    offsets, cursors, results = [], [], []
    for _ in range(8):
        before = counter[0]
        results.append(session._phantom_consumption_verdicts([entry]))
        assert counter[0] - before <= BUDGET
        state = session._fold_history._states[path]
        offsets.append(state.offset)
        cursors.append(state.cursor)
        if state.complete:
            break
        assert results[-1] == [False]
    assert results[0] == [False] and results[-1] == [True]
    assert cursors == sorted(cursors) and len(set(cursors)) > 1
    assert offsets == sorted(offsets)


def test_pending_row_cap_drops_the_buffer(tmp_path):
    session = _make_session()
    assert session._fold_history.max_pending_row_bytes == 64 * 1024 * 1024
    session._fold_history.max_pending_row_bytes = 256
    path = tmp_path / "capped.jsonl"
    path.write_text('{"type":"system"}\n')
    _fill(path, 1024, single=True)
    _bind(session, path)
    session._warm_fold_history()
    state = session._fold_history._states[path]
    assert state.oversized and not state.complete
    assert state.pending_size == 0 and state.pending_chunks == []


@pytest.mark.parametrize("rewrite", [False, True])
def test_compact_rows_preserve_anchor_rewrite_proof(tmp_path, rewrite):
    session = _make_session()
    path = tmp_path / "compact.jsonl"
    path.write_text('{"type":"system"}\n')
    prompt = "long prompt " + "p" * 100_000
    _append_entry(path, {**_queue_entry("enqueue", prompt), "epoch": 0})
    _bind(session, path)
    _warm(session, path)
    entry = _seed_inflight(session, prompt=prompt)
    _bind_ticket(entry, path)
    if rewrite:
        path.write_text(path.read_text().replace('"epoch": 0', '"epoch": 1'))
    _append_entry(path, _queue_entry("remove", prompt))
    _append_entry(path, _attachment_entry(prompt))
    verdicts, classes = _probe_with_classes(session, [entry])
    _assert_reference(session, [entry], verdicts, classes)
    assert verdicts == [rewrite]
    state = session._fold_history._states[path]
    assert state.complete
    for chain in state.complete_chains:
        for row in [chain.enqueue, chain.remove, chain.attachment]:
            assert len(row.raw) <= 4096
            assert row.raw_start > 0


@pytest.mark.parametrize("shallow_check", [False, True])
def test_restore_abort_preserves_prior_state_and_atomic_publication(tmp_path, shallow_check):
    session = _make_session()
    path = tmp_path / "abort.jsonl"
    path.write_text('{"type":"system"}\n')
    _append_entry(path, _queue_entry("enqueue", "unfinished"))
    _bind(session, path)
    old = _warm(session, path)
    _append_entry(path, _attachment_entry("unfinished"))
    observed = []
    attempts = [0]
    opened = session._open_transcript

    class FailingRead(_ReadCounter):
        def readline(self, limit):
            attempts[0] += 1
            if attempts[0] <= 2:
                observed.append(session._fold_history._states.get(path) is old)
            if attempts[0] > 1:
                raise OSError("injected read abort")
            return self._wrapped.readline(limit)

    session._open_transcript = lambda *args: FailingRead(opened(*args), [0])
    session._warm_fold_history()
    assert path not in session._fold_history._states
    if shallow_check:
        assert old.open_occurrences[0].attachment is None
    else:
        assert observed and all(observed)


async def _drive_ticks(session, monkeypatch, clock, limit):
    real_sleep = asyncio.sleep
    elapsed, previous = [], [None]

    async def sleep(delay):
        if delay == tmux_session._WATCHDOG_TICK_SEC:
            now = time.perf_counter()
            if previous[0] is not None:
                elapsed.append(now - previous[0])
            if clock.ticks >= limit:
                raise asyncio.CancelledError
            clock.now += delay
            clock.ticks += 1
            previous[0] = time.perf_counter()
        await real_sleep(0)

    session._config.live_status_fn = lambda: {"status": "idle", "last_updated": clock.now + 1}
    session._transcript_recently_grew = lambda *_args: False
    with monkeypatch.context() as patch:
        patch.setattr(tmux_session.time, "time", lambda: clock.now)
        patch.setattr(tmux_session.asyncio, "sleep", sleep)
        await session._inflight_watchdog()
    return elapsed


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [2 * BUDGET, 10 * 1024 * 1024])
async def test_real_watchdog_cold_start_has_zero_replays(tmp_path, monkeypatch, size):
    session = _make_session()
    path = tmp_path / "cold.jsonl"
    path.write_text('{"type":"system"}\n')
    _fill(path, size)
    entry = _seed_inflight(session, prompt="cold-start consumed prompt")
    _bind_ticket(entry, path)
    _append_fold_chain(path, entry.turn.prompt, attachment_first=False)
    _bind(session, path)
    clock = SimpleNamespace(now=100_000.0, ticks=0)
    session._head_started_at = entry.dispatched_at = clock.now
    ticks = math.ceil(tmux_session._TURN_DONE_TIMEOUT_SEC / tmux_session._WATCHDOG_TICK_SEC) + 2
    elapsed = await _drive_ticks(session, monkeypatch, clock, ticks)
    assert not session._inflight_metas and session._message_queue.empty()
    assert entry.turn.replay_count == 0
    assert session._fold_history._states[path].complete
    assert session._phantom_consumption_verdicts([entry]) == [True]
    ordered = sorted(elapsed)
    print("fold_tick_timing=" + json.dumps({
        "size": size, "ticks": ticks, "tick_s": tmux_session._WATCHDOG_TICK_SEC,
        "threshold_s": tmux_session._TURN_DONE_TIMEOUT_SEC,
        "max_ms": max(ordered) * 1000, "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1] * 1000,
    }))


@pytest.mark.asyncio
async def test_watchdog_warms_without_pending_candidates(tmp_path, monkeypatch):
    session = _make_session()
    path = tmp_path / "no-pending.jsonl"
    path.write_text('{"type":"system"}\n')
    _fill(path, 10 * 1024 * 1024)
    _bind(session, path)
    clock = SimpleNamespace(now=100_000.0, ticks=0)
    await _drive_ticks(session, monkeypatch, clock, 8)
    assert session._fold_history._states[path].complete
    assert clock.ticks * tmux_session._WATCHDOG_TICK_SEC < tmux_session._TURN_DONE_TIMEOUT_SEC


@pytest.mark.asyncio
async def test_genuinely_unconsumed_prompt_still_replays_to_cap(tmp_path, monkeypatch):
    session = _make_session()
    path = tmp_path / "unconsumed.jsonl"
    path.write_text('{"type":"system"}\n')
    _bind(session, path)
    entry = _seed_inflight(session, prompt="not consumed")
    turn = entry.turn
    clock = SimpleNamespace(now=100_000.0, ticks=0)
    for attempt in range(tmux_session._inflight_replay_cap() + 1):
        _bind_ticket(entry, path)
        entry.dispatched_at = clock.now - tmux_session._TURN_DONE_TIMEOUT_SEC - 1
        session._head_started_at = entry.dispatched_at
        await _drive_ticks(session, monkeypatch, clock, clock.ticks + 3)
        assert not session._inflight_metas
        if attempt < tmux_session._inflight_replay_cap():
            assert session._message_queue.get_nowait() is turn
            assert turn.replay_count == attempt + 1
            session._inflight_metas.append(entry)
        else:
            assert session._message_queue.empty()
            assert turn.replay_count == tmux_session._inflight_replay_cap() + 1
