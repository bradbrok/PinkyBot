"""Registered tool routes and verified-caller task ownership controls."""

import inspect
import io
import json
import urllib.parse
import urllib.request
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from starlette.requests import Request

from pinky_daemon import isolated_policy as policy
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.routes import projects_tasks
from pinky_daemon.shared_mcp import _current_agent, derive_mcp_bearer
from pinky_daemon.shared_mcp_policy import Principal, credential_digest, install_tool_policy
from tests.conftest import TEST_SESSION_SECRET
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth

# Independent literal snapshot: each old grant remains, with exactly five additions.
PREVIOUS_ALLOW = frozenset(
    {
        ("DELETE", "/agents/{agent_name}/pending-schedule-wakes/{pending_id}"),
        ("DELETE", "/agents/{agent_name}/triggers/{trigger_id}"),
        ("PATCH", "/agents/{agent_name}/schedules/{schedule_id}"),
        ("POST", "/agents/{agent_name}/heartbeat"),
        ("POST", "/agents/{agent_name}/schedules"),
        ("POST", "/agents/{agent_name}/triggers/{trigger_id}/test"),
        ("POST", "/agents/{name}/effort-drift"),
        ("POST", "/agents/{name}/mesh/send"),
        ("POST", "/agents/{name}/message"),
        ("POST", "/agents/{name}/policy/evaluate"),
        ("POST", "/agents/{name}/sessions/{session_label}/effort"),
        ("POST", "/agents/{name}/status"),
        ("POST", "/agents/{name}/streaming/restart"),
        ("POST", "/agents/{name}/transport/stop-failure"),
        ("POST", "/agents/{name}/transport/tool-result"),
        ("POST", "/agents/{name}/transport/tool-use"),
        ("POST", "/agents/{name}/transport/transcript-path"),
        ("POST", "/agents/{name}/transport/wake"),
        ("POST", "/api/voice/request"),
        ("POST", "/broker/broadcast"),
        ("POST", "/broker/react"),
        ("POST", "/broker/send"),
        ("POST", "/broker/send-document"),
        ("POST", "/broker/send-gif"),
        ("POST", "/broker/send-photo"),
        ("POST", "/broker/send-video"),
        ("POST", "/broker/send-voice"),
        ("POST", "/broker/thread"),
        ("PUT", "/agents/{agent_name}/context"),
    }
)
ADDED_ALLOW = frozenset(
    {
        ("POST", "/tasks"),
        ("POST", "/tasks/claim/{task_id}"),
        ("POST", "/tasks/complete/{task_id}"),
        ("POST", "/tasks/block/{task_id}"),
        ("DELETE", "/agents/{agent_name}/schedules/{schedule_id}"),
    }
)
ALL_SELF_NAMES = frozenset(
    [
        "add_skill",
        "agent_status",
        "app_url",
        "block_task",
        "bulk_create_tasks",
        "check_for_updates",
        "check_inbox",
        "check_my_health",
        "claim_research_topic",
        "claim_task",
        "complete_task",
        "context_restart",
        "context_status",
        "create_app",
        "create_presentation",
        "create_research_topic",
        "create_skill",
        "create_task",
        "create_trigger",
        "decompose_project",
        "delete_app",
        "delete_trigger",
        "deploy_app",
        "discard_pending_schedule_wake",
        "discover_skills",
        "export_research_pdf",
        "get_agent_card",
        "get_app_source",
        "get_attribution",
        "get_my_research_assignments",
        "get_next_task",
        "get_owner_profile",
        "get_presentation_template",
        "get_research_detail",
        "get_schedule",
        "get_task",
        "install_skill",
        "kb_delete_raw",
        "kb_delete_wiki",
        "kb_get_wiki",
        "kb_ingest",
        "kb_run_librarian",
        "kb_save_wiki",
        "kb_search",
        "kb_stats",
        "kb_update_raw",
        "list_agents",
        "list_apps",
        "list_available_skills",
        "list_call_requests",
        "list_my_schedules",
        "list_my_skills",
        "list_presentations",
        "list_research_topics",
        "list_triggers",
        "list_voice_calls",
        "load_my_context",
        "load_skill",
        "mcp_probe",
        "mesh_remote_send",
        "propose_call",
        "propose_skill",
        "publish_research",
        "register_agent",
        "remove_skill",
        "remove_wake_schedule",
        "render_pdf",
        "restart_daemon",
        "save_my_context",
        "search_history",
        "send_file_to_agent",
        "send_heartbeat",
        "send_to_agent",
        "set_thinking_effort",
        "set_wake_schedule",
        "spawn_clone",
        "submit_research_brief",
        "submit_research_review",
        "test_trigger",
        "update_and_restart",
        "update_app",
        "update_presentation",
        "update_wake_schedule",
        "who_am_i",
    ]
)

