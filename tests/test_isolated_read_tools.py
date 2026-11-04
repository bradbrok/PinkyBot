"""Read tools use scoped routes or return a plain refusal without response data."""

import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.responses import Response

from pinky_daemon.routes import projects_tasks
from pinky_self import server as self_server
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_read_scopes import layout as layout

pytestmark = pytest.mark.real_auth

FORBIDDEN_READ_TOOLS = [
    ("kb_search", {"query": "fixture"}),
    ("kb_get_wiki", {"topic": "fixture"}),
    ("kb_stats", {}),
    ("list_presentations", {}),
    ("get_my_research_assignments", {}),
    ("list_research_topics", {}),
    ("get_research_detail", {"topic_id": 1}),
    ("export_research_pdf", {"topic_id": 1}),
    ("get_app_source", {"app_id": 1}),
    ("list_apps", {}),
    ("app_url", {"app_id": 1}),
]


def _server(instance, name="tenant"):
    return self_server.create_server(
        agent_name=name, api_url="http://fixture",
        tool_gates=["kb", "research", "presentations", "schedule", "voice", "apps"],
        signing_key_resolver=instance.agents.get_signing_key,
    )


def _functions(server):
    return {tool.name: tool.fn for tool in server._tool_manager.list_tools()}


@pytest.fixture
def transport(layout, monkeypatch):
    client = TestClient(layout.app)
    requests = []

    def urlopen(request, *args, **kwargs):
        path = urlsplit(request.full_url)
        target = path.path + ("?" + path.query if path.query else "")
        requests.append((request.method, target, dict(request.header_items())))
        response = client.request(
            request.method, target, headers=dict(request.header_items()), content=request.data,
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, "fixture refusal", response.headers,
                io.BytesIO(response.content),
            )
        body = io.BytesIO(response.content)
        body.headers = response.headers
        return body

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    try:
        yield SimpleNamespace(requests=requests, functions=_functions(_server(layout)))
    finally:
        client.close()


@pytest.mark.parametrize("name,arguments", FORBIDDEN_READ_TOOLS)
def test_forbidden_read_tools_return_one_plain_sentence(layout, monkeypatch, name, arguments):
    functions = _functions(_server(layout))

    def refuse(request, *args, **kwargs):
        raise urllib.error.HTTPError(
            request.full_url, 403, "forbidden", {},
            io.BytesIO(json.dumps({
                "error": "isolated agent 'tenant' cannot read this route", "agent": "tenant",
            }).encode()),
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    result = functions[name](**arguments)
    assert isinstance(result, str)
    assert "\n" not in result
    assert not result.startswith(("{", "["))
    assert "private-fleet-marker" not in result and "Traceback" not in result
    assert "This read is not available to this caller." in result


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("body", [
    '{"detail":"existing refusal"}',
    '{"error":"isolated agent \'tenant\' cannot read this route","agent":"peer"}',
    '{"error":"isolated agent \'tenant\' cannot read this route extra","agent":"tenant"}',
    '{"error":"isolated agent \'tenant\' cannot read this route","agent":"tenant","extra":true}',
])
def test_preexisting_get_refusal_text_stays_exact(layout, monkeypatch, mode, body):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)

    def refuse(request, *args, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO(body.encode()))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    functions = _functions(_server(layout))
    assert functions["kb_stats"]() == f"Error: {body}"


@pytest.mark.parametrize("skill", ["assigned-fixture", "delegated-fixture"])
def test_assigned_skill_tools_work_through_signed_requests(layout, monkeypatch, transport, skill):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert f"text-{skill}" in transport.functions["load_skill"](skill_name=skill)
    result = transport.functions["list_my_skills"]()
    assert "assigned-fixture" in result
    assert "unassigned-fixture" not in result
    assert transport.functions["load_skill"](skill_name="unassigned-fixture") == (
        "Skill 'unassigned-fixture' not found."
    )


def test_task_and_schedule_tools_keep_working(layout, monkeypatch, transport):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    task = projects_tasks._tasks.create(
        "Caller fixture task", assigned_agent="tenant", created_by="tenant",
    )
    projects_tasks._tasks.create("private-task-peer", assigned_agent="peer", created_by="peer")
    result = transport.functions["get_next_task"]()
    assert "Caller fixture task" in result and "private-task-peer" not in result
    assert "Caller fixture task" in transport.functions["get_task"](task_id=task.id)
    schedules = transport.functions["list_my_schedules"]()
    assert "private-wake-tenant" in schedules and "private-wake-peer" not in schedules
    schedule = layout.agents.get_schedules("tenant")[0]
    full = json.loads(transport.functions["get_schedule"](schedule_id=schedule.id))
    assert full["agent_name"] == "tenant"


def test_kb_tools_require_reader_grant_and_work_when_it_is_set(layout, monkeypatch, transport):
    from pinky_daemon.routes import kb

    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert "not available" in transport.functions["kb_stats"]()
    layout.agents.set_isolated_grants("tenant", ["kb_reader"])
    kb._kb.save_wiki("fixture-page", title="Fixture", content="Assigned reference text")
    assert transport.functions["kb_get_wiki"](topic="fixture-page").endswith("Assigned reference text")
    assert "Knowledge Base Stats" in transport.functions["kb_stats"]()
    assert "Error:" not in transport.functions["kb_search"](query="fixture")
    layout.agents.set_isolated_grants("tenant", [])
    assert "Assigned reference text" not in transport.functions["kb_get_wiki"](topic="fixture-page")


def test_scoped_tool_answers_do_not_include_private_peer_text(layout, monkeypatch, transport):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    for name in ["list_agents", "get_owner_profile", "list_voice_calls", "list_call_requests"]:
        result = transport.functions[name]()
        assert "private-code-word" not in result
        assert "private-normal" not in result
        assert "private-voice-peer" not in result


def test_export_still_signs_an_allowed_request_and_saves_in_private_tmp(
    layout, monkeypatch, transport, tmp_path,
):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    monkeypatch.setattr(self_server, "__file__", str(tmp_path / "src" / "pinky_self" / "server.py"))

    async def document(topic_id: int):
        return Response(
            b"%PDF-fixture", media_type="application/pdf",
            headers={"content-disposition": 'attachment; filename="fixture.pdf"'},
        )

    layout.app.router.routes.insert(
        0, APIRoute("/research/{topic_id}/export", document, methods=["GET"]),
    )
    functions = _functions(_server(layout, "normal"))
    result = json.loads(functions["export_research_pdf"](topic_id=1))
    assert result["success"] is True
    assert result["path"] == str(tmp_path / "data" / "exports" / "fixture.pdf")
    assert (tmp_path / "data" / "exports" / "fixture.pdf").read_bytes() == b"%PDF-fixture"
    method, path, headers = transport.requests[-1]
    assert (method, path) == ("GET", "/research/1/export?format=pdf")
    assert any(key.lower() == "x-pinky-signature" for key in headers)
