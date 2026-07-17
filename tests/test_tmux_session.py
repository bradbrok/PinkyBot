"""Tests for TmuxSession.

PR8 of the #486 sequence. Focused on the lifecycle choreography +
state-machine integration. The response capture pipeline is PR8b — its
tests will land alongside that PR.

Test strategy:
- Mock ``_TmuxControl`` (the subprocess wrapper) end-to-end; never shell
  out to a real tmux binary.
- Pin the state-machine transitions on every lifecycle path (cold-start
  success/failure, warm-reconnect, idle-sleep, force-restart).
- Pin the concurrent-connect Cases A + B from PR6's framework: greenfield
  backend should get the race protection by construction.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import re
import time as _time
from types import SimpleNamespace
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
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer, TurnResponse
from pinky_daemon.transport_state import SessionState, TransitionResult, Trigger


def _seed_inflight(
    ss: TmuxSession,
    *,
    meta: dict | None = None,
    internal: bool = False,
    completion_event: asyncio.Event | None = None,
    prompt: str = "",
) -> _InflightMeta:
    """Append one ``_InflightMeta`` entry to ``ss._inflight_metas``.

    Pre-#560 tests injected state via ``ss._inflight_meta = {...}``; the
    back-compat setter still does that for ROUTING-only seeds. This
    helper is the modern path — needed when tests want to seed an
    ``internal=True`` or ``completion_event`` entry, or chain multiple
    entries to exercise FIFO + watchdog tail-requeue behavior. Also
    bumps ``_head_started_at`` if this is the new head.

    Returns the entry so the test can assert on it. ``prompt`` lets
    watchdog-requeue tests verify the right turn body is replayed.
    """
    m = meta or {}
    synthetic_turn = _QueuedTurn(
        prompt=prompt,
        platform=m.get("platform", ""),
        chat_id=m.get("chat_id", ""),
        message_id=m.get("message_id", ""),
        internal=internal,
        completion_event=completion_event,
    )
    entry = _InflightMeta(
        meta=m,
        completion_event=completion_event,
        internal=internal,
        dispatched_at=_time.time(),
        turn=synthetic_turn,
    )
    was_empty = not ss._inflight_metas
    ss._inflight_metas.append(entry)
    if was_empty:
        ss._head_started_at = _time.time()
    return entry


def _ok() -> TmuxCommandResult:
    """Successful tmux command result."""
    return TmuxCommandResult(returncode=0, stdout="", stderr="")


def _fail(msg: str = "boom") -> TmuxCommandResult:
    """Failed tmux command result."""
    return TmuxCommandResult(returncode=1, stdout="", stderr=msg)


def _make_mock_tmux(*, has_session_initial: bool = False) -> MagicMock:
    """Build a MagicMock of ``_TmuxControl`` with sensible async defaults.

    All methods return success unless overridden by the test.
    """
    tmux = MagicMock(spec=_TmuxControl)
    tmux.session_name = "pinky-test"
    tmux.has_session = AsyncMock(return_value=has_session_initial)
    tmux.new_session = AsyncMock(return_value=_ok())
    tmux.kill_session = AsyncMock(return_value=_ok())
    tmux.send_keys = AsyncMock(return_value=_ok())
    tmux.paste_text = AsyncMock(return_value=_ok())
    tmux.capture_pane = AsyncMock(return_value=_ok())
    tmux.resize_window = AsyncMock(return_value=_ok())
    return tmux


def _make_session(
    *,
    agent_name: str = "dymok",
    state: SessionState | None = None,
    restart_guard=None,
    tmux: MagicMock | None = None,
    analytics_store=None,
) -> tuple[TmuxSession, MagicMock]:
    """Build a TmuxSession with mocked tmux control.

    Returns (session, tmux_mock). Tests that need to start in a specific
    state pass ``state=...``; the state machine is direct-mutated to that
    state (same bypass pattern existing StreamingSession tests use).
    Optional ``analytics_store`` for tests that pin emission behavior
    (e.g. wake_gate metrics — #570 follow-up).
    """
    cfg = StreamingSessionConfig(
        agent_name=agent_name,
        working_dir="/tmp/tmux-session-test",
        restart_guard=restart_guard,
    )
    tmux = tmux or _make_mock_tmux()
    ss = TmuxSession(cfg, tmux_control=tmux, analytics_store=analytics_store)
    # Skip wake-prompt enqueue by default in unit tests — without a
    # simulated transcript tailer the worker would block forever on the
    # never-completing wake turn. Wake-prompt behavior has its own
    # dedicated tests (see TestWakePromptEnqueue below) which set this
    # flag explicitly to False and provide the tailer simulation.
    ss._skip_wake_prompt_for_tests = True
    if state is not None:
        ss._state_machine._state = state
    return ss, tmux


# ──────────────────────────────────────────────────────────────────────────
# Construction + identity
# ──────────────────────────────────────────────────────────────────────────


def test_default_initial_state_is_uninitialized() -> None:
    ss, _ = _make_session()
    assert ss.state == SessionState.UNINITIALIZED


def test_completed_turn_tracking_starts_false() -> None:
    ss, _ = _make_session()
    assert ss._has_completed_turn is False


def test_id_format_matches_streaming_session() -> None:
    ss, _ = _make_session(agent_name="dymok")
    # Default label → "main"
    assert ss.id == "dymok-main"


def test_resume_handle_is_tmux_session_name() -> None:
    """For tmux, the tmux session name IS the resume handle. Pinning by
    name preserves cwd → claude --continue resumes via that cwd's most-
    recent transcript automatically."""
    ss, _ = _make_session(agent_name="dymok")
    assert ss.resume_handle == "pinky-dymok"


def test_session_name_prefix_isolates_pinky_from_operator_tmux() -> None:
    """Tmux session name has the ``pinky-`` prefix so Pinky-owned sessions
    can be distinguished from the operator's own tmux sessions on the host."""
    ss, _ = _make_session(agent_name="dymok")
    assert ss._session_name.startswith("pinky-")


# ──────────────────────────────────────────────────────────────────────────
# Cold-start lifecycle: UNINITIALIZED → BOOTING → CONNECTED / DEAD
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_start_drives_state_through_booting_to_connected() -> None:
    """Successful cold-start lands in CONNECTED via the BOOT/BOOT_COMPLETE
    Trigger pair. Mirrors StreamingSession's PR6 cold-start contract."""
    ss, tmux = _make_session()
    await ss.connect()
    assert ss.state == SessionState.CONNECTED
    # Exactly one tmux new-session call (the cold-start spawn).
    assert tmux.new_session.await_count == 1


@pytest.mark.asyncio
async def test_cold_start_failure_drives_to_dead_via_boot_failed() -> None:
    """If ``tmux new-session`` fails, cold-start lands BOOTING → DEAD via
    BOOT_FAILED (not silent disconnect)."""
    tmux = _make_mock_tmux()
    tmux.new_session = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(tmux=tmux)
    with pytest.raises(RuntimeError, match="tmux new-session failed"):
        await ss.connect()
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_cold_start_reaps_stale_session_before_spawn() -> None:
    """If a stale tmux session is found at cold-start time (e.g. previous
    daemon crashed without graceful disconnect), reap it first."""
    tmux = _make_mock_tmux(has_session_initial=True)
    ss, _ = _make_session(tmux=tmux)
    await ss.connect()
    # has_session checked, then kill_session called for the stale reap,
    # then new_session for the fresh spawn.
    tmux.has_session.assert_awaited()
    tmux.kill_session.assert_awaited()
    tmux.new_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_cold_start_uses_correct_claude_invocation(tmp_path, monkeypatch) -> None:
    """The in-pane command must be
    ``claude --continue --dangerously-skip-permissions`` when a prior
    transcript exists for cwd. Pinned because a typo in the invocation
    silently breaks billing semantics (would hit SDK credits instead of
    subscription).

    Post-#511: ``--continue`` is gated on transcript existence, so this
    test pre-seeds a fake transcript before asserting.
    """
    # Point HOME at tmp so we can seed a transcript at the encoded-cwd path.
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, tmux = _make_session()
    project_dir = ss._project_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "seed.jsonl").write_text("")
    await ss.connect()
    _, kwargs = tmux.new_session.call_args
    cmd = kwargs["command"]
    assert "claude" in cmd
    assert "--continue" in cmd
    assert "--dangerously-skip-permissions" in cmd


@pytest.mark.asyncio
async def test_cold_start_omits_continue_when_no_prior_transcript(
    tmp_path, monkeypatch
) -> None:
    """Issue #511 regression: a freshly-registered agent has no transcript
    at ``~/.claude/projects/<encoded-cwd>/``. ``claude --continue`` exits 1
    in that case, tmux auto-reaps the detached session, and the Python
    state machine ends up CONNECTED against a dead REPL.

    Fix (#512): cold-start cmd must fall through to ``claude`` (no
    ``--continue``) when no prior transcript exists. The Claude CLI
    then creates a fresh transcript on the first turn, and subsequent
    reconnects find it and resume normally.
    """
    # Point HOME at an empty tmp dir — no project_dir, no transcripts.
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, tmux = _make_session()
    await ss.connect()
    _, kwargs = tmux.new_session.call_args
    cmd = kwargs["command"]
    assert "claude" in cmd
    assert "--dangerously-skip-permissions" in cmd
    # The critical assertion — no --continue when no prior transcript.
    assert "--continue" not in cmd


def test_has_prior_transcript_false_when_project_dir_missing(
    tmp_path, monkeypatch
) -> None:
    """``_has_prior_transcript`` returns False when the encoded-cwd
    project dir doesn't exist (cold-start case)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    assert ss._has_prior_transcript() is False


def test_has_prior_transcript_false_when_project_dir_empty(
    tmp_path, monkeypatch
) -> None:
    """``_has_prior_transcript`` returns False when the project dir
    exists but contains no ``*.jsonl`` transcripts.

    Defends the race where Claude Code has created the directory
    (e.g. via a SessionStart hook) but hasn't written a transcript yet.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    project_dir = ss._project_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    # Drop an unrelated file in there to confirm the glob filters by suffix.
    (project_dir / "not-a-transcript.txt").write_text("")
    assert ss._has_prior_transcript() is False


def test_has_prior_transcript_true_when_jsonl_exists(tmp_path, monkeypatch) -> None:
    """``_has_prior_transcript`` returns True when at least one .jsonl
    transcript exists for the agent's cwd."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    project_dir = ss._project_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "abc123.jsonl").write_text("")
    assert ss._has_prior_transcript() is True


def test_project_dir_matches_claude_code_encoding(tmp_path, monkeypatch) -> None:
    """Regression guard for the double-dash ``_project_dir`` bug.

    The previous encoder was ``"-" + str(cwd).replace("/", "-")``. Because
    an absolute ``cwd`` already starts with ``/`` (which the replace turns
    into a leading ``-``), prepending another ``-`` produced a *double-dash*
    path (``--Users-...``) that never exists on disk. That made
    ``_has_prior_transcript()`` always return False → ``--continue`` was
    never passed → every tmux restart silently dropped the conversation.

    This test pins ``_project_dir`` to Claude Code's *actual* slug
    algorithm (``[^a-zA-Z0-9]`` → ``-``). Crucially the expected path is
    computed INDEPENDENTLY (not by calling ``_project_dir`` to seed it,
    which is how the prior True-case test hid the bug). It also exercises
    a dot-containing segment (``.pulse-v2``), which the old encoder
    silently mangled.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # A working_dir with a dot segment — guards both the double-dash bug
    # and the dot-collapse case. tmp_path is already canonical, so the
    # session's internal ``.resolve()`` is idempotent here.
    wd = tmp_path / ".pulse-v2" / "agents" / "dymok"
    wd.mkdir(parents=True)
    cfg = StreamingSessionConfig(agent_name="dymok", working_dir=str(wd))
    ss = TmuxSession(cfg, tmux_control=_make_mock_tmux())

    # Independently reproduce Claude Code's encoder. Do NOT route through
    # _project_dir — the whole point is to catch _project_dir diverging.
    expected_name = re.sub(r"[^a-zA-Z0-9]", "-", str(wd.resolve()))
    canonical = tmp_path / ".claude" / "projects" / expected_name

    assert ss._project_dir() == canonical
    # Explicit invariant: the historical bug was a leading double-dash.
    assert not ss._project_dir().name.startswith("--")
    assert ss._project_dir().name.startswith("-")

    # End-to-end: seed at the canonical path and confirm the gate sees it.
    # Under the old buggy encoder, _project_dir would look at the
    # double-dash sibling and this assertion would fail.
    canonical.mkdir(parents=True)
    (canonical / "abc123.jsonl").write_text("")
    assert ss._has_prior_transcript() is True


def test_build_claude_cmd_includes_dangerously_skip_when_no_transcript(
    tmp_path, monkeypatch
) -> None:
    """Even with no prior transcript, the cold-start cmd must still
    carry ``--dangerously-skip-permissions`` (the non-interactive
    bootstrap flag). Pinning so the #511 fix can't accidentally regress
    the unrelated permissions handling.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    cmd = ss._build_claude_cmd()
    assert "claude" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--continue" not in cmd


# ──────────────────────────────────────────────────────────────────────────
# Thinking effort → --effort wiring (#151). tmux historically never passed
# --effort; ultracode resolves to xhigh because the flag rejects "ultracode".
# ──────────────────────────────────────────────────────────────────────────


def test_build_claude_cmd_passes_effort_for_medium(tmp_path, monkeypatch) -> None:
    """Medium is passed EXPLICITLY (model/effort selector fix): the CLI
    persists the last interactive /effort per project dir, so a flagless
    launch would boot a medium-configured agent at whatever the previous
    session ran at."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    ss._config.thinking_effort = "medium"
    assert "--effort medium" in ss._build_claude_cmd()


def test_build_claude_cmd_passes_effort_for_xhigh(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    ss._config.thinking_effort = "xhigh"
    assert "--effort xhigh" in ss._build_claude_cmd()


def test_build_claude_cmd_resolves_ultracode_to_xhigh(tmp_path, monkeypatch) -> None:
    """The CLI flag rejects "ultracode" — it must never reach --effort.
    ultracode resolves to xhigh."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    ss._config.thinking_effort = "ultracode"
    cmd = ss._build_claude_cmd()
    assert "--effort xhigh" in cmd
    assert "ultracode" not in cmd


def test_build_repl_env_expects_resolved_effort_for_ultracode() -> None:
    """PINKY_EXPECTED_EFFORT must be the resolved level (xhigh) so the drift
    hook matches the runtime $CLAUDE_EFFORT and doesn't false-positive."""
    ss, _ = _make_session()
    ss._config.thinking_effort = "ultracode"
    env = ss._build_repl_env()
    assert env["PINKY_EXPECTED_EFFORT"] == "xhigh"


def test_build_repl_env_caps_autocompact_for_codex_sub_proxy(monkeypatch) -> None:
    """gpt-5.6-sol on the ChatGPT-sub proxy (trusted loopback :18765) gets
    CLAUDE_CODE_AUTO_COMPACT_WINDOW=150000 so CC compacts below the sub's real
    ~167k cap — the old 272k value overflowed and wedged solik for 13 hours on
    2026-07-16. Tier-suffix tolerant; loopback host + path OK."""
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    ss, _ = _make_session()
    ss._config.provider_url = "http://localhost:18765"
    ss._config.model = "gpt-5.6-sol[1m]"
    assert ss._build_repl_env()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "150000"
    ss._config.model = "gpt-5.6-sol"  # bare id too
    assert ss._build_repl_env()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "150000"
    ss._config.provider_url = "http://127.0.0.1:18765/v1"  # 127.0.0.1 + path
    assert ss._build_repl_env()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "150000"


def test_build_repl_env_does_not_cap_paid_api_or_claude(monkeypatch) -> None:
    """The 150k cap is scoped to the ChatGPT-sub proxy route ONLY. The SAME
    gpt-5.6-sol slug on a paid API gateway does not inherit that override;
    real 1M Claude agents are never capped."""
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    ss, _ = _make_session()
    ss._config.model = "gpt-5.6-sol[1m]"
    ss._config.provider_url = "https://paid-api-gateway.example/anthropic"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in ss._build_repl_env()
    ss._config.provider_url = ""  # no provider → not the sub proxy
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in ss._build_repl_env()
    ss._config.provider_url = "http://localhost:18765"  # on the proxy, but...
    ss._config.model = "claude-opus-4-8"  # ...real 1M Claude → NOT capped
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in ss._build_repl_env()
    ss._config.model = "claude-opus-4-8[1m]"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in ss._build_repl_env()


def test_build_repl_env_autocompact_allows_only_safer_ambient_override(
    monkeypatch,
) -> None:
    """An ambient override can compact earlier but cannot raise the 150k cap."""
    ss, _ = _make_session()
    ss._config.provider_url = "http://localhost:18765"
    ss._config.model = "gpt-5.6-sol[1m]"
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "120000")
    assert ss._build_repl_env()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "120000"
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "240000")
    assert ss._build_repl_env()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "150000"
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "not-a-number")
    assert ss._build_repl_env()["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "150000"


def test_is_codex_sub_proxy_classifier_is_total() -> None:
    """The route classifier matches only the trusted http(s) loopback :18765 and
    is total — malformed / non-numeric / out-of-range ports fail closed (return
    False) rather than raising (parsed.port raises ValueError only on access)."""
    match = TmuxSession._is_codex_sub_proxy
    assert match("http://localhost:18765") is True
    assert match("http://127.0.0.1:18765/v1") is True
    assert match("https://[::1]:18765") is True
    assert match("https://paid-api-gateway.example/anthropic") is False
    assert match("http://localhost:9999") is False
    assert match("http://localhost.example:18765") is False  # subdomain
    assert match("") is False
    assert match("http://localhost:not-a-port") is False  # non-numeric port
    assert match("http://localhost:99999") is False  # out-of-range port
    assert match("ftp://localhost:18765") is False  # wrong scheme


def test_build_repl_env_fails_closed_on_malformed_provider_url() -> None:
    """A malformed provider_url must not crash _build_repl_env or cap — it fails
    closed (no CLAUDE_CODE_AUTO_COMPACT_WINDOW), never raises."""
    ss, _ = _make_session()
    ss._config.model = "gpt-5.6-sol[1m]"
    ss._config.provider_url = "http://localhost:not-a-port"
    env = ss._build_repl_env()  # must not raise
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env


def test_set_effort_accepts_ultracode() -> None:
    ss, _ = _make_session()
    ss.set_effort("ultracode")
    assert ss._effort_override == "ultracode"
    assert ss.effective_effort == "ultracode"


# ──────────────────────────────────────────────────────────────────────────
# Live per-session effort/model apply (model/effort selector fix).
# apply_effort_live types the interactive /effort into an idle REPL,
# auto-confirming the mid-session dialog; busy REPLs defer to next idle;
# disconnected sessions stash for the relaunch --effort flag.
# ──────────────────────────────────────────────────────────────────────────


def _capture(text: str) -> TmuxCommandResult:
    return TmuxCommandResult(returncode=0, stdout=text, stderr="")


@pytest.fixture()
def fast_settle(monkeypatch):
    """Shrink the post-command settle so live-apply tests don't sleep
    real wall-clock time."""
    monkeypatch.setattr(tmux_session, "_REPL_COMMAND_SETTLE_SEC", 0.001)


@pytest.mark.asyncio
async def test_apply_effort_live_idle_types_command(fast_settle) -> None:
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_capture("> \n"))
    result = await ss.apply_effort_live("xhigh")
    assert result == "live"
    assert ss._effort_override == "xhigh"
    tmux.send_keys.assert_awaited_once_with("/effort xhigh", enter=True)


@pytest.mark.asyncio
async def test_apply_effort_live_types_ultracode_verbatim(fast_settle) -> None:
    """Unlike the --effort flag, interactive /effort accepts the literal
    ultracode tier — the live apply must not resolve it to xhigh."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_capture("> \n"))
    result = await ss.apply_effort_live("ultracode")
    assert result == "live"
    tmux.send_keys.assert_awaited_once_with("/effort ultracode", enter=True)


@pytest.mark.asyncio
async def test_apply_effort_live_busy_defers() -> None:
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    _seed_inflight(ss)
    result = await ss.apply_effort_live("high")
    assert result == "deferred"
    assert ss._pending_live_effort == "high"
    assert ss._effort_override == "high"  # stash still lands
    tmux.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_effort_live_disconnected_pends_restart() -> None:
    ss, tmux = _make_session()  # UNINITIALIZED
    result = await ss.apply_effort_live("max")
    assert result == "pending_restart"
    assert ss._effort_override == "max"
    tmux.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_effort_live_confirms_dialog(fast_settle) -> None:
    """Mid-session /effort pops the 'Change effort level?' confirmation —
    the apply presses Enter to accept and reports live once it clears."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(
        side_effect=[
            _capture("Change effort level?\n> 1. Yes  2. No"),
            _capture("> \n"),
        ]
    )
    result = await ss.apply_effort_live("xhigh")
    assert result == "live"
    # /effort send + the Enter that confirmed the dialog.
    assert tmux.send_keys.await_count == 2
    assert tmux.send_keys.await_args_list[1].args == ("",)


@pytest.mark.asyncio
async def test_apply_effort_live_escapes_stuck_dialog(fast_settle) -> None:
    """If the dialog survives the confirm, Escape it (a modal left open
    would wedge the next prompt paste) and fall back to the relaunch path."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(
        return_value=_capture("Change effort level?\n> 1. Yes  2. No")
    )
    result = await ss.apply_effort_live("xhigh")
    assert result == "pending_restart"
    assert ss._effort_override == "xhigh"  # stash still applies on relaunch
    escape_calls = [
        c for c in tmux.send_keys.await_args_list if c.args == ("Escape",)
    ]
    assert len(escape_calls) == 1


@pytest.mark.asyncio
async def test_apply_effort_live_auto_reverts_to_default(fast_settle) -> None:
    """'auto' clears the override and pushes the agent DEFAULT live."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    ss._config.thinking_effort = "high"
    ss._effort_override = "xhigh"
    tmux.capture_pane = AsyncMock(return_value=_capture("> \n"))
    result = await ss.apply_effort_live("auto")
    assert result == "live"
    assert ss._effort_override is None
    tmux.send_keys.assert_awaited_once_with("/effort high", enter=True)


@pytest.mark.asyncio
async def test_turn_complete_consumes_pending_live_effort(fast_settle) -> None:
    """A deferred live effort is typed once the in-flight work drains."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_capture("> \n"))
    _seed_inflight(ss)
    ss._pending_live_effort = "xhigh"
    response = TurnResponse(text="done", stop_reason="stop_hook_summary")
    await ss._handle_turn_complete(response)
    # The apply runs as a fire-and-forget task — let it settle.
    for _ in range(10):
        await asyncio.sleep(0)
        if tmux.send_keys.await_count:
            break
    assert ss._pending_live_effort is None
    tmux.send_keys.assert_awaited_once_with("/effort xhigh", enter=True)


@pytest.mark.asyncio
async def test_apply_model_live_idle_types_command(fast_settle) -> None:
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_capture("> \n"))
    result = await ss.apply_model_live("claude-opus-4-8")
    assert result == "live"
    assert ss._config.model == "claude-opus-4-8"
    tmux.send_keys.assert_awaited_once_with("/model claude-opus-4-8", enter=True)


@pytest.mark.asyncio
async def test_apply_model_live_busy_pends_restart() -> None:
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    _seed_inflight(ss)
    result = await ss.apply_model_live("claude-opus-4-8")
    assert result == "pending_restart"
    # Config still updated → next relaunch boots with --model <new>.
    assert ss._config.model == "claude-opus-4-8"
    tmux.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_model_live_rejected_keeps_config(fast_settle) -> None:
    """A CLI 'unknown model' rejection must NOT update config — a
    relaunch with a bad --model flag would wedge the boot."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    old_model = ss._config.model
    tmux.capture_pane = AsyncMock(
        return_value=_capture("Unknown model: claude-bogus")
    )
    result = await ss.apply_model_live("claude-bogus")
    assert result == "rejected"
    assert ss._config.model == old_model


# Native ultracode activation arming (#151). A fresh cold-start with ultracode
# effort arms a one-shot so ``_deliver_turn`` types the interactive
# ``/effort ultracode`` (the CLI flag can't express it). A ``--continue``
# reconnect must NOT arm — it carries context where /effort trips the
# mid-session "Change effort level?" confirmation.
# ──────────────────────────────────────────────────────────────────────────


def test_build_claude_cmd_arms_native_ultracode_on_fresh_ultracode(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    ss._config.thinking_effort = "ultracode"
    cmd = ss._build_claude_cmd()
    # Fresh (no prior transcript) → no --continue, and the one-shot is armed.
    assert "--continue" not in cmd
    assert ss._native_ultracode_pending is True


def test_build_claude_cmd_does_not_arm_native_ultracode_on_continue(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    ss._config.thinking_effort = "ultracode"
    # Seed a prior transcript at the encoded-cwd path → use_continue=True.
    project_dir = ss._project_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "seed.jsonl").write_text("")
    cmd = ss._build_claude_cmd()
    assert "--continue" in cmd
    assert ss._native_ultracode_pending is False


def test_build_claude_cmd_does_not_arm_native_ultracode_for_non_ultracode(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    ss._config.thinking_effort = "xhigh"
    ss._build_claude_cmd()
    assert ss._native_ultracode_pending is False


# ──────────────────────────────────────────────────────────────────────────
# capture_pane signature: escapes flag for the read-only pane viewer
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_pane_default_omits_escapes_flag() -> None:
    """Existing callers that want plain text shouldn't suddenly start
    getting ANSI escapes back. Default ``escapes=False`` keeps the
    response pipeline's fallback capture clean."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.capture_pane(lines=50)
    assert "-e" not in calls[0]
    assert "-p" in calls[0]
    assert "-S" in calls[0]


@pytest.mark.asyncio
async def test_capture_pane_with_escapes_adds_e_flag() -> None:
    """The pane-viewer endpoint needs ANSI escapes preserved for xterm.js
    rendering. ``escapes=True`` adds ``-e`` to the tmux invocation."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.capture_pane(lines=200, escapes=True)
    assert "-e" in calls[0]


# ──────────────────────────────────────────────────────────────────────────
# CommandRunner seam (#149 phase-3): _run delegates exec to its runner
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_defaults_to_local_command_runner() -> None:
    """A _TmuxControl built without an explicit runner uses LocalCommandRunner
    — i.e. the prior verbatim behavior (daemon's own user)."""
    from pinky_daemon.command_runner import LocalCommandRunner

    tmux = _TmuxControl("pinky-test")
    assert isinstance(tmux._runner, LocalCommandRunner)


@pytest.mark.asyncio
async def test_run_delegates_built_argv_to_injected_runner() -> None:
    """_run hands the fully-built ``tmux …`` argv to its CommandRunner and
    decodes the bytes result into a TmuxCommandResult. This is the swap point
    a unix_user tenant uses (RunuserCommandRunner wraps the same argv)."""
    from pinky_daemon.command_runner import CommandResult, CommandRunner

    class _Recording(CommandRunner):
        def __init__(self):
            self.argv = None
            self.timeout = None

        async def run(self, argv, *, timeout=None, stdin=None):
            self.argv = argv
            self.timeout = timeout
            return CommandResult(0, b"out", b"err")

    rec = _Recording()
    tmux = _TmuxControl("pinky-test", socket_name="pinkysock", command_runner=rec)
    result = await tmux._run("has-session", "-t", "pinky-test", timeout=5.0)

    # Built argv includes the base cmd (binary + socket flag) then the args.
    assert rec.argv == ["tmux", "-L", "pinkysock", "has-session", "-t", "pinky-test"]
    assert rec.timeout == 5.0
    assert result.returncode == 0
    assert result.stdout == "out"  # bytes decoded
    assert result.stderr == "err"


@pytest.mark.asyncio
async def test_run_propagates_runner_timeout() -> None:
    """A runner that times out surfaces asyncio.TimeoutError to the caller,
    same as the prior inline behavior."""
    from pinky_daemon.command_runner import CommandRunner

    class _TimingOut(CommandRunner):
        async def run(self, argv, *, timeout=None, stdin=None):
            raise asyncio.TimeoutError

    tmux = _TmuxControl("pinky-test", command_runner=_TimingOut())
    with pytest.raises(asyncio.TimeoutError):
        await tmux._run("has-session")


# ──────────────────────────────────────────────────────────────────────────
# resize_window / resize_pane: viewer reshapes tmux pane to fit the modal
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resize_window_invokes_tmux_with_xy_flags() -> None:
    """``_TmuxControl.resize_window`` must call ``tmux resize-window``
    targeting the session with ``-x cols -y rows`` — that's what
    propagates the new geometry to the single pane (Claude Code's TUI
    then redraws on SIGWINCH)."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.resize_window(cols=180, rows=48)

    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "resize-window"
    assert "-t" in args and "pinky-test" in args
    assert "-x" in args and "180" in args
    assert "-y" in args and "48" in args


@pytest.mark.asyncio
async def test_resize_window_clamps_dims_into_safe_range() -> None:
    """Defensive clamping: callers come from the network (query params)
    so we don't trust them. Below-floor values clamp up to a Claude-
    Code-renderable size; above-ceiling values clamp down to a tmux-
    accepting size."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run

    # Below floor: cols<20 → 20, rows<10 → 10.
    await tmux.resize_window(cols=5, rows=2)
    assert "20" in calls[-1] and "10" in calls[-1]

    # Above ceiling: cols>500 → 500, rows>200 → 200.
    await tmux.resize_window(cols=9999, rows=9999)
    assert "500" in calls[-1] and "200" in calls[-1]


@pytest.mark.asyncio
async def test_resize_pane_returns_true_on_success() -> None:
    """Happy path: tmux returns 0, ``resize_pane`` returns ``True`` so
    the caller can log success / proceed."""
    session, tmux = _make_session()
    tmux.resize_window = AsyncMock(return_value=_ok())

    ok = await session.resize_pane(cols=200, rows=60)

    assert ok is True
    tmux.resize_window.assert_awaited_once_with(cols=200, rows=60)


@pytest.mark.asyncio
async def test_resize_pane_returns_false_on_tmux_failure() -> None:
    """tmux non-zero exit shouldn't bubble — the snapshot stream is
    more valuable than the resize. ``resize_pane`` returns ``False``
    and the stream continues (slightly mis-sized snapshot is better
    than aborting the modal)."""
    session, tmux = _make_session()
    tmux.resize_window = AsyncMock(
        return_value=_fail("can't find window")
    )

    ok = await session.resize_pane(cols=200, rows=60)

    assert ok is False


@pytest.mark.asyncio
async def test_resize_pane_swallows_subprocess_exceptions() -> None:
    """Defensive: any unexpected raise from the subprocess layer is
    logged + returns ``False`` instead of crashing the SSE generator
    (which would close the viewer mid-stream)."""
    session, tmux = _make_session()
    tmux.resize_window = AsyncMock(side_effect=RuntimeError("kaboom"))

    ok = await session.resize_pane(cols=120, rows=40)

    assert ok is False


# ──────────────────────────────────────────────────────────────────────────
# #514 — paste-buffer + delayed Enter for prompt delivery
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paste_text_sets_buffer_pastes_and_sends_enter() -> None:
    """``_TmuxControl.paste_text`` must invoke three tmux subcommands
    in order: ``set-buffer``, ``paste-buffer -p`` (bracketed paste),
    then ``send-keys Enter``. The bracketed-paste mode is what makes
    this reliable across the claude cold-start splash UI (#514).
    """
    tmux = _TmuxControl("pinky-test")

    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    result = await tmux.paste_text("hello world", enter_delay_ms=0)

    # Expect three commands in order.
    assert len(calls) == 3
    assert calls[0][:3] == ("set-buffer", "-b", "pinky-pinky-test")
    assert calls[0][3] == "hello world"
    assert "paste-buffer" in calls[1][0]
    assert "-p" in calls[1]  # bracketed paste mode
    assert "-d" in calls[1]  # delete buffer after paste
    assert calls[2] == ("send-keys", "-t", "pinky-test", "Enter")
    assert result.ok


@pytest.mark.asyncio
async def test_paste_text_skips_enter_when_enter_false() -> None:
    """``enter=False`` leaves the pasted text in claude's input buffer
    unsubmitted. Used by callers who want to stage a prompt without
    triggering a turn (e.g. internal setup, debugging)."""
    tmux = _TmuxControl("pinky-test")

    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.paste_text("hello", enter=False, enter_delay_ms=0)

    assert len(calls) == 2  # set-buffer + paste-buffer only
    assert not any("send-keys" in c[0] for c in calls)


@pytest.mark.asyncio
async def test_paste_text_short_circuits_on_set_buffer_failure() -> None:
    """If ``set-buffer`` fails (tmux server down, bad session name),
    paste_text returns the failure immediately without trying paste
    or Enter."""
    tmux = _TmuxControl("pinky-test")

    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _fail("set-buffer broke")

    tmux._run = fake_run
    result = await tmux.paste_text("hello", enter_delay_ms=0)

    assert len(calls) == 1
    assert not result.ok


@pytest.mark.asyncio
async def test_paste_text_short_circuits_on_paste_failure() -> None:
    """If ``paste-buffer`` fails after a successful ``set-buffer``,
    paste_text returns the failure without trying Enter. Skipping the
    Enter avoids submitting stale buffer content from a previous turn.
    """
    tmux = _TmuxControl("pinky-test")

    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        if args[0] == "paste-buffer":
            return _fail("paste broke")
        return _ok()

    tmux._run = fake_run
    result = await tmux.paste_text("hello", enter_delay_ms=0)

    assert len(calls) == 2  # set-buffer + paste-buffer; no send-keys
    assert not result.ok


@pytest.mark.asyncio
async def test_paste_text_waits_enter_delay_between_paste_and_enter() -> None:
    """The Enter delay between paste and Enter is the mechanism that
    lets claude's cold-start splash UI dismiss itself before the
    submit Enter arrives. Pinning so the sleep can't be accidentally
    removed during refactor.
    """
    tmux = _TmuxControl("pinky-test")
    sleep_durations: list[float] = []

    original_sleep = asyncio.sleep

    async def tracked_sleep(seconds):
        sleep_durations.append(seconds)
        # No-op the actual sleep so the test runs fast.
        await original_sleep(0)

    async def fake_run(*args, timeout=5.0):
        return _ok()

    tmux._run = fake_run
    # Patch asyncio.sleep IN the module under test, not globally.
    original = tmux_session.asyncio.sleep
    tmux_session.asyncio.sleep = tracked_sleep
    try:
        await tmux.paste_text("hello", enter_delay_ms=250)
    finally:
        tmux_session.asyncio.sleep = original

    assert 0.25 in sleep_durations


@pytest.mark.asyncio
async def test_deliver_turn_uses_paste_text_not_send_keys() -> None:
    """The worker's per-turn delivery must go through paste_text (not
    raw send-keys) so cold-start splash absorption (#514) is avoided.
    Pinning so a future refactor can't silently revert the delivery
    path."""
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_ok())
    ss, _ = _make_session(tmux=tmux)
    ss._state_machine._state = SessionState.CONNECTED

    turn = _QueuedTurn(
        prompt="hello dymok",
        platform="telegram",
        chat_id="123",
        message_id="m1",
    )
    await ss._deliver_turn(turn)

    tmux.paste_text.assert_awaited_once()
    args, kwargs = tmux.paste_text.call_args
    assert args[0] == "hello dymok" or kwargs.get("text") == "hello dymok"
    # And raw send_keys must NOT have been used for dispatch.
    tmux.send_keys.assert_not_awaited()


# Native ultracode activation delivery (#151). When armed, ``_deliver_turn``
# types ``/effort ultracode`` via send_keys BEFORE the prompt's paste_text, on
# the still-empty input area (no mid-session confirmation). One-shot +
# best-effort: fires once, and a send failure still pastes the prompt.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_turn_types_native_effort_before_paste_when_armed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._NATIVE_ULTRACODE_SETTLE_SEC", 0.0
    )
    tmux = _make_mock_tmux()
    ss, _ = _make_session(tmux=tmux)
    ss._state_machine._state = SessionState.CONNECTED
    ss._native_ultracode_pending = True
    ss._session_ready_event.set()  # input area live; skip the readiness wait

    # Record cross-mock call order so we can assert effort precedes prompt.
    order = MagicMock()
    order.attach_mock(tmux.send_keys, "send_keys")
    order.attach_mock(tmux.paste_text, "paste_text")

    turn = _QueuedTurn(
        prompt="hello dymok",
        platform="telegram",
        chat_id="123",
        message_id="m1",
    )
    await ss._deliver_turn(turn)

    tmux.send_keys.assert_awaited_once_with("/effort ultracode", enter=True)
    tmux.paste_text.assert_awaited_once()
    names = [c[0] for c in order.mock_calls]
    assert names.index("send_keys") < names.index("paste_text")
    # One-shot consumed.
    assert ss._native_ultracode_pending is False


@pytest.mark.asyncio
async def test_deliver_turn_native_effort_is_one_shot(monkeypatch) -> None:
    """Fires once per session. A second turn must not re-type /effort — by
    then context exists and it would hit the confirmation prompt."""
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._NATIVE_ULTRACODE_SETTLE_SEC", 0.0
    )
    tmux = _make_mock_tmux()
    ss, _ = _make_session(tmux=tmux)
    ss._state_machine._state = SessionState.CONNECTED
    ss._native_ultracode_pending = True
    ss._session_ready_event.set()

    t1 = _QueuedTurn(
        prompt="first", platform="telegram", chat_id="1", message_id="a"
    )
    t2 = _QueuedTurn(
        prompt="second", platform="telegram", chat_id="1", message_id="b"
    )
    await ss._deliver_turn(t1)
    await ss._deliver_turn(t2)

    assert tmux.send_keys.await_count == 1
    assert tmux.paste_text.await_count == 2


@pytest.mark.asyncio
async def test_deliver_turn_native_effort_send_failure_still_pastes(
    monkeypatch,
) -> None:
    """If the /effort keystroke send fails, delivery still pastes the prompt
    (degrade to the ULTRACODE_DIRECTIVE fallback, never block)."""
    monkeypatch.setattr(
        "pinky_daemon.tmux_session._NATIVE_ULTRACODE_SETTLE_SEC", 0.0
    )
    tmux = _make_mock_tmux()
    tmux.send_keys = AsyncMock(return_value=_fail("boom"))
    ss, _ = _make_session(tmux=tmux)
    ss._state_machine._state = SessionState.CONNECTED
    ss._native_ultracode_pending = True
    ss._session_ready_event.set()

    turn = _QueuedTurn(
        prompt="hello", platform="telegram", chat_id="1", message_id="m"
    )
    await ss._deliver_turn(turn)

    tmux.send_keys.assert_awaited_once()
    tmux.paste_text.assert_awaited_once()  # prompt still delivered
    assert ss._native_ultracode_pending is False


# ──────────────────────────────────────────────────────────────────────────
# REPL env propagation — #515 follow-up.
#
# Tmux ``new-session`` only propagates env via explicit ``-e KEY=VAL``;
# parent env is dropped (except the small ``update-environment``
# allowlist). Without explicit propagation, every PinkyBot-managed hook
# silently exits at ``if not secret: sys.exit(0)`` and the SessionStart
# tailer-repoint, Stop wake, presence updates, and effort-drift logs
# all stop working for tmux agents.
# ──────────────────────────────────────────────────────────────────────────


