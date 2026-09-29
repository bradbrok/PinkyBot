"""Behavioral expectations for isolated route authorization."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.routing import Route

from pinky_daemon import api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from tests.conftest import TEST_SESSION_SECRET
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth

# Existing handlers, harmless inputs. Missing objects/invalid business inputs
# prove that auth passed without invoking deployment or provider side effects.
CASES = [
    ("POST", "/admin/update", {"branch": "invalid"}, {}, 400),
    ("POST", "/admin/channel", {"channel": "invalid"}, {}, 400),
    ("POST", "/admin/force-restart-agent/peer", {}, {}, 404),
    ("PUT", "/system/timezone", {"timezone": "UTC"}, {}, 200),
    ("POST", "/broker/send-animation", {}, {"agent_name": "tenant"}, 400),
    ("DELETE", "/sessions/missing", {}, None, 404),
    ("DELETE", "/projects/987654", {}, None, 404),
    ("DELETE", "/kb/raw/missing", {}, None, 404),
    ("DELETE", "/tasks/987654", {}, None, 404),
    ("DELETE", "/user-profiles/missing", {}, None, 200),
    ("DELETE", "/apps/987654", {}, None, 404),
    ("DELETE", "/presentations/987654", {}, None, 404),
    ("POST", "/research/987654/assign", {}, {"agent_name": "tenant"}, 404),
    ("DELETE", "/outreach/platforms/invalid", {}, None, 404),
    ("PUT", "/calendar/config", {}, {}, 200),
    ("POST", "/auth/logout", {}, {}, 200),
    ("PUT", "/settings/heartbeat/prompt", {}, {"prompt": "fixture"}, 200),
    ("DELETE", "/sprints/987654", {}, None, 404),
    ("POST", "/groups/missing/leave", {}, {}, 422),
    ("DELETE", "/providers/missing", {}, None, 404),
    ("DELETE", "/models/missing", {}, None, 404),
    ("DELETE", "/bot-tokens/missing", {}, None, 404),
    ("DELETE", "/federation/peers/missing/missing", {}, None, 200),
    ("POST", "/api/migrate/openclaw/parse", {}, {}, 422),
    ("POST", "/internal/stores/snapshot", {}, {}, 200),
    ("POST", "/agents", {}, {}, 400),
    ("POST", "/soul-templates/render", {}, {}, 200),
    ("DELETE", "/milestones/987654", {}, None, 404),
    ("DELETE", "/presentation-templates/987654", {}, None, 404),
    ("POST", "/render/pdf", {}, {}, 400),
]


@pytest.mark.parametrize("method,path,params,body,baseline", CASES, ids=[c[1] for c in CASES])
def test_isolated_denied_across_route_families(daemon, method, path, params, body, baseline):
    d = daemon()
    client = TestClient(d.app)
    try:
        response = client.request(
            method, path, params=params, json=body, headers=signed(d, method, path)
        )
    finally:
        client.close()
    assert response.status_code == 403, (path, response.status_code, response.text)


@pytest.mark.parametrize("method,path,params,body,baseline", CASES, ids=[c[1] for c in CASES])
def test_nonisolated_route_results_unchanged(daemon, method, path, params, body, baseline):
    d = daemon()
    # Body actor needs to match the control principal where a guard applies.
    if body and body.get("agent_name") == "tenant":
        body = dict(body, agent_name="normal")
    client = TestClient(d.app)
    try:
        response = client.request(
            method, path, params=params, json=body, headers=signed(d, method, path, "normal")
        )
    finally:
        client.close()
    assert response.status_code == baseline, (path, response.status_code, response.text)


def test_restart_denied_before_task_scheduled(daemon, monkeypatch):
    d = daemon()
    scheduled = []
    original = asyncio.create_task

    def capture(coro, *args, **kwargs):
        if getattr(coro, "__name__", "") == "_delayed_exit":
            scheduled.append("restart")
            coro.close()
            return None
        return original(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", capture)
    client = TestClient(d.app)
    response = client.post("/admin/restart", headers=signed(d, "POST", "/admin/restart"))
    client.close()
    assert (response.status_code, scheduled) == (403, [])


@pytest.mark.parametrize(
    "mode,expected",
    [(None, 200), ("off", 200), ("shadow", 200), ("enforce", 403), ("misspelled", 403)],
)
def test_mode_behavior_and_logs(daemon, monkeypatch, mode, expected):
    logs = []
    monkeypatch.setattr(api, "_log", lambda message, **kw: logs.append(str(message)))
    d = daemon(mode)
    hits = []

    @d.app.post("/new-test-mutation")
    async def sentinel():
        hits.append(True)
        return {"ok": True}

    client = TestClient(d.app)
    response = client.post("/new-test-mutation", headers=signed(d, "POST", "/new-test-mutation"))
    client.close()
    assert (response.status_code, bool(hits)) == (expected, expected == 200)
    if mode == "shadow":
        assert any("WOULD DENY" in log and "tenant" in log for log in logs), logs
    if mode == "misspelled":
        assert any("enforce" in log and ("invalid" in log or "unknown" in log) for log in logs), (
            logs
        )


@pytest.mark.parametrize("cookie", [False, True])
def test_signed_public_mutation_is_not_exempt(daemon, cookie):
    d = daemon()
    client = TestClient(d.app)
    if cookie:
        client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
    response = client.post("/auth/logout", headers=signed(d, "POST", "/auth/logout"))
    client.close()
    assert response.status_code == 403, response.text


def test_unsigned_public_behavior_preserved(daemon):
    d = daemon()
    client = TestClient(d.app)
    response = client.post("/auth/logout")
    client.close()
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/unknown-mutation",
        "/admin//restart",
        "/agents/tenant/status/child",
        "/agents/tenant/status%2Fchild",
    ],
)
def test_unknown_or_unlisted_descendant_denied(daemon, path):
    from urllib.parse import unquote

    d = daemon()
    hits = []

    @d.app.post("/agents/{name}/status/child")
    async def child(name: str):
        hits.append(name)
        return {"ok": True}

    client = TestClient(d.app, follow_redirects=False)
    response = client.post(path, headers=signed(d, "POST", unquote(path)))
    client.close()
    assert (response.status_code, hits) == (403, [])


def test_first_full_unsupported_route_match_is_denied(daemon):
    d = daemon()
    hits = []

    async def unsupported(request):
        hits.append(True)
        return JSONResponse({"wrong_handler": True}, status_code=202)

    d.app.router.routes.insert(0, Route("/agents/{name}/status", unsupported, methods=["POST"]))
    client = TestClient(d.app)
    response = client.post(
        "/agents/tenant/status",
        json={"status": "idle"},
        headers=signed(d, "POST", "/agents/tenant/status"),
    )
    client.close()
    assert (response.status_code, hits) == (403, [])


@pytest.mark.parametrize("failure", ["error", "missing"])
def test_registry_uncertainty_cannot_authorize_mutation(daemon, monkeypatch, failure):
    d = daemon()
    path = "/new-test-registry-mutation"
    headers = signed(d, "POST", path)
    hits = []

    @d.app.post(path)
    async def sentinel():
        hits.append(True)
        return {"ok": True}

    original = d.agents.get

    def lookup(name):
        if name == "tenant":
            if failure == "error":
                raise RuntimeError("fixture registry unavailable")
            return None
        return original(name)

    monkeypatch.setattr(d.agents, "get", lookup)
    client = TestClient(d.app, raise_server_exceptions=False)
    response = client.post(path, headers=headers)
    client.close()
    assert (response.status_code, hits) == (403, [])


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_method_retains_existing_behavior(daemon, method):
    d = daemon()
    d.app.add_api_route("/safe-control", lambda: {"ok": True}, methods=[method])
    client = TestClient(d.app)
    response = client.request(method, "/safe-control", headers=signed(d, method, "/safe-control"))
    client.close()
    assert response.status_code == 200


def test_allowed_method_does_not_allow_another_method(daemon):
    d = daemon()
    hits = []

    @d.app.delete("/agents/{name}/status")
    async def sentinel(name: str):
        hits.append(name)
        return {"ok": True}

    client = TestClient(d.app)
    response = client.delete(
        "/agents/tenant/status", headers=signed(d, "DELETE", "/agents/tenant/status")
    )
    client.close()
    assert (response.status_code, hits) == (403, [])
