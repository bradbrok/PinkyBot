"""The owner's identity settings change only from an owner session.

The primary user and the owner notification destinations say who the owner
is and where owner alerts go, and the owner profile holds the code word
agents use to verify the owner. An agent's signed request passes the auth
middleware, so these write routes check for an owner session themselves.
"""

import os

import httpx
import pytest

from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth

DESTINATION = {
    "platform": "telegram",
    "account_id": "fixture-account",
    "conversation_id": "fixture-chat",
    "principal_id": "fixture-owner",
}


def owner_headers():
    secret = os.environ["PINKY_SESSION_SECRET"]
    return {"cookie": f"{SESSION_COOKIE_NAME}={create_session_cookie(secret)}"}


def client(d, headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=d.app), base_url="http://test", headers=headers or {}
    )


async def put_primary(c, headers=None):
    return await c.put(
        "/system/primary-user",
        params={"chat_id": "agent-chosen-chat", "display_name": "Not the owner"},
        headers=headers or {},
    )


async def put_destinations(c, headers=None):
    return await c.put(
        "/system/owner-notification-destinations",
        json={"destinations": [DESTINATION]},
        headers=headers or {},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["normal", "dreamer", "tenant"])
async def test_a_signed_agent_cannot_change_the_primary_user(daemon, actor):
    d = daemon()
    before = d.agents.get_primary_user()
    async with client(d) as c:
        response = await put_primary(c, signed(d, "PUT", "/system/primary-user", actor))
    assert response.status_code == 403, response.text
    if actor != "tenant":  # an isolated tenant may be refused earlier by its own policy
        assert response.json() == {"detail": "authenticated owner session required"}
    assert d.agents.get_primary_user() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["normal", "dreamer", "tenant"])
async def test_a_signed_agent_cannot_change_the_owner_destinations(daemon, actor):
    d = daemon()
    path = "/system/owner-notification-destinations"
    before = d.agents.get_owner_notification_destinations()
    async with client(d) as c:
        response = await put_destinations(c, signed(d, "PUT", path, actor))
    assert response.status_code == 403, response.text
    if actor != "tenant":
        assert response.json() == {"detail": "authenticated owner session required"}
    assert d.agents.get_owner_notification_destinations() == before


@pytest.mark.asyncio
async def test_no_credentials_are_refused_and_change_nothing(daemon):
    d = daemon()
    primary = d.agents.get_primary_user()
    destinations = d.agents.get_owner_notification_destinations()
    async with client(d) as c:
        assert (await put_primary(c)).status_code in (401, 403)
        assert (await put_destinations(c)).status_code in (401, 403)
    assert d.agents.get_primary_user() == primary
    assert d.agents.get_owner_notification_destinations() == destinations


@pytest.mark.asyncio
async def test_the_owner_session_still_sets_both(daemon):
    d = daemon()
    async with client(d, owner_headers()) as c:
        response = await put_primary(c)
        assert response.status_code == 200, response.text
        assert response.json()["updated"] is True
        response = await put_destinations(c)
        assert response.status_code == 200, response.text
        assert response.json()["destinations"][0]["conversation_id"] == "fixture-chat"
    assert d.agents.get_primary_user()["chat_id"] == "agent-chosen-chat"
    assert d.agents.get_owner_notification_destinations()[0]["conversation_id"] == "fixture-chat"


@pytest.mark.asyncio
async def test_reads_stay_open_to_signed_agents(daemon):
    d = daemon()
    async with client(d) as c:
        for path in ("/system/primary-user", "/system/owner-notification-destinations"):
            response = await c.get(path, headers=signed(d, "GET", path, "normal"))
            assert response.status_code == 200, response.text


async def put_profile(c, headers=None):
    return await c.put(
        "/settings/owner-profile",
        json={"name": "Not the owner", "code_word": "agent-chosen-word"},
        headers=headers or {},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["normal", "dreamer", "tenant"])
async def test_a_signed_agent_cannot_change_the_owner_profile_or_code_word(daemon, actor):
    d = daemon()
    path = "/settings/owner-profile"
    before = d.agents.get_owner_profile()
    async with client(d) as c:
        response = await put_profile(c, signed(d, "PUT", path, actor))
    assert response.status_code == 403, response.text
    if actor != "tenant":
        assert response.json() == {"detail": "authenticated owner session required"}
    assert d.agents.get_owner_profile() == before


@pytest.mark.asyncio
async def test_the_owner_session_still_sets_the_profile_and_agents_can_read_it(daemon):
    d = daemon()
    async with client(d, owner_headers()) as c:
        response = await put_profile(c)
        assert response.status_code == 200, response.text
    assert d.agents.get_owner_profile()["code_word"] == "agent-chosen-word"
    async with client(d) as c:
        path = "/settings/owner-profile"
        response = await c.get(path, headers=signed(d, "GET", path, "normal"))
        assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["ui_password_hash", "primary_user_chat_id", "owner_notification_destinations", "OPENAI_API_KEY"],
)
@pytest.mark.parametrize("actor", ["normal", "dreamer"])
async def test_a_signed_agent_cannot_clear_any_setting_through_the_api_key_routes(daemon, actor, key):
    d = daemon()
    d.agents.set_setting(key, "kept")
    path = f"/system/api-keys/{key}"
    async with client(d) as c:
        response = await c.delete(path, headers=signed(d, "DELETE", path, actor))
        assert response.status_code == 403, response.text
        response = await c.put(path, json={"value": "x"}, headers=signed(d, "PUT", path, actor))
        assert response.status_code == 403, response.text
    assert d.agents.get_setting(key) == "kept"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["ui_password_hash", "primary_user_chat_id", "owner_notification_destinations"])
async def test_the_owner_cannot_clear_non_api_key_settings_through_delete(daemon, key):
    d = daemon()
    d.agents.set_setting(key, "kept")
    async with client(d, owner_headers()) as c:
        response = await c.delete(f"/system/api-keys/{key}")
    assert response.status_code == 400, response.text
    assert d.agents.get_setting(key) == "kept"


@pytest.mark.asyncio
async def test_the_owner_still_sets_and_clears_an_api_key(daemon):
    d = daemon()
    async with client(d, owner_headers()) as c:
        assert (await c.put("/system/api-keys/OPENAI_API_KEY", json={"value": "sk-test"})).status_code == 200
        assert d.agents.get_setting("OPENAI_API_KEY") == "sk-test"
        assert (await c.delete("/system/api-keys/OPENAI_API_KEY")).status_code == 200
    assert d.agents.get_setting("OPENAI_API_KEY") == ""


@pytest.mark.asyncio
async def test_a_refused_agent_delete_keeps_ui_setup_closed(daemon, monkeypatch):
    monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
    setup = {"password": "a-long-fixture-password"}
    first = daemon()
    async with client(first) as c:  # control: a first-ever setup still opens a session
        response = await c.post("/auth/setup", json=setup)
        assert response.status_code == 200, response.text
        assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")

    d = daemon()
    d.agents.set_setting("ui_password_hash", "stored-hash")
    path = "/system/api-keys/ui_password_hash"
    async with client(d) as c:
        response = await c.delete(path, headers=signed(d, "DELETE", path, "normal"))
        assert response.status_code == 403, response.text
        response = await c.post("/auth/setup", json=setup)
        assert response.status_code == 409, response.text
        assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")
    assert d.agents.get_setting("ui_password_hash") == "stored-hash"