def test_build_repl_env_propagates_pinky_session_secret_when_set(
    monkeypatch,
) -> None:
    """When the daemon env has ``PINKY_SESSION_SECRET``, it must be
    included in the tmux env so the HMAC-signing hook scripts inside
    the tmux session can authenticate to the daemon."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "test-secret-32-bytes-min-xyz")
    ss, _ = _make_session()
    env = ss._build_repl_env()
    assert env.get("PINKY_SESSION_SECRET") == "test-secret-32-bytes-min-xyz"


def test_build_repl_env_omits_pinky_session_secret_when_unset(
    monkeypatch,
) -> None:
    """When the daemon env has no ``PINKY_SESSION_SECRET`` (dev-mode,
    misconfigured deploy), the env must NOT include an empty
    ``PINKY_SESSION_SECRET=``. Hooks already handle missing-secret
    gracefully (silent no-op); polluting tmux with an empty value
    risks future bugs where empty-string is treated as "present"."""
    monkeypatch.delenv("PINKY_SESSION_SECRET", raising=False)
    ss, _ = _make_session()
    env = ss._build_repl_env()
    assert "PINKY_SESSION_SECRET" not in env


def test_build_repl_env_strips_whitespace_in_pinky_session_secret(
    monkeypatch,
) -> None:
    """Whitespace-only env value is treated as unset. Defends against
    ``PINKY_SESSION_SECRET=" "`` accidentally passing the truthy guard
    while still failing HMAC verification on the daemon side."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "   ")
    ss, _ = _make_session()
    env = ss._build_repl_env()
    assert "PINKY_SESSION_SECRET" not in env


def test_build_repl_env_provisions_per_agent_key(monkeypatch) -> None:
    """#623 increment 2: the agent's per-agent signing key is injected as
    ``PINKY_AGENT_KEY`` so hooks in the tmux session sign with a
    non-forgeable identity (daemon dual-accepts)."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret-xyz")
    ss, _ = _make_session(agent_name="dymok")
    ss._registry = MagicMock()
    ss._registry.get_signing_key.return_value = "dymok-per-agent-key"
    # Non-isolated agent: dual-accept fallback retains the global secret.
    ss._registry.get.return_value.isolated = False
    # #638: a bare MagicMock auto-generates a truthy Mock for isolation_mode,
    # which the non-local coupling rightly treats as isolated — declare local.
    ss._registry.get.return_value.isolation_mode = "local"
    env = ss._build_repl_env()
    assert env.get("PINKY_AGENT_KEY") == "dymok-per-agent-key"
    # Global secret still propagated (dual-accept fallback for other paths).
    assert env.get("PINKY_SESSION_SECRET") == "global-secret-xyz"
    ss._registry.get_signing_key.assert_called_once_with("dymok")


def test_build_repl_env_isolated_withholds_global_secret(monkeypatch) -> None:
    """#149 phase-3 gate: an isolated agent that carries its own per-agent key
    must NOT receive the global PINKY_SESSION_SECRET (which the daemon accepts
    for any name → a forgery vector). It gets PINKY_AGENT_KEY only."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret-xyz")
    ss, _ = _make_session(agent_name="dymok")
    ss._registry = MagicMock()
    ss._registry.get_signing_key.return_value = "dymok-per-agent-key"
    ss._registry.get.return_value.isolated = True
    env = ss._build_repl_env()
    assert env.get("PINKY_AGENT_KEY") == "dymok-per-agent-key"
    assert "PINKY_SESSION_SECRET" not in env


def test_build_repl_env_isolated_without_key_withholds_secret(monkeypatch) -> None:
    """Fail CLOSED (Murzik #639 review): an isolated agent with NO per-agent
    key is a provisioning failure, not an availability case — withhold the
    global secret too (hooks/MCP no-op) rather than hand a sandbox the
    forgeable fleet-wide signing secret for the very window this gate closes."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret-xyz")
    ss, _ = _make_session(agent_name="dymok")
    ss._registry = MagicMock()
    ss._registry.get_signing_key.return_value = None
    ss._registry.get.return_value.isolated = True
    env = ss._build_repl_env()
    assert "PINKY_AGENT_KEY" not in env
    assert "PINKY_SESSION_SECRET" not in env


def test_build_repl_env_withholds_secret_when_isolation_unknown_but_key_present(
    monkeypatch,
) -> None:
    """Fail-open guard (Murzik #639 review): if a per-agent key resolves but the
    isolation lookup RAISES (status unknown), the global secret must still be
    withheld — registry uncertainty must not expose the forgeable fleet secret
    (same fail-open class as #635). The key already gives a working identity."""
    monkeypatch.setenv("PINKY_SESSION_SECRET", "global-secret-xyz")
    ss, _ = _make_session(agent_name="dymok")
    ss._registry = MagicMock()
    ss._registry.get_signing_key.return_value = "dymok-per-agent-key"
    ss._registry.get.side_effect = RuntimeError("db locked")
    env = ss._build_repl_env()
    assert env.get("PINKY_AGENT_KEY") == "dymok-per-agent-key"
    assert "PINKY_SESSION_SECRET" not in env


def test_build_repl_env_omits_agent_key_when_registry_absent() -> None:
    """No registry wired → no PINKY_AGENT_KEY (graceful degrade to global
    secret, which the daemon still accepts)."""
    ss, _ = _make_session()
    ss._registry = None
    env = ss._build_repl_env()
    assert "PINKY_AGENT_KEY" not in env


def test_build_repl_env_omits_agent_key_when_lookup_raises() -> None:
    """A registry hiccup must not break session env construction."""
    ss, _ = _make_session()
    ss._registry = MagicMock()
    ss._registry.get_signing_key.side_effect = RuntimeError("db locked")
    env = ss._build_repl_env()
    assert "PINKY_AGENT_KEY" not in env


def test_build_repl_env_omits_agent_key_when_none(monkeypatch) -> None:
    """Agent has no signing key yet (None) → no empty PINKY_AGENT_KEY."""
    ss, _ = _make_session()
    ss._registry = MagicMock()
    ss._registry.get_signing_key.return_value = None
    env = ss._build_repl_env()
    assert "PINKY_AGENT_KEY" not in env


# ──────────────────────────────────────────────────────────────────────────
# Concurrent cold-start race (PR6 framework: Case A + Case B)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_cold_start_runs_one_tmux_spawn() -> None:
    """PR6's canonical concurrent-connect race regression, applied to the
    greenfield tmux backend. Two concurrent connect() calls must result
    in exactly one tmux new-session.

    By the time the second caller enters connect(), state is BOOTING
    (the first caller flipped it at grant time). The widened guard
    (``state in {UNINITIALIZED, BOOTING}``) routes the second caller to
    the same-target in-flight branch — subscribes via InFlightHandle,
    inherits the owner's CONNECTED outcome.
    """
    tmux = _make_mock_tmux()
    release_spawn = asyncio.Event()
    spawn_started = asyncio.Event()
    spawn_count = 0

    async def blocking_new_session(*, cwd, command, env=None):
        nonlocal spawn_count
        spawn_count += 1
        spawn_started.set()
        await release_spawn.wait()
        return _ok()

    tmux.new_session = AsyncMock(side_effect=blocking_new_session)
    ss, _ = _make_session(tmux=tmux)

    t1 = asyncio.create_task(ss.connect())
    await spawn_started.wait()
    assert ss.state == SessionState.BOOTING, (
        "First caller must hold BOOTING ownership while spawn is in flight"
    )

    t2 = asyncio.create_task(ss.connect())
    # Yield to let t2 subscribe.
    for _ in range(10):
        await asyncio.sleep(0)

    assert spawn_count == 1, (
        f"Greenfield TmuxSession must inherit PR6's one-spawn invariant; "
        f"got {spawn_count} concurrent tmux new-session calls"
    )

    release_spawn.set()
    await asyncio.gather(t1, t2)

    assert spawn_count == 1
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_concurrent_cold_start_subscriber_raises_on_owner_dead() -> None:
    """Case B — owner's spawn raises, subscriber inherits DEAD and raises.

    Subscriber must NOT silently return as if connected (which would
    leave the broker thinking tmux is up when it isn't).
    """
    tmux = _make_mock_tmux()
    release_spawn = asyncio.Event()
    spawn_started = asyncio.Event()

    async def failing_new_session(*, cwd, command, env=None):
        spawn_started.set()
        await release_spawn.wait()
        return _fail("rc=1")

    tmux.new_session = AsyncMock(side_effect=failing_new_session)
    ss, _ = _make_session(tmux=tmux)

    t1 = asyncio.create_task(ss.connect())
    await spawn_started.wait()
    assert ss.state == SessionState.BOOTING

    t2 = asyncio.create_task(ss.connect())
    for _ in range(10):
        await asyncio.sleep(0)

    release_spawn.set()
    # Owner re-raises the original tmux failure; subscriber raises with
    # the "resolved to dead" marker.
    with pytest.raises(RuntimeError, match="tmux new-session failed"):
        await t1
    with pytest.raises(RuntimeError, match="resolved to dead"):
        await t2
    assert ss.state == SessionState.DEAD


# ──────────────────────────────────────────────────────────────────────────
# Cold-start Case D (post-DEAD rejection) — Pushok PR #495 round-1 nit 2
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_start_rejection_post_dead_raises() -> None:
    """Pushok's Case D from PR #494, applied to TmuxSession. A caller
    enters connect() observing state == BOOTING, but by the time
    request_transition acquires the lock the owner has already completed
    to DEAD. The matrix rejection branch (in_flight_handle is None) must
    surface the failure, not silently return as if connected.

    Surrogate test — pre-set state to BOOTING and patch request_transition
    to return rejection + DEAD state, matching the race outcome.
    """
    ss, _ = _make_session(state=SessionState.BOOTING)

    async def fake_request_transition(target, trigger, *, reason=None):
        # Simulate the race outcome: owner just completed DEAD; rejection.
        ss._state_machine._state = SessionState.DEAD
        return TransitionResult(
            changed=False,
            from_state=SessionState.DEAD,
            to_state=SessionState.DEAD,
            rejection_reason="phantom: owner completed DEAD before subscribe",
        )

    ss._state_machine.request_transition = fake_request_transition  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="post-DEAD"):
        await ss.connect()


# ──────────────────────────────────────────────────────────────────────────
# Warm-wake from IDLE_SLEEPING / DEAD — Murzik PR #495 round-1 finding 1
# ──────────────────────────────────────────────────────────────────────────
#
# Pre-fix: connect() only handled UNINITIALIZED/BOOTING. IDLE_SLEEPING and
# DEAD entries fell through to direct-mutating CONNECTED via the warm-
# reconnect else branch — skipping the matrix IDLE_SLEEPING|DEAD →
# RECONNECTING edge entirely, and giving concurrent wakes no subscriber
# protection.
#
# Post-fix: connect() takes a ``trigger`` parameter; IDLE_SLEEPING/DEAD
# entries drive ``→ RECONNECTING`` via the caller-supplied trigger
# (default BROKER — the most common caller: broker auto-wake on inbound).
# Same in-flight subscriber protection as cold-start.


@pytest.mark.asyncio
async def test_warm_wake_from_idle_sleeping_drives_through_reconnecting() -> None:
    """Auto-wake on inbound from IDLE_SLEEPING must drive
    IDLE_SLEEPING → RECONNECTING → CONNECTED, NOT direct-mutate CONNECTED.

    The matrix audit log captures every transition; pre-fix the
    IDLE_SLEEPING → RECONNECTING edge was invisible because the code
    skipped it entirely.
    """
    ss, tmux = _make_session(state=SessionState.IDLE_SLEEPING)
    await ss.connect()  # default trigger=BROKER

    assert ss.state == SessionState.CONNECTED
    # The new-session call confirms we ran the warm-wake spawn.
    tmux.new_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_warm_wake_from_dead_drives_through_reconnecting() -> None:
    """Same path from DEAD — the resurrection-on-inbound case.
    api._heartbeat_resurrect relies on this working; pre-fix it would
    silently bail because INTERNAL isn't legal for DEAD → RECONNECTING."""
    ss, _ = _make_session(state=SessionState.DEAD)
    await ss.connect(trigger=Trigger.BROKER)
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_warm_wake_failure_drives_to_dead() -> None:
    """If spawn fails during warm-wake, the in-flight transition completes
    DEAD via the emergency-exit path. State must NOT be left parked in
    RECONNECTING — that would strand subscribers + leak the in-flight
    record (driver-abandonment failure mode)."""
    tmux = _make_mock_tmux()
    tmux.new_session = AsyncMock(return_value=_fail("simulated wake failure"))
    ss, _ = _make_session(state=SessionState.IDLE_SLEEPING, tmux=tmux)

    with pytest.raises(RuntimeError, match="tmux new-session failed"):
        await ss.connect()
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_concurrent_warm_wake_runs_one_spawn() -> None:
    """Concurrent connect() on an IDLE_SLEEPING session must result in
    exactly one tmux spawn. Same shape as the cold-start Case A
    regression — caller A wins RECONNECTING ownership, caller B
    subscribes via the in-flight handle.

    Pre-fix: both callers fell through to ``_spawn_tmux_repl`` and
    direct-mutated CONNECTED — double-spawn, no subscriber protection.
    Post-fix: matrix subscriber path applies to warm-wake too.
    """
    tmux = _make_mock_tmux()
    release_spawn = asyncio.Event()
    spawn_started = asyncio.Event()
    spawn_count = 0

    async def blocking_new_session(*, cwd, command, env=None):
        nonlocal spawn_count
        spawn_count += 1
        spawn_started.set()
        await release_spawn.wait()
        return _ok()

    tmux.new_session = AsyncMock(side_effect=blocking_new_session)
    ss, _ = _make_session(state=SessionState.IDLE_SLEEPING, tmux=tmux)

    t1 = asyncio.create_task(ss.connect())
    await spawn_started.wait()
    assert ss.state == SessionState.RECONNECTING, (
        "First caller must hold RECONNECTING ownership while spawn is "
        "in flight (warm-wake path)"
    )

    t2 = asyncio.create_task(ss.connect())
    for _ in range(10):
        await asyncio.sleep(0)

    assert spawn_count == 1, (
        f"Warm-wake concurrent-connect must run exactly one tmux spawn; "
        f"got {spawn_count}"
    )

    release_spawn.set()
    await asyncio.gather(t1, t2)
    assert spawn_count == 1
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_warm_wake_uses_caller_supplied_trigger() -> None:
    """Trigger threads through to the matrix audit. WATCHDOG is the
    canonical resurrect-from-watchdog trigger; verify it's accepted.
    Matrix-legality is enforced by the state machine — this test pins
    that the call doesn't crash and lands CONNECTED."""
    ss, _ = _make_session(state=SessionState.DEAD)
    await ss.connect(trigger=Trigger.WATCHDOG)
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_connect_from_connected_is_no_op() -> None:
    """Post-completion straggler — connect() called while already
    CONNECTED returns silently. Pushok's Case C from PR #494, applied to
    TmuxSession. No double-spawn, no state mutation."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    await ss.connect()
    assert ss.state == SessionState.CONNECTED
    tmux.new_session.assert_not_awaited()


# ──────────────────────────────────────────────────────────────────────────
# attempt_reconnect with trigger awareness — Murzik PR #495 round-1 finding 2
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attempt_reconnect_from_dead_lands_connected_with_broker_trigger() -> None:
    """Murzik's finding 2: pre-fix attempt_reconnect used Trigger.INTERNAL
    unconditionally. INTERNAL is matrix-rejected from DEAD/IDLE_SLEEPING
    (only BROKER/WATCHDOG/SCHEDULER/API_ADMIN are legal for those edges),
    so a DEAD agent's reconnect would silently bail without ever retrying.

    Post-fix: caller-supplied trigger threads through. With BROKER (the
    default), DEAD → RECONNECTING → CONNECTED works."""
    ss, _ = _make_session(state=SessionState.DEAD)
    ss._RECONNECT_BACKOFF = (0,)  # speed up the test
    # Override module constant locally so the test doesn't sleep.
    import pinky_daemon.tmux_session as ts_mod
    original_backoff = ts_mod._RECONNECT_BACKOFF
    ts_mod._RECONNECT_BACKOFF = (0,)
    try:
        await ss.attempt_reconnect()  # default trigger=BROKER
        assert ss.state == SessionState.CONNECTED
    finally:
        ts_mod._RECONNECT_BACKOFF = original_backoff


@pytest.mark.asyncio
async def test_attempt_reconnect_from_idle_sleeping_lands_connected_with_watchdog_trigger() -> None:
    """Watchdog-driven warm-wake from IDLE_SLEEPING via attempt_reconnect.
    Pins the IDLE_SLEEPING → RECONNECTING edge with WATCHDOG trigger."""
    ss, _ = _make_session(state=SessionState.IDLE_SLEEPING)
    import pinky_daemon.tmux_session as ts_mod
    original_backoff = ts_mod._RECONNECT_BACKOFF
    ts_mod._RECONNECT_BACKOFF = (0,)
    try:
        await ss.attempt_reconnect(trigger=Trigger.WATCHDOG)
        assert ss.state == SessionState.CONNECTED
    finally:
        ts_mod._RECONNECT_BACKOFF = original_backoff


@pytest.mark.asyncio
async def test_attempt_reconnect_exhausted_budget_lands_dead() -> None:
    """If all retries fail, the in-flight transition completes DEAD."""
    tmux = _make_mock_tmux()
    tmux.new_session = AsyncMock(return_value=_fail("persistent failure"))
    ss, _ = _make_session(state=SessionState.DEAD, tmux=tmux)
    import pinky_daemon.tmux_session as ts_mod
    original_backoff = ts_mod._RECONNECT_BACKOFF
    ts_mod._RECONNECT_BACKOFF = (0, 0)  # 2 failed attempts, no sleep
    try:
        await ss.attempt_reconnect(trigger=Trigger.BROKER)
        assert ss.state == SessionState.DEAD
    finally:
        ts_mod._RECONNECT_BACKOFF = original_backoff


# ──────────────────────────────────────────────────────────────────────────
# disconnect + idle_sleep + force_restart choreography
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_from_connected_lands_in_dead() -> None:
    """Default disconnect (no prior intent set) lands CONNECTED → DEAD.
    Matches StreamingSession.disconnect's contract."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    await ss.disconnect()
    assert ss.state == SessionState.DEAD
    tmux.kill_session.assert_awaited()


@pytest.mark.asyncio
async def test_idle_sleep_drives_to_idle_sleeping_not_dead() -> None:
    """idle_sleep must pre-set IDLE_SLEEPING so disconnect's default
    CONNECTED → DEAD fallback doesn't fire. Otherwise the watchdog's
    resurrection callback would race the idle-sleep intent.

    Same flicker-class bug as Pushok's PR #492 Nit 2 on CodexSession.
    """
    ss, tmux = _make_session(state=SessionState.CONNECTED)

    # idle_sleep now enqueues a pre-sleep prompt via
    # ``_enqueue_internal_prompt(wait_for_completion=True, timeout_sec=120)``
    # before the state transition. This unit test doesn't run a real
    # message queue/worker/tailer, so the wait would block on the
    # 120s timeout. Stub the helper to a no-op (same pattern used by
    # ``TestIdleSleepPresavePrompt`` below) so we exercise only the
    # CONNECTED → IDLE_SLEEPING transition this test is pinning.
    async def _noop(prompt, *, reason, wait_for_completion=False, timeout_sec=None):
        return None

    ss._enqueue_internal_prompt = _noop

    result = await ss.idle_sleep()
    assert result is True
    assert ss.state == SessionState.IDLE_SLEEPING
    tmux.kill_session.assert_awaited()


@pytest.mark.asyncio
async def test_idle_sleep_returns_false_when_not_connected() -> None:
    ss, _ = _make_session(state=SessionState.DEAD)
    result = await ss.idle_sleep()
    assert result is False
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_force_restart_holds_reconnecting_across_disconnect_and_spawn() -> None:
    """Mirror of test_force_restart_holds_reconnecting_across_disconnect_and_connect
    from test_streaming_session.py. The macro state must stay RECONNECTING
    throughout the restart — no flicker through DEAD.
    """
    ss, tmux = _make_session(state=SessionState.CONNECTED)

    observed_states: list[SessionState] = []
    original_kill = tmux.kill_session

    async def kill_with_observation(*args, **kwargs):
        observed_states.append(ss.state)
        return await original_kill(*args, **kwargs)

    tmux.kill_session = AsyncMock(side_effect=kill_with_observation)

    result = await ss.force_restart()
    assert result is True
    # State at every observation point during the restart must be
    # RECONNECTING (or CONNECTED at the very end), never DEAD.
    for s in observed_states:
        assert s == SessionState.RECONNECTING, (
            f"force_restart must hold RECONNECTING across teardown — "
            f"observed {s}"
        )
    assert ss.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_force_restart_failure_lands_in_dead() -> None:
    """If the re-spawn fails after disconnect, force_restart returns False
    and the state machine lands DEAD."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    # First new_session call (post-restart spawn) fails.
    tmux.new_session = AsyncMock(return_value=_fail("re-spawn failed"))
    result = await ss.force_restart()
    assert result is False
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_force_restart_enqueues_resume_wake_prompt() -> None:
    """force_restart must re-prime the agent with an orientation wake
    prompt after respawn. Before this fix it respawned the REPL but —
    unlike connect() — never enqueued a wake prompt, leaving the agent
    on a blank session with no saved-state context (the "comes back
    idle / no anything" symptom). With a prior transcript present the
    reason is RESUME.
    """
    ss, _ = _make_session(state=SessionState.CONNECTED)
    ss._skip_wake_prompt_for_tests = False
    # Pin a prior transcript so the launch records had_prior=True → RESUME.
    ss._has_prior_transcript = lambda: True

    enqueued: list[tuple[str, bool]] = []

    async def _record(
        prompt,
        *,
        reason,
        wait_for_completion=False,
        timeout_sec=None,
        front=False,
        on_delivered=None,
    ):
        enqueued.append((reason, front))
        return None

    ss._enqueue_internal_prompt = _record

    result = await ss.force_restart()
    assert result is True
    assert ss.state == SessionState.CONNECTED
    wake = [e for e in enqueued if e[0].startswith("wake_")]
    assert len(wake) == 1, f"expected exactly one wake prompt, got {enqueued}"
    assert wake[0][0] == "wake_resume"
    # Must front-enqueue so it leads any watchdog-replayed backlog
    # (Murzik #589). See test_force_restart_wake_prompt_leads_backlog.
    assert wake[0][1] is True, "force_restart wake prompt must be front-enqueued"


@pytest.mark.asyncio
async def test_force_restart_skips_wake_prompt_under_test_seam() -> None:
    """The ``_skip_wake_prompt_for_tests`` seam must short-circuit the
    force_restart re-prime too, so existing force_restart unit tests
    (which don't simulate a tailer) don't hang the worker on a
    never-completing wake turn."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    # Default skip flag is True.
    ss._has_prior_transcript = lambda: True

    enqueued: list[str] = []

    async def _record(
        prompt,
        *,
        reason,
        wait_for_completion=False,
        timeout_sec=None,
        front=False,
        on_delivered=None,
    ):
        enqueued.append(reason)
        return None

    ss._enqueue_internal_prompt = _record

    result = await ss.force_restart()
    assert result is True
    assert not any(r.startswith("wake_") for r in enqueued)


@pytest.mark.asyncio
async def test_force_restart_wake_prompt_leads_backlog() -> None:
    """Murzik #589 review (blocker): the inflight watchdog requeues
    replay/backlog at the FRONT of _message_queue *before* scheduling
    force_restart. The re-prime wake prompt must still lead — otherwise
    the resumed REPL processes a user turn before ever seeing the
    saved-state/current-time orientation, defeating the exact recovery
    path this fix targets.

    This preloads the queue with a pending user turn (as the watchdog
    would have requeued), runs force_restart with a prior transcript,
    and asserts the wake prompt sits at the HEAD ahead of that backlog.
    The worker is stubbed to a no-op so the queue can be inspected in
    order rather than being drained mid-test.
    """
    ss, _ = _make_session(state=SessionState.CONNECTED)
    ss._skip_wake_prompt_for_tests = False
    ss._has_prior_transcript = lambda: True

    # A pending user turn already in the queue (watchdog-requeued backlog).
    ss._message_queue.put_nowait(
        _QueuedTurn(
            prompt="user backlog turn",
            platform="telegram",
            chat_id="c",
            message_id="m1",
            internal=False,
            reason="external",
        )
    )

    # Stub the worker to a no-op so force_restart doesn't drain the queue;
    # we inspect ordering directly.
    async def _noop_worker():
        return None

    ss._message_worker = _noop_worker

    result = await ss.force_restart()
    assert result is True
    assert ss.state == SessionState.CONNECTED

    drained: list[_QueuedTurn] = []
    while not ss._message_queue.empty():
        drained.append(ss._message_queue.get_nowait())

    reasons = [t.reason for t in drained]
    assert "wake_resume" in reasons, f"wake prompt missing: {reasons}"
    assert "external" in reasons, f"backlog turn missing: {reasons}"
    # The wake prompt must be delivered BEFORE the preexisting user turn.
    assert reasons.index("wake_resume") < reasons.index("external"), (
        f"wake_resume must lead the backlog, got order: {reasons}"
    )
    # Specifically, it must be at the very head.
    assert reasons[0] == "wake_resume", f"wake must be at queue head, got {reasons}"


@pytest.mark.asyncio
async def test_force_restart_skips_restart_guard_before_first_completed_turn() -> None:
    """A pre-first-turn tmux restart cannot lose completed work, so the
    persistence guard must not block watchdog recovery from a cold-start wedge.
    """
    guard = MagicMock(return_value={"restart_safe": False, "reason": "no save"})
    ss, tmux = _make_session(state=SessionState.CONNECTED, restart_guard=guard)

    result = await ss.force_restart()

    assert result is True
    assert ss.state == SessionState.CONNECTED
    guard.assert_not_called()
    tmux.kill_session.assert_awaited()


@pytest.mark.asyncio
async def test_force_restart_honors_restart_guard_after_completed_turn() -> None:
    """Once any turn has completed, force_restart keeps the existing
    persistence guard behavior to avoid dropping unsaved agent state.

    #518 retargeting note: assertion was ``send_keys.assert_awaited()``
    before #518 moved per-turn dispatch to ``paste_text`` (bracketed
    paste + delayed Enter for cold-start splash survival). Updated to
    pin the new contract; this test slipped through #518's PR-level
    CI as a rebase artifact and broke main, picked up here in #519.
    """
    guard = MagicMock(return_value={"restart_safe": False, "reason": "stale"})
    ss, tmux = _make_session(restart_guard=guard)
    await ss.connect()

    await ss.send(prompt="done", platform="t", chat_id="c", message_id="m")
    for _ in range(20):
        await asyncio.sleep(0)
        if tmux.paste_text.await_count >= 1:
            break
    tmux.paste_text.assert_awaited()

    await ss._handle_turn_complete(TurnResponse(text="ok", stop_reason="end_turn"))
    for _ in range(20):
        await asyncio.sleep(0)
        if ss._has_completed_turn:
            break
    assert ss._has_completed_turn is True

    result = await ss.force_restart()

    assert result is False
    assert ss.state == SessionState.CONNECTED
    guard.assert_called_once_with(ss)
    tmux.kill_session.assert_not_awaited()
    await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# send + worker
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_drops_when_not_connected() -> None:
    """Per Transport contract, send() drops when not CONNECTED. Matches
    StreamingSession's legacy drop-silent behavior."""
    ss, tmux = _make_session(state=SessionState.DEAD)
    await ss.send("hello", platform="telegram", chat_id="123")
    tmux.paste_text.assert_not_awaited()
    assert ss._stats["messages_sent"] == 0


@pytest.mark.asyncio
async def test_send_queues_and_worker_delivers_via_paste_text() -> None:
    """Happy path: cold-start, send a message, worker dequeues and
    pushes via tmux paste_text (bracketed paste + delayed Enter, the
    #514 fix). Pins the paste_text invocation shape (enter=True
    submits the prompt after the cold-start splash dismisses)."""
    ss, tmux = _make_session()
    await ss.connect()
    await ss.send("hello world", platform="telegram", chat_id="123")

    # Let the worker drain one item.
    for _ in range(20):
        await asyncio.sleep(0)
        if tmux.paste_text.await_count >= 1:
            break

    tmux.paste_text.assert_awaited()
    # paste_text is called with the prompt + enter=True (default).
    args, kwargs = tmux.paste_text.call_args
    assert args[0] == "hello world"
    assert kwargs.get("enter", True) is True

    # Clean up — cancel worker so the test doesn't leak the task.
    await ss.disconnect()


@pytest.mark.asyncio
async def test_send_increments_turn_and_message_counters() -> None:
    ss, tmux = _make_session()
    await ss.connect()
    await ss.send("first", platform="telegram", chat_id="123")
    for _ in range(20):
        await asyncio.sleep(0)
        if ss._stats["turns"] >= 1:
            break
    assert ss._stats["messages_sent"] == 1
    assert ss._stats["turns"] == 1
    await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# Effort knob (protocol parity; no in-session effect on tmux backend)
# ──────────────────────────────────────────────────────────────────────────


def test_set_effort_accepts_valid_levels() -> None:
    ss, _ = _make_session()
    for level in ("low", "medium", "high", "xhigh", "max", "auto"):
        ss.set_effort(level)


def test_set_effort_rejects_invalid_level() -> None:
    ss, _ = _make_session()
    with pytest.raises(ValueError, match="invalid effort"):
        ss.set_effort("nuclear")


def test_set_effort_auto_clears_override() -> None:
    ss, _ = _make_session()
    ss.set_effort("max")
    assert ss.effective_effort == "max"
    ss.set_effort("auto")
    # auto resolves to medium (default) per the contract.
    assert ss.effective_effort == "medium"


def test_clear_effort_override_resets_to_config_default() -> None:
    ss, _ = _make_session()
    ss.set_effort("max")
    ss.clear_effort_override()
    # Falls back to config's thinking_effort, defaulting to "medium".
    assert ss.effective_effort == "medium"


# ──────────────────────────────────────────────────────────────────────────
# stats shape
# ──────────────────────────────────────────────────────────────────────────


def test_stats_shape_matches_broker_consumer_keys() -> None:
    """Stats dict must include the keys broker/api/watchdog read.

    Intentionally absent: ``cost_usd`` — tmux billing is against the
    subscription, not per-turn metered. Documented gap.
    """
    ss, _ = _make_session(state=SessionState.CONNECTED)
    stats = ss.stats
    for key in ("turns", "messages_sent", "errors", "reconnects",
                "auto_restarts", "state", "thinking_effort",
                "current_thinking"):
        assert key in stats, f"stats missing required key: {key}"
    # state stringified for JSON-friendly transport over the API.
    assert stats["state"] == "connected"
    # cost_usd not reported.
    assert "cost_usd" not in stats


# ──────────────────────────────────────────────────────────────────────────
# PR8b — Response capture pipeline integration
# ──────────────────────────────────────────────────────────────────────────


class _AsyncCollector:
    """Drop-in async callback that records TurnResponse calls."""

    def __init__(self) -> None:
        self.calls: list[TurnResponse] = []

    async def __call__(self, response: TurnResponse):
        self.calls.append(response)


def _make_session_with_response_cb(
    *, response_cb=None, conv_store=None, stream_evt=None,
) -> tuple[TmuxSession, MagicMock]:
    """TmuxSession built with the response-side callbacks wired up."""
    cfg = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-session-test",
    )
    tmux = _make_mock_tmux()
    ss = TmuxSession(
        cfg,
        tmux_control=tmux,
        response_callback=response_cb,
        conversation_store=conv_store,
        stream_event_callback=stream_evt,
    )
    # Skip wake-prompt enqueue — see _make_session docstring. Tests that
    # specifically exercise wake-prompt behavior flip this back to False.
    ss._skip_wake_prompt_for_tests = True
    return ss, tmux


@pytest.mark.asyncio
async def test_connect_starts_tailer() -> None:
    """After cold-start, the tailer is constructed and running."""
    ss, _ = _make_session()
    await ss.connect()
    assert ss._tailer is not None
    assert ss._tailer.stats["running"] is True
    await ss.disconnect()


@pytest.mark.asyncio
async def test_disconnect_stops_tailer() -> None:
    """disconnect() cancels the tailer's background task."""
    ss, _ = _make_session()
    await ss.connect()
    tailer = ss._tailer
    assert tailer is not None
    await ss.disconnect()
    # Tailer instance preserved (so stats survive); but task is stopped.
    assert ss._tailer is tailer
    assert ss._tailer.stats["running"] is False


@pytest.mark.asyncio
async def test_deliver_turn_captures_inflight_meta() -> None:
    """_deliver_turn stashes routing metadata for the tailer's callback
    to forward through response_callback."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    turn = _QueuedTurn(
        prompt="hello",
        platform="telegram",
        chat_id="12345",
        message_id="m1",
    )
    await ss._deliver_turn(turn)
    assert ss._inflight_meta == {
        "platform": "telegram",
        "chat_id": "12345",
        "message_id": "m1",
    }


@pytest.mark.asyncio
async def test_deliver_turn_clears_meta_on_paste_text_failure() -> None:
    """If tmux paste_text fails, in-flight meta is cleared so a stale
    tail doesn't fire response_callback with bogus routing data."""
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    with pytest.raises(RuntimeError, match="tmux paste-buffer"):
        await ss._deliver_turn(turn)
    assert ss._inflight_meta == {}


@pytest.mark.asyncio
async def test_handle_turn_complete_fires_response_callback() -> None:
    """End-to-end: synthetic TurnResponse → response_callback called with
    correct unified routing payload."""
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    ss._state_machine._state = SessionState.CONNECTED
    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "12345",
        "message_id": "m1",
    }
    response = TurnResponse(
        text="hello back",
        stop_reason="end_turn",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    await ss._handle_turn_complete(response)

    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.agent_name == "dymok"
    assert result.session_id == ss.id
    assert result.response_text == "hello back"
    assert result.platform == "telegram"
    assert result.chat_id == "12345"
    assert result.message_id == "m1"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}
    # Meta cleared after firing — next turn starts clean.
    assert ss._inflight_meta == {}


@pytest.mark.asyncio
async def test_handle_turn_complete_skips_callback_for_empty_text() -> None:
    """Empty response with no tool activity doesn't fire the response_callback."""
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    response = TurnResponse(text="", stop_reason="tool_use")
    await ss._handle_turn_complete(response)
    assert cb.calls == []


@pytest.mark.asyncio
async def test_handle_turn_complete_fires_callback_for_tool_only_turn() -> None:
    """Tool-only turns still notify the broker so it can stop typing and
    suppress plain-text fallback when an outreach tool handled delivery.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "12345",
        "message_id": "m1",
    }
    response = TurnResponse(
        text="",
        stop_reason="tool_use",
        tool_uses=[
            {
                "name": "mcp__pinky-messaging__send",
                "input": {"chat_id": "12345", "text": "sent via tool"},
                "id": "toolu_1",
            }
        ],
    )
    await ss._handle_turn_complete(response)

    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.response_text == ""
    assert result.chat_id == "12345"
    assert result.used_outreach_tools is True


@pytest.mark.asyncio
async def test_handle_turn_complete_writes_to_conversation_store() -> None:
    """assistant response is appended to the conversation store."""
    conv = MagicMock()
    ss, _ = _make_session_with_response_cb(conv_store=conv)
    # #560: handler now requires an in-flight meta entry; seed one with
    # empty routing (this test only exercises the conversation_store side
    # effect, not the broker callback).
    _seed_inflight(ss)
    response = TurnResponse(text="response text", stop_reason="end_turn")
    await ss._handle_turn_complete(response)
    conv.append.assert_called_once_with(ss.id, "assistant", "response text")


@pytest.mark.asyncio
async def test_handle_turn_complete_stores_thinking_metadata() -> None:
    """Tmux thinking blocks persist in conversation metadata like SDK turns."""
    conv = MagicMock()
    ss, _ = _make_session_with_response_cb(conv_store=conv)
    _seed_inflight(ss)  # #560
    response = TurnResponse(
        text="response text",
        thinking="checked the transcript and tool state",
        stop_reason="end_turn",
    )

    await ss._handle_turn_complete(response)

    conv.append.assert_called_once_with(
        ss.id,
        "assistant",
        "response text",
        metadata={"thinking": ["checked the transcript and tool state"]},
    )
    assert ss.stats["current_thinking"] == ""


@pytest.mark.asyncio
async def test_handle_turn_complete_fires_stream_event() -> None:
    """stream_event_callback gets a turn_completed event with usage + duration."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    _seed_inflight(ss)  # #560
    response = TurnResponse(
        text="x", stop_reason="end_turn",
        usage={"input_tokens": 100, "output_tokens": 50},
        duration_ms=1500,
        assistant_entry_count=2,
        thinking="reasoned about the pane state",
        tool_uses=[{"name": "Bash", "input": {}, "id": "t1"}],
    )
    await ss._handle_turn_complete(response)
    # Two events fire per turn since task #95: a ``context_usage``
    # event with the cumulative token snapshot, then the existing
    # ``turn_completed`` event. The watchdog snapshot precedes the
    # turn-end so the chat UI can update its session-info card
    # *before* the thinking bubble clears.
    types = [e["type"] for e in events]
    assert types == ["context_usage", "turn_completed"]

    turn_evt = events[1]
    assert turn_evt["agent_name"] == "dymok"
    # Renamed from "turn_complete" to "turn_completed" for parity with
    # StreamingSession + CodexSession — Chat.svelte listens for the
    # -d suffix so the thinking-bubble clears at turn end.
    assert turn_evt["type"] == "turn_completed"
    assert turn_evt["stop_reason"] == "end_turn"
    assert turn_evt["duration_ms"] == 1500
    assert turn_evt["assistant_entry_count"] == 2
    assert turn_evt["tool_use_count"] == 1
    assert turn_evt["thinking_chars"] == len("reasoned about the pane state")
    assert turn_evt["thinking_block_count"] == 1


@pytest.mark.asyncio
async def test_handle_turn_complete_resets_live_activity_state() -> None:
    """Activity log + current activity must clear at turn end.

    Without this reset, the polling endpoint ``/streaming/status``
    keeps returning the previous turn's accumulated tool-call lines
    and Chat.svelte's thinking-bubble shows stale activity blending
    across turns (Brad's msg #7950 with screenshot, 2026-05-17)."""
    ss, _ = _make_session_with_response_cb()
    # Simulate mid-turn state: chips pushed by PreToolUse hook before
    # the model finished responding.
    ss._current_activity = "Bash — echo hi"
    ss._current_thinking = "working through the command output"
    ss._activity_log = ["ToolSearch", "Bash — echo test", "Bash — echo hi"]
    _seed_inflight(ss)  # #560
    response = TurnResponse(text="done", stop_reason="end_turn")

    await ss._handle_turn_complete(response)

    assert ss._current_activity == ""
    assert ss._current_thinking == ""
    assert ss._activity_log == []


@pytest.mark.asyncio
async def test_handle_turn_complete_autonomous_turn_clears_activity() -> None:
    """Empty-deque stop hooks (autonomous turns — background-task
    notifications, harness re-invocations with no daemon dispatch) must
    still clear live-activity state and emit ``turn_completed``.

    The empty-deque bail correctly skips the routing chain (no meta to
    synthesize), but before this fix it returned ABOVE the per-turn
    activity reset — Chat.svelte showed stale thinking dots + a frozen
    activity log after the agent stopped (Brad's msg #10938 screenshot,
    2026-06-11)."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._current_activity = "Bash — gh run watch 12345"
    ss._current_thinking = "waiting on CI"
    ss._activity_log = ["Bash — gh run watch 12345"]
    assert not ss._inflight_metas  # autonomous: nothing dispatched
    response = TurnResponse(text="", stop_reason="end_turn")

    await ss._handle_turn_complete(response)

    assert ss._current_activity == ""
    assert ss._current_thinking == ""
    assert ss._activity_log == []
    types = [e["type"] for e in events]
    assert types == ["turn_completed"]
    assert events[0]["autonomous"] is True


@pytest.mark.asyncio
async def test_handle_turn_complete_swallows_callback_exceptions() -> None:
    """A misbehaving response_callback must not strand the session.

    Critical because the tailer awaits this method; an unhandled raise
    would leak out and (in production) blow up the tail loop's exception
    handler, dropping subsequent turns."""
    async def bad_cb(*args, **kwargs):
        raise RuntimeError("downstream broke")

    bad_conv = MagicMock()
    bad_conv.append = MagicMock(side_effect=RuntimeError("store broke"))

    async def bad_stream(*args, **kwargs):
        raise RuntimeError("stream broke")

    ss, _ = _make_session_with_response_cb(
        response_cb=bad_cb,
        conv_store=bad_conv,
        stream_evt=bad_stream,
    )
    response = TurnResponse(text="x", stop_reason="end_turn")
    # All three callbacks raise; method must not.
    await ss._handle_turn_complete(response)
    # Meta still cleared (PR8b contract: clear at end regardless).
    assert ss._inflight_meta == {}


@pytest.mark.asyncio
async def test_notify_tail_wakes_tailer() -> None:
    """notify_tail() forwards to the tailer's wake() method."""
    ss, _ = _make_session()
    await ss.connect()
    assert ss._tailer is not None
    # Clear any latched wake from start().
    ss._tailer._wake_event.clear()
    ss.notify_tail()
    assert ss._tailer._wake_event.is_set()
    await ss.disconnect()


@pytest.mark.asyncio
async def test_notify_tail_safe_before_connect() -> None:
    """notify_tail() before tailer is constructed is a silent no-op."""
    ss, _ = _make_session()
    assert ss._tailer is None
    ss.notify_tail()  # must not raise


@pytest.mark.asyncio
async def test_set_transcript_path_forwards_to_tailer(tmp_path) -> None:
    """SessionStart hook reports a new path → tailer is repointed."""
    ss, _ = _make_session()
    await ss.connect()
    new_path = tmp_path / "new-session.jsonl"
    new_path.touch()
    ss.set_transcript_path(new_path)
    assert ss._tailer.transcript_path == new_path
    # Offset reset on rotation (per tailer contract).
    assert ss._tailer.offset == 0
    await ss.disconnect()


@pytest.mark.asyncio
async def test_set_transcript_path_safe_before_connect(tmp_path) -> None:
    """set_transcript_path before tailer exists is a silent no-op."""
    ss, _ = _make_session()
    # Don't connect — tailer is None.
    ss.set_transcript_path(tmp_path / "x.jsonl")  # must not raise
    assert ss._tailer is None


@pytest.mark.asyncio
async def test_end_to_end_tailer_to_response_callback(tmp_path) -> None:
    """Full integration: synthetic transcript file → tailer reads → turn
    complete → response_callback fires with full routing metadata.

    This exercises the real tailer (no mocks) but feeds it a synthetic
    transcript instead of a live claude REPL. Pins the entire PR8b
    contract end-to-end."""
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # Replace the tailer's path with our synthetic transcript.
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_text("")
    ss.set_transcript_path(transcript)

    # Simulate _deliver_turn capturing routing meta.
    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "777",
        "message_id": "m42",
    }

    # Write a synthetic turn to the transcript file.
    entries = [
        {
            "type": "user",
            "timestamp": "2026-05-14T05:00:00.000Z",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-05-14T05:00:00.100Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello there"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-14T05:00:00.500Z",
        },
    ]
    transcript.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    # Drive the tailer to read.
    await ss._tailer.read_once()

    # response_callback fired with the right unified payload.
    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.agent_name == "dymok"
    assert result.response_text == "hello there"
    assert result.platform == "telegram"
    assert result.chat_id == "777"
    assert result.message_id == "m42"

    await ss.disconnect()


