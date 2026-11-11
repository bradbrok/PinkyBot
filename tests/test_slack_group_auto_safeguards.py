"""Deadline, ingress and provenance requirements for automatic Slack groups."""

import asyncio
import os
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from pinky_daemon import broker as broker_module
from pinky_daemon import pollers
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from tests.test_slack_buttons import _payload
from tests.test_slack_group_auto_approval import (
    AUTO_BY,
    FORMER,
    GROUP,
    MEMBER,
    OWN_BOT,
    SELF,
    assert_pending,
    until,
)
from tests.test_slack_group_auto_approval import (
    case as case,
)

pytestmark = pytest.mark.real_auth
OWN_BOT_ID = "B000000006"


def make_poller(case):
    poller = pollers.BrokerSlackPoller(
        Mock(),
        SELF,
        case.broker,
        case.agents,
        app_token="fixture",
    )
    poller._bot_user_id = OWN_BOT
    poller._bot_id = OWN_BOT_ID
    poller._resolve_user_name = AsyncMock(return_value="Fixture Sender")
    poller._resolve_channel_title = AsyncMock(return_value="Fixture Group")
    return poller


async def drain_poller_delivery():
    while pollers._DELIVERY_TASKS:
        await asyncio.gather(*tuple(pollers._DELIVERY_TASKS))
        await asyncio.sleep(0)


async def test_blocked_lookup_releases_owner_request_and_cannot_approve_late(case, monkeypatch):
    case.grant()
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    monkeypatch.setattr(broker_module, "_SLACK_GROUP_CHECK_TIMEOUT_SEC", 0.05)

    async def blocked_resolver(chat, deadline):
        entered.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return OWN_BOT, (OWN_BOT, MEMBER), ""
        finally:
            finished.set()

    case.broker.set_slack_group_resolver(SELF, blocked_resolver)
    incoming = asyncio.create_task(case.broker.handle_inbound(case.message()))
    try:
        assert await until(entered.is_set)
        assert case.owner_texts() == []
        assert await until(lambda: len(case.owner_texts()) == 1, seconds=1), (
            "owner request stayed deferred behind a blocked resolver"
        )
        assert not finished.is_set()
        assert_pending(case)
    finally:
        release.set()
        await incoming
        if entered.is_set():
            await finished.wait()

    assert_pending(case)
    assert await case.broker.reconcile_approved_pending_messages() == 0


@pytest.mark.parametrize("subtype", ["", "bot_message", "file_share"])
async def test_senderless_group_message_from_poller_is_kept_without_request(case, subtype):
    case.grant()
    case.seed_auto()
    poller = make_poller(case)
    await poller._handle_event(
        {
            "event": {
                "type": "message",
                "subtype": subtype,
                "channel_type": "mpim",
                "channel": GROUP,
                "ts": "1700000010.000100",
                "text": "Fixture senderless message",
            }
        }
    )
    await drain_poller_delivery()

    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    assert case.agents.get_approval_request(SELF, GROUP) is None
    assert case.delivered() == []
    assert [row["content"] for row in case.held()] == ["Fixture senderless message"]
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert case.owner_texts() == [] and case.wire.calls == []


async def test_poller_filters_own_echoes_and_system_subtypes(case, monkeypatch):
    case.seed_auto()
    poller = make_poller(case)
    spy = AsyncMock(wraps=case.broker.handle_inbound)
    monkeypatch.setattr(case.broker, "handle_inbound", spy)
    for fields in (
        {"user": OWN_BOT},
        {"bot_id": OWN_BOT_ID, "subtype": "bot_message"},
        {"user": MEMBER, "subtype": "channel_join"},
    ):
        await poller._handle_event(
            {
                "event": {
                    "type": "message",
                    "channel_type": "mpim",
                    "channel": GROUP,
                    "ts": "1700000010.000100",
                    "text": "Fixture event",
                    **fields,
                }
            }
        )
    await drain_poller_delivery()
    spy.assert_not_called()
    assert case.held() == [] and case.owner_texts() == []


@pytest.mark.parametrize("clicker", [OWN_BOT, OWN_BOT_ID])
async def test_own_identity_in_managed_click_is_kept_without_request(case, clicker):
    case.grant(clicker)
    case.seed_auto()
    poller = make_poller(case)
    payload = _payload(channel=GROUP, user=clicker)
    payload["message"].update(user=OWN_BOT, bot_id=OWN_BOT_ID)
    await poller._handle_button_tap(payload)
    await drain_poller_delivery()

    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    assert case.agents.get_approval_request(SELF, GROUP) is None
    assert case.delivered() == []
    held = case.held()
    assert [row["sender_id"] for row in held] == [clicker]
    assert "own_sender" not in held[0]["slack_envelope"]
    assert await case.broker.reconcile_approved_pending_messages() == 0
    assert case.owner_texts() == [] and case.wire.calls == []


