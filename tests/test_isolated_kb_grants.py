"""Owner-controlled wiki grants preserve both isolation boundaries."""

import json
import os
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest

from pinky_daemon import api, isolated_policy
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.routes import kb as kb_routes
from tests.isolated_policy_support import (
    assert_refused,
    auth_headers,
    names,
    protocol,
    shared_service,
    signed,
)
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth
PAGE = {"title": "A shared page", "content": "A useful reference"}
OTHER_WRITES = [
    ("POST", "/kb/ingest", {"title": "A source", "content": "Source text"}),
    ("DELETE", "/kb/wiki/x", None),
    ("PUT", "/kb/raw/source", {"title": "Changed"}),
    ("DELETE", "/kb/raw/source", None),
    ("POST", "/kb/librarian/run", None),
    ("POST", "/kb/reindex", None),
    ("PUT", "/kb/librarian/auto-run", None),
    ("POST", "/kb/auto-ingest", None),
]


def memory_gate(d, name="tenant"):
    if d.skills.get("pinky-memory") is None:
        d.skills.register("pinky-memory", description="Knowledge base tools")
    d.skills.assign_to_agent(name, "pinky-memory")


def stored_grants(d, value, name="tenant", *, raw=False):
    # A database overlay also exercises the unchanged product in the RED run.
    columns = {r[1] for r in d.agents._db.execute("PRAGMA table_info(agents)")}
    if "isolated_grants" not in columns:
        d.agents._db.execute("ALTER TABLE agents ADD COLUMN isolated_grants TEXT DEFAULT '[]'")
    d.agents._db.execute(
        "UPDATE agents SET isolated_grants=? WHERE name=?",
        (value if raw else json.dumps(value), name),
    )
    d.agents._db.commit()


def grant_reader(registry, name="tenant"):
    reader = getattr(isolated_policy, "isolation_grants", None)
    assert callable(reader), "The request-time grant reader is required"
    return reader(registry, name)


def owner_headers():
    return {"cookie": f"{SESSION_COOKIE_NAME}={create_session_cookie(os.environ['PINKY_SESSION_SECRET'])}"}


async def put_page(d, *, actor="tenant", slug="x"):
    path = f"/kb/wiki/{slug}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d.app), base_url="http://test") as c:
        return await c.put(path, headers=signed(d, "PUT", path, actor), json=PAGE)


@pytest.mark.asyncio
async def test_ungranted_wiki_write_is_denied_in_both_layers(daemon, monkeypatch):
    d = daemon()
    memory_gate(d)
    lines = []
    monkeypatch.setattr(api, "_log", lines.append)
    response = await put_page(d)
    assert response.status_code == 403
    assert any("DENY PUT /kb/wiki/{slug:path}" in line and "reason=unlisted" in line for line in lines)
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, "http", auth_headers(d)) as client:
            assert "kb_save_wiki" not in await names(client)
            await assert_refused(client, "kb_save_wiki", {"slug": "x", **PAGE}, service)
            result = await client.call_tool("kb_save_wiki", {"slug": "x", **PAGE})
            assert result.isError and result.content[0].text == "Tool denied by caller policy"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_wiki_writer_grant_allows_only_the_wiki_save(daemon, monkeypatch, transport):
    d = daemon()
    memory_gate(d)
    stored_grants(d, ["kb_writer"])
    lines = []
    monkeypatch.setattr(api, "_log", lines.append)
    response = await put_page(d)
    assert response.status_code == 200, response.text
    assert kb_routes._kb.get_wiki_content("x").endswith(PAGE["content"])
    assert [line for line in lines if "isolation:" in line] == [
        "isolation: GRANT PUT /kb/wiki/{slug:path} for tenant grant=kb_writer mode=enforce"
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d.app), base_url="http://test") as c:
        for method, path, body in OTHER_WRITES:
            response = await c.request(method, path, headers=signed(d, method, path), json=body)
            assert response.status_code == 403, (method, path, response.text)
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, transport, auth_headers(d)) as client:
            advertised = await names(client)
            assert "kb_save_wiki" in advertised
            assert not {"kb_ingest", "kb_run_librarian", "kb_delete_wiki", "kb_delete_raw", "kb_update_raw"} & advertised
            result = await client.call_tool("kb_save_wiki", {"slug": "x", **PAGE})
            assert not result.isError
            assert [(row[0], row[1]) for row in service.outgoing] == [("PUT", "/kb/wiki/x")]


