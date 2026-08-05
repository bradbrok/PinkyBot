"""Unit tests for CodexTmuxSession (#215 PR2) — the codex-CLI variant of
TmuxSession.

CodexTmuxSession subclasses TmuxSession and overrides only the codex-specific
seams; these tests pin exactly those seams (command, env, session name,
transcript discovery, tailer class, trust pre-seed, NUX dismissal + readiness
gate, paste settle). The inherited state machine / worker / delivery are covered
by test_tmux_session.py and are NOT re-tested here.

The real codex-under-tmux round-trip lives in the gated integration smoke at the
bottom (opt-in, like the app-server soak) — it's the make-or-break proof and was
validated live 2026-06-17.
"""
import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.codex_tmux_session import (
    _CODEX_ENTER_DELAY_MS,
    CodexTmuxSession,
    _CodexTmuxControl,
)
from pinky_daemon.codex_tmux_transcript import CodexTmuxTranscriptTailer
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    TmuxSession,
    _InflightMeta,
    _QueuedTurn,
    _TmuxControl,
)
from pinky_daemon.tmux_transcript import TurnResponse
from pinky_daemon.transport_state import SessionState


def _ok(stdout: str = "") -> TmuxCommandResult:
    return TmuxCommandResult(returncode=0, stdout=stdout, stderr="")


def _mock_tmux() -> MagicMock:
    tmux = MagicMock(spec=_TmuxControl)
    tmux.session_name = "pinky-codex-test"
    tmux.tmux_binary = "tmux"
    tmux.socket_name = ""
    tmux.has_session = AsyncMock(return_value=False)
    tmux.new_session = AsyncMock(return_value=_ok())
    tmux.kill_session = AsyncMock(return_value=_ok())
    tmux.send_keys = AsyncMock(return_value=_ok())
    tmux.paste_text = AsyncMock(return_value=_ok())
    tmux.capture_pane = AsyncMock(return_value=_ok())
    return tmux


def _session(*, model="gpt-5.5", effort="medium", mcp=None, working_dir="/tmp/codex-tmux-test",
             provider_key="sk-test", tmux=None) -> CodexTmuxSession:
    cfg = StreamingSessionConfig(
        agent_name="murzik",
        working_dir=working_dir,
        model=model,
        thinking_effort=effort,
        provider_key=provider_key,
        mcp_servers=mcp or {},
    )
    return CodexTmuxSession(cfg, tmux_control=tmux or _mock_tmux())


# ── session name ────────────────────────────────────────────────────────────
def test_session_name_codex_namespaced():
    ss = _session()
    assert ss._build_session_name() == "pinky-codex-murzik"
    assert ss._session_name == "pinky-codex-murzik"  # distinct from claude's pinky-<agent>


# ── in-pane command ─────────────────────────────────────────────────────────
def test_build_cmd_fresh(monkeypatch):
    ss = _session(model="gpt-5.5")
    monkeypatch.setattr(ss, "_has_prior_transcript", lambda: False)
    cmd = ss._build_claude_cmd()
    assert cmd.startswith("codex ")
    assert "resume" not in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--no-alt-screen" in cmd
    assert "-m gpt-5.5" in cmd
    assert "-C " in cmd  # fresh launch carries the cwd
    # effort knob set via -c at launch → native ultracode keystroke block disabled
    assert ss._native_ultracode_pending is False
    assert ss._last_launch_used_continue is False


def test_build_cmd_resume_omits_cwd(monkeypatch):
    ss = _session()
    monkeypatch.setattr(ss, "_has_prior_transcript", lambda: True)
    cmd = ss._build_claude_cmd()
    assert "resume --last" in cmd
    assert "-C " not in cmd  # codex rejects -C on resume
    assert ss._last_launch_used_continue is True


def test_build_cmd_force_fresh_skips_resume(monkeypatch):
    ss = _session()
    monkeypatch.setattr(ss, "_has_prior_transcript", lambda: True)
    ss._config.force_fresh_context_once = True
    cmd = ss._build_claude_cmd()
    assert "resume" not in cmd
    assert "-C " in cmd
    assert ss._last_launch_forced_fresh is True


