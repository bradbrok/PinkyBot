"""Token listing responses must not return secret settings values."""

from __future__ import annotations

import copy
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api, claude_runner, pollers
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from tests.conftest import TEST_SESSION_SECRET

pytestmark = pytest.mark.real_auth

BOT_TOKEN = "xoxb-FAKE-BOT"
APP_TOKEN = "xapp-FAKE-APP"
RESPONSE_KINDS = ("global", "self", "put")


@pytest.fixture
def service(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Token response tests must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setattr(claude_runner, "_find_claude_binary", lambda: "/fixture/no-cli")
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "off")
    monkeypatch.setenv("PINKY_SLACK_SOCKET_MODE", "0")
    started = []
    monkeypatch.setattr(pollers, "start_poller", started.append)
    app = api.create_api(
        db_path=str(tmp_path / "registry.db"),
        default_working_dir=str(tmp_path),
    )
    registry = app.state.agents
    keys = {}
    for name, isolated in (("tenant", True), ("normal", False), ("other", True)):
        work = tmp_path / name
        work.mkdir()
        registry.register(name, isolated=isolated, working_dir=str(work))
        keys[name] = registry.get_signing_key(name)
    client = TestClient(app, follow_redirects=False)
    try:
        yield SimpleNamespace(
            app=app, registry=registry, client=client, keys=keys, started=started,
        )
    finally:
        client.close()
        app.state.store_catalog.close()
        registry.close()


def request(service, method, path, *, caller="normal", body=None):
    if caller == "operator":
        cookie = create_session_cookie(TEST_SESSION_SECRET)
        headers = {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}"}
    else:
        headers = build_internal_auth_headers(
            service.keys[caller], agent_name=caller, method=method, path=path,
        )
    # An absolute URL preserves leading repeated slashes as an ASGI path.
    return service.client.request(
        method, f"http://testserver{path}", headers=headers, json=body,
    )


def seed(service, settings, *, name="tenant"):
    return service.registry.set_token(
        name, "slack", BOT_TOKEN, enabled=True, settings=copy.deepcopy(settings),
    )


def settings_response(service, kind, settings):
    seed(service, settings)
    if kind == "put":
        response = request(
            service, "PUT", "/agents/tenant/tokens/slack", caller="tenant",
            body={"token": BOT_TOKEN, "enabled": True, "settings": settings},
        )
        assert response.status_code == 200, response.text
        value = response.json()["settings"]
    else:
        path = "/system/all-tokens" if kind == "global" else "/agents/tenant/tokens"
        response = request(
            service, "GET", path, caller="normal" if kind == "global" else "tenant",
        )
        assert response.status_code == 200, response.text
        value = response.json()["tokens"][0]["settings"]
        if kind == "global":
            assert isinstance(value, str), "Global token settings keep their JSON-string type"
            value = json.loads(value)
    if kind != "global":
        assert isinstance(value, dict), "Per-token response settings keep their dict type"
    assert BOT_TOKEN not in response.text
    return value


@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
@pytest.mark.parametrize(
    "path", ("/system/all-tokens", "/system/all-tokens/", "//system//all-tokens"),
)
def test_isolated_global_token_listing_is_always_denied(service, monkeypatch, mode, path):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
    seed(service, {"app_token": APP_TOKEN})
    response = request(service, "GET", path, caller="tenant")
    assert response.status_code == 403, response.text
    assert APP_TOKEN not in response.text


@pytest.mark.parametrize("lookup", ("missing", "error"))
def test_verified_caller_without_registry_authority_is_denied(service, monkeypatch, lookup):
    seed(service, {"app_token": APP_TOKEN})
    original = service.registry.get

    def get(name):
        if name == "tenant":
            if lookup == "error":
                raise RuntimeError("FAKE-LOOKUP-ERROR")
            return None
        return original(name)

    monkeypatch.setattr(service.registry, "get", get)
    response = request(service, "GET", "/system/all-tokens", caller="tenant")
    assert response.status_code == 403, response.text
    assert APP_TOKEN not in response.text and "FAKE-LOOKUP-ERROR" not in response.text


