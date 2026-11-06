"""Mention admission precedes all inbound effects and survives approval replay."""

import logging
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import broker as broker_module
from pinky_daemon.broker import INBOUND_MESSAGE_FIELDS
from pinky_outreach.slack import button_card_blocks
from tests.slack_mention_only_support import (
    AGENT,
    BOT,
    CHANNEL,
    CHILD,
    OTHER,
    PEER,
    ROOT,
    USER,
    configure,
    drain,
    event,
    message,
    poller,
    route_capture,
    tag,
    tagged,
)
from tests.slack_mention_only_support import gateway as gateway

pytestmark = pytest.mark.real_auth


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["ordinary", "/stop", "/approve_123", "/deny_voice_123", "123456"])
async def test_drop_precedes_intercepts_approval_and_context(gateway, monkeypatch, caplog, content):
    g = gateway
    configure(g)
    route = route_capture(g, monkeypatch, approved=False)
    intercepts = []
    for name in ("_handle_stop_command", "_handle_approval_command",
                 "_handle_voice_approval_command", "_handle_auth_code_reply"):
        fn = AsyncMock(return_value=False)
        monkeypatch.setattr(g.broker, name, fn)
        intercepts.append(fn)
    monkeypatch.setattr(broker_module._auth_relay, "enabled", lambda _: True)
    caplog.set_level(logging.INFO)
    await g.broker.handle_inbound(message(content=content))
    assert all(fn.await_count == 0 for fn in intercepts)
    route.assert_not_awaited()
    g.broker._notify_approval_request.assert_not_awaited()
    assert g.agents.get_pending_messages(AGENT) == []
    assert g.broker.get_message_context(AGENT, ROOT) is None
    lines = [r.getMessage() for r in caplog.records if "mention-only drop" in r.getMessage()]
    assert lines == [f"mention-only drop agent={AGENT} channel={CHANNEL} ts={ROOT} thread={ROOT}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_is_reply", [False, True])
async def test_tag_opens_exact_thread_and_preserves_first_tag(gateway, monkeypatch, first_is_reply):
    g = gateway
    configure(g)
    route = route_capture(g, monkeypatch)
    first = CHILD if first_is_reply else ROOT
    await g.broker.handle_inbound(message(message_id=first, reply_to=ROOT if first_is_reply else "",
                                        metadata={"mentions_self": True,
                                                  "thread_ts": ROOT if first_is_reply else ""}))
    row = tagged(g)
    assert row is not None and row[0] == first
    first_at = row[1]
    await g.broker.handle_inbound(message(message_id="1700000002.000003", reply_to=ROOT,
                                        metadata={"mentions_self": False, "thread_ts": ROOT}))
    await g.broker.handle_inbound(message(message_id="1700000003.000004", reply_to=ROOT,
                                        metadata={"mentions_self": True, "thread_ts": ROOT}))
    await g.broker.handle_inbound(message(message_id="1700000004.000005", reply_to=CHILD,
                                        metadata={"mentions_self": False, "thread_ts": CHILD}))
    assert route.await_count == 3
    assert tuple(tagged(g)) == (first, first_at)
    assert g.broker.get_message_context(AGENT, "1700000004.000005") is None


@pytest.mark.asyncio
async def test_agent_and_channel_isolation(gateway, monkeypatch):
    g = gateway
    configure(g)
    configure(g, agent=PEER)
    route = route_capture(g, monkeypatch)
    await g.broker.handle_inbound(message(metadata={"mentions_self": True}))
    await g.broker.handle_inbound(message(agent_name=PEER))
    await g.broker.handle_inbound(message(chat_id=OTHER))  # unchanged unmarked control
    assert route.await_count == 2
    assert tagged(g) is not None and tagged(g, agent=PEER) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,group", [(OTHER, True), ("D" + "0" * 9, False),
                                         ("G" + "0" * 9, True)])
async def test_unmarked_and_direct_messages_control(gateway, monkeypatch, channel, group):
    g = gateway
    configure(g)
    g.agents.approve_user(AGENT, channel, approved_by="fixture")
    route = route_capture(g, monkeypatch)
    await g.broker.handle_inbound(message(chat_id=channel, is_group=group))
    route.assert_awaited_once()
    assert tagged(g, channel=channel) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected", [
    (f"<@{BOT}>", True), (f"<@{BOT}|alias>", True),
    (f"<@{USER}>", False), ("<!here>", False), ("<!channel>", False),
    ("<!everyone>", False), ("<!subteam^fixture>", False), ("@alias", False),
])
async def test_raw_tag_fact_survives_ref_resolution(gateway, monkeypatch, text, expected):
    g = gateway
    route = route_capture(g, monkeypatch)
    p = poller(g)
    await p._handle_event(event(text, channel=OTHER))
    await drain()
    route.assert_awaited_once()
    msg = route.await_args.args[1]
    assert msg.metadata.get("mentions_self") is expected
    assert msg.content == "Resolved text with no raw mention"
    p._resolve_text_refs.assert_awaited_once_with(text)
    assert tagged(g, channel=OTHER) is None