@pytest.mark.parametrize("was_automatic", [False, True], ids=["new", "converted"])
async def test_owner_approval_ignores_extra_provenance_fields(case, was_automatic):
    case.grant()
    if was_automatic:
        case.seed_auto()
        case.agents._db.execute(
            "UPDATE approved_users SET slack_auto_checked_at=10 WHERE agent_name=? AND chat_id=?",
            (SELF, GROUP),
        )
        case.agents._db.commit()
    case.seed_hold("fixture backlog", FORMER)
    path = f"/agents/{SELF}/approved-users"
    cookie = create_session_cookie(os.environ["PINKY_SESSION_SECRET"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=case.app),
        base_url="http://fixture",
        cookies={SESSION_COOKIE_NAME: cookie},
    ) as client:
        result = await client.post(
            path,
            json={
                "chat_id": GROUP,
                "display_name": "Fixture Group",
                "approved_by": AUTO_BY,
                "slack_auto_member_ids": [OWN_BOT, MEMBER],
                "slack_auto_checked_at": 123,
            },
        )
        assert result.status_code == 200
        row = case.approvals()[GROUP].to_dict()
        assert row["approved_by"] == AUTO_BY
        assert result.json() == {**row, "pending_delivered": 1}
        listed = await client.get(path)
        assert listed.status_code == 200
        assert next(item for item in listed.json()["users"] if item["chat_id"] == GROUP) == row
    values = case.agents._db.execute(
        "SELECT slack_auto_member_ids, slack_auto_checked_at FROM approved_users "
        "WHERE agent_name=? AND chat_id=?",
        (SELF, GROUP),
    ).fetchone()
    assert values == (None, None)

    case.agents.deny_user(SELF, MEMBER)
    assert case.agents.get_user_status(SELF, GROUP) == "approved"
    await case.broker.handle_inbound(case.message("fixture manual arrival", member=FORMER))
    assert case.delivered() == ["fixture backlog", "fixture manual arrival"]
    assert case.held() == [] and case.owner_texts() == [] and case.wire.calls == []


async def test_manual_replay_keeps_own_sender_fact_after_an_automatic_grant(case):
    case.grant()
    case.grant(OWN_BOT_ID)
    case.seed_auto()
    case.seed_hold("first eligible", MEMBER)
    poller = make_poller(case)
    payload = _payload(channel=GROUP, user=OWN_BOT_ID)
    payload["message"].update(user=OWN_BOT, bot_id=OWN_BOT_ID)
    await poller._handle_button_tap(payload)
    await drain_poller_delivery()
    rows = case.agents.get_pending_messages(SELF, GROUP, include_slack_provenance=True)
    own_row = next(row for row in rows if row["sender_id"] == OWN_BOT_ID)
    assert own_row["slack_envelope"]["own_sender"] is True
    assert case.delivered() == []
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
            trigger = asyncio.create_task(case.broker.handle_inbound(case.message("fresh trigger")))
            assert await until(lambda: case.agents.get_user_status(SELF, GROUP) == "approved")
            assert case.agents.get_slack_group_approval(SELF, GROUP).automatic
        finally:
            release.set()
            response = await approval
            if trigger is not None:
                await trigger
        assert response.status_code == 200
    assert [call.args[1].sender_id for call in case.route.await_args_list] == [MEMBER, MEMBER]
    assert [row["id"] for row in case.held()] == [own_row["id"]]


async def test_public_pending_reads_omit_private_sender_fact(case):
    case.grant(OWN_BOT_ID)
    case.seed_auto()
    poller = make_poller(case)
    payload = _payload(channel=GROUP, user=OWN_BOT_ID)
    payload["message"].update(user=OWN_BOT, bot_id=OWN_BOT_ID)
    await poller._handle_button_tap(payload)
    await drain_poller_delivery()
    internal = case.agents.get_pending_messages(SELF, GROUP, include_slack_provenance=True)
    assert len(internal) == 1 and internal[0]["slack_envelope"]["own_sender"] is True
    scoped = case.agents.get_pending_messages(SELF, GROUP)
    unscoped = case.agents.get_pending_messages(SELF)
    assert scoped == unscoped
    assert "own_sender" not in scoped[0]["slack_envelope"]
    cookie = create_session_cookie(os.environ["PINKY_SESSION_SECRET"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=case.app),
        base_url="http://fixture", cookies={SESSION_COOKIE_NAME: cookie},
    ) as client:
        response = await client.get(f"/agents/{SELF}/pending-messages")
    assert response.status_code == 200
    public_rows = response.json()["by_sender"][GROUP]
    assert public_rows == scoped
    assert "own_sender" not in public_rows[0]["slack_envelope"]
    assert case.agents.get_pending_messages(
        SELF, GROUP, include_slack_provenance=True,
    ) == internal
