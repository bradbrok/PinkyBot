"""Approval notices retain the requesting gate across a different sending bot."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import create_api
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.sessions import SessionManager

OWNER_DESTINATION = {
    "platform": "telegram", "account_id": "owner-route",
    "conversation_id": "100", "principal_id": "100",
}


def _rig(tmp_path, monkeypatch, registry=None):
    registry = registry or AgentRegistry(db_path=str(tmp_path / "agents.db"))
    for name in ("requester", "sender", "sibling"):
        registry.register(name, model="sonnet", working_dir=str(tmp_path / name))
    registry.set_owner_notification_destinations([OWNER_DESTINATION])
    sent, attempted, routed, logs = [], [], [], []
    clock = [10000.0]
    monkeypatch.setattr("pinky_daemon.broker.time.time", lambda: clock[0])
    monkeypatch.setattr("pinky_daemon.broker._log", logs.append)

    async def send(agent, platform, chat, text, *, account_id=""):
        call = (agent, platform, chat, text, account_id)
        attempted.append(call)
        if account_id and not registry.get_raw_token_for_account(agent, platform, account_id):
            raise HTTPException(409, f"No {platform} token bound to destination account {account_id}")
        sent.append(call)
        return {"sent": True}

    broker = MessageBroker(registry, SessionManager(), send_callback=send)

    async def route(agent, message):
        routed.append((agent, message))
        return True

    monkeypatch.setattr(broker, "_route_streaming", route)
    return SimpleNamespace(
        registry=registry, broker=broker, sent=sent, attempted=attempted,
        routed=routed, logs=logs, clock=clock, send=send,
    )


def _bind(rig, name="sender", *, account="owner-route", platform="telegram"):
    rig.registry.set_token(name, platform, "synthetic-token", settings={"account_id": account})


def _hold(rig, agent="requester", chat="U_GUEST", *, platform="slack", group=False):
    rig.registry.add_pending_user(agent, chat, "Guest")
    _, request = rig.registry.queue_pending_message_with_approval_request(
        agent_name=agent, platform=platform, chat_id=chat, sender_name="Guest",
        sender_id=chat, content="held message", is_group=group,
        target_name="Guest", held_at=rig.clock[0] - 10,
    )
    return request


def _command(content, **kwargs):
    return BrokerMessage(
        platform="telegram", chat_id=kwargs.pop("chat_id", "100"),
        sender_id=kwargs.pop("sender_id", "100"), sender_name="Owner",
        agent_name=kwargs.pop("agent_name", "sender"), content=content, **kwargs,
    )


def _snapshot(registry):
    return {
        table: registry._db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("approved_users", "approval_requests", "pending_messages")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("group,high_signal", [(False, False), (True, False), (True, True)])
async def test_unbound_requester_uses_bound_sender(tmp_path, monkeypatch, group, high_signal):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    if high_signal:
        rig.registry.approve_user("requester", "U_GUEST")
    request = _hold(rig, chat="C_GATE" if group else "U_GUEST", group=group)
    if high_signal:
        rig.registry._db.execute(
            "UPDATE pending_messages SET sender_id='U_GUEST' WHERE agent_name='requester'"
        )
        rig.registry._db.commit()
    await rig.broker._notify_approval_request(request)
    assert len(rig.sent) == 1
    sender, platform, chat, text, account = rig.sent[0]
    assert (sender, platform, chat, account) == ("sender", "telegram", "100", "owner-route")
    first = text.splitlines()[0]
    assert "requester" in first and "sender" in first and "no bot" in first
    assert text.endswith(f"/approve_req{request['id']}\n/deny_req{request['id']}")
    current = rig.registry.get_approval_request("requester", request["chat_id"])
    assert current["notification_state"] == "delivered"
    assert current["notification_destination"] == {**OWNER_DESTINATION, "sender": "sender"}
    fallback_logs = [line for line in rig.logs if "OWNER_NOTIFY_ROUTE_FALLBACK" in line]
    assert fallback_logs == [
        f"OWNER_NOTIFY_ROUTE_FALLBACK for approval request {request['id']} "
        "agent 'requester' via sender 'sender'"
    ]


@pytest.mark.asyncio
async def test_direct_notice_preserves_original_bytes(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig, "requester")
    _bind(rig)
    request = _hold(rig)
    await rig.broker._notify_approval_request(request)
    assert rig.sent == [(
        "requester", "telegram", "100",
        "🆕 New user wants to talk to requester:\nGuest (ID: U_GUEST)\n\n"
        "Held messages: 1; oldest: 10s\nReview the request:\n"
        "/approve_U_GUEST\n/deny_U_GUEST", "owner-route",
    )]


@pytest.mark.asyncio
async def test_unbound_routes_keep_failure_and_retry_policy(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    request = _hold(rig)
    for attempt in range(1, 6):
        now = rig.clock[0]
        await rig.broker._notify_approval_request(request)
        current = rig.registry.get_approval_request("requester", "U_GUEST")
        assert current["notification_attempts"] == attempt
        assert current["notification_state"] == ("failed" if attempt == 5 else "retrying")
        assert current["last_error"] == (
            "HTTPException: 409: No telegram token bound to destination account owner-route"
        )
        assert current["next_retry_at"] == (0 if attempt == 5 else now + 30 * 2 ** (attempt - 1))
        rig.clock[0] = current["next_retry_at"]
    assert rig.sent == []
    assert [call[0] for call in rig.attempted] == ["requester"] * 5


@pytest.mark.asyncio
async def test_each_failed_route_records_selected_sender(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    fallback = {**OWNER_DESTINATION, "platform": "slack", "account_id": "backup-route"}
    rig.registry.set_owner_notification_destinations([OWNER_DESTINATION, fallback])
    _bind(rig, "sibling", account="backup-route", platform="slack")

    async def send(*args, **kwargs):
        if kwargs.get("account_id") == "owner-route":
            raise RuntimeError("route unavailable")
        return await rig.send(*args, **kwargs)

    rig.broker._send_callback = send
    request = _hold(rig)
    await rig.broker._notify_approval_request(request)
    current = rig.registry.get_approval_request("requester", "U_GUEST")
    assert current["notification_destination"] == {**fallback, "sender": "sibling"}
    assert current["fallback_path"] == [{
        "destination": OWNER_DESTINATION, "sender": "sender",
        "error": "RuntimeError: route unavailable",
    }]


@pytest.mark.asyncio
async def test_broker_and_scheduler_share_ordered_sender_resolution(tmp_path, monkeypatch):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "pinky.db"))
    rig = _rig(tmp_path, monkeypatch, app.state.agents)
    for name in ("aaa_disabled", "aab_wrong_account", "aac_disabled_token", "z_sender"):
        rig.registry.register(name, working_dir=str(tmp_path / name))
        _bind(rig, name)
    rig.registry.update("aaa_disabled", enabled=False)
    _bind(rig, "aab_wrong_account", account="different-route")
    rig.registry.set_token(
        "aac_disabled_token", "telegram", "synthetic-token", enabled=False,
        settings={"account_id": "owner-route"},
    )
    _bind(rig)
    resolver = getattr(rig.registry, "resolve_owner_notification_sender", None)
    assert callable(resolver)
    calls = []

    def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return resolver(*args, **kwargs)

    monkeypatch.setattr(rig.registry, "resolve_owner_notification_sender", resolve)
    app.state.broker._send_callback = rig.send
    request = _hold(rig)
    await rig.broker._notify_approval_request(request)
    assert len(calls) == 1
    assert rig.sent[-1][0] == "sender"
    assert await app.state.scheduler._owner_notify_callback("requester", "scheduler alert") is True
    assert len(calls) == 2 and calls[0] == calls[1]
    assert rig.sent[-1][0] == "sender"
    rig.sent.clear()
    _bind(rig, "requester")
    assert resolver("requester", "telegram", "owner-route") == ("requester", "owner-route")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_bound_requester_precedes_an_earlier_enabled_sender(tmp_path, monkeypatch, enabled):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "pinky.db"))
    rig = _rig(tmp_path, monkeypatch, app.state.agents)
    rig.registry.register("aaa_bound", working_dir=str(tmp_path / "aaa_bound"))
    _bind(rig, "aaa_bound")
    _bind(rig, "requester")
    rig.registry.update("requester", enabled=enabled)
    app.state.broker._send_callback = rig.send
    request = _hold(rig)

    await rig.broker._notify_approval_request(request)

    assert rig.sent == [(
        "requester", "telegram", "100",
        "🆕 New user wants to talk to requester:\nGuest (ID: U_GUEST)\n\n"
        "Held messages: 1; oldest: 10s\nReview the request:\n"
        "/approve_U_GUEST\n/deny_U_GUEST", "owner-route",
    )]
    assert not any("OWNER_NOTIFY_ROUTE_FALLBACK" in line for line in rig.logs)
    assert await app.state.scheduler._owner_notify_callback("requester", "scheduler alert") is True
    assert rig.sent[1:] == [("requester", "telegram", "100", "scheduler alert", "owner-route")]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["approve", "APPROVE"])
async def test_request_command_approves_requester_without_phantom_rows(tmp_path, monkeypatch, prefix):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    request = _hold(rig)
    receiver_before = rig.registry.list_approved_users("sender")
    handled = await rig.broker._handle_approval_command(
        _command(f" /{prefix}_req{request['id']} \n")
    )
    assert handled is True
    assert rig.registry.get_user_status("requester", "U_GUEST") == "approved"
    assert rig.registry.get_approval_request("requester", "U_GUEST")["gate_state"] == "approved"
    assert rig.registry.list_approved_users("sender") == receiver_before
    for table in ("approved_users", "approval_requests", "pending_messages"):
        assert not rig.registry._db.execute(
            f"SELECT 1 FROM {table} WHERE chat_id=?", (f"req{request['id']}",)
        ).fetchall()
    assert [(agent, message.chat_id, message.platform) for agent, message in rig.routed] == [
        ("requester", "U_GUEST", "slack"),
    ]
    assert rig.registry.get_pending_messages("requester", "U_GUEST") == []
    assert rig.sent[-1][:3] == ("sender", "telegram", "100")
    assert "1 pending message(s) delivered to requester" in rig.sent[-1][3]
    assert rig.sent[0][:3] == ("requester", "slack", "U_GUEST")


@pytest.mark.asyncio
async def test_request_command_denies_only_requester(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    request = _hold(rig)
    receiver_before = rig.registry.list_approved_users("sender")
    assert await rig.broker._handle_approval_command(_command(f"/deny_req{request['id']}"))
    assert rig.registry.get_user_status("requester", "U_GUEST") == "denied"
    assert rig.registry.get_approval_request("requester", "U_GUEST")["gate_state"] == "denied"
    assert rig.registry.list_approved_users("sender") == receiver_before
    assert len(rig.registry.get_pending_messages("requester", "U_GUEST")) == 1
    assert rig.routed == []
    assert len(rig.sent) == 1 and rig.sent[0][:3] == ("sender", "telegram", "100")


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"is_group": True}, {"sender_id": "200"}, {"chat_id": "300"},
    {"agent_name": "sibling"},
], ids=["group", "non_owner", "different_conversation", "unbound_bot"])
async def test_request_command_keeps_owner_authorization(tmp_path, monkeypatch, overrides):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    request = _hold(rig)
    before = _snapshot(rig.registry)
    assert await rig.broker._handle_approval_command(
        _command(f"/approve_req{request['id']}", **overrides)
    ) is False
    assert _snapshot(rig.registry) == before
    assert rig.sent == rig.routed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "deny"])
async def test_unknown_request_only_replies(tmp_path, monkeypatch, action):
    """The req<digits> form is reserved; supported platform chat ids never use it."""
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    _hold(rig)
    before = _snapshot(rig.registry)
    assert await rig.broker._handle_approval_command(_command(f"/{action}_req999999"))
    assert _snapshot(rig.registry) == before
    assert rig.sent == [("sender", "telegram", "100", "No such approval request.", "")]
    assert rig.routed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "/approve_reqabc", "/approve_req1 trailing", "/deny_req1suffix",
    "/approve_req1\n/deny_req1", "/approve_req1.0", "/approve_req-1",
    "/approve_req", "/deny_req１", "prefix/approve_req1",
    "/APPROVE_REQabc", "/Deny_Req1 extra", "/approve_REQ",
])
async def test_malformed_request_commands_cannot_become_legacy_targets(
    tmp_path, monkeypatch, command,
):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    _hold(rig)
    before = _snapshot(rig.registry)
    assert await rig.broker._handle_approval_command(_command(command)) is False
    assert _snapshot(rig.registry) == before
    assert rig.sent == rig.routed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["approved", "denied"])
@pytest.mark.parametrize("action", ["approve", "deny"])
async def test_settled_request_cannot_be_changed(tmp_path, monkeypatch, state, action):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    request = _hold(rig)
    getattr(rig.registry, "approve_user" if state == "approved" else "deny_user")(
        "requester", "U_GUEST"
    )
    before = _snapshot(rig.registry)
    assert await rig.broker._handle_approval_command(_command(f"/{action}_req{request['id']}"))
    assert _snapshot(rig.registry) == before
    assert len(rig.sent) == 1 and f"already {state}" in rig.sent[0][3].lower()
    assert rig.routed == []


@pytest.mark.asyncio
async def test_legacy_command_uses_receiving_agent_and_preserves_target_case(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    _hold(rig)
    _hold(rig, "sender")
    assert await rig.broker._handle_approval_command(_command("/APPROVE_U_GUEST"))
    assert rig.registry.get_user_status("sender", "U_GUEST") == "approved"
    assert rig.registry.get_user_status("requester", "U_GUEST") == "pending"
    assert rig.registry.get_user_status("sender", "u_guest") is None
    assert [agent for agent, _ in rig.routed] == ["sender"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("platform", ["slack", "", None])
async def test_user_notice_uses_held_platform_or_skips_without_one(
    tmp_path, monkeypatch, scoped, platform,
):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    agent = "requester" if scoped else "sender"
    if platform is None:
        rig.registry.add_pending_user(agent, "U_GUEST", "Guest")
        request = rig.registry.record_approval_hold(agent, "U_GUEST")
    else:
        request = _hold(rig, agent, platform=platform)
    key = f"req{request['id']}" if scoped else "U_GUEST"
    assert await rig.broker._handle_approval_command(_command(f"/approve_{key}"))
    notices = [call for call in rig.sent if "You've been approved!" in call[3]]
    if platform:
        assert len(notices) == 1 and notices[0][:3] == (agent, platform, "U_GUEST")
    else:
        assert notices == []
        assert len([line for line in rig.logs if "held platform" in line]) == 1
    assert rig.sent[-1][:3] == ("sender", "telegram", "100")


@pytest.mark.asyncio
async def test_request_id_distinguishes_same_chat_between_agents(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    selected = _hold(rig)
    sibling = _hold(rig, "sibling")
    assert selected["id"] != sibling["id"]
    assert await rig.broker._handle_approval_command(_command(f"/approve_req{selected['id']}"))
    assert rig.registry.get_user_status("requester", "U_GUEST") == "approved"
    assert rig.registry.get_user_status("sibling", "U_GUEST") == "pending"
    assert rig.registry.get_approval_request("sibling", "U_GUEST")["gate_state"] == "pending"
    assert len(rig.registry.get_pending_messages("sibling", "U_GUEST")) == 1
    assert [agent for agent, _ in rig.routed] == ["requester"]


@pytest.mark.asyncio
async def test_channel_approval_never_posts_user_notice(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    _bind(rig)
    request = _hold(rig, chat="C_GATE", group=True)
    assert await rig.broker._handle_approval_command(_command(f"/approve_req{request['id']}"))
    assert len(rig.routed) == 1 and rig.routed[0][1].is_group is True
    assert len(rig.sent) == 1 and rig.sent[0][2] == "100"


@pytest.mark.asyncio
async def test_sender_survives_retry_health_and_backlog_projections(tmp_path, monkeypatch):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "pinky.db"))
    rig = _rig(tmp_path, monkeypatch, app.state.agents)
    _bind(rig)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("route unavailable")

    rig.broker._send_callback = unavailable
    request = _hold(rig)
    await rig.broker._notify_approval_request(request)
    current = rig.registry.get_approval_request("requester", "U_GUEST")
    assert current["fallback_path"][0].get("sender") == "sender"
    client = TestClient(app)
    fleet = client.get("/system/approval-backlog")
    health = client.get("/agents/requester/health")
    assert fleet.status_code == health.status_code == 200
    row = fleet.json()["backlogs"][0]
    assert row["fallback_path"] == current["fallback_path"]
    assert (row["agent_name"], row["notification_state"], row["undelivered_count"]) == (
        "requester", "retrying", 1,
    )
    notification_health = health.json()["checks"]["approval_notifications"]
    assert notification_health["healthy"] is False and notification_health["retrying"] == 1
    assert notification_health["requests"][0]["fallback_path"] == current["fallback_path"]
    rig.clock[0] = current["next_retry_at"]
    rig.broker._send_callback = rig.send
    await rig.broker._notify_approval_request(request)
    row = client.get("/system/approval-backlog").json()["backlogs"][0]
    assert row["notification_destination"]["sender"] == "sender"
    notification_health = client.get("/agents/requester/health").json()["checks"][
        "approval_notifications"
    ]
    assert notification_health["healthy"] is True and notification_health["requests"] == []
    stored = rig.registry._db.execute(
        "SELECT notification_destination FROM approval_requests WHERE id=?", (request["id"],)
    ).fetchone()[0]
    assert json.loads(stored)["sender"] == "sender"


@pytest.mark.asyncio
async def test_failed_aging_notice_keeps_the_last_delivered_destination(tmp_path, monkeypatch):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "pinky.db"))
    rig = _rig(tmp_path, monkeypatch, app.state.agents)
    _bind(rig)
    request = _hold(rig)
    await rig.broker._notify_approval_request(request)
    assert len(rig.sent) == 1 and rig.sent[0][0] == "sender"
    destination = {**OWNER_DESTINATION, "sender": "sender"}
    delivered = rig.registry.get_approval_request("requester", "U_GUEST")
    assert delivered["notification_state"] == "delivered"
    assert delivered["notification_destination"] == destination
    rig.clock[0] += 24 * 60 * 60
    failed_calls = []

    async def unavailable(*args, **kwargs):
        failed_calls.append((args, kwargs))
        raise RuntimeError("route unavailable")

    rig.broker._send_callback = unavailable
    await rig.broker._notify_approval_request(request)

    assert len(failed_calls) == 1
    current = rig.registry.get_approval_request("requester", "U_GUEST")
    assert current["notification_state"] == "retrying"
    assert current["aging_reprompt_count"] == 1
    client = TestClient(app)
    health = client.get("/agents/requester/health")
    backlog = client.get("/system/approval-backlog")
    assert health.status_code == backlog.status_code == 200
    health_row = health.json()["checks"]["approval_notifications"]["requests"][0]
    assert health_row.get("notification_destination") == destination
    backlog_row = backlog.json()["backlogs"][0]
    assert backlog_row["notification_destination"] == destination


@pytest.mark.asyncio
async def test_default_owner_channel_uses_another_senders_account(tmp_path, monkeypatch):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "pinky.db"))
    rig = _rig(tmp_path, monkeypatch, app.state.agents)
    _bind(rig, "sender", platform="slack", account="default-route")
    rig.registry.set_setting("operator_chat_id", "D_OWNER")
    rig.registry.set_setting("operator_platform", "slack")
    app.state.broker._send_callback = rig.send
    logs = []
    monkeypatch.setattr("pinky_daemon.api._log", logs.append)

    assert await app.state.scheduler._owner_notify_callback("requester", "scheduler alert") is True

    assert rig.sent == [("sender", "slack", "D_OWNER", "scheduler alert", "default-route")]
    fallback_lines = [line for line in logs if "OWNER_NOTIFY_ROUTE_FALLBACK" in line]
    assert fallback_lines == [
        "api: OWNER_NOTIFY_ROUTE_FALLBACK for scheduler alert agent 'requester': "
        "using daemon default owner channel slack/default-route/D_OWNER via local sender 'sender'"
    ]
