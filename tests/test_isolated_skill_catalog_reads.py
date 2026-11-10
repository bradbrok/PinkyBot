"""Catalog discovery and skill reads respect the signed caller's read boundary."""

import pytest

from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_read_enforce import _read

pytestmark = pytest.mark.real_auth


def test_available_catalog_hides_unassigned_skill_bodies(daemon, monkeypatch):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    marker = "private-catalog-directive-fixture"
    instance.skills.register("catalog-private", directive=marker, origin_agent="peer")
    instance.skills.assign_to_agent("peer", "catalog-private")
    named = _read(instance, "/skills/catalog-private")
    available = _read(instance, "/agents/tenant/skills/available")
    assert named.status_code == 404
    assert available.status_code == 200
    assert marker not in available.text


def test_shared_skill_loads_without_an_explicit_assignment(daemon, monkeypatch):
    instance = daemon()
    instance.skills.register("catalog-shared", directive="Shared fixture text", shared=True)
    assert instance.skills.is_assigned("tenant", "catalog-shared")
    assert "tenant" not in instance.skills.assigned_agents("catalog-shared")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    listing = _read(instance, "/agents/tenant/skills")
    response = _read(instance, "/skills/catalog-shared")
    assert any(row["name"] == "catalog-shared" for row in listing.json()["skills"])
    assert response.status_code == 200
    assert response.json()["directive"] == "Shared fixture text"


@pytest.fixture
def skill_logs(monkeypatch):
    from pinky_daemon import api

    lines = []
    monkeypatch.setattr(api, "_log", lambda text, **kwargs: lines.append(str(text)))
    return lines


def _assert_skill_read(instance, monkeypatch, lines, name, mode, readable):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    lines.clear()
    response = _read(instance, "/skills/" + name)
    assert response.status_code == (404 if mode == "enforce" and not readable else 200)
    if response.status_code == 404:
        assert response.json() == {"detail": f"Skill '{name}' not found"}
    else:
        assert response.json()["directive"] == "Skill body fixture"
    assert [line for line in lines if " READ " in line] == ([
        "isolation: WOULD DENY READ GET /skills/{name} for tenant "
        "reason=unassigned-skill repeats=0"
    ] if mode == "shadow" and not readable else [])


def test_shared_skill_has_no_unassigned_shadow_line(daemon, monkeypatch, skill_logs):
    instance = daemon()
    instance.skills.register("catalog-shared", directive="Skill body fixture", shared=True)
    _assert_skill_read(instance, monkeypatch, skill_logs, "catalog-shared", "shadow", True)


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
def test_shared_skill_loading_opt_out_remains_readable(daemon, monkeypatch, skill_logs, mode):
    instance = daemon()
    name = "catalog-shared-opt-out"
    instance.skills.register(name, directive="Skill body fixture", shared=True)
    instance.skills.assign_to_agent("tenant", name, enabled=False)
    assert instance.skills.is_assigned("tenant", name)
    assert not any(row["name"] == name for row in instance.skills.get_agent_skills("tenant"))
    disabled = next(row for row in instance.skills.get_agent_skills("tenant", enabled_only=False)
                    if row["name"] == name)
    assert disabled["agent_enabled"] is False and disabled["effective_enabled"] is False
    _assert_skill_read(instance, monkeypatch, skill_logs, name, mode, True)


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
def test_disabled_private_assignment_is_not_readable(daemon, monkeypatch, skill_logs, mode):
    instance = daemon()
    name = "catalog-disabled-private"
    instance.skills.register(name, directive="Skill body fixture")
    instance.skills.assign_to_agent("tenant", name, enabled=False)
    assert "tenant" in instance.skills.assigned_agents(name)
    assert not instance.skills.is_assigned("tenant", name)
    _assert_skill_read(instance, monkeypatch, skill_logs, name, mode, False)


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
def test_globally_disabled_shared_skill_is_not_readable(daemon, monkeypatch, skill_logs, mode):
    instance = daemon()
    name = "catalog-disabled-shared"
    instance.skills.register(name, directive="Skill body fixture", shared=True, enabled=False)
    assert "tenant" not in instance.skills.assigned_agents(name)
    assert not instance.skills.is_assigned("tenant", name)
    _assert_skill_read(instance, monkeypatch, skill_logs, name, mode, False)


@pytest.fixture
def catalog(daemon):
    instance = daemon()
    instance.skills.register(
        "catalog-option", description="Catalog option fixture", directive="private-option-body",
        self_assignable=True, category="fixtures", requires=["dependency-fixture"],
    )
    instance.skills.register(
        "catalog-private", description="Private fixture", directive="private-catalog-body",
        self_assignable=False, category="fixtures",
    )
    instance.skills.register("catalog-equipped", self_assignable=True, category="fixtures")
    instance.skills.assign_to_agent("tenant", "catalog-equipped")
    instance.skills.register(
        "catalog-shared", shared=True, self_assignable=True, category="fixtures",
    )
    return instance


