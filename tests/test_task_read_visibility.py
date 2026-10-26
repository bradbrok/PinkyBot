"""Signed task readers see only their permitted rows and derived counts."""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.routes import projects_tasks
from tests.conftest import TEST_SESSION_SECRET
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth

ROUTES = (
    "list", "stats", "next", "task", "comments", "project", "hub",
    "milestones", "sprints", "sprint", "burndown",
)
ROLES = {
    "own": ("tenant", "user"),
    "created": ("peer", "tenant"),
    "unassigned": ("", "peer"),
    "hidden": ("peer", "peer"),
}


@pytest.fixture
def data(daemon):
    d = daemon()
    store = projects_tasks._tasks
    project = store.create_project("Example project")
    milestone = store.create_milestone(project.id, "Example milestone")
    sprint = store.create_sprint(project.id, "Example sprint", start_date=date.today().isoformat())
    store.start_sprint(sprint.id)
    rows = {}
    for role, (assigned, creator) in ROLES.items():
        for status in ("pending", "completed", "cancelled"):
            rows[role, status] = store.create(
                f"{role} {status}", assigned_agent=assigned, created_by=creator,
                status=status, priority="urgent" if role == "hidden" else "normal",
                project_id=project.id, milestone_id=milestone.id, sprint_id=sprint.id,
            )
    parent = rows["own", "pending"]
    for role, (assigned, creator) in ROLES.items():
        rows[role, "child"] = store.create(
            f"{role} child", assigned_agent=assigned, created_by=creator,
            parent_id=parent.id, project_id=project.id, milestone_id=milestone.id,
            sprint_id=sprint.id, priority="urgent" if role == "hidden" else "normal",
        )
    for task in rows.values():
        store.add_comment(task.id, "user", f"Comment for {task.title}")
        projects_tasks._activity.log(
            agent_name=task.assigned_agent, event_type="task_created", title=task.title,
            metadata={"project_id": project.id, "task_id": task.id},
        )
    client = TestClient(d.app)
    yield SimpleNamespace(d=d, client=client, store=store, rows=rows, parent=parent,
                          project=project, milestone=milestone, sprint=sprint)
    client.close()


def path_for(x, route):
    return {
        "list": "/tasks?include_completed=true",
        "stats": "/tasks/stats",
        "next": "/tasks/next?agent_name=peer",
        "task": f"/tasks/{x.parent.id}",
        "comments": f"/tasks/{x.rows['hidden', 'pending'].id}/comments",
        "project": f"/projects/{x.project.id}",
        "hub": f"/projects/{x.project.id}/hub",
        "milestones": f"/projects/{x.project.id}/milestones",
        "sprints": f"/projects/{x.project.id}/sprints",
        "sprint": f"/sprints/{x.sprint.id}",
        "burndown": f"/sprints/{x.sprint.id}/burndown",
    }[route]


def read(x, path, actor="tenant"):
    headers = signed(x.d, "GET", path.split("?")[0], actor) if actor else {}
    return x.client.get(path, headers=headers)


def assert_view(x, route, response, restricted):
    roles = ("own", "created", "unassigned") if restricted else tuple(ROLES)
    ids = {t.id for (role, _), t in x.rows.items() if role in roles}
    count = len(ids)
    payload = response.json()
    if route == "comments" and restricted:
        assert (response.status_code, payload) == (404, {"detail": "Task not found"})
        return
    assert response.status_code == 200, response.text
    if route in ("list", "project"):
        assert {t["id"] for t in payload["tasks"]} == ids
        assert payload["count" if route == "list" else "task_count"] == count
    elif route == "stats":
        assert payload == {
            "by_status": {"pending": len(roles)*2, "completed": len(roles), "cancelled": len(roles)},
            "by_agent": {"tenant": 2, "peer": 2 if restricted else 4},
        }
    elif route == "next":
        assert payload["source"] == "assigned"
        expected_role = "created" if restricted else "hidden"
        assert payload["task"]["id"] in {
            x.rows[expected_role, status].id for status in ("pending", "child")
        }
    elif route == "task":
        assert payload["task"] == x.parent.to_dict()
        assert {t["id"] for t in payload["subtasks"]} == {x.rows[r, "child"].id for r in roles}
        assert payload["comments"] == [c.to_dict() for c in x.store.get_comments(x.parent.id)]
    elif route == "comments":
        assert payload == {"comments": [c.to_dict() for c in x.store.get_comments(x.rows['hidden', 'pending'].id)], "count": 1}
    elif route == "hub":
        assert len(payload["recent_tasks"]) == 10
        assert {t["id"] for t in payload["recent_tasks"]} <= ids
        assert payload["task_stats"] == {"pending": len(roles)*2, "completed": len(roles), "cancelled": len(roles)}
        assert payload["active_sprint"]["tasks_total"] == count
        assert payload["active_sprint"]["tasks_completed"] == len(roles)*2
        assert payload["active_sprint"]["progress_pct"] == 50
        assert payload["milestones"][0]["task_count"] == count
        assert payload["milestones"][0]["tasks_completed"] == len(roles)*2
        assert payload["milestones"][0]["progress_pct"] == 50
        assert len(payload["recent_activity"]) == 10
        assert {e["metadata"]["task_id"] for e in payload["recent_activity"]} <= ids
    elif route == "milestones":
        assert payload["count"] == 1
        assert payload["milestones"][0]["task_count"] == count
    elif route in ("sprints", "sprint"):
        value = payload["sprints"][0] if route == "sprints" else payload
        assert value["task_counts"] == {"total": count, "completed": len(roles)*2}
    elif route == "burndown":
        assert payload == {"sprint_id": x.sprint.id, "series": [{
            "date": date.today().isoformat(), "completed": len(roles)*2,
            "cumulative": len(roles)*2, "total": count,
        }]}


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("route", ROUTES)
def test_task_reads_restrict_every_route(data, monkeypatch, mode, route):
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
    assert_view(data, route, read(data, path_for(data, route)), True)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("failure", ["error", "missing"])