SELF_READ_ONLY = frozenset(
    {
        "agent_status",
        "check_inbox",
        "check_my_health",
        "context_status",
        "get_agent_card",
        "get_attribution",
        "get_next_task",
        "get_owner_profile",
        "get_schedule",
        "get_task",
        "kb_get_wiki",
        "kb_search",
        "kb_stats",
        "list_agents",
        "list_call_requests",
        "list_my_schedules",
        "list_my_skills",
        "list_triggers",
        "list_voice_calls",
        "load_my_context",
        "load_skill",
        "mcp_probe",
        "search_history",
    }
)
SELF_MUTATIONS = {
    "block_task": (("POST", "/tasks/block/{task_id}"),),
    "claim_task": (("POST", "/tasks/claim/{task_id}"),),
    "complete_task": (("POST", "/tasks/complete/{task_id}"),),
    "context_restart": (("POST", "/agents/{name}/streaming/restart"),),
    "create_task": (("POST", "/tasks"),),
    "delete_trigger": (("DELETE", "/agents/{agent_name}/triggers/{trigger_id}"),),
    "discard_pending_schedule_wake": (
        ("DELETE", "/agents/{agent_name}/pending-schedule-wakes/{pending_id}"),
    ),
    "mesh_remote_send": (("POST", "/agents/{name}/mesh/send"),),
    "propose_call": (("POST", "/api/voice/request"),),
    "remove_wake_schedule": (("DELETE", "/agents/{agent_name}/schedules/{schedule_id}"),),
    "save_my_context": (("PUT", "/agents/{agent_name}/context"),),
    "send_heartbeat": (("POST", "/agents/{agent_name}/heartbeat"),),
    "send_to_agent": (("POST", "/agents/{name}/message"),),
    "set_thinking_effort": (("POST", "/agents/{name}/sessions/{session_label}/effort"),),
    "set_wake_schedule": (("POST", "/agents/{agent_name}/schedules"),),
    "test_trigger": (("POST", "/agents/{agent_name}/triggers/{trigger_id}/test"),),
    "update_wake_schedule": (("PATCH", "/agents/{agent_name}/schedules/{schedule_id}"),),
}
MESSAGING_MUTATIONS = {
    name: (("POST", "/broker/" + route),)
    for name, route in {
        "broadcast": "broadcast",
        "react": "react",
        "send": "send",
        "thread": "thread",
        "send_document": "send-document",
        "send_gif": "send-gif",
        "send_photo": "send-photo",
        "send_video": "send-video",
        "send_voice": "send-voice",
    }.items()
}
EXPECTED_TABLE = {
    **{("self", name): () for name in SELF_READ_ONLY},
    **{("self", name): routes for name, routes in SELF_MUTATIONS.items()},
    **{("messaging", name): routes for name, routes in MESSAGING_MUTATIONS.items()},
}
HIDDEN_SELF = ALL_SELF_NAMES - SELF_READ_ONLY - SELF_MUTATIONS.keys()
MEMORY_NAMES = frozenset(
    {
        "reflect",
        "recall",
        "introspect",
        "memory_links",
        "memory_query",
        "kg_add",
        "kg_query",
        "kg_invalidate",
        "kg_timeline",
        "kg_connections",
        "kg_what_changed",
        "kg_find_contradictions",
        "kg_sweep_ephemeral",
        "kg_stats",
        "reflect_for",
        "kg_add_for",
        "recall_for",
    }
)
# These are the only bypasses of the usual HTTP seam; the export uses its own seam.
DIRECT_TOOLS = {
    ("self", "check_inbox"): (),
    ("self", "mcp_probe"): (),
    ("self", "get_presentation_template"): (),
    ("self", "export_research_pdf"): (("GET", "/research/{topic_id}/export"),),
    **{("memory", name): () for name in MEMORY_NAMES},
}
EXPECTED_UNRESOLVED = {
    ("self", "send_file_to_agent", "POST", "/agents/caller/file"),
}
OWNERSHIP_ERROR = {"error": "isolated agent may only modify its own tasks"}
SUMMARY = (
    "A substantive completion summary with enough detail to trigger the shared knowledge writer."
)