@pytest.mark.asyncio
async def test_discover_transcript_path_returns_none_for_empty_project_dir(tmp_path, monkeypatch) -> None:
    """When the encoded-cwd project dir doesn't exist, return None.

    This is the cold-start case — agent has never been run, so Claude
    Code hasn't created the project dir yet. Tailer starts with a
    placeholder path and SessionStart hook later reports the canonical one."""
    # Point HOME at a tmp dir so the glob has nowhere to find anything.
    monkeypatch.setenv("HOME", str(tmp_path))
    ss, _ = _make_session()
    # working_dir is /tmp/tmux-session-test from the fixture; project dir
    # path under our fake HOME does not exist.
    assert ss._discover_transcript_path() is None


@pytest.mark.asyncio
async def test_set_transcript_path_from_placeholder_seeks_to_start(
    tmp_path, monkeypatch
) -> None:
    """Issue #563 — placeholder→real path transition must seek to byte 0.

    Cold-start sequence observed on Dymok:
    1. ``_start_tailer`` constructs the tailer with the cold-start
       placeholder (`/dev/null/no-transcript-yet`).
    2. Worker pastes the wake-action prompt; Claude Code writes its
       response + ``stop_hook_summary`` to the real JSONL.
    3. SessionStart hook fires AFTER the response is written and calls
       ``set_transcript_path(real_path)``.

    The pre-#563 default was to seek to ``stat().st_size`` (EOF) on
    path swap — that defends against the compact-resume reply-spam
    (#496 round-1 Case 3) BUT skips past every line CC already wrote
    for the wake-action turn, including the ``stop_hook_summary``. The
    deque head meta then stays unresolved, the watchdog ages it out
    at 600s, and the visible symptom is "Dymok restarted and re-sent
    messages" (4 observed instances on Dymok across log history).

    Fix: detect the placeholder source path and pass
    ``seek_to_start=True`` to the tailer. This test pre-seeds a
    complete turn into the JSONL BEFORE calling
    ``set_transcript_path``; under the buggy default-EOF behavior,
    ``read_once`` would observe nothing. Under the fix, it observes
    the stop_hook_summary and the response callback fires.
    """
    # Isolate HOME to a clean dir so _discover_transcript_path finds no
    # prior transcript for the fixture's working_dir and the tailer falls
    # through to the placeholder. Pre-fix this held only because the buggy
    # _project_dir double-dash never matched a real dir; the now-correct
    # encoder would otherwise discover a stray transcript under the real
    # ~/.claude/projects and start the tailer there instead.
    monkeypatch.setenv("HOME", str(tmp_path))
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()
    # Sanity: post-connect, tailer is on the placeholder (no prior transcript
    # in the test fixture's working_dir).
    from pinky_daemon.tmux_session import _PLACEHOLDER_TRANSCRIPT_PATH
    assert ss._tailer.transcript_path == _PLACEHOLDER_TRANSCRIPT_PATH

    # Simulate routing meta for an in-flight turn (the wake-action equivalent).
    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "777",
        "message_id": "mWake",
    }

    # CC has already written a full turn (response + stop_hook_summary)
    # by the time the SessionStart hook lands.
    transcript = tmp_path / "preexisting-content.jsonl"
    entries = [
        {
            "type": "user",
            "timestamp": "2026-05-20T15:59:18.000Z",
            "message": {"role": "user", "content": "wake action"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-05-20T15:59:44.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Replied on Telegram. Standing by."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-20T15:59:44.366Z",
        },
    ]
    transcript.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")
    assert transcript.stat().st_size > 0, "pre-condition: JSONL has content"

    # SessionStart hook fires AFTER the content was written.
    ss.set_transcript_path(transcript)
    assert ss._tailer.transcript_path == transcript
    assert ss._tailer.offset == 0, (
        "placeholder→real transition must seek to byte 0 — otherwise "
        "the pre-existing stop_hook_summary is skipped forever"
    )

    # Drive the tailer to read. Under the fix, this observes the turn
    # and fires the response callback; under the bug it sees nothing.
    await ss._tailer.read_once()
    assert len(cb.calls) == 1, (
        "stop_hook_summary written before set_transcript_path must still "
        "fire the response callback under the placeholder→real fix"
    )
    assert cb.calls[0].response_text == "Replied on Telegram. Standing by."

    await ss.disconnect()


@pytest.mark.asyncio
async def test_set_transcript_path_fresh_launch_old_real_to_new_real_seeks_to_start(tmp_path) -> None:
    """Issue #563 — fresh-launch case with prior history (Murzik review
    on PR #564 commit 1).

    ``force_fresh_context_once=True`` (e.g. ``context_restart``) means
    the launch is fresh even though prior transcripts exist. The
    ``_start_tailer`` discovery via mtime scan finds the OLD JSONL
    and seeks the tailer to its EOF. Then CC creates a NEW JSONL for
    this fresh session, writes the wake-action's first turn including
    ``stop_hook_summary``, and SessionStart hook lands AFTER that —
    same late-hook race as the placeholder case, just with an old-real
    starting point instead of the placeholder.

    Pre-#564-commit-2 the predicate was ``current path is the
    placeholder``, which missed this case. Post-fix the predicate is
    ``_tailer_first_bind_pending AND not _last_launch_used_continue``
    — both flavors of fresh-launch race now seek to byte 0 on the
    first hook bind. Continue launches still seek to EOF (#496
    defense unchanged).
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()
    # Simulate post-_start_tailer state: tailer bound to an OLD real
    # JSONL (discovered via mtime scan) + fresh-launch flags set.
    old_real = tmp_path / "old-history.jsonl"
    old_real.write_text(_json.dumps({
        "type": "system", "subtype": "stop_hook_summary",
        "timestamp": "2026-05-01T00:00:00.000Z",
    }) + "\n")
    ss._tailer._path = old_real
    ss._tailer._offset = old_real.stat().st_size  # seek-to-EOF of OLD
    ss._tailer_first_bind_pending = True
    ss._last_launch_used_continue = False  # fresh launch — explicit

    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "777",
        "message_id": "mWake",
    }

    # CC creates a NEW JSONL for the fresh session and writes the
    # wake-action's first turn including stop_hook_summary BEFORE the
    # hook lands.
    new_real = tmp_path / "fresh-session.jsonl"
    entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-20T16:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "fresh-session reply"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-20T16:00:00.500Z",
        },
    ]
    new_real.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    # SessionStart hook lands.
    ss.set_transcript_path(new_real)
    assert ss._tailer.transcript_path == new_real
    assert ss._tailer.offset == 0, (
        "fresh-launch old-real→new-real transition must seek to byte 0 — "
        "otherwise CC's pre-hook-write stop_hook_summary is skipped "
        "(same race as the placeholder case, just with a real old path)"
    )
    # First-bind flag must be consumed regardless of path change.
    assert ss._tailer_first_bind_pending is False

    await ss._tailer.read_once()
    assert len(cb.calls) == 1
    assert cb.calls[0].response_text == "fresh-session reply"

    await ss.disconnect()


@pytest.mark.asyncio
async def test_set_transcript_path_continue_launch_old_real_to_new_real_preserves_eof(tmp_path) -> None:
    """Issue #563 — continue-launch case must NOT seek to byte 0
    (Murzik review on PR #564 commit 1).

    For ``claude --continue`` launches the predicate must evaluate
    False so the seek-to-EOF default fires — even if a hook later
    rebinds to a different transcript that has historical turns.
    Replaying those would be exactly the #496 round-1 Case 3
    reply-spam scenario.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()
    # Simulate continue-launch state: tailer bound to the historical
    # JSONL (which is the canonical continued transcript), first-bind
    # pending, but ``_last_launch_used_continue=True``.
    historical = tmp_path / "historical.jsonl"
    historical.write_text(_json.dumps({
        "type": "system", "subtype": "stop_hook_summary",
        "timestamp": "2026-05-01T00:00:00.000Z",
    }) + "\n")
    ss._tailer._path = historical
    ss._tailer._offset = historical.stat().st_size
    ss._tailer_first_bind_pending = True
    ss._last_launch_used_continue = True  # continue launch

    # Hook reports a transcript with PRIOR content — under a buggy
    # seek-to-start, this would replay through the callback.
    other = tmp_path / "other-with-history.jsonl"
    historical_entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-10T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "old continued reply"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-10T10:00:00.500Z",
        },
    ]
    other.write_text("\n".join(_json.dumps(e) for e in historical_entries) + "\n")
    historical_size = other.stat().st_size

    ss.set_transcript_path(other)
    assert ss._tailer.transcript_path == other
    assert ss._tailer.offset == historical_size, (
        "continue-launch path swap must seek to EOF — #496 reply-spam "
        "defense applies regardless of first-bind state"
    )
    assert ss._tailer_first_bind_pending is False, (
        "first-bind flag must be consumed even when predicate evaluates False"
    )

    await ss._tailer.read_once()
    assert len(cb.calls) == 0, "historical turns must not replay on continue launch"

    await ss.disconnect()


# ── Issue #565 — delayed first-bind recovery for the bind-never-arrives
# ── case on a fresh launch with prior history.
# ──
# ── Setup invariant for these tests: PR #564 fixed the seek position
# ── for the case where SessionStart hook DOES arrive. #565 covers the
# ── separate case where the hook never arrives but the tailer is bound
# ── to a stale real path (so the existing #515 self-heal's
# ── ``self._path.exists()`` early-return blocks recovery forever).
@pytest.mark.asyncio
async def test_first_bind_recovery_fresh_with_prior_history_rebinds_and_seeks_to_start(
    tmp_path, monkeypatch,
) -> None:
    """Issue #565 — fresh launch with prior history + no explicit
    SessionStart bind must self-heal to the newer JSONL and seek to
    byte 0 so the wake-action turn's ``stop_hook_summary`` is observed.

    Reproduction shape:
      1. ``_start_tailer`` discovers an OLD real JSONL and seeks EOF
         (warm-wake protection from pre-#564 behavior).
      2. CC creates a NEW JSONL for this fresh session and writes its
         first turn (assistant + ``stop_hook_summary``).
      3. SessionStart hook never lands (dropped, env stripped, etc.).
      4. After ``_FIRST_BIND_RECOVERY_DELAY_SEC`` the scheduled
         recovery runs: re-discovers, sees a different newer path,
         routes through ``set_transcript_path`` → seeks to byte 0.

    Pre-#565 the tailer's existing self-heal returned early because
    the OLD path still existed on disk, so the new ``stop_hook_summary``
    was never observed and the head meta hung until the watchdog.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # Simulate post-_start_tailer state: tailer bound to an OLD real
    # JSONL via mtime-scan discovery + fresh-launch flags set.
    old_real = tmp_path / "old-history.jsonl"
    old_real.write_text(_json.dumps({
        "type": "system", "subtype": "stop_hook_summary",
        "timestamp": "2026-05-01T00:00:00.000Z",
    }) + "\n")
    ss._tailer._path = old_real
    ss._tailer._offset = old_real.stat().st_size  # seek-to-EOF of OLD
    ss._tailer_first_bind_pending = True
    ss._last_launch_used_continue = False  # fresh launch — explicit

    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "777",
        "message_id": "mWake",
    }

    # CC creates a NEW JSONL for the fresh session and writes the first
    # turn including ``stop_hook_summary`` — but the SessionStart hook
    # never lands. The delayed recovery is what must observe this.
    new_real = tmp_path / "fresh-session.jsonl"
    entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-20T16:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "recovered fresh reply"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-20T16:00:00.500Z",
        },
    ]
    new_real.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    # Stub discovery to return the new fresh path. Avoids needing a
    # full encoded-cwd project dir in the test fixture.
    monkeypatch.setattr(ss, "_discover_transcript_path", lambda: new_real)

    # Drive the recovery directly — bypasses the ``asyncio.sleep`` in
    # ``_delayed_first_bind_recovery`` so the test stays fast and
    # deterministic. The integration of sleep+attempt is exercised by
    # the recovery-task lifecycle tests below.
    ss._attempt_first_bind_recovery()

    assert ss._tailer.transcript_path == new_real, (
        "bind-never-arrives recovery must rebind to the discovered "
        "newer JSONL even though the old path still exists on disk"
    )
    assert ss._tailer.offset == 0, (
        "recovery must seek to byte 0 — otherwise CC's pre-hook-write "
        "stop_hook_summary is skipped (same race as the #563 cold-start "
        "case, just driven by recovery instead of the hook)"
    )
    assert ss._tailer_first_bind_pending is False, (
        "recovery must consume the first-bind flag so a late hook "
        "arrival doesn't double-trigger seek-to-start"
    )

    await ss._tailer.read_once()
    assert len(cb.calls) == 1
    assert cb.calls[0].response_text == "recovered fresh reply"

    await ss.disconnect()


@pytest.mark.asyncio
async def test_first_bind_recovery_continue_launch_noops_and_preserves_eof(
    tmp_path, monkeypatch,
) -> None:
    """Issue #565 — continue launches must NEVER trigger recovery.

    The recovery's whole motivation is the fresh-launch race. On a
    continue launch the tailer is correctly bound to the continued
    transcript and we explicitly want to seek to EOF (#496 round-1
    Case 3 reply-spam defense). If recovery fired here it would bind
    to whatever ``_discover_transcript_path`` returns and seek to
    byte 0 — replaying every historical turn.

    Asserts the predicate-False branch of ``_attempt_first_bind_recovery``.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # Continue-launch state: tailer bound to the continued JSONL with
    # prior history, seeked to EOF, first-bind flag still pending
    # (continue launches set the flag too — predicate-False is the
    # gate, not flag-False).
    continued = tmp_path / "continued.jsonl"
    historical_entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-10T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "old continued reply"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-10T10:00:00.500Z",
        },
    ]
    continued.write_text("\n".join(_json.dumps(e) for e in historical_entries) + "\n")
    continued_size = continued.stat().st_size
    ss._tailer._path = continued
    ss._tailer._offset = continued_size  # EOF
    ss._tailer_first_bind_pending = True
    ss._last_launch_used_continue = True  # continue — predicate False

    # Even though discovery would return a *different* fresh path, the
    # predicate guards against acting on it for continue launches.
    other = tmp_path / "other-fresh.jsonl"
    other.write_text(_json.dumps({
        "type": "system", "subtype": "stop_hook_summary",
        "timestamp": "2026-05-20T16:00:00.000Z",
    }) + "\n")
    monkeypatch.setattr(ss, "_discover_transcript_path", lambda: other)

    ss._attempt_first_bind_recovery()

    assert ss._tailer.transcript_path == continued, (
        "continue-launch recovery must no-op — rebinding would break "
        "the #496 reply-spam defense"
    )
    assert ss._tailer.offset == continued_size, (
        "offset must remain at EOF — recovery must not seek to byte 0 "
        "on continue launches"
    )
    assert ss._tailer_first_bind_pending is True, (
        "first-bind flag must NOT be consumed when recovery no-ops — "
        "an explicit hook arrival can still take the seek-to-EOF path"
    )

    await ss._tailer.read_once()
    assert len(cb.calls) == 0, "historical turns must not replay on continue launch"

    await ss.disconnect()


@pytest.mark.asyncio
async def test_first_bind_recovery_noops_when_flag_already_consumed(
    tmp_path, monkeypatch,
) -> None:
    """Issue #565 — recovery must no-op when an explicit
    ``set_transcript_path`` already consumed the first-bind flag.

    The realistic shape: SessionStart hook arrives within the deadline
    → ``_tailer_first_bind_pending`` flips False → the still-scheduled
    recovery task wakes and the attempt method sees the consumed flag
    and returns without doing anything.

    Without this guard the recovery could rebind a second time after
    the hook already handled things — potentially redirecting to a
    different newest JSONL if CC rotated transcripts during the
    deadline window.
    """
    ss, _ = _make_session_with_response_cb()
    await ss.connect()

    real = tmp_path / "real.jsonl"
    real.write_text("")
    ss._tailer._path = real
    ss._tailer._offset = 0
    ss._tailer_first_bind_pending = False  # explicit hook already ran
    ss._last_launch_used_continue = False  # fresh launch

    # Discovery returns a *different* path — but recovery must ignore
    # it because the flag is already consumed.
    other = tmp_path / "newer.jsonl"
    other.write_text("")
    monkeypatch.setattr(ss, "_discover_transcript_path", lambda: other)

    ss._attempt_first_bind_recovery()

    assert ss._tailer.transcript_path == real, (
        "consumed-flag guard must prevent recovery from rebinding"
    )

    await ss.disconnect()


@pytest.mark.asyncio
async def test_first_bind_recovery_noops_when_discovery_returns_same_path(
    tmp_path, monkeypatch,
) -> None:
    """Issue #565 — recovery must no-op (no log spam, no seek reset)
    when discovery returns the path the tailer is already bound to.

    This is the common case when the SessionStart hook arrives at the
    same time discovery would have found the right path (CC just
    happens to have its newest JSONL be the one the tailer already
    points at). The attempt should detect the same-path case and
    return without going through ``set_transcript_path``.
    """
    ss, _ = _make_session_with_response_cb()
    await ss.connect()

    current = tmp_path / "current.jsonl"
    current.write_text("")
    ss._tailer._path = current
    ss._tailer._offset = 1234  # arbitrary non-zero, must stay put
    ss._tailer_first_bind_pending = True
    ss._last_launch_used_continue = False

    monkeypatch.setattr(ss, "_discover_transcript_path", lambda: current)

    ss._attempt_first_bind_recovery()

    assert ss._tailer.transcript_path == current
    assert ss._tailer.offset == 1234, "same-path recovery must not reset offset"
    assert ss._tailer_first_bind_pending is True, (
        "same-path no-op must not consume the first-bind flag — "
        "the real bind hasn't happened yet"
    )

    await ss.disconnect()


@pytest.mark.asyncio
async def test_first_bind_recovery_noops_when_discovery_returns_none(
    tmp_path, monkeypatch,
) -> None:
    """Issue #565 — recovery must no-op when discovery finds nothing.

    Cold-start with no transcripts on disk at all. The tailer should
    keep waiting for the hook; rebinding to None would be a regression.
    """
    ss, _ = _make_session_with_response_cb()
    await ss.connect()

    placeholder_like = tmp_path / "placeholder-like.jsonl"
    placeholder_like.write_text("")
    ss._tailer._path = placeholder_like
    ss._tailer._offset = 0
    ss._tailer_first_bind_pending = True
    ss._last_launch_used_continue = False

    monkeypatch.setattr(ss, "_discover_transcript_path", lambda: None)

    ss._attempt_first_bind_recovery()

    assert ss._tailer.transcript_path == placeholder_like
    assert ss._tailer_first_bind_pending is True

    await ss.disconnect()


@pytest.mark.asyncio
async def test_start_tailer_schedules_recovery_task() -> None:
    """Issue #565 — ``_start_tailer`` must schedule a recovery task so
    the bind-never-arrives gap is actually closed (not just covered by
    a method nobody calls).
    """
    ss, _ = _make_session_with_response_cb()
    await ss.connect()
    assert ss._first_bind_recovery_task is not None
    assert not ss._first_bind_recovery_task.done(), (
        "recovery task must be live until the deadline or cancellation"
    )
    await ss.disconnect()


@pytest.mark.asyncio
async def test_stop_tailer_cancels_pending_recovery_task() -> None:
    """Issue #565 — ``_stop_tailer`` must cancel the pending recovery
    task so a torn-down session doesn't have a stray timer firing
    ``set_transcript_path`` against a stopped tailer.
    """
    ss, _ = _make_session_with_response_cb()
    await ss.connect()
    task = ss._first_bind_recovery_task
    assert task is not None
    await ss.disconnect()
    # ``disconnect`` → ``_stop_tailer`` cancels + clears the handle.
    assert ss._first_bind_recovery_task is None
    # Give the cancellation a moment to propagate; the task should be
    # done (cancelled) by now.
    await asyncio.sleep(0)
    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_start_tailer_rearms_first_bind_state_on_retained_instance_respawn(
    tmp_path,
) -> None:
    """Issue #565 — Murzik's PR #566 round-1 finding: per-spawn first-
    bind state must be re-armed on every ``_start_tailer`` call,
    including the retained-instance respawn path (force_restart /
    attempt_reconnect).

    Pre-fix shape: ``_start_tailer`` early-returned when
    ``self._tailer is not None`` and never re-armed
    ``_tailer_first_bind_pending`` nor (re)scheduled the recovery
    task. Result: any second-or-later fresh-launch spawn silently
    lost both PR #564's first-bind seek-to-start AND PR #566's
    delayed recovery for the rest of its lifetime.

    This test exercises the bug pattern: consume the flag via an
    explicit bind, stop the tailer (instance retained per #496
    round-3 Case 2''), restart the tailer, and assert the per-spawn
    arming ran.
    """
    ss, _ = _make_session_with_response_cb()
    await ss.connect()
    # Sanity: cold-start arming worked.
    assert ss._tailer_first_bind_pending is True
    initial_recovery_task = ss._first_bind_recovery_task
    assert initial_recovery_task is not None
    tailer_instance_id_before = id(ss._tailer)

    # Simulate the SessionStart hook arriving and consuming the flag.
    # ``_stop_tailer`` will tear down the task but keep the tailer
    # instance so the next spawn can resume from its last path.
    fake_path = tmp_path / "retained-instance.jsonl"
    fake_path.write_text("")
    ss.set_transcript_path(fake_path)
    assert ss._tailer_first_bind_pending is False, (
        "explicit bind must consume the flag — sanity check before "
        "exercising the respawn re-arm"
    )

    # Tear down and restart, retaining the instance (the contract
    # ``_stop_tailer``'s docstring describes for #496 Case 2'' and
    # the force_restart flow Pushok wired up).
    await ss._stop_tailer()
    # Sanity: the recovery task was cancelled by _stop_tailer.
    assert ss._first_bind_recovery_task is None

    # Mark this spawn as fresh — the predicate that gates the
    # delayed recovery depends on ``_last_launch_used_continue``.
    ss._last_launch_used_continue = False
    await ss._start_tailer()

    # Critical invariants for the fix:
    assert id(ss._tailer) == tailer_instance_id_before, (
        "tailer instance must be retained across stop/start — this is "
        "the bug-reproducing path; if the instance is fresh, the test "
        "isn't exercising the retained-instance scenario"
    )
    assert ss._tailer_first_bind_pending is True, (
        "per-spawn arming must reset _tailer_first_bind_pending so the "
        "next set_transcript_path or the #565 delayed recovery seeks "
        "to byte 0 (Murzik PR #566 round-1 finding)"
    )
    assert ss._first_bind_recovery_task is not None, (
        "per-spawn arming must (re)schedule the #565 recovery task on "
        "respawn — without this the bind-never-arrives gap reopens "
        "for the second and later spawns"
    )
    assert not ss._first_bind_recovery_task.done(), (
        "the freshly scheduled recovery task must still be live "
        "(it will only fire after ``_FIRST_BIND_RECOVERY_DELAY_SEC``)"
    )

    await ss.disconnect()


@pytest.mark.asyncio
async def test_first_bind_recovery_after_retained_instance_respawn_rebinds_and_seeks(
    tmp_path, monkeypatch,
) -> None:
    """Issue #565 — stronger variant of the retained-instance respawn
    test (Murzik's PR #566 round-1 stronger-version suggestion):
    after the respawn, simulate the bind-never-arrives shape with a
    new JSONL on disk and assert the recovery actually rebinds +
    seeks to 0.

    This is the end-to-end version of what the previous test pins
    structurally. Together they prevent regression of both:
      - the per-spawn arming itself, and
      - the arming actually producing a working recovery on the
        second spawn (e.g. a future refactor could re-arm the flag
        but forget to schedule the task, or vice versa).
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # First spawn: consume the flag via an explicit bind, then stop
    # the tailer (retaining the instance).
    first_real = tmp_path / "first.jsonl"
    first_real.write_text("")
    ss.set_transcript_path(first_real)
    assert ss._tailer_first_bind_pending is False
    await ss._stop_tailer()

    # Respawn — force_fresh shape: the previous tailer was bound to
    # ``first_real`` (still on disk), the new REPL will write to
    # ``second_real``. The SessionStart hook never lands; only the
    # #565 delayed recovery saves us.
    ss._last_launch_used_continue = False
    await ss._start_tailer()
    # Sanity: the retained tailer is still on ``first_real``.
    assert ss._tailer.transcript_path == first_real

    # Simulate the in-flight wake-action turn meta the way #563/#564
    # tests do — so the recovery's read_once will fire the response
    # callback rather than swallow the entry as unrouted.
    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "777",
        "message_id": "m_post_respawn",
    }

    # Newer real JSONL exists on disk and already has a complete turn
    # written by Claude Code before any hook would have landed.
    second_real = tmp_path / "second.jsonl"
    entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-20T17:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "post-respawn reply"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-20T17:00:00.500Z",
        },
    ]
    second_real.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    monkeypatch.setattr(ss, "_discover_transcript_path", lambda: second_real)

    # Drive the recovery directly (the same trick the other #565
    # tests use to skip the timer).
    ss._attempt_first_bind_recovery()

    assert ss._tailer.transcript_path == second_real, (
        "post-respawn recovery must rebind to the newer JSONL — this "
        "is the regression Murzik's PR #566 round-1 finding warned "
        "about: pre-fix the recovery wasn't even scheduled on the "
        "second spawn, so this rebind never happens"
    )
    assert ss._tailer.offset == 0
    assert ss._tailer_first_bind_pending is False

    await ss._tailer.read_once()
    assert len(cb.calls) == 1
    assert cb.calls[0].response_text == "post-respawn reply"

    await ss.disconnect()


@pytest.mark.asyncio
async def test_set_transcript_path_real_to_real_preserves_seek_to_eof(tmp_path) -> None:
    """Issue #563 fix must NOT break the compact-resume defense from
    #496 round-1 Case 3. Real→real path swap (e.g. compact-resume
    binds a fresh transcript while the same agent session continues)
    must still seek to EOF so historical turns in the new transcript
    don't replay through the response callback.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # First real path — empty, swap to it (placeholder→real transition,
    # seeks to start, but file is empty so offset stays 0).
    first_real = tmp_path / "first.jsonl"
    first_real.write_text("")
    ss.set_transcript_path(first_real)
    assert ss._tailer.transcript_path == first_real

    # Second real path — has prior turns already (simulating compact-resume
    # binding to a transcript with historical content). The real→real swap
    # must seek to EOF so we don't replay them.
    second_real = tmp_path / "second.jsonl"
    historical_entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-19T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "old reply 1"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-19T10:00:00.500Z",
        },
        {
            "type": "assistant",
            "timestamp": "2026-05-19T10:01:00.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "old reply 2"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-19T10:01:00.500Z",
        },
    ]
    second_real.write_text("\n".join(_json.dumps(e) for e in historical_entries) + "\n")
    historical_size = second_real.stat().st_size

    ss.set_transcript_path(second_real)
    assert ss._tailer.transcript_path == second_real
    assert ss._tailer.offset == historical_size, (
        "real→real swap must seek to EOF (#496 reply-spam defense) — "
        "historical stop_hooks in the new transcript must NOT re-fire"
    )

    # Read confirms no historical replay.
    await ss._tailer.read_once()
    assert len(cb.calls) == 0, "historical turns must not replay on real→real swap"

    await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# PR8b round 2 — Pushok's review fixes
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_done_set_unconditionally_for_empty_text_turn() -> None:
    """Pushok's PR #496 round-1 Case 1 follow-up: a turn that produces
    zero assistant text (pure tool-use that hit max_tokens, or refusal)
    must still set ``_turn_done`` so the worker can dispatch the next
    prompt. If turn_done were gated on ``response.text``, the worker
    would deadlock forever on tool-use-only turns.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    # Pre-clear turn_done to mimic what _deliver_turn does.
    ss._turn_done.clear()
    assert not ss._turn_done.is_set()
    # #560: handler now requires an in-flight meta entry. The contract
    # this test pins (empty-text turn still sets turn_done) is preserved
    # because the critical-section block at the top of the handler sets
    # turn_done before any text/tool gating.
    _seed_inflight(ss)

    # Empty-text turn — pure tool-use refusal, max_tokens, etc.
    empty_response = TurnResponse(text="", stop_reason="max_tokens")
    await ss._handle_turn_complete(empty_response)

    # response_callback NOT fired (empty text), but turn_done IS set.
    assert cb.calls == []
    assert ss._turn_done.is_set(), (
        "turn_done must be set unconditionally so the worker can proceed"
    )


@pytest.mark.asyncio
async def test_deliver_turn_clears_turn_done_before_paste_text() -> None:
    """The clear must happen BEFORE paste_text so that any subsequent
    stop_hook_summary unambiguously belongs to THIS turn (not a stale
    pre-arm from a prior callback). Pinned via call-order observation.
    """
    tmux = _make_mock_tmux()
    cleared_at: list[bool] = []

    async def observing_paste(*args, **kwargs):
        # Snapshot turn_done state at the moment paste_text is called.
        cleared_at.append(not ss._turn_done.is_set())
        return _ok()

    tmux.paste_text = AsyncMock(side_effect=observing_paste)
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    # Pre-arm turn_done to a SET state to prove the clear() actually fires.
    ss._turn_done.set()

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    await ss._deliver_turn(turn)

    assert cleared_at == [True], (
        "turn_done must be CLEARED at the moment paste_text is invoked"
    )


@pytest.mark.asyncio
async def test_deliver_turn_paste_text_failure_re_arms_turn_done() -> None:
    """If paste_text fails, the worker would otherwise block forever on
    turn_done.wait() because no callback will ever fire for the failed
    dispatch. _deliver_turn must re-arm turn_done as part of its failure
    cleanup so the worker's next iteration starts in a clean state.
    """
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    ss._turn_done.clear()

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m")
    with pytest.raises(RuntimeError):
        await ss._deliver_turn(turn)

    # Meta cleared AND turn_done re-armed.
    assert ss._inflight_meta == {}
    assert ss._turn_done.is_set()


@pytest.mark.asyncio
async def test_deliver_turn_dead_pane_schedules_disconnect_and_worker_exits() -> None:
    """Task #90: when paste_text fails because the tmux pane is gone
    (external kill, tmux server crash), _deliver_turn must schedule a
    disconnect so the session transitions CONNECTED → DEAD. The worker
    must exit cleanly on the resulting RuntimeError (rather than
    looping forever pasting into the missing pane). After this, a
    follow-up send() must be dropped per the not-CONNECTED contract
    — the next inbound message will cold-start a fresh pane via the
    auto-wake path validated in #517/#518/#519.
    """
    tmux = _make_mock_tmux()
    # Mimic tmux's exact stderr shape when the target session/pane is gone.
    tmux.paste_text = AsyncMock(
        return_value=_fail("can't find pane: pinky-dymok")
    )
    ss, _ = _make_session(tmux=tmux)
    await ss.connect()
    assert ss.state == SessionState.CONNECTED
    worker_task = ss._worker_task
    assert worker_task is not None

    # Queue one turn — worker will pick it up, paste_text fails with
    # dead-pane stderr, disconnect gets scheduled, worker exits.
    await ss.send("hi", platform="telegram", chat_id="123", message_id="m1")

    # Wait for state to transition to DEAD (disconnect runs as a
    # background task scheduled via create_task).
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ss.state == SessionState.DEAD and worker_task.done():
            break

    assert ss.state == SessionState.DEAD, (
        f"expected DEAD after dead-pane detect, got {ss.state.value}"
    )
    assert worker_task.done(), "worker must exit cleanly on dead-pane"

    # Follow-up send is dropped because state != CONNECTED (matches the
    # existing "drop with log line" behavior at the top of send()).
    paste_count_before = tmux.paste_text.await_count
    await ss.send("again", platform="telegram", chat_id="123", message_id="m2")
    assert tmux.paste_text.await_count == paste_count_before, (
        "send() must drop while not CONNECTED — no additional paste_text "
        "calls into the dead pane"
    )


# ──────────────────────────────────────────────────────────────────────────
# Paste-time safety: context-lock check (#522 + #525)
# ──────────────────────────────────────────────────────────────────────────
# Issue #525 removed the pane-scraping idle-prompt readiness gate (#522 +
# #524). It waited for a pane signal (bare ``❯``) that Claude Code's splash
# never produces and killed every cold start. Splash-state paste is
# handled by ``_TmuxControl.paste_text`` (bracketed-paste + delayed Enter,
# commit 0864f4e / issue #514): the splash dismisses on input focus.
# Context-lock check is the only remaining paste-time gate.


@pytest.mark.asyncio
async def test_deliver_turn_pastes_immediately_on_cold_repl() -> None:
    """Issue #525: cold-start paste must NOT block on any pane-state
    readiness check. The original gate (#522) waited for a bare ``❯``
    that Claude Code's splash never produces, deadlocking cold starts.
    First turn after spawn pastes directly — splash-state paste is
    handled by ``paste_text`` (splash dismisses on input focus).
    """
    tmux = _make_mock_tmux()
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    assert ss._has_completed_turn is False

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m1")
    await ss._deliver_turn(turn)

    # Paste must fire immediately, no readiness polling involved.
    tmux.paste_text.assert_awaited_once()
    assert not hasattr(tmux, "wait_for_idle_prompt") or \
        not tmux.wait_for_idle_prompt.await_count


@pytest.mark.asyncio
async def test_deliver_turn_skips_paste_when_context_locked(
    monkeypatch, tmp_path
) -> None:
    """Pulse-v2 port: if the daemon-level context manager has touched
    the agent's transport-lock file, ``_deliver_turn`` must raise a
    typed ``_ContextLockDeferral`` BEFORE paste_text so the worker
    preserves the inflight turn and re-pastes the SAME prompt on the
    next iteration once the lock is released (worker-level retry
    behavior pinned separately in
    ``test_context_lock_preserves_turn_until_released``).
    """
    # Point the lock dir at a tmp path so the test can't escape the
    # sandbox or collide with a real lock.
    monkeypatch.setattr(tmux_session, "_TRANSPORT_LOCK_DIR", tmp_path)
    tmux = _make_mock_tmux()
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)

    # Touch the lock for this agent.
    lock_path = tmp_path / f"{ss.agent_name}.lock"
    lock_path.write_text("")

    turn = _QueuedTurn(prompt="hi", platform="t", chat_id="c", message_id="m1")
    # Murzik #522 round-1: typed transient exception (was bare
    # RuntimeError) — the worker recognises it as "preserve inflight,
    # sleep + retry the same turn".
    with pytest.raises(
        tmux_session._ContextLockDeferral, match="context lock present"
    ):
        await ss._deliver_turn(turn)

    # Paste must not have been called — lock check is the first thing
    # _deliver_turn does.
    tmux.paste_text.assert_not_awaited()

    # Once the lock is released, the next dispatch proceeds normally.
    lock_path.unlink()
    await ss._deliver_turn(turn)
    tmux.paste_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_tmux_repl_resets_completed_turn_flag() -> None:
    """The idle-prompt gate's discriminator is ``_has_completed_turn``.
    Every fresh spawn must reset it to False so the gate fires for the
    first paste against the new REPL — even after a prior REPL on this
    session object had completed turns.
    """
    ss, _ = _make_session()
    # Simulate a prior REPL having completed turns.
    ss._has_completed_turn = True
    await ss._spawn_tmux_repl()
    assert ss._has_completed_turn is False
    await ss.disconnect()


@pytest.mark.asyncio
async def test_disconnect_clears_inflight_meta() -> None:
    """Pushok's PR #496 round-1 Case 2: a stale ``_inflight_meta`` from
    a turn that was in-flight at disconnect time must not survive the
    disconnect — otherwise a straggler stop_hook_summary read after
    reconnect could route a response to a stale chat."""
    ss, _ = _make_session()
    await ss.connect()
    # Simulate an in-flight turn.
    ss._inflight_meta = {"platform": "telegram", "chat_id": "999", "message_id": "m"}
    await ss.disconnect()
    assert ss._inflight_meta == {}


@pytest.mark.asyncio
async def test_multi_prompt_routing_no_cross_user_leak(tmp_path) -> None:
    """#560 / Pushok PR #496 round-1 Case 1 — preserved with concurrent
    dispatch: two ``send()`` calls in quick succession must route response
    A to chat A and response B to chat B, regardless of how quickly the
    worker dispatched them.

    Pre-#560 contract: worker awaited ``_turn_done`` between dispatches,
    so turn B sat in ``_message_queue`` until turn A finished. The
    in-flight meta cell was always exactly turn A's.

    Post-#560 contract: worker dispatches both back-to-back (paste-with-
    Enter), Claude Code's native queued-prompt feature absorbs turn B
    while turn A runs, ``_inflight_metas`` holds BOTH metas in FIFO
    order. The router still routes A→A, B→B because the deque pops
    oldest-first and each entry carries its own routing dict (no
    shared mutable cell to clobber).
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # Repoint tailer at our synthetic transcript.
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    ss.set_transcript_path(transcript)
    # Use tight cadences so the test runs fast.
    ss._tailer._fallback_poll_sec = 0.02
    ss._tailer._active_poll_sec = 0.01

    # Queue two prompts back-to-back.
    await ss.send(prompt="from A", platform="telegram", chat_id="A", message_id="mA")
    await ss.send(prompt="from B", platform="telegram", chat_id="B", message_id="mB")

    # Under #560 the worker dispatches both turns; both metas land in
    # ``_inflight_metas`` in order. Spin until both are appended.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(ss._inflight_metas) >= 2:
            break
    assert len(ss._inflight_metas) == 2, (
        "worker should dispatch BOTH turns back-to-back under concurrent "
        f"dispatch (#560); got {len(ss._inflight_metas)} in-flight metas"
    )
    # FIFO check: oldest entry (deque head) is A, next is B.
    assert ss._inflight_metas[0].meta == {
        "platform": "telegram", "chat_id": "A", "message_id": "mA",
    }, "deque head must be turn A's meta"
    assert ss._inflight_metas[1].meta == {
        "platform": "telegram", "chat_id": "B", "message_id": "mB",
    }, "deque second must be turn B's meta"

    # Write turn A's response + stop_hook_summary to the transcript.
    # (``_json`` is imported at file scope; no local re-import needed.)
    turn_a_entries = [
        {"type": "assistant", "timestamp": "2026-05-14T05:00:00.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "response A"}],
                     "stop_reason": "end_turn",
                     "usage": {}}},
        {"type": "system", "subtype": "stop_hook_summary",
         "timestamp": "2026-05-14T05:00:00.500Z"},
    ]
    transcript.write_text(
        "\n".join(_json.dumps(e) for e in turn_a_entries) + "\n"
    )
    ss._tailer.wake()

    # Wait for response A to fire its callback. After popleft, the deque
    # head should advance to B.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if cb.calls and len(ss._inflight_metas) == 1:
            break

    # Critical: response A was routed to chat A, NOT chat B (the original
    # Case 1 leak). With the deque each entry carries its own routing
    # dict — there is no shared mutable cell to clobber.
    assert len(cb.calls) == 1
    result = cb.calls[0]
    assert result.response_text == "response A"
    assert result.chat_id == "A", (
        f"response A leaked to wrong chat: {result} — Case 1 regression"
    )
    # Deque head is now B.
    assert ss._inflight_metas[0].meta["chat_id"] == "B"

    # Append turn B's response + stop_hook_summary.
    turn_b_entries = [
        {"type": "assistant", "timestamp": "2026-05-14T05:00:01.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "response B"}],
                     "stop_reason": "end_turn",
                     "usage": {}}},
        {"type": "system", "subtype": "stop_hook_summary",
         "timestamp": "2026-05-14T05:00:01.500Z"},
    ]
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(_json.dumps(e) for e in turn_b_entries) + "\n")
    ss._tailer.wake()

    for _ in range(50):
        await asyncio.sleep(0.02)
        if len(cb.calls) == 2:
            break

    # Critical: response B was routed to chat B.
    assert len(cb.calls) == 2
    result = cb.calls[1]
    assert result.response_text == "response B"
    assert result.chat_id == "B"
    # Deque is now empty.
    assert len(ss._inflight_metas) == 0

    await ss.disconnect()


