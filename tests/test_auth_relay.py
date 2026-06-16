"""Tests for the tmux OAuth login relay coordinator (#205)."""

from __future__ import annotations

import asyncio

import pytest

from pinky_daemon.auth_relay import (
    AuthRelayCoordinator,
    build_expiry_message,
    build_relay_message,
    extract_auth_code,
    extract_oauth_url,
    looks_like_login_wall,
)

# ── pure extractors ──────────────────────────────────────────────────────────


def test_extract_oauth_url_claude_ai():
    pane = "Browse to: https://claude.ai/oauth/authorize?code=true&client_id=abc\n> "
    assert (
        extract_oauth_url(pane)
        == "https://claude.ai/oauth/authorize?code=true&client_id=abc"
    )


def test_extract_oauth_url_console_anthropic():
    url = "https://console.anthropic.com/oauth/authorize?x=1"
    assert extract_oauth_url(f"visit {url} now") == url


def test_extract_oauth_url_dewraps_whitespace():
    # tmux may wrap a long URL across terminal columns.
    wrapped = "https://claude.ai/oauth/authorize?code=true&\n   client_id=verylongvalue123"
    assert (
        extract_oauth_url(wrapped)
        == "https://claude.ai/oauth/authorize?code=true&"  # stops at first whitespace run
    ) or "client_id=verylongvalue123" in extract_oauth_url(wrapped)


def test_extract_oauth_url_absent():
    assert extract_oauth_url("just a normal prompt with no url") is None
    assert extract_oauth_url("") is None
    assert extract_oauth_url("http://evil.example.com/oauth") is None  # wrong host


def test_extract_oauth_url_first_of_many():
    pane = "a https://claude.ai/oauth/one b https://claude.ai/oauth/two"
    assert extract_oauth_url(pane) == "https://claude.ai/oauth/one"


def test_looks_like_login_wall():
    assert looks_like_login_wall("open https://claude.ai/oauth/authorize?x=1")
    assert not looks_like_login_wall("a normal claude prompt")


def test_extract_auth_code_bare_token():
    code = "aBcD1234efGh5678ijKl#state9012"
    assert extract_auth_code(code) == code


def test_extract_auth_code_in_sentence():
    code = "abcdef0123456789ABCDEF"
    assert extract_auth_code(f"the code is {code} thanks") == code


def test_extract_auth_code_strips_trailing_punctuation():
    code = "abcdef0123456789ABCDEF"
    assert extract_auth_code(f"{code}.") == code


def test_extract_auth_code_rejects_prose():
    assert extract_auth_code("ok done") is None
    assert extract_auth_code("yes") is None
    assert extract_auth_code("") is None
    # short token below the 12-char floor
    assert extract_auth_code("short1") is None


def test_extract_auth_code_picks_longest():
    pane = "code shortish1234 longer56789012345678 ok"
    assert extract_auth_code(pane) == "longer56789012345678"


def test_relay_and_expiry_copy_have_no_emoji_and_name_agent():
    msg = build_relay_message("barsik", "https://claude.ai/oauth/x")
    assert "barsik" in msg and "https://claude.ai/oauth/x" in msg
    assert "Reply to THIS message" in msg
    assert build_expiry_message("barsik").startswith("The Claude sign-in link")


# ── coordinator ──────────────────────────────────────────────────────────────


def _make_coord(message_id: str = "m1", chat_id: str = "999"):
    sent: list[tuple] = []

    async def send_fn(agent, platform, chat_id_, content):
        sent.append((agent, platform, chat_id_, content))
        return {"message_id": message_id}

    coord = AuthRelayCoordinator()
    coord.configure(send_fn, lambda: {"chat_id": chat_id})
    return coord, sent


async def _wait_pending(coord, agent, *, want=True):
    for _ in range(200):
        if coord.has_pending(agent) is want:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"has_pending({agent}) never became {want}")


async def test_open_then_submit_happy_path():
    coord, sent = _make_coord()
    task = asyncio.create_task(coord.open("barsik", "https://claude.ai/oauth/x"))
    await _wait_pending(coord, "barsik", want=True)

    assert coord.pending_relay_mid("barsik") == "m1"
    assert coord.submit("barsik", "THECODE123456789") is True

    code = await task
    assert code == "THECODE123456789"
    assert not coord.has_pending("barsik")
    # exactly one outbound: the relay (no expiry).
    assert len(sent) == 1
    assert "claude.ai/oauth/x" in sent[0][3]


async def test_open_timeout_notifies_owner():
    coord, sent = _make_coord()
    with pytest.raises(asyncio.TimeoutError):
        await coord.open("barsik", "https://claude.ai/oauth/x", ttl_sec=0.05)
    assert not coord.has_pending("barsik")
    # relay + expiry notice.
    assert len(sent) == 2
    assert "expired" in sent[1][3]


async def test_submit_no_pending_returns_false():
    coord, _ = _make_coord()
    assert coord.submit("barsik", "THECODE123456789") is False


async def test_submit_wrong_agent_returns_false():
    coord, _ = _make_coord()
    task = asyncio.create_task(coord.open("barsik", "https://claude.ai/oauth/x"))
    await _wait_pending(coord, "barsik", want=True)
    assert coord.submit("someone-else", "THECODE123456789") is False
    # clean up the still-pending open
    assert coord.submit("barsik", "THECODE123456789") is True
    await task


async def test_open_unconfigured_raises():
    coord = AuthRelayCoordinator()
    with pytest.raises(RuntimeError):
        await coord.open("barsik", "https://claude.ai/oauth/x")


async def test_open_no_owner_chat_raises():
    coord = AuthRelayCoordinator()

    async def send_fn(*a):
        return {"message_id": "m1"}

    coord.configure(send_fn, lambda: {})  # no chat_id
    with pytest.raises(RuntimeError):
        await coord.open("barsik", "https://claude.ai/oauth/x")


async def test_second_open_supersedes_stale_pending():
    coord, _ = _make_coord()
    t1 = asyncio.create_task(coord.open("barsik", "https://claude.ai/oauth/one"))
    await _wait_pending(coord, "barsik", want=True)
    t2 = asyncio.create_task(coord.open("barsik", "https://claude.ai/oauth/two"))
    await asyncio.sleep(0)
    # the first future was cancelled when superseded
    with pytest.raises(asyncio.CancelledError):
        await t1
    assert coord.submit("barsik", "THECODE123456789") is True
    assert await t2 == "THECODE123456789"


# ── flag ─────────────────────────────────────────────────────────────────────


def test_enabled_env(monkeypatch):
    monkeypatch.delenv("PINKY_TMUX_AUTH_RELAY", raising=False)
    assert AuthRelayCoordinator.enabled() is False
    monkeypatch.setenv("PINKY_TMUX_AUTH_RELAY", "1")
    assert AuthRelayCoordinator.enabled() is True
    monkeypatch.setenv("PINKY_TMUX_AUTH_RELAY", "off")
    assert AuthRelayCoordinator.enabled() is False


def test_enabled_registry_overrides_env(monkeypatch):
    monkeypatch.setenv("PINKY_TMUX_AUTH_RELAY", "0")
    assert AuthRelayCoordinator.enabled(lambda k, d="": "1") is True
    # empty registry setting falls through to env
    assert AuthRelayCoordinator.enabled(lambda k, d="": "") is False