@pytest.mark.asyncio
async def test_shadow_logs_only_ungranted_wiki_requests(daemon, monkeypatch):
    d = daemon(mode="shadow")
    memory_gate(d)
    lines = []
    monkeypatch.setattr(api, "_log", lines.append)
    assert (await put_page(d)).status_code == 200
    assert [line for line in lines if "WOULD DENY" in line] == [
        "isolation: WOULD DENY PUT /kb/wiki/{slug:path} for tenant mode=shadow reason=unlisted"
    ]
    stored_grants(d, ["kb_writer"])
    lines.clear()
    assert (await put_page(d)).status_code == 200
    assert [line for line in lines if "isolation:" in line] == [
        "isolation: GRANT PUT /kb/wiki/{slug:path} for tenant grant=kb_writer mode=shadow"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_error", [False, True])
async def test_wiki_grant_requires_the_registering_gate(daemon, monkeypatch, gate_error):
    d = daemon()
    stored_grants(d, ["kb_writer"])
    assert grant_reader(d.agents) == frozenset({"kb_writer"})
    if gate_error:
        def fail(*args, **kwargs):
            raise RuntimeError("Skill lookup unavailable")
        monkeypatch.setattr(d.skills, "get_agent_skills", fail)
    assert (await put_page(d)).status_code == 403
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, "http", auth_headers(d)) as client:
            assert "kb_save_wiki" not in await names(client)
            await assert_refused(client, "kb_save_wiki", {"slug": "x", **PAGE}, service)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ['["unknown"]', '{', '"kb_writer"', '{"kb_writer":true}', '[7,{},null]'])
async def test_invalid_stored_grants_fail_closed_in_both_layers(daemon, monkeypatch, value):
    d = daemon()
    memory_gate(d)
    stored_grants(d, value, raw=True)
    assert grant_reader(d.agents) == frozenset()
    assert (await put_page(d)).status_code == 403
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, "http", auth_headers(d)) as client:
            assert "kb_save_wiki" not in await names(client)
            await assert_refused(client, "kb_save_wiki", {"slug": "x", **PAGE}, service)


@pytest.mark.asyncio
async def test_grant_read_errors_fail_closed_in_both_layers(daemon, monkeypatch):
    d = daemon()
    memory_gate(d)
    stored_grants(d, ["kb_writer"])
    original = d.agents.get

    def fail(name):
        if name == "tenant":
            raise RuntimeError("Registry unavailable")
        return original(name)

    monkeypatch.setattr(d.agents, "get", fail)
    assert grant_reader(d.agents) == frozenset()
    assert (await put_page(d)).status_code == 403
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, "http", auth_headers(d)) as client:
            assert "kb_save_wiki" not in await names(client)
            await assert_refused(client, "kb_save_wiki", {"slug": "x", **PAGE}, service)


@pytest.mark.parametrize("row", [None, SimpleNamespace(isolated=True), SimpleNamespace(isolated_grants="kb_writer")])
def test_missing_or_nonlist_grant_attributes_are_closed(row):
    registry = SimpleNamespace(get=lambda name: row, _rmw_lock=nullcontext())
    assert grant_reader(registry) == frozenset()


def test_grant_reader_filters_unknown_names_under_the_registry_lock():
    held = []

    class Lock:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *args):
            held.pop()

    def get(name):
        assert held == [True]
        return SimpleNamespace(isolated_grants=["unknown", "kb_writer", 1, ["kb_writer"]])

    registry = SimpleNamespace(get=get, _rmw_lock=Lock())
    assert grant_reader(registry) == frozenset({"kb_writer"})


@pytest.mark.asyncio
async def test_nonisolated_grants_do_not_narrow_existing_access(daemon, monkeypatch):
    d = daemon()
    memory_gate(d, "normal")
    stored_grants(d, ["kb_writer"], "normal")
    assert grant_reader(d.agents, "normal") == frozenset({"kb_writer"})
    lines = []
    monkeypatch.setattr(api, "_log", lines.append)
    assert (await put_page(d, actor="normal")).status_code == 200
    assert not [line for line in lines if "isolation:" in line]
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, "http", auth_headers(d, "normal")) as client:
            assert {"kb_save_wiki", "kb_ingest", "kb_delete_wiki", "restart_daemon"} <= await names(client)


