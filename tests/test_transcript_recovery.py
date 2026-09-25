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
        path_discovery=ss._discover_transcript_path,
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
    assert foreign not in dict(ss._transcript_candidates())
