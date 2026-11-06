"""Controlled-clock schedules for expiry, activation, and original-method admission."""

import time
from unittest.mock import AsyncMock

from pinky_daemon.broker import MessageBroker
from tests.slack_mention_only_support import (
    AGENT,
    BOT,
    CHANNEL,
    ROOT,
    SECRET,
    USER,
    drain,
    event,
    message,
    poller,
)

EPOCH = 1700000000.0
LIFETIME = 30 * 86400


class Clock:
    def __init__(self, monkeypatch):
        self.now = EPOCH
        monkeypatch.setattr(time, "time", lambda: self.now)

    def at(self, offset):
        self.now = EPOCH + offset
        return self.ts(offset)

    @staticmethod
    def ts(offset):
        return f"{EPOCH + offset:.6f}"


def policy(g, channels=(CHANNEL,), actor="actor-a"):
    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

    cookie = create_session_cookie(SECRET, user=actor)
    response = g.client.put(
        f"/agents/{AGENT}/slack/mention-only",
        headers={"cookie": f"{SESSION_COOKIE_NAME}={cookie}"},
        json={"channels": list(channels)},
    )
    assert response.status_code == 200, response.text


def policy_rows(g):
    return {row[0]: tuple(row[1:]) for row in g.agents._db.execute(
        "SELECT channel_id, created_at, created_by FROM agent_slack_mention_only WHERE agent_name=?",
        (AGENT,),
    ).fetchall()}


def queue(g, tag_id, *, root=ROOT, timestamp=None):
    g.agents.queue_pending_message(
        AGENT, "slack", CHANNEL, "Fixture", "Held", reply_chat_id=CHANNEL,
        is_group=True, sender_id=USER,
        slack_envelope={"message_id": tag_id, "reply_to": root,
                        "timestamp": time.time() if timestamp is None else timestamp,
                        "metadata": {"thread_ts": root, "mentions_self": True}},
    )


def intercepts(g, monkeypatch):
    from pinky_daemon import broker

    spies = []
    for name in ("_handle_stop_command", "_handle_approval_command",
                 "_handle_voice_approval_command", "_handle_auth_code_reply"):
        fn = AsyncMock(return_value=False)
        monkeypatch.setattr(g.broker, name, fn)
        spies.append(fn)
    monkeypatch.setattr(broker._auth_relay, "enabled", lambda _: True)
    return spies


async def ingress(g, kind, tag_id, *, root=ROOT):
    if kind == "poller":
        p = poller(g)
        await p._handle_event(event(f"<@{BOT}>", ts=tag_id, thread_ts=root))
        await drain()
        return
    if kind == "replay":
        queue(g, tag_id, root=root)
        return await g.broker._handle_approval_unlocked(AGENT, CHANNEL)
    msg = message(message_id=tag_id, reply_to=root, timestamp=time.time(),
                  metadata={"mentions_self": True, "thread_ts": root})
    if kind == "original":
        await MessageBroker.handle_inbound(g.broker, msg)
    else:
        assert kind == "wrapper"
        await g.broker.handle_inbound(msg)