@pytest.mark.asyncio
async def test_worker_force_restarts_on_turn_done_timeout(monkeypatch) -> None:
    """#560 retargeting note: pre-#560 this contract was enforced by
    the worker's per-iter ``_turn_done`` timeout. Under concurrent
    dispatch the worker no longer waits between turns, so the "stop
    hook never fires → force_restart" failure mode moves to the
    ``_inflight_watchdog`` background task — which ages the deque
    HEAD (not paste age) so each queued turn gets its own fair
    window once it becomes the head (Murzik review point #1).

    Test name preserved for `git blame` continuity; behavior pinned
    on the new mechanism. Original Case 1 protection (no cross-routing
    leak) is now structural via the per-entry routing dicts in
    ``_inflight_metas``.
    """
    from pinky_daemon import tmux_session
    # Shorten both timers so the test doesn't take 10 minutes. Watchdog
    # ticks at _WATCHDOG_TICK_SEC; head age must exceed _TURN_DONE_TIMEOUT_SEC
    # before force_restart fires.
    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)

    guard = MagicMock(return_value={"restart_safe": False, "reason": "no save"})
    ss, _ = _make_session(restart_guard=guard)
    await ss.connect()

    # Track force_restart calls — replace with a stub that signals.
    force_restart_called = asyncio.Event()
    force_restart_done = asyncio.Event()
    force_restart_results: list[bool] = []
    original_force_restart = ss.force_restart

    async def stub_force_restart(*, bypass_guard: bool = False):
        force_restart_called.set()
        # Call original to drive state machine through reconnect.
        # Propagate bypass_guard so the watchdog's recovery path is
        # exercised end-to-end (Murzik review on commit 3 of PR #561).
        try:
            result = await original_force_restart(bypass_guard=bypass_guard)
            force_restart_results.append(result)
            return result
        finally:
            force_restart_done.set()

    ss.force_restart = stub_force_restart

    # Send one prompt — worker dispatches; deque grows by one; no stop
    # hook ever fires, so the watchdog detects the stuck head and
    # schedules force_restart.
    await ss.send(prompt="stuck", platform="t", chat_id="c", message_id="m")

    # Wait for the watchdog to detect + fire force_restart.
    try:
        await asyncio.wait_for(force_restart_called.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "force_restart should have been called after inflight watchdog "
            "head-age exceeded _TURN_DONE_TIMEOUT_SEC"
        )

    try:
        await asyncio.wait_for(force_restart_done.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail("pre-first-turn force_restart should not be blocked by guard")

    # Turn-timeout counter incremented (now in the watchdog).
    assert ss._stats.get("turn_timeouts", 0) == 1
    assert ss._has_completed_turn is False
    assert force_restart_results == [True]
    guard.assert_not_called()
    # Deque was drained by the watchdog's force_restart path.
    assert len(ss._inflight_metas) == 0

    await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# #118 — watchdog only restarts when ACTUALLY wedged (verdict carve-outs)
# ──────────────────────────────────────────────────────────────────────────


def _mk_inflight_meta(completion_event=None):
    """Build an _InflightMeta with all required fields for watchdog tests."""
    return tmux_session._InflightMeta(
        meta={},
        completion_event=completion_event,
        internal=False,
        dispatched_at=_time.time(),
        turn=_QueuedTurn(prompt="x", platform="t", chat_id="c", message_id="m"),
    )


def _age_out_head(ss) -> None:
    """Put the inflight head well past _TURN_DONE_TIMEOUT_SEC with one meta."""
    ss._inflight_metas.append(_mk_inflight_meta())
    ss._head_started_at = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)


def test_inflight_verdict_ok_when_not_aged() -> None:
    ss, _ = _make_session(state=SessionState.CONNECTED)
    ss._inflight_metas.append(_mk_inflight_meta())
    ss._head_started_at = _time.time()  # age ~0
    assert ss._inflight_stall_verdict(_time.time()) == "ok"


def test_inflight_verdict_ok_when_empty() -> None:
    ss, _ = _make_session(state=SessionState.CONNECTED)
    ss._head_started_at = None
    assert ss._inflight_stall_verdict(_time.time()) == "ok"


def test_inflight_verdict_growing_when_transcript_fresh() -> None:
    """A long/streaming turn keeps writing the transcript — not wedged."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: True
    assert ss._inflight_stall_verdict(_time.time()) == "growing"


def test_inflight_verdict_idle_when_repl_idle_recent() -> None:
    """REPL reported idle after the head started → phantom meta, not wedged."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": _time.time(),
    }
    assert ss._inflight_stall_verdict(_time.time()) == "idle"


def test_inflight_verdict_wedged_when_working_and_quiet() -> None:
    """REPL says working but produced nothing → genuinely wedged."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "working",
        "last_updated": _time.time(),
    }
    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_inflight_verdict_wedged_when_no_live_status_fn() -> None:
    """Signal unavailable (e.g. tests) → preserve original recovery
    behavior: treat a quiet aged-out head as wedged."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: False
    assert ss._config.live_status_fn is None
    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_inflight_verdict_growing_when_background_task_active(tmp_path) -> None:
    """#692: a turn parked on a background Workflow/Agent — main transcript
    quiet, REPL 'working' — must be ``growing`` (not wedged) while its subagent
    transcripts are still being written under the sibling session dir."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: False
    # REPL genuinely busy on the background tool call (would be "wedged" today).
    ss._config.live_status_fn = lambda: {
        "status": "working",
        "last_updated": _time.time(),
    }
    # Main transcript at <session>.jsonl; background work under <session>/.
    main = tmp_path / "session.jsonl"
    main.write_text("{}\n")
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = main
    sub = tmp_path / "session" / "subagents" / "workflows" / "wf_x"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text("{}\n")  # fresh mtime = now
    assert ss._inflight_stall_verdict(_time.time()) == "growing"


def test_inflight_verdict_wedged_when_background_task_stale(tmp_path) -> None:
    """#692 negative: background transcripts exist but were last written long
    ago (the workflow itself hung) while the main REPL is also quiet → still
    ``wedged``, preserving genuine stuck-REPL recovery."""
    import os

    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "working",
        "last_updated": _time.time(),
    }
    main = tmp_path / "session.jsonl"
    main.write_text("{}\n")
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = main
    sub = tmp_path / "session" / "subagents" / "wf_x"
    sub.mkdir(parents=True)
    f = sub / "agent-1.jsonl"
    f.write_text("{}\n")
    stale = _time.time() - (tmux_session._BACKGROUND_TASK_ACTIVE_WINDOW_SEC + 60.0)
    # Age both the file AND its parent dir past the window (writing the file
    # had bumped the dir mtime to "now").
    os.utime(f, (stale, stale))
    os.utime(sub, (stale, stale))
    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_background_tasks_recently_active_false_without_session_dir(tmp_path) -> None:
    """Helper returns False when there's no sibling background dir — preserves
    the wedged/idle fall-through for ordinary (non-background) turns."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    main = tmp_path / "session.jsonl"
    main.write_text("{}\n")
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = main
    assert (
        ss._background_tasks_recently_active(
            _time.time(), tmux_session._BACKGROUND_TASK_ACTIVE_WINDOW_SEC
        )
        is False
    )


def test_inflight_verdict_growing_when_foreground_tool_in_flight() -> None:
    """#731: a long FOREGROUND tool call (main transcript quiet, no subagent
    dir, REPL 'working') must be ``growing`` while its tool_use_id is in
    flight — otherwise the watchdog SIGKILLs a healthy turn's tool child."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "working",
        "last_updated": _time.time(),
    }
    # A foreground tool started recently and hasn't reported finish.
    ss._inflight_tool_calls = {"toolu_x": _time.time()}
    assert ss._inflight_stall_verdict(_time.time()) == "growing"


def test_inflight_verdict_wedged_when_no_foreground_tool() -> None:
    """#731 negative: no in-flight tool + quiet + 'working' → still ``wedged``,
    preserving genuine stuck-REPL recovery (the wedge the model actually hit
    has no tool running)."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "working",
        "last_updated": _time.time(),
    }
    assert ss._inflight_tool_calls == {}
    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_inflight_verdict_wedged_when_foreground_tool_past_ceiling() -> None:
    """#731 bound: a tool 'in flight' longer than the ceiling is a lost
    finish-POST or a hung child — NOT credited (and pruned), so a real wedge
    still recovers (just later)."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    _age_out_head(ss)
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "working",
        "last_updated": _time.time(),
    }
    stale = _time.time() - (
        tmux_session._FOREGROUND_TOOL_ACTIVE_CEILING_SEC + 60.0
    )
    ss._inflight_tool_calls = {"toolu_stale": stale}
    assert ss._inflight_stall_verdict(_time.time()) == "wedged"
    # Stale entry pruned so the set can't grow unbounded.
    assert ss._inflight_tool_calls == {}


def test_foreground_tool_in_flight_prunes_stale_keeps_fresh() -> None:
    """Helper prunes only entries past the ceiling; a fresh concurrent entry
    still counts as live."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    now = _time.time()
    stale = now - (tmux_session._FOREGROUND_TOOL_ACTIVE_CEILING_SEC + 1.0)
    ss._inflight_tool_calls = {"old": stale, "new": now}
    assert ss._foreground_tool_in_flight(now) is True
    assert "old" not in ss._inflight_tool_calls  # pruned
    assert "new" in ss._inflight_tool_calls  # retained


def test_foreground_tool_in_flight_false_when_empty() -> None:
    """Helper returns False with no in-flight tools — preserves the
    wedged/idle fall-through for ordinary turns."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    assert ss._inflight_tool_calls == {}
    assert ss._foreground_tool_in_flight(_time.time()) is False


def test_inflight_verdict_wedged_when_idle_predates_head() -> None:
    """Hang-on-paste: a turn was pasted (head started) but the REPL's idle
    status is STALE (predates the head) — the REPL never came alive for this
    turn, so it IS a wedge, not a phantom."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    head = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)
    ss._inflight_metas.append(_mk_inflight_meta())
    ss._head_started_at = head
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": head - 100.0,  # idle reported BEFORE this head started
    }
    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_inflight_verdict_wedged_when_stale_idle_within_old_slack() -> None:
    """Regression (#118 / Murzik round-2): a FRESH first turn whose idle status
    is stale — left over from the PREVIOUS turn, reported just 1s before this
    turn was pasted — must be ``wedged``, not phantom-drained.

    The earlier ``_head_started_at - 5s`` slack window accepted this stale idle
    (``head - 1 >= head - 5``) and silently dropped a real hang-on-paste. The
    freshness floor is now ``min(_head_started_at, head.dispatched_at)`` with no
    slack; for a fresh first turn the two are equal, so any pre-dispatch idle is
    rejected.
    """
    ss, _ = _make_session(state=SessionState.CONNECTED)
    head = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)
    meta = _mk_inflight_meta()
    meta.dispatched_at = head  # fresh first turn: dispatched == head start
    ss._inflight_metas.append(meta)
    ss._head_started_at = head
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": head - 1.0,  # 1s stale — inside the OLD 5s slack window
    }
    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_inflight_verdict_idle_for_queued_turn_uses_dispatch_floor() -> None:
    """A queued turn that inherited the head spot was pasted (``dispatched_at``)
    BEFORE the head re-based to it (``_head_started_at``). An idle reported after
    its paste but before the re-base is still a valid phantom signal → ``idle``.

    The floor is ``min(_head_started_at, dispatched_at) = dispatched_at``, so
    tailer/status ordering jitter for queued turns is tolerated. (Under the old
    fixed-slack rule this would have been mis-classified ``wedged``.)
    """
    ss, _ = _make_session(state=SessionState.CONNECTED)
    head = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)
    meta = _mk_inflight_meta()
    meta.dispatched_at = head - 50.0  # pasted 50s before the head re-base
    ss._inflight_metas.append(meta)
    ss._head_started_at = head
    ss._transcript_recently_grew = lambda now, window: False
    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": head - 25.0,  # after this turn's paste, before re-base
    }
    assert ss._inflight_stall_verdict(_time.time()) == "idle"


def test_inflight_verdict_idle_via_transcript_mtime(tmp_path) -> None:
    """#592: stale live_status but transcript grew well after paste → phantom, not wedged."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    head_t = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)
    meta = _mk_inflight_meta()
    meta.dispatched_at = head_t
    meta.transcript_mtime_at_paste = head_t
    meta.paste_succeeded_at = head_t
    ss._inflight_metas.append(meta)
    ss._head_started_at = head_t
    ss._transcript_recently_grew = lambda now, window: False

    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": head_t - 50.0,  # predates this turn's paste
    }

    f = tmp_path / "transcript.jsonl"
    f.write_text("{}\n")
    import os

    mtime_with_response = head_t + 30.0  # well past _TRANSCRIPT_PASTE_SLACK
    os.utime(f, (mtime_with_response, mtime_with_response))
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = f

    assert ss._inflight_stall_verdict(_time.time()) == "idle"


def test_inflight_verdict_wedged_when_transcript_only_paste_echo(tmp_path) -> None:
    """#592: stale live_status, transcript mtime is only the paste echo (< slack) → wedged."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    head_t = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)
    meta = _mk_inflight_meta()
    meta.dispatched_at = head_t
    meta.transcript_mtime_at_paste = head_t
    meta.paste_succeeded_at = head_t
    ss._inflight_metas.append(meta)
    ss._head_started_at = head_t
    ss._transcript_recently_grew = lambda now, window: False

    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": head_t - 50.0,
    }

    f = tmp_path / "transcript.jsonl"
    f.write_text("{}\n")
    import os

    mtime_paste_echo = head_t + 1.0  # within _TRANSCRIPT_PASTE_SLACK (5s)
    os.utime(f, (mtime_paste_echo, mtime_paste_echo))
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = f

    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_inflight_verdict_wedged_when_no_paste_baselines(tmp_path) -> None:
    """#592: stale live_status, BOTH paste baselines None (legacy meta) → falls back to wedged."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    head_t = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)
    meta = _mk_inflight_meta()
    meta.dispatched_at = head_t
    meta.transcript_mtime_at_paste = None  # unavailable at paste time
    meta.paste_succeeded_at = None  # legacy meta from before the daemon-clock baseline
    ss._inflight_metas.append(meta)
    ss._head_started_at = head_t
    ss._transcript_recently_grew = lambda now, window: False

    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": head_t - 50.0,
    }

    f = tmp_path / "transcript.jsonl"
    f.write_text("{}\n")
    import os

    mtime_with_response = head_t + 30.0
    os.utime(f, (mtime_with_response, mtime_with_response))
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = f

    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_inflight_verdict_wedged_when_mtime_at_paste_is_stale(tmp_path) -> None:
    """#592/#595: transcript_mtime_at_paste is a STALE previous-turn write (the JSONL
    write lagged the tmux paste), and the only write afterward is the paste echo within
    slack of the ACTUAL paste time. The daemon-clock paste_succeeded_at baseline must
    anchor the floor to this turn so a real hang-on-paste stays wedged (Murzik #595)."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    head_t = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)
    meta = _mk_inflight_meta()
    meta.dispatched_at = head_t
    # File mtime sampled at paste was the PREVIOUS turn's write, far in the past —
    # the JSONL had not been touched for this turn when _deliver_turn sampled it.
    meta.transcript_mtime_at_paste = head_t - 100.0
    meta.paste_succeeded_at = head_t  # daemon clock: paste actually happened at head_t
    ss._inflight_metas.append(meta)
    ss._head_started_at = head_t
    ss._transcript_recently_grew = lambda now, window: False

    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": head_t - 50.0,
    }

    f = tmp_path / "transcript.jsonl"
    f.write_text("{}\n")
    import os

    # Only the paste echo lands, ~1 s after the real paste — within _TRANSCRIPT_PASTE_SLACK
    # of paste_succeeded_at. A naive mtime-only baseline (head_t-100) would clear the slack
    # (head_t+1 > head_t-95) and false-drain; the max(...) baseline keeps it wedged.
    mtime_paste_echo = head_t + 1.0
    os.utime(f, (mtime_paste_echo, mtime_paste_echo))
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = f

    assert ss._inflight_stall_verdict(_time.time()) == "wedged"


def test_inflight_verdict_idle_via_paste_clock_when_mtime_at_paste_missing(tmp_path) -> None:
    """#592/#595: mtime-at-paste sampling failed (None) but the daemon-clock
    paste_succeeded_at baseline still detects a real post-paste response → phantom/idle."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    head_t = _time.time() - (tmux_session._TURN_DONE_TIMEOUT_SEC + 100.0)
    meta = _mk_inflight_meta()
    meta.dispatched_at = head_t
    meta.transcript_mtime_at_paste = None  # sampling failed at paste time
    meta.paste_succeeded_at = head_t
    ss._inflight_metas.append(meta)
    ss._head_started_at = head_t
    ss._transcript_recently_grew = lambda now, window: False

    ss._config.live_status_fn = lambda: {
        "status": "idle",
        "last_updated": head_t - 50.0,
    }

    f = tmp_path / "transcript.jsonl"
    f.write_text("{}\n")
    import os

    mtime_with_response = head_t + 30.0  # real response well past the paste + slack
    os.utime(f, (mtime_with_response, mtime_with_response))
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = f

    assert ss._inflight_stall_verdict(_time.time()) == "idle"


def test_transcript_recently_grew(tmp_path) -> None:
    import os

    ss, _ = _make_session(state=SessionState.CONNECTED)
    f = tmp_path / "transcript.jsonl"
    f.write_text("{}\n")
    ss._tailer = MagicMock()
    ss._tailer.transcript_path = f

    now = _time.time()
    assert ss._transcript_recently_grew(now, 600.0) is True

    old = now - 10_000
    os.utime(f, (old, old))
    assert ss._transcript_recently_grew(now, 600.0) is False

    # No tailer / no path → False (not growing).
    ss._tailer = None
    assert ss._transcript_recently_grew(now, 600.0) is False


@pytest.mark.asyncio
async def test_inflight_watchdog_drains_phantom_when_repl_idle(monkeypatch) -> None:
    """#118 end-to-end: an aged-out head with a quiet transcript and an idle
    REPL must be RECONCILED (deque drained, completion events fired) — NOT
    force_restarted. This is the core "stop tearing sessions down when nothing
    is wedged" fix.
    """
    from pinky_daemon import tmux_session

    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)

    ss, _ = _make_session(state=SessionState.CONNECTED)
    ss._config.live_status_fn = lambda: {"status": "idle", "last_updated": _time.time()}
    ss._transcript_recently_grew = lambda now, window: False

    restarted = {"v": False}

    async def _no_restart(*, bypass_guard: bool = False):
        restarted["v"] = True
        return True

    ss.force_restart = _no_restart

    ev = asyncio.Event()
    ss._inflight_metas.append(_mk_inflight_meta(completion_event=ev))
    ss._head_started_at = _time.time() - 1.0  # aged past the 0.05s timeout

    task = asyncio.create_task(ss._inflight_watchdog())
    try:
        # The drain fires the phantom's completion_event.
        await asyncio.wait_for(ev.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail("watchdog should have reconciled the phantom meta")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert restarted["v"] is False, "must NOT restart an idle REPL"
    assert len(ss._inflight_metas) == 0, "phantom meta must be drained"
    assert ss._head_started_at is None


# ──────────────────────────────────────────────────────────────────────────
# #832 — pane-content liveness (rescue a long pure-reasoning / slow-generation
# turn the transcript/tool signals miss, esp. under ultracode/xhigh)
# ──────────────────────────────────────────────────────────────────────────


def _pane(content: str) -> TmuxCommandResult:
    return TmuxCommandResult(returncode=0, stdout=content, stderr="")


@pytest.mark.asyncio
async def test_pane_is_animating_true_when_pane_changes() -> None:
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(side_effect=[_pane("Thinking (44s)"), _pane("Thinking (46s)")])
    assert await ss._pane_is_animating() is True


@pytest.mark.asyncio
async def test_pane_is_animating_false_when_pane_frozen() -> None:
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_pane("> frozen prompt"))
    assert await ss._pane_is_animating() is False


@pytest.mark.asyncio
async def test_pane_is_animating_false_on_capture_failure() -> None:
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_fail("no pane"))
    assert await ss._pane_is_animating() is False
    tmux.capture_pane = AsyncMock(side_effect=RuntimeError("tmux gone"))
    assert await ss._pane_is_animating() is False


def test_pane_liveness_enabled_default_and_killswitch(monkeypatch) -> None:
    from pinky_daemon import tmux_session

    monkeypatch.delenv("PINKY_WATCHDOG_PANE_LIVENESS", raising=False)
    assert tmux_session._pane_liveness_enabled() is True  # default ON
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("PINKY_WATCHDOG_PANE_LIVENESS", off)
        assert tmux_session._pane_liveness_enabled() is False
    monkeypatch.setenv("PINKY_WATCHDOG_PANE_LIVENESS", "1")
    assert tmux_session._pane_liveness_enabled() is True


def test_inflight_hard_ceiling_default_and_override(monkeypatch) -> None:
    from pinky_daemon import tmux_session

    monkeypatch.delenv("PINKY_INFLIGHT_HARD_CEILING_SEC", raising=False)
    assert tmux_session._inflight_hard_ceiling_sec() == 3600.0
    monkeypatch.setenv("PINKY_INFLIGHT_HARD_CEILING_SEC", "1200")
    assert tmux_session._inflight_hard_ceiling_sec() == 1200.0
    # Never below the base timeout, even if env asks for less.
    monkeypatch.setenv("PINKY_INFLIGHT_HARD_CEILING_SEC", "1")
    assert tmux_session._inflight_hard_ceiling_sec() == tmux_session._TURN_DONE_TIMEOUT_SEC
    # Garbage falls back to the default.
    monkeypatch.setenv("PINKY_INFLIGHT_HARD_CEILING_SEC", "nope")
    assert tmux_session._inflight_hard_ceiling_sec() == 3600.0


def _wedged_session():
    """A CONNECTED session whose aged-out head would classify ``wedged``:
    transcript quiet, no fg tool, REPL ``working`` (not idle)."""
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    ss._config.live_status_fn = lambda: {"status": "working", "last_updated": _time.time()}
    ss._transcript_recently_grew = lambda now, window: False
    ss._inflight_metas.append(_mk_inflight_meta())
    ss._head_started_at = _time.time() - 1.0  # aged past the (monkeypatched) timeout
    return ss, tmux


@pytest.mark.asyncio
async def test_inflight_watchdog_extends_when_pane_animating(monkeypatch) -> None:
    """The #832 fix: a wedged-looking head whose pane is still animating is
    EXTENDED, not force_restarted."""
    from pinky_daemon import tmux_session

    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)
    monkeypatch.setattr(tmux_session, "_PANE_LIVENESS_SAMPLE_GAP_SEC", 0.0)

    ss, tmux = _wedged_session()
    flip = {"n": 0}

    def _alt(**kw):
        flip["n"] += 1
        return _pane("frame-a" if flip["n"] % 2 else "frame-b")

    tmux.capture_pane = AsyncMock(side_effect=_alt)

    restarted = {"v": False}

    async def _no_restart(*, bypass_guard: bool = False):
        restarted["v"] = True
        return True

    ss.force_restart = _no_restart

    task = asyncio.create_task(ss._inflight_watchdog())
    try:
        await asyncio.sleep(0.25)  # several ticks
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert restarted["v"] is False, "animating pane must NOT be force_restarted"
    assert tmux.capture_pane.await_count >= 2, "pane must be sampled (twice)"
    assert ss._head_started_at is not None, "window must be extended, not cleared"


@pytest.mark.asyncio
async def test_inflight_watchdog_restarts_when_pane_frozen(monkeypatch) -> None:
    """A frozen pane (genuine wedge) still force_restarts."""
    from pinky_daemon import tmux_session

    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)
    monkeypatch.setattr(tmux_session, "_PANE_LIVENESS_SAMPLE_GAP_SEC", 0.0)

    ss, tmux = _wedged_session()
    tmux.capture_pane = AsyncMock(return_value=_pane("> frozen"))

    fired = asyncio.Event()

    async def _restart(*, bypass_guard: bool = False):
        fired.set()
        return True

    ss.force_restart = _restart

    task = asyncio.create_task(ss._inflight_watchdog())
    try:
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail("frozen pane must force_restart")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_inflight_watchdog_restarts_over_ceiling_even_if_animating(monkeypatch) -> None:
    """The absolute ceiling beats pane-liveness: an animating-but-stuck REPL
    past the ceiling still recovers."""
    from pinky_daemon import tmux_session

    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)
    monkeypatch.setattr(tmux_session, "_PANE_LIVENESS_SAMPLE_GAP_SEC", 0.0)
    monkeypatch.setenv("PINKY_INFLIGHT_HARD_CEILING_SEC", "0.1")  # head aged ~1s > ceiling

    ss, tmux = _wedged_session()
    flip = {"n": 0}

    def _alt(**kw):
        flip["n"] += 1
        return _pane("a" if flip["n"] % 2 else "b")

    tmux.capture_pane = AsyncMock(side_effect=_alt)

    fired = asyncio.Event()

    async def _restart(*, bypass_guard: bool = False):
        fired.set()
        return True

    ss.force_restart = _restart

    task = asyncio.create_task(ss._inflight_watchdog())
    try:
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail("over-ceiling head must force_restart even if animating")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Over the ceiling, the rescue must short-circuit BEFORE sampling the pane —
    # no point paying two capture-pane calls + a sample gap on a head we are about
    # to tear down regardless.
    assert tmux.capture_pane.await_count == 0, (
        "over-ceiling head must not sample the pane (ceiling short-circuits first)"
    )


@pytest.mark.asyncio
async def test_inflight_watchdog_no_restart_when_head_completes_during_sampling(
    monkeypatch,
) -> None:
    """#832 race guard: a stop hook can pop the head DURING the pane-liveness
    sampling await (capture-pane twice + sample gap). The now-stale "wedged"
    verdict must NOT force_restart, and must NOT ``popleft`` an emptied deque
    (an IndexError there is swallowed by the watchdog's ``except`` and returns,
    silently killing recovery for the session). Simulate the concurrent
    completion by emptying the deque from inside the awaited capture_pane."""
    from pinky_daemon import tmux_session

    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)
    monkeypatch.setattr(tmux_session, "_PANE_LIVENESS_SAMPLE_GAP_SEC", 0.0)

    ss, tmux = _wedged_session()

    calls = {"n": 0}

    def _cap(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # Mimic ``_handle_turn_complete`` firing mid-sample: head completed,
            # deque emptied out from under the watchdog.
            ss._inflight_metas.clear()
            ss._head_started_at = None
        return _pane("> idle")  # frozen → _pane_is_animating False → fall through

    tmux.capture_pane = AsyncMock(side_effect=_cap)

    restarted = {"v": False}

    async def _no_restart(*, bypass_guard: bool = False):
        restarted["v"] = True
        return True

    ss.force_restart = _no_restart

    task = asyncio.create_task(ss._inflight_watchdog())
    await asyncio.sleep(0.2)  # let several ticks run
    crashed = task.done()
    crash_exc = task.exception() if crashed else None
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert restarted["v"] is False, (
        "must not force_restart a head that completed during pane sampling"
    )
    assert not crashed, (
        "watchdog must survive a head emptied mid-sample (no IndexError on an "
        f"empty-deque popleft) — task exited early: {crash_exc!r}"
    )


@pytest.mark.asyncio
async def test_inflight_watchdog_ceiling_accumulates_across_animating_samples(
    monkeypatch,
) -> None:
    """Regression for the #832 ceiling bug: a head that starts BELOW the ceiling
    and animates continuously must STILL force_restart once cumulative wall-time
    crosses the ceiling. The extend branch resets ``_head_started_at`` on every
    sample, so the ceiling must be anchored to a clock that does NOT reset — else
    ``age`` would reset each cycle and the bound is unreachable (a stuck-but-
    animating REPL pinned alive forever). This fails on the pre-fix code (no
    restart, anchor-less ``age < ceiling`` always true) and passes once the
    ceiling is anchored to the head's first pane-liveness credit."""
    from pinky_daemon import tmux_session

    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)
    monkeypatch.setattr(tmux_session, "_PANE_LIVENESS_SAMPLE_GAP_SEC", 0.0)
    # Ceiling spans several timeout cycles, so the rescue extends the head
    # multiple times BEFORE the absolute bound forces recovery — exactly the
    # accumulation the buggy code never reached.
    monkeypatch.setenv("PINKY_INFLIGHT_HARD_CEILING_SEC", "0.3")

    ss, tmux = _wedged_session()
    # Start the head FRESH (age ~0, well under the 0.3s ceiling) so the only way
    # to reach the ceiling is genuine accumulation across animating samples.
    ss._head_started_at = _time.time()

    flip = {"n": 0}

    def _alt(**kw):
        flip["n"] += 1
        return _pane("a" if flip["n"] % 2 else "b")  # always changing → animating

    tmux.capture_pane = AsyncMock(side_effect=_alt)

    fired = asyncio.Event()

    async def _restart(*, bypass_guard: bool = False):
        fired.set()
        return True

    ss.force_restart = _restart

    task = asyncio.create_task(ss._inflight_watchdog())
    try:
        # Must fire well after one timeout cycle (0.05s) — proving the head was
        # extended repeatedly — but bounded by the 0.3s ceiling. 2s gives slack.
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "continuously-animating head must STILL force_restart once cumulative "
            "age crosses the ceiling (ceiling must not reset with _head_started_at)"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # The pane was sampled many times → the head was genuinely extended across
    # multiple cycles before the ceiling won (not a tick-1 over-ceiling restart).
    assert tmux.capture_pane.await_count >= 2, (
        "head should have been extended (pane sampled) before the ceiling fired"
    )


@pytest.mark.asyncio
async def test_inflight_watchdog_ceiling_anchor_keyed_to_head_identity(
    monkeypatch,
) -> None:
    """The #832 ceiling anchor is keyed to the head meta's identity. When the
    deque advances to a genuinely new head while a STALE anchor (from the prior
    head) is still set, the rescue must re-anchor to the new head's own start —
    NOT judge the fresh head against the previous head's already-elapsed clock
    (which would force_restart a brand-new healthy turn almost immediately)."""
    from pinky_daemon import tmux_session

    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)
    monkeypatch.setattr(tmux_session, "_PANE_LIVENESS_SAMPLE_GAP_SEC", 0.0)
    monkeypatch.setenv("PINKY_INFLIGHT_HARD_CEILING_SEC", "0.3")

    ss, tmux = _wedged_session()
    head_b = ss._inflight_metas[0]
    # A stale anchor from a PRIOR head (some other meta) that already burned far
    # more than the ceiling. If the rescue keyed off this instead of head_b's
    # identity, head_b would be judged over-ceiling on tick 1 and torn down.
    ss._inflight_pane_ext_anchor = (object(), _time.time() - 999.0)
    ss._head_started_at = _time.time()  # head_b just started

    flip = {"n": 0}

    def _alt(**kw):
        flip["n"] += 1
        return _pane("a" if flip["n"] % 2 else "b")  # animating

    tmux.capture_pane = AsyncMock(side_effect=_alt)

    restarted = {"v": False}

    async def _no_restart(*, bypass_guard: bool = False):
        restarted["v"] = True
        return True

    ss.force_restart = _no_restart

    task = asyncio.create_task(ss._inflight_watchdog())
    await asyncio.sleep(0.15)  # > timeout, < the 0.3s fresh ceiling
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert restarted["v"] is False, (
        "fresh head must get its OWN ceiling budget, not inherit a stale anchor"
    )
    anchor = ss._inflight_pane_ext_anchor
    assert anchor is not None and anchor[0] is head_b, (
        "anchor must be re-keyed to the current head's identity"
    )


@pytest.mark.asyncio
async def test_inflight_watchdog_killswitch_disables_pane_rescue(monkeypatch) -> None:
    """PINKY_WATCHDOG_PANE_LIVENESS=0 restores the pre-#832 transcript/tool-only
    verdict — an animating pane is no longer credited."""
    from pinky_daemon import tmux_session

    monkeypatch.setattr(tmux_session, "_TURN_DONE_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_WATCHDOG_TICK_SEC", 0.02)
    monkeypatch.setattr(tmux_session, "_PANE_LIVENESS_SAMPLE_GAP_SEC", 0.0)
    monkeypatch.setenv("PINKY_WATCHDOG_PANE_LIVENESS", "0")

    ss, tmux = _wedged_session()
    flip = {"n": 0}

    def _alt(**kw):
        flip["n"] += 1
        return _pane("a" if flip["n"] % 2 else "b")

    tmux.capture_pane = AsyncMock(side_effect=_alt)

    fired = asyncio.Event()

    async def _restart(*, bypass_guard: bool = False):
        fired.set()
        return True

    ss.force_restart = _restart

    task = asyncio.create_task(ss._inflight_watchdog())
    try:
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail("kill switch must restore force_restart on a wedged head")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert tmux.capture_pane.await_count == 0, "kill switch must skip pane sampling"


@pytest.mark.asyncio
async def test_spawn_clears_turn_done_after_reconnect() -> None:
    """The turn_done invariant ("cleared between dispatches") must be
    re-established after force_restart so the first dispatch on the
    new tmux pane doesn't see a stale set() from the killed session's
    last callback.
    """
    ss, _ = _make_session()
    # Pre-set turn_done to simulate the state at the moment a
    # force_restart happens (last turn completed → callback set it).
    ss._turn_done.set()
    assert ss._turn_done.is_set()

    await ss.connect()
    # After connect (which calls _spawn_tmux_repl), the invariant is
    # restored: turn_done is cleared.
    assert not ss._turn_done.is_set(), (
        "turn_done invariant violated post-spawn — should be cleared"
    )
    await ss.disconnect()