def _replace_api(monkeypatch, fn, recorder):
    pending = [fn]
    seen = set()
    while pending:
        current = pending.pop()
        if not inspect.isfunction(current) or id(current) in seen:
            continue
        seen.add(id(current))
        cells = dict(zip(current.__code__.co_freevars, current.__closure__ or ()))
        if "_api" in cells:
            monkeypatch.setattr(cells["_api"], "cell_contents", recorder)
            return True
        pending.extend(inspect.getclosurevars(current).nonlocals.values())
    return False


def _arguments(fn):
    result = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        if parameter.default is not inspect.Parameter.empty:
            result[name] = parameter.default
        elif "int" in str(parameter.annotation):
            result[name] = 1
        elif "dict" in str(parameter.annotation):
            result[name] = [{"title": "Fixture"}] if "list" in str(parameter.annotation) else {}
        else:
            result[name] = "fixture"
    overrides = {
        "name": "caller",
        "to": "caller",
        "to_agent": "caller",
        "target_agent": "caller",
        "level": "high",
        "cron": "0 1 * * *",
        "text": "fixture",
        "content": "fixture",
        "description": "fixture",
        "html_content": "<p>fixture</p>",
        "trigger_type": "url",
        "since": "2020-01-01T00:00:00",
        "url": "https://fixture.invalid/source",
        "title": "Fixture",
        "skills": ["fixture"],
        "auto_install": True,
    }
    result.update({name: value for name, value in overrides.items() if name in result})
    return result


@pytest.fixture
def tool_capture(tmp_path, monkeypatch):
    from pinky_memory.embeddings import NoOpEmbeddingClient
    from pinky_memory.server import create_server as memory_server
    from pinky_memory.store import ReflectionStore
    from pinky_messaging.server import create_server as messaging_server
    from pinky_self import server as self_server

    calls = []

    def record(method, path, body=None):
        calls.append((method, urllib.parse.urlsplit(path).path))
        if path.endswith("/health"):
            return {"agent": "caller", "recommendation": "ok", "tasks": {}}
        return {
            "id": 1,
            "title": "Fixture",
            "name": "caller",
            "status": "pending",
            "priority": "normal",
            "trigger_type": "url",
            "tasks": [],
            "agents": [],
            "task": {"id": 1, "title": "Fixture", "status": "pending"},
            "topic": {"id": 1, "title": "Fixture", "status": "pending"},
            "accepted": True,
            "restart_scheduled": True,
            "success": True,
            "agent_name": "caller",
            "health_score": 100,
            "overall_status": "ok",
            "checks": {},
            "issues": [],
            "slug": "fixture",
            "version": 1,
            "share_token": "fixture",
            "current_version": 1,
            "count": 0,
        }

    class Response(io.BytesIO):
        headers = {"content-disposition": 'attachment; filename="fixture.pdf"'}

    def export(request, *args, **kwargs):
        calls.append((request.method, urllib.parse.urlsplit(request.full_url).path))
        return Response(b"fixture PDF")

    monkeypatch.setattr(urllib.request, "urlopen", export)
    monkeypatch.setattr(self_server, "__file__", str(tmp_path / "src/pinky_self/server.py"))
    monkeypatch.setattr(self_server, "record_probe_success", lambda *args: {})
    store = ReflectionStore(str(tmp_path / "fixture-memory.db"))
    servers = {
        "self": self_server.create_server(
            agent_name="caller",
            tool_gates=policy.ALL_TOOL_GATES,
            signing_key_resolver=lambda name: "fixture-key",
        ),
        "messaging": messaging_server(
            agent_name="caller",
            signing_key_resolver=lambda name: "fixture-key",
        ),
        "memory": memory_server(
            store=store,
            embedder=NoOpEmbeddingClient(),
            store_factory=lambda name: store,
            cross_agent_authorizer=lambda name: True,
        ),
    }
    for mount, server in servers.items():
        for tool in server._tool_manager.list_tools():
            if not _replace_api(monkeypatch, tool.fn, record):
                assert (mount, tool.name) in DIRECT_TOOLS, (mount, tool.name)
    yield SimpleNamespace(servers=servers, calls=calls)
    store.close()


