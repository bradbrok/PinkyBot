"""Signed read enforcement denies broad answers and unassigned skill text."""

import pytest
from fastapi.testclient import TestClient

from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth


@pytest.fixture
def enforced(daemon, monkeypatch):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    return instance


@pytest.fixture
def client(enforced):
    instance = TestClient(enforced.app)
    try:
        yield instance
    finally:
        instance.close()


def test_enforce_restricts_roster_to_caller(enforced, client):
    response = client.get("/agents", headers=signed(enforced, "GET", "/agents"))
    assert response.status_code == 200
    assert {row["name"] for row in response.json()["agents"]} == {"tenant"}


def test_enforce_refuses_unlisted_system_read(enforced, client):
    path = "/system/primary-user"
    response = client.get(path, headers=signed(enforced, "GET", path))
    assert response.status_code == 403
    assert response.json() == {
        "error": "isolated agent 'tenant' cannot read this route", "agent": "tenant",
    }


def test_enforce_hides_unassigned_skill(enforced, client):
    name = "unassigned-fixture"
    path = f"/skills/{name}"
    missing = client.get(path, headers=signed(enforced, "GET", path))
    enforced.skills.register(name, directive="Private skill fixture text")
    response = client.get(path, headers=signed(enforced, "GET", path))
    assert missing.status_code == 404
    assert response.status_code == 404
    assert response.json() == missing.json()


REFUSED_ROUTES = [
    "/system/primary-user", "/settings/default-provider", "/comms/messages", "/audit",
    "/sessions", "/conversations", "/apps", "/presentations", "/research",
    "/analytics/overview", "/user-profiles", "/triggers", "/kb/stats",
]


@pytest.fixture
def read_logs(monkeypatch):
    from pinky_daemon import api, isolated_policy

    lines = []
    monkeypatch.setattr(api, "_log", lambda text, **kwargs: lines.append(str(text)))
    monkeypatch.setattr(isolated_policy, "_read_policy_warning_emitted", False)
    return lines


def _read(instance, path, *, method="GET", name="tenant", root_path=""):
    client = TestClient(instance.app, root_path=root_path, follow_redirects=False)
    try:
        headers = signed(instance, method, path.split("?", 1)[0], name)
        return client.request(method, "http://testserver" + path, headers=headers)
    finally:
        client.close()


def _route_probe(instance, path):
    from fastapi.routing import APIRoute

    assert any(
        route.path == path and "GET" in (getattr(route, "methods", ()) or ())
        for route in instance.app.routes
    ), path
    hits = []

    async def endpoint():
        hits.append(True)
        return {"ok": True}

    instance.app.router.routes.insert(0, APIRoute(path, endpoint, methods=["GET", "HEAD"]))
    return hits


def test_read_tables_are_exact():
    from pinky_daemon import isolated_policy as policy

    assert policy.ISOLATED_READ_ALLOW == frozenset(("GET", route) for route in [
        "/agents", "/settings/owner-profile", "/settings/main-agent", "/skills/{name}",
        "/api/voice/requests", "/api/voice/sessions", "/scheduler/wake-ledger",
        "/schedules", "/system/health",
    ])
    assert policy.granted_read_allow(["kb_reader"]) == frozenset({
        ("GET", "/kb/search"), ("GET", "/kb/wiki/{slug:path}"), ("GET", "/kb/stats"),
    })
    assert policy.granted_read_allow(["fleet_roster"]) == frozenset({("GET", "/agents")})
    assert policy.granted_read_allow(["kb_writer", "unknown"]) == frozenset()
    assert not hasattr(policy, "ISOLATED_READ_PENDING")


@pytest.mark.parametrize("path", REFUSED_ROUTES)
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_refused_families_follow_the_read_mode(daemon, monkeypatch, read_logs, path, mode):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    hits = _route_probe(instance, path)
    read_logs.clear()
    response = _read(instance, path)
    assert response.status_code == (403 if mode == "enforce" else 200)
    assert hits == ([] if mode == "enforce" else [True])
    lines = [line for line in read_logs if " READ " in line]
    action = "DENY" if mode == "enforce" else "WOULD DENY"
    assert lines == ([] if mode == "off" else [
        f"isolation: {action} READ GET {path} for tenant reason=unlisted repeats=0"
    ])


