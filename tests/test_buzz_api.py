"""API/broker integration coverage for Buzz inc1."""

from __future__ import annotations

import hashlib
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
from pinky_outreach.types import Message, Platform

PRIVATE_KEY = "11" * 32
RECEIPT = "telegram:6770805286:15921|buzz-tos-and-18-plus-approved"
CHANNEL = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _disable_shared_mcp(monkeypatch):
    """API tests don't need the process-global shared MCP listener."""
    monkeypatch.setattr("pinky_daemon.api.SHARED_MCP_ENABLED", False)


def _body(**overrides):
    body = {
        "private_key": PRIVATE_KEY,
        "relay_url": "wss://example.communities.buzz.xyz",
        "community_id": "example",
        "tos_receipt": RECEIPT,
        "enabled": True,
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


def test_owner_bind_is_one_step_server_derived_and_redacted(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/agents", json={"name": "barsik", "model": "sonnet"})
        response = client.put(
            "/system/buzz-identities/barsik",
            json=_body(
                tos_approved_by="agent:forged",
                tos_approved_at=1,
                tos_approval_ref="forged",
            ),
        )

        assert response.status_code == 200, response.text
        identity = response.json()
        assert identity["enabled"] is True
        assert identity["status"] == "active"
        assert identity["tos_approved_by"] == "ui:admin"
        assert identity["tos_approved_at"] > 0
        assert re.fullmatch(
            r"owner-control:[0-9a-f]{32}:sha256:[0-9a-f]{64}",
            identity["tos_approval_ref"],
        )
        assert identity["tos_approval_ref"].endswith(hashlib.sha256(RECEIPT.encode()).hexdigest())
        assert PRIVATE_KEY not in response.text
        assert RECEIPT not in response.text
        assert "ciphertext" not in identity
        assert "nonce" not in identity

        listed = client.get("/system/buzz-identities").text
        assert PRIVATE_KEY not in listed
        assert RECEIPT not in listed

    agents_db = tmp_path / "conversations_agents.db"
    audit_db = tmp_path / "conversations_audit.db"
    assert PRIVATE_KEY.encode() not in agents_db.read_bytes()
    assert PRIVATE_KEY.encode() not in audit_db.read_bytes()
    assert RECEIPT.encode() not in audit_db.read_bytes()


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


def test_startup_refuses_enabled_identity_when_buzz_dependency_is_missing(tmp_path, monkeypatch):
    conversation_db = tmp_path / "conversations.db"
    registry = AgentRegistry(
        str(tmp_path / "conversations_agents.db"),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    registry.register("barsik", model="sonnet", working_dir=str(tmp_path / "barsik"))
    digest = hashlib.sha256(RECEIPT.encode()).hexdigest()
    registry.bind_buzz_identity_owner_control(
        "barsik",
        private_key=PRIVATE_KEY,
        relay_url="wss://example.communities.buzz.xyz",
        community_id="example",
        enabled=True,
        tos_receipt=RECEIPT,
        tos_approved_by="ui:admin",
        tos_approved_at=1234.5,
        tos_approval_ref=f"owner-control:{'a' * 32}:sha256:{digest}",
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