async def _capture_all(capture):
    observed = {}
    errors = []
    token = _current_agent.set("caller")
    try:
        for mount, server in capture.servers.items():
            for tool in server._tool_manager.list_tools():
                capture.calls.clear()
                try:
                    value = tool.fn(**_arguments(tool.fn))
                    if inspect.isawaitable(value):
                        value = await value
                    assert value is not None, (mount, tool.name)
                except Exception as error:
                    errors.append((mount, tool.name, type(error).__name__, str(error)))
                observed[mount, tool.name] = tuple(capture.calls)
    finally:
        _current_agent.reset(token)
    print("capture-walk " + json.dumps({f"{m}/{n}": c for (m, n), c in observed.items()}))
    assert not errors, errors
    return observed


def _scope(method, path, app=None):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "app": app,
    }


def _mutation_denied(app):
    pending = [middleware.kwargs.get("dispatch") for middleware in app.user_middleware]
    seen = set()
    while pending:
        fn = pending.pop()
        if not inspect.isfunction(fn) or id(fn) in seen:
            continue
        seen.add(id(fn))
        if fn.__name__ == "_isolated_mutation_denied":
            return fn
        pending.extend(inspect.getclosurevars(fn).nonlocals.values())
    raise AssertionError("Mutation authorization middleware was not found")


async def _dispatch(server, name=None, arguments=None):
    scope = _scope("POST", "/mcp/self/http")
    scope["pinky.principal"] = Principal(
        "caller",
        True,
        credential_digest(derive_mcp_bearer("fixture-key")),
    )
    token = request_ctx.set(
        RequestContext(
            request_id="fixture",
            meta=None,
            session=None,
            lifespan_context={},
            request=Request(scope),
        )
    )
    try:
        if name is None:
            result = await server._mcp_server.request_handlers[types.ListToolsRequest](
                types.ListToolsRequest(method="tools/list"),
            )
        else:
            result = await server._mcp_server.request_handlers[types.CallToolRequest](
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(
                        name=name,
                        arguments=arguments or {},
                    ),
                ),
            )
        return result.root
    finally:
        request_ctx.reset(token)


async def test_capture_walk_classifies_every_registered_tool(daemon, tool_capture):
    d = daemon()
    observed = await _capture_all(tool_capture)
    assert {n for m, n in observed if m == "self"} == ALL_SELF_NAMES
    assert {n for m, n in observed if m == "messaging"} == MESSAGING_MUTATIONS.keys()
    assert {n for m, n in observed if m == "memory"} == MEMORY_NAMES
    actual_table = getattr(policy, "ISOLATED_TOOL_ROUTES", {})
    assert actual_table == EXPECTED_TABLE
    unresolved = set()
    for (mount, name), calls in observed.items():
        mutations = set()
        resolved = set()
        for method, path in calls:
            template, _ = policy.resolve_mutation_route(d.app, _scope(method, path))
            if template is None:
                unresolved.add((mount, name, method, path))
                assert (method, path) not in policy.ISOLATED_MUTATION_ALLOW
                continue
            resolved.add((method, template))
            if method not in {"GET", "HEAD", "OPTIONS"}:
                mutations.add((method, template))
        if (mount, name) in actual_table:
            assert mutations == set(actual_table[mount, name]), (mount, name, mutations)
        else:
            assert (mount == "self" and name in HIDDEN_SELF) or mount == "memory"
        if (mount, name) in DIRECT_TOOLS:
            assert resolved == set(DIRECT_TOOLS[mount, name])
    assert unresolved == EXPECTED_UNRESOLVED
    assert all(not calls for (mount, _), calls in observed.items() if mount == "memory")