@pytest.mark.asyncio
async def test_force_restart_resumes_tailer(tmp_path) -> None:
    """Pushok's PR #496 round-2 Case 1': ``force_restart`` must leave
    the tailer running so the new session can complete a turn.

    Bug shape pre-fix: ``_start_tailer`` was only called from
    ``connect``. ``force_restart`` invoked ``_spawn_tmux_repl`` directly
    (bypassing ``connect``), so after a restart the tailer instance
    survived but its background task was dead. Result:

    1. New worker dispatches turn → ``mark_active`` wakes a dead task.
    2. No ``stop_hook_summary`` ever fires → ``turn_done`` never set.
    3. Worker times out after 600s → another ``force_restart``.
    4. Death loop. Agent silently never delivers responses.

    The round-2 turn_done event gate (Case 1) made this loud — without
    it, the failure was silently-dropped responses on a "live" agent.

    Pre-fix this test fails with:
      ``assert ss._tailer.stats["running"] is True`` → ``False``
    and the end-to-end response_callback never fires.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()
    assert ss._tailer.stats["running"] is True, (
        "tailer should be running after cold-start connect"
    )

    # Trigger a force_restart. This drives:
    #   CONNECTED → RECONNECTING (via disconnect+_stop_tailer)
    #            → CONNECTED (via _spawn_tmux_repl)
    # The fix moves _start_tailer into _spawn_tmux_repl so the post-
    # restart session has a live tailer task.
    restart_ok = await ss.force_restart()
    assert restart_ok, "force_restart should succeed"

    # Critical invariant — pre-fix this is False (task killed by
    # disconnect's _stop_tailer, never restarted).
    assert ss._tailer is not None
    assert ss._tailer.stats["running"] is True, (
        "tailer task must be running after force_restart — Pushok Case 1' "
        "regression: if this fires, force_restart skipped _start_tailer"
    )

    # End-to-end pin: drive a complete turn through the post-restart
    # tailer and assert response_callback fires. This is the real test
    # — even if the tailer instance is "running" by accident, can it
    # actually deliver a response?
    transcript = tmp_path / "post_restart.jsonl"
    transcript.write_text("")
    ss.set_transcript_path(transcript)

    ss._inflight_meta = {
        "platform": "telegram",
        "chat_id": "999",
        "message_id": "m_post_restart",
    }

    entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-14T06:00:00.100Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "alive after restart"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "timestamp": "2026-05-14T06:00:00.500Z",
        },
    ]
    transcript.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    await ss._tailer.read_once()

    # Pre-fix: this assertion fails because the tailer task is dead and
    # ``read_once`` is the only thing keeping it limping — but the
    # background loop that would actually drive turn completion in
    # production never runs. We explicitly call ``read_once`` here so
    # the assertion is robust against scheduling jitter; the real
    # production-shape assertion is the ``stats["running"]`` one above.
    assert len(cb.calls) == 1, (
        "post-restart turn should have completed end-to-end"
    )
    result = cb.calls[0]
    assert result.agent_name == "dymok"
    assert result.response_text == "alive after restart"
    assert result.chat_id == "999"

    await ss.disconnect()


@pytest.mark.asyncio
async def test_stop_tailer_drains_buffer_for_same_path_resume(tmp_path) -> None:
    """Murzik's PR #496 round-3 Case 2'' regression: ``_stop_tailer``
    must drain the in-progress turn buffer so a same-path lifecycle
    restart (``claude --continue`` resume) doesn't leak dead-session
    partial text into the next session's first turn.

    The round-2 fix in ``set_transcript_path`` only drained on path
    *change*. If ``force_restart`` is followed by Claude Code resuming
    the same JSONL path, ``set_transcript_path`` either isn't called or
    skips the drain due to path equality. The killed session's partial
    assistant text would then survive into the next session, and the
    first complete turn's callback would fire with
    ``old_partial + new_text``.

    Pre-fix this test fails with:
      ``assert ss._tailer._buffer.is_empty`` → ``False`` after
      ``_stop_tailer``, because round-2's drain was scoped to path
      swaps and round-3's ``_start_tailer`` fix did not address the
      buffer-retention angle.
    """
    cb = _AsyncCollector()
    ss, _ = _make_session_with_response_cb(response_cb=cb)
    await ss.connect()

    # Replace the tailer's path with a controlled synthetic transcript.
    transcript = tmp_path / "session_x.jsonl"
    transcript.write_text("")
    ss.set_transcript_path(transcript)

    # Feed a partial turn — assistant entry without stop_hook_summary.
    # This simulates the in-flight state when a session is killed mid-turn.
    partial_entries = [
        {
            "type": "assistant",
            "timestamp": "2026-05-14T06:00:00.100Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "partial from X"}],
                "stop_reason": "",
                "usage": {},
            },
        },
    ]
    transcript.write_text("\n".join(_json.dumps(e) for e in partial_entries) + "\n")
    await ss._tailer.read_once()

    # Buffer should hold "partial from X"; no callback yet (no
    # stop_hook_summary).
    assert len(cb.calls) == 0
    assert not ss._tailer._buffer.is_empty, (
        "buffer should have accumulated partial assistant text"
    )

    # Stop the tailer — the fix: this should also drain the buffer.
    await ss._stop_tailer()
    assert ss._tailer._buffer.is_empty, (
        "_stop_tailer must drain the buffer (Murzik Case 2'') — "
        "without this, a same-path resume leaks dead-session text "
        "into the next session's first reply"
    )

    # Restart the tailer with the SAME path — simulates claude --continue
    # resuming. The round-2 set_transcript_path drain wouldn't fire here
    # because the path didn't change; we rely on _stop_tailer's drain.
    await ss._start_tailer()

    # Session Y produces a complete turn, appended to the same transcript.
    with open(transcript, "a") as fh:
        new_entries = [
            {
                "type": "assistant",
                "timestamp": "2026-05-14T06:05:00.100Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "response from Y"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            },
            {
                "type": "system",
                "subtype": "stop_hook_summary",
                "timestamp": "2026-05-14T06:05:00.500Z",
            },
        ]
        fh.write("\n".join(_json.dumps(e) for e in new_entries) + "\n")

    ss._inflight_meta = {"platform": "telegram", "chat_id": "Y", "message_id": "mY"}
    await ss._tailer.read_once()

    # Callback fires with ONLY Y's text — no "partial from X" prefix.
    assert len(cb.calls) == 1, "Y's turn should have fired exactly one callback"
    result = cb.calls[0]
    assert result.response_text == "response from Y", (
        f"expected clean Y response, got {result.response_text!r} — "
        f"if this contains 'partial from X', the same-path-resume "
        f"buffer-leak regression has reopened"
    )

    await ss.disconnect()


@pytest.mark.asyncio
async def test_spawn_tmux_repl_rollback_clears_partial_tailer() -> None:
    """Murzik's PR #496 round-3 cleanup-hole regression: if
    ``_start_tailer`` raises AFTER constructing ``self._tailer``, the
    ``_spawn_tmux_repl`` rollback path must stop+null the partial
    tailer instance before re-raising. Otherwise the caller transitions
    DEAD with a live orphan tailer instance.

    Pre-fix this test fails with:
      ``assert ss._tailer is None`` → ``False``, because the round-3
      rollback only killed tmux and left ``self._tailer`` populated.
    """
    ss, tmux = _make_session()

    # Monkeypatch TmuxTranscriptTailer.start so it raises AFTER the
    # tailer instance has been constructed by _start_tailer's
    # ``self._tailer = TmuxTranscriptTailer(...)`` assignment.
    from pinky_daemon import tmux_transcript

    async def boom(self):
        raise RuntimeError("synthetic tailer start failure")

    original_start = tmux_transcript.TmuxTranscriptTailer.start
    tmux_transcript.TmuxTranscriptTailer.start = boom
    try:
        with pytest.raises(RuntimeError, match="synthetic tailer start failure"):
            await ss._spawn_tmux_repl()
    finally:
        tmux_transcript.TmuxTranscriptTailer.start = original_start

    # Rollback assertions: tailer slot is cleared and tmux was killed.
    assert ss._tailer is None, (
        "rollback in _spawn_tmux_repl must reset self._tailer — "
        "otherwise the caller transitions DEAD with a live orphan "
        "tailer instance (Murzik round-3 cleanup-hole regression)"
    )
    # The tmux.kill_session call from the rollback block should have
    # fired at least once (it may also have been called by the pre-spawn
    # stale-session reaper; either way, count >= 1).
    assert tmux.kill_session.await_count >= 1, (
        "rollback must call tmux.kill_session"
    )


# ──────────────────────────────────────────────────────────────────────────
# Murzik #522 round-1 — worker-level inflight-preservation for transient
# failures (context-lock + idle-prompt timeout). The PR-1 shape ``get()``-d
# the turn from the queue BEFORE _deliver_turn, then let any exception fall
# through the catch-all, silently dropping the message. These tests pin the
# fix at the worker level (not just _deliver_turn unit) — Murzik
# specifically called this out as required.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_lock_preserves_turn_until_released(
    monkeypatch, tmp_path
) -> None:
    """Murzik #522 round-1 (the actual bug): the worker must keep the
    inflight turn in-hand while the context lock is held and re-paste
    after the lock is released, not silently drop the message.

    Pre-fix shape: ``_message_queue.get()`` happened BEFORE
    ``_deliver_turn``; the gate's ``RuntimeError`` fell through the
    worker's catch-all log-only handler. qsize went to 0 and paste_text
    was never called — for that turn or any successor.
    """
    # Speed up the worker's transient-failure backoff so the test
    # doesn't sit on a 2s sleep per retry.
    monkeypatch.setattr(tmux_session, "_TRANSIENT_RETRY_BACKOFF_SEC", 0.01)
    # Sandbox the lock dir to tmp_path.
    monkeypatch.setattr(tmux_session, "_TRANSPORT_LOCK_DIR", tmp_path)

    ss, tmux = _make_session()
    # Touch lock BEFORE connect/send so the very first delivery attempt
    # hits the deferral.
    lock_path = tmp_path / f"{ss.agent_name}.lock"
    lock_path.write_text("")

    await ss.connect()
    await ss.send("hi", platform="t", chat_id="c", message_id="m1")

    # Give the worker several scheduler ticks to (a) get() the turn,
    # (b) hit the deferral, (c) loop a few times still seeing the lock.
    for _ in range(20):
        await asyncio.sleep(0.005)
    # Pre-unlock invariants: paste must NOT have fired, queue is empty
    # (turn is held in _inflight_turn, not in the queue), and the
    # inflight slot holds the turn.
    assert tmux.paste_text.await_count == 0, (
        "Murzik #522 round-1: paste must not fire while context lock "
        "is held — pre-fix this was the silent-drop window"
    )
    assert ss._message_queue.qsize() == 0
    assert ss._inflight_turn is not None
    assert ss._inflight_turn.prompt == "hi"

    # Release the lock. Within a handful of backoff cycles the worker
    # should re-attempt _deliver_turn and paste the SAME turn.
    lock_path.unlink()
    for _ in range(50):
        await asyncio.sleep(0.005)
        if tmux.paste_text.await_count >= 1:
            break

    assert tmux.paste_text.await_count == 1, (
        "Murzik #522 round-1 fix: same turn must re-paste once the lock "
        "is released (pre-fix paste_count stayed at 0 forever)"
    )
    args, _ = tmux.paste_text.call_args
    assert args[0] == "hi"

    # Drive the turn to completion so the worker clears _inflight_turn.
    await ss._handle_turn_complete(TurnResponse(text="ok", stop_reason="end_turn"))
    for _ in range(20):
        await asyncio.sleep(0)
        if ss._inflight_turn is None:
            break
    assert ss._inflight_turn is None

    await ss.disconnect()


# Issue #525 removed the idle-prompt readiness gate (#522 + #524) along
# with its worker-loop escalation paths. The deleted tests covered:
# - test_idle_prompt_timeout_retries_then_force_restarts
# - test_rate_limit_wait_preserves_turn_without_force_restart
# - test_rate_limit_wait_budget_trips_dead_with_turn_preserved
# - test_idle_prompt_preserves_turn_across_force_restart
# - test_pre_first_turn_restart_circuit_breaker_marks_dead
# Splash-state paste is handled by `paste_text` (splash dismisses on
# input focus); no readiness gate is needed.


# ──────────────────────────────────────────────────────────────────────────
# Task #93: tool-use tracking via PreToolUse / PostToolUse hooks
# ──────────────────────────────────────────────────────────────────────────


def _make_session_with_analytics(
    *, agent_name: str = "dymok"
) -> tuple[TmuxSession, MagicMock, MagicMock]:
    """Build a TmuxSession with a mocked analytics_store + stream callback.

    Returns (session, analytics_mock, events_list). The session is in
    CONNECTED state so the methods under test (which don't touch state)
    are reachable without going through cold-start.
    """
    cfg = StreamingSessionConfig(
        agent_name=agent_name,
        working_dir="/tmp/tmux-session-test",
    )
    tmux = _make_mock_tmux()
    analytics = MagicMock()
    analytics.start_tool_call = MagicMock()
    analytics.finish_tool_call = MagicMock()
    events: list[dict] = []

    async def stream_cb(event: dict) -> None:
        events.append(event)

    ss = TmuxSession(
        cfg,
        tmux_control=tmux,
        analytics_store=analytics,
        stream_event_callback=stream_cb,
    )
    return ss, analytics, events


@pytest.mark.asyncio
async def test_record_tool_use_start_emits_event_and_opens_analytics() -> None:
    """``record_tool_use_start`` must update activity, open an analytics
    row keyed by tool_use_id, and emit a ``tool_use_start`` stream event
    matching SDK parity (task #93).
    """
    ss, analytics, events = _make_session_with_analytics()

    await ss.record_tool_use_start(
        tool_use_id="toolu_abc123",
        tool_name="mcp__pinky-self__send_to_agent",
        tool_input={"to": "dymok", "message": "hi"},
    )

    # Activity surfaced for live status consumers.
    assert ss._current_activity  # non-empty human-readable description

    # Analytics row opened with the correct key + PII-safe arg_keys.
    # ``description`` is now also persisted so the chip strip can be
    # rebuilt after a chat-page refresh — it's the same human-readable
    # label already in the live SSE event.
    analytics.start_tool_call.assert_called_once()
    kwargs = analytics.start_tool_call.call_args.kwargs
    assert kwargs["agent_name"] == "dymok"
    assert kwargs["tool_call_key"] == "toolu_abc123"
    assert kwargs["tool_name"] == "mcp__pinky-self__send_to_agent"
    assert kwargs["tool_namespace"] == "pinky-self"
    meta = kwargs["metadata"]
    assert meta["arg_keys"] == ["message", "to"]
    assert meta["description"]  # non-empty human-readable label

    # Stream event emitted with the expected shape.
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "tool_use_start"
    assert evt["agent_name"] == "dymok"
    assert evt["tool_use_id"] == "toolu_abc123"
    assert evt["tool_name"] == "mcp__pinky-self__send_to_agent"
    assert evt["tool_namespace"] == "pinky-self"
    assert evt["arg_keys"] == ["message", "to"]
    assert evt["description"]  # non-empty


@pytest.mark.asyncio
async def test_record_tool_use_start_noops_on_empty_tool_name() -> None:
    """Defensive: an empty tool_name should not open analytics or emit."""
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_start(
        tool_use_id="x", tool_name="", tool_input={}
    )
    analytics.start_tool_call.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_record_tool_use_start_synthesizes_key_when_missing() -> None:
    """Some Claude Code event flows omit tool_use_id. We must still open
    an analytics row using a synthetic key so the call shows up in the
    feed instead of being silently dropped.
    """
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_start(
        tool_use_id="", tool_name="Bash", tool_input={"command": "ls"}
    )
    analytics.start_tool_call.assert_called_once()
    key = analytics.start_tool_call.call_args.kwargs["tool_call_key"]
    assert key.startswith("Bash_")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_record_tool_use_finish_emits_finish_and_closes_analytics() -> None:
    """``record_tool_use_finish`` must close the analytics row keyed by
    tool_use_id with success=True (when not is_error) and emit a
    ``tool_use_finish`` stream event with a capped result_preview.
    """
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_finish(
        tool_use_id="toolu_abc123",
        tool_name="Bash",
        is_error=False,
        tool_response={"content": [{"type": "text", "text": "ok"}]},
    )

    analytics.finish_tool_call.assert_called_once()
    kwargs = analytics.finish_tool_call.call_args.kwargs
    assert kwargs["tool_call_key"] == "toolu_abc123"
    assert kwargs["success"] is True
    assert kwargs["error_type"] == ""
    # ``result_preview`` now lands in metadata so the chip strip can
    # surface the truncated tool output after a page refresh. Same
    # 200-char cap as the live SSE event.
    fmeta = kwargs["metadata"] or {}
    assert "result_preview" in fmeta
    assert fmeta["result_preview"]
    assert len(fmeta["result_preview"]) <= 200

    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "tool_use_finish"
    assert evt["tool_use_id"] == "toolu_abc123"
    assert evt["is_error"] is False
    assert evt["result_preview"]  # non-empty snippet
    assert len(evt["result_preview"]) <= 200


@pytest.mark.asyncio
async def test_record_tool_use_finish_marks_error_when_is_error() -> None:
    """When ``is_error=True``, analytics gets success=False + error_type
    and the stream event carries the flag through for UI consumers.
    """
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_finish(
        tool_use_id="toolu_err",
        tool_name="Bash",
        is_error=True,
        tool_response="command not found: blorp",
    )

    analytics.finish_tool_call.assert_called_once()
    kwargs = analytics.finish_tool_call.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["error_type"] == "tool_error"

    assert events[0]["is_error"] is True


@pytest.mark.asyncio
async def test_record_tool_use_finish_skips_analytics_without_id() -> None:
    """No tool_use_id → skip analytics close (no row to close), but
    still emit the stream event so consumers see the finish signal.
    """
    ss, analytics, events = _make_session_with_analytics()
    await ss.record_tool_use_finish(
        tool_use_id="",
        tool_name="Bash",
        is_error=False,
        tool_response="ok",
    )
    analytics.finish_tool_call.assert_not_called()
    assert len(events) == 1
    assert events[0]["type"] == "tool_use_finish"


@pytest.mark.asyncio
async def test_record_tool_use_finish_caps_huge_response_at_200() -> None:
    """``result_preview`` must be capped to match SDK's 200-char cap;
    tool responses can be enormous (file contents, search results).
    """
    ss, analytics, events = _make_session_with_analytics()
    huge = "x" * 5000
    await ss.record_tool_use_finish(
        tool_use_id="toolu_huge",
        tool_name="Read",
        is_error=False,
        tool_response=huge,
    )
    assert len(events[0]["result_preview"]) == 200


@pytest.mark.asyncio
async def test_record_tool_use_start_marks_inflight() -> None:
    """#731: record_tool_use_start adds the tool_use_id to the in-flight set so
    the watchdog can credit a long foreground call as liveness."""
    ss, _analytics, _events = _make_session_with_analytics()
    await ss.record_tool_use_start(
        tool_use_id="toolu_fg",
        tool_name="Bash",
        tool_input={"command": "gh run watch 123 --exit-status"},
    )
    assert "toolu_fg" in ss._inflight_tool_calls


@pytest.mark.asyncio
async def test_record_tool_use_finish_clears_inflight() -> None:
    """#731: record_tool_use_finish removes the tool_use_id so the watchdog
    stops crediting it once the tool returns."""
    ss, _analytics, _events = _make_session_with_analytics()
    await ss.record_tool_use_start(
        tool_use_id="toolu_fg", tool_name="Bash", tool_input={"command": "sleep 1"}
    )
    assert "toolu_fg" in ss._inflight_tool_calls
    await ss.record_tool_use_finish(
        tool_use_id="toolu_fg", tool_name="Bash", is_error=False, tool_response="done"
    )
    assert "toolu_fg" not in ss._inflight_tool_calls


@pytest.mark.asyncio
async def test_record_tool_use_start_untracked_without_id() -> None:
    """#731: a synthetic-key tool call (no tool_use_id) isn't tracked for the
    watchdog — analytics still opens a row, but with no id there'd be nothing
    to clear on finish, so we avoid a permanent in-flight leak."""
    ss, _analytics, _events = _make_session_with_analytics()
    await ss.record_tool_use_start(
        tool_use_id="", tool_name="Bash", tool_input={"command": "ls"}
    )
    assert ss._inflight_tool_calls == {}


# ──────────────────────────────────────────────────────────────────────────
# Pane snapshot (read-only viewer endpoint)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pane_snapshot_returns_stdout_with_escapes() -> None:
    """``get_pane_snapshot`` calls capture_pane with escapes=True and
    returns the raw stdout (ANSI escapes preserved for xterm.js)."""
    ss, tmux = _make_session()
    tmux.capture_pane = AsyncMock(return_value=TmuxCommandResult(
        returncode=0,
        stdout="\x1b[1;32mhello\x1b[0m",
        stderr="",
    ))
    out = await ss.get_pane_snapshot(lines=150)
    assert out == "\x1b[1;32mhello\x1b[0m"
    # Verify escapes flag was passed through.
    tmux.capture_pane.assert_awaited_once()
    kwargs = tmux.capture_pane.await_args.kwargs
    assert kwargs.get("escapes") is True
    assert kwargs.get("lines") == 150


@pytest.mark.asyncio
async def test_get_pane_snapshot_returns_empty_on_tmux_failure() -> None:
    """A non-zero return from tmux capture-pane (transient server blip,
    session went away during a frame) must NOT raise — the SSE generator
    just emits an empty frame and the next one will recover."""
    ss, tmux = _make_session()
    tmux.capture_pane = AsyncMock(return_value=TmuxCommandResult(
        returncode=1, stdout="", stderr="lost server",
    ))
    out = await ss.get_pane_snapshot()
    assert out == ""


@pytest.mark.asyncio
async def test_get_pane_snapshot_swallows_subprocess_exception() -> None:
    """If capture_pane itself raises (e.g. the tmux binary disappeared
    mid-flight), the snapshot helper must return "" rather than letting
    the exception escape into the SSE generator and kill the stream."""
    ss, tmux = _make_session()
    tmux.capture_pane = AsyncMock(side_effect=RuntimeError("tmux gone"))
    out = await ss.get_pane_snapshot()
    assert out == ""


# ──────────────────────────────────────────────────────────────────────────
# Context-budget watchdog (task #95)
#
# Per-turn usage accumulation + ``context_usage`` SSE emission +
# ``restart_nudge`` when crossing the agent's restart_threshold_pct.
# Tmux agents need this for parity with SDK agents — without it they're
# blind to their own context window and can't make their own /compact /
# restart / sleep decisions.
# ──────────────────────────────────────────────────────────────────────────


def _turn_response(*, input_tokens: int, output_tokens: int = 0,
                   cache_read: int = 0, cache_write: int = 0,
                   text: str = "ok") -> TurnResponse:
    """Build a TurnResponse with a realistic usage block.

    Mirrors the Claude Code transcript schema: cache fields use the
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` names
    rather than the SDK's shortened forms. The watchdog accepts either.
    """
    return TurnResponse(
        text=text,
        stop_reason="end_turn",
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
        },
        duration_ms=100,
        assistant_entry_count=1,
        tool_uses=[],
    )


@pytest.mark.asyncio
async def test_record_turn_usage_accumulates_across_turns() -> None:
    """Cumulative token counts on ``self.usage`` grow turn over turn,
    rather than getting overwritten. Without this, the chat UI's
    context-percentage gauge sees only the latest turn's deltas and
    can't show "approaching compaction"."""
    ss, _ = _make_session_with_response_cb()
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(
        _turn_response(input_tokens=1000, output_tokens=100, cache_read=500)
    )
    _seed_inflight(ss)  # #560 — second turn
    await ss._handle_turn_complete(
        _turn_response(input_tokens=2000, output_tokens=200, cache_write=300)
    )
    assert ss.usage.input_tokens == 3000
    assert ss.usage.output_tokens == 300
    assert ss.usage.cache_read_tokens == 500
    assert ss.usage.cache_write_tokens == 300
    assert ss.usage.total_turns == 2


@pytest.mark.asyncio
async def test_record_turn_usage_tolerates_schema_drift() -> None:
    """A usage block with unexpected shapes (None values, strings,
    missing keys) must not crash the tailer. Token visibility is
    best-effort; the tailer's invariant is to keep running."""
    ss, _ = _make_session_with_response_cb()
    _seed_inflight(ss)  # #560
    drift = TurnResponse(
        text="ok",
        stop_reason="end_turn",
        usage={"input_tokens": None, "output_tokens": "definitely a number"},
        duration_ms=50,
        assistant_entry_count=1,
        tool_uses=[],
    )
    # Must not raise.
    await ss._handle_turn_complete(drift)
    # Should still bump turn count + record stop_reason even if tokens drift.
    assert ss.usage.total_turns == 1
    assert ss.usage.last_stop_reason == "end_turn"


# ──────────────────────────────────────────────────────────────────────────
# #648 — per-turn analytics + cost forwarding (parity with SDK path)
# ──────────────────────────────────────────────────────────────────────────


def _make_usage_session(
    *, analytics=None, cost_cb=None, model: str = "claude-opus-4-8"
) -> TmuxSession:
    """Build a CONNECTED TmuxSession wired with the callbacks #648 forwards.

    Self-contained (uses only the ctor + the mock-tmux primitive) so it
    doesn't depend on the optional kwargs of other test helpers.
    """
    cfg = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-session-test",
        model=model,
    )
    ss = TmuxSession(
        cfg,
        tmux_control=_make_mock_tmux(),
        analytics_store=analytics,
        cost_callback=cost_cb,
    )
    ss._skip_wake_prompt_for_tests = True
    ss._state_machine._state = SessionState.CONNECTED
    return ss


def _usage_turn_response(
    *,
    model: str = "claude-opus-4-8",
    input_tokens: int = 10_000,
    output_tokens: int = 500,
    cache_read: int = 2_000,
    cache_write: int = 0,
    text: str = "ok",
) -> TurnResponse:
    return TurnResponse(
        text=text,
        stop_reason="end_turn",
        model=model,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
        },
        duration_ms=100,
        assistant_entry_count=1,
        tool_uses=[],
    )


@pytest.mark.asyncio
async def test_turn_complete_logs_analytics_row() -> None:
    """A completed turn must upsert an ``analytics_turn_usage`` row with
    the SDK-shaped kwargs so tmux agents show up on the live Analytics
    page (#648). ``cached_input_tokens`` is cache-READ only."""
    analytics = MagicMock()
    ss = _make_usage_session(analytics=analytics)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_usage_turn_response())

    analytics.log_turn_usage.assert_called_once()
    kwargs = analytics.log_turn_usage.call_args.kwargs
    assert kwargs["session_id"] == ss.id
    assert kwargs["agent_name"] == "dymok"
    assert kwargs["turn_seq"] == 1
    assert kwargs["provider"] == "anthropic"
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["input_tokens"] == 10_000
    assert kwargs["output_tokens"] == 500
    assert kwargs["cached_input_tokens"] == 2_000
    assert kwargs["error"] is False


@pytest.mark.asyncio
async def test_turn_complete_fires_cost_callback_with_computed_cost() -> None:
    """tmux has no per-turn dollar figure from the transcript, so the
    cost must be COMPUTED from token counts and forwarded via
    ``cost_callback`` (signature: agent, cost, input, output, handle)."""
    cost_cb = MagicMock()
    ss = _make_usage_session(cost_cb=cost_cb)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_usage_turn_response())

    cost_cb.assert_called_once()
    args = cost_cb.call_args.args
    assert args[0] == "dymok"
    # 10000*5 + 500*25 + 2000*0.5 (all /1e6) = 0.0635 (no cache-write).
    expected = 10_000 / 1e6 * 5 + 500 / 1e6 * 25 + 2_000 / 1e6 * 0.5
    assert args[1] == pytest.approx(expected)
    assert args[2] == 10_000  # input_tokens
    assert args[3] == 500  # output_tokens
    assert args[4] == ss.resume_handle
    # Lifetime cost accumulates onto session usage.
    assert ss.usage.total_cost_usd == pytest.approx(expected)


@pytest.mark.asyncio
async def test_turn_seq_increments_across_turns() -> None:
    """Each turn upserts under a monotonic turn_seq (the tmux analog of
    the SDK's ``_turn_seq``) so rows don't clobber each other."""
    analytics = MagicMock()
    ss = _make_usage_session(analytics=analytics)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_usage_turn_response())
    _seed_inflight(ss)
    await ss._handle_turn_complete(_usage_turn_response())
    seqs = [c.kwargs["turn_seq"] for c in analytics.log_turn_usage.call_args_list]
    assert seqs == [1, 2]


@pytest.mark.asyncio
async def test_model_falls_back_to_config_when_transcript_blank() -> None:
    """If the transcript carried no model field, price under the
    configured model rather than dropping the cost to zero."""
    cost_cb = MagicMock()
    analytics = MagicMock()
    ss = _make_usage_session(
        cost_cb=cost_cb, analytics=analytics, model="claude-sonnet-4-6"
    )
    _seed_inflight(ss)
    # response.model empty → fall back to config.model.
    await ss._handle_turn_complete(_usage_turn_response(model=""))
    assert analytics.log_turn_usage.call_args.kwargs["model"] == "claude-sonnet-4-6"
    # Sonnet input rate $3: 10000/1e6*3 + 500/1e6*15 + 2000/1e6*0.3
    expected = 10_000 / 1e6 * 3 + 500 / 1e6 * 15 + 2_000 / 1e6 * 0.3
    assert cost_cb.call_args.args[1] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_unknown_model_costs_zero_but_still_logs_usage() -> None:
    """An unpriced model records $0 cost but STILL logs the token row —
    analytics visibility must not depend on having a rate."""
    cost_cb = MagicMock()
    analytics = MagicMock()
    ss = _make_usage_session(cost_cb=cost_cb, analytics=analytics)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_usage_turn_response(model="some-future-model"))
    assert cost_cb.call_args.args[1] == 0.0
    analytics.log_turn_usage.assert_called_once()
    assert analytics.log_turn_usage.call_args.kwargs["input_tokens"] == 10_000


@pytest.mark.asyncio
async def test_no_callbacks_is_safe() -> None:
    """A session with neither analytics_store nor cost_callback must
    complete turns without error (the common pre-#648 wiring)."""
    ss = _make_usage_session()
    _seed_inflight(ss)
    # Must not raise.
    await ss._handle_turn_complete(_usage_turn_response())
    assert ss.usage.total_turns == 1


@pytest.mark.asyncio
async def test_analytics_failure_is_swallowed() -> None:
    """A flaky analytics_store must not break turn completion / delivery —
    cost/analytics are side telemetry, not correctness."""
    analytics = MagicMock()
    analytics.log_turn_usage = MagicMock(side_effect=RuntimeError("db locked"))
    ss = _make_usage_session(analytics=analytics)
    _seed_inflight(ss)
    # Must not raise.
    await ss._handle_turn_complete(_usage_turn_response())
    assert ss.usage.total_turns == 1


@pytest.mark.asyncio
async def test_cost_callback_failure_is_swallowed() -> None:
    """A raising cost_callback must not crash the tailer."""
    cost_cb = MagicMock(side_effect=RuntimeError("boom"))
    ss = _make_usage_session(cost_cb=cost_cb)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_usage_turn_response())
    assert ss.usage.total_turns == 1


# ── #860 — transport-truthful provider attribution + usage normalization ──


def _make_usage_session_of(
    cls, *, analytics=None, cost_cb=None, model: str = "claude-opus-4-8"
) -> TmuxSession:
    """_make_usage_session for a TmuxSession SUBCLASS (transport variants)."""
    cfg = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-session-test",
        model=model,
    )
    ss = cls(
        cfg,
        tmux_control=_make_mock_tmux(),
        analytics_store=analytics,
        cost_callback=cost_cb,
    )
    ss._skip_wake_prompt_for_tests = True
    ss._state_machine._state = SessionState.CONNECTED
    return ss


@pytest.mark.asyncio
async def test_analytics_provider_comes_from_class_attr() -> None:
    """#860: the provider on analytics rows must read _ANALYTICS_PROVIDER —
    not a hardcoded literal — so transport subclasses (CodexTmuxSession)
    attribute their turns truthfully. The hardcoded "anthropic" landed every
    codex turn as anthropic/gpt-*, which no pricing row can match ($0)."""

    class _OtherTransport(TmuxSession):
        _ANALYTICS_PROVIDER = "other_provider"

    analytics = MagicMock()
    ss = _make_usage_session_of(_OtherTransport, analytics=analytics)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_usage_turn_response())
    assert analytics.log_turn_usage.call_args.kwargs["provider"] == "other_provider"


def test_base_normalize_turn_usage_is_identity() -> None:
    """The Claude transport already emits the daemon's disjoint convention,
    so the base hook must pass the dict through untouched (same object)."""
    u = {"input_tokens": 10_000, "cache_read_input_tokens": 2_000}
    assert TmuxSession._normalize_turn_usage(u) is u


@pytest.mark.asyncio
async def test_normalize_hook_runs_before_all_consumers() -> None:
    """#860: _normalize_turn_usage must rewrite response.usage ONCE, before
    ANY consumer — accumulation (SessionUsage), pricing, and analytics all
    see the normalized dict. A partial conversion spread across consumers is
    how the cached split got silently dropped in the first place."""

    class _NormalizingTransport(TmuxSession):
        @staticmethod
        def _normalize_turn_usage(u: dict) -> dict:
            out = dict(u)
            out["input_tokens"] = 7_777
            return out

    analytics = MagicMock()
    cost_cb = MagicMock()
    ss = _make_usage_session_of(
        _NormalizingTransport, analytics=analytics, cost_cb=cost_cb
    )
    _seed_inflight(ss)
    await ss._handle_turn_complete(_usage_turn_response(input_tokens=999))
    # Analytics row: normalized, not the raw 999.
    assert analytics.log_turn_usage.call_args.kwargs["input_tokens"] == 7_777
    # SessionUsage accumulation: normalized.
    assert ss.usage.input_tokens == 7_777
    # Cost: priced off the normalized count (opus $5/Mtok input).
    expected = 7_777 / 1e6 * 5 + 500 / 1e6 * 25 + 2_000 / 1e6 * 0.5
    assert cost_cb.call_args.args[1] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_emits_context_usage_event_with_sdk_shape(monkeypatch) -> None:
    """``context_usage`` SSE event must carry the SDK-compatible fields
    so Chat.svelte's session-info card renders for tmux agents the same
    way it does for SDK ones (totalTokens / maxTokens / categories)."""
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(
        _turn_response(input_tokens=10_000, output_tokens=500, cache_read=2_000)
    )

    ctx_events = [e for e in events if e["type"] == "context_usage"]
    assert len(ctx_events) == 1
    evt = ctx_events[0]
    assert evt["agent_name"] == "dymok"
    assert evt["totalTokens"] == 12_500
    # Effective max for a 200K-raw model = 200K - 33K autocompact
    # buffer (Claude Code reserves room for /compact to fire before
    # the API rejects for context exhaustion). ``rawMaxTokens`` keeps
    # the raw cap visible for any UI that wants both.
    assert evt["maxTokens"] == 200_000 - 33_000
    assert evt["rawMaxTokens"] == 200_000
    assert evt["percentage"] == round(12_500 / (200_000 - 33_000) * 100, 1)
    # Category breakdown — coarse for tmux, but matches the SDK's shape.
    names = {c["name"] for c in evt["categories"]}
    assert names == {"Input", "Output", "Cache read", "Cache write"}


@pytest.mark.asyncio
async def test_get_context_info_returns_sdk_shape() -> None:
    """``get_context_info()`` is the synchronous fallback that
    ``api._streaming_context_info`` calls when no ``_client`` is
    present (the tmux path). Shape must match what the existing
    ``/streaming/status`` consumer expects."""
    ss, _ = _make_session_with_response_cb()
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(
        _turn_response(input_tokens=5_000, output_tokens=200)
    )
    info = ss.get_context_info()
    # camelCase + snake_case both populated — different call sites read
    # different conventions.
    assert info["totalTokens"] == 5_200
    assert info["total_tokens"] == 5_200
    assert info["maxTokens"] == info["max_tokens"]
    assert info["percentage"] >= 0.0
    assert isinstance(info["categories"], list)
    assert info["mcpTools"] == info["mcp_tools"] == []


@pytest.mark.asyncio
async def test_context_used_pct_property_for_heartbeat_reconciler() -> None:
    """#745: the scheduler's heartbeat reconciler reads
    ``context_used_pct`` via ``getattr(session, ..., 0.0)`` across all
    transports. TmuxSession didn't define it, so every reconciled
    heartbeat for a tmux agent recorded 0.0% while the real number sat
    in ``get_context_info()``. The property must agree with the
    percentage that the status endpoints already serve."""
    ss, _ = _make_session_with_response_cb()
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(
        _turn_response(input_tokens=5_000, output_tokens=200)
    )
    pct = ss.context_used_pct
    assert pct > 0.0
    assert pct == ss.get_context_info()["percentage"]


@pytest.mark.asyncio
async def test_raw_max_tokens_for_1m_vs_200k_model() -> None:
    """``_raw_max_tokens_for_model`` returns the raw cap unaltered —
    1M for ``_1M_MODELS`` members, 200k otherwise. Without this split,
    tmux agents on Opus 4.7 would peg their context gauge at 20% of
    real capacity."""
    from pinky_daemon import streaming_session as _ss_mod
    # Pin a known 1M model so the test doesn't drift if the DB-backed
    # set changes underneath us.
    original = set(_ss_mod._1M_MODELS)
    try:
        _ss_mod._1M_MODELS = {"claude-opus-4-7"}
        cfg = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-session-test",
            model="claude-opus-4-7",
        )
        tmux = _make_mock_tmux()
        ss = TmuxSession(cfg, tmux_control=tmux)
        assert ss._raw_max_tokens_for_model() == 1_000_000

        cfg2 = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-session-test",
            model="claude-haiku-4-5",
        )
        ss2 = TmuxSession(cfg2, tmux_control=tmux)
        assert ss2._raw_max_tokens_for_model() == 200_000
    finally:
        _ss_mod._1M_MODELS = original


@pytest.mark.asyncio
async def test_max_tokens_subtracts_autocompact_buffer(monkeypatch) -> None:
    """Effective cap = raw - 33K. Critical: Claude Code's ``/compact``
    fires before the API rejects for context exhaustion, so the gauge
    must measure against effective max not raw — otherwise we
    under-report by ~16 points on the 200K window and the
    restart-nudge fires too late."""
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)

    from pinky_daemon import streaming_session as _ss_mod
    original = set(_ss_mod._1M_MODELS)
    try:
        _ss_mod._1M_MODELS = {"claude-opus-4-7"}

        cfg = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-session-test",
            model="claude-haiku-4-5",
        )
        ss = TmuxSession(cfg, tmux_control=_make_mock_tmux())
        # 200K raw → 167K effective.
        assert ss._max_tokens_for_model() == 200_000 - 33_000

        cfg_big = StreamingSessionConfig(
            agent_name="dymok",
            working_dir="/tmp/tmux-session-test",
            model="claude-opus-4-7",
        )
        ss_big = TmuxSession(cfg_big, tmux_control=_make_mock_tmux())
        # 1M raw → 967K effective. Per SDK source the autocompact
        # buffer is a fixed 33K, not a proportional fraction.
        assert ss_big._max_tokens_for_model() == 1_000_000 - 33_000
    finally:
        _ss_mod._1M_MODELS = original


@pytest.mark.asyncio
async def test_autocompact_override_env_sets_effective_cap(monkeypatch) -> None:
    """``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` is Claude Code's own env
    var — the percentage at which autocompact triggers. We interpret
    it as effective-cap percentage of raw, matching the docs and the
    SDK behavior."""
    cfg = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-session-test",
        model="claude-haiku-4-5",
    )
    tmux = _make_mock_tmux()
    ss = TmuxSession(cfg, tmux_control=tmux)

    # 85% → effective = 0.85 * 200K = 170K.
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "85")
    assert ss._max_tokens_for_model() == 170_000

    # 100 → autocompact disabled, effective == raw.
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "100")
    assert ss._max_tokens_for_model() == 200_000

    # Malformed value falls back to the default 33K buffer.
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "not-a-number")
    assert ss._max_tokens_for_model() == 200_000 - 33_000

    # Empty / zero values also fall back (zero would imply effective=0,
    # which would be a divide-by-zero waiting to happen).
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "0")
    assert ss._max_tokens_for_model() == 200_000 - 33_000


@pytest.mark.asyncio
async def test_get_context_info_exposes_raw_and_effective_caps(monkeypatch) -> None:
    """``get_context_info`` returns both ``maxTokens`` (effective) and
    ``rawMaxTokens`` (raw cap) for SDK parity — matches the shape of
    ``ContextUsageResponse`` so the same frontend can render tmux and
    SDK sessions interchangeably. Snake-case aliases mirror the
    existing camelCase fields."""
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)

    cfg = StreamingSessionConfig(
        agent_name="dymok",
        working_dir="/tmp/tmux-session-test",
        model="claude-haiku-4-5",
    )
    ss = TmuxSession(cfg, tmux_control=_make_mock_tmux())

    info = ss.get_context_info()

    assert info["rawMaxTokens"] == 200_000
    assert info["maxTokens"] == 200_000 - 33_000
    # Snake-case aliases populated identically.
    assert info["raw_max_tokens"] == info["rawMaxTokens"]
    assert info["max_tokens"] == info["maxTokens"]


@pytest.mark.asyncio
async def test_restart_nudge_fires_when_crossing_threshold() -> None:
    """When per-turn context window crosses ``restart_threshold_pct``,
    emit a ``restart_nudge`` SSE event so the chat UI can light up the
    "you should /compact" indicator. SDK agents have had this for
    months via the SDK's context callbacks; tmux now matches.

    Each Claude Code turn re-sends the full prior conversation, so the
    LAST turn's ``input_tokens`` already reflects the current window —
    no summing needed. Threshold check rides off ``last_usage``.
    """
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)

    # Stub the threshold so we can hit it deterministically with small
    # token counts (default is 80%, which is 160k tokens of 200k — too
    # big to push through in a unit test usage block).
    ss._restart_threshold_pct = lambda: 50.0

    # Below threshold: no nudge. (50k / 200k = 25%.)
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(_turn_response(input_tokens=50_000))
    assert not [e for e in events if e["type"] == "restart_nudge"]

    # Cross threshold: nudge fires. (110k / 200k = 55%.)
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(_turn_response(input_tokens=110_000))
    nudges = [e for e in events if e["type"] == "restart_nudge"]
    assert len(nudges) == 1
    assert nudges[0]["percentage"] >= 50.0
    assert nudges[0]["threshold_pct"] == 50.0


@pytest.mark.asyncio
async def test_restart_nudge_does_not_refire_while_above_threshold() -> None:
    """Once above threshold, subsequent turns must not fire the nudge
    every time. The chat UI gets one signal per crossing, not a
    cascade of identical events while context stays above the bar."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._restart_threshold_pct = lambda: 50.0

    # Three turns, all above threshold — only one nudge total.
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(_turn_response(input_tokens=110_000))
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=130_000))
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=140_000))

    nudges = [e for e in events if e["type"] == "restart_nudge"]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_restart_nudge_rearms_after_drop_below_threshold() -> None:
    """After a /compact, the per-turn input_tokens drops sharply (the
    conversation history was compressed). The next sub-threshold turn
    re-arms the latch; a subsequent above-threshold turn fires a fresh
    nudge."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._restart_threshold_pct = lambda: 50.0

    # Cross threshold (above 50% of 200k).
    _seed_inflight(ss)  # #560
    await ss._handle_turn_complete(_turn_response(input_tokens=110_000))
    assert ss._restart_nudge_fired is True

    # Simulate post-compact: a turn with low input_tokens. The latch
    # resets because the window measurement is now below threshold.
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=5_000))
    assert ss._restart_nudge_fired is False

    # Cross again — fresh nudge.
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=110_000))
    nudges = [e for e in events if e["type"] == "restart_nudge"]
    assert len(nudges) == 2


# ──────────────────────────────────────────────────────────────────────────
# Soft context-watermark nudge (#614). Distinct from the restart_nudge
# above: when usage first crosses the agent's *soft* threshold (well below
# the hard restart_threshold_pct), inject a one-time reminder INTO the
# agent's REPL telling it to checkpoint + context_restart at a natural
# break. Fires once per crossing; never fires at/above the hard line.
# ──────────────────────────────────────────────────────────────────────────


def _drain_queue(ss) -> list:
    drained = []
    while not ss._message_queue.empty():
        drained.append(ss._message_queue.get_nowait())
    return drained


@pytest.mark.asyncio
async def test_soft_nudge_fires_and_injects_when_crossing_soft_threshold() -> None:
    """Crossing the soft threshold (but staying below the hard one) fires a
    ``context_nudge_soft`` SSE event AND enqueues an internal prompt into the
    REPL via the wake/internal-prompt path."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._state_machine._state = SessionState.CONNECTED
    ss._restart_threshold_pct = lambda: 50.0
    ss._soft_nudge_threshold_pct = lambda: 20.0

    # Below soft: nothing. (5k / 167k ~ 3%.)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=5_000))
    assert not [e for e in events if e["type"] == "context_nudge_soft"]
    assert _drain_queue(ss) == []

    # Cross soft, stay below hard. (50k / 167k ~ 30%.)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=50_000))

    soft_events = [e for e in events if e["type"] == "context_nudge_soft"]
    assert len(soft_events) == 1
    assert soft_events[0]["threshold_pct"] == 20.0
    assert soft_events[0]["percentage"] >= 20.0
    assert ss._soft_nudge_fired is True
    # The hard restart_nudge must NOT have fired (we're below 50%).
    assert not [e for e in events if e["type"] == "restart_nudge"]

    # Exactly one internal prompt was enqueued for the REPL.
    queued = _drain_queue(ss)
    assert len(queued) == 1
    assert queued[0].internal is True
    assert queued[0].reason == "context_nudge_soft"
    assert "context_restart" in queued[0].prompt