@pytest.mark.parametrize("query", [
    "", "&self_assignable=false", "&self_assignable=true",
    "&self_assignable=false&agent_name=peer&assigned_agent=peer&agent=peer&created_by=peer",
])
def test_available_catalog_query_cannot_widen_the_projection(catalog, monkeypatch, query):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(catalog, "/agents/tenant/skills/available?category=fixtures" + query)
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "tenant" and body["count"] == 1
    assert body["skills"] == [{
        "name": "catalog-option", "description": "Catalog option fixture",
        "category": "fixtures", "self_assignable": True,
    }]
    assert _read(catalog, "/agents/tenant/skills/available?category=missing").json()["skills"] == []


@pytest.mark.parametrize("field,value", [
    ("directive", "private-field-fixture"),
    ("config", {"setting": "private-field-fixture"}),
    ("mcp_server_config", {"fixture": {"command": "private-field-fixture"}}),
    ("file_templates", {"fixture.txt": "private-field-fixture"}),
    ("default_config", {"setting": "private-field-fixture"}),
    ("requires", ["private-field-fixture"]),
])
def test_available_catalog_drops_each_private_field_family(daemon, monkeypatch, field, value):
    instance = daemon()
    skill = instance.skills.register(
        "catalog-field", description="Field fixture", self_assignable=True,
        privileged_tool_opt_in=True, category="fixtures", **{field: value},
    )
    assert skill.self_assignable
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(instance, "/agents/tenant/skills/available?category=fixtures")
    assert response.status_code == 200
    assert response.json()["skills"] == [{
        "name": "catalog-field", "description": "Field fixture",
        "category": "fixtures", "self_assignable": True,
    }]
    assert "private-field-fixture" not in response.text


def _read_as(instance, path, actor):
    if actor != "browser":
        return _read(instance, path, name=actor)
    import os

    from fastapi.testclient import TestClient

    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

    client = TestClient(instance.app)
    client.cookies.set(
        SESSION_COOKIE_NAME, create_session_cookie(os.environ["PINKY_SESSION_SECRET"]),
    )
    try:
        return client.get(path)
    finally:
        client.close()


@pytest.mark.parametrize("actor,mode", [
    ("tenant", "off"), ("tenant", "shadow"),
    ("normal", "off"), ("normal", "shadow"), ("normal", "enforce"),
    ("browser", "off"), ("browser", "shadow"), ("browser", "enforce"),
])
def test_available_catalog_full_response_controls(catalog, monkeypatch, skill_logs, actor, mode):
    path = f"/agents/{'tenant' if actor == 'browser' else actor}/skills/available?category=fixtures"
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "off")
    before = _read_as(catalog, path, actor)
    assert before.status_code == 200 and "private-catalog-body" in before.text
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    skill_logs.clear()
    after = _read_as(catalog, path, actor)
    assert after.status_code == before.status_code
    assert after.content == before.content
    assert not [line for line in skill_logs if " READ " in line]


@pytest.mark.parametrize("component", ["registry-missing", "registry-error", "store-error"])
def test_available_catalog_dependency_failure_never_returns_the_wide_answer(
    catalog, monkeypatch, component,
):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")

    def unavailable(*args, **kwargs):
        raise RuntimeError("private-catalog-error")

    if component == "registry-missing":
        monkeypatch.setattr(catalog.agents, "get", lambda name: None)
    elif component == "registry-error":
        monkeypatch.setattr(catalog.agents, "get", unavailable)
    else:
        monkeypatch.setattr(catalog.skills, "get_available_skills", unavailable)
    response = _read(catalog, "/agents/tenant/skills/available?category=fixtures")
    assert response.status_code == 200
    assert response.json() == {"agent": "tenant", "skills": [], "count": 0}


@pytest.mark.parametrize("caller", ["tenant", "normal"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_available_skill_tool_renders_the_projected_rows(catalog, monkeypatch, caller, mode):
    import io
    import urllib.error
    import urllib.request

    from fastapi.testclient import TestClient

    from pinky_self.server import create_server

    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    client = TestClient(catalog.app)
    calls = []

    def urlopen(request, *args, **kwargs):
        calls.append((request.get_method(), request.selector))
        response = client.request(
            request.get_method(), request.selector, headers=dict(request.header_items()),
            content=request.data,
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, "fixture refusal", {},
                io.BytesIO(response.content),
            )
        return io.BytesIO(response.content)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    server = create_server(
        agent_name=caller, api_url="http://fixture", tool_gates=["skill-admin"],
        signing_key_resolver=catalog.agents.get_signing_key,
    )
    tool = next(tool for tool in server._tool_manager.list_tools()
                if tool.name == "list_available_skills")
    try:
        result = tool.fn(category="fixtures")
    finally:
        client.close()
    assert calls == [("GET", f"/agents/{caller}/skills/available?self_assignable=true&category=fixtures")]
    assert "**catalog-option**" in result and "Catalog option fixture" in result
    assert "[fixtures]" in result and "Failed" not in result
    assert ("Requires: dependency-fixture" in result) is (caller != "tenant" or mode != "enforce")