def test_task_reads_fail_closed(data, monkeypatch, route, failure):
    path = path_for(data, route)
    headers = signed(data.d, "GET", path.split("?")[0])
    original = data.d.agents.get

    def get(name):
        if name == "tenant":
            if failure == "error":
                raise RuntimeError("registry unavailable")
            return None
        return original(name)

    monkeypatch.setattr(data.d.agents, "get", get)
    assert_view(data, route, data.client.get(path, headers=headers), True)


@pytest.mark.parametrize("flag", [None, 0, 1, "false", "true"])
def test_nonboolean_isolation_flag_cannot_unrestrict(data, monkeypatch, flag):
    path = path_for(data, "list")
    headers = signed(data.d, "GET", path.split("?")[0])
    original = data.d.agents.get
    monkeypatch.setattr(data.d.agents, "get", lambda name: SimpleNamespace(isolated=flag) if name == "tenant" else original(name))
    assert_view(data, "list", data.client.get(path, headers=headers), True)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("actor", ["normal", "browser", "unsigned"])
def test_unrestricted_task_read_controls(data, route, actor):
    if actor == "browser":
        data.client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
    response = read(data, path_for(data, route), "normal" if actor == "normal" else "")
    if actor == "unsigned":
        assert response.status_code == 401
    else:
        assert_view(data, route, response, False)


@pytest.mark.parametrize("role", ["own", "created", "unassigned"])
def test_visible_task_and_comments_remain_readable(data, role):
    task = data.rows[role, "completed"]
    response = read(data, f"/tasks/{task.id}")
    assert response.status_code == 200
    assert response.json()["task"] == task.to_dict()
    comments = read(data, f"/tasks/{task.id}/comments")
    assert comments.status_code == 200
    assert comments.json() == {"comments": [c.to_dict() for c in data.store.get_comments(task.id)], "count": 1}


@pytest.mark.parametrize("suffix", ["", "/comments"])
def test_invisible_and_missing_task_responses_match(data, suffix):
    hidden = read(data, f"/tasks/{data.rows['hidden', 'pending'].id}{suffix}")
    missing = read(data, f"/tasks/987654321{suffix}")
    assert (hidden.status_code, hidden.content) == (404, b'{"detail":"Task not found"}')
    assert (missing.status_code, missing.content) == (hidden.status_code, hidden.content)


def test_query_parameters_cannot_expand_visibility(data):
    path = "/tasks"
    headers = signed(data.d, "GET", path)
    response = data.client.get(path, params={"assigned_agent": "peer", "include_completed": "true", "internal_caller": "normal", "visible_to": "normal"}, headers=headers)
    assert response.status_code == 200
    assert {t["id"] for t in response.json()["tasks"]} == {
        t.id for (role, _), t in data.rows.items() if role == "created"
    }
    data.store.create("Private assignment", assigned_agent="other", created_by="other")
    response = data.client.get(path, params={"assigned_agent": "other"}, headers=headers)
    assert response.json() == {"tasks": [], "count": 0}


def test_visibility_filter_precedes_list_limit(data):
    response = read(data, "/tasks?limit=1&include_completed=true")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["tasks"][0]["id"] in {
        t.id for (role, _), t in data.rows.items() if role != "hidden"
    }


def test_hub_filters_activity_before_recent_limit(data):
    for i in range(15):
        projects_tasks._activity.log(agent_name="peer", event_type="task_created", title=f"Private event {i}", metadata={"project_id": data.project.id, "task_id": data.rows['hidden', 'pending'].id})
    response = read(data, path_for(data, "hub"))
    assert response.status_code == 200
    events = response.json()["recent_activity"]
    assert len(events) == 10
    assert all(e["metadata"]["task_id"] not in {t.id for (role, _), t in data.rows.items() if role == "hidden"} for e in events)


def test_self_health_already_counts_only_own_tasks(data):
    response = read(data, "/agents/tenant/health")
    assert response.status_code == 200
    assert response.json()["tasks"] == {"total_active": 2, "pending": 2, "in_progress": 0, "blocked": 0}
    assert read(data, "/agents/peer/health").status_code == 403


def test_missing_comments_unrestricted_behavior_is_preserved(data):
    response = read(data, "/tasks/987654321/comments", "normal")
    assert (response.status_code, response.json()) == (200, {"comments": [], "count": 0})
