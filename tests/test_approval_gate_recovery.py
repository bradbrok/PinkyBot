from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import create_api
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.sessions import SessionManager


def _registry(tmp_path) -> AgentRegistry:
    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    registry.register("onesie", model="sonnet", working_dir=str(tmp_path))
    return registry


def _broker(registry: AgentRegistry, sent: list[str]) -> MessageBroker:
    async def send_callback(
        agent_name: str,
        platform: str,
        chat_id: str,
        content: str,
        *,
        account_id: str = "",
    ) -> None:
        sent.append(content)

    return MessageBroker(
        registry,
        SessionManager(),
        send_callback=send_callback,
    )


def _owner_notifications(registry: AgentRegistry) -> None:
    registry.set_primary_user("owner", display_name="Brad")
    registry.set_owner_notification_destinations([
        {
            "platform": "telegram",
            "account_id": "owner-bot",
            "conversation_id": "owner",
            "principal_id": "owner",
        }
    ])


@pytest.mark.asyncio
async def test_aging_reprompts_at_24h_and_72h_then_stops(tmp_path) -> None:
    registry = _registry(tmp_path)
    sent: list[str] = []
    broker = _broker(registry, sent)
    _owner_notifications(registry)

    registry.add_pending_user("onesie", "C_TRAVEL", "Travel group")
    _, request = registry.queue_pending_message_with_approval_request(
        agent_name="onesie",
        platform="telegram",
        chat_id="C_TRAVEL",
        reply_chat_id="C_TRAVEL",
        sender_name="Guest",
        sender_id="guest",
        content="Are you here?",
        is_group=True,
        target_name="Travel group",
    )
    await broker._notify_approval_request(request)
    assert len(sent) == 1

    now = time.time()
    registry._db.execute(
        "UPDATE approval_requests SET oldest_held_at=? WHERE agent_name=? AND chat_id=?",
        (now - (25 * 60 * 60), "onesie", "C_TRAVEL"),
    )
    registry._db.commit()
    assert await broker.retry_due_approval_notifications() == 1
    assert len(sent) == 2
    assert registry.get_approval_request("onesie", "C_TRAVEL")[
        "aging_reprompt_count"
    ] == 1

    registry._db.execute(
        "UPDATE approval_requests SET oldest_held_at=? WHERE agent_name=? AND chat_id=?",
        (now - (73 * 60 * 60), "onesie", "C_TRAVEL"),
    )
    registry._db.commit()
    assert await broker.retry_due_approval_notifications() == 1
    assert len(sent) == 3
    assert registry.get_approval_request("onesie", "C_TRAVEL")[
        "aging_reprompt_count"
    ] == 2

    assert await broker.retry_due_approval_notifications() == 0
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_aging_does_not_reprompt_without_undelivered_messages(tmp_path) -> None:
    registry = _registry(tmp_path)
    sent: list[str] = []
    broker = _broker(registry, sent)
    _owner_notifications(registry)
    registry.add_pending_user("onesie", "C_EMPTY", "Empty group")
    _, request = registry.queue_pending_message_with_approval_request(
        agent_name="onesie",
        platform="telegram",
        chat_id="C_EMPTY",
        sender_name="Guest",
        sender_id="guest",
        content="already handled",
        is_group=True,
        target_name="Empty group",
    )
    await broker._notify_approval_request(request)
    registry.mark_pending_delivered("onesie", "C_EMPTY")
    registry._db.execute(
        "UPDATE approval_requests SET oldest_held_at=? WHERE agent_name=? AND chat_id=?",
        (time.time() - (96 * 60 * 60), "onesie", "C_EMPTY"),
    )
    registry._db.commit()

    assert await broker.retry_due_approval_notifications() == 0
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_pending_channel_pages_loudly_for_approved_principal(tmp_path) -> None:
    registry = _registry(tmp_path)
    sent: list[str] = []
    broker = _broker(registry, sent)
    _owner_notifications(registry)
    registry.approve_user("onesie", "dmitri", "Dmitri", "owner")

    await broker.handle_inbound(BrokerMessage(
        platform="telegram",
        chat_id="C_TRAVEL",
        sender_name="Guest",
        sender_id="guest",
        content="first held message",
        agent_name="onesie",
        is_group=True,
    ))
    assert len(sent) == 1
    assert not sent[0].startswith("🚨")

    await broker.handle_inbound(BrokerMessage(
        platform="telegram",
        chat_id="C_TRAVEL",
        sender_name="Dmitri",
        sender_id="dmitri",
        content="Are you here?",
        agent_name="onesie",
        is_group=True,
    ))

    assert len(sent) == 2
    assert sent[1].startswith("🚨 APPROVAL GATE BLACK-HOLE RISK")
    assert "already-approved onesie principal (dmitri)" in sent[1]
    request = registry.get_approval_request("onesie", "C_TRAVEL")
    assert request["high_signal"] is True
    assert request["high_signal_alerted_at"] > 0