@pytest.mark.asyncio
async def test_soft_nudge_does_not_fire_at_or_above_hard_threshold() -> None:
    """If usage is already at/above the hard threshold, the hard path owns
    the response — the soft nudge must not also fire or inject."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._state_machine._state = SessionState.CONNECTED
    ss._restart_threshold_pct = lambda: 50.0
    ss._soft_nudge_threshold_pct = lambda: 20.0

    # Straight past both thresholds. (100k / 167k ~ 60%.)
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=100_000))

    assert [e for e in events if e["type"] == "restart_nudge"]
    assert not [e for e in events if e["type"] == "context_nudge_soft"]
    assert ss._soft_nudge_fired is False
    # Post-#618: crossing the hard line ALSO enqueues the autorestart nudge
    # (reason="context_autorestart_nudge"), so the queue is no longer empty
    # here. The #614 invariant being pinned is narrower — "hard wins, soft
    # does not also act" — so assert specifically that no SOFT nudge turn was
    # injected, while tolerating the expected autorestart turn.
    queued = _drain_queue(ss)
    assert not [t for t in queued if t.reason == "context_nudge_soft"]


@pytest.mark.asyncio
async def test_soft_nudge_does_not_refire_while_above_soft() -> None:
    """One signal per crossing — staying above the soft line on later turns
    must not re-inject."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._state_machine._state = SessionState.CONNECTED
    ss._restart_threshold_pct = lambda: 80.0
    ss._soft_nudge_threshold_pct = lambda: 20.0

    for tokens in (50_000, 60_000, 70_000):  # all in [20%, 80%)
        _seed_inflight(ss)
        await ss._handle_turn_complete(_turn_response(input_tokens=tokens))

    assert len([e for e in events if e["type"] == "context_nudge_soft"]) == 1
    assert len([t for t in _drain_queue(ss) if t.reason == "context_nudge_soft"]) == 1