@pytest.mark.asyncio
async def test_grants_are_reread_on_an_established_mcp_connection(daemon, monkeypatch):
    d = daemon()
    memory_gate(d)
    stored_grants(d, [])
    assert grant_reader(d.agents) == frozenset()
    async with shared_service(d, monkeypatch) as service:
        async with protocol(service, "http", auth_headers(d)) as client:
            assert "kb_save_wiki" not in await names(client)
            stored_grants(d, ["kb_writer"])
            assert "kb_save_wiki" in await names(client)
            assert not (await client.call_tool("kb_save_wiki", {"slug": "x", **PAGE})).isError
            stored_grants(d, [])
            assert "kb_save_wiki" not in await names(client)
            await assert_refused(client, "kb_save_wiki", {"slug": "x", **PAGE}, service)


@pytest.mark.asyncio
async def test_owner_sets_and_clears_grants_and_unknown_names_change_nothing(daemon):
    d = daemon()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d.app), base_url="http://test", headers=owner_headers()) as c:
        response = await c.put("/agents/tenant", json={"isolated_grants": ["kb_writer", "kb_writer"]})
        assert response.status_code == 200, response.text
        assert response.json().get("isolated_grants") == ["kb_writer"]
        assert (await c.get("/agents/tenant")).json()["isolated_grants"] == ["kb_writer"]
        before = d.agents.get("tenant").to_dict()
        response = await c.put("/agents/tenant", json={"display_name": "Uncommitted", "isolated_grants": ["kb_writer", "unknown"]})
        assert response.status_code == 400
        assert d.agents.get("tenant").to_dict() == before
        response = await c.put("/agents/tenant", json={"isolated_grants": []})
        assert response.status_code == 200
        assert (await c.get("/agents/tenant")).json()["isolated_grants"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("actor,target", [("normal", "tenant"), ("tenant", "tenant"), ("tenant", "peer")])
async def test_internal_callers_cannot_set_grants_in_any_mode(daemon, mode, actor, target):
    d = daemon(mode=mode)
    path = f"/agents/{target}"
    before = d.agents.get(target).to_dict()
    assert getattr(d.agents.get(target), "isolated_grants", []) == []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=d.app), base_url="http://test") as c:
        response = await c.put(path, headers=signed(d, "PUT", path, actor), json={"display_name": "Uncommitted", "isolated_grants": ["kb_writer"]})
    assert response.status_code == 403, response.text
    if actor == "normal" or mode in {"off", "shadow"}:
        assert response.json() == {"detail": "authenticated owner session required"}
    assert d.agents.get(target).to_dict() == before
    assert getattr(d.agents.get(target), "isolated_grants", []) == []


def test_grants_have_a_dedicated_canonical_setter_and_are_not_generic_updates(daemon):
    d = daemon()
    setter = getattr(d.agents, "set_isolated_grants", None)
    getter = getattr(d.agents, "get_isolated_grants", None)
    assert callable(setter) and callable(getter), "Grants need dedicated storage accessors"
    assert getter("tenant") == []
    setter("tenant", ["kb_writer", "kb_writer"])
    raw = d.agents._db.execute("SELECT isolated_grants FROM agents WHERE name='tenant'").fetchone()[0]
    assert raw == '["kb_writer"]'
    assert getter("tenant") == ["kb_writer"]
    with pytest.raises(ValueError):
        setter("tenant", ["unknown"])
    assert getter("tenant") == ["kb_writer"]
    d.agents.update("tenant", isolated_grants=[])
    d.agents.register("tenant", isolated_grants=[])
    assert getter("tenant") == ["kb_writer"]
    new = d.agents.register("fresh", working_dir=str(d.root / "fresh"), isolated_grants=["kb_writer"])
    assert new.isolated_grants == []
    setter("tenant", [])
    assert getter("tenant") == []


def test_migration_adds_default_empty_grants_to_existing_agents(daemon):
    d = daemon()
    columns = {r[1] for r in d.agents._db.execute("PRAGMA table_info(agents)")}
    if "isolated_grants" in columns:
        d.agents._db.execute("ALTER TABLE agents DROP COLUMN isolated_grants")
    d.agents._migrate()
    columns = {r[1]: r for r in d.agents._db.execute("PRAGMA table_info(agents)")}
    assert "isolated_grants" in columns
    assert columns["isolated_grants"][2:5] == ("TEXT", 1, "'[]'")
    assert d.agents.get("tenant").isolated_grants == []