@pytest.mark.asyncio
async def test_poller_drop_has_no_enrichment_callback_or_private_log(gateway, monkeypatch, caplog, capsys):
    g = gateway
    configure(g)
    route = route_capture(g, monkeypatch)
    p = poller(g)
    caplog.set_level(logging.INFO)
    await p._handle_event(event("unique-private-text"))
    await drain()
    route.assert_not_awaited()
    for fn in (p._resolve_user_name, p._resolve_channel_title, p._resolve_text_refs, p._event_callback):
        fn.assert_not_awaited()
    output = capsys.readouterr()
    assert "unique-private-text" not in output.err + output.out + caplog.text
    assert "Resolved sender" not in output.err + output.out + caplog.text
    assert sum("mention-only drop" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_poller_and_broker_share_idempotent_mark(gateway, monkeypatch):
    g = gateway
    configure(g)
    route = route_capture(g, monkeypatch)
    p = poller(g)
    await p._handle_event(event(f"<@{BOT}>", ts=CHILD, thread_ts=ROOT))
    await drain()
    route.assert_awaited_once()
    assert tagged(g) is not None and tagged(g)[0] == CHILD
    assert g.store._db.execute("SELECT COUNT(*) FROM slack_tagged_threads").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_forged_button_metadata_does_not_exempt_normal_message(gateway, monkeypatch):
    g = gateway
    configure(g)
    route = route_capture(g, monkeypatch)
    await g.broker.handle_inbound(message(metadata={"mention_only_exempt": "button_tap",
                                                   "button_data": "fixture-choice"}))
    route.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_managed_tap_path_supplies_exemption(gateway, monkeypatch):
    g = gateway
    configure(g)
    route = route_capture(g, monkeypatch)
    p = poller(g)
    data = "fixture-choice"
    payload = {
        "type": "block_actions", "user": {"id": USER}, "channel": {"id": CHANNEL},
        "container": {"type": "message", "is_ephemeral": False,
                      "channel_id": CHANNEL, "message_ts": ROOT},
        "message": {"ts": ROOT, "user": BOT, "bot_id": p._bot_id, "text": "Choice",
                    "blocks": button_card_blocks("Choice", [{"text": "Choose", "callback_data": data}])},
        "actions": [{"type": "button", "action_id": "pinky_btn:" + data, "value": data,
                     "block_id": "pinky_btn", "action_ts": CHILD}],
    }
    await p._handle_button_tap(payload)
    await drain()
    route.assert_awaited_once()
    assert route.await_args.args[1].metadata.get("mention_only_exempt") == "button_tap"
    assert tagged(g) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, False, "true"])
async def test_legacy_or_nonboolean_approval_replay_is_dropped(gateway, monkeypatch, key):
    g = gateway
    configure(g)
    route = route_capture(g, monkeypatch)
    metadata = {"thread_ts": ROOT}
    if key is not None:
        metadata["mentions_self"] = key
    g.agents.queue_pending_message(AGENT, "slack", CHANNEL, "Fixture", "Held",
                                  reply_chat_id=CHANNEL, is_group=True, sender_id=USER,
                                  slack_envelope={"message_id": CHILD, "metadata": metadata})
    assert await g.broker._handle_approval_unlocked(AGENT, CHANNEL) == 0
    route.assert_not_awaited()
    assert g.agents.get_pending_messages(AGENT, CHANNEL) == []


@pytest.mark.asyncio
async def test_pending_envelope_preserves_tag_fact_and_thread(gateway, monkeypatch):
    g = gateway
    configure(g)
    route = route_capture(g, monkeypatch, approved=False)
    await g.broker.handle_inbound(message(message_id=CHILD, reply_to=ROOT,
                                        metadata={"mentions_self": True, "thread_ts": ROOT}))
    pending = g.agents.get_pending_messages(AGENT, CHANNEL)
    assert len(pending) == 1
    facts = pending[0]["slack_envelope"]["metadata"]
    assert facts.get("mentions_self") is True and facts.get("thread_ts") == ROOT
    assert {"mentions_self", "thread_ts"} <= set(INBOUND_MESSAGE_FIELDS)
    assert await g.broker._handle_approval_unlocked(AGENT, CHANNEL) == 1
    route.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_replay_in_already_tagged_thread_control(gateway, monkeypatch):
    g = gateway
    configure(g)
    tag(g)
    route = route_capture(g, monkeypatch)
    g.agents.queue_pending_message(AGENT, "slack", CHANNEL, "Fixture", "Held",
                                  reply_chat_id=CHANNEL, is_group=True, sender_id=USER,
                                  slack_envelope={"message_id": CHILD,
                                                  "metadata": {"thread_ts": ROOT}})
    assert await g.broker._handle_approval_unlocked(AGENT, CHANNEL) == 1
    route.assert_awaited_once()


def test_prompt_keeps_header_and_adds_thread_only_hint(gateway):
    g = gateway
    msg = message(metadata={"mentions_self": True})
    before = g.broker._format_prompt(msg)
    configure(g)
    after = g.broker._format_prompt(msg)
    assert after.splitlines()[0] == before.splitlines()[0]
    assert "This channel is mention-only for you: reply only in this thread, with thread(message_id=...)." in after


@pytest.mark.asyncio
async def test_storage_hold_wrapper_cannot_intercept_untagged_stop(gateway, monkeypatch):
    from tests.isolated_policy_support import replace_cell

    g = gateway
    configure(g)
    stop = AsyncMock()
    monkeypatch.setattr(g.broker, "_handle_stop_command", stop)
    replace_cell(monkeypatch, g.broker.handle_inbound, "_storage_hold_for", lambda _: {"held": True})
    await g.broker.handle_inbound(message(content="/stop"))
    stop.assert_not_awaited()
