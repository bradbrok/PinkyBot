"""API/broker integration coverage for Buzz inc1."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import create_api
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.broker import BrokerMessage
from pinky_outreach.buzz import BuzzNostrSigner
from pinky_outreach.types import Message, Platform

PRIVATE_KEY = "11" * 32
OTHER_PRIVATE_KEY = "22" * 32
CHANNEL = "00000000-0000-4000-8000-000000000001"
OWNER_PUBKEY = BuzzNostrSigner(bytes.fromhex("33" * 32)).pubkey
APPROVED_PUBKEY = BuzzNostrSigner(bytes.fromhex("44" * 32)).pubkey
RELAY_SIGNING_PUBKEY = BuzzNostrSigner(bytes.fromhex("66" * 32)).pubkey


@pytest.fixture(autouse=True)
def _disable_shared_mcp(monkeypatch):
    """API tests don't need the process-global shared MCP listener."""
    monkeypatch.setattr("pinky_daemon.api.SHARED_MCP_ENABLED", False)


def _body(**overrides):
    body = {
        "private_key": PRIVATE_KEY,
        "relay_url": "wss://example.communities.buzz.xyz",
        "community_id": "example",
        "relay_signing_pubkey": RELAY_SIGNING_PUBKEY,
        "enabled": True,
    }
    body.update(overrides)
    return body


def _inbound_body(**overrides):
    body = {
        "owner_pubkey": OWNER_PUBKEY,
        "channels": [{"channel_id": CHANNEL, "label": "#general"}],
        "approved_users": [{"pubkey": APPROVED_PUBKEY, "display_name": "Brad"}],
    }
    body.update(overrides)
    return body


def _app(tmp_path):
    return create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=str(tmp_path / "conversations.db"),
    )


def _register_and_bind(client: TestClient):
    created = client.post("/agents", json={"name": "barsik", "model": "sonnet"})
    assert created.status_code == 200, created.text
    bound = client.put("/system/buzz-identities/barsik", json=_body())
    assert bound.status_code == 200, bound.text
    return bound


