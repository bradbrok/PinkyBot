"""TmuxSession watcher tests for the OAuth login relay (#205)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pinky_daemon import tmux_session
from pinky_daemon.auth_relay import AuthRelayCoordinator
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _TmuxControl
from pinky_daemon.transport_state import SessionState

WALL = "Browse to: https://claude.ai/oauth/authorize?code=true&client_id=abc\n> "
CURRENT_CAI_WALL = (
    "Browse to: https://claude.com/cai/oauth/authorize?"
    "code=true&client_id=abc&state=xyz\n> "
)


def _ok(stdout: str = "") -> TmuxCommandResult:
    return TmuxCommandResult(returncode=0, stdout=stdout, stderr="")


def _make_mock_tmux() -> MagicMock:
    tmux = MagicMock(spec=_TmuxControl)
    tmux.session_name = "pinky-test"
    tmux.paste_text = AsyncMock(return_value=_ok())
    tmux.capture_pane = AsyncMock(return_value=_ok())
    return tmux


def _make_session(*, state: SessionState, agent_name: str = "dymok"):
    cfg = StreamingSessionConfig(agent_name=agent_name, working_dir="/tmp/auth-relay-test")
    tmux = _make_mock_tmux()
    ss = TmuxSession(cfg, tmux_control=tmux)
    ss._skip_wake_prompt_for_tests = True
    ss._state_machine._state = state
    return ss, tmux


def _coord_with_owner():
    coord = AuthRelayCoordinator()
    sent: list[str] = []

    async def send_fn(agent, platform, chat_id, content):
        sent.append(content)
        return {"message_id": "relay-1"}

    coord.configure(send_fn, lambda: {"chat_id": "999"})
    return coord, sent


async def _wait(cond, *, tries: int = 300):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0)
    return False


async def test_relay_injects_code_and_confirms(monkeypatch):
    monkeypatch.setattr(tmux_session, "_AUTH_LOGIN_SETTLE_SEC", 0.0)
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    coord, sent = _coord_with_owner()
    monkeypatch.setattr(tmux_session, "_auth_relay", coord)
    # Post-paste pane capture: wall cleared (empty) → "signed in" path.
    tmux.capture_pane = AsyncMock(return_value=_ok())

    task = asyncio.create_task(
        ss._relay_login_and_inject("https://claude.ai/oauth/authorize?x=1")
    )
    assert await _wait(lambda: coord.has_pending("dymok"))
    coord.submit("dymok", "THECODE123456789")
    await asyncio.wait_for(task, timeout=1)

    tmux.paste_text.assert_awaited_once()
    args, kwargs = tmux.paste_text.call_args
    assert args[0] == "THECODE123456789"  # the code is injected verbatim
    assert kwargs.get("enter") is True
    # relay message + "signed in" confirmation were sent.
    assert any("oauth" in c for c in sent)
    assert any("signed in" in c for c in sent)


async def test_relay_warns_when_wall_persists(monkeypatch):
    monkeypatch.setattr(tmux_session, "_AUTH_LOGIN_SETTLE_SEC", 0.0)
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    coord, sent = _coord_with_owner()
    monkeypatch.setattr(tmux_session, "_auth_relay", coord)
    # Post-paste pane STILL shows the wall → code rejected → restart advice.
    tmux.capture_pane = AsyncMock(return_value=_ok(WALL))

    task = asyncio.create_task(ss._relay_login_and_inject("https://claude.ai/oauth/x"))
    assert await _wait(lambda: coord.has_pending("dymok"))
    coord.submit("dymok", "THECODE123456789")
    await asyncio.wait_for(task, timeout=1)

    assert any("did not complete" in c for c in sent)
    assert not any("signed in" in c for c in sent)


async def test_watch_detects_wall_and_relays(monkeypatch):
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_ok(WALL))
    recorder = AsyncMock()
    monkeypatch.setattr(ss, "_relay_login_and_inject", recorder)

    await ss._watch_for_oauth_url()

    recorder.assert_awaited_once()
    assert "claude.ai/oauth" in recorder.call_args[0][0]


async def test_flag_on_current_cai_wall_never_opens_submits_or_pastes(monkeypatch):
    """#916 Phase 1 URLs cannot enter the legacy reply/paste relay."""
    monkeypatch.setenv("PINKY_TMUX_AUTH_RELAY", "1")
    monkeypatch.setattr(tmux_session, "_AUTH_WALL_DETECT_WINDOW_SEC", 0.03)
    monkeypatch.setattr(tmux_session, "_AUTH_WALL_POLL_SEC", 0.01)
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_ok(CURRENT_CAI_WALL))
    coord = MagicMock(spec=AuthRelayCoordinator)
    coord.open = AsyncMock()
    monkeypatch.setattr(tmux_session, "_auth_relay", coord)

    await ss._watch_for_oauth_url()

    coord.open.assert_not_awaited()
    coord.submit.assert_not_called()
    tmux.paste_text.assert_not_awaited()


async def test_watch_no_wall_exits_without_relay(monkeypatch):
    monkeypatch.setattr(tmux_session, "_AUTH_WALL_DETECT_WINDOW_SEC", 0.05)
    monkeypatch.setattr(tmux_session, "_AUTH_WALL_POLL_SEC", 0.01)
    ss, tmux = _make_session(state=SessionState.CONNECTED)
    tmux.capture_pane = AsyncMock(return_value=_ok("a normal claude prompt\n> "))
    recorder = AsyncMock()
    monkeypatch.setattr(ss, "_relay_login_and_inject", recorder)

    await ss._watch_for_oauth_url()

    recorder.assert_not_awaited()


async def test_watch_exits_when_not_connected(monkeypatch):
    ss, tmux = _make_session(state=SessionState.DEAD)
    tmux.capture_pane = AsyncMock(return_value=_ok(WALL))
    recorder = AsyncMock()
    monkeypatch.setattr(ss, "_relay_login_and_inject", recorder)

    await ss._watch_for_oauth_url()

    recorder.assert_not_awaited()
    tmux.capture_pane.assert_not_awaited()