@pytest.mark.parametrize("write", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("read", [None, "", "off", "shadow", "enforce", "invalid"])
def test_read_mode_matrix_never_enforces_by_default(monkeypatch, read_logs, write, read):
    from pinky_daemon import isolated_policy

    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", write)
    if read is None:
        monkeypatch.delenv("PINKY_ISOLATED_READ_POLICY_MODE", raising=False)
    else:
        monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", read)
    expected = read if read in {"off", "shadow", "enforce"} else (
        "shadow" if read == "invalid" or write != "off" else "off"
    )
    assert isolated_policy.read_policy_mode(read_logs.append) == expected
    assert isolated_policy.read_policy_mode(read_logs.append) == expected
    assert len(read_logs) == (1 if read == "invalid" else 0)


@pytest.mark.parametrize("path", [
    "/system/primary-user", "/system/primary-user/", "/system//primary-user",
    "/%73ystem/primary-user",
])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_path_spellings_cannot_bypass_a_read_refusal(enforced, read_logs, path, method):
    read_logs.clear()
    response = _read(enforced, path, method=method)
    assert response.status_code == 403
    assert [line for line in read_logs if " READ " in line] == [
        f"isolation: DENY READ {method} /system/primary-user for tenant "
        "reason=unlisted repeats=0"
    ]


@pytest.mark.parametrize("path", ["/system/health", "/system/health/", "/system//health"])
def test_allowed_head_follows_the_get_decision(enforced, read_logs, path):
    read_logs.clear()
    response = _read(enforced, path, method="HEAD")
    assert response.status_code in {307, 404, 405}
    assert not any(" READ " in line for line in read_logs)


@pytest.mark.parametrize("root", ["/proxy", "/agents/tenant/proxy"])
def test_mount_prefix_is_not_a_self_rule_bypass(enforced, root):
    response = _read(enforced, root + "/system/primary-user", root_path=root)
    assert response.status_code == 403


@pytest.mark.parametrize("state", [None, "corrupt"])
def test_uncertain_flags_refuse_unlisted_reads(enforced, monkeypatch, read_logs, state):
    from types import SimpleNamespace

    original = enforced.agents.get
    row = SimpleNamespace(**{**vars(original("tenant")), "isolated": state})
    monkeypatch.setattr(
        enforced.agents, "get", lambda name: row if name == "tenant" else original(name),
    )
    read_logs.clear()
    response = _read(enforced, "/system/primary-user")
    assert response.status_code == 403
    assert "reason=registry unavailable" in read_logs[-1]


def test_unknown_route_is_refused_without_logging_values(enforced, read_logs):
    read_logs.clear()
    response = _read(enforced, "/missing/private-path?token=private-query")
    assert response.status_code == 403
    assert read_logs == [
        "isolation: DENY READ GET <unknown> for tenant reason=unlisted repeats=0"
    ]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_unlisted_reads_do_not_restrict_nonisolated_callers(daemon, monkeypatch, read_logs, mode):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    _route_probe(instance, "/system/primary-user")
    read_logs.clear()
    assert _read(instance, "/system/primary-user", name="normal").status_code == 200
    assert not any(" READ " in line for line in read_logs)


def test_read_grant_adds_only_its_routes_and_revokes_immediately(enforced):
    assert _read(enforced, "/kb/stats").status_code == 403
    enforced.agents.set_isolated_grants("tenant", ["kb_reader"])
    for path in ["/kb/stats", "/kb/search?q=fixture", "/kb/wiki/missing-page"]:
        assert _read(enforced, path).status_code in {200, 404}
    assert _read(enforced, "/research").status_code == 403
    assert _read(enforced, "/kb/raw").status_code == 403
    enforced.agents.set_isolated_grants("tenant", [])
    assert _read(enforced, "/kb/stats").status_code == 403


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_deny_log_window_repeats_and_map_limit(enforced, monkeypatch, read_logs, method):
    from pinky_daemon import api
    from tests.test_isolated_read_shadow import _request, _shadow_check

    check = _shadow_check(enforced)
    now = [100.0]
    monkeypatch.setattr(api, "_isolated_read_now", lambda: now[0])
    monkeypatch.setattr(api, "resolve_mutation_route", lambda app, scope: (scope["path"], {}))
    read_logs.clear()
    for _ in range(3):
        assert check(_request(enforced, "/counter/0", method), "tenant") is True
    assert len(read_logs) == 1 and read_logs[0].endswith("repeats=0")
    now[0] = 699.0
    assert check(_request(enforced, "/counter/0", method), "tenant") is True
    assert len(read_logs) == 1
    now[0] = 700.0
    assert check(_request(enforced, "/counter/0", method), "tenant") is True
    assert len(read_logs) == 2 and read_logs[-1].endswith("repeats=3")
    for index in range(1, 1024):
        assert check(_request(enforced, f"/counter/{index}", method), "tenant") is True
    assert not any("map reset" in line for line in read_logs)
    assert check(_request(enforced, "/counter/1024", method), "tenant") is True
    assert sum("map reset" in line for line in read_logs) == 1
    assert check(_request(enforced, "/counter/0", method), "tenant") is True
    assert read_logs[-1].endswith("repeats=0")


def test_logging_failure_does_not_disable_enforcement(enforced, monkeypatch):
    from pinky_daemon import api

    def unavailable(*args, **kwargs):
        raise RuntimeError("private-log-marker")

    monkeypatch.setattr(api, "_log", unavailable)
    assert _read(enforced, "/system/primary-user").status_code == 403


@pytest.mark.parametrize("path", [
    "/calendar/google/auth-url", "/calendar/google/fetch-token", "/calendar/status",
    "/system/auth",
])
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_side_effect_reads_decide_before_the_handler(
    daemon, monkeypatch, read_logs, tmp_path, path, mode,
):
    from fastapi.routing import APIRoute

    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    _route_probe(instance, path)
    marker = tmp_path / "handler-write"
    observed = []

    async def endpoint():
        observed.extend(line for line in read_logs if " READ " in line)
        assert not marker.exists()
        marker.write_text("handler")
        return {"ok": True}

    instance.app.router.routes.insert(0, APIRoute(path, endpoint, methods=["GET"]))
    read_logs.clear()
    response = _read(instance, path)
    assert response.status_code == (403 if mode == "enforce" else 200)
    assert marker.exists() is (mode == "shadow")
    if mode == "shadow":
        assert observed == [
            f"isolation: WOULD DENY READ GET {path} for tenant reason=unlisted repeats=0"
        ]


@pytest.mark.parametrize("grant", ["kb_reader", "fleet_roster"])
@pytest.mark.parametrize("actor", ["tenant", "normal"])
def test_only_browser_owner_can_set_the_new_grants(enforced, grant, actor):
    import os

    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

    client = TestClient(enforced.app)
    try:
        path = "/agents/tenant"
        response = client.put(
            path, headers=signed(enforced, "PUT", path, actor), json={"isolated_grants": [grant]},
        )
        assert response.status_code == 403
        assert enforced.agents.get_isolated_grants("tenant") == []
        client.cookies.set(
            SESSION_COOKIE_NAME, create_session_cookie(os.environ["PINKY_SESSION_SECRET"]),
        )
        response = client.put(path, json={"isolated_grants": [grant]})
        assert response.status_code == 200
        assert enforced.agents.get_isolated_grants("tenant") == [grant]
    finally:
        client.close()


@pytest.mark.parametrize("path,status", [
    ("/skills/assigned-fixture", 200), ("/skills/assigned-fixture/", 307),
    ("/skills//assigned-fixture", 404), ("/skills/%61ssigned-fixture", 200),
])
def test_parameter_read_spellings_use_the_same_allowed_template(enforced, read_logs, path, status):
    enforced.skills.register("assigned-fixture", directive="Assigned text")
    enforced.skills.assign_to_agent("tenant", "assigned-fixture")
    read_logs.clear()
    assert _read(enforced, path).status_code == status
    assert not any(" READ " in line for line in read_logs)


@pytest.mark.parametrize("write_mode", ["off", "shadow", "enforce"])
def test_explicit_read_enforcement_is_independent_of_write_mode(daemon, monkeypatch, write_mode):
    instance = daemon(write_mode)
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert _read(instance, "/system/primary-user").status_code == 403
