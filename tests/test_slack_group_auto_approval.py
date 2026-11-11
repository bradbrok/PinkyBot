"""Slack group approval at the real broker, registry and adapter boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest

from pinky_daemon import agent_registry, api
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from pinky_daemon.broker import BrokerMessage
from pinky_outreach.slack import SlackAdapter

pytestmark = pytest.mark.real_auth

SELF = "fixture-tenant"
PEER = "fixture-peer"
GROUP = "G000000001"
SECOND_GROUP = "G000000002"
THIRD_GROUP = "G000000003"
OWNER = "U000000001"
MEMBER = "U000000002"
OTHER = "U000000003"
FORMER = "U000000004"
EXTRA_BOT = "U000000005"
OWN_BOT = "U000000006"
PEER_BOT = "U000000007"
SELF_TOKEN = "xoxb-fixture-self"
PEER_TOKEN = "xoxb-fixture-peer"
ROTATED_TOKEN = "xoxb-fixture-rotated"
AUTO_BY = "slack-group-auto"
DECISION = "slack group approval:"
KEPT = "slack group replay kept:"


@dataclass
class SlackWire:
    calls: list = field(default_factory=list)
    clients: list = field(default_factory=list)
    info: dict = field(default_factory=dict)
    members: dict = field(default_factory=dict)

    def request(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        token = request.headers["Authorization"].removeprefix("Bearer ")
        args = parse_qs(request.content.decode())
        chat = args.get("channel", [""])[0]
        self.calls.append((method, token, chat, args, threading.get_ident()))
        own_id = {SELF_TOKEN: OWN_BOT, PEER_TOKEN: PEER_BOT, ROTATED_TOKEN: PEER_BOT}[token]
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        if method == "auth.test":
            result = {"ok": True, "user_id": own_id}
        elif method == "conversations.info":
            result = self.info.get(
                (token, chat), {"ok": True, "channel": {"id": chat, "is_mpim": True}}
            )
        elif method == "conversations.members":
            result = self.members.get((token, chat), {"ok": True, "members": [own_id, MEMBER]})
        else:
            raise AssertionError(f"Unexpected Slack fixture method: {method}")
        if callable(result):
            result = result()
        if isinstance(result, Exception):
            raise result
        return httpx.Response(200, json=result)

    def read_count(self, method, *, token=None, chat=None):
        return sum(
            got == method and (token is None or token == used) and (chat is None or chat == target)
            for got, used, target, _, _ in self.calls
        )

    def use_members(self, ids, *, token=SELF_TOKEN, chat=GROUP, cursor=""):
        self.members[token, chat] = {
            "ok": True,
            "members": ids,
            "response_metadata": {"next_cursor": cursor},
        }


@dataclass
class Case:
    app: object
    root: object
    wire: SlackWire
    send: AsyncMock
    route: AsyncMock

    @property
    def agents(self):
        return self.app.state.agents

    @property
    def broker(self):
        return self.app.state.broker

    def grant(self, member=MEMBER, label="Fixture Participant", *, agent=SELF):
        return self.agents.approve_user(agent, member, label, "manual")

    def message(
        self,
        text="fixture message",
        *,
        member=MEMBER,
        agent=SELF,
        chat=GROUP,
        platform="slack",
        is_group=True,
        channel_type="mpim",
        ts="1700000000.000001",
    ):
        return BrokerMessage(
            platform=platform,
            chat_id=chat,
            sender_id=member,
            sender_name="Fixture Display",
            content=text,
            agent_name=agent,
            is_group=is_group,
            message_id=ts,
            reply_to="1700000000.000000",
            metadata={"channel_type": channel_type, "thread_ts": "1700000000.000000"},
            timestamp=time.time(),
        )

    def seed_auto(self, chat=GROUP, *, agent=SELF, members=(OWN_BOT, MEMBER)):
        # Supply only the stored state for replay/hot-path RED on the old schema.
        # The migration test starts with the unmodified legacy table instead.
        db = self.agents._db
        names = {row[1] for row in db.execute("PRAGMA table_info(approved_users)")}
        if "slack_auto_member_ids" not in names:
            db.execute("ALTER TABLE approved_users ADD COLUMN slack_auto_member_ids TEXT")
        self.agents.approve_user(agent, chat, "Fixture Group", AUTO_BY)
        db.execute(
            "UPDATE approved_users SET slack_auto_member_ids=? WHERE agent_name=? AND chat_id=?",
            (json.dumps(members), agent, chat),
        )
        db.commit()

    def held(self, *, agent=SELF, chat=GROUP):
        return self.agents.get_pending_messages(agent, chat)

    def seed_hold(self, content, member=MEMBER, *, chat=GROUP):
        return self.agents.queue_pending_message_with_approval_request(
            agent_name=SELF,
            platform="slack",
            chat_id=chat,
            sender_name="Fixture Display",
            sender_id=member,
            content=content,
            is_group=True,
        )[0]

    def approvals(self, *, agent=SELF):
        return {row.chat_id: row for row in self.agents.list_approved_users(agent)}

    def delivered(self):
        return [call.args[1].content for call in self.route.await_args_list]

    def owner_texts(self):
        return [call.args[3] for call in self.send.await_args_list]


@pytest.fixture
async def case(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_SLACK_SOCKET_MODE", "0")
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "enforce")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    wire = SlackWire()
    original = SlackAdapter.__init__

    def init_adapter(adapter, token, **kwargs):
        original(adapter, token, **kwargs)
        adapter._client.close()
        adapter._client = httpx.Client(
            base_url=adapter.BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            transport=httpx.MockTransport(wire.request),
            trust_env=False,
        )
        wire.clients.append(adapter)

    monkeypatch.setattr(SlackAdapter, "__init__", init_adapter)
    app = api.create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    send = AsyncMock(return_value={"sent": True})
    route = AsyncMock(return_value=True)
    fixture = Case(app, tmp_path, wire, send, route)
    fixture.broker._send_callback = send
    fixture.broker._route_streaming = route
    for name, token in ((SELF, SELF_TOKEN), (PEER, PEER_TOKEN)):
        work = tmp_path / name
        work.mkdir()
        fixture.agents.register(name, isolated=True, working_dir=str(work))
        fixture.agents.set_token(name, "slack", token)
        fixture.agents.set_token(
            name, "telegram", "fixture-notify-token", settings={"account_id": "fixture-account"}
        )
    fixture.agents.set_primary_user(OWNER, "Fixture Owner")
    fixture.agents.set_owner_notification_destinations(
        [
            {
                "platform": "telegram",
                "account_id": "fixture-account",
                "conversation_id": OWNER,
                "principal_id": OWNER,
            }
        ]
    )
    try:
        yield fixture
    finally:
        tasks = tuple(fixture.broker._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for adapter in wire.clients:
            adapter.close()
        fixture.app.state.store_catalog.close()


async def until(predicate, *, seconds=3.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.001)
    return predicate()


async def drain_background(case):
    tasks = tuple(case.broker._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def assert_pending(case, *, chat=GROUP, agent=SELF, sender=MEMBER, content="fixture message"):
    assert case.agents.get_user_status(agent, chat) == "pending"
    assert case.delivered() == []
    held = case.held(chat=chat, agent=agent)
    assert [(row["sender_id"], row["reply_chat_id"], row["content"]) for row in held] == [
        (sender, chat, content),
    ]
    request = case.agents.get_approval_request(agent, chat)
    assert request["gate_state"] == "pending"
    assert request["held_count"] == 1
    assert request["notification_state"] == "delivered"
    assert len(case.owner_texts()) == 1
    assert f"/approve_{chat}" in case.owner_texts()[0]
    assert f"/deny_{chat}" in case.owner_texts()[0]


async def test_group_approval_reuses_shared_replay_in_order(case, monkeypatch, capsys):
    case.grant()
    case.grant(OTHER, "Second Fixture Participant")
    case.wire.use_members([OWN_BOT, MEMBER, OTHER])
    case.agents.add_pending_user(SELF, GROUP, GROUP)
    case.seed_hold("first held", MEMBER)
    case.seed_hold("second held", OTHER)
    shared = AsyncMock(wraps=case.broker.handle_approval)
    monkeypatch.setattr(case.broker, "handle_approval", shared)
    message = case.message("trigger", channel_type="channel")
    await case.broker.handle_inbound(message)
    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    shared.assert_awaited_once_with(SELF, GROUP)
    assert case.delivered() == ["first held", "second held", "trigger"]
    routed = case.route.await_args_list[-1].args[1]
    assert (routed.message_id, routed.reply_to, routed.metadata["thread_ts"]) == (
        message.message_id,
        message.reply_to,
        message.metadata["thread_ts"],
    )
    assert case.held() == []
    assert case.agents.get_approval_request(SELF, GROUP)["gate_state"] == "approved"
    record = case.approvals()[GROUP]
    assert record.approved_by == AUTO_BY
    assert "Fixture Participant" in record.display_name
    assert "Second Fixture Participant" in record.display_name
    stored = case.agents._db.execute(
        "SELECT slack_auto_member_ids FROM approved_users WHERE agent_name=? AND chat_id=?",
        (SELF, GROUP),
    ).fetchone()[0]
    assert json.loads(stored) == [OWN_BOT, MEMBER, OTHER]
    assert "slack_auto_member_ids" not in record.to_dict()
    assert "slack_auto_checked_at" not in record.to_dict()
    await drain_background(case)
    assert len(case.owner_texts()) == 1
    notice = case.owner_texts()[0]
    assert "Fixture Participant" in notice and "Second Fixture Participant" in notice
    assert "/approve_" not in notice and "/deny_" not in notice
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert await case.broker.retry_due_approval_notifications() == 0
    assert case.delivered() == ["first held", "second held", "trigger"]
    assert case.wire.read_count("conversations.members", token=SELF_TOKEN, chat=GROUP) == 1
    assert all(row[4] != threading.get_ident() for row in case.wire.calls)
    lines = [line for line in capsys.readouterr().err.splitlines() if DECISION in line]
    assert len(lines) == 1
    assert SELF in lines[0] and GROUP in lines[0]
    assert "Fixture" not in lines[0] and "trigger" not in lines[0]


async def test_exactly_nine_members_can_be_approved(case):
    extra = [f"U000000{value:03}" for value in range(100, 107)]
    for member in [MEMBER, *extra]:
        case.grant(member)
    case.wire.use_members([OWN_BOT, MEMBER, *extra])
    await case.broker.handle_inbound(case.message())
    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    assert case.delivered() == ["fixture message"]


@pytest.mark.parametrize("kind", ["unknown", "pending", "denied", "bot"])
async def test_one_unapproved_member_uses_the_existing_hold(case, kind):
    case.grant()
    member = EXTRA_BOT if kind == "bot" else OTHER
    if kind == "pending":
        case.agents.add_pending_user(SELF, member)
    elif kind == "denied":
        case.agents.deny_user(SELF, member)
    case.wire.use_members([OWN_BOT, MEMBER, member])
    await case.broker.handle_inbound(case.message())
    assert_pending(case)
    assert case.wire.read_count("conversations.members") == 1
    expected = {"unknown": None, "pending": "pending", "denied": "denied", "bot": None}
    assert case.agents.get_user_status(SELF, member) == expected[kind]


@pytest.mark.parametrize("kind", ["cursor", "oversize", "missing-self", "only-self"])
async def test_membership_requires_a_complete_small_group_with_the_own_bot(case, kind):
    case.grant()
    if kind == "cursor":
        case.wire.use_members([OWN_BOT, MEMBER], cursor="next-fixture-page")
    elif kind == "oversize":
        extra = [f"U000000{value:03}" for value in range(100, 108)]
        for member in extra:
            case.grant(member)
        case.wire.use_members([OWN_BOT, MEMBER, *extra])
    else:
        case.wire.use_members([MEMBER] if kind == "missing-self" else [OWN_BOT])
    await case.broker.handle_inbound(case.message())
    assert_pending(case)
    assert case.wire.read_count("conversations.members") == 1


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(httpx.ReadError("fixture read failure"), id="request-error"),
        pytest.param(httpx.ReadTimeout("fixture timeout"), id="timeout"),
        pytest.param({"ok": False, "error": "missing_scope"}, id="missing-scope"),
        pytest.param({"ok": True, "members": "not-a-list"}, id="malformed-members"),
        pytest.param(
            {"ok": True, "members": [OWN_BOT, MEMBER], "response_metadata": {"next_cursor": 1}},
            id="malformed-cursor",
        ),
    ],
)
async def test_member_api_failure_keeps_the_existing_hold(case, reply):
    case.grant()
    case.wire.members[SELF_TOKEN, GROUP] = reply
    await case.broker.handle_inbound(case.message())
    assert_pending(case)
    assert case.wire.read_count("conversations.members") == 1


@pytest.mark.parametrize(
    "channel",
    [
        pytest.param({"id": GROUP, "is_channel": True}, id="channel"),
        pytest.param({"id": GROUP, "is_private": True}, id="private-channel"),
        pytest.param({"id": GROUP, "is_im": True}, id="direct-message"),
        pytest.param({"id": GROUP, "is_mpim": "true"}, id="non-boolean-mpim"),
        pytest.param({"id": SECOND_GROUP, "is_mpim": True}, id="wrong-conversation"),
    ],
)
async def test_inbound_mpim_label_cannot_authorize_a_non_mpim(case, channel):
    case.grant()
    case.wire.info[SELF_TOKEN, GROUP] = {"ok": True, "channel": channel}
    await case.broker.handle_inbound(case.message(channel_type="mpim"))
    assert_pending(case)
    assert case.wire.read_count("conversations.members") == 0
    assert case.wire.read_count("conversations.info", token=SELF_TOKEN, chat=GROUP) == 1


@pytest.mark.parametrize("platform", ["telegram", "discord"])
async def test_non_slack_group_keeps_existing_hold_without_resolver(case, platform):
    case.grant()
    await case.broker.handle_inbound(case.message(platform=platform))
    assert_pending(case)
    assert case.wire.calls == []


async def test_slack_direct_message_uses_individual_gate(case):
    case.grant()
    await case.broker.handle_inbound(case.message(chat="D000000001", is_group=False))
    assert case.delivered() == ["fixture message"]
    assert case.agents.get_user_status(SELF, "D000000001") is None
    assert case.wire.calls == []


async def test_denied_conversation_key_is_not_checked_or_held(case):
    case.grant()
    case.agents.deny_user(SELF, GROUP)
    await case.broker.handle_inbound(case.message())
    assert case.agents.get_user_status(SELF, GROUP) == "denied"
    assert case.wire.calls == []
    assert case.held() == [] and case.delivered() == [] and case.owner_texts() == []


async def test_missing_resolver_does_not_use_another_agents_token(case):
    case.grant()
    assert case.agents.remove_token(SELF, "slack")
    await case.broker.handle_inbound(case.message())
    assert_pending(case)
    assert case.wire.calls == []


async def test_approved_automatic_sender_is_db_only(case):
    case.grant()
    case.seed_auto()
    await case.broker.handle_inbound(case.message())
    assert case.delivered() == ["fixture message"]
    assert case.wire.calls == [] and case.owner_texts() == []
    assert case.agents.get_user_status(SELF, GROUP) == "approved"


@pytest.mark.parametrize(
    "status", [None, "pending", "denied"], ids=["unknown", "pending", "denied"]
)
async def test_unapproved_sender_demotes_automatic_group_and_holds(case, status):
    case.grant()
    case.seed_auto()
    if status == "pending":
        case.agents.add_pending_user(SELF, FORMER)
    elif status == "denied":
        case.agents.deny_user(SELF, FORMER)
    await case.broker.handle_inbound(case.message(member=FORMER))
    assert_pending(case, sender=FORMER)
    assert case.wire.calls == []
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert case.delivered() == [] and len(case.owner_texts()) == 1
    assert case.agents.get_user_status(SELF, FORMER) == status
    if status is None:
        await case.broker.handle_inbound(case.message("next eligible arrival"))
        assert case.wire.read_count("conversations.members", chat=GROUP) == 1
        assert case.agents.get_user_status(SELF, GROUP) == "approved"
        assert case.delivered() == ["next eligible arrival"]
        assert [row["sender_id"] for row in case.held()] == [FORMER]


async def test_revoked_sender_cannot_use_an_approved_automatic_group(case):
    case.grant()
    case.grant(FORMER)
    case.seed_auto()
    # The later participant was not part of the recorded approval snapshot.
    assert case.agents.revoke_user(SELF, FORMER)
    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    await case.broker.handle_inbound(case.message(member=FORMER))
    assert_pending(case, sender=FORMER)
    assert case.wire.calls == []


async def test_demotion_and_hold_are_visible_at_the_same_commit(case, monkeypatch):
    case.grant()
    case.seed_auto()
    real_open = agent_registry.open_store_connection
    snapshots = []

    def read_commit():
        with sqlite3.connect(case.agents._db_path) as observer:
            row = observer.execute(
                "SELECT status FROM approved_users WHERE agent_name=? AND chat_id=?",
                (SELF, GROUP),
            ).fetchone()
            count = observer.execute(
                "SELECT count(*) FROM pending_messages WHERE agent_name=? AND chat_id=? "
                "AND delivered=0",
                (SELF, GROUP),
            ).fetchone()[0]
            snapshots.append((row[0], count))
        observer.close()

    class ObserveCommit:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            self.connection.commit()
            read_commit()

    def open_observed(*args, **kwargs):
        return ObserveCommit(real_open(*args, **kwargs))

    monkeypatch.setattr(agent_registry, "open_store_connection", open_observed)
    monkeypatch.setattr(case.agents, "_db", ObserveCommit(case.agents._db))
    await case.broker.handle_inbound(case.message(member=FORMER))
    assert_pending(case, sender=FORMER)
    assert ("pending", 1) in snapshots
    assert ("approved", 1) not in snapshots


async def test_owner_approval_converts_automatic_group_to_manual(case):
    case.grant()
    case.seed_auto()
    case.seed_hold("held before manual approval", FORMER)
    path = f"/agents/{SELF}/approved-users"
    cookie = create_session_cookie(os.environ["PINKY_SESSION_SECRET"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=case.app),
        base_url="http://fixture",
        cookies={SESSION_COOKIE_NAME: cookie},
    ) as client:
        response = await client.post(path, json={"chat_id": GROUP, "approved_by": AUTO_BY})
    assert response.status_code == 200
    assert response.json()["pending_delivered"] == 1
    await case.broker.handle_inbound(case.message("manual route", member=FORMER))
    assert case.delivered() == ["held before manual approval", "manual route"]
    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    assert case.wire.calls == []
    # The display marker is not authoritative if manually supplied.
    stored = case.agents._db.execute(
        "SELECT slack_auto_member_ids FROM approved_users WHERE agent_name=? AND chat_id=?",
        (SELF, GROUP),
    ).fetchone()[0]
    assert stored is None


async def test_manually_approved_group_does_not_check_sender(case):
    case.agents.approve_user(SELF, GROUP, "Fixture Group", "manual")
    case.agents.deny_user(SELF, FORMER)
    await case.broker.handle_inbound(case.message(member=FORMER))
    assert case.delivered() == ["fixture message"]
    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    assert case.held() == [] and case.wire.calls == [] and case.owner_texts() == []


@pytest.mark.parametrize("remove", ["deny_user", "revoke_user"])
async def test_member_removal_demotes_only_matching_automatic_groups(case, remove):
    case.grant()
    case.grant(OTHER, "Other Fixture Participant")
    case.grant(MEMBER, agent=PEER)
    case.seed_auto()
    case.seed_auto(SECOND_GROUP)
    case.seed_auto(THIRD_GROUP, members=(OWN_BOT, OTHER))
    case.seed_auto(agent=PEER, members=(PEER_BOT, MEMBER))
    manual = "G000000010"
    case.agents.approve_user(SELF, manual, "Manual Fixture", "manual")
    getattr(case.agents, remove)(SELF, MEMBER)
    assert {key: row.status for key, row in case.approvals().items() if key.startswith("G")} == {
        GROUP: "pending",
        SECOND_GROUP: "pending",
        THIRD_GROUP: "approved",
        manual: "approved",
    }
    assert case.agents.get_user_status(PEER, GROUP) == "approved"
    assert case.wire.calls == []
    await case.broker.handle_inbound(case.message(member=OTHER))
    assert_pending(case, sender=OTHER)
    assert case.wire.read_count("conversations.members", token=SELF_TOKEN, chat=GROUP) == 1


async def test_former_sender_is_kept_while_later_rows_replay_in_order(case, capsys):
    case.grant()
    case.grant(OTHER, "Other Fixture Participant")
    case.agents.deny_user(SELF, FORMER)
    case.agents.add_pending_user(SELF, GROUP, GROUP)
    kept_id = case.seed_hold("fixture former payload", FORMER)
    case.seed_hold("eligible one", MEMBER)
    case.seed_hold("eligible two", OTHER)
    case.wire.use_members([OWN_BOT, MEMBER, OTHER])
    await case.broker.handle_inbound(case.message("trigger"))
    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    assert case.delivered() == ["eligible one", "eligible two", "trigger"]
    assert [(row["id"], row["sender_id"]) for row in case.held()] == [(kept_id, FORMER)]
    await drain_background(case)
    assert len(case.owner_texts()) == 1
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert case.delivered() == ["eligible one", "eligible two", "trigger"]
    assert len(case.owner_texts()) == 1
    kept_logs = [line for line in capsys.readouterr().err.splitlines() if KEPT in line]
    assert len(kept_logs) == 1
    assert f"row={kept_id}" in kept_logs[0]
    assert "Fixture Display" not in kept_logs[0] and "fixture former payload" not in kept_logs[0]
    case.grant(FORMER)
    assert await case.broker.reconcile_approved_pending_messages() == 1
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert case.delivered() == ["eligible one", "eligible two", "trigger", "fixture former payload"]
    assert case.held() == []


async def test_existing_automatic_replay_keeps_unapproved_sender_without_owner_traffic(
    case, capsys
):
    case.grant()
    case.seed_auto()
    kept_id = case.seed_hold("held", FORMER)
    # The retry loop will see a held row under an approved key on every pass.
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert case.delivered() == [] and case.owner_texts() == []
    assert [(row["id"], row["sender_id"]) for row in case.held()] == [(kept_id, FORMER)]
    kept_logs = [line for line in capsys.readouterr().err.splitlines() if KEPT in line]
    assert len(kept_logs) == 1


@pytest.mark.parametrize("remove", ["deny_user", "revoke_user"])
async def test_replay_stops_after_gate_is_lost(case, remove):
    case.grant()
    case.grant(OTHER)
    case.seed_auto()
    case.seed_hold("first")
    case.seed_hold("second", OTHER)
    case.seed_hold("third")

    async def lose_gate(*args):
        if args[1].content == "first":
            getattr(case.agents, remove)(SELF, GROUP)
        return True

    case.route.side_effect = lose_gate
    assert await case.broker.handle_approval(SELF, GROUP) == 1
    expected = "denied" if remove == "deny_user" else None
    assert case.agents.get_user_status(SELF, GROUP) == expected
    assert case.delivered() == ["first"]
    assert [row["content"] for row in case.held()] == ["second", "third"]


async def test_legacy_group_key_is_not_a_held_sender(case):
    case.grant()
    case.seed_auto()
    kept_id = case.seed_hold("missing sender", "")
    case.seed_hold("known sender")
    assert await case.broker.handle_approval(SELF, GROUP) == 1
    assert case.delivered() == ["known sender"]
    assert [row["id"] for row in case.held()] == [kept_id]


async def test_notification_failure_cannot_block_or_undo_routing(case):
    case.grant()
    blocked, release = asyncio.Event(), asyncio.Event()

    async def send_notice(*args, **kwargs):
        if "/approve_" not in args[3]:
            blocked.set()
            await release.wait()
            raise RuntimeError("fixture notice failure")
        return {"sent": True}

    case.send.side_effect = send_notice
    delivery = asyncio.create_task(case.broker.handle_inbound(case.message()))
    try:
        assert await until(delivery.done), "routing waited on the owner notice"
        await delivery
        assert case.agents.get_user_status(SELF, GROUP) == "approved"
        assert await until(blocked.is_set)
        assert case.delivered() == ["fixture message"]
    finally:
        release.set()
        await delivery
    await drain_background(case)
    assert case.agents.get_user_status(SELF, GROUP) == "approved"


async def test_pending_checks_are_at_most_once_per_minute_per_key(case, monkeypatch):
    case.grant()
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    case.wire.use_members([OWN_BOT, MEMBER, OTHER])
    await case.broker.handle_inbound(case.message("at zero"))
    assert case.wire.read_count("conversations.members", chat=GROUP) == 1
    clock[0] += 59
    await case.broker.handle_inbound(case.message("at fifty-nine"))
    assert case.wire.read_count("conversations.members", chat=GROUP) == 1
    await case.broker.handle_inbound(case.message("other key", chat=SECOND_GROUP))
    assert case.wire.read_count("conversations.members", chat=SECOND_GROUP) == 1
    assert case.agents.get_user_status(SELF, SECOND_GROUP) == "approved"
    clock[0] += 1
    await case.broker.handle_inbound(case.message("at sixty"))
    assert case.wire.read_count("conversations.members", chat=GROUP) == 2
    assert [row["content"] for row in case.held()] == ["at zero", "at fifty-nine", "at sixty"]


async def test_two_arrivals_share_one_check_without_blocking_another_group(case, monkeypatch):
    case.grant()
    started, release = threading.Event(), threading.Event()

    def blocked_members():
        started.set()
        if not release.wait(10):
            raise httpx.ReadTimeout("fixture did not release member read")
        return {"ok": True, "members": [OWN_BOT, MEMBER]}

    case.wire.members[SELF_TOKEN, GROUP] = blocked_members
    shared = AsyncMock(wraps=case.broker.handle_approval)
    monkeypatch.setattr(case.broker, "handle_approval", shared)
    first = asyncio.create_task(case.broker.handle_inbound(case.message("first")))
    pending = [first]
    try:
        await until(lambda: started.is_set() or first.done())
        assert started.is_set(), "the production resolver did not start"
        assert not first.done()
        pending.append(asyncio.create_task(case.broker.handle_inbound(case.message("second"))))
        assert await until(lambda: len(case.held()) == 2), "second arrival could not queue"
        await case.broker.retry_due_approval_notifications()
        assert case.owner_texts() == []
        case.agents.approve_user(SELF, SECOND_GROUP, "Manual Fixture", "manual")
        await case.broker.handle_inbound(case.message("unrelated", chat=SECOND_GROUP))
        assert case.delivered() == ["unrelated"]
    finally:
        release.set()
        await asyncio.gather(*pending)
    assert case.delivered() == ["unrelated", "first", "second"]
    assert case.wire.read_count("conversations.members", chat=GROUP) == 1
    shared.assert_awaited_once_with(SELF, GROUP)
    assert case.held() == []
    await drain_background(case)
    assert len(case.owner_texts()) == 1


async def test_member_removed_during_lookup_cannot_be_committed(case):
    case.grant()
    started, release = threading.Event(), threading.Event()

    def blocked_members():
        started.set()
        if not release.wait(10):
            raise httpx.ReadTimeout("fixture did not release member read")
        return {"ok": True, "members": [OWN_BOT, MEMBER]}

    case.wire.members[SELF_TOKEN, GROUP] = blocked_members
    incoming = asyncio.create_task(case.broker.handle_inbound(case.message()))
    try:
        await until(lambda: started.is_set() or incoming.done())
        assert started.is_set(), "the production resolver did not start"
        case.agents.deny_user(SELF, MEMBER)
    finally:
        release.set()
        await incoming
    assert_pending(case)


async def test_isolated_agent_uses_its_own_token_and_person_rows(case):
    case.grant(MEMBER, "Same Fixture Name", agent=PEER)
    case.grant(OTHER, "Same Fixture Name")
    assert case.agents.get(SELF).isolated is True
    await case.broker.handle_inbound(case.message())
    assert_pending(case)
    assert case.wire.read_count("conversations.members", token=SELF_TOKEN, chat=GROUP) == 1
    assert not any(row[1] == PEER_TOKEN for row in case.wire.calls)
    await case.broker.handle_inbound(case.message("peer delivery", agent=PEER))
    assert case.agents.get_user_status(PEER, GROUP) == "approved"
    assert case.delivered() == ["peer delivery"]
    assert case.wire.read_count("conversations.members", token=PEER_TOKEN, chat=GROUP) == 1


async def test_signed_self_and_system_reads_keep_their_base_boundaries(case):
    case.grant()
    case.grant(OTHER, agent=PEER)

    def headers(path):
        return build_internal_auth_headers(
            case.agents.get_signing_key(SELF),
            agent_name=SELF,
            method="GET",
            path=path,
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=case.app),
        base_url="http://fixture",
    ) as client:
        own = f"/agents/{SELF}/approved-users"
        peer = f"/agents/{PEER}/approved-users"
        refused = "/system/primary-user"
        allowed = "/system/health"
        own_read = await client.get(own, headers=headers(own))
        assert own_read.status_code == 200
        assert {row["chat_id"] for row in own_read.json()["users"]} == {MEMBER, OWNER}
        assert (await client.get(peer, headers=headers(peer))).status_code == 403
        assert (await client.get(refused, headers=headers(refused))).status_code == 403
        assert (await client.get(allowed, headers=headers(allowed))).status_code == 200
    assert case.wire.calls == []


async def test_credential_replacement_updates_resolver_without_socket_mode(case):
    case.grant()
    await case.broker.handle_inbound(case.message("original"))
    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    case.agents.set_token(SELF, "slack", ROTATED_TOKEN)
    await case.broker.handle_inbound(case.message("rotated", chat=SECOND_GROUP))
    assert case.agents.get_user_status(SELF, SECOND_GROUP) == "approved"
    assert case.wire.read_count("conversations.members", token=SELF_TOKEN, chat=GROUP) == 1
    assert (
        case.wire.read_count("conversations.members", token=ROTATED_TOKEN, chat=SECOND_GROUP) == 1
    )
    assert case.agents.remove_token(SELF, "slack")
    prior = len(case.wire.calls)
    await case.broker.handle_inbound(case.message("removed", chat=THIRD_GROUP))
    assert len(case.wire.calls) == prior
    assert case.agents.get_user_status(SELF, THIRD_GROUP) == "pending"
    assert case.delivered() == ["original", "rotated"]


async def test_existing_token_resolves_from_api_assembly_without_socket_mode(case):
    root = case.root / "existing-credential"
    root.mkdir()
    registry_path = root / os.path.basename(case.agents._db_path)
    registry = agent_registry.AgentRegistry(str(registry_path))
    try:
        registry.register(SELF, isolated=True, working_dir=str(root))
        registry.approve_user(SELF, MEMBER, "Fixture Participant", "manual")
        registry.set_token(SELF, "slack", SELF_TOKEN)
    finally:
        registry.close()
    second = api.create_api(db_path=str(root / "memory.db"), default_working_dir=str(root))
    broker = second.state.broker
    broker._send_callback = case.send
    broker._route_streaming = case.route
    try:
        await broker.handle_inbound(case.message())
        assert second.state.agents.get_user_status(SELF, GROUP) == "approved"
        assert case.delivered() == ["fixture message"]
        assert case.wire.read_count("conversations.members", token=SELF_TOKEN, chat=GROUP) == 1
    finally:
        tasks = tuple(broker._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        second.state.store_catalog.close()


async def test_manual_replay_rechecks_rows_after_an_automatic_grant(case):
    case.grant()
    case.agents.approve_user(SELF, GROUP, "Manual Fixture", "manual")
    case.seed_hold("first eligible", MEMBER)
    kept = case.seed_hold("unapproved suffix", FORMER)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_first(agent, message):
        if message.content == "first eligible":
            entered.set()
            await release.wait()
        return True
    case.route.side_effect = hold_first
    manual_replay = asyncio.create_task(case.broker.handle_approval(SELF, GROUP))
    await entered.wait()
    # Owner removes the old conversation grant. A real later message starts
    # the production membership resolver and establishes a NEW automatic grant.
    case.agents.revoke_user(SELF, GROUP)
    trigger = asyncio.create_task(case.broker.handle_inbound(case.message("fresh trigger")))
    try:
        assert await until(lambda: case.agents.get_user_status(SELF, GROUP) == "approved")
        assert case.agents.get_slack_group_approval(SELF, GROUP).automatic
    finally:
        release.set()
        await asyncio.gather(manual_replay, trigger)
    assert "unapproved suffix" not in case.delivered(), case.delivered()
    assert [row["id"] for row in case.held()] == [kept]

    assert case.delivered() == ["first eligible", "fresh trigger"]


async def test_owner_routes_recheck_rows_after_an_automatic_grant(case):
    import os
    case.grant()
    case.agents.add_pending_user(SELF, GROUP)
    case.seed_hold("first eligible", MEMBER)
    former = case.seed_hold("unapproved suffix", FORMER)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_first(agent, message):
        if message.content == "first eligible":
            entered.set()
            await release.wait()
        return True
    case.route.side_effect = hold_first
    cookie = create_session_cookie(os.environ["PINKY_SESSION_SECRET"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app),
            base_url="http://fixture", cookies={SESSION_COOKIE_NAME: cookie}) as client:
        path = f"/agents/{SELF}/approved-users"
        approval = asyncio.create_task(client.post(path, json={"chat_id": GROUP}))
        await entered.wait()
        trigger = None
        try:
            assert (await client.delete(f"{path}/{GROUP}")).status_code == 200
            assert case.agents.get_user_status(SELF, GROUP) is None
            trigger = asyncio.create_task(case.broker.handle_inbound(case.message("fresh trigger")))
            assert await until(lambda: case.agents.get_user_status(SELF, GROUP) == "approved")
            assert case.agents.get_slack_group_approval(SELF, GROUP).automatic
            assert case.wire.read_count("conversations.members") == 1
        finally:
            release.set()
            response = await approval
            if trigger is not None:
                await trigger
        assert response.status_code == 200
    assert "unapproved suffix" not in case.delivered(), case.delivered()
    assert [row["id"] for row in case.held()] == [former]

    assert case.delivered() == ["first eligible", "fresh trigger"]


async def test_manual_replay_without_a_new_automatic_grant_keeps_draining(case):
    case.grant()
    case.agents.approve_user(SELF, GROUP, "Manual Fixture", "manual")
    case.seed_hold("first eligible", MEMBER)
    case.seed_hold("manual suffix", FORMER)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_first(agent, message):
        if message.content == "first eligible":
            entered.set()
            await release.wait()
        return True
    case.route.side_effect = hold_first
    replay = asyncio.create_task(case.broker.handle_approval(SELF, GROUP))
    await entered.wait()
    try:
        case.agents.revoke_user(SELF, GROUP)
        assert case.agents.get_user_status(SELF, GROUP) is None
    finally:
        release.set()
        assert await replay == 2
    assert case.delivered() == ["first eligible", "manual suffix"]
    assert case.held() == []