def test_isolated_signature_cannot_borrow_operator_session(service):
    seed(service, {"app_token": APP_TOKEN})
    path = "/system/all-tokens"
    headers = build_internal_auth_headers(
        service.keys["tenant"], agent_name="tenant", method="GET", path=path,
    )
    headers["Cookie"] = (
        f"{SESSION_COOKIE_NAME}={create_session_cookie(TEST_SESSION_SECRET)}"
    )
    response = service.client.get(path, headers=headers)
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("caller", ("normal", "operator"))
def test_global_listing_redacts_all_rows_and_preserves_json_type(service, caller):
    settings = {
        "app_token": APP_TOKEN,
        "poll_interval_sec": 3.5,
        "watched_channels": ["FAKE-CHANNEL"],
        "label": "visible",
    }
    seed(service, settings)
    seed(service, {**settings, "app_token": "xapp-FAKE-OTHER"}, name="other")
    before = service.registry.list_all_tokens()
    response = request(service, "GET", "/system/all-tokens", caller=caller)
    assert response.status_code == 200, response.text
    rows = response.json()["tokens"]
    assert len(rows) == 2
    for row in rows:
        assert row["token_set"] is True and row["enabled"] is True
        assert isinstance(row["settings"], str)
        assert json.loads(row["settings"]) == {**settings, "app_token": "set"}
    assert APP_TOKEN not in response.text and "xapp-FAKE-OTHER" not in response.text
    assert BOT_TOKEN not in response.text
    assert service.registry.list_all_tokens() == before, "Redaction is response-only"


def test_isolated_self_listing_redacts_settings(service):
    seed(service, {"app_token": APP_TOKEN, "label": "visible"})
    response = request(service, "GET", "/agents/tenant/tokens", caller="tenant")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["agent"] == "tenant" and value["count"] == 1
    assert value["tokens"][0]["settings"] == {"app_token": "set", "label": "visible"}
    assert service.registry.get_token("tenant", "slack").settings["app_token"] == APP_TOKEN


@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
def test_isolated_caller_still_cannot_list_another_agent(service, monkeypatch, mode):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
    seed(service, {"app_token": APP_TOKEN}, name="other")
    response = request(service, "GET", "/agents/other/tokens", caller="tenant")
    assert response.status_code == 403, response.text
    assert APP_TOKEN not in response.text


@pytest.mark.parametrize("kind", RESPONSE_KINDS)
@pytest.mark.parametrize(
    "key",
    ("client_TOKEN_value", "CLIENT_SECRET", "PaSsWoRd", "credential_ref",
     "key", "API_KEY", "signing_key", "mOnKeY"),
    ids=("token", "secret", "password", "credential", "bare-key", "api-key",
         "signing-key", "suffix-only"),
)
def test_secret_key_rules_are_case_insensitive_at_every_response(service, kind, key):
    original = {key: "FAKE-SECRET-VALUE", "label": "visible"}
    assert settings_response(service, kind, original) == {key: "set", "label": "visible"}
    token = service.registry.get_token("tenant", "slack")
    assert token.settings == original
    assert token.to_dict()["settings"] == original


@pytest.mark.parametrize("kind", RESPONSE_KINDS)
@pytest.mark.parametrize(
    "value", ("", None, False, 0, [], {}), ids=("text", "null", "false", "zero", "list", "dict"),
)
def test_falsey_secret_values_are_unchanged(service, kind, value):
    original = {"api_key": value, "label": "visible"}
    actual = settings_response(service, kind, original)
    assert actual == original
    assert type(actual["api_key"]) is type(value)


@pytest.mark.parametrize("kind", RESPONSE_KINDS)
@pytest.mark.parametrize(
    "value",
    ({"label": "visible", "child_password": "FAKE-SECRET"}, ["visible", {"label": "visible"}],
     True, 7),
    ids=("dict", "list", "true", "number"),
)
def test_truthy_secret_values_are_redacted_as_a_whole(service, kind, value):
    original = {"app_token": value, "label": "visible"}
    assert settings_response(service, kind, original) == {"app_token": "set", "label": "visible"}
    assert service.registry.get_token("tenant", "slack").settings == original


