"""Regression coverage for the #863 grandfather approval migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import (
    _GRANDFATHER_DIGEST_PENDING,
    _GRANDFATHER_MARKER,
    _format_grandfather_digest,
    _resume_grandfather_migration,
    _run_grandfather_approved_users_migration,
)
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.sessions import SessionManager


@pytest.fixture
def migration_stores(tmp_path):
    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    conversations = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    registry.register("kuzya", working_dir=str(tmp_path / "kuzya"))
    yield registry, conversations
    conversations.close()
    registry.close()


def _tool_send(
    conversations: ConversationStore,
    chat_id: str,
    *,
    platform: str = "telegram",
    agent_name: str = "kuzya",
) -> None:
    conversations.append(
        f"{agent_name}-main",
        "assistant",
        "sent",
        platform=platform,
        chat_id=chat_id,
        metadata={"tool": "send"},
    )


def test_migration_unions_active_groups_and_tool_sends_only(migration_stores):
    registry, conversations = migration_stores
    registry.upsert_group_chat("kuzya", "GROUP_A", "A Team", platform="slack")
    registry.upsert_group_chat("kuzya", "INACTIVE", "Old Group")
    registry.deactivate_group_chat("kuzya", "INACTIVE")

    _tool_send(conversations, "DM_B")
    _tool_send(conversations, "PREAPPROVED")
    _tool_send(conversations, "WEB_CHAT", platform="web")
    _tool_send(conversations, "FERRY_CHAT", platform="ferry")
    _tool_send(conversations, "OTHER_AGENT", agent_name="barsik")
    conversations.append(
        "kuzya-main", "user", "inbound only",
        platform="telegram", chat_id="INBOUND_ONLY",
    )
    conversations.append(
        "kuzya-main", "assistant", "turn transcript",
        platform="telegram", chat_id="TRANSCRIPT_ONLY",
    )
    registry.approve_user("kuzya", "PREAPPROVED", "Original Name", "owner")

    seeded = _run_grandfather_approved_users_migration(conversations, registry)

    assert registry.get_setting(_GRANDFATHER_MARKER) == "1"
    assert {(chat["chat_id"], chat["display_name"]) for chat in seeded[0]["chats"]} == {
        ("GROUP_A", "A Team"),
        ("DM_B", ""),
    }
    assert registry.get_user_status("kuzya", "INACTIVE") is None
    assert registry.get_user_status("kuzya", "INBOUND_ONLY") is None
    assert registry.get_user_status("kuzya", "TRANSCRIPT_ONLY") is None
    assert registry.get_user_status("kuzya", "WEB_CHAT") is None
    assert registry.get_user_status("kuzya", "FERRY_CHAT") is None
    preapproved = next(
        user for user in registry.list_approved_users("kuzya")
        if user.chat_id == "PREAPPROVED"
    )
    assert preapproved.display_name == "Original Name"
    assert preapproved.approved_by == "owner"


def test_outbound_history_uses_exact_agent_main_session(migration_stores):
    registry, conversations = migration_stores
    agents_root = Path(registry.get("kuzya").working_dir).parent
    for agent_name in ("foo", "foo-bar", "a_b", "axb"):
        registry.register(agent_name, working_dir=str(agents_root / agent_name))
    _tool_send(conversations, "FROM_FOO_BAR", agent_name="foo-bar")
    _tool_send(conversations, "FROM_AXB", agent_name="axb")

    _run_grandfather_approved_users_migration(conversations, registry)

    assert registry.get_user_status("foo", "FROM_FOO_BAR") is None
    assert registry.get_user_status("a_b", "FROM_AXB") is None
    assert registry.get_user_status("foo-bar", "FROM_FOO_BAR") == "approved"
    assert registry.get_user_status("axb", "FROM_AXB") == "approved"


def test_denied_is_untouched_and_reruns_do_not_recreate_digest(migration_stores):
    registry, conversations = migration_stores
    _tool_send(conversations, "DENIED")
    _tool_send(conversations, "NEW_CHAT")
    registry.deny_user("kuzya", "DENIED")

    _run_grandfather_approved_users_migration(conversations, registry)
    assert registry.get_user_status("kuzya", "DENIED") == "denied"
    assert registry.get_setting(_GRANDFATHER_DIGEST_PENDING)

    registry.delete_setting(_GRANDFATHER_DIGEST_PENDING)
    assert _run_grandfather_approved_users_migration(conversations, registry) == []
    assert registry.get_setting(_GRANDFATHER_DIGEST_PENDING) == ""

    registry.delete_setting(_GRANDFATHER_MARKER)
    assert _run_grandfather_approved_users_migration(conversations, registry) == []
    assert registry.get_setting(_GRANDFATHER_DIGEST_PENDING) == ""


def test_no_seeded_rows_sets_marker_without_digest(migration_stores):
    registry, conversations = migration_stores

    assert _run_grandfather_approved_users_migration(conversations, registry) == []
    assert registry.get_setting(_GRANDFATHER_MARKER) == "1"
    assert registry.get_setting(_GRANDFATHER_DIGEST_PENDING) == ""


def test_unmarked_journal_resumes_after_row_was_already_committed(migration_stores):
    registry, conversations = migration_stores
    digest = {
        "version": 1,
        "seeded_at": 1.0,
        "agents": [{
            "agent_name": "kuzya",
            "count": 1,
            "chats": [{
                "chat_id": "PARTIAL",
                "display_name": "Partial Group",
                "pending_to_approved": False,
            }],
        }],
    }
    registry.set_setting(_GRANDFATHER_DIGEST_PENDING, json.dumps(digest))
    registry.approve_user(
        "kuzya", "PARTIAL", "Partial Group", "grandfather-migration",
    )

    seeded = _run_grandfather_approved_users_migration(conversations, registry)

    assert seeded[0]["chats"][0]["chat_id"] == "PARTIAL"
    assert registry.get_setting(_GRANDFATHER_MARKER) == "1"
    resumed = json.loads(registry.get_setting(_GRANDFATHER_DIGEST_PENDING))
    assert resumed["agents"] == seeded


@pytest.mark.asyncio
async def test_pending_chat_flushes_and_digest_retries_until_confirmed(migration_stores):
    registry, conversations = migration_stores
    chat_id = "C_TOD"
    _tool_send(conversations, chat_id, platform="slack")
    registry.add_pending_user("kuzya", chat_id, "TOD")
    registry.queue_pending_message_with_approval_request(
        agent_name="kuzya",
        platform="slack",
        chat_id=chat_id,
        reply_chat_id=chat_id,
        sender_id="U_TOD",
        sender_name="TOD",
        content="onesie?",
        is_group=True,
        target_name="TOD",
    )
    registry.set_token(
        "kuzya", "telegram", "token",
        settings={"account_id": "owner-bot"},
    )
    registry.set_owner_notification_destinations([{
        "platform": "telegram",
        "account_id": "owner-bot",
        "conversation_id": "OWNER_DM",
        "principal_id": "OWNER",
    }])
    _run_grandfather_approved_users_migration(conversations, registry)

    routed: list[BrokerMessage] = []
    attempts: list[str] = []

    async def route(_agent_name, message):
        routed.append(message)

    async def fail_send(*args, **kwargs):
        attempts.append(args[3])
        raise RuntimeError("offline")

    broker = MessageBroker(registry, SessionManager(), send_callback=fail_send)
    broker._route_streaming = route

    assert registry.get_user_status("kuzya", chat_id) == "approved"
    assert registry.get_approval_request("kuzya", chat_id)["gate_state"] == "approved"
    assert await _resume_grandfather_migration(registry, broker) is False
    assert registry.get_pending_messages("kuzya", chat_id) == []
    delivered = registry._db.execute(
        "SELECT delivered FROM pending_messages WHERE agent_name=? AND chat_id=?",
        ("kuzya", chat_id),
    ).fetchone()
    assert delivered[0] == 1
    assert [message.content for message in routed] == ["onesie?"]
    assert registry.get_setting(_GRANDFATHER_DIGEST_PENDING)

    async def confirm_send(*args, **kwargs):
        attempts.append(args[3])
        return {"sent": True}

    broker._send_callback = confirm_send
    assert await _resume_grandfather_migration(registry, broker) is True
    assert registry.get_setting(_GRANDFATHER_DIGEST_PENDING) == ""
    assert len(attempts) == 2
    assert "C_TOD" in attempts[-1]
    assert "pending -> approved" in attempts[-1]


@pytest.mark.asyncio
async def test_partial_flush_retries_only_the_undelivered_row(migration_stores):
    registry, _conversations = migration_stores
    chat_id = "C_PARTIAL"
    for content in ("first", "second"):
        registry.queue_pending_message(
            agent_name="kuzya",
            platform="slack",
            chat_id=chat_id,
            reply_chat_id=chat_id,
            sender_id="U_PARTIAL",
            sender_name="Partial",
            content=content,
            is_group=True,
        )

    seen: list[str] = []
    fail_second = True

    async def route(_agent_name, message):
        seen.append(message.content)
        if message.content == "second" and fail_second:
            raise RuntimeError("second row failed")

    broker = MessageBroker(registry, SessionManager())
    broker._route_streaming = route

    with pytest.raises(RuntimeError, match="second row failed"):
        await broker.handle_approval("kuzya", chat_id)
    rows = registry._db.execute(
        "SELECT content, delivered FROM pending_messages ORDER BY id",
    ).fetchall()
    assert rows == [("first", 1), ("second", 0)]

    fail_second = False
    assert await broker.handle_approval("kuzya", chat_id) == 1
    assert seen == ["first", "second", "second"]
    rows = registry._db.execute(
        "SELECT content, delivered FROM pending_messages ORDER BY id",
    ).fetchall()
    assert rows == [("first", 1), ("second", 1)]


@pytest.mark.asyncio
async def test_tod_shaped_active_group_routes_without_gate(migration_stores):
    registry, conversations = migration_stores
    registry.upsert_group_chat("kuzya", "C_ACTIVE", "TOD", platform="slack")
    _run_grandfather_approved_users_migration(conversations, registry)

    routed: list[BrokerMessage] = []

    async def route(_agent_name, message):
        routed.append(message)

    broker = MessageBroker(registry, SessionManager())
    broker._route_streaming = route
    await broker.handle_inbound(BrokerMessage(
        platform="slack",
        chat_id="C_ACTIVE",
        sender_id="U_TOD",
        sender_name="TOD",
        content="post-upgrade",
        agent_name="kuzya",
        is_group=True,
    ))

    assert [message.content for message in routed] == ["post-upgrade"]
    assert registry.get_pending_messages("kuzya", "C_ACTIVE") == []
    assert registry.get_approval_request("kuzya", "C_ACTIVE") is None


def test_digest_render_lists_exact_seeded_chats():
    digest = {
        "agents": [{
            "agent_name": "kuzya",
            "count": 2,
            "chats": [
                {"chat_id": "C1", "display_name": "Group One", "pending_to_approved": False},
                {"chat_id": "D2", "display_name": "", "pending_to_approved": True},
            ],
        }],
    }

    rendered = _format_grandfather_digest(json.loads(json.dumps(digest)))

    assert rendered.startswith("Grandfather approval migration completed.")
    assert "✅" not in rendered
    assert "•" not in rendered
    assert "kuzya: 2 chat(s)" in rendered
    assert "- Group One (C1)" in rendered
    assert "- D2 - pending -> approved" in rendered
    assert "Seeded 2 prior conversation(s)" in rendered
