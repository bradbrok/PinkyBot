"""Every outbound route and callback applies the current thread policy."""

import logging

import pytest

from tests.isolated_policy_support import closure
from tests.slack_mention_only_support import (
    AGENT,
    CHANNEL,
    CHILD,
    OTHER,
    ROOT,
    USER,
    configure,
    message,
    post,
    tag,
)
from tests.slack_mention_only_support import gateway as gateway

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("reply", ["", ROOT])
def test_text_refusal_never_selects_adapter(gateway, caplog, reply):
    g = gateway
    configure(g)
    caplog.set_level(logging.WARNING)
    response = post(g, "/broker/send", chat_id=CHANNEL, content="payload", reply_to=reply)
    assert response.status_code == 403, response.text
    assert "post only in a thread where you were tagged" in response.json()["detail"]
    g.select.assert_not_called()
    g.adapter.send_message.assert_not_called()
    assert sum("mention-only refuse" in r.getMessage() for r in caplog.records) == 1
    assert "payload" not in caplog.text


@pytest.mark.parametrize("target", ["fixture-conversation", "#fixture-conversation"])
def test_name_target_cannot_bypass_marked_ids(gateway, target):
    configure(gateway)
    response = post(gateway, "/broker/send", chat_id=target, content="payload")
    assert response.status_code == 403, response.text
    assert "use the channel id" in response.json()["detail"]
    gateway.select.assert_not_called()


@pytest.mark.parametrize("target", [OTHER, "D" + "0" * 9, USER,
                                    USER + ",U" + "0" * 8 + "6"])
def test_unmarked_id_targets_control(gateway, target):
    configure(gateway)
    response = post(gateway, "/broker/send", chat_id=target, content="payload")
    assert response.status_code == 200, response.text
    gateway.adapter.send_message.assert_called_once()


def test_name_target_without_config_control(gateway):
    response = post(gateway, "/broker/send", chat_id="fixture-conversation", content="payload")
    assert response.status_code == 200, response.text
    gateway.adapter.send_message.assert_called_once()


def test_tagged_thread_send_then_policy_change(gateway):
    g = gateway
    configure(g)
    tag(g)
    g.broker.remember_message_context(message(message_id=CHILD, reply_to=ROOT))
    response = post(g, "/broker/thread", message_id=CHILD, content="payload")
    assert response.status_code == 200, response.text
    assert g.adapter.send_message.call_args.kwargs["thread_ts"] == ROOT
    g.store._db.execute("DELETE FROM slack_tagged_threads")
    g.store._db.commit()
    # Identical payload must recheck policy before an old dedupe success is served.
    response = post(g, "/broker/thread", message_id=CHILD, content="payload")
    assert response.status_code == 403, response.text
    assert g.adapter.send_message.call_count == 1


@pytest.mark.parametrize("kind", ["photo", "document", "video", "animation"])
@pytest.mark.parametrize("threaded", [False, True])
def test_file_refusal_uses_resolved_target(gateway, kind, threaded):
    g = gateway
    configure(g)
    body = {"chat_id": CHANNEL, "file_path": str(g.file)}
    if threaded:
        g.broker.remember_message_context(message(message_id=CHILD, reply_to=ROOT))
        body["message_id"] = CHILD
    response = post(g, f"/broker/send-{kind}", **body)
    assert response.status_code == 403, response.text
    g.select.assert_not_called()
    g.adapter.upload_file.assert_not_called()


@pytest.mark.parametrize("kind", ["photo", "document", "video", "animation"])
def test_file_tagged_thread_control(gateway, kind):
    g = gateway
    configure(g)
    tag(g)
    g.broker.remember_message_context(message(message_id=CHILD, reply_to=ROOT))
    response = post(g, f"/broker/send-{kind}", message_id=CHILD, file_path=str(g.file))
    assert response.status_code == 200, response.text
    assert g.adapter.upload_file.call_args.kwargs["thread_ts"] == ROOT