def test_allow_set_has_only_the_five_authorized_additions():
    assert policy.ISOLATED_MUTATION_ALLOW == PREVIOUS_ALLOW | ADDED_ALLOW
    table = getattr(policy, "ISOLATED_TOOL_ROUTES", {})
    assert table == EXPECTED_TABLE
    non_tool = getattr(policy, "ISOLATED_NON_TOOL_ROUTES", frozenset())
    assert non_tool == {
        pair
        for pair in PREVIOUS_ALLOW
        if pair[1].endswith(
            (
                "/effort-drift",
                "/transport/wake",
                "/transport/transcript-path",
                "/transport/tool-use",
                "/transport/tool-result",
                "/transport/stop-failure",
                "/status",
                "/policy/evaluate",
            )
        )
    }
    assert policy.ISOLATED_MUTATION_ALLOW == non_tool | {
        pair for routes in table.values() for pair in routes
    }


@pytest.mark.parametrize("mount", ["self", "messaging"])
@pytest.mark.parametrize(
    "skills", [[], ["pinky-self"], ["pinky-self", "pinky-memory", "research", "presentations"]]
)
async def test_isolated_visibility_and_route_grants_share_one_table(
    daemon,
    monkeypatch,
    tool_capture,
    mount,
    skills,
):
    d = daemon()
    d.agents.register("caller", isolated=True)
    recorded = await _capture_all(tool_capture)
    server = tool_capture.servers[mount]
    store = SimpleNamespace(get_agent_skills=lambda *args, **kwargs: [{"name": s} for s in skills])
    install_tool_policy(server, mount, d.agents, store, lambda name: "fixture-key")
    current = set(policy.CORE_TOOLS[mount])
    if mount == "self":
        for gate in policy.agent_tool_gates("caller", store, d.agents):
            current.update(policy.GATE_TOOL_NAMES[gate])
    expected = current & {name for m, name in EXPECTED_TABLE if m == mount}
    listed = {tool.name for tool in (await _dispatch(server)).tools}
    assert listed == expected
    denied = _mutation_denied(d.app)
    for name in listed:
        for method, path in recorded[mount, name]:
            assert not denied(Request(_scope(method, path, d.app)), "caller", "enforce")
    hidden = {tool.name for tool in server._tool_manager.list_tools()} - listed
    for name in sorted(hidden):
        tool_capture.calls.clear()
        result = await _dispatch(server, name, _arguments(server._tool_manager.get_tool(name).fn))
        assert result.isError, name
        assert result.content == [
            types.TextContent(
                type="text",
                text="Tool denied by caller policy",
            )
        ]
        assert not tool_capture.calls, name


@pytest.mark.parametrize("flag", [True, None])
async def test_shadow_lists_everything_but_logs_hidden_tools(
    monkeypatch, tool_capture, capsys, flag
):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "shadow")
    server = tool_capture.servers["self"]
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(isolated=flag))
    skills = SimpleNamespace(
        get_agent_skills=lambda *args, **kwargs: [
            {"name": name} for name in ("pinky-self", "pinky-memory", "research", "presentations")
        ]
    )
    install_tool_policy(server, "self", registry, skills, lambda name: "fixture-key")
    assert {t.name for t in (await _dispatch(server)).tools} == ALL_SELF_NAMES
    log = capsys.readouterr().err
    assert all(f"WOULD DENY mcp self list {name} for caller" in log for name in HIDDEN_SELF)
    tool_capture.calls.clear()
    result = await _dispatch(server, "kb_ingest", {"text": "fixture"})
    assert not result.isError
    assert tool_capture.calls == [("POST", "/kb/ingest")]
    assert "WOULD DENY mcp self call kb_ingest for caller" in capsys.readouterr().err


@pytest.mark.parametrize("mount", ["self", "messaging", "memory"])
@pytest.mark.parametrize("mode,flag", [("enforce", False), ("off", True)])
async def test_nonisolated_and_policy_off_visibility_is_unchanged(
    monkeypatch,
    tool_capture,
    mount,
    mode,
    flag,
):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
    server = tool_capture.servers[mount]
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(isolated=flag))
    skills = SimpleNamespace(
        get_agent_skills=lambda *args, **kwargs: [
            {"name": name} for name in ("pinky-self", "pinky-memory", "research", "presentations")
        ]
    )
    install_tool_policy(server, mount, registry, skills, lambda name: "fixture-key")
    registered = {tool.name for tool in server._tool_manager.list_tools()}
    assert {t.name for t in (await _dispatch(server)).tools} == registered