def test_owner_bind_generates_identity_scoped_authority_and_redacts_it(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/agents", json={"name": "barsik", "model": "sonnet"})
        response = client.put("/system/buzz-identities/barsik", json=_body())

        assert response.status_code == 200, response.text
        identity = response.json()
        assert identity["enabled"] is True
        assert identity["status"] == "active"
        assert identity["relay_signing_pubkey"] == RELAY_SIGNING_PUBKEY
        assert identity["tos_approved_by"] == "ui:admin"
        assert identity["tos_approved_at"] > 0
        assert re.fullmatch(
            r"owner-control:[0-9a-f]{32}:sha256:[0-9a-f]{64}",
            identity["tos_approval_ref"],
        )
        assert PRIVATE_KEY not in response.text
        assert "ciphertext" not in identity
        assert "nonce" not in identity

        receipt = app.state.agents._db.execute(
            "SELECT tos_receipt FROM buzz_identities WHERE agent='barsik'"
        ).fetchone()[0]
        assert json.loads(receipt) == {
            "action": "buzz.identity.bind",
            "agent": "barsik",
            "community_id": "example",
            "policy": "buzz-tos-and-18-plus",
            "policy_version": 1,
            "pubkey": identity["pubkey"],
            "relay_url": "wss://example.communities.buzz.xyz",
        }
        assert identity["tos_approval_ref"].endswith(hashlib.sha256(receipt.encode()).hexdigest())

        listed = client.get("/system/buzz-identities").text
        assert PRIVATE_KEY not in listed
        assert receipt not in listed

    agents_db = tmp_path / "conversations_agents.db"
    audit_db = tmp_path / "conversations_audit.db"
    assert PRIVATE_KEY.encode() not in agents_db.read_bytes()
    assert PRIVATE_KEY.encode() not in audit_db.read_bytes()
    assert receipt.encode() not in audit_db.read_bytes()


def test_arbitrary_or_caller_supplied_authority_cannot_enable(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/agents", json={"name": "barsik", "model": "sonnet"})
        schema = client.get("/openapi.json").json()["components"]["schemas"][
            "BindBuzzIdentityRequest"
        ]
        assert set(schema["properties"]) == {
            "private_key",
            "relay_url",
            "community_id",
            "relay_signing_pubkey",
            "enabled",
            "inbound",
        }
        assert schema["additionalProperties"] is False
        response = client.put(
            "/system/buzz-identities/barsik",
            json=_body(
                tos_receipt="THIS IS ARBITRARY CALLER TEXT, NOT A RESOLVED APPROVAL RECORD",
                tos_approved_by="agent:forged",
                tos_approved_at=1,
                tos_approval_ref="forged",
            ),
        )

        assert response.status_code == 422
        assert app.state.agents.get_buzz_identity("barsik") is None
        assert PRIVATE_KEY not in response.text


def test_one_approval_cannot_enable_a_second_identity(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        for name in ("barsik", "murzik"):
            response = client.post("/agents", json={"name": name, "model": "sonnet"})
            assert response.status_code == 200
        first = client.put("/system/buzz-identities/barsik", json=_body())
        assert first.status_code == 200
        first_state = first.json()
        first_receipt = app.state.agents._db.execute(
            "SELECT tos_receipt FROM buzz_identities WHERE agent='barsik'"
        ).fetchone()[0]

        replay = client.put(
            "/system/buzz-identities/murzik",
            json=_body(
                private_key=OTHER_PRIVATE_KEY,
                tos_receipt=first_receipt,
                tos_approved_by=first_state["tos_approved_by"],
                tos_approved_at=first_state["tos_approved_at"],
                tos_approval_ref=first_state["tos_approval_ref"],
            ),
        )
        assert replay.status_code == 422
        assert app.state.agents.get_buzz_identity("murzik") is None

        second = client.put(
            "/system/buzz-identities/murzik",
            json=_body(private_key=OTHER_PRIVATE_KEY),
        )
        assert second.status_code == 200
        second_state = second.json()
        second_receipt = app.state.agents._db.execute(
            "SELECT tos_receipt FROM buzz_identities WHERE agent='murzik'"
        ).fetchone()[0]
        assert second_state["tos_approval_ref"] != first_state["tos_approval_ref"]
        assert second_receipt != first_receipt
        assert json.loads(second_receipt)["agent"] == "murzik"
        assert json.loads(second_receipt)["pubkey"] == second_state["pubkey"]


def test_rebind_preserves_immutable_server_authority(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        first = _register_and_bind(client).json()
        before = app.state.agents._db.execute(
            "SELECT tos_receipt, tos_approved_by, tos_approved_at, tos_approval_ref "
            "FROM buzz_identities WHERE agent='barsik'"
        ).fetchone()

        rebound = client.put("/system/buzz-identities/barsik", json=_body())
        assert rebound.status_code == 200
        after = app.state.agents._db.execute(
            "SELECT tos_receipt, tos_approved_by, tos_approved_at, tos_approval_ref "
            "FROM buzz_identities WHERE agent='barsik'"
        ).fetchone()
        assert after == before
        assert rebound.json()["tos_approval_ref"] == first["tos_approval_ref"]


def test_valid_internal_agent_auth_cannot_write_tos_authority(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/agents", json={"name": "barsik", "model": "sonnet"})
        signing_key = app.state.agents.get_signing_key("barsik")
        client.cookies.clear()
        headers = build_internal_auth_headers(
            signing_key,
            agent_name="barsik",
            method="PUT",
            path="/system/buzz-identities/barsik",
        )

        response = client.put(
            "/system/buzz-identities/barsik",
            json=_body(),
            headers=headers,
        )

        assert response.status_code == 403
        assert "owner session" in response.json()["detail"]
        assert PRIVATE_KEY not in response.text
        assert app.state.agents.get_buzz_identity("barsik") is None


def test_one_step_bind_configures_inbound_gates_and_starts_native_poller(
    tmp_path, monkeypatch
):
    async def idle_poller(self):  # noqa: ANN001
        self._running = True
        while self._running:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(
        "pinky_daemon.buzz_inbound.BrokerBuzzPoller.start",
        idle_poller,
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/agents", json={"name": "barsik", "model": "sonnet"})
        response = client.put(
            "/system/buzz-identities/barsik",
            json=_body(inbound=_inbound_body()),
        )

        assert response.status_code == 200, response.text
        policy = response.json()["inbound"]
        assert policy["owner_principal"] == f"buzz:example:{OWNER_PUBKEY}"
        assert policy["channels"] == [{"channel_id": CHANNEL, "label": "#general"}]
        assert policy["approved_users"][0]["principal"] == (
            f"buzz:example:{APPROVED_PUBKEY}"
        )
        assert policy["updated_by"] == "ui:admin"
        fetched = client.get("/system/buzz-identities/barsik/inbound")
        assert fetched.status_code == 200
        assert fetched.json()["principals"] == policy["principals"]
        status = client.get("/broker/status").json()
        assert status["active_pollers"] == [
            {"agent": "barsik", "polls": 0, "running": True}
        ]

        disabled = client.post("/system/buzz-identities/barsik/disable")
        assert disabled.status_code == 200
        health = client.get("/agents/barsik/health").json()
        assert health["checks"]["buzz_inbound"]["enabled"] is False
        assert health["recommendation"] != "degraded"


def test_failed_one_step_bind_rolls_back_identity_and_policy_together(tmp_path):
    app = _app(tmp_path)
    agent_pubkey = BuzzNostrSigner(bytes.fromhex(PRIVATE_KEY)).pubkey
    other_pubkey = BuzzNostrSigner(bytes.fromhex(OTHER_PRIVATE_KEY)).pubkey
    with TestClient(app) as client:
        for name in ("barsik", "murzik"):
            created = client.post("/agents", json={"name": name, "model": "sonnet"})
            assert created.status_code == 200

        initial = client.put(
            "/system/buzz-identities/barsik",
            json=_body(enabled=False),
        )
        assert initial.status_code == 200
        identity_before = app.state.agents._db.execute(
            "SELECT * FROM buzz_identities WHERE agent='barsik'"
        ).fetchone()

        failed_rebind = client.put(
            "/system/buzz-identities/barsik",
            json=_body(enabled=True, inbound=_inbound_body(owner_pubkey=agent_pubkey)),
        )
        failed_first_bind = client.put(
            "/system/buzz-identities/murzik",
            json=_body(
                private_key=OTHER_PRIVATE_KEY,
                inbound=_inbound_body(owner_pubkey=other_pubkey),
            ),
        )

        assert failed_rebind.status_code == 400
        assert failed_first_bind.status_code == 400
        assert (
            app.state.agents._db.execute(
                "SELECT * FROM buzz_identities WHERE agent='barsik'"
            ).fetchone()
            == identity_before
        )
        assert app.state.agents.get_buzz_identity("barsik")["enabled"] is False
        assert app.state.agents.get_buzz_inbound_policy("barsik") is None
        assert app.state.agents.get_buzz_identity("murzik") is None
        assert app.state.agents.get_buzz_inbound_policy("murzik") is None


def test_inbound_policy_rejects_caller_authority_and_internal_agent_auth(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/agents", json={"name": "barsik", "model": "sonnet"})
        bound = client.put("/system/buzz-identities/barsik", json=_body())
        assert bound.status_code == 200

        forged = client.put(
            "/system/buzz-identities/barsik/inbound",
            json=_inbound_body(
                updated_by="agent:forged",
                updated_at=1,
                owner_last_seen_at=1,
                relay_url="wss://attacker.invalid",
                community_id="attacker",
            ),
        )
        assert forged.status_code == 422
        assert app.state.agents.get_buzz_inbound_policy("barsik") is None

        signing_key = app.state.agents.get_signing_key("barsik")
        client.cookies.clear()
        path = "/system/buzz-identities/barsik/inbound"
        headers = build_internal_auth_headers(
            signing_key,
            agent_name="barsik",
            method="PUT",
            path=path,
        )
        denied = client.put(path, json=_inbound_body(), headers=headers)
        assert denied.status_code == 403
        assert "owner session" in denied.json()["detail"]
        assert app.state.agents.get_buzz_inbound_policy("barsik") is None


def test_daemon_restart_resumes_configured_native_buzz_poller(
    tmp_path, monkeypatch, stub_sdk_transport
):
    registry = AgentRegistry(
        str(tmp_path / "conversations_agents.db"),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    registry.register("barsik", model="sonnet", working_dir=str(tmp_path / "barsik"))
    registry.bind_buzz_identity_owner_control(
        "barsik",
        private_key=PRIVATE_KEY,
        relay_url="wss://example.communities.buzz.xyz",
        community_id="example",
        relay_signing_pubkey=RELAY_SIGNING_PUBKEY,
        enabled=True,
        owner_actor="ui:admin",
    )
    registry.configure_buzz_inbound_owner_control(
        "barsik",
        owner_pubkey=OWNER_PUBKEY,
        channels=[{"channel_id": CHANNEL, "label": "#general"}],
        approved_users=[{"pubkey": APPROVED_PUBKEY, "display_name": "Brad"}],
        owner_actor="ui:admin",
    )
    interrupted = BuzzNostrSigner(bytes.fromhex("44" * 32)).sign_event(
        kind=9,
        tags=[["h", CHANNEL]],
        content="resume after kill -9",
    )
    assert registry.begin_buzz_inbound_event_delivery(
        "barsik",
        interrupted,
        community_id="example",
        channel_id=CHANNEL,
    )
    registry.close()

    async def idle_poller(self):  # noqa: ANN001
        self._running = True
        while self._running:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(
        "pinky_daemon.buzz_inbound.BrokerBuzzPoller.start",
        idle_poller,
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/broker/status").json()["active_pollers"] == [
            {"agent": "barsik", "polls": 0, "running": True}
        ]
        assert app.state.agents._db.execute(
            "SELECT claimed_at FROM buzz_inbound_events WHERE event_id=?",
            (interrupted["id"],),
        ).fetchone()[0] == 0


def test_broker_send_thread_and_react_route_to_native_buzz_adapter(tmp_path):
    app = _app(tmp_path)
    parent = "22" * 32
    author = "33" * 32
    with TestClient(app) as client:
        _register_and_bind(client)
        app.state.broker.remember_message_context(
            BrokerMessage(
                platform="buzz",
                chat_id=CHANNEL,
                sender_name="Brad",
                sender_id=author,
                content="ephemeral metadata only",
                agent_name="barsik",
                message_id=parent,
                reply_to="aa" * 32,
                is_group=True,
                metadata={
                    "buzz_verified_event": {
                        "verified": True,
                        "event_id": parent,
                        "kind": 20002,
                        "author_pubkey": author,
                        "channel_id": CHANNEL,
                    },
                    "content": "raw ephemeral body must never be forwarded",
                },
            )
        )
        sent_message = Message(
            platform=Platform.buzz,
            chat_id=CHANNEL,
            sender="44" * 32,
            content="",
            timestamp=datetime.now(timezone.utc),
            message_id="55" * 32,
            is_outbound=True,
        )

        with (
            patch(
                "pinky_outreach.buzz.BuzzAdapter.send_message",
                return_value=sent_message,
            ) as send,
            patch(
                "pinky_outreach.buzz.BuzzAdapter.add_reaction",
                return_value=SimpleNamespace(message_id="66" * 32),
            ) as react,
        ):
            root = client.post(
                "/broker/send",
                json={
                    "agent_name": "barsik",
                    "platform": "buzz",
                    "chat_id": CHANNEL,
                    "content": "hello",
                },
            )
            thread = client.post(
                "/broker/thread",
                json={
                    "agent_name": "barsik",
                    "message_id": parent,
                    "content": "reply",
                },
            )
            reaction = client.post(
                "/broker/react",
                json={
                    "agent_name": "barsik",
                    "message_id": parent,
                    "emoji": "👍",
                },
            )

        assert root.status_code == 200, root.text
        assert root.json()["message_id"] == "55" * 32
        assert thread.status_code == 200, thread.text
        assert reaction.status_code == 200, reaction.text
        assert send.call_count == 2
        assert send.call_args_list[0].kwargs == {
            "reply_to": None,
            "reply_metadata": None,
        }
        assert send.call_args_list[1].kwargs["reply_to"] == parent
        assert (
            send.call_args_list[1].kwargs["reply_metadata"]["buzz_verified_event"]["kind"] == 20002
        )
        assert "content" not in send.call_args_list[1].kwargs["reply_metadata"]
        react.assert_called_once_with(CHANNEL, parent, "👍")


def test_startup_refuses_enabled_identity_when_buzz_dependency_is_missing(
    tmp_path, monkeypatch, stub_sdk_transport
):
    conversation_db = tmp_path / "conversations.db"
    registry = AgentRegistry(
        str(tmp_path / "conversations_agents.db"),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    registry.register("barsik", model="sonnet", working_dir=str(tmp_path / "barsik"))
    registry.bind_buzz_identity_owner_control(
        "barsik",
        private_key=PRIVATE_KEY,
        relay_url="wss://example.communities.buzz.xyz",
        community_id="example",
        relay_signing_pubkey=RELAY_SIGNING_PUBKEY,
        enabled=True,
        owner_actor="ui:admin",
    )
    registry.close()
    monkeypatch.setattr(
        "pinky_daemon.buzz_runtime.missing_buzz_dependencies",
        lambda: ("coincurve",),
    )

    app = create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=str(conversation_db),
    )
    with TestClient(app):
        assert app.state.buzz_registration["refused"] == 1
        identity = app.state.agents.get_buzz_identity("barsik")
        assert identity["enabled"] is True
        assert identity["status"] == "dependency_refused"
        assert app.state.agents.get_buzz_signing_material("barsik") is None