def test_build_cmd_effort_max_maps_to_high(monkeypatch):
    ss = _session(effort="max")
    monkeypatch.setattr(ss, "_has_prior_transcript", lambda: False)
    cmd = ss._build_claude_cmd()
    assert 'model_reasoning_effort="high"' in cmd  # codex has no "max"


def test_build_cmd_medium_effort_omitted(monkeypatch):
    ss = _session(effort="medium")
    monkeypatch.setattr(ss, "_has_prior_transcript", lambda: False)
    assert "model_reasoning_effort" not in ss._build_claude_cmd()


def test_build_cmd_injects_mcp(monkeypatch):
    ss = _session(mcp={"pinky": {"url": "http://h:8890/sse", "headers": {"X-Agent-Name": "murzik"}}})
    monkeypatch.setattr(ss, "_has_prior_transcript", lambda: False)
    cmd = ss._build_claude_cmd()
    assert 'mcp_servers.pinky.url="http://h:8890/sse"' in cmd
    assert 'mcp_servers.pinky.http_headers.X-Agent-Name="murzik"' in cmd


# ── env ─────────────────────────────────────────────────────────────────────
def test_build_repl_env_codex(monkeypatch):
    # #795 P1: full daemon-env PARITY (mirrors the subprocess + #792 app-server
    # codex transports), NOT a 4-key allowlist. tmux `new-session -e` is the only
    # env the pane receives, so everything codex can depend on must be carried.
    monkeypatch.setenv("CODEX_HOME", "/tmp/fleet-codex")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # Sentinels codex can depend on (auth / network / TLS / config dirs).
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://oai.internal/v1")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/corp.pem")
    # tmux-internal vars must be dropped (they'd corrupt a nested session).
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    # multiline values can't be passed via tmux -e and must be dropped.
    monkeypatch.setenv("BROKEN_MULTILINE", "line1\nline2")

    ss = _session(provider_key="sk-abc")
    env = ss._build_repl_env()

    # configured key + agent identity overlaid
    assert env["OPENAI_API_KEY"] == "sk-abc"
    assert env["PINKY_AGENT_NAME"] == "murzik"
    # daemon config carried through (parity)
    assert env["CODEX_HOME"] == "/tmp/fleet-codex"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HTTPS_PROXY"] == "http://proxy.local:3128"
    assert env["OPENAI_BASE_URL"] == "https://oai.internal/v1"
    assert env["XDG_CONFIG_HOME"] == "/tmp/xdg"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/corp.pem"
    # tmux-internal + multiline dropped
    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    assert "BROKEN_MULTILINE" not in env


def test_build_repl_env_falls_back_to_openai_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    ss = _session(provider_key="")
    assert ss._build_repl_env()["OPENAI_API_KEY"] == "sk-env"


# ── transcript discovery ────────────────────────────────────────────────────
def test_project_dir_is_codex_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
    ss = _session()
    assert ss._project_dir() == (tmp_path / "ch" / "sessions")


def test_discovery_delegates_to_codex_rollout(monkeypatch):
    import pinky_daemon.codex_tmux_session as cm
    sentinel = object()
    seen = {}

    def fake(wd):
        seen["wd"] = wd
        return sentinel

    monkeypatch.setattr(cm, "_discover_codex_rollout", fake)
    ss = _session(working_dir="/tmp/abc")
    assert ss._discover_transcript_path() is sentinel
    assert ss._has_prior_transcript() is True
    assert seen["wd"] == "/tmp/abc"


# ── codex trust pre-seed ────────────────────────────────────────────────────
def test_seed_codex_trust_writes_and_idempotent(monkeypatch, tmp_path):
    ch = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(ch))
    ss = _session()
    ss._seed_codex_trust("/tmp/proj")
    cfg = (ch / "config.toml").read_text()
    real = os.path.realpath("/tmp/proj")
    assert f'[projects."{real}"]' in cfg
    assert 'trust_level = "trusted"' in cfg
    # idempotent — a second call must not duplicate the block.
    ss._seed_codex_trust("/tmp/proj")
    assert (ch / "config.toml").read_text().count(f'[projects."{real}"]') == 1


