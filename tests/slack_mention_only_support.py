"""Synthetic, offline fixtures usable on both sides of the policy change."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import pollers
from pinky_daemon.api import create_api
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from pinky_daemon.broker import BrokerMessage
from tests.isolated_policy_support import closure, replace_cell

AGENT = "fixture-agent"
PEER = "fixture-peer"
CHANNEL = "C" + "0" * 8 + "1"
OTHER = "G" + "0" * 8 + "2"
USER = "U" + "0" * 8 + "3"
BOT = "U" + "0" * 8 + "4"
ROOT = "1700000000.000001"
CHILD = "1700000001.000002"
SECRET = "fixture-mention-only-session-secret"


def required(owner, name):
    method = getattr(owner, name, None)
    assert callable(method), "missing policy contract: " + name
    return method


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    # Keep policy activation and the existing synthetic Slack ids on one epoch.
    monkeypatch.setattr(time, "time", lambda: float(ROOT) - 1)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_SESSION_SECRET", SECRET)
    monkeypatch.setenv("PINKY_TOOL_POLICY", "off")
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "off")
    app = create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    agents = app.state.agents
    for name in (AGENT, PEER):
        agents.register(name, working_dir=str(tmp_path / name), isolated=False)
    agents.set_default_timezone("UTC")
    agents.set_primary_user(USER)
    adapter = Mock()
    adapter.send_message.return_value = SimpleNamespace(message_id=CHILD, chat_id=CHANNEL)
    adapter.upload_file.return_value = SimpleNamespace(message_id=CHILD, chat_id=CHANNEL)
    select = Mock(return_value=adapter)
    replace_cell(monkeypatch, closure(app, "_send_file_message"), "_get_platform_adapter", select)
    file = tmp_path / "attachment.txt"
    file.write_text("synthetic attachment")
    client = TestClient(app, raise_server_exceptions=False)
    yield SimpleNamespace(app=app, client=client, agents=agents, broker=app.state.broker,
                          store=app.state.message_context_store, adapter=adapter,
                          select=select, file=file)
    client.close()
    app.state.store_catalog.close()


def owner():
    return {"cookie": f"{SESSION_COOKIE_NAME}={create_session_cookie(SECRET)}"}


def signed(g, method, path, agent=AGENT):
    return build_internal_auth_headers(g.agents.get_signing_key(agent), agent_name=agent,
                                       method=method, path=path)


def configure(g, channels=(CHANNEL,), agent=AGENT):
    """Seed only configuration on the base; never emulate enforcement."""
    setter = getattr(g.agents, "set_slack_mention_only", None)
    if callable(setter):
        setter(agent, list(channels), "ui:fixture")
        return
    g.agents._db.execute(
        "CREATE TABLE IF NOT EXISTS agent_slack_mention_only "
        "(agent_name TEXT, channel_id TEXT, created_at REAL, created_by TEXT, "
        "PRIMARY KEY(agent_name, channel_id))"
    )
    g.agents._db.execute("DELETE FROM agent_slack_mention_only WHERE agent_name=?", (agent,))
    g.agents._db.executemany("INSERT INTO agent_slack_mention_only VALUES (?,?,?,?)",
                            [(agent, channel, time.time(), "ui:fixture") for channel in channels])
    g.agents._db.commit()


def tagged_schema(g):
    g.store._db.execute(
        "CREATE TABLE IF NOT EXISTS slack_tagged_threads "
        "(agent_name TEXT, chat_id TEXT, thread_ts TEXT, first_tag_ts TEXT, tagged_at REAL, "
        "PRIMARY KEY(agent_name, chat_id, thread_ts))"
    )
    g.store._db.commit()


def tag(g, root=ROOT, channel=CHANNEL, agent=AGENT, at=None):
    tagged_schema(g)
    g.store._db.execute("INSERT OR IGNORE INTO slack_tagged_threads VALUES (?,?,?,?,?)",
                        (agent, channel, root, root, time.time() if at is None else at))
    g.store._db.commit()


def tagged(g, root=ROOT, channel=CHANNEL, agent=AGENT):
    tagged_schema(g)
    return g.store._db.execute(
        "SELECT first_tag_ts, tagged_at FROM slack_tagged_threads "
        "WHERE agent_name=? AND chat_id=? AND thread_ts=?", (agent, channel, root),
    ).fetchone()


def message(**changes):
    values = dict(platform="slack", chat_id=CHANNEL, sender_name="Private sender",
                  sender_id=USER, content="Private message", agent_name=AGENT,
                  message_id=ROOT, is_group=True, metadata={})
    values.update(changes)
    return BrokerMessage(**values)


def route_capture(g, monkeypatch, approved=True):
    if approved:
        for name in (AGENT, PEER):
            for channel in (CHANNEL, OTHER, USER, "D" + "0" * 9):
                g.agents.approve_user(name, channel, approved_by="fixture")

    async def route(agent, msg):
        g.broker.remember_message_context(msg)
        return True

    capture = AsyncMock(side_effect=route)
    monkeypatch.setattr(g.broker, "_route_streaming", capture)
    monkeypatch.setattr(g.broker, "_notify_approval_request", AsyncMock())
    return capture


def poller(g):
    result = pollers.BrokerSlackPoller(g.adapter, AGENT, g.broker, g.agents,
                                      app_token="fixture", event_callback=AsyncMock())
    result._bot_user_id = BOT
    result._bot_id = "B" + "0" * 8 + "5"
    result._resolve_user_name = AsyncMock(return_value="Resolved sender")
    result._resolve_channel_title = AsyncMock(return_value="Resolved conversation")
    result._resolve_text_refs = AsyncMock(return_value="Resolved text with no raw mention")
    return result


async def drain():
    while pollers._DELIVERY_TASKS:
        await asyncio.gather(*tuple(pollers._DELIVERY_TASKS))


def event(text, **changes):
    body = dict(type="message", channel=CHANNEL, user=USER, ts=ROOT,
                text=text, channel_type="channel")
    body.update(changes)
    return {"event": body}


def post(g, path, **body):
    return g.client.post(path, headers=signed(g, "POST", path),
                         json={"agent_name": AGENT, "platform": "slack", **body})
