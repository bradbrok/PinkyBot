"""Recovery across process startup, runtime layouts and filesystem races."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import codex_tmux_session, codex_tmux_transcript, tmux_session
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.codex_tmux_transcript import CodexTmuxTranscriptTailer
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer
from tests import test_tmux_session as session_tests


def _write_transcript(path, working_dir):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"cwd": str(working_dir)},
            }
        )
        + "\n"
    )


def _retained_session(tmp_path, monkeypatch, runtime="claude", cold=False):
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    if runtime == "codex":
        home = tmp_path / "codex"
        monkeypatch.setattr(codex_tmux_session, "codex_home_for", lambda config: home)
        monkeypatch.setattr(codex_tmux_transcript, "codex_home_for", lambda config: home)
        ss = CodexTmuxSession(
            StreamingSessionConfig(agent_name="test-agent", working_dir=str(working_dir)),
            tmux_control=session_tests._make_mock_tmux(),
        )
        root = home / "sessions"
        old = root / "2026" / "09" / "24" / "rollout-session.jsonl"
        tailer_class = CodexTmuxTranscriptTailer
    else:
        ss, _ = session_tests._make_session(agent_name="test-agent")
        ss._config.working_dir = str(working_dir)
        root = tmp_path / "transcripts"
        old = root / "old.jsonl"
        tailer_class = TmuxTranscriptTailer
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ss, "_project_dir", lambda: root)
    if not cold:
        _write_transcript(old, working_dir)
    ss._tailer = tailer_class(
        transcript_path=tmux_session._PLACEHOLDER_TRANSCRIPT_PATH if cold else old,
        on_turn_complete=AsyncMock(),
        path_discovery=(
            ss._discover_transcript_path
            if runtime == "codex"
            else ss._discover_post_launch_transcript_path
        ),
    )
    if not cold:
        ss._tailer.set_offset(old.stat().st_size)
    monkeypatch.setattr(ss._tailer, "start", AsyncMock())
    monkeypatch.setattr(ss._tailer, "stop", AsyncMock())
    ss._config.force_fresh_context_once = True
    monkeypatch.setattr(ss._tmux, "has_session", AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr(ss, "_build_repl_env", lambda **kwargs: {})
    monkeypatch.setattr(ss, "_prepare_tmux_spawn", lambda: None)
    monkeypatch.setattr(ss, "_reap_retained_spawn_cleanup_debt", AsyncMock())
    monkeypatch.setattr(tmux_session, "_seed_claude_trust_file", lambda *args: False)
    monkeypatch.setattr(tmux_session, "_async_sleep", AsyncMock())
    monkeypatch.setattr(tmux_session._auth_relay, "enabled", lambda: False)
    return ss, old, root


@pytest.mark.parametrize("runtime", ["claude", "codex"])
async def test_recovery_captures_history_before_process_creation(tmp_path, monkeypatch, runtime):
    ss, old, root = _retained_session(tmp_path, monkeypatch, runtime)
    new = root / "2026" / "09" / "25" / old.name if runtime == "codex" else root / "new.jsonl"

    async def launch(**kwargs):
        _write_transcript(new, ss._config.working_dir)
        os.utime(new, (old.stat().st_mtime + 10,) * 2)
        return session_tests._ok()

    monkeypatch.setattr(ss._tmux, "new_session", launch)
    clock = session_tests._recovery_clock(monkeypatch)
    await TmuxSession._spawn_tmux_repl(ss)
    await ss._first_bind_recovery_task
    assert ss._tailer.transcript_path == new
    assert ss._tailer.offset == 0
    assert not ss._tailer_first_bind_pending
    assert not ss._session_ready_event.is_set()
    assert clock.sleeps == [5.0]
    assert old in ss._prelaunch_transcripts
    assert new not in ss._prelaunch_transcripts


@pytest.mark.parametrize("runtime", ["claude", "codex"])
async def test_recovery_acknowledges_self_heal_without_rewind(tmp_path, monkeypatch, runtime):
    ss, _, root = _retained_session(tmp_path, monkeypatch, runtime, cold=True)
    ss._tailer = None
    tailer_class = CodexTmuxTranscriptTailer if runtime == "codex" else TmuxTranscriptTailer
    monkeypatch.setattr(tailer_class, "start", AsyncMock())
    monkeypatch.setattr(tailer_class, "stop", AsyncMock())
    new = (
        root / "2026" / "09" / "25" / "rollout-new.jsonl"
        if runtime == "codex"
        else root / "new.jsonl"
    )
    logs = []
    monkeypatch.setattr(tmux_session, "_log", logs.append)

    def tick(number):
        if number == 1:
            _write_transcript(new, ss._config.working_dir)
            os.utime(new, (getattr(ss._tailer, "_path_bound_at", new.stat().st_mtime) + 10,) * 2)
            ss._tailer._try_self_heal_repoint()
            assert ss._tailer.transcript_path == new
            ss._tailer.set_offset(7)

    clock = session_tests._recovery_clock(monkeypatch, tick)
    bind = MagicMock(wraps=ss._set_transcript_path_internal)
    monkeypatch.setattr(ss, "_set_transcript_path_internal", bind)
    await TmuxSession._spawn_tmux_repl(ss)
    await ss._first_bind_recovery_task
    assert not ss._tailer_first_bind_pending
    assert ss._tailer.offset == 7
    bind.assert_not_called()
    assert not ss._session_ready_event.is_set()
    assert clock.sleeps == [5.0]
    assert sum("first bind already satisfied by self-heal" in line for line in logs) == 1
    assert not any(line.startswith("TRANSCRIPT_FIRST_BIND_MISSING") for line in logs)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("invalid_kind", ["directory", "symlink", "dangling", "stat_error"])
async def test_recovery_skips_invalid_candidates(tmp_path, monkeypatch, runtime, invalid_kind):
    ss, old, root = _retained_session(tmp_path, monkeypatch, runtime)
    new = root / "rollout-valid.jsonl"
    invalid = root / "rollout-invalid.jsonl"
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == invalid and invalid_kind == "stat_error":
            raise PermissionError("candidate unavailable")
        return original_lstat(path, *args, **kwargs)

    def tick(number):
        if number != 1:
            return
        _write_transcript(new, ss._config.working_dir)
        if invalid_kind == "directory":
            invalid.mkdir()
            os.utime(invalid, (new.stat().st_mtime + 20,) * 2)
        elif invalid_kind == "symlink":
            invalid.symlink_to(old)
            os.utime(old, (new.stat().st_mtime + 20,) * 2)
        elif invalid_kind == "dangling":
            invalid.symlink_to(root / "missing")
        else:
            _write_transcript(invalid, ss._config.working_dir)
            os.utime(invalid, (new.stat().st_mtime + 20,) * 2)
        monkeypatch.setattr(Path, "lstat", lstat)

    clock = session_tests._recovery_clock(monkeypatch, tick)
    await TmuxSession._spawn_tmux_repl(ss)
    await ss._first_bind_recovery_task
    assert ss._tailer.transcript_path == new
    assert not ss._tailer_first_bind_pending
    assert clock.sleeps == [5.0]


async def test_recovery_codex_candidates_exclude_other_working_directories(tmp_path, monkeypatch):
    ss, old, root = _retained_session(tmp_path, monkeypatch, "codex")
    new = root / "2026" / "09" / "25" / old.name
    foreign = root / "2026" / "09" / "25" / "rollout-foreign.jsonl"
    malformed = root / "2026" / "09" / "25" / "rollout-malformed.jsonl"

    def tick(number):
        if number == 1:
            _write_transcript(new, ss._config.working_dir)
            _write_transcript(foreign, tmp_path / "different-work")
            malformed.write_text("not json\n")
            os.utime(foreign, (new.stat().st_mtime + 10,) * 2)
            os.utime(malformed, (new.stat().st_mtime + 20,) * 2)

    session_tests._recovery_clock(monkeypatch, tick)
    await TmuxSession._spawn_tmux_repl(ss)
    await ss._first_bind_recovery_task
    assert ss._tailer.transcript_path == new
    assert old in ss._prelaunch_transcripts
    assert foreign in dict(ss._transcript_candidates())
    assert not ss._is_own_transcript(foreign)


async def test_recovery_never_reads_prelaunch_rollout_history(tmp_path, monkeypatch):
    ss, old, root = _retained_session(tmp_path, monkeypatch, "codex")
    history = {old}
    for number in range(500):
        path = root / "2025" / f"rollout-history-{number}.jsonl"
        cwd = ss._config.working_dir if number % 2 else tmp_path / "other-work"
        _write_transcript(path, cwd)
        history.add(path)
    # Launch-mode discovery is independent of snapshot and recovery scans.
    monkeypatch.setattr(ss, "_has_prior_transcript", lambda: True)
    reads = []
    original_open = Path.open

    def counted_open(path, mode="r", *args, **kwargs):
        if "r" in mode and path.is_relative_to(root):
            reads.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    new = root / "2026" / "09" / "25" / "rollout-new.jsonl"

    def tick(number):
        if number == 2:
            assert reads == [], "snapshot and empty polls must not read history"
            _write_transcript(new, ss._config.working_dir)

    clock = session_tests._recovery_clock(monkeypatch, tick)
    await TmuxSession._spawn_tmux_repl(ss)
    await ss._first_bind_recovery_task
    assert ss._prelaunch_transcripts == history
    assert reads == [new]
    assert clock.sleeps == [5.0, 5.0]
    assert ss._tailer.transcript_path == new


async def _spawn_self_healing_session(tmp_path, monkeypatch, *, cold=False, continued=False):
    ss, old, root = _retained_session(tmp_path, monkeypatch, cold=cold)
    ss._tailer = None
    ss._config.force_fresh_context_once = not continued
    monkeypatch.setattr(TmuxTranscriptTailer, "start", AsyncMock())
    monkeypatch.setattr(TmuxTranscriptTailer, "stop", AsyncMock())
    seen = []
    monkeypatch.setattr(ss, "_on_transcript_entry", seen.append)
    await TmuxSession._spawn_tmux_repl(ss)
    return ss, old, root, seen


async def test_self_heal_keeps_missing_hook_bind_after_previous_shutdown_write(tmp_path, monkeypatch):
    ss, old, root, seen = await _spawn_self_healing_session(tmp_path, monkeypatch)
    try:
        new = root / "new.jsonl"
        assert old in ss._prelaunch_transcripts
        assert not ss._last_launch_used_continue
        assert ss.set_transcript_path(new, session_id="new-session")
        assert not new.exists()

        # The previous session's shutdown write lands after the new bind.
        with old.open("a") as handle:
            handle.write(json.dumps({"type": "user", "message": "old shutdown"}) + "\n")
        os.utime(old, (ss._tailer._path_bound_at + 10,) * 2)
        assert old.stat().st_mtime > ss._tailer._path_bound_at
        ss._tailer._try_self_heal_repoint()

        assert ss._tailer.transcript_path == new
        assert ss._tailer.stats["self_heal_repoints"] == 0
        assert seen == []
        entry = {"type": "queue-operation", "operation": "dequeue", "content": "new turn"}
        new.write_text(json.dumps(entry) + "\n")
        await ss._tailer.read_once()
        assert seen == [entry]
        assert ss._tailer.transcript_path == new
        assert ss._tailer.offset == new.stat().st_size
        assert ss._tailer.stats["self_heal_repoints"] == 0
    finally:
        await ss._stop_tailer()


async def test_self_heal_chooses_postlaunch_file_over_newest_history(tmp_path, monkeypatch):
    ss, old, root, seen = await _spawn_self_healing_session(tmp_path, monkeypatch)
    try:
        new = root / "bound.jsonl"
        assert old in ss._prelaunch_transcripts
        assert ss.set_transcript_path(new, session_id="new-session")
        assert not new.exists()
        postlaunch = root / "postlaunch.jsonl"
        entry = {"type": "queue-operation", "operation": "dequeue", "content": "recovered turn"}
        postlaunch.write_text(json.dumps(entry) + "\n")
        os.utime(postlaunch, (ss._tailer._path_bound_at + 10,) * 2)
        with old.open("a") as handle:
            handle.write(json.dumps({"type": "user", "message": "old shutdown"}) + "\n")
        os.utime(old, (ss._tailer._path_bound_at + 20,) * 2)
        assert postlaunch not in ss._prelaunch_transcripts
        assert old.stat().st_mtime > postlaunch.stat().st_mtime > ss._tailer._path_bound_at
        ss._tailer.set_offset(7)

        ss._tailer._try_self_heal_repoint()

        assert ss._tailer.transcript_path == postlaunch
        assert ss._tailer.offset == 0
        assert ss._tailer.stats["self_heal_repoints"] == 1
        await ss._tailer.read_once()
        assert seen == [entry]
        assert ss._tailer.transcript_path == postlaunch
    finally:
        await ss._stop_tailer()


@pytest.mark.parametrize("mtime_delta", [-10, 10], ids=["older-than-bind", "newer-than-bind"])
async def test_self_heal_continue_launch_preserves_mtime_floor(tmp_path, monkeypatch, mtime_delta):
    ss, old, root, _ = await _spawn_self_healing_session(tmp_path, monkeypatch, continued=True)
    try:
        assert ss._last_launch_used_continue
        assert ss._prelaunch_transcripts == frozenset()
        missing = root / "missing.jsonl"
        assert ss.set_transcript_path(missing, session_id="continued-session")
        os.utime(old, (ss._tailer._path_bound_at + mtime_delta,) * 2)

        ss._tailer._try_self_heal_repoint()

        if mtime_delta < 0:
            assert ss._tailer.transcript_path == missing
            assert ss._tailer.stats["self_heal_repoints"] == 0
            assert ss._tailer.stats["self_heal_stale_skips"] == 1
        else:
            assert ss._tailer.transcript_path == old
            assert ss._tailer.offset == 0
            assert ss._tailer.stats["self_heal_repoints"] == 1
            assert ss._tailer.stats["self_heal_stale_skips"] == 0
    finally:
        await ss._stop_tailer()


async def test_self_heal_cold_start_finds_first_transcript(tmp_path, monkeypatch):
    ss, _, root, seen = await _spawn_self_healing_session(tmp_path, monkeypatch, cold=True)
    try:
        assert ss._prelaunch_transcripts == frozenset()
        assert ss._tailer.transcript_path == tmux_session._PLACEHOLDER_TRANSCRIPT_PATH
        assert not list(root.iterdir())
        new = root / "first.jsonl"
        entry = {"type": "queue-operation", "operation": "dequeue", "content": "first turn"}
        new.write_text(json.dumps(entry) + "\n")

        ss._tailer._try_self_heal_repoint()

        assert ss._tailer.transcript_path == new
        assert ss._tailer.offset == 0
        assert ss._tailer.stats["self_heal_repoints"] == 1
        await ss._tailer.read_once()
        assert seen == [entry]
    finally:
        await ss._stop_tailer()


def test_recovery_caps_postlaunch_content_reads_and_stops_at_first_match(tmp_path, monkeypatch):
    ss, old, root = _retained_session(tmp_path, monkeypatch, "codex")
    ss._prelaunch_transcripts = frozenset({old})
    new = root / "rollout-own.jsonl"
    _write_transcript(new, ss._config.working_dir)
    initial_mtime = new.stat().st_mtime
    limit = codex_tmux_transcript._DISCOVERY_SCAN_LIMIT
    foreign = []
    for number in range(limit + 10):
        path = root / f"rollout-other-{number}.jsonl"
        _write_transcript(path, tmp_path / "other-work")
        os.utime(path, (initial_mtime + number + 1,) * 2)
        foreign.append(path)
    reads = []
    original_open = Path.open

    def counted_open(path, mode="r", *args, **kwargs):
        if "r" in mode and path.is_relative_to(root):
            reads.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    assert ss._discover_post_launch_transcript_path() is None
    assert reads == list(reversed(foreign))[:limit]
    reads.clear()
    os.utime(new, (initial_mtime + limit + 20,) * 2)
    assert ss._discover_post_launch_transcript_path() == new
    assert reads == [new]