@pytest.mark.parametrize("flag", [True, None])
async def test_memory_visibility_is_independent_of_route_table(monkeypatch, tool_capture, flag):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "enforce")
    server = tool_capture.servers["memory"]
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(isolated=flag))
    install_tool_policy(server, "memory", registry, None, lambda name: "fixture-key")
    assert {t.name for t in (await _dispatch(server)).tools} == policy.CORE_TOOLS["memory"]


@pytest.fixture
def task_api(daemon, monkeypatch):
    d = daemon()
    d.agents.register("caller", isolated=True)
    d.agents.register("other", isolated=False)
    monkeypatch.setattr(projects_tasks, "_autonomy", SimpleNamespace(push_event=AsyncMock()))
    monkeypatch.setattr(projects_tasks, "_activity", SimpleNamespace(log=Mock()))
    ingest = Mock()
    monkeypatch.setattr(projects_tasks, "_kb_auto_ingest", ingest)
    return SimpleNamespace(
        d=d, client=TestClient(d.app), tasks=projects_tasks._tasks, ingest=ingest
    )


def _request(fixture, path, mode, actor="caller", body=None, params=None):
    headers = signed(fixture.d, "POST", path, name=actor) if actor else {}
    if not actor:
        fixture.client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
    return fixture.client.post(path, headers=headers, json=body, params=params)


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
def test_create_task_checks_assignee_before_inserting(task_api, monkeypatch, capsys, mode):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
    before = task_api.tasks.list(include_completed=True)
    response = _request(
        task_api,
        "/tasks",
        mode,
        body={
            "title": "Fixture",
            "assigned_agent": "other",
            "created_by": "forged",
        },
    )
    if mode == "enforce":
        assert response.status_code == 403
        assert response.json() == OWNERSHIP_ERROR
        assert task_api.tasks.list(include_completed=True) == before
        assert not projects_tasks._autonomy.push_event.called
        assert not projects_tasks._activity.log.called
    else:
        assert response.status_code == 200
        assert response.json()["assigned_agent"] == "other"
        assert response.json()["created_by"] == "forged"
        assert (
            capsys.readouterr().err.count(
                "isolation: WOULD DENY POST /tasks for caller mode=shadow reason=ownership"
            )
            == 1
        )


@pytest.mark.parametrize("assignee", ["", "caller"])
def test_create_task_forces_verified_creator(task_api, assignee):
    response = _request(
        task_api,
        "/tasks",
        "enforce",
        body={
            "title": "Fixture",
            "assigned_agent": assignee,
            "created_by": "forged",
        },
    )
    assert response.status_code == 200
    assert response.json()["assigned_agent"] == assignee
    assert response.json()["created_by"] == "caller"
    assert task_api.tasks.get(response.json()["id"]).created_by == "caller"


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("operation", ["claim", "complete", "block"])
@pytest.mark.parametrize("assignee,creator", [("other", "caller"), ("", "other")])
def test_foreign_task_mutations_leave_rows_and_comments_unchanged(
    task_api,
    monkeypatch,
    capsys,
    mode,
    operation,
    assignee,
    creator,
):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
    task = task_api.tasks.create("Fixture", assigned_agent=assignee, created_by=creator)
    before = task.to_dict()
    path = f"/tasks/{operation}/{task.id}"
    # Spoofing the existing assignee bypasses the old claim conflict, not ownership.
    response = task_api.client.post(
        path,
        params={"agent_name": assignee, "summary": SUMMARY, "reason": "fixture"},
        headers=signed(task_api.d, "POST", path, name="caller"),
    )
    if mode == "enforce":
        assert response.status_code == 403
        assert response.json() == OWNERSHIP_ERROR
        assert task_api.tasks.get(task.id).to_dict() == before
        assert task_api.tasks.get_comments(task.id) == []
        assert not task_api.ingest.called
        assert not projects_tasks._autonomy.push_event.called
        assert not projects_tasks._activity.log.called
    else:
        assert response.status_code == 200
        assert task_api.tasks.get_comments(task.id)[0].author == assignee
        assert (
            capsys.readouterr().err.count(
                f"isolation: WOULD DENY POST /tasks/{operation}/{{task_id}} "
                "for caller mode=shadow reason=ownership"
            )
            == 1
        )