# ── tailer class ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_tailer_uses_codex_tailer(monkeypatch):
    import pinky_daemon.codex_tmux_session as cm
    monkeypatch.setattr(cm, "_discover_codex_rollout", lambda wd: None)  # cold start
    ss = _session()
    await ss._start_tailer()
    try:
        assert isinstance(ss._tailer, CodexTmuxTranscriptTailer)
    finally:
        await ss._stop_tailer()


# ── paste settle (codex composer is slower) ─────────────────────────────────
def test_default_control_is_codex_aware():
    # No injected control → the session upgrades to the slower-paste control.
    cfg = StreamingSessionConfig(agent_name="murzik", working_dir="/tmp/x")
    ss = CodexTmuxSession(cfg)
    assert isinstance(ss._tmux, _CodexTmuxControl)


def test_injected_control_is_preserved():
    mock = _mock_tmux()
    ss = _session(tmux=mock)
    assert ss._tmux is mock  # tests' mock must not be swapped out


@pytest.mark.asyncio
async def test_codex_control_paste_defaults_to_4000ms():
    captured = {}

    async def fake_super_paste(self, text, *, enter=True, enter_delay_ms=300):
        captured["enter_delay_ms"] = enter_delay_ms
        return _ok()

    # Patch the base _TmuxControl.paste_text so we observe the default the
    # codex control forwards.
    import pinky_daemon.tmux_session as tm
    orig = tm._TmuxControl.paste_text
    tm._TmuxControl.paste_text = fake_super_paste
    try:
        ctrl = _CodexTmuxControl("pinky-codex-x")
        await ctrl.paste_text("hello")
        assert captured["enter_delay_ms"] == _CODEX_ENTER_DELAY_MS == 4000
    finally:
        tm._TmuxControl.paste_text = orig


# ── cold-start NUX dismissal + readiness gate ───────────────────────────────
@pytest.mark.asyncio
async def test_dismiss_nux_opens_gate_on_composer(monkeypatch):
    import pinky_daemon.codex_tmux_session as cm
    monkeypatch.setattr(cm, "_CODEX_NUX_POLL_SEC", 0.001)
    tmux = _mock_tmux()
    tmux.capture_pane = AsyncMock(return_value=_ok(
        "model: gpt-5.5 low   /model to change\ndirectory: /tmp/x\npermissions: YOLO mode"
    ))
    ss = _session(tmux=tmux)
    assert not ss._session_ready_event.is_set()
    await ss._codex_dismiss_nux_and_ready()
    assert ss._session_ready_event.is_set()
    tmux.send_keys.assert_not_called()  # composer already up → no NUX keys


@pytest.mark.asyncio
async def test_dismiss_nux_skips_update_prompt(monkeypatch):
    import pinky_daemon.codex_tmux_session as cm
    monkeypatch.setattr(cm, "_CODEX_NUX_POLL_SEC", 0.001)
    tmux = _mock_tmux()
    update_pane = _ok(
        "Update available! 0.125.0 -> 0.130.0\n1. Update now\n2. Skip\nPress enter to continue"
    )
    composer_pane = _ok("model: gpt-5.5   /model to change\ndirectory: /x")
    tmux.capture_pane = AsyncMock(side_effect=[update_pane, composer_pane])
    ss = _session(tmux=tmux)
    await ss._codex_dismiss_nux_and_ready()
    # Navigated off "Update now" to a Skip option, then confirmed.
    keys = [c.args[0] for c in tmux.send_keys.call_args_list]
    assert "Down" in keys and "Enter" in keys
    # Never picked "Update now".
    assert ss._session_ready_event.is_set()


@pytest.mark.asyncio
async def test_dismiss_nux_opens_gate_on_timeout(monkeypatch):
    import pinky_daemon.codex_tmux_session as cm
    monkeypatch.setattr(cm, "_CODEX_NUX_POLL_SEC", 0.001)
    monkeypatch.setattr(cm, "_CODEX_NUX_TIMEOUT_SEC", 0.02)
    tmux = _mock_tmux()
    tmux.capture_pane = AsyncMock(return_value=_ok("(unrecognized junk)"))
    ss = _session(tmux=tmux)
    await ss._codex_dismiss_nux_and_ready()
    # Gate is opened regardless so delivery never hangs (inherited path also
    # has its own gate-timeout fallback).
    assert ss._session_ready_event.is_set()