@pytest.mark.asyncio
async def test_approved_transition_settles_and_reconcile_flushes(tmp_path, monkeypatch) -> None:
    registry = _registry(tmp_path)
    broker = _broker(registry, [])
    routed: list[BrokerMessage] = []

    async def route(agent_name: str, message: BrokerMessage) -> None:
        routed.append(message)

    monkeypatch.setattr(broker, "_route_streaming", route)
    registry.add_pending_user("onesie", "C_TRAVEL", "Travel group")
    registry.queue_pending_message_with_approval_request(
        agent_name="onesie",
        platform="telegram",
        chat_id="C_TRAVEL",
        reply_chat_id="C_TRAVEL",
        sender_name="Dmitri",
        sender_id="dmitri",
        content="standing instruction",
        is_group=True,
        target_name="Travel group",
    )

    registry.approve_user("onesie", "C_TRAVEL", "Travel group", "migration")

    assert registry.get_approval_request("onesie", "C_TRAVEL")["gate_state"] == "approved"
    assert await broker.reconcile_approved_pending_messages() == 1
    assert [message.content for message in routed] == ["standing instruction"]
    assert registry.get_pending_messages("onesie", "C_TRAVEL") == []


@pytest.mark.asyncio
async def test_startup_reconcile_heals_direct_migration_approval(tmp_path, monkeypatch) -> None:
    registry = _registry(tmp_path)
    broker = _broker(registry, [])
    routed: list[BrokerMessage] = []

    async def route(agent_name: str, message: BrokerMessage) -> None:
        routed.append(message)

    monkeypatch.setattr(broker, "_route_streaming", route)
    registry.add_pending_user("onesie", "C_LEGACY", "Legacy group")
    registry.queue_pending_message_with_approval_request(
        agent_name="onesie",
        platform="telegram",
        chat_id="C_LEGACY",
        reply_chat_id="C_LEGACY",
        sender_name="Dmitri",
        sender_id="dmitri",
        content="legacy held row",
        is_group=True,
        target_name="Legacy group",
    )
    registry._db.execute(
        "UPDATE approved_users SET status='approved' WHERE agent_name=? AND chat_id=?",
        ("onesie", "C_LEGACY"),
    )
    registry._db.commit()

    assert registry.get_approval_request("onesie", "C_LEGACY")["gate_state"] == "pending"
    assert await broker.reconcile_approved_pending_messages() == 1
    assert registry.get_approval_request("onesie", "C_LEGACY")["gate_state"] == "approved"
    assert [message.content for message in routed] == ["legacy held row"]


@pytest.mark.asyncio
async def test_reconcile_keeps_row_undelivered_when_session_handoff_fails(
    tmp_path, monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    broker = _broker(registry, [])

    async def unavailable(agent_name: str, message: BrokerMessage) -> bool:
        return False

    monkeypatch.setattr(broker, "_route_streaming", unavailable)
    registry.add_pending_user("onesie", "C_OFFLINE", "Offline group")
    registry.queue_pending_message_with_approval_request(
        agent_name="onesie",
        platform="telegram",
        chat_id="C_OFFLINE",
        reply_chat_id="C_OFFLINE",
        sender_name="Dmitri",
        sender_id="dmitri",
        content="do not drop me",
        is_group=True,
        target_name="Offline group",
    )
    registry.approve_user("onesie", "C_OFFLINE", "Offline group", "migration")

    assert await broker.reconcile_approved_pending_messages() == 0
    assert [
        message["content"]
        for message in registry.get_pending_messages("onesie", "C_OFFLINE")
    ] == ["do not drop me"]


def test_backlog_diagnostic_is_fleet_and_agent_visible(tmp_path) -> None:
    app = create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=str(tmp_path / "pinky.db"),
    )
    registry = app.state.agents
    registry.register("onesie", model="sonnet", working_dir=str(tmp_path))
    registry.set_primary_user("owner", display_name="Brad")
    registry.approve_user("onesie", "dmitri", "Dmitri", "owner")
    registry.add_pending_user("onesie", "C_TRAVEL", "Travel group")
    registry.queue_pending_message_with_approval_request(
        agent_name="onesie",
        platform="telegram",
        chat_id="C_TRAVEL",
        reply_chat_id="C_TRAVEL",
        sender_name="Dmitri",
        sender_id="dmitri",
        content="Are you here?",
        is_group=True,
        target_name="Travel group",
    )
    client = TestClient(app)

    fleet = client.get("/system/approval-backlog")
    health = client.get("/agents/onesie/health")

    assert fleet.status_code == 200
    assert fleet.json()["pending_chats"] == 1
    assert fleet.json()["high_signal_chats"] == 1
    assert fleet.json()["undelivered_messages"] == 1
    row = fleet.json()["backlogs"][0]
    assert (row["agent_name"], row["chat_id"], row["undelivered_count"]) == (
        "onesie", "C_TRAVEL", 1,
    )
    check = health.json()["checks"]["approval_backlog"]
    assert check["high_signal_chats"] == 1
    assert health.json()["recommendation"] == "needs_attention"