@pytest.mark.parametrize(
    "operation,assignee",
    [
        ("claim", "caller"),
        ("claim", ""),
        ("complete", "caller"),
        ("block", "caller"),
    ],
)
def test_own_task_identity_comes_from_signed_caller(task_api, operation, assignee):
    task = task_api.tasks.create("Fixture", assigned_agent=assignee, created_by="caller")
    path = f"/tasks/{operation}/{task.id}"
    response = task_api.client.post(
        path,
        params={"agent_name": "forged", "summary": SUMMARY, "reason": "fixture"},
        headers=signed(task_api.d, "POST", path, name="caller"),
    )
    assert response.status_code == 200
    assert response.json()["assigned_agent"] == "caller"
    assert task_api.tasks.get_comments(task.id)[0].author == "caller"
    assert (
        response.json()["status"]
        == {"claim": "in_progress", "complete": "completed", "block": "blocked"}[operation]
    )
    assert not task_api.ingest.called


def test_owned_completion_skips_shared_knowledge_ingest(task_api):
    task = task_api.tasks.create("Fixture", assigned_agent="caller", created_by="other")
    path = f"/tasks/complete/{task.id}"
    response = task_api.client.post(
        path,
        params={"agent_name": "forged", "summary": SUMMARY},
        headers=signed(task_api.d, "POST", path, name="caller"),
    )
    assert response.status_code == 200
    assert task_api.tasks.get_comments(task.id)[0].author == "caller"
    assert not task_api.ingest.called
    assert projects_tasks._autonomy.push_event.await_args.args[0].data["worker"] == "caller"
    assert projects_tasks._activity.log.call_args.kwargs["agent_name"] == "caller"


@pytest.mark.parametrize(
    "actor,mode", [("other", "enforce"), ("", "enforce"), ("caller", "off"), ("caller", "shadow")]
)
def test_legacy_task_identity_and_knowledge_ingest_are_unchanged(
    task_api, monkeypatch, actor, mode
):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
    created = _request(
        task_api,
        "/tasks",
        mode,
        actor,
        {
            "title": "Fixture",
            "assigned_agent": "other",
            "created_by": "forged",
        },
    )
    assert created.status_code == 200
    assert created.json()["created_by"] == "forged"
    assert created.json()["assigned_agent"] == "other"
    task_id = created.json()["id"]
    for operation in ("claim", "complete", "block"):
        path = f"/tasks/{operation}/{task_id}"
        response = _request(
            task_api, path, mode, actor, params={"agent_name": "other", "summary": SUMMARY}
        )
        assert response.status_code == 200
        assert task_api.tasks.get_comments(task_id)[-1].author == "other"
    assert task_api.ingest.call_count == 1


@pytest.mark.parametrize("operation", ["create", "claim", "complete", "block"])
def test_shadow_identity_mismatch_is_observed_without_rewriting(
    task_api,
    monkeypatch,
    capsys,
    operation,
):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "shadow")
    if operation == "create":
        path, template = "/tasks", "/tasks"
        response = _request(
            task_api,
            path,
            "shadow",
            body={
                "title": "Fixture",
                "assigned_agent": "caller",
                "created_by": "forged",
            },
        )
        assert response.status_code == 200
        assert response.json()["created_by"] == "forged"
    else:
        task = task_api.tasks.create(
            "Fixture", assigned_agent="" if operation == "claim" else "caller", created_by="caller"
        )
        path, template = f"/tasks/{operation}/{task.id}", f"/tasks/{operation}/{{task_id}}"
        response = _request(
            task_api,
            path,
            "shadow",
            params={
                "agent_name": "forged",
                "summary": SUMMARY,
                "reason": "fixture",
            },
        )
        assert response.status_code == 200
        assert task_api.tasks.get_comments(task.id)[0].author == "forged"
        if operation == "claim":
            assert response.json()["assigned_agent"] == "forged"
        assert task_api.ingest.call_count == (1 if operation == "complete" else 0)
    assert (
        capsys.readouterr().err.count(
            f"isolation: WOULD DENY POST {template} for caller mode=shadow reason=identity"
        )
        == 1
    )