@pytest.mark.asyncio
async def test_soft_nudge_rearms_after_drop_below_soft() -> None:
    """After a context_restart drops usage below the soft line, the latch
    re-arms and the next crossing injects a fresh nudge."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._state_machine._state = SessionState.CONNECTED
    ss._restart_threshold_pct = lambda: 80.0
    ss._soft_nudge_threshold_pct = lambda: 20.0

    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=50_000))
    assert ss._soft_nudge_fired is True

    # Post-restart: usage drops below soft → re-arm.
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=5_000))
    assert ss._soft_nudge_fired is False

    # Cross again → fresh injection.
    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=50_000))
    assert len([e for e in events if e["type"] == "context_nudge_soft"]) == 2
    assert len([t for t in _drain_queue(ss) if t.reason == "context_nudge_soft"]) == 2


@pytest.mark.asyncio
async def test_soft_nudge_skipped_when_soft_not_below_hard() -> None:
    """A misconfigured soft threshold that is not strictly below the hard one
    is inert — the gate requires ``soft < hard``."""
    events: list[dict] = []

    async def stream_cb(evt):
        events.append(evt)

    ss, _ = _make_session_with_response_cb(stream_evt=stream_cb)
    ss._state_machine._state = SessionState.CONNECTED
    ss._restart_threshold_pct = lambda: 50.0
    ss._soft_nudge_threshold_pct = lambda: 50.0  # == hard, not below

    _seed_inflight(ss)
    await ss._handle_turn_complete(_turn_response(input_tokens=60_000))  # ~36%
    assert not [e for e in events if e["type"] == "context_nudge_soft"]
    assert _drain_queue(ss) == []


def test_soft_nudge_threshold_resolves_global_default_when_unset() -> None:
    """The resolver returns the per-agent value when positive, else the
    global ``DEFAULT_CONTEXT_NUDGE_THRESHOLD_PCT``."""
    ss, _ = _make_session_with_response_cb()

    # No registry → global default.
    ss._registry = None
    assert ss._soft_nudge_threshold_pct() == tmux_session.DEFAULT_CONTEXT_NUDGE_THRESHOLD_PCT

    # Registry agent with 0 (unset) → global default.
    ss._registry = MagicMock()
    ss._registry.get.return_value = MagicMock(context_nudge_threshold_pct=0.0)
    assert ss._soft_nudge_threshold_pct() == tmux_session.DEFAULT_CONTEXT_NUDGE_THRESHOLD_PCT

    # Registry agent with a positive override → that value.
    ss._registry.get.return_value = MagicMock(context_nudge_threshold_pct=42.0)
    assert ss._soft_nudge_threshold_pct() == 42.0


# ──────────────────────────────────────────────────────────────────────────
# Wake-prompt / internal-prompt path (PR for #543)
# ──────────────────────────────────────────────────────────────────────────


def _turn_response(
    *,
    text: str = "",
    input_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    output_tokens: int = 0,
    thinking: str = "",
    tool_uses: list | None = None,
    stop_reason: str = "end_turn",
) -> TurnResponse:
    return TurnResponse(
        text=text,
        thinking=thinking,
        tool_uses=tool_uses or [],
        stop_reason=stop_reason,
        usage={
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens": output_tokens,
        },
        duration_ms=0,
        assistant_entry_count=1 if text else 0,
    )


class TestInternalPromptBypasses:
    """``_enqueue_internal_prompt`` must NOT mutate the external-stat
    surfaces: no conversation_store user-message append, no
    ``messages_sent`` increment, no ``_inflight_meta`` write.

    These are the contractual guarantees Murzik called out — without
    them, an internal turn would poison external-turn state (Case 1
    regression from PR #496 round-1 surfacing through a new path).
    """

    @pytest.mark.asyncio
    async def test_internal_prompt_does_not_append_to_conversation_store(self):
        conv_store = MagicMock()
        conv_store.append = MagicMock()
        ss, _ = _make_session_with_response_cb(conv_store=conv_store)
        # Force CONNECTED so the enqueue gate passes without running
        # the full connect path.
        ss._state_machine._state = SessionState.CONNECTED

        await ss._enqueue_internal_prompt(
            "wake prompt body",
            reason="wake_new_session",
            wait_for_completion=False,
        )

        conv_store.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_prompt_does_not_increment_messages_sent(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED
        before = ss._stats["messages_sent"]

        await ss._enqueue_internal_prompt(
            "wake prompt body",
            reason="wake_new_session",
            wait_for_completion=False,
        )

        assert ss._stats["messages_sent"] == before, (
            "internal prompts are daemon-side; external-message stats "
            "must stay at the pre-enqueue value"
        )

    @pytest.mark.asyncio
    async def test_internal_prompt_does_not_write_inflight_meta(self):
        """Murzik's regression guard against #496 round-1 Case 1 surfacing
        via the internal-prompt path. An internal turn that wrote routing
        metadata would clobber a back-to-back external turn's routing
        fields.

        #560 retargeting: ``_deliver_turn`` now APPENDS to
        ``_inflight_metas`` (FIFO) instead of overwriting a single dict.
        Internal turns still append with empty meta + ``internal=True``,
        so an external turn dispatched after carries its OWN routing
        in its OWN deque entry — no shared mutable cell to clobber.
        """
        ss, tmux = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        internal_turn = _QueuedTurn(
            prompt="wake prompt body",
            platform="",
            chat_id="",
            message_id="",
            internal=True,
            reason="wake_new_session",
        )

        await ss._deliver_turn(internal_turn)

        # Internal turn appended an entry with EMPTY meta + internal=True.
        # The back-compat ``_inflight_meta`` property reads the oldest
        # entry's meta — which is empty for this internal turn.
        assert ss._inflight_meta == {}, (
            "internal turn wrote routing metadata — would have clobbered "
            "back-to-back external routing pre-#560; under #560 each "
            "entry carries its own dict so this is structurally impossible"
        )
        assert len(ss._inflight_metas) == 1
        assert ss._inflight_metas[0].internal is True
        assert ss._inflight_metas[0].meta == {}

    @pytest.mark.asyncio
    async def test_internal_wake_prompt_does_not_clobber_external_routing(self):
        """The full Case 1 regression guard, post-#560: enqueue an
        internal wake prompt, then an external turn, dispatch both —
        each entry in ``_inflight_metas`` must carry its OWN routing.
        Pre-#560 the single ``_inflight_meta`` cell would have been
        overwritten by whichever turn dispatched last; post-#560 the
        deque holds both side-by-side."""
        ss, tmux = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        # Internal turn first — appends an empty-meta + internal=True entry.
        internal_turn = _QueuedTurn(
            prompt="wake",
            internal=True,
            reason="wake_new_session",
        )
        await ss._deliver_turn(internal_turn)
        assert len(ss._inflight_metas) == 1
        assert ss._inflight_metas[0].internal is True
        assert ss._inflight_metas[0].meta == {}

        # External turn next — appends its own routing dict + internal=False.
        # The internal turn's entry is unaffected.
        external_turn = _QueuedTurn(
            prompt="hello",
            platform="telegram",
            chat_id="42",
            message_id="m1",
        )
        await ss._deliver_turn(external_turn)
        assert len(ss._inflight_metas) == 2
        # FIFO: internal first, external second.
        assert ss._inflight_metas[0].internal is True
        assert ss._inflight_metas[0].meta == {}
        assert ss._inflight_metas[1].internal is False
        assert ss._inflight_metas[1].meta == {
            "platform": "telegram",
            "chat_id": "42",
            "message_id": "m1",
        }


class TestInternalPromptCompletion:
    """The ``wait_for_completion`` semantic. Without it, PR B (idle_sleep
    parity) would paste a "please save state" instruction and kill the
    pane before the agent could honor it. This contract is the lifecycle
    primitive Murzik flagged as critical."""

    @pytest.mark.asyncio
    async def test_wait_for_completion_false_returns_immediately(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED
        result = await ss._enqueue_internal_prompt(
            "wake",
            reason="wake_new_session",
            wait_for_completion=False,
        )
        # When wait=False, the helper returns the completion_event (or
        # None if the caller doesn't need to observe). Per implementation
        # it returns None because we don't construct the event in
        # fire-and-forget mode. This pins that contract.
        assert result is None or isinstance(result, asyncio.Event)
        # The internal turn is in the queue.
        assert ss._message_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_wait_for_completion_true_blocks_until_event_fires(self):
        """The caller awaits the completion event before returning. The
        worker sets it from ``_handle_turn_complete``. We simulate that
        sequence here without running the full worker loop."""
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        async def _enqueue():
            await ss._enqueue_internal_prompt(
                "presave",
                reason="idle_sleep_presave",
                wait_for_completion=True,
                timeout_sec=2.0,
            )

        enqueue_task = asyncio.create_task(_enqueue())
        # Yield so the enqueue task can put the turn on the queue + start
        # awaiting the completion event.
        for _ in range(5):
            await asyncio.sleep(0)
            if ss._message_queue.qsize() >= 1:
                break

        # Pull the queued turn, simulate the worker's "inflight" state,
        # and fire the completion event the way ``_handle_turn_complete``
        # would. The caller awaiting ``_enqueue`` should now return.
        turn = await ss._message_queue.get()
        ss._inflight_turn = turn
        assert turn.completion_event is not None
        turn.completion_event.set()

        await asyncio.wait_for(enqueue_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_wait_for_completion_timeout_raises(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED
        with pytest.raises(asyncio.TimeoutError):
            await ss._enqueue_internal_prompt(
                "presave",
                reason="idle_sleep_presave",
                wait_for_completion=True,
                timeout_sec=0.05,
            )

    @pytest.mark.asyncio
    async def test_completion_event_fires_before_turn_done(self):
        """Critical ordering: ``completion_event.set()`` MUST fire before
        ``_turn_done.set()`` in ``_handle_turn_complete``. Otherwise a
        ``wait_for_completion=True`` caller might observe ``_turn_done``
        without its own event being set — a race that could break PR B's
        "save state before disconnect" contract.

        #560 retargeting: pre-#560 the completion_event lived on
        ``self._inflight_turn`` (the worker's current dispatch). Under
        concurrent dispatch each entry in ``_inflight_metas`` carries
        its OWN completion_event; the handler popleft's the entry and
        fires its event. Still in the synchronous critical section,
        still before ``_turn_done``.
        """
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        # Seed an internal entry with a completion_event into the deque.
        completion = asyncio.Event()
        _seed_inflight(ss, internal=True, completion_event=completion)
        # _turn_done starts cleared.
        ss._turn_done.clear()

        await ss._handle_turn_complete(_turn_response(text="ack"))

        assert completion.is_set(), (
            "completion_event must be set by _handle_turn_complete"
        )
        assert ss._turn_done.is_set(), (
            "_turn_done must also be set so the worker progresses"
        )


class TestInternalTurnSkipsResponseCallback:
    """Internal turns have no chat target — the response_callback must
    not fire (no broker routing, no Telegram delivery)."""

    @pytest.mark.asyncio
    async def test_handle_turn_complete_skips_response_callback_for_internal(self):
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        ss._inflight_turn = _QueuedTurn(
            prompt="wake",
            internal=True,
            reason="wake_new_session",
        )

        await ss._handle_turn_complete(_turn_response(text="agent reply"))

        assert cb.calls == [], (
            "internal turn must not invoke response_callback — no chat "
            "to route the reply back to"
        )

    @pytest.mark.asyncio
    async def test_handle_turn_complete_skips_conversation_store_for_internal(self):
        conv_store = MagicMock()
        conv_store.append = MagicMock()
        ss, _ = _make_session_with_response_cb(conv_store=conv_store)
        ss._state_machine._state = SessionState.CONNECTED
        ss._inflight_turn = _QueuedTurn(
            prompt="wake",
            internal=True,
            reason="wake_new_session",
        )

        await ss._handle_turn_complete(_turn_response(text="agent reply"))

        # No user-message append (handled in _enqueue_internal_prompt
        # by not calling conv_store at all), no assistant-message append
        # (this method's branch).
        conv_store.append.assert_not_called()


class TestForceFreshContextOnce:
    """``--continue`` suppression flag — independent contract from
    wake-prompt copy (per Murzik's separation principle)."""

    @pytest.mark.asyncio
    async def test_build_claude_cmd_suppresses_continue_when_flag_set(self, tmp_path):
        ss, _ = _make_session()
        # Force a prior-transcript condition so ``_build_claude_cmd``
        # would normally add ``--continue``.
        ss._has_prior_transcript = lambda: True

        ss._config.force_fresh_context_once = True
        cmd = ss._build_claude_cmd()

        assert "--continue" not in cmd, (
            "force_fresh_context_once must suppress --continue even when "
            "a prior transcript exists"
        )

    def test_build_claude_cmd_records_launch_mode_on_session(self):
        ss, _ = _make_session()
        ss._has_prior_transcript = lambda: True
        ss._config.force_fresh_context_once = True

        ss._build_claude_cmd()

        assert ss._last_launch_forced_fresh is True
        assert ss._last_launch_used_continue is False
        assert ss._last_launch_had_prior_transcript is True

    def test_build_claude_cmd_default_uses_continue_with_prior_transcript(self):
        ss, _ = _make_session()
        ss._has_prior_transcript = lambda: True
        # force_fresh defaults to False.
        cmd = ss._build_claude_cmd()
        assert "--continue" in cmd


class TestWakePromptEnqueueOnConnect:
    """Wake-prompt assembly + enqueue is the parent defect from #543.
    These tests pin that ``connect()`` actually injects the wake prompt,
    with the right WakeReason for each runtime signal.

    Note (#570 / Murzik #571): the readiness gate lives in
    ``_deliver_turn`` (delivery time), NOT in ``_enqueue_internal_prompt``.
    The wake ``_QueuedTurn`` is put into ``_message_queue`` immediately
    when ``connect()`` calls ``_enqueue_internal_prompt`` — these tests
    can inspect queue contents synchronously without waiting on the
    gate. See ``TestWakePromptReadinessGate`` for the delivery-time
    behavior tests."""

    @pytest.mark.asyncio
    async def test_connect_enqueues_wake_prompt_by_default(self):
        ss, _ = _make_session()
        # Override the test-default skip so the real path runs.
        ss._skip_wake_prompt_for_tests = False
        await ss.connect()

        assert ss._message_queue.qsize() >= 1, (
            "connect() must enqueue a wake prompt"
        )
        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert turn.internal is True
        assert turn.reason.startswith("wake_")
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_skips_wake_prompt_when_flag_set(self):
        """Test seam — verifies the unit-test path doesn't queue the
        wake prompt (otherwise the worker would hang waiting for the
        transcript tailer to fire ``_handle_turn_complete``)."""
        ss, _ = _make_session()
        # Default: skip flag is True.
        await ss.connect()
        assert ss._message_queue.qsize() == 0
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wake_reason_is_new_session_on_cold_start(
        self, tmp_path, monkeypatch
    ):
        # Isolate HOME to a clean dir so NO transcript exists for the
        # agent's cwd. Otherwise the (now-correct) _has_prior_transcript
        # gate finds a stray transcript and resolves the wake reason to
        # RESUME instead of NEW_SESSION. Pre-fix this test only passed
        # because the buggy _project_dir double-dash never matched any
        # real directory — i.e. the test was implicitly relying on the
        # bug. See test_project_dir_matches_claude_code_encoding.
        monkeypatch.setenv("HOME", str(tmp_path))
        ss, _ = _make_session()
        ss._skip_wake_prompt_for_tests = False
        # No prior transcript, no restart_reason → NEW_SESSION.
        await ss.connect()
        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert "new_session" in turn.reason
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wake_reason_is_context_restart_when_flag_set(self):
        ss, _ = _make_session()
        ss._skip_wake_prompt_for_tests = False
        ss._config.force_fresh_context_once = True

        await ss.connect()

        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert "context_restart" in turn.reason
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wake_reason_is_auto_restart_when_set(self):
        ss, _ = _make_session()
        ss._skip_wake_prompt_for_tests = False
        ss._config.restart_reason = "auto_restart"

        await ss.connect()

        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert "auto_restart" in turn.reason
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_connect_wake_prompt_body_includes_saved_state_when_present(self):
        ss, _ = _make_session()
        ss._skip_wake_prompt_for_tests = False
        ss._config.wake_context = "## Continuation\nWorking on X"

        await ss.connect()

        turn = ss._message_queue._queue[0]  # type: ignore[attr-defined]
        assert "── Saved State ──" in turn.prompt
        assert "## Continuation" in turn.prompt
        assert "Working on X" in turn.prompt
        await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# Issue #570 — wake-prompt readiness gate (CR-01 from #543 validation)
# ──────────────────────────────────────────────────────────────────────────


class TestWakePromptReadinessGate:
    """The wake-prompt readiness gate (#570) is the fix for CR-01: after
    ``force_fresh_context_once`` (context_restart) respawns the REPL,
    the wake prompt's bracketed-paste + 300ms-Enter sequence completes
    before claude is ready to receive the submit Enter — the Enter is
    consumed by splash/MCP-loading transition state and the typed
    prompt sits in the input area unsubmitted.

    Fix: ``_deliver_turn`` awaits ``_session_ready_event`` for turns
    with ``internal=True and reason.startswith("wake_")`` before
    calling ``paste_text``. The event opens when ``set_transcript_path``
    is called by the SessionStart hook — the most reliable "claude is
    past splash + MCP-bootstrap, input area is live" signal we have.
    Bounded by ``_SESSION_READY_GATE_TIMEOUT_SEC`` (30s) with proceed-
    anyway fallback on timeout.

    **Gate at delivery time, not enqueue time** (Murzik #571 review).
    Gating at enqueue time would let concurrent external messages
    jump the queue while the wake sits in the SessionStart wait —
    broker calls ``send()`` the moment ``state == CONNECTED``, which
    fires before the gate would have ended. Gating at delivery keeps
    the wake at the queue HEAD; the worker blocks; external sends
    enqueue BEHIND the wake; FIFO preserved.
    """

    @pytest.mark.asyncio
    async def test_wake_turn_delivery_blocks_on_closed_gate(self):
        """When the gate is closed, ``_deliver_turn`` for a wake_*
        internal turn must NOT call ``paste_text`` until
        ``set_transcript_path`` opens the gate."""
        ss, tmux = _make_session(state=SessionState.CONNECTED)
        ss._session_ready_event = asyncio.Event()  # closed
        ss._tailer = MagicMock()
        ss._tailer.set_transcript_path = MagicMock()

        wake_turn = _QueuedTurn(
            prompt="WAKE_BODY",
            platform="", chat_id="", message_id="",
            internal=True, reason="wake_context_restart",
        )

        deliver_task = asyncio.create_task(ss._deliver_turn(wake_turn))
        # Give the task a tick to enter the gate await.
        await asyncio.sleep(0.05)
        assert not deliver_task.done(), (
            "_deliver_turn must block on the closed gate"
        )
        tmux.paste_text.assert_not_called()

        # Simulate SessionStart hook arriving.
        ss.set_transcript_path("/tmp/fake-transcript.jsonl")
        assert ss._session_ready_event.is_set()

        await asyncio.wait_for(deliver_task, timeout=2.0)
        tmux.paste_text.assert_called_once_with("WAKE_BODY", enter=True)

    @pytest.mark.asyncio
    async def test_wake_turn_delivery_fast_when_gate_already_open(self):
        """If the gate is already open (SessionStart fired before the
        worker pulled the turn), delivery must short-circuit and
        paste immediately."""
        ss, tmux = _make_session(state=SessionState.CONNECTED)
        ss._session_ready_event.set()  # pre-open

        wake_turn = _QueuedTurn(
            prompt="WAKE_BODY",
            platform="", chat_id="", message_id="",
            internal=True, reason="wake_new_session",
        )

        start = _time.monotonic()
        await ss._deliver_turn(wake_turn)
        elapsed_ms = (_time.monotonic() - start) * 1000

        assert elapsed_ms < 100, (
            f"already-open gate must short-circuit; took {elapsed_ms:.1f}ms"
        )
        tmux.paste_text.assert_called_once_with("WAKE_BODY", enter=True)

    @pytest.mark.asyncio
    async def test_non_wake_internal_turn_skips_gate(self):
        """``idle_sleep_presave`` (and any other non-wake internal
        reason) MUST NOT block on the gate. The pre-sleep save is
        delivered into an already-live session whose gate event may
        have been reset during a respawn — gating would deadlock the
        idle-sleep flow."""
        ss, tmux = _make_session(state=SessionState.CONNECTED)
        ss._session_ready_event = asyncio.Event()  # closed

        presave_turn = _QueuedTurn(
            prompt="save state please",
            platform="", chat_id="", message_id="",
            internal=True, reason="idle_sleep_presave",
        )

        start = _time.monotonic()
        await asyncio.wait_for(
            ss._deliver_turn(presave_turn), timeout=2.0,
        )
        elapsed_ms = (_time.monotonic() - start) * 1000

        assert elapsed_ms < 100, (
            f"non-wake reason must skip the gate; took {elapsed_ms:.1f}ms"
        )
        tmux.paste_text.assert_called_once()
        assert not ss._session_ready_event.is_set(), (
            "non-wake delivery must not mutate the gate event"
        )

    @pytest.mark.asyncio
    async def test_external_turn_skips_gate(self):
        """External turns (``internal=False``) MUST skip the gate even
        when closed — they flow through ``send()`` and are routed by
        the broker the moment ``state == CONNECTED``. The gate is a
        wake-prompt-only protection."""
        ss, tmux = _make_session(state=SessionState.CONNECTED)
        ss._session_ready_event = asyncio.Event()  # closed

        external_turn = _QueuedTurn(
            prompt="hi from user",
            platform="telegram", chat_id="123", message_id="msg1",
            internal=False, reason="",
        )

        start = _time.monotonic()
        await asyncio.wait_for(
            ss._deliver_turn(external_turn), timeout=2.0,
        )
        elapsed_ms = (_time.monotonic() - start) * 1000

        assert elapsed_ms < 100, (
            f"external turn must skip the gate; took {elapsed_ms:.1f}ms"
        )
        tmux.paste_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_wake_turn_proceeds_after_gate_timeout(self):
        """Fallback contract: if SessionStart hook never fires (broken
        hook, missing hook script), the gate times out and the wake
        turn pastes anyway — degrades to pre-#570 race behavior rather
        than hanging the session indefinitely."""
        ss, tmux = _make_session(state=SessionState.CONNECTED)
        ss._session_ready_event = asyncio.Event()

        wake_turn = _QueuedTurn(
            prompt="WAKE_BODY",
            platform="", chat_id="", message_id="",
            internal=True, reason="wake_context_restart",
        )

        # Monkey-patch the gate timeout down to something testable.
        original_timeout = tmux_session._SESSION_READY_GATE_TIMEOUT_SEC
        tmux_session._SESSION_READY_GATE_TIMEOUT_SEC = 0.2
        try:
            start = _time.monotonic()
            await ss._deliver_turn(wake_turn)
            elapsed = _time.monotonic() - start
        finally:
            tmux_session._SESSION_READY_GATE_TIMEOUT_SEC = original_timeout

        tmux.paste_text.assert_called_once(), (
            "wake turn must paste after timeout (not hang or drop)"
        )
        assert 0.15 < elapsed < 1.0, (
            f"expected ~0.2s timeout wait; got {elapsed:.3f}s"
        )

    @pytest.mark.asyncio
    async def test_set_transcript_path_opens_gate(self):
        """``set_transcript_path`` is the production code path that
        opens the gate (called by the SessionStart hook via the
        ``/transport/transcript-path`` API endpoint). Verify the side
        effect is unconditional on first call after spawn."""
        ss, _ = _make_session()
        ss._session_ready_event = asyncio.Event()  # closed
        ss._tailer = MagicMock()
        ss._tailer.set_transcript_path = MagicMock()

        assert not ss._session_ready_event.is_set()
        ss.set_transcript_path("/tmp/fake-transcript.jsonl")
        assert ss._session_ready_event.is_set(), (
            "set_transcript_path must open the readiness gate"
        )

    @pytest.mark.asyncio
    async def test_set_transcript_path_idempotent_gate_open(self):
        """Subsequent ``set_transcript_path`` calls (e.g. CC firing
        SessionStart on resume) must not error or churn the gate
        state. Idempotent ``.set()`` is the asyncio.Event contract;
        this test pins that the wrapper doesn't accidentally clear
        or reassign on repeat calls."""
        ss, _ = _make_session()
        ss._tailer = MagicMock()
        ss._tailer.set_transcript_path = MagicMock()

        ss.set_transcript_path("/tmp/fake-transcript.jsonl")
        first_event = ss._session_ready_event
        assert first_event.is_set()

        ss.set_transcript_path("/tmp/fake-transcript.jsonl")
        assert ss._session_ready_event is first_event, (
            "repeat set_transcript_path must not reassign the event"
        )
        assert ss._session_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_external_send_during_gate_wait_stays_behind_wake(self):
        """Murzik #571 regression — FIFO across the bootstrap window.

        With the gate at delivery time (not enqueue), an external
        ``send()`` arriving WHILE the worker is blocked on the wake
        gate must enqueue BEHIND the wake turn. When the gate opens,
        paste order must be [wake, external] — not [external, wake].

        This is the regression that gating-at-enqueue would have
        introduced: the broker calls ``send`` the moment ``state ==
        CONNECTED`` (well before this gate would have ended for a
        cold-spawn ~5-15s MCP boot), so an external message would
        have jumped ahead of the wake prompt the user/restart-handler
        relied on as orientation context.
        """
        ss, tmux = _make_session(state=SessionState.CONNECTED)
        ss._session_ready_event = asyncio.Event()  # closed

        paste_order: list[str] = []

        async def _track_paste(text, *, enter=True, enter_delay_ms=300):
            paste_order.append(text)
            return _ok()

        tmux.paste_text = AsyncMock(side_effect=_track_paste)

        # Start the worker manually — we want a real worker draining
        # the queue but no full connect() flow (which would also
        # enqueue its own wake prompt).
        ss._worker_task = asyncio.create_task(ss._message_worker())

        try:
            # Enqueue wake first.
            await ss._enqueue_internal_prompt(
                "WAKE", reason="wake_context_restart",
            )
            # Let the worker pop the wake turn and hit the gate.
            await asyncio.sleep(0.05)

            # Now an external send arrives while the worker is gated.
            await ss.send(
                "EXTERNAL",
                platform="telegram", chat_id="123", message_id="msg1",
            )
            await asyncio.sleep(0.05)

            assert paste_order == [], (
                f"gate must hold both turns until it opens; "
                f"unexpected paste order: {paste_order}"
            )

            # Open the gate — worker proceeds wake first, then external.
            ss._session_ready_event.set()
            # Generous wait for both to drain through the worker.
            for _ in range(40):
                if len(paste_order) >= 2:
                    break
                await asyncio.sleep(0.05)

            assert paste_order == ["WAKE", "EXTERNAL"], (
                f"FIFO violated — paste order was {paste_order}; "
                f"the regression Murzik flagged in #571 review"
            )
        finally:
            ss._worker_task.cancel()
            try:
                await ss._worker_task
            except asyncio.CancelledError:
                pass


# ──────────────────────────────────────────────────────────────────────────
# Issue #570 follow-up — wake-prompt gate observability metrics
# ──────────────────────────────────────────────────────────────────────────


class TestWakePromptGateMetrics:
    """The #570 fix proceeds-on-timeout if the SessionStart hook ever
    fails to fire — which silently degrades to the pre-#570 race. We
    need production visibility into:

      1. Gate-latency distribution: is the hook actually firing fast?
      2. Timeout count: are we hitting the fallback path at all?
      3. Total wake count: denominator for ratios.

    Implementation: ``_deliver_turn`` emits one ``wake_gate`` activity
    event per wake_* turn with subtype ``instant`` (gate already open),
    ``opened`` (gate waited, then opened in time), or ``timeout`` (gate
    hit the 30s fallback). Metadata carries ``reason`` and
    ``latency_ms``. Queryable via ``analytics_activity_events`` with no
    new schema.
    """

    @pytest.mark.asyncio
    async def test_pre_open_gate_emits_instant_event(self):
        """When the gate is already open before the worker pops the
        turn (SessionStart hook beat the worker), the metric must
        record a ``instant`` event with latency_ms=0. Without this
        the denominator is wrong — fast paths would be invisible and
        the distribution would look slower than reality."""
        analytics = MagicMock()
        ss, _tmux = _make_session(
            state=SessionState.CONNECTED, analytics_store=analytics,
        )
        ss._session_ready_event.set()  # pre-open

        wake_turn = _QueuedTurn(
            prompt="WAKE_BODY",
            platform="", chat_id="", message_id="",
            internal=True, reason="wake_new_session",
        )

        await ss._deliver_turn(wake_turn)

        analytics.log_activity.assert_called_once()
        kwargs = analytics.log_activity.call_args.kwargs
        assert kwargs["event_type"] == "wake_gate"
        assert kwargs["subtype"] == "instant"
        assert kwargs["agent_name"] == ss.agent_name
        assert kwargs["session_id"] == ss.id
        assert kwargs["metadata"]["reason"] == "wake_new_session"
        assert kwargs["metadata"]["latency_ms"] == 0

    @pytest.mark.asyncio
    async def test_gate_wait_then_open_emits_opened_with_latency(self):
        """When the gate is closed and SessionStart opens it mid-wait,
        the metric must record subtype ``opened`` with a positive
        latency_ms. This is the primary production metric — the
        distribution tells us how long the bootstrap actually takes
        in the wild."""
        analytics = MagicMock()
        ss, _tmux = _make_session(
            state=SessionState.CONNECTED, analytics_store=analytics,
        )
        ss._session_ready_event = asyncio.Event()  # closed
        ss._tailer = MagicMock()
        ss._tailer.set_transcript_path = MagicMock()

        wake_turn = _QueuedTurn(
            prompt="WAKE_BODY",
            platform="", chat_id="", message_id="",
            internal=True, reason="wake_context_restart",
        )

        deliver_task = asyncio.create_task(ss._deliver_turn(wake_turn))
        # Let the deliver task park in the gate await.
        await asyncio.sleep(0.15)
        ss.set_transcript_path("/tmp/fake-transcript.jsonl")
        await asyncio.wait_for(deliver_task, timeout=2.0)

        analytics.log_activity.assert_called_once()
        kwargs = analytics.log_activity.call_args.kwargs
        assert kwargs["event_type"] == "wake_gate"
        assert kwargs["subtype"] == "opened"
        assert kwargs["metadata"]["reason"] == "wake_context_restart"
        # ~150ms wait; allow loose bounds for CI jitter.
        latency_ms = kwargs["metadata"]["latency_ms"]
        assert 100 <= latency_ms < 2000, (
            f"expected gate latency in [100ms, 2000ms]; got {latency_ms}ms"
        )

    @pytest.mark.asyncio
    async def test_gate_timeout_emits_timeout_event(self):
        """When the gate times out (hook never fires), the metric
        must record subtype ``timeout`` with latency_ms set to the
        full timeout window. This is the alarm signal — a non-zero
        timeout rate in production means we've silently regressed to
        the pre-#570 race and need to investigate the hook."""
        analytics = MagicMock()
        ss, _tmux = _make_session(
            state=SessionState.CONNECTED, analytics_store=analytics,
        )
        ss._session_ready_event = asyncio.Event()  # closed, never opens

        wake_turn = _QueuedTurn(
            prompt="WAKE_BODY",
            platform="", chat_id="", message_id="",
            internal=True, reason="wake_context_restart",
        )

        original_timeout = tmux_session._SESSION_READY_GATE_TIMEOUT_SEC
        tmux_session._SESSION_READY_GATE_TIMEOUT_SEC = 0.2
        try:
            await ss._deliver_turn(wake_turn)
        finally:
            tmux_session._SESSION_READY_GATE_TIMEOUT_SEC = original_timeout

        analytics.log_activity.assert_called_once()
        kwargs = analytics.log_activity.call_args.kwargs
        assert kwargs["event_type"] == "wake_gate"
        assert kwargs["subtype"] == "timeout"
        assert kwargs["metadata"]["reason"] == "wake_context_restart"
        # latency_ms recorded as full timeout window (200ms in this test
        # after the monkey-patch — 30000 in production).
        assert kwargs["metadata"]["latency_ms"] == 200

    @pytest.mark.asyncio
    async def test_non_wake_turn_does_not_emit_wake_gate_event(self):
        """Scope guard: ``idle_sleep_presave`` and other non-wake
        internal reasons (which skip the gate entirely) must NOT
        emit a wake_gate event. Otherwise the metric is meaningless
        — we'd count turns that never touched the gate code path."""
        analytics = MagicMock()
        ss, _tmux = _make_session(
            state=SessionState.CONNECTED, analytics_store=analytics,
        )

        presave_turn = _QueuedTurn(
            prompt="save state please",
            platform="", chat_id="", message_id="",
            internal=True, reason="idle_sleep_presave",
        )
        await ss._deliver_turn(presave_turn)

        # No wake_gate event for non-wake internal turns. Other
        # analytics calls (e.g. tool_use) may still fire — assert
        # only that no wake_gate emission happened.
        wake_gate_calls = [
            c for c in analytics.log_activity.call_args_list
            if c.kwargs.get("event_type") == "wake_gate"
        ]
        assert wake_gate_calls == [], (
            f"non-wake turn must not emit wake_gate; got {wake_gate_calls}"
        )

    @pytest.mark.asyncio
    async def test_external_turn_does_not_emit_wake_gate_event(self):
        """External turns flow through ``send()`` and skip the gate.
        They must also skip the wake_gate metric — mixing external
        turns into the wake distribution would dilute the signal."""
        analytics = MagicMock()
        ss, _tmux = _make_session(
            state=SessionState.CONNECTED, analytics_store=analytics,
        )

        external_turn = _QueuedTurn(
            prompt="hi from user",
            platform="telegram", chat_id="123", message_id="msg1",
            internal=False, reason="",
        )
        await ss._deliver_turn(external_turn)

        wake_gate_calls = [
            c for c in analytics.log_activity.call_args_list
            if c.kwargs.get("event_type") == "wake_gate"
        ]
        assert wake_gate_calls == [], (
            f"external turn must not emit wake_gate; got {wake_gate_calls}"
        )

    @pytest.mark.asyncio
    async def test_analytics_emit_failure_is_swallowed_not_raised(self):
        """A flaky analytics_store must not break wake-turn delivery.
        The gate code is in the hot path of every cold-start —
        analytics is a side observation, not a correctness
        precondition. Emission failure should log and proceed."""
        analytics = MagicMock()
        analytics.log_activity.side_effect = RuntimeError("db locked")
        ss, tmux = _make_session(
            state=SessionState.CONNECTED, analytics_store=analytics,
        )
        ss._session_ready_event.set()  # pre-open path, fastest

        wake_turn = _QueuedTurn(
            prompt="WAKE_BODY",
            platform="", chat_id="", message_id="",
            internal=True, reason="wake_new_session",
        )

        # Must not raise even though log_activity errors.
        await ss._deliver_turn(wake_turn)
        # And the paste must still have fired — delivery contract intact.
        tmux.paste_text.assert_called_once_with("WAKE_BODY", enter=True)

    @pytest.mark.asyncio
    async def test_no_analytics_store_is_safe(self):
        """Sessions without an analytics_store (existing default) must
        not crash on the gate code path. Back-compat for callers that
        haven't wired the analytics surface."""
        ss, tmux = _make_session(
            state=SessionState.CONNECTED, analytics_store=None,
        )
        ss._session_ready_event.set()

        wake_turn = _QueuedTurn(
            prompt="WAKE_BODY",
            platform="", chat_id="", message_id="",
            internal=True, reason="wake_new_session",
        )
        await ss._deliver_turn(wake_turn)
        tmux.paste_text.assert_called_once_with("WAKE_BODY", enter=True)


# ──────────────────────────────────────────────────────────────────────────
# Idle-sleep parity (PR B for #543) + #545 follow-ups (Murzik review)
# ──────────────────────────────────────────────────────────────────────────


class TestIdleSleepPresavePrompt:
    """Pre-sleep save instruction parity with SDK.

    Tmux ``idle_sleep()`` must enqueue the "use reflect()/save state"
    prompt the SDK sends, with ``wait_for_completion=True`` so the
    disconnect doesn't kill the pane before the agent honors the
    instruction (the footgun Murzik flagged when reviewing the
    internal-prompt API)."""

    @pytest.mark.asyncio
    async def test_idle_sleep_enqueues_presave_prompt_before_disconnect(self):
        """The pre-sleep prompt must be enqueued via
        ``_enqueue_internal_prompt`` BEFORE the state transition to
        IDLE_SLEEPING — otherwise the enqueue gate (which requires
        CONNECTED) would drop the prompt."""
        ss, tmux = _make_session(state=SessionState.CONNECTED)
        observed: list[tuple[str, dict]] = []

        async def _spy(prompt, *, reason, wait_for_completion=False, timeout_sec=None):
            observed.append(
                (prompt, {"reason": reason, "wait": wait_for_completion, "timeout": timeout_sec})
            )
            # Don't actually call original — we don't want to involve the
            # message queue + worker in this unit test (no transcript
            # tailer to fire ``_handle_turn_complete``). The contract
            # we're pinning is "idle_sleep called _enqueue_internal_prompt
            # with the right args BEFORE state transition + disconnect."
            return None

        ss._enqueue_internal_prompt = _spy

        result = await ss.idle_sleep()
        assert result is True
        assert ss.state == SessionState.IDLE_SLEEPING

        # The pre-sleep prompt was sent.
        assert len(observed) == 1, "exactly one pre-sleep prompt expected"
        prompt, kwargs = observed[0]
        assert "Auto-sleep is activating" in prompt
        assert "reflect()" in prompt
        assert kwargs["reason"] == "idle_sleep_presave"
        assert kwargs["wait"] is True, (
            "wait_for_completion MUST be True — otherwise disconnect "
            "would kill the pane before the agent could honor the save "
            "instruction (footgun from Murzik's API review)"
        )
        assert kwargs["timeout"] == 120.0

    @pytest.mark.asyncio
    async def test_idle_sleep_proceeds_on_presave_timeout(self):
        """A wedged REPL must not block idle-sleep semantics. If the
        pre-sleep enqueue times out, log + continue to disconnect."""
        ss, _ = _make_session(state=SessionState.CONNECTED)

        async def _timeout(*args, **kwargs):
            raise asyncio.TimeoutError("simulated")

        ss._enqueue_internal_prompt = _timeout

        result = await ss.idle_sleep()
        assert result is True, (
            "idle_sleep must complete even when pre-sleep enqueue raises"
        )
        assert ss.state == SessionState.IDLE_SLEEPING

    @pytest.mark.asyncio
    async def test_idle_sleep_proceeds_on_presave_arbitrary_failure(self):
        ss, _ = _make_session(state=SessionState.CONNECTED)

        async def _raise(*args, **kwargs):
            raise RuntimeError("simulated REPL failure")

        ss._enqueue_internal_prompt = _raise

        result = await ss.idle_sleep()
        assert result is True
        assert ss.state == SessionState.IDLE_SLEEPING


class TestForceFreshContextOnceDeferredConsume:
    """Murzik's #545 follow-up: the flag MUST stay set across a failed
    spawn so a retry honors the fresh-context guarantee. Two regressions:

    1. ``new_session`` fails first time → retry sees flag still set.
    2. ``_start_tailer`` fails first time → retry sees flag still set
       (this is the round-2 case that catches a post-``_spawn()``
       clear instead of post-``_start_tailer()`` clear).
    """

    @pytest.mark.asyncio
    async def test_build_claude_cmd_does_not_consume_flag(self):
        """The flag must survive ``_build_claude_cmd`` so a failed
        ``_spawn_tmux_repl`` leaves the flag observable for retry."""
        ss, _ = _make_session()
        ss._has_prior_transcript = lambda: True
        ss._config.force_fresh_context_once = True

        ss._build_claude_cmd()

        assert ss._config.force_fresh_context_once is True, (
            "_build_claude_cmd must NOT consume the flag — that's the "
            "spawn-success path's job. Premature consumption is the bug "
            "Murzik caught on PR #545 review."
        )

    @pytest.mark.asyncio
    async def test_force_fresh_survives_failed_new_session_retry(self):
        """Failure mode 1: ``new_session`` fails on first attempt.
        Flag stays set; retry's ``_build_claude_cmd`` re-applies the
        suppression."""
        tmux = _make_mock_tmux()
        # First call fails, second call succeeds.
        tmux.new_session = AsyncMock(side_effect=[_fail("rc=1"), _ok()])
        ss, _ = _make_session(tmux=tmux)

        # Seed flag + prior transcript so suppression is meaningful.
        ss._config.force_fresh_context_once = True
        ss._has_prior_transcript = lambda: True

        # First connect — should fail.
        with pytest.raises(RuntimeError, match="new-session failed"):
            await ss.connect()

        # Flag must still be set so the retry honors it.
        assert ss._config.force_fresh_context_once is True, (
            "force_fresh_context_once was consumed despite spawn failure — "
            "retry would silently lose the fresh-context guarantee"
        )

        # Reset state machine + recreate tmux (kept original behavior:
        # second new_session call returns _ok()). Force CONNECTED-eligible
        # state for retry.
        ss._state_machine._state = SessionState.UNINITIALIZED
        await ss.connect()
        assert ss.state == SessionState.CONNECTED
        # After successful spawn + tailer, flag is now consumed.
        assert ss._config.force_fresh_context_once is False, (
            "flag must consume on successful launch — otherwise it stays "
            "set forever and every subsequent launch is fresh"
        )

    @pytest.mark.asyncio
    async def test_force_fresh_survives_failed_tailer_start_retry(self):
        """Failure mode 2 (the round-2 case): ``_start_tailer`` fails
        after ``new_session`` succeeded. Flag stays set; retry honors it.

        This is the test that catches a post-``_spawn()`` clear
        (the round-1 fix that Murzik flagged as still buggy)."""
        ss, _ = _make_session()
        ss._config.force_fresh_context_once = True
        ss._has_prior_transcript = lambda: True

        # Make _start_tailer fail on first call, succeed on second.
        call_count = {"n": 0}
        real_start_tailer = ss._start_tailer

        async def _flaky_start_tailer():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated tailer-start failure")
            await real_start_tailer()

        ss._start_tailer = _flaky_start_tailer

        # First connect — tailer-start raises, _spawn_tmux_repl rolls
        # back the tmux session, exception propagates.
        with pytest.raises(RuntimeError, match="tailer-start failure"):
            await ss.connect()

        # Round-2 case: even though ``_spawn()`` succeeded above, the
        # tailer-start failure rolled back the whole launch — flag
        # MUST still be set.
        assert ss._config.force_fresh_context_once is True, (
            "force_fresh_context_once was consumed after _spawn() but "
            "before _start_tailer() succeeded — retry would lose the "
            "fresh-context guarantee. This is the round-2 case from "
            "Murzik's #545 review."
        )

        # Reset for retry.
        ss._state_machine._state = SessionState.UNINITIALIZED
        await ss.connect()
        # After REPL + tailer both up, NOW the flag clears.
        assert ss._config.force_fresh_context_once is False


class TestInternalPromptReturnContract:
    """Murzik #545 follow-up: ``_enqueue_internal_prompt`` always
    returns None. Earlier drafts suggested lazy event observation on
    wait=False but the pattern wasn't used and added a footgun."""

    @pytest.mark.asyncio
    async def test_wait_false_returns_none(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED
        result = await ss._enqueue_internal_prompt(
            "wake",
            reason="wake_new_session",
            wait_for_completion=False,
        )
        assert result is None, (
            "wait_for_completion=False must return None — lazy event "
            "observation is intentionally not part of the contract "
            "(Murzik #545 follow-up; doc/impl mismatch fixed)"
        )

    @pytest.mark.asyncio
    async def test_wait_true_returns_none(self):
        """For consistency: wait_for_completion=True also returns None
        (the call already waited inline)."""
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.CONNECTED

        # Same simulation as test_wait_for_completion_true_blocks_until_event_fires.
        async def _enqueue():
            return await ss._enqueue_internal_prompt(
                "presave",
                reason="idle_sleep_presave",
                wait_for_completion=True,
                timeout_sec=2.0,
            )

        task = asyncio.create_task(_enqueue())
        for _ in range(5):
            await asyncio.sleep(0)
            if ss._message_queue.qsize() >= 1:
                break
        turn = await ss._message_queue.get()
        ss._inflight_turn = turn
        assert turn.completion_event is not None
        turn.completion_event.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_dropped_when_not_connected_returns_none(self):
        ss, _ = _make_session()
        ss._state_machine._state = SessionState.DEAD
        result = await ss._enqueue_internal_prompt(
            "wake",
            reason="wake_new_session",
            wait_for_completion=False,
        )
        assert result is None


# ──────────────────────────────────────────────────────────────────────────
# #560 — concurrent dispatch / inflight deque
# ──────────────────────────────────────────────────────────────────────────


class TestInflightDequeConcurrentDispatch:
    """The contract block #560 introduces.

    Murzik's review points pinned as named tests:
    1. FIFO response routing across three back-to-back chat_ids
    2. Internal-prompt completion_event fires only on its own popleft
    3. Paste failure → no deque append + immediate completion_event set
    4. Force_restart / disconnect drains the deque and unblocks every
       pending completion_event so wait_for_completion callers can't
       hang on an abandoned turn
    5. Watchdog ages the HEAD, not paste_time — a queued turn gets its
       own full timeout window once it becomes the head
    """

    @pytest.mark.asyncio
    async def test_fifo_routing_a_b_c_no_cross_leak(self):
        """Three turns with distinct chat_ids → three stop hooks → each
        response routes to its own chat in FIFO order. Pinned per
        Murzik review point #3."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED

        # Seed three external entries directly (bypass paste — this
        # test pins the deque + routing contract, not the paste path).
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "A", "message_id": "mA"})
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "B", "message_id": "mB"})
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "C", "message_id": "mC"})
        assert len(ss._inflight_metas) == 3

        # Fire stop hooks in order.
        await ss._handle_turn_complete(TurnResponse(text="reply A", stop_reason="end_turn"))
        await ss._handle_turn_complete(TurnResponse(text="reply B", stop_reason="end_turn"))
        await ss._handle_turn_complete(TurnResponse(text="reply C", stop_reason="end_turn"))

        assert len(cb.calls) == 3
        assert (cb.calls[0].response_text, cb.calls[0].chat_id) == ("reply A", "A")
        assert (cb.calls[1].response_text, cb.calls[1].chat_id) == ("reply B", "B")
        assert (cb.calls[2].response_text, cb.calls[2].chat_id) == ("reply C", "C")
        assert len(ss._inflight_metas) == 0

    @pytest.mark.asyncio
    async def test_internal_in_middle_completion_event_fires_only_on_own_pop(self):
        """Queue: external, internal-with-event, external. The
        completion_event for the middle internal entry must fire ONLY
        when that entry pops — not on the first external's stop hook,
        not on the last external's. Murzik review point #4."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED

        completion = asyncio.Event()
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "A", "message_id": "mA"})
        _seed_inflight(ss, internal=True, completion_event=completion)
        _seed_inflight(ss, meta={"platform": "telegram", "chat_id": "C", "message_id": "mC"})

        # First stop hook: pops external A. completion_event NOT set yet.
        await ss._handle_turn_complete(TurnResponse(text="reply A", stop_reason="end_turn"))
        assert not completion.is_set(), (
            "completion_event must NOT fire when an unrelated entry pops"
        )
        assert len(cb.calls) == 1
        assert cb.calls[0].chat_id == "A"

        # Second stop hook: pops the internal middle entry. NOW completion fires.
        await ss._handle_turn_complete(TurnResponse(text="ack", stop_reason="end_turn"))
        assert completion.is_set(), (
            "completion_event MUST fire when its own internal entry pops"
        )
        # Internal entry skips response_callback — callback count unchanged.
        assert len(cb.calls) == 1

        # Third stop hook: pops external C.
        await ss._handle_turn_complete(TurnResponse(text="reply C", stop_reason="end_turn"))
        assert len(cb.calls) == 2
        assert cb.calls[1].chat_id == "C"

    @pytest.mark.asyncio
    async def test_paste_failure_does_not_append_to_deque(self):
        """``_deliver_turn`` must not append an entry to
        ``_inflight_metas`` when ``paste_text`` reports !ok. Otherwise a
        future stop hook would pop a meta with no corresponding CC
        turn, routing a later response to the wrong chat."""
        tmux = _make_mock_tmux()
        tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
        ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
        turn = _QueuedTurn(prompt="x", platform="t", chat_id="c", message_id="m")
        with pytest.raises(RuntimeError, match="tmux paste-buffer"):
            await ss._deliver_turn(turn)
        assert len(ss._inflight_metas) == 0, (
            "paste failure must NOT leave a phantom meta entry"
        )

    @pytest.mark.asyncio
    async def test_paste_failure_unblocks_pending_completion_event(self):
        """Murzik review point #2 — explicit: a turn with
        ``completion_event`` whose paste fails must NOT hang the caller.
        ``_deliver_turn`` fires the event on the failure path."""
        tmux = _make_mock_tmux()
        tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
        ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
        completion = asyncio.Event()
        turn = _QueuedTurn(
            prompt="x", internal=True, reason="presave",
            completion_event=completion,
        )
        with pytest.raises(RuntimeError):
            await ss._deliver_turn(turn)
        assert completion.is_set(), (
            "paste failure must unblock the caller waiting on completion_event"
        )

    @pytest.mark.asyncio
    async def test_disconnect_unblocks_pending_completion_events(self):
        """Disconnect drains ``_inflight_metas`` and sets every pending
        ``completion_event`` so a ``wait_for_completion=True`` caller
        doesn't hang on an abandoned turn (e.g. force_restart cycling
        the pane mid-save). Murzik review point #2 — explicit."""
        ss, _ = _make_session(state=SessionState.CONNECTED)
        await ss.connect()

        event1 = asyncio.Event()
        event2 = asyncio.Event()
        _seed_inflight(ss, internal=True, completion_event=event1)
        _seed_inflight(ss, meta={"platform": "t", "chat_id": "c", "message_id": "m"})
        # No event on the second; the entry is external — disconnect
        # still drains it, just doesn't have anything to signal.
        _seed_inflight(ss, internal=True, completion_event=event2)
        assert len(ss._inflight_metas) == 3

        await ss.disconnect()

        assert len(ss._inflight_metas) == 0
        assert event1.is_set(), "disconnect must unblock event1"
        assert event2.is_set(), "disconnect must unblock event2"

    @pytest.mark.asyncio
    async def test_handle_turn_complete_with_empty_deque_logs_and_bails(self):
        """Empty-deque defense (Murzik review point #7): a stop hook
        with no pending meta must not synthesize routing. It logs and
        skips the callback chain — better silent than wrong-routed.
        """
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        # Deque is empty.
        assert len(ss._inflight_metas) == 0

        # Handler should bail cleanly without raising or calling cb.
        await ss._handle_turn_complete(TurnResponse(text="orphan", stop_reason="end_turn"))

        assert cb.calls == [], (
            "empty-deque path must NOT synthesize routing — would "
            "re-introduce #496 Case 1 from zero state"
        )

    @pytest.mark.asyncio
    async def test_head_clock_resets_on_popleft_with_remaining_entries(self):
        """Murzik review point #1: when the head pops and entries remain,
        the NEW head's clock starts fresh so it gets its own full
        ``_TURN_DONE_TIMEOUT_SEC`` window (rather than inheriting the
        previous head's age)."""
        ss, _ = _make_session_with_response_cb()
        ss._state_machine._state = SessionState.CONNECTED

        _seed_inflight(ss, meta={"platform": "t", "chat_id": "A", "message_id": "mA"})
        _seed_inflight(ss, meta={"platform": "t", "chat_id": "B", "message_id": "mB"})
        first_head_at = ss._head_started_at
        assert first_head_at is not None

        # Sleep a tiny bit so the new head_started_at differs.
        await asyncio.sleep(0.01)
        await ss._handle_turn_complete(TurnResponse(text="A", stop_reason="end_turn"))

        # Deque still has one entry; head clock should have advanced.
        assert len(ss._inflight_metas) == 1
        assert ss._head_started_at is not None
        assert ss._head_started_at > first_head_at, (
            "post-popleft head clock must reset so the new head gets "
            "its own full timeout window (Murzik #560 review point #1)"
        )

        # Drain — clock becomes None.
        await ss._handle_turn_complete(TurnResponse(text="B", stop_reason="end_turn"))
        assert ss._head_started_at is None
        assert len(ss._inflight_metas) == 0

    @pytest.mark.asyncio
    async def test_worker_permanent_failure_unblocks_inflight_turn_completion_event(self):
        """Issue #547: a turn the worker had in-hand whose delivery
        raised — for any reason other than the explicit !ok branch in
        ``_deliver_turn`` that already fires the event — must still
        unblock its ``completion_event``. Otherwise an unbounded
        ``wait_for_completion=True`` caller (timeout_sec=None) deadlocks
        forever.

        Repro by stubbing ``_deliver_turn`` to raise directly,
        bypassing its own completion_event handling — the catch-all in
        the worker's except branch is what saves the caller.
        """
        ss, _ = _make_session()
        await ss.connect()
        # Stub _deliver_turn to raise unconditionally.
        async def _raise(turn):
            raise RuntimeError("tailer state corruption simulated")
        ss._deliver_turn = _raise

        completion = asyncio.Event()
        internal_turn = _QueuedTurn(
            prompt="presave",
            internal=True,
            reason="idle_sleep_presave",
            completion_event=completion,
        )
        await ss._message_queue.put(internal_turn)

        # Worker should hit the permanent-failure branch and fire the
        # completion event. Tight timeout — a regression manifests as
        # test hang→fail, not a silent deadlock.
        try:
            await asyncio.wait_for(completion.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail(
                "completion_event must fire on worker permanent-failure "
                "path (#547) so unbounded wait_for_completion can't deadlock"
            )

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_unblocks_pre_paste_inflight_turn(self):
        """Issue #547 (extended): the worker may hold ``_inflight_turn``
        for a turn it pulled from the queue but hadn't yet pasted
        (e.g. mid context-lock retry, or worker cancelled before
        ``_deliver_turn`` ran). The meta isn't in ``_inflight_metas``
        yet, so the disconnect drain loop misses it. ``disconnect``
        must explicitly unblock the in-hand turn's ``completion_event``.
        """
        ss, _ = _make_session(state=SessionState.CONNECTED)
        await ss.connect()
        # Inject a pre-paste in-hand turn directly (the worker would
        # normally set this during a context-lock retry).
        completion = asyncio.Event()
        ss._inflight_turn = _QueuedTurn(
            prompt="presave",
            internal=True,
            reason="idle_sleep_presave",
            completion_event=completion,
        )
        # No entry in the deque yet — the paste never happened.
        assert len(ss._inflight_metas) == 0

        await ss.disconnect()

        assert completion.is_set(), (
            "disconnect must unblock the worker's pre-paste in-hand "
            "turn (#547) — otherwise unbounded wait_for_completion hangs"
        )
        assert ss._inflight_turn is None, (
            "disconnect must also clear the in-hand reference so the "
            "next connect doesn't redeliver a stale turn"
        )

    @pytest.mark.asyncio
    async def test_watchdog_requeues_tail_on_stuck_head(self, monkeypatch):
        """Murzik review on PR #561 — critical data-loss fix.

        Before this fix the watchdog drained the WHOLE deque, fired
        all completion_events, and force_restarted — silently dropping
        every queued turn behind the stuck head. With CC's native
        queue absorbing back-to-back pastes, that's a regression vs
        the pre-#560 serial dispatch (where B/C would still be in
        _message_queue when A timed out).

        New contract:
        - HEAD ONLY is abandoned (its completion_event fires)
        - TAIL entries are requeued at the front of _message_queue in
          FIFO order, with their original ``_QueuedTurn`` (prompt +
          completion_event) intact for replay after force_restart
        - Tail completion_events stay UNSET — they fire only when the
          rerun actually completes
        """
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)

        ss, _ = _make_session()
        await ss.connect()
        # Cancel the worker so the test isolates the watchdog's
        # drain+requeue behavior. In production, force_restart's
        # disconnect cancels the worker BEFORE it can re-pull the
        # requeued turns from _message_queue; without cancelling here
        # the worker would immediately re-dispatch B/C back into the
        # deque, masking the requeue we want to assert.
        ss._worker_task.cancel()
        try:
            await ss._worker_task
        except asyncio.CancelledError:
            pass

        # Stub force_restart so we don't actually drive the full
        # disconnect→reconnect cycle.
        force_called = asyncio.Event()
        async def _stub_force_restart(*, bypass_guard: bool = False):
            force_called.set()
            return True
        ss.force_restart = _stub_force_restart

        # Seed three in-flight entries (A=internal-with-event,
        # B=external, C=external-with-event). Distinct prompts so we
        # can assert replay order. ``_seed_inflight`` synthesizes the
        # _QueuedTurn so the requeue has something to push.
        completion_a = asyncio.Event()
        completion_c = asyncio.Event()
        _seed_inflight(ss, internal=True, completion_event=completion_a, prompt="A")
        _seed_inflight(
            ss,
            meta={"platform": "telegram", "chat_id": "B", "message_id": "mB"},
            prompt="B",
        )
        _seed_inflight(
            ss,
            meta={"platform": "telegram", "chat_id": "C", "message_id": "mC"},
            completion_event=completion_c,
            prompt="C",
        )
        # Force the head clock to look ancient so the watchdog trips
        # immediately on its next tick.
        ss._head_started_at = 0.0
        assert len(ss._inflight_metas) == 3
        # Sanity: queue is empty pre-timeout.
        assert ss._message_queue.qsize() == 0

        try:
            await asyncio.wait_for(force_called.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("watchdog must trigger force_restart when head ages out")

        # HEAD only fired its completion_event. Tail events stayed unset.
        assert completion_a.is_set(), "head's completion_event must fire (abandoned)"
        assert not completion_c.is_set(), (
            "tail entry's completion_event must NOT fire — it'll fire when "
            "the rerun actually completes after force_restart"
        )

        # Deque drained.
        assert len(ss._inflight_metas) == 0
        assert ss._head_started_at is None

        # B and C requeued at the front of _message_queue in FIFO order.
        assert ss._message_queue.qsize() == 2, (
            "tail entries B and C must be requeued for replay"
        )
        b_replay = ss._message_queue.get_nowait()
        c_replay = ss._message_queue.get_nowait()
        assert b_replay.prompt == "B", "B must be at the front (FIFO)"
        assert c_replay.prompt == "C", "C must follow B"
        # Replay carries the original completion_event so the eventual
        # rerun unblocks the right caller.
        assert c_replay.completion_event is completion_c

        # Stats updated.
        assert ss._stats.get("turn_timeouts", 0) == 1

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_watchdog_requeues_in_hand_turn_with_tail(self, monkeypatch):
        """Tail-requeue must also handle the worker's
        ``_inflight_turn`` (a turn pulled from the queue but not yet
        pasted — e.g. mid context-lock retry). Murzik review on
        commit 2 of PR #561: it was LATER in original send-order than
        the deque tail (the worker is single-threaded — it pulls one,
        pastes it, pulls the next; tail entries had already been
        pasted by the time in_hand was pulled), so replay AFTER the
        tail, not before.
        """
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)

        ss, _ = _make_session()
        await ss.connect()
        # Same worker-cancel guard as the sibling test above.
        ss._worker_task.cancel()
        try:
            await ss._worker_task
        except asyncio.CancelledError:
            pass
        force_called = asyncio.Event()
        async def _stub(*, bypass_guard: bool = False):
            force_called.set()
            return True
        ss.force_restart = _stub

        # Deque: [A (stuck head), B] — A and B were pasted by the
        # worker in that order.
        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})
        _seed_inflight(ss, prompt="B", meta={"chat_id": "B"})
        # In-hand: a turn the worker pulled AFTER pasting B but had
        # not yet pasted (e.g. context-lock retry). Original send-order:
        # A → B → C.
        in_hand_turn = _QueuedTurn(prompt="C", platform="t", chat_id="C", message_id="mC")
        ss._inflight_turn = in_hand_turn
        ss._head_started_at = 0.0

        try:
            await asyncio.wait_for(force_called.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("watchdog must trigger force_restart")

        assert ss._inflight_turn is None, "in-hand turn cleared after requeue"
        # Replay order: tail B FIRST (pasted before C was pulled),
        # then in_hand C LAST. Preserves original send-order.
        assert ss._message_queue.qsize() == 2
        first = ss._message_queue.get_nowait()
        second = ss._message_queue.get_nowait()
        assert first.prompt == "B", (
            "tail entry B was pasted before in_hand C was pulled — "
            "replay first to preserve A→B→C send-order"
        )
        assert second.prompt == "C", (
            "in_hand C was pulled from queue AFTER B was pasted — "
            "replay last"
        )

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_watchdog_cancels_live_worker_before_requeue(self, monkeypatch):
        """Race-window regression. Murzik review on commit 2 of PR #561.

        In commit 2, the watchdog requeued tail/in-hand turns into
        ``_message_queue`` while the worker was still alive. The worker
        is parked in ``_message_queue.get()`` waiting for the next turn;
        ``put_nowait`` resolves that getter future SYNCHRONOUSLY. So
        the live worker could wake up, pull B/C from the queue, and
        ``_deliver_turn`` them back into the still-wedged REPL BEFORE
        ``force_restart()`` could call ``disconnect()`` and cancel the
        worker. Then ``disconnect()``'s drain would fire B/C's
        completion_events on those abandoned new deque entries —
        recreating the loss/false-completion bug.

        Commit 3 fix: cancel the worker SYNCHRONOUSLY in the watchdog
        critical path BEFORE making the replay visible. The cancelled
        worker's getter future is in CANCELLED state;
        ``asyncio.Queue._wakeup_next`` skips done waiters; so the
        subsequent ``put_nowait`` cannot wake the worker. The replay
        sits in the queue untouched until ``force_restart()`` spawns
        the fresh worker.

        Asserts:
        - Old worker task is done (cancelled) after watchdog trips
        - Replay turns (B, C) are sitting in the queue undisturbed
        - In FIFO order matching original send-order (A→B→C, with A
          abandoned)
        """
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)

        ss, _ = _make_session()
        await ss.connect()
        # CRITICAL DIFFERENCE FROM SIBLING TESTS: do NOT cancel the
        # worker here. The whole point of this regression is that the
        # watchdog must cancel the LIVE worker itself before exposing
        # replay. The worker should be parked in ``_message_queue.get()``
        # right now (queue is empty after connect's wake-prompt skip).
        assert ss._worker_task is not None
        assert not ss._worker_task.done(), (
            "worker should be live and parked on _message_queue.get() "
            "before watchdog trips"
        )
        worker_task_ref = ss._worker_task

        # Stub force_restart so we don't actually disconnect+spawn.
        # We want to inspect post-watchdog state IN PLACE before any
        # post-restart machinery runs. The stub also confirms the
        # watchdog reached scheduling.
        force_called = asyncio.Event()
        async def _stub_force_restart(*, bypass_guard: bool = False):
            force_called.set()
            return True
        ss.force_restart = _stub_force_restart

        # Seed: A (stuck head) + B (tail, pasted) + C (in_hand, pulled
        # but not yet pasted). Original send-order A→B→C.
        completion_a = asyncio.Event()
        completion_b = asyncio.Event()
        completion_c = asyncio.Event()
        _seed_inflight(
            ss, prompt="A", meta={"chat_id": "A"}, completion_event=completion_a,
        )
        _seed_inflight(
            ss, prompt="B", meta={"chat_id": "B"}, completion_event=completion_b,
        )
        ss._inflight_turn = _QueuedTurn(
            prompt="C",
            platform="telegram",
            chat_id="C",
            message_id="mC",
            completion_event=completion_c,
        )
        ss._head_started_at = 0.0

        try:
            await asyncio.wait_for(force_called.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("watchdog must trigger force_restart when head ages out")

        # Give cancellation a tick to propagate through the worker's
        # parked ``_message_queue.get()`` awaitable. The watchdog called
        # ``cancel()`` synchronously but the worker's CancelledError
        # handler only runs when the event loop reschedules it.
        for _ in range(20):
            if worker_task_ref.done():
                break
            await asyncio.sleep(0.01)

        # PRIMARY CLAIM: the old worker is done. No way it can consume
        # replay turns out of the queue.
        assert worker_task_ref.done(), (
            "watchdog must cancel the live worker synchronously before "
            "exposing replay — otherwise the worker races force_restart "
            "and pastes B/C back into the still-wedged REPL"
        )

        # SECONDARY CLAIM: the replay turns are sitting in the queue
        # undisturbed, in FIFO order (B then C). If the live worker
        # had consumed them, the queue would be empty (or contain a
        # subset), and B/C would have been re-pasted into the wedged
        # REPL.
        assert ss._message_queue.qsize() == 2, (
            f"replay must be intact in queue, got qsize="
            f"{ss._message_queue.qsize()}"
        )
        first = ss._message_queue.get_nowait()
        second = ss._message_queue.get_nowait()
        assert first.prompt == "B", "tail entry B replays first (FIFO)"
        assert second.prompt == "C", "in_hand C replays after B"

        # Tail/in_hand events stay UNSET (per the head-only-abandons
        # contract) so the post-restart rerun is what unblocks them.
        assert completion_a.is_set(), "head A's event fires (abandoned)"
        assert not completion_b.is_set(), "tail B's event waits for actual rerun"
        assert not completion_c.is_set(), "in_hand C's event waits for actual rerun"

        # No deque entries leaked.
        assert len(ss._inflight_metas) == 0
        assert ss._inflight_turn is None

    @pytest.mark.asyncio
    async def test_force_restart_bypass_guard_ignores_blocking_guard(self) -> None:
        """``force_restart(bypass_guard=True)`` ignores a guard that
        would otherwise block.

        Murzik review on commit 3 of PR #561. The persistence guard
        preserves completed-but-unsaved state across direct restart
        calls. The watchdog calls with ``bypass_guard=True`` because
        by the time the watchdog fires, the REPL is wedged — the
        guard's "preserve mid-conversation state" premise no longer
        holds. Without this, the watchdog would cancel the worker,
        move replay into the queue, then ``force_restart()`` would
        return False with the session inert (no worker, no watchdog,
        stranded queue).
        """
        guard = MagicMock(return_value={"restart_safe": False, "reason": "stale"})
        ss, tmux = _make_session(restart_guard=guard)
        await ss.connect()
        # Manually mark _has_completed_turn so the guard would trip
        # under a normal force_restart call.
        ss._has_completed_turn = True

        # Sanity: without bypass, the guard blocks.
        result_blocked = await ss.force_restart()
        assert result_blocked is False
        assert tmux.kill_session.await_count == 0, (
            "guard must block kill when bypass_guard=False"
        )

        # With bypass, restart proceeds despite the guard saying no.
        result_bypassed = await ss.force_restart(bypass_guard=True)
        assert result_bypassed is True
        assert tmux.kill_session.await_count >= 1, (
            "bypass_guard=True must drive disconnect→reconnect even when "
            "the persistence guard says no"
        )
        assert ss.state == SessionState.CONNECTED
        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_watchdog_bypasses_guard_to_recover_wedged_repl(self, monkeypatch):
        """Murzik review on commit 3 of PR #561 — full-stack regression.

        The watchdog must drive a reconnect even when a persistence
        guard would otherwise block ``force_restart``. If it didn't,
        the session would silently strand:
        - head's completion_event already fired (abandoned),
        - tail/in_hand replay already moved into ``_message_queue``,
        - the only worker already cancelled,
        - ``force_restart`` returns False, no new worker, no new
          watchdog → session stays CONNECTED but inert.

        Contract: watchdog → ``force_restart(bypass_guard=True)`` →
        disconnect + fresh spawn + fresh worker that drains the
        replay queue.
        """
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)

        # Guard that would block any normal force_restart.
        guard = MagicMock(return_value={"restart_safe": False, "reason": "stale"})
        ss, tmux = _make_session(restart_guard=guard)
        await ss.connect()
        # Mark completed-turn so guard becomes active.
        ss._has_completed_turn = True
        original_worker = ss._worker_task

        # Seed head + tail to trip the watchdog. Head has a completion
        # event so we can verify head-only abandonment ran.
        completion_a = asyncio.Event()
        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"}, completion_event=completion_a)
        _seed_inflight(ss, prompt="B", meta={"chat_id": "B"})
        ss._head_started_at = 0.0

        # Wait until the watchdog has driven the reconnect: kill_session
        # called (disconnect) AND a new worker task spawned that isn't
        # the original.
        for _ in range(200):
            await asyncio.sleep(0.02)
            if (
                tmux.kill_session.await_count >= 1
                and ss._worker_task is not None
                and ss._worker_task is not original_worker
                and not ss._worker_task.done()
            ):
                break
        assert tmux.kill_session.await_count >= 1, (
            "watchdog must drive disconnect (kill_session) via "
            "force_restart(bypass_guard=True) — guard would have blocked "
            "a normal force_restart and stranded the session"
        )
        assert ss._worker_task is not None
        assert ss._worker_task is not original_worker, (
            "watchdog's reconnect must spawn a FRESH worker task — the "
            "original was cancelled in the critical path"
        )
        assert not ss._worker_task.done(), (
            "fresh worker should be live to consume the replay queue"
        )
        assert ss.state == SessionState.CONNECTED
        # Head abandoned, tail's event still unset (waits for replay).
        assert completion_a.is_set()

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_head_clock_does_not_advance_on_subsequent_append(self):
        """Appending another entry behind an existing head must NOT
        reset the head clock — that would game the watchdog. The clock
        only resets on empty→nonempty append (new head) or on popleft
        with remaining entries (new head)."""
        ss, _ = _make_session_with_response_cb()
        ss._state_machine._state = SessionState.CONNECTED

        _seed_inflight(ss, meta={"chat_id": "A"})
        first_head_at = ss._head_started_at
        assert first_head_at is not None
        await asyncio.sleep(0.01)
        # Append second entry — head clock must NOT advance.
        _seed_inflight(ss, meta={"chat_id": "B"})
        assert ss._head_started_at == first_head_at, (
            "appending behind an existing head must not reset the head "
            "clock — the original head is what the watchdog ages"
        )


# ──────────────────────────────────────────────────────────────────────────
# #846 — inflight watchdog kill-switch (watchdog_config.enabled) + replay caps
# ──────────────────────────────────────────────────────────────────────────