# ── claude-OAuth watcher is a no-op for codex ───────────────────────────────
@pytest.mark.asyncio
async def test_oauth_watcher_is_noop():
    ss = _session()
    # Must return immediately without touching the pane.
    await ss._watch_for_oauth_url()
    ss._tmux.capture_pane.assert_not_called()


# ── #1006 scheduler queued-route delivery ──────────────────────────────────
@pytest.mark.asyncio
async def test_scheduler_queued_route_reaches_codex_pane_with_stale_cc_status(
    capsys,
):
    """Codex has no Claude working/idle hooks, so their persisted status is
    not a delivery gate.  A scheduler wake must use Codex-local in-flight
    state, reach the pane, and keep the inherited exact-receipt contract."""
    tmux = _mock_tmux()
    ss = _session(tmux=tmux)
    ss._state_machine._state = SessionState.CONNECTED
    ss._config.live_status_fn = lambda: {
        "status": "working",
        "last_updated": time.time(),
    }

    receipt = await ss.send_scheduler_prompt("scheduled codex wake")
    for _ in range(100):
        if tmux.paste_text.await_count == 1:
            break
        await asyncio.sleep(0.01)

    tmux.paste_text.assert_awaited_once_with(
        "scheduled codex wake", enter=True
    )
    assert not receipt.done(), "pane keystrokes alone are not acceptance"
    assert "tmux[murzik]: queued message (chat=)" in capsys.readouterr().err

    # Codex's rollout user_message is the exact transport-acceptance edge.
    ss._on_transcript_entry({
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": "scheduled codex wake",
        },
    })
    assert await receipt is True


@pytest.mark.asyncio
async def test_codex_acceptance_reserves_meta_before_same_read_completion():
    """A user_message + task_complete pair can beat paste_text's return."""
    ss = _session()
    ss._state_machine._state = SessionState.CONNECTED
    receipt = asyncio.get_running_loop().create_future()
    turn = _QueuedTurn(
        prompt="fast scheduled wake",
        scheduler_delivery=receipt,
        scheduler_serialized=True,
        pane_delivery_started=True,
    )
    ss._scheduler_pending_turns.append(turn)

    ss._on_transcript_entry({
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": "fast scheduled wake",
        },
    })

    assert await receipt is True
    assert turn.pane_delivery_recorded is True
    assert len(ss._inflight_metas) == 1

    await ss._handle_turn_complete(
        TurnResponse(text="done", stop_reason="task_complete")
    )
    assert len(ss._inflight_metas) == 0


# ── StopFailure hook is a no-op for codex (#795 P2) ─────────────────────────
@pytest.mark.asyncio
async def test_handle_stop_failure_is_noop_for_codex():
    # Codex turn-end is owned by the rollout tailer (task_complete/turn_aborted),
    # NOT the .claude StopFailure hook. A misrouted StopFailure POST must NOT pop
    # a live codex turn (the inherited claude impl would synthesize + pop it).
    ss = _session()
    sentinel = object()
    ss._inflight_metas.append(sentinel)
    before = len(ss._inflight_metas)

    result = await ss.handle_stop_failure("api_error", message="boom", session_id="sid")

    assert result is False
    assert len(ss._inflight_metas) == before  # in-flight codex turn left intact
    assert ss._inflight_metas[-1] is sentinel


# ── container isolation guard: codex_cli + container blocked (#795 P1) ──────
def test_container_isolation_blocks_codex_cli():
    from pinky_daemon.api import _container_isolation_block_reason

    blocked = _container_isolation_block_reason(
        transport="tmux", runtime="codex_cli", agent_name="ctr"
    )
    assert blocked is not None
    status, detail = blocked
    assert status == 501  # not-implemented-yet (deferred to PR3)
    assert "codex_cli" in detail and "PR3" in detail