@pytest.mark.parametrize("context", [False, True])
def test_reaction_unknown_thread_refuses_before_adapter(gateway, context):
    g = gateway
    configure(g)
    if context:
        g.broker.remember_message_context(message(message_id=CHILD, reply_to=ROOT))
    response = post(g, "/broker/react", chat_id=CHANNEL, message_id=CHILD, emoji="eyes")
    assert response.status_code == 403, response.text
    g.select.assert_not_called()
    g.adapter.add_reaction.assert_not_called()


@pytest.mark.parametrize("context", [False, True])
def test_reaction_tagged_root_or_known_child_control(gateway, context):
    g = gateway
    configure(g)
    tag(g)
    target = ROOT
    if context:
        g.broker.remember_message_context(message(message_id=CHILD, reply_to=ROOT))
        target = CHILD
    response = post(g, "/broker/react", chat_id=CHANNEL, message_id=target, emoji="eyes")
    assert response.status_code == 200, response.text
    g.adapter.add_reaction.assert_called_once_with(CHANNEL, target, "eyes")


@pytest.mark.asyncio
async def test_internal_send_returns_failed_result(gateway):
    g = gateway
    configure(g)
    result = await g.broker.send_callback(AGENT, "slack", CHANNEL, "payload")
    assert result.get("sent") is False and result.get("error")
    g.select.assert_not_called()


@pytest.mark.asyncio
async def test_internal_reaction_returns_failed_result(gateway):
    g = gateway
    configure(g)
    result = await closure(g.app, "_broker_react")(AGENT, "slack", CHANNEL, ROOT, "eyes")
    assert isinstance(result, dict) and result.get("reacted") is False and result.get("error")
    g.select.assert_not_called()


def test_broadcast_counts_refusal_as_failure(gateway):
    g = gateway
    configure(g)
    g.agents.upsert_group_chat(AGENT, CHANNEL, platform="slack")
    response = post(g, "/broker/broadcast", content="payload")
    assert response.status_code == 200, response.text
    result = response.json()
    blocked = [entry for entry in result["deliveries"] if entry.get("chat_id") == CHANNEL]
    assert len(blocked) == 1 and blocked[0].get("sent") is False
    assert any("mention-only" in error for error in result["errors"])
    assert all(call.args[0] != CHANNEL for call in g.adapter.send_message.call_args_list)


@pytest.mark.parametrize("kind", ["send", "react", "file"])
def test_r3_expired_outbound_without_sweep(gateway, monkeypatch, kind):
    from tests.slack_mention_only_r3_support import LIFETIME, Clock, policy
    from tests.slack_mention_only_support import tagged

    g = gateway
    clock = Clock(monkeypatch)
    policy(g)
    # Context retention is configurable; keep the pre-advance file context
    # usable beyond the independent, fixed 30-day tagged-thread lifetime.
    g.store.retention_days = 31
    g.store.mark_tagged(AGENT, CHANNEL, ROOT, ROOT)
    if kind == "file":
        g.broker.remember_message_context(message(message_id=CHILD, reply_to=ROOT))
    before = tuple(tagged(g))
    clock.at(LIFETIME + 1)
    assert tuple(tagged(g)) == before
    if kind == "send":
        response = post(g, "/broker/send", chat_id=CHANNEL, reply_to=ROOT, content="stale")
    elif kind == "react":
        response = post(g, "/broker/react", chat_id=CHANNEL, message_id=ROOT, emoji="eyes")
    else:
        response = post(g, "/broker/send-document", message_id=CHILD, file_path=str(g.file))
    assert response.status_code == 403, response.text
    g.select.assert_not_called()
    for fn in (g.adapter.send_message, g.adapter.add_reaction, g.adapter.upload_file):
        fn.assert_not_called()
    assert tuple(tagged(g)) == before, "refusal must work without deleting the stale row"