class TestInflightWatchdogKillSwitch:
    """(b) ``watchdog_config.enabled=false`` disables the per-session inflight
    watchdog's force_restart decision — the same operator kill-switch the
    daemon SessionWatchdog already respects (#846)."""

    def test_watchdog_enabled_defaults_true_and_fails_open(self):
        ss, _ = _make_session()
        # No fn wired → enabled (default True).
        assert ss._config.watchdog_enabled_fn is None
        assert ss._watchdog_enabled() is True
        # Explicit False.
        ss._config.watchdog_enabled_fn = lambda: False
        assert ss._watchdog_enabled() is False
        # Explicit True.
        ss._config.watchdog_enabled_fn = lambda: True
        assert ss._watchdog_enabled() is True

        # A raising fn must fail OPEN (return True) — a wiring bug must never
        # silently disable stuck-REPL recovery.
        def _boom():
            raise RuntimeError("config lookup exploded")

        ss._config.watchdog_enabled_fn = _boom
        assert ss._watchdog_enabled() is True

    @pytest.mark.asyncio
    async def test_disabled_watchdog_skips_force_restart(self, monkeypatch):
        """enabled=false → aged-out head with NULL live-status (would be
        'wedged') is NOT force_restarted, and the deque is left intact. The
        task stays alive so re-enabling takes effect live."""
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)

        ss, _ = _make_session()
        ss._config.watchdog_enabled_fn = lambda: False
        await ss.connect()
        # Isolate the watchdog from the worker.
        ss._worker_task.cancel()
        try:
            await ss._worker_task
        except asyncio.CancelledError:
            pass

        force_called = asyncio.Event()

        async def _stub(*, bypass_guard: bool = False):
            force_called.set()
            return True

        ss.force_restart = _stub

        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})
        ss._head_started_at = 0.0  # ancient → would trip immediately
        assert ss._config.live_status_fn is None  # verdict would be "wedged"

        # Give the watchdog many ticks — it must NOT force_restart.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(force_called.wait(), timeout=0.3)
        assert not force_called.is_set()
        assert len(ss._inflight_metas) == 1, "disabled watchdog must not drain"
        assert ss._stats.get("turn_timeouts", 0) == 0

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_enabled_watchdog_still_fires(self, monkeypatch):
        """enabled=true (explicit) → aged-out quiet head with null live-status
        still force_restarts, as before. Confirms the gate doesn't break the
        normal recovery path."""
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)
        # Disable pane-liveness so the wedged path doesn't spend the ~1.5s
        # capture-pane sampling gap (orthogonal to the kill-switch under test).
        monkeypatch.setenv("PINKY_WATCHDOG_PANE_LIVENESS", "0")

        ss, _ = _make_session()
        ss._config.watchdog_enabled_fn = lambda: True
        await ss.connect()
        ss._worker_task.cancel()
        try:
            await ss._worker_task
        except asyncio.CancelledError:
            pass

        force_called = asyncio.Event()

        async def _stub(*, bypass_guard: bool = False):
            force_called.set()
            return True

        ss.force_restart = _stub

        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})
        ss._head_started_at = 0.0

        try:
            await asyncio.wait_for(force_called.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("enabled watchdog must still force_restart a wedged head")
        assert ss._stats.get("turn_timeouts", 0) == 1

        await ss.disconnect()


class TestInflightWatchdogReplayCaps:
    """(c) replay-amplification defenses on the watchdog requeue path (#846)."""

    async def _wedge_once(self, ss, monkeypatch):
        """Shrink the watchdog timers, disable pane-liveness (orthogonal ~1.5s
        capture-pane gap), cancel the worker so it can't re-pull the requeue,
        and stub force_restart. Returns an Event set when force_restart fires."""
        from pinky_daemon import tmux_session as _ts
        monkeypatch.setattr(_ts, "_TURN_DONE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(_ts, "_WATCHDOG_TICK_SEC", 0.02)
        monkeypatch.setenv("PINKY_WATCHDOG_PANE_LIVENESS", "0")
        ss._worker_task.cancel()
        try:
            await ss._worker_task
        except asyncio.CancelledError:
            pass
        force_called = asyncio.Event()

        async def _stub(*, bypass_guard: bool = False):
            force_called.set()
            return True

        ss.force_restart = _stub
        return force_called

    @pytest.mark.asyncio
    async def test_single_wedge_requeues_and_bumps_replay_count(self, monkeypatch):
        """Single wedge → tail requeued normally, replay_count → 1."""
        ss, _ = _make_session()
        await ss.connect()
        force_called = await self._wedge_once(ss, monkeypatch)

        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})  # head
        b_entry = _seed_inflight(ss, prompt="B", meta={"chat_id": "B"})  # tail
        ss._head_started_at = 0.0

        await asyncio.wait_for(force_called.wait(), timeout=2.0)

        assert ss._message_queue.qsize() == 1
        b_replay = ss._message_queue.get_nowait()
        assert b_replay.prompt == "B"
        assert b_replay is b_entry.turn
        assert b_replay.replay_count == 1, "each requeue bumps the counter"

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_turn_dropped_after_replay_cap(self, monkeypatch):
        """A turn already replayed cap-many times is DROPPED (not requeued);
        its completion_event fires so any waiter unblocks."""
        monkeypatch.setenv("PINKY_INFLIGHT_REPLAY_CAP", "3")

        ss, _ = _make_session()
        await ss.connect()
        force_called = await self._wedge_once(ss, monkeypatch)

        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})  # head
        b_event = asyncio.Event()
        b_entry = _seed_inflight(
            ss, prompt="B", meta={"chat_id": "B"}, completion_event=b_event
        )
        # B has already been replayed 3 times (== cap). This wedge bumps it to
        # 4 > 3 → drop.
        b_entry.turn.replay_count = 3
        ss._head_started_at = 0.0

        await asyncio.wait_for(force_called.wait(), timeout=2.0)

        assert ss._message_queue.qsize() == 0, "over-cap turn must NOT be requeued"
        assert b_event.is_set(), "dropped turn fires its completion_event"

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_replay_cap_zero_disables_drop(self, monkeypatch):
        """PINKY_INFLIGHT_REPLAY_CAP=0 disables the cap — high replay_count
        still requeues (ops revert lever, no deploy)."""
        monkeypatch.setenv("PINKY_INFLIGHT_REPLAY_CAP", "0")

        ss, _ = _make_session()
        await ss.connect()
        force_called = await self._wedge_once(ss, monkeypatch)

        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})
        b_entry = _seed_inflight(ss, prompt="B", meta={"chat_id": "B"})
        b_entry.turn.replay_count = 99  # way past any cap
        ss._head_started_at = 0.0

        await asyncio.wait_for(force_called.wait(), timeout=2.0)

        assert ss._message_queue.qsize() == 1, "cap=0 disables the drop"
        assert ss._message_queue.get_nowait().prompt == "B"

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_completed_turn_not_requeued(self, monkeypatch):
        """A tail turn whose completion_event is ALREADY set (answered) must not
        be requeued — that is the murzik duplicate-ack amplification."""
        ss, _ = _make_session()
        await ss.connect()
        force_called = await self._wedge_once(ss, monkeypatch)

        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})  # head
        done_event = asyncio.Event()
        done_event.set()  # B already completed
        b_entry = _seed_inflight(
            ss, prompt="B", meta={"chat_id": "B"}, completion_event=done_event
        )
        ss._head_started_at = 0.0

        await asyncio.wait_for(force_called.wait(), timeout=2.0)

        assert ss._message_queue.qsize() == 0, "already-completed turn not requeued"
        assert b_entry.turn.replay_count == 0, "skip is before the counter bump"

        await ss.disconnect()

    @pytest.mark.asyncio
    async def test_tail_cap_respected(self, monkeypatch):
        """No more than PINKY_INFLIGHT_REPLAY_TAIL_CAP tail entries are requeued
        per restart; the overflow is dropped with completion_events fired."""
        monkeypatch.setenv("PINKY_INFLIGHT_REPLAY_TAIL_CAP", "2")

        ss, _ = _make_session()
        await ss.connect()
        force_called = await self._wedge_once(ss, monkeypatch)

        _seed_inflight(ss, prompt="A", meta={"chat_id": "A"})  # head
        events = {}
        for name in ("B", "C", "D", "E"):
            ev = asyncio.Event()
            events[name] = ev
            _seed_inflight(ss, prompt=name, meta={"chat_id": name}, completion_event=ev)
        ss._head_started_at = 0.0

        await asyncio.wait_for(force_called.wait(), timeout=2.0)

        # Only the first 2 tail entries (B, C) requeued.
        assert ss._message_queue.qsize() == 2
        assert ss._message_queue.get_nowait().prompt == "B"
        assert ss._message_queue.get_nowait().prompt == "C"
        # Dropped overflow (D, E) fired their completion_events; requeued ones
        # (B, C) did NOT (they wait for the actual rerun).
        assert not events["B"].is_set()
        assert not events["C"].is_set()
        assert events["D"].is_set()
        assert events["E"].is_set()

        await ss.disconnect()


# ──────────────────────────────────────────────────────────────────────────
# #108 — StopFailure POST resolves the in-flight turn (turn-end-detection gap)
# ──────────────────────────────────────────────────────────────────────────


class TestHandleStopFailure:
    """``TmuxSession.handle_stop_failure`` — make the #584 StopFailure POST
    the authoritative turn-end signal so a terminal API-error turn doesn't
    wedge at the deque head until the 10-min inflight watchdog.

    Test matrix (Murzik, #108): external inflight, internal inflight,
    no-inflight idempotence, FIFO advance (A fails → B becomes head),
    late stop_hook_summary → no double callback, buffer drain, session_id
    is log-only.
    """

    @pytest.mark.asyncio
    async def test_resolves_external_inflight(self) -> None:
        """External in-flight turn → response_callback fires with a
        ``stop_failure:<type>`` stop_reason and the deque drains."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        _seed_inflight(
            ss,
            meta={"platform": "telegram", "chat_id": "123", "message_id": "m1"},
        )

        resolved = await ss.handle_stop_failure("rate_limit")

        assert resolved is True
        assert len(cb.calls) == 1
        result = cb.calls[0]
        assert result.stop_reason == "stop_failure:rate_limit"
        assert result.chat_id == "123"
        assert result.message_id == "m1"
        # Default human-legible failure note when CC sent no message.
        assert result.response_text == "Claude Code turn failed: rate_limit"
        # Turn fully resolved — deque empty, back-compat signal set.
        assert len(ss._inflight_metas) == 0
        assert ss._head_started_at is None
        assert ss._turn_done.is_set()

    @pytest.mark.asyncio
    async def test_uses_cc_message_as_text_when_provided(self) -> None:
        """CC's rendered error text (``message``) is surfaced verbatim
        when present, instead of the synthesized fallback."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        _seed_inflight(ss, meta={"chat_id": "c"})

        await ss.handle_stop_failure(
            "authentication_failed", "API Error: 401 Unauthorized"
        )

        assert cb.calls[0].response_text == "API Error: 401 Unauthorized"
        assert cb.calls[0].stop_reason == "stop_failure:authentication_failed"

    @pytest.mark.asyncio
    async def test_resolves_internal_inflight_suppresses_callback(self) -> None:
        """Internal in-flight turn → completion_event fires + deque drains,
        but response_callback is suppressed (no external recipient)."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        ev = asyncio.Event()
        _seed_inflight(ss, internal=True, completion_event=ev)

        resolved = await ss.handle_stop_failure("server_error")

        assert resolved is True
        # Internal turn: no broker callback (its routing meta is empty).
        assert cb.calls == []
        # But the waiter is released and the deque drained.
        assert ev.is_set()
        assert len(ss._inflight_metas) == 0

    @pytest.mark.asyncio
    async def test_no_inflight_is_idempotent_noop(self) -> None:
        """Empty deque → idempotent no-op: return False, fire nothing.
        Covers the double-POST / already-resolved race."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        assert len(ss._inflight_metas) == 0

        resolved = await ss.handle_stop_failure("rate_limit")

        assert resolved is False
        assert cb.calls == []

    @pytest.mark.asyncio
    async def test_fifo_advance_a_fails_b_becomes_head(self) -> None:
        """A (head) + B in flight; A's StopFailure pops A and B inherits
        the head with a fresh timeout window. Only A is resolved."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        _seed_inflight(ss, meta={"chat_id": "A", "message_id": "ma"})
        head_at_before = ss._head_started_at
        _seed_inflight(ss, meta={"chat_id": "B", "message_id": "mb"})
        await asyncio.sleep(0.01)

        resolved = await ss.handle_stop_failure("rate_limit")

        assert resolved is True
        # Exactly A resolved through the callback.
        assert len(cb.calls) == 1
        assert cb.calls[0].chat_id == "A"
        # B is now the head, deque depth 1.
        assert len(ss._inflight_metas) == 1
        assert ss._inflight_metas[0].meta["chat_id"] == "B"
        # Head clock advanced to B's window (fresh, not A's original).
        assert ss._head_started_at is not None
        assert ss._head_started_at > head_at_before

    @pytest.mark.asyncio
    async def test_late_stop_hook_summary_no_double_callback(self) -> None:
        """After StopFailure resolves the only in-flight turn, a late
        ``stop_hook_summary`` for that turn finds an empty deque and is a
        harmless no-op — no second callback (#496 Case-1 defense reused)."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        _seed_inflight(ss, meta={"chat_id": "c", "message_id": "m1"})

        await ss.handle_stop_failure("rate_limit")
        assert len(cb.calls) == 1

        # Late stop_hook_summary lands for the already-resolved turn.
        await ss._handle_turn_complete(
            TurnResponse(text="late straggler", stop_reason="end_turn")
        )

        # Still exactly one callback — the stop_failure resolve. The late
        # hook hit the empty-on-pop defense and bailed.
        assert len(cb.calls) == 1
        assert cb.calls[0].stop_reason == "stop_failure:rate_limit"

    @pytest.mark.asyncio
    async def test_drains_tailer_buffer_on_resolve(self) -> None:
        """The tailer's in-progress buffer is drained so partial failed-turn
        text can't bleed into the next real stop_hook_summary."""
        ss, _ = _make_session_with_response_cb()
        ss._state_machine._state = SessionState.CONNECTED
        ss._tailer = MagicMock()
        _seed_inflight(ss, meta={"chat_id": "c"})

        await ss.handle_stop_failure("rate_limit")

        ss._tailer.drain_buffer.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_deque_does_not_drain(self) -> None:
        """The idempotent empty-deque path must NOT drain — there's no
        failed turn, and draining could discard an accumulating next turn."""
        ss, _ = _make_session_with_response_cb()
        ss._state_machine._state = SessionState.CONNECTED
        ss._tailer = MagicMock()

        resolved = await ss.handle_stop_failure("rate_limit")

        assert resolved is False
        ss._tailer.drain_buffer.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_tailer_does_not_crash(self) -> None:
        """Resolve still works when no tailer is attached (pre-spawn / unit
        seams) — the drain is guarded."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        assert ss._tailer is None
        _seed_inflight(ss, meta={"chat_id": "c"})

        resolved = await ss.handle_stop_failure("rate_limit")

        assert resolved is True
        assert len(cb.calls) == 1

    @pytest.mark.asyncio
    async def test_session_id_is_log_context_not_a_gate(self) -> None:
        """A mismatched / foreign session_id must NOT block unwedging the
        only live in-flight turn — it's log context only."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        _seed_inflight(ss, meta={"chat_id": "c", "message_id": "m1"})

        resolved = await ss.handle_stop_failure(
            "rate_limit", "", session_id="some-other-session-uuid"
        )

        assert resolved is True
        assert len(cb.calls) == 1

    @pytest.mark.asyncio
    async def test_blank_error_type_defaults_unknown(self) -> None:
        """A blank/whitespace error_type normalizes to ``unknown`` in the
        stop_reason."""
        cb = _AsyncCollector()
        ss, _ = _make_session_with_response_cb(response_cb=cb)
        ss._state_machine._state = SessionState.CONNECTED
        _seed_inflight(ss, meta={"chat_id": "c"})

        await ss.handle_stop_failure("   ")

        assert cb.calls[0].stop_reason == "stop_failure:unknown"

    @pytest.mark.asyncio
    async def test_late_a_stop_hook_does_not_false_complete_b(
        self, tmp_path
    ) -> None:
        """FIFO false-completion regression (Murzik, PR #585).

        A + B in flight. A's StopFailure resolves A, and A's late
        ``stop_hook_summary`` lands WHILE B is the new head — during the
        very await window inside ``_handle_turn_complete`` (the buffer
        drain must therefore run BEFORE that method's first await, in the
        same no-await span as the synchronous pop).

        The race is reproduced deterministically by driving the late tailer
        read from inside the response_callback — which IS one of
        ``_handle_turn_complete``'s awaited steps, i.e. exactly the
        interleaving point. With the drain ordered correctly the late stop
        hook reads an empty buffer and is absorbed by the tailer's
        ``is_empty`` branch (no callback); B stays in flight. With the
        buggy drain-after-await ordering, the late stop hook reads A's
        stale buffered text and falsely pops/completes B.
        """
        transcript = tmp_path / "synthetic.jsonl"
        transcript.write_text("")

        # A's content WITHOUT a stop_hook yet — accumulates in the buffer.
        a_entries = [
            {
                "type": "user",
                "timestamp": "2026-05-14T05:00:00.000Z",
                "message": {"role": "user", "content": "hi"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-14T05:00:00.100Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial A work"}],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            },
        ]
        a_text = "\n".join(_json.dumps(e) for e in a_entries) + "\n"
        late_stop_hook = (
            _json.dumps(
                {
                    "type": "system",
                    "subtype": "stop_hook_summary",
                    "timestamp": "2026-05-14T05:00:01.000Z",
                }
            )
            + "\n"
        )

        calls: list[TurnResponse] = []
        fired = {"done": False}

        async def _cb(response):
            calls.append(response)
            # On A's resolve callback (which runs DURING
            # _handle_turn_complete's await chain), simulate A's late
            # stop_hook_summary landing while B is the head.
            if not fired["done"]:
                fired["done"] = True
                transcript.write_text(a_text + late_stop_hook)
                await ss._tailer.read_once()

        ss, _ = _make_session_with_response_cb(response_cb=_cb)
        ss._state_machine._state = SessionState.CONNECTED
        # Real tailer wired to this session — but DON'T start its background
        # loop; drive reads manually via read_once() for determinism.
        ss._tailer = TmuxTranscriptTailer(
            transcript_path=transcript,
            on_turn_complete=ss._handle_turn_complete,
            agent_name=ss.agent_name,
        )
        ss._tailer.set_offset(0)

        # Pre-read A's assistant content into the buffer (no stop_hook → no
        # fire), so the buggy drain-after path would still see A's stale
        # text when the late stop_hook is read.
        transcript.write_text(a_text)
        await ss._tailer.read_once()
        assert not ss._tailer._buffer.is_empty
        assert calls == []  # nothing fired yet — no stop_hook seen

        # Seed A (head) + B; B carries a completion_event to prove it is
        # NOT falsely released by the late A stop hook.
        b_event = asyncio.Event()
        _seed_inflight(
            ss, meta={"platform": "telegram", "chat_id": "A", "message_id": "ma"}
        )
        _seed_inflight(
            ss,
            meta={"platform": "telegram", "chat_id": "B", "message_id": "mb"},
            completion_event=b_event,
        )

        resolved = await ss.handle_stop_failure("rate_limit")

        assert resolved is True
        # Exactly ONE callback — A's resolve. B must NOT have been completed
        # by the late A stop hook.
        assert len(calls) == 1, (
            "late A stop_hook falsely fired a second completion — drain must "
            "run before _handle_turn_complete's awaits"
        )
        assert calls[0].chat_id == "A"
        assert calls[0].stop_reason == "stop_failure:rate_limit"
        # B still in flight, waiter still waiting.
        assert len(ss._inflight_metas) == 1
        assert ss._inflight_metas[0].meta["chat_id"] == "B"
        assert not b_event.is_set()


@pytest.mark.asyncio
async def test_deliver_turn_fires_on_delivered_after_paste_success() -> None:
    """#591 P1#2 (Murzik round-2): for wake turns, the
    ``on_delivered`` callback (carried on the _QueuedTurn) must fire
    AFTER paste_text succeeds — not at enqueue time. This pins the
    "boundary advances on confirmed delivery" invariant; enqueue-time
    fire would let context-lock deferrals or stuck pastes advance
    the #591 cycle-gate against a wake that never reached the model.
    """
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_ok())
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)

    fires: list[str] = []
    turn = _QueuedTurn(
        prompt="wake prompt body",
        platform="",
        chat_id="",
        message_id="",
        internal=True,
        reason="wake_resume",
        on_delivered=lambda: fires.append("delivered"),
    )

    await ss._deliver_turn(turn)

    tmux.paste_text.assert_awaited_once()
    assert fires == ["delivered"], (
        "on_delivered must fire exactly once after paste-success"
    )


@pytest.mark.asyncio
async def test_deliver_turn_skips_on_delivered_on_paste_failure() -> None:
    """#591 P1#2 (Murzik round-2): on paste_text failure, _deliver_turn
    raises BEFORE the on_delivered fire site. This pins the
    failure-doesn't-advance-boundary invariant — a wedged paste leaves
    the cycle-gate boundary intact so the next attempt re-emits the
    directive.
    """
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_fail("rc=1"))
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)

    fires: list[str] = []
    turn = _QueuedTurn(
        prompt="wake prompt body",
        platform="",
        chat_id="",
        message_id="",
        internal=True,
        reason="wake_resume",
        on_delivered=lambda: fires.append("delivered"),
    )

    with pytest.raises(RuntimeError):
        await ss._deliver_turn(turn)

    assert fires == [], (
        "on_delivered MUST NOT fire on paste-failure — boundary stays put "
        "so next attempt re-emits the directive"
    )


@pytest.mark.asyncio
async def test_deliver_turn_no_on_delivered_is_safe() -> None:
    """#591 P1#2: external (non-wake) turns have no on_delivered set;
    _deliver_turn must handle the None gracefully without firing
    anything. Regression guard for the external-message path.
    """
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(return_value=_ok())
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)

    # External turn — no on_delivered (default None).
    turn = _QueuedTurn(
        prompt="hi dymok",
        platform="telegram",
        chat_id="123",
        message_id="m1",
    )

    await ss._deliver_turn(turn)
    tmux.paste_text.assert_awaited_once()  # delivered cleanly, no crash


# --------------------------------------------------------------------------
# Worker transient-timeout retry + delivery-failure notice
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_retries_turn_after_tmux_timeout(monkeypatch) -> None:
    """A tmux command timeout (asyncio.TimeoutError from _TmuxControl._run's
    5s subprocess ceiling) is transient: the worker must keep the turn in
    hand and retry instead of silently dropping the user's message."""
    monkeypatch.setattr(tmux_session, "_TRANSIENT_RETRY_BACKOFF_SEC", 0)
    ss, _ = _make_session(state=SessionState.CONNECTED)

    attempts: list[str] = []

    async def flaky_deliver(turn):
        attempts.append(turn.prompt)
        if len(attempts) < 3:
            raise asyncio.TimeoutError("tmux server busy")

    ss._deliver_turn = flaky_deliver
    ss._message_queue.put_nowait(
        _QueuedTurn(prompt="keep me", platform="telegram", chat_id="c", message_id="m")
    )

    worker = asyncio.create_task(ss._message_worker())
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(attempts) >= 3 and ss._inflight_turn is None:
                break
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    assert attempts == ["keep me"] * 3, "same turn must be retried, not dropped"
    assert ss._stats["turns"] == 1
    assert ss._inflight_turn is None


@pytest.mark.asyncio
async def test_worker_gives_up_after_timeout_budget_and_notifies_chat(monkeypatch) -> None:
    """When the timeout retry budget is exhausted, the turn is dropped but
    the sending chat gets a delivery-failure notice instead of dead
    silence."""
    monkeypatch.setattr(tmux_session, "_TRANSIENT_RETRY_BACKOFF_SEC", 0)
    ss, _ = _make_session(state=SessionState.CONNECTED)

    notices: list[TurnResponse] = []

    async def record_notice(resp):
        notices.append(resp)

    ss._response_callback = record_notice

    attempts = 0

    async def always_timeout(turn):
        nonlocal attempts
        attempts += 1
        raise asyncio.TimeoutError("tmux server busy")

    ss._deliver_turn = always_timeout
    ss._message_queue.put_nowait(
        _QueuedTurn(prompt="lost", platform="telegram", chat_id="c1", message_id="m1")
    )

    worker = asyncio.create_task(ss._message_worker())
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if notices:
                break
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    assert attempts == tmux_session._DELIVERY_TIMEOUT_RETRY_LIMIT
    assert ss._inflight_turn is None
    assert len(notices) == 1
    assert notices[0].platform == "telegram"
    assert notices[0].chat_id == "c1"
    assert notices[0].message_id == "m1"
    assert notices[0].stop_reason == "delivery_error"
    assert "delivery" in notices[0].text.lower()


@pytest.mark.asyncio
async def test_worker_timeout_after_landed_paste_does_not_repaste(monkeypatch) -> None:
    """A tmux timeout that expires AFTER the paste+submit actually landed
    must not re-paste the turn -- a side-effecting instruction would run
    twice. The capture-pane guard sees the prompt in the pane, so the
    worker records the delivery and moves on."""
    monkeypatch.setattr(tmux_session, "_TRANSIENT_RETRY_BACKOFF_SEC", 0)
    prompt = "please deploy release 26.06.001 to production now"
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(side_effect=asyncio.TimeoutError("tmux busy"))
    tmux.capture_pane = AsyncMock(
        return_value=TmuxCommandResult(
            returncode=0, stdout=f"> {prompt}\nesc to interrupt", stderr=""
        )
    )
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    ss._message_queue.put_nowait(
        _QueuedTurn(prompt=prompt, platform="telegram", chat_id="c", message_id="m")
    )

    worker = asyncio.create_task(ss._message_worker())
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if ss._inflight_metas and ss._inflight_turn is None:
                break
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    assert tmux.paste_text.await_count == 1, "must not re-paste a landed turn"
    assert len(ss._inflight_metas) == 1
    assert ss._inflight_metas[0].meta["chat_id"] == "c"
    assert ss._stats["turns"] == 1
    assert ss._inflight_turn is None


@pytest.mark.asyncio
async def test_worker_timeout_without_landed_paste_retries(monkeypatch) -> None:
    """When the pane shows no trace of the prompt after a timeout, the
    paste never landed: the worker must re-paste rather than treat the
    turn as delivered (a false 'landed' verdict would drop the message)."""
    monkeypatch.setattr(tmux_session, "_TRANSIENT_RETRY_BACKOFF_SEC", 0)
    prompt = "please deploy release 26.06.001 to production now"
    tmux = _make_mock_tmux()
    tmux.paste_text = AsyncMock(
        side_effect=[asyncio.TimeoutError("tmux busy"), _ok()]
    )
    tmux.capture_pane = AsyncMock(
        return_value=TmuxCommandResult(returncode=0, stdout="unrelated pane", stderr="")
    )
    ss, _ = _make_session(state=SessionState.CONNECTED, tmux=tmux)
    ss._message_queue.put_nowait(
        _QueuedTurn(prompt=prompt, platform="telegram", chat_id="c", message_id="m")
    )

    worker = asyncio.create_task(ss._message_worker())
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if ss._inflight_metas and ss._inflight_turn is None:
                break
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    assert tmux.paste_text.await_count == 2, "unlanded paste must be retried"
    assert len(ss._inflight_metas) == 1
    assert ss._stats["turns"] == 1


@pytest.mark.asyncio
async def test_worker_notifies_chat_on_permanent_delivery_failure() -> None:
    """A permanent delivery failure (paste-buffer/send-keys error) must
    route a delivery-failure notice to the external sender."""
    ss, _ = _make_session(state=SessionState.CONNECTED)

    notices: list[TurnResponse] = []

    async def record_notice(resp):
        notices.append(resp)

    ss._response_callback = record_notice

    async def boom(turn):
        raise RuntimeError("tmux paste-buffer / send-keys failed: rc=1")

    ss._deliver_turn = boom
    ss._message_queue.put_nowait(
        _QueuedTurn(prompt="x", platform="discord", chat_id="c2", message_id="m2")
    )

    worker = asyncio.create_task(ss._message_worker())
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if notices:
                break
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    assert len(notices) == 1
    assert notices[0].chat_id == "c2"
    assert ss._inflight_turn is None


@pytest.mark.asyncio
async def test_delivery_failure_notice_skips_internal_turns() -> None:
    """Internal turns have no chat target; the notice helper must not
    route anything for them."""
    ss, _ = _make_session(state=SessionState.CONNECTED)

    notices: list[TurnResponse] = []

    async def record_notice(resp):
        notices.append(resp)

    ss._response_callback = record_notice
    await ss._notify_delivery_failure(
        _QueuedTurn(prompt="wake", internal=True, reason="wake_resume")
    )
    assert notices == []


# --------------------------------------------------------------------------
# attempt_reconnect wake-prompt re-prime (#589 parity)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attempt_reconnect_enqueues_resume_wake_prompt() -> None:
    """attempt_reconnect (the heartbeat-resurrect path) must re-prime the
    agent with an orientation wake prompt after respawn, exactly like
    connect() and force_restart() do. Pre-fix it respawned the REPL and
    restarted the worker but never enqueued a wake prompt, so a
    resurrected agent came back orientationless (the #589 symptom on a
    third lifecycle path)."""
    ss, _ = _make_session(state=SessionState.DEAD)
    ss._skip_wake_prompt_for_tests = False
    ss._has_prior_transcript = lambda: True

    enqueued: list[tuple[str, bool]] = []

    async def _record(
        prompt,
        *,
        reason,
        wait_for_completion=False,
        timeout_sec=None,
        front=False,
        on_delivered=None,
    ):
        enqueued.append((reason, front))
        return None

    ss._enqueue_internal_prompt = _record

    import pinky_daemon.tmux_session as ts_mod
    original_backoff = ts_mod._RECONNECT_BACKOFF
    ts_mod._RECONNECT_BACKOFF = (0,)
    try:
        await ss.attempt_reconnect()
        assert ss.state == SessionState.CONNECTED
    finally:
        ts_mod._RECONNECT_BACKOFF = original_backoff

    wake = [e for e in enqueued if e[0].startswith("wake_")]
    assert len(wake) == 1, f"expected exactly one wake prompt, got {enqueued}"
    assert wake[0][0] == "wake_resume"
    assert wake[0][1] is True, "reconnect wake prompt must be front-enqueued"
    await ss.disconnect()


# --------------------------------------------------------------------------
# stats: pending_responses is backlog-only; inflight_turns is separate
# --------------------------------------------------------------------------


def test_stats_pending_responses_excludes_inflight_turns() -> None:
    """pending_responses is the key session_watchdog's require_backlog
    gate reads, so it must count ONLY undelivered queue backlog. An
    in-flight turn must not arm the outer watchdog (it lacks the inner
    _inflight_watchdog's liveness carve-outs and would warn/auto-recover
    mid-turn on any long turn); the running span is exposed separately
    as inflight_turns for busy-state consumers."""
    ss, _ = _make_session(state=SessionState.CONNECTED)
    assert ss.stats["pending_responses"] == 0
    assert ss.stats["inflight_turns"] == 0

    _seed_inflight(ss, meta={"platform": "t", "chat_id": "c", "message_id": "m"})
    assert ss.stats["pending_responses"] == 0
    assert ss.stats["inflight_turns"] == 1

    ss._message_queue.put_nowait(_QueuedTurn(prompt="queued"))
    assert ss.stats["pending_responses"] == 1
    assert ss.stats["inflight_turns"] == 1

# ──────────────────────────────────────────────────────────────────────────
# send_literal / send_pane_keys: typeable pane view (operator input from the
# terminal modal — born from lera's container rollout, #735)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_literal_passes_l_flag() -> None:
    """Literal sends must use ``send-keys -l`` so tmux performs no keyname
    interpretation — operator-typed "Enter" is five letters, not a submit."""
    tmux = _TmuxControl("pinky-test")
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return _ok()

    tmux._run = fake_run
    await tmux.send_literal("Enter")

    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "send-keys"
    assert "-l" in args
    assert args[-1] == "Enter"
    # -l must precede the text argument (tmux flag ordering).
    assert args.index("-l") < args.index("Enter")


@pytest.mark.asyncio
async def test_send_pane_keys_text_goes_literal() -> None:
    session, tmux = _make_session()
    tmux.send_literal = AsyncMock(return_value=_ok())
    tmux.send_keys = AsyncMock(return_value=_ok())

    ok = await session.send_pane_keys(text="hello")

    assert ok is True
    tmux.send_literal.assert_awaited_once_with("hello")
    tmux.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_pane_keys_named_key_goes_send_keys() -> None:
    session, tmux = _make_session()
    tmux.send_literal = AsyncMock(return_value=_ok())
    tmux.send_keys = AsyncMock(return_value=_ok())

    ok = await session.send_pane_keys(key="Enter")

    assert ok is True
    tmux.send_keys.assert_awaited_once_with("Enter", enter=False)
    tmux.send_literal.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_pane_keys_requires_exactly_one_mode() -> None:
    """Both or neither of text/key is a caller bug — refuse without
    touching tmux."""
    session, tmux = _make_session()
    tmux.send_literal = AsyncMock(return_value=_ok())
    tmux.send_keys = AsyncMock(return_value=_ok())

    assert await session.send_pane_keys() is False
    assert await session.send_pane_keys(text="x", key="Enter") is False
    tmux.send_literal.assert_not_awaited()
    tmux.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_pane_keys_rejects_non_whitelisted_key() -> None:
    """Key names outside PANE_KEY_WHITELIST never reach tmux — the
    whitelist is the API's security boundary for named keys."""
    session, tmux = _make_session()
    tmux.send_keys = AsyncMock(return_value=_ok())

    assert await session.send_pane_keys(key="C-d") is False
    assert await session.send_pane_keys(key="kill-server") is False
    tmux.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_pane_keys_rejects_control_chars_in_text() -> None:
    """Control bytes in the literal channel never reach tmux — a literal
    "\\x04" is C-d in the pane, which would bypass the named-key
    whitelist's explicit C-d exclusion."""
    session, tmux = _make_session()
    tmux.send_literal = AsyncMock(return_value=_ok())

    assert await session.send_pane_keys(text="\x04") is False  # C-d
    assert await session.send_pane_keys(text="ok\x04") is False  # embedded
    assert await session.send_pane_keys(text="\x1b[A") is False  # raw ESC seq
    assert await session.send_pane_keys(text="\x7f") is False  # DEL
    assert await session.send_pane_keys(text="a\nb") is False  # newline
    tmux.send_literal.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_pane_keys_allows_printable_text() -> None:
    """The control-char guard must not over-reject: printable ASCII,
    spaces, and non-ASCII (IME input, emoji) all pass through."""
    session, tmux = _make_session()
    tmux.send_literal = AsyncMock(return_value=_ok())

    assert await session.send_pane_keys(text="ls -la ~/файл 🐈") is True
    tmux.send_literal.assert_awaited_once_with("ls -la ~/файл 🐈")


@pytest.mark.asyncio
async def test_send_pane_keys_swallows_exceptions() -> None:
    """Same defensive posture as get_pane_snapshot/resize_pane: a tmux
    blip logs + returns False, never raises into the API layer."""
    session, tmux = _make_session()
    tmux.send_literal = AsyncMock(side_effect=RuntimeError("kaboom"))

    assert await session.send_pane_keys(text="x") is False


@pytest.mark.asyncio
async def test_send_pane_keys_false_on_tmux_failure() -> None:
    session, tmux = _make_session()
    tmux.send_keys = AsyncMock(return_value=_fail("no server running"))

    assert await session.send_pane_keys(key="Enter") is False


# ──────────────────────────────────────────────────────────────────────────
# #230 — _watchdog_liveness: live carve-out signal for the OUTER watchdogs
# (daemon SessionWatchdog warn/recover + scheduler idle-sleep). Active ONLY
# when an in-flight turn is genuinely busy right now; computed live (never
# latched) so it releases the instant liveness stops.
# ──────────────────────────────────────────────────────────────────────────


def _point_transcript(ss: TmuxSession, path) -> None:
    """Point the session's tailer at ``path`` for liveness mtime checks."""
    ss._tailer = SimpleNamespace(transcript_path=str(path))


def _age_file(path, seconds: float) -> None:
    """Backdate a file's mtime by ``seconds`` so it reads as 'old'."""
    old = _time.time() - seconds
    os.utime(path, (old, old))


def test_watchdog_liveness_inactive_without_inflight_turn(tmp_path) -> None:
    ss, _ = _make_session()
    # Even with a freshly-written transcript, NO in-flight turn → not active:
    # a session between turns must be sleepable/recoverable as normal.
    main = tmp_path / "session.jsonl"
    main.write_text("{}")
    _point_transcript(ss, main)
    live = ss._watchdog_liveness(_time.time())
    assert live["active"] is False
    assert live["reason"] == "no_inflight_turn"


def test_watchdog_liveness_foreground_tool_in_flight(tmp_path) -> None:
    ss, _ = _make_session()
    _seed_inflight(ss)
    ss._inflight_tool_calls = {"tool-1": _time.time()}
    live = ss._watchdog_liveness(_time.time())
    assert live["active"] is True
    assert live["reason"] == "foreground_tool_in_flight"
    assert live["age_s"] is not None


def test_watchdog_liveness_main_transcript_recent(tmp_path) -> None:
    ss, _ = _make_session()
    _seed_inflight(ss)
    main = tmp_path / "session.jsonl"
    main.write_text("{}")  # just written → recent mtime
    _point_transcript(ss, main)
    live = ss._watchdog_liveness(_time.time())
    assert live["active"] is True
    assert live["reason"] == "main_transcript_recent"


def test_watchdog_liveness_background_transcript_recent(tmp_path) -> None:
    ss, _ = _make_session()
    _seed_inflight(ss)
    main = tmp_path / "session.jsonl"
    main.write_text("{}")
    _age_file(main, 1000)  # main quiet → fall through to background evidence
    wf = tmp_path / "session" / "workflows"
    wf.mkdir(parents=True)
    (wf / "agent-1.jsonl").write_text("{}")  # recent subagent/workflow write
    _point_transcript(ss, main)
    live = ss._watchdog_liveness(_time.time())
    assert live["active"] is True
    assert live["reason"] == "background_transcript_recent"


def test_watchdog_liveness_quiet_inflight_is_inactive(tmp_path) -> None:
    ss, _ = _make_session()
    _seed_inflight(ss)
    main = tmp_path / "session.jsonl"
    main.write_text("{}")
    _age_file(main, 1000)  # main quiet, no fg tool, no background dir
    _point_transcript(ss, main)
    live = ss._watchdog_liveness(_time.time())
    assert live["active"] is False
    assert live["reason"] == "quiet"


def test_watchdog_liveness_stale_background_no_longer_active(tmp_path) -> None:
    # Murzik correctness point: a stale-but-present subagent dir must STOP
    # masking once its writes age out of the window.
    ss, _ = _make_session()
    _seed_inflight(ss)
    main = tmp_path / "session.jsonl"
    main.write_text("{}")
    _age_file(main, 1000)
    wf = tmp_path / "session" / "workflows"
    wf.mkdir(parents=True)
    stale = wf / "agent-1.jsonl"
    stale.write_text("{}")
    _age_file(stale, 1000)  # background write aged out of the 180s window
    _point_transcript(ss, main)
    live = ss._watchdog_liveness(_time.time())
    assert live["active"] is False
    assert live["reason"] == "quiet"


def test_watchdog_liveness_surfaced_in_stats(tmp_path) -> None:
    ss, _ = _make_session()
    _seed_inflight(ss)
    ss._inflight_tool_calls = {"tool-1": _time.time()}
    stats = ss.stats
    assert stats["inflight_active"] is True
    assert stats["inflight_liveness_reason"] == "foreground_tool_in_flight"
    assert stats["inflight_turns"] == 1


def test_stats_inflight_inactive_when_idle(tmp_path) -> None:
    ss, _ = _make_session()
    stats = ss.stats
    assert stats["inflight_active"] is False
    assert stats["inflight_liveness_reason"] == "no_inflight_turn"