def test_container_isolation_allows_claude_tmux():
    from pinky_daemon.api import _container_isolation_block_reason

    # The existing supported combo must keep working.
    assert _container_isolation_block_reason(
        transport="tmux", runtime="claude_sdk", agent_name="ctr"
    ) is None


def test_container_isolation_requires_tmux_transport():
    from pinky_daemon.api import _container_isolation_block_reason

    blocked = _container_isolation_block_reason(
        transport="sdk", runtime="claude_sdk", agent_name="ctr"
    )
    assert blocked is not None
    status, detail = blocked
    assert status == 400
    assert "transport='tmux'" in detail


def test_container_isolation_transport_checked_before_runtime():
    # codex_cli + non-tmux + container hits the transport wall first (400), not
    # the codex 501 — both are blocks; this just pins the ordering.
    from pinky_daemon.api import _container_isolation_block_reason

    status, _ = _container_isolation_block_reason(
        transport="sdk", runtime="codex_cli", agent_name="ctr"
    )
    assert status == 400


# ── #860 — cost attribution + usage normalization ───────────────────────────
# The codex transport shares TmuxSession._log_turn_cost_and_analytics; its two
# seams are _ANALYTICS_PROVIDER (truthful provider on analytics rows) and
# _normalize_turn_usage (codex's inclusive input_tokens + cached_input_tokens
# → the daemon's disjoint convention, once, before any consumer).


def test_analytics_provider_is_codex_cli():
    """Codex turns must not claim "anthropic": the pricing lookup keys on
    (provider, model) and _provider_alias never crosses providers, so
    anthropic/gpt-* rows price at $0. "codex_cli" matches the SDK path's
    default and aliases onto the openai rate rows."""
    assert CodexTmuxSession._ANALYTICS_PROVIDER == "codex_cli"
    assert TmuxSession._ANALYTICS_PROVIDER == "anthropic"


def test_normalize_converts_codex_schema_to_disjoint():
    """Codex reports input_tokens INCLUSIVE of the cached prefix; the daemon
    convention is disjoint (input = uncached remainder, cached span under
    cache_read_input_tokens). The ambiguous source key is consumed."""
    u = {"input_tokens": 100_000, "cached_input_tokens": 90_000,
         "output_tokens": 500}
    out = CodexTmuxSession._normalize_turn_usage(u)
    assert out["input_tokens"] == 10_000
    assert out["cache_read_input_tokens"] == 90_000
    assert "cached_input_tokens" not in out
    assert out["output_tokens"] == 500
    # The source dict is copied, never mutated in place.
    assert u["input_tokens"] == 100_000
    assert u["cached_input_tokens"] == 90_000


def test_normalize_without_cached_key_passes_through():
    """No cached_input_tokens ⇒ nothing to convert; the dict passes through
    untouched (fail toward the visible undercount, never a double-bill)."""
    u = {"input_tokens": 5_000, "output_tokens": 100}
    assert CodexTmuxSession._normalize_turn_usage(u) is u


def test_normalize_malformed_counts_pass_through():
    u = {"input_tokens": 5_000, "cached_input_tokens": "garbage"}
    assert CodexTmuxSession._normalize_turn_usage(u) is u


def test_normalize_clamps_cached_larger_than_inclusive():
    """cached > inclusive shouldn't happen, but must clamp to 0 rather than
    log a negative input count."""
    u = {"input_tokens": 1_000, "cached_input_tokens": 2_000}
    out = CodexTmuxSession._normalize_turn_usage(u)
    assert out["input_tokens"] == 0
    assert out["cache_read_input_tokens"] == 2_000


def _seed_inflight(ss: CodexTmuxSession) -> None:
    """Minimal _InflightMeta seed so _handle_turn_complete has a head entry
    (mirrors tests/test_tmux_session.py's helper)."""
    ss._inflight_metas.append(
        _InflightMeta(
            meta={},
            completion_event=None,
            internal=False,
            dispatched_at=time.time(),
            turn=_QueuedTurn(prompt=""),
        )
    )
    ss._head_started_at = time.time()


