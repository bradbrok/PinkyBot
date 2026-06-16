"""Broker intercept tests for the tmux OAuth login relay (#205).

Covers the security-critical path in ``MessageBroker._handle_auth_code_reply``:
owner-only acceptance and quote-reply correlation.
"""

from __future__ import annotations

import asyncio
import tempfile

from pinky_daemon import broker as broker_module
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.auth_relay import AuthRelayCoordinator
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.sessions import SessionManager

OWNER = "6770805286"
RELAY_MID = "relay-1"
CODE = "abcdef0123456789ABCDEF#state"


def _make_broker():
    tmp = tempfile.TemporaryDirectory()
    registry = AgentRegistry(db_path=f"{tmp.name}/agents.db")
    registry.register("barsik", model="sonnet", working_dir=tmp.name)
    registry.set_primary_user(OWNER, "Brad")
    sent: list[tuple] = []

    async def send_cb(agent, platform, chat_id, content):
        sent.append((agent, platform, chat_id, content))
        return {"message_id": RELAY_MID}

    brk = MessageBroker(registry, SessionManager(), send_callback=send_cb)
    return tmp, registry, brk, sent


async def _fresh_coord_with_pending(monkeypatch, registry, *, agent="barsik"):
    """Swap the broker's coordinator for a fresh one and open a pending relay."""
    coord = AuthRelayCoordinator()

    async def send_fn(a, p, c, content):
        return {"message_id": RELAY_MID}

    coord.configure(send_fn, registry.get_primary_user)
    monkeypatch.setattr(broker_module, "_auth_relay", coord)
    task = asyncio.create_task(coord.open(agent, "https://claude.ai/oauth/x"))
    for _ in range(200):
        if coord.has_pending(agent):
            break
        await asyncio.sleep(0)
    assert coord.has_pending(agent)
    return coord, task


def _msg(*, sender_id=OWNER, chat_id=OWNER, content=CODE, reply_to=RELAY_MID):
    return BrokerMessage(
        platform="telegram",
        chat_id=chat_id,
        sender_name="Brad",
        sender_id=sender_id,
        content=content,
        agent_name="barsik",
        message_id="incoming-1",
        reply_to=reply_to,
    )


async def test_owner_quote_reply_with_code_is_consumed(monkeypatch):
    tmp, registry, brk, sent = _make_broker()
    try:
        coord, task = await _fresh_coord_with_pending(monkeypatch, registry)
        handled = await brk._handle_auth_code_reply(_msg())
        assert handled is True
        assert await asyncio.wait_for(task, timeout=1) == CODE
        assert not coord.has_pending("barsik")
        assert any("Got it" in s[3] for s in sent)
    finally:
        tmp.cleanup()


async def test_plain_reply_without_quote_is_consumed(monkeypatch):
    tmp, registry, brk, sent = _make_broker()
    try:
        coord, task = await _fresh_coord_with_pending(monkeypatch, registry)
        handled = await brk._handle_auth_code_reply(_msg(reply_to=""))
        assert handled is True
        assert await asyncio.wait_for(task, timeout=1) == CODE
    finally:
        tmp.cleanup()


async def test_non_owner_is_not_consumed(monkeypatch):
    tmp, registry, brk, sent = _make_broker()
    try:
        coord, task = await _fresh_coord_with_pending(monkeypatch, registry)
        handled = await brk._handle_auth_code_reply(_msg(sender_id="intruder-9"))
        assert handled is False  # routes normally; code NOT injected
        assert coord.has_pending("barsik")
        # clean up the still-pending open
        coord.submit("barsik", CODE)
        await task
    finally:
        tmp.cleanup()


async def test_quote_reply_to_other_message_is_not_consumed(monkeypatch):
    tmp, registry, brk, sent = _make_broker()
    try:
        coord, task = await _fresh_coord_with_pending(monkeypatch, registry)
        handled = await brk._handle_auth_code_reply(_msg(reply_to="some-other-msg"))
        assert handled is False
        assert coord.has_pending("barsik")
        coord.submit("barsik", CODE)
        await task
    finally:
        tmp.cleanup()


async def test_no_pending_is_not_consumed(monkeypatch):
    tmp, registry, brk, sent = _make_broker()
    try:
        coord = AuthRelayCoordinator()
        monkeypatch.setattr(broker_module, "_auth_relay", coord)
        handled = await brk._handle_auth_code_reply(_msg())
        assert handled is False
    finally:
        tmp.cleanup()


async def test_owner_junk_reply_asks_again_and_does_not_submit(monkeypatch):
    tmp, registry, brk, sent = _make_broker()
    try:
        coord, task = await _fresh_coord_with_pending(monkeypatch, registry)
        handled = await brk._handle_auth_code_reply(_msg(content="ok thanks"))
        assert handled is True  # consumed (it's a reply to our relay)
        assert coord.has_pending("barsik")  # but NOT submitted
        assert any("didn't look like" in s[3] for s in sent)
        coord.submit("barsik", CODE)
        await task
    finally:
        tmp.cleanup()
