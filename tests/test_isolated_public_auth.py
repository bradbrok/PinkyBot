"""Public callback authentication and request-state precedence matrix."""

import time

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from pinky_daemon.auth import (
    INTERNAL_AGENT_HEADER,
    INTERNAL_SIGNATURE_HEADER,
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from tests.conftest import TEST_SESSION_SECRET
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import replace_cell, signed

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("signature", ["absent", "malformed", "expired", "wrong-agent", "valid"])
@pytest.mark.parametrize("cookie", ["absent", "invalid", "valid"])
@pytest.mark.parametrize("provider", [False, True], ids=["bad-provider", "valid-provider"])
@pytest.mark.parametrize("actor", ["tenant", "normal"])
def test_public_callback_auth_and_state_matrix(
    daemon,
    monkeypatch,
    mode,
    signature,
    cookie,
    provider,
    actor,
):
    # The provider leg needs the optional voice extra; CI installs only the dev extras.
    validator = pytest.importorskip("twilio.request_validator")
    d = daemon(mode)
    d.agents.set_setting("TWILIO_AUTH_TOKEN", "fixture-provider-token")
    path = "/api/voice/status/fixture-call"
    body = {"CallStatus": "completed", "CallSid": "fixture-call"}
    headers = {}
    if signature != "absent":
        headers = signed(d, "POST", path, actor)
        if signature == "malformed":
            headers[INTERNAL_SIGNATURE_HEADER] = "invalid"
        elif signature == "expired":
            headers = build_internal_auth_headers(
                d.agents.get_signing_key(actor),
                agent_name=actor,
                method="POST",
                path=path,
                timestamp=int(time.time()) - 86400,
            )
        elif signature == "wrong-agent":
            headers[INTERNAL_AGENT_HEADER] = "peer"
    headers["X-Twilio-Signature"] = (
        validator.RequestValidator("fixture-provider-token").compute_signature(
            "http://testserver" + path, body
        )
        if provider
        else "invalid"
    )
    visited = []
    route = next(
        r for r in d.app.routes if getattr(r, "path", "") == "/api/voice/status/{call_sid}"
    )
    original = route.dependant.call

    async def observe(*args, **kwargs):
        request = kwargs["request"]
        visited.append(
            {
                key: getattr(request.state, key, None)
                for key in ("auth_gate", "auth_user", "internal_caller")
            }
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(route.dependant, "call", observe)
    client = TestClient(d.app)
    if cookie != "absent":
        client.cookies.set(
            SESSION_COOKIE_NAME,
            create_session_cookie(TEST_SESSION_SECRET) if cookie == "valid" else "invalid",
        )
    response = client.post(path, data=body, headers=headers)
    client.close()
    must_deny = mode == "enforce" and signature == "valid" and actor == "tenant"
    expected = 403 if must_deny or not provider else 200
    assert response.status_code == expected, response.text
    if must_deny:
        assert visited == [], "Public handler executed before isolated policy"
    else:
        assert len(visited) == 1
        if mode != "enforce" or signature != "valid":
            assert visited == [
                {"auth_gate": "public", "auth_user": None, "internal_caller": None}
            ], visited


@pytest.mark.parametrize("signature", ["valid", "invalid", "absent"])
def test_protected_cookie_does_not_override_valid_isolated_signature(daemon, signature):
    d = daemon()
    visited = []

    @d.app.post("/protected-state-probe")
    async def endpoint(request: Request):
        visited.append(
            (
                getattr(request.state, "auth_gate", None),
                getattr(request.state, "internal_caller", None),
            )
        )
        return {"ok": True}

    path = "/protected-state-probe"
    headers = signed(d, "POST", path) if signature != "absent" else {}
    if signature == "invalid":
        headers[INTERNAL_SIGNATURE_HEADER] = "invalid"
    client = TestClient(d.app)
    client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
    response = client.post(path, headers=headers)
    client.close()
    if signature == "valid":
        assert (response.status_code, visited) == (403, [])
    else:
        assert (response.status_code, visited) == (200, [("session", None)])


PUBLIC_MUTATIONS = [
    "/auth/login",
    "/auth/logout",
    "/auth/setup",
    "/hooks/fixture-token",
    "/api/voice/twiml/outbound/987654",
    "/api/voice/twiml/inbound",
    "/api/voice/status/fixture",
    "/api/voice/amd/987654",
    "/a/fixture-share",
    "/p/fixture-share/unlock",
]


@pytest.mark.parametrize("path", PUBLIC_MUTATIONS)
def test_each_public_mutation_reaches_policy_before_dispatch(daemon, monkeypatch, path):
    import re

    d = daemon()
    visited = []
    route = next(
        r
        for r in d.app.routes
        if hasattr(r, "dependant") and "POST" in r.methods and re.fullmatch(r.path_regex, path)
    )
    original = route.dependant.call

    async def observe(*args, **kwargs):
        visited.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(route.dependant, "call", observe)
    client = TestClient(d.app)
    response = client.post(path, json={}, headers=signed(d, "POST", path))
    client.close()
    assert (response.status_code, visited) == (403, []), (path, response.status_code, visited)


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_public_signed_missing_policy_row(daemon, monkeypatch, mode):
    d = daemon(mode)
    path = "/auth/logout"
    headers = signed(d, "POST", path)
    original = d.agents.get
    monkeypatch.setattr(d.agents, "get", lambda name: None if name == "tenant" else original(name))
    client = TestClient(d.app)
    response = client.post(path, headers=headers)
    client.close()
    assert response.status_code == (403 if mode == "enforce" else 200)


@pytest.mark.parametrize("mode,expected", [("off", 0), ("shadow", 1), ("enforce", 1)])
def test_present_public_signature_verified_once(daemon, monkeypatch, mode, expected):
    d = daemon(mode)
    dispatch = next(
        m.kwargs["dispatch"]
        for m in d.app.user_middleware
        if "dispatch" in m.kwargs and m.kwargs["dispatch"].__name__ == "auth_middleware"
    )
    import inspect

    original = inspect.getclosurevars(dispatch).nonlocals["_has_valid_internal_auth"]
    checks = []

    def count(request):
        checks.append(True)
        return original(request)

    replace_cell(monkeypatch, dispatch, "_has_valid_internal_auth", count)
    client = TestClient(d.app)
    client.post("/auth/logout", headers=signed(d, "POST", "/auth/logout"))
    client.close()
    assert len(checks) == expected, checks