@pytest.mark.asyncio
async def test_turn_complete_prices_codex_turn_correctly():
    """#860 end-to-end: a codex-schema turn (usage exactly as the tailer
    snapshots token_count.info.last_token_usage) must log provider=codex_cli,
    the DISJOINT token split, and the openai-rate dollar figure. Pre-#860 this
    row logged as anthropic/gpt-* at $0 — and had a rate row existed, the
    inclusive input count would have over-billed the cached span ~10x on
    review-heavy sessions."""
    analytics = MagicMock()
    cost_cb = MagicMock()
    cfg = StreamingSessionConfig(
        agent_name="murzik",
        working_dir="/tmp/codex-tmux-test",
        model="gpt-5.6-sol",
        provider_key="sk-test",
    )
    ss = CodexTmuxSession(
        cfg,
        tmux_control=_mock_tmux(),
        analytics_store=analytics,
        cost_callback=cost_cb,
    )
    ss._skip_wake_prompt_for_tests = True
    ss._state_machine._state = SessionState.CONNECTED
    _seed_inflight(ss)

    await ss._handle_turn_complete(
        TurnResponse(
            text="ok",
            stop_reason="end_turn",
            model="gpt-5.6-sol",
            usage={
                "input_tokens": 100_000,       # inclusive of cached (codex)
                "cached_input_tokens": 90_000,
                "output_tokens": 1_000,
            },
            duration_ms=100,
            assistant_entry_count=1,
            tool_uses=[],
        )
    )

    analytics.log_turn_usage.assert_called_once()
    kwargs = analytics.log_turn_usage.call_args.kwargs
    assert kwargs["provider"] == "codex_cli"
    assert kwargs["model"] == "gpt-5.6-sol"
    assert kwargs["input_tokens"] == 10_000        # uncached remainder
    assert kwargs["cached_input_tokens"] == 90_000  # cache-READ column
    assert kwargs["output_tokens"] == 1_000

    # 10k uncached @ $5 + 1k out @ $30 + 90k cached @ $0.50 (per Mtok).
    expected = 10_000 / 1e6 * 5 + 1_000 / 1e6 * 30 + 90_000 / 1e6 * 0.5
    assert cost_cb.call_args.args[1] == pytest.approx(expected)
    assert ss.usage.total_cost_usd == pytest.approx(expected)
    # Accumulation saw the disjoint split too (context gauge correctness).
    assert ss.usage.input_tokens == 10_000
    assert ss.usage.cache_read_tokens == 90_000


# ── gated integration smoke (the make-or-break; opt-in) ─────────────────────
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("PINKY_CODEX_TMUX_SMOKE") != "1",
    reason="real codex+tmux round-trip; opt-in via PINKY_CODEX_TMUX_SMOKE=1",
)
async def test_integration_codex_tmux_roundtrip(tmp_path):
    """Drive a real codex TUI under real tmux: cold-start (NUX dismissal +
    readiness), paste a trivial prompt, assert the rollout is written and the
    codex tailer reads task_complete → TurnResponse. Validated manually
    2026-06-17 (replied "PONG", tailer parsed text/usage/duration)."""
    import shutil

    if not shutil.which("codex") or not shutil.which("tmux"):
        pytest.skip("codex/tmux not installed")

    cwd = tmp_path / "smoke"
    cwd.mkdir()
    captured = []

    cfg = StreamingSessionConfig(
        agent_name="codex-smoke",
        working_dir=str(cwd),
        model=os.environ.get("PINKY_CODEX_SMOKE_MODEL", "gpt-5.5"),
        thinking_effort="low",
    )
    ss = CodexTmuxSession(cfg, response_callback=lambda r, **_: captured.append(r))
    try:
        await ss._spawn_tmux_repl()  # cold-start: trust seed + NUX dismissal + ready
        assert ss._session_ready_event.is_set()
        await ss._tmux.paste_text("Reply with exactly one word: PONG", enter=True)
        # Poll the tailer for the turn (codex writes the rollout on first turn).
        for _ in range(60):
            if ss._tailer and ss._tailer.transcript_path.exists():
                await ss._tailer.read_once()
            if captured:
                break
            await asyncio.sleep(1.0)
        assert captured, "no turn captured from the codex rollout"
        assert "PONG" in captured[-1].text.upper()
    finally:
        await ss.disconnect()