@pytest.mark.parametrize("kind", RESPONSE_KINDS)
def test_nested_nonsecret_containers_are_recursively_redacted(service, kind):
    original = {
        "metadata": {
            "items": [
                {"service_secret": "FAKE-SECRET", "label": "visible"},
                {"credential": "", "children": [{"Password": "FAKE-PASSWORD"}]},
                "visible",
            ],
            "label": "visible",
        },
    }
    expected = {
        "metadata": {
            "items": [
                {"service_secret": "set", "label": "visible"},
                {"credential": "", "children": [{"Password": "set"}]},
                "visible",
            ],
            "label": "visible",
        },
    }
    assert settings_response(service, kind, original) == expected
    assert service.registry.get_token("tenant", "slack").settings == original


@pytest.mark.parametrize("kind", RESPONSE_KINDS)
def test_absent_secret_keys_are_not_added(service, kind):
    assert settings_response(service, kind, {"label": "visible"}) == {"label": "visible"}


@pytest.mark.parametrize("caller", ("normal", "operator"))
def test_unparseable_global_settings_are_unreadable_without_raw_echo(service, caller):
    seed(service, {})
    raw = "{broken-json xapp-FAKE-UNREADABLE"
    service.registry._db.execute(
        "UPDATE agent_tokens SET settings=? WHERE agent_name=? AND platform=?",
        (raw, "tenant", "slack"),
    )
    service.registry._db.commit()
    response = request(service, "GET", "/system/all-tokens", caller=caller)
    assert response.status_code == 200, response.text
    assert response.json()["tokens"][0]["settings"] == "unreadable"
    assert raw not in response.text
    assert service.registry.list_all_tokens()[0]["settings"] == raw


def test_per_agent_malformed_stored_json_keeps_existing_reader_behavior(service):
    seed(service, {})
    raw = "{broken-json xapp-FAKE-UNREADABLE"
    service.registry._db.execute(
        "UPDATE agent_tokens SET settings=? WHERE agent_name=? AND platform=?",
        (raw, "tenant", "slack"),
    )
    service.registry._db.commit()
    with pytest.raises(json.JSONDecodeError):
        service.registry.list_tokens("tenant")
    client = TestClient(service.app, raise_server_exceptions=False)
    try:
        headers = build_internal_auth_headers(
            service.keys["tenant"], agent_name="tenant", method="GET",
            path="/agents/tenant/tokens",
        )
        response = client.get("/agents/tenant/tokens", headers=headers)
    finally:
        client.close()
    assert response.status_code == 500
    assert "xapp-FAKE-UNREADABLE" not in response.text


def test_internal_slack_settings_and_poller_factory_receive_real_fixture_values(
    service, monkeypatch,
):
    seed(service, {"app_token": APP_TOKEN, "label": "visible"})
    monkeypatch.setattr(service.registry, "get_main_agent", lambda: None)
    monkeypatch.setenv("PINKY_SLACK_SOCKET_MODE", "1")
    adapters = []

    def adapter(token):
        assert token == BOT_TOKEN
        value = Mock()
        adapters.append(value)
        return value

    monkeypatch.setattr("pinky_outreach.slack.SlackAdapter", adapter)
    try:
        with TestClient(service.app):
            assert len(service.started) == 1
            actual = service.started[0]
            assert isinstance(actual, pollers.BrokerSlackPoller)
            assert actual._app_token == APP_TOKEN
            assert actual._adapter is adapters[0]
            assert service.registry.get_raw_token("tenant", "slack") == BOT_TOKEN
            token = service.registry.list_tokens("tenant")[0]
            assert token.settings["app_token"] == APP_TOKEN
            assert token.to_dict()["settings"]["app_token"] == APP_TOKEN
    finally:
        for actual in service.started:
            actual.stop()
        for value in adapters:
            value.close()
