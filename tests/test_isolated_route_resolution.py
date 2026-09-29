"""Authorization must follow the framework's first actual dispatch match."""

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from starlette.routing import Mount

from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("first", ["partial", "literal", "overlap", "mount", "same-template"])
def test_dispatch_registration_order(daemon, first):
    d = daemon()
    hits = []

    async def endpoint():
        hits.append(first)
        return {"sentinel": first}

    if first == "partial":
        route = APIRoute("/agents/tenant/status", endpoint, methods=["GET"])
        expected = 200  # skip PARTIAL, reach the real later POST status handler
    elif first == "literal":
        route = APIRoute("/agents/tenant/status", endpoint, methods=["POST"])
        expected = 403
    elif first == "overlap":
        route = APIRoute("/agents/{other_param}/status", endpoint, methods=["POST"])
        expected = 403
    elif first == "same-template":
        route = APIRoute("/agents/{name}/status", endpoint, methods=["POST"])
        expected = 200
    else:

        async def unsupported(scope, receive, send):
            hits.append(first)
            await JSONResponse({"sentinel": first})(scope, receive, send)

        route = Mount("/agents", app=unsupported)
        expected = 403
    d.app.router.routes.insert(0, route)
    path = "/agents/tenant/status"
    client = TestClient(d.app)
    response = client.post(path, headers=signed(d, "POST", path), json={"status": "idle"})
    client.close()
    assert response.status_code == expected, (first, response.text)
    assert hits == ([first] if first == "same-template" else []), hits


@pytest.mark.parametrize(
    "root,target,expected",
    [
        ("/proxy", "tenant", 200),
        ("/agents/peer/proxy", "tenant", 200),
        ("/agents/tenant/proxy", "peer", 403),
    ],
)
def test_root_path_uses_returned_self_parameters(daemon, root, target, expected):
    d = daemon()
    path = root + f"/agents/{target}/status"
    client = TestClient(d.app, root_path=root)
    response = client.post(path, headers=signed(d, "POST", path), json={"status": "idle"})
    client.close()
    assert response.status_code == expected, (response.status_code, response.text)


@pytest.mark.parametrize(
    "raw_path,decoded",
    [
        ("/agents/tenant/status/", "/agents/tenant/status/"),
        ("/agents//tenant/status", "/agents//tenant/status"),
        ("/agents/tenant/status%252Fchild", "/agents/tenant/status%2Fchild"),
    ],
)
def test_noncanonical_unknown_mutations_fail_closed(daemon, raw_path, decoded):
    d = daemon()
    client = TestClient(d.app, follow_redirects=False)
    response = client.post(raw_path, headers=signed(d, "POST", decoded), json={"status": "idle"})
    client.close()
    assert response.status_code == 403, (response.status_code, response.text)


def test_trace_is_a_nonsafe_method(daemon):
    d = daemon()
    hits = []

    @d.app.api_route("/trace-probe", methods=["TRACE"])
    async def sentinel():
        hits.append(True)
        return {"ok": True}

    client = TestClient(d.app)
    response = client.request("TRACE", "/trace-probe", headers=signed(d, "TRACE", "/trace-probe"))
    client.close()
    assert (response.status_code, hits) == (403, [])


def test_resolver_exception_denies_before_dispatch(daemon, monkeypatch):
    d = daemon()
    route = next(
        r
        for r in d.app.routes
        if getattr(r, "path", "") == "/agents/{name}/status" and "POST" in r.methods
    )

    def broken(scope):
        raise RuntimeError("fixture route lookup failure")

    monkeypatch.setattr(route, "matches", broken)
    client = TestClient(d.app, raise_server_exceptions=False)
    response = client.post(
        "/agents/tenant/status",
        headers=signed(d, "POST", "/agents/tenant/status"),
        json={"status": "idle"},
    )
    client.close()
    assert response.status_code == 403, response.status_code


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_head_options_actual_dispatch_preserved(daemon, method):
    d = daemon()
    client = TestClient(d.app)
    response = client.request(
        method, "/agents/tenant/status", headers=signed(d, method, "/agents/tenant/status")
    )
    client.close()
    # This app's GET registration does not add HEAD automatically.
    assert response.status_code == 405, response.text


@pytest.mark.parametrize("configured,expected", [(False, 405), (True, 200)])
def test_cors_options_preflight_keeps_baseline(daemon, monkeypatch, configured, expected):
    if configured:
        monkeypatch.setenv("PINKY_CORS_ORIGINS", "http://localhost:3000")
    d = daemon()
    client = TestClient(d.app)
    response = client.options(
        "/agents/tenant/status",
        headers={
            **signed(d, "OPTIONS", "/agents/tenant/status"),
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    client.close()
    assert response.status_code == expected, response.text
