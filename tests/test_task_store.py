"""Tests for task store, projects, and task API."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.task_store import TaskStore


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = TaskStore(db_path=path)
    yield s
    s.close()
    os.unlink(path)


class TestTaskCRUD:
    def test_create(self, store):
        task = store.create("Fix the bug", description="It's broken", priority="high")
        assert task.id > 0
        assert task.title == "Fix the bug"
        assert task.description == "It's broken"
        assert task.priority == "high"
        assert task.status == "pending"

    def test_get(self, store):
        created = store.create("Test task")
        got = store.get(created.id)
        assert got is not None
        assert got.title == "Test task"

    def test_get_missing(self, store):
        assert store.get(999) is None

    def test_update(self, store):
        task = store.create("Original")
        updated = store.update(task.id, title="Updated", status="in_progress")
        assert updated.title == "Updated"
        assert updated.status == "in_progress"

    def test_update_missing(self, store):
        assert store.update(999, title="nope") is None

    def test_update_tags(self, store):
        task = store.create("Tagged", tags=["a", "b"])
        updated = store.update(task.id, tags=["x", "y", "z"])
        assert updated.tags == ["x", "y", "z"]

    def test_update_blocked_by(self, store):
        t1 = store.create("First")
        t2 = store.create("Second", blocked_by=[t1.id])
        assert t2.blocked_by == [t1.id]
        updated = store.update(t2.id, blocked_by=[])
        assert updated.blocked_by == []

    def test_delete(self, store):
        task = store.create("Doomed")
        assert store.delete(task.id) is True
        assert store.get(task.id) is None

    def test_delete_missing(self, store):
        assert store.delete(999) is False

    def test_delete_cascades_subtasks(self, store):
        parent = store.create("Parent")
        child = store.create("Child", parent_id=parent.id)
        store.delete(parent.id)
        assert store.get(child.id) is None


class TestTaskQueries:
    def test_list_default(self, store):
        store.create("A", status="pending")
        store.create("B", status="in_progress")
        store.create("C", status="completed")
        # Default excludes completed
        tasks = store.list()
        assert len(tasks) == 2

    def test_list_include_completed(self, store):
        store.create("A", status="pending")
        store.create("B", status="completed")
        tasks = store.list(include_completed=True)
        assert len(tasks) == 2

    def test_list_by_status(self, store):
        store.create("A", status="pending")
        store.create("B", status="in_progress")
        tasks = store.list(status="in_progress")
        assert len(tasks) == 1
        assert tasks[0].title == "B"

    def test_list_by_agent(self, store):
        store.create("Oleg task", assigned_agent="oleg")
        store.create("Rex task", assigned_agent="rex")
        tasks = store.list(assigned_agent="oleg")
        assert len(tasks) == 1
        assert tasks[0].assigned_agent == "oleg"

    def test_list_by_priority(self, store):
        store.create("Urgent", priority="urgent")
        store.create("Low", priority="low")
        tasks = store.list(priority="urgent")
        assert len(tasks) == 1

    def test_list_by_tag(self, store):
        store.create("Tagged", tags=["bug", "backend"])
        store.create("Other", tags=["frontend"])
        tasks = store.list(tag="bug")
        assert len(tasks) == 1
        assert "bug" in tasks[0].tags

    def test_list_by_project(self, store):
        p = store.create_project("Proj")
        store.create("In project", project_id=p.id)
        store.create("No project")
        tasks = store.list(project_id=p.id)
        assert len(tasks) == 1

    def test_priority_ordering(self, store):
        store.create("Low", priority="low")
        store.create("Urgent", priority="urgent")
        store.create("Normal", priority="normal")
        tasks = store.list()
        assert tasks[0].priority == "urgent"
        assert tasks[2].priority == "low"

    def test_subtasks(self, store):
        parent = store.create("Parent")
        store.create("Sub 1", parent_id=parent.id)
        store.create("Sub 2", parent_id=parent.id)
        subs = store.get_subtasks(parent.id)
        assert len(subs) == 2

    def test_count_by_status(self, store):
        store.create("A", status="pending")
        store.create("B", status="pending")
        store.create("C", status="in_progress")
        counts = store.count_by_status()
        assert counts["pending"] == 2
        assert counts["in_progress"] == 1

    def test_count_by_agent(self, store):
        store.create("A", assigned_agent="oleg")
        store.create("B", assigned_agent="oleg")
        store.create("C", assigned_agent="rex")
        store.create("D", assigned_agent="rex", status="completed")
        counts = store.count_by_agent()
        assert counts["oleg"] == 2
        assert counts["rex"] == 1  # completed excluded


class TestProjects:
    def test_create_project(self, store):
        p = store.create_project("PinkyBot v1", description="The big one")
        assert p.id > 0
        assert p.name == "PinkyBot v1"
        assert p.status == "active"

    def test_get_project(self, store):
        p = store.create_project("Test")
        got = store.get_project(p.id)
        assert got is not None
        assert got.name == "Test"

    def test_get_missing(self, store):
        assert store.get_project(999) is None

    def test_list_projects(self, store):
        store.create_project("A")
        store.create_project("B")
        projects = store.list_projects()
        assert len(projects) == 2

    def test_list_excludes_archived(self, store):
        store.create_project("Active")
        p = store.create_project("Archived")
        store.update_project(p.id, status="archived")
        projects = store.list_projects()
        assert len(projects) == 1
        all_projects = store.list_projects(include_archived=True)
        assert len(all_projects) == 2

    def test_update_project(self, store):
        p = store.create_project("Old name")
        updated = store.update_project(p.id, name="New name")
        assert updated.name == "New name"

    def test_delete_project(self, store):
        p = store.create_project("Doomed")
        store.create("Task in project", project_id=p.id)
        assert store.delete_project(p.id) is True
        # Tasks should be unlinked, not deleted
        tasks = store.list(include_completed=True)
        assert len(tasks) == 1
        assert tasks[0].project_id == 0


class TestComments:
    def test_add_comment(self, store):
        task = store.create("Task")
        comment = store.add_comment(task.id, "oleg", "Working on it")
        assert comment.id > 0
        assert comment.author == "oleg"
        assert comment.content == "Working on it"

    def test_get_comments(self, store):
        task = store.create("Task")
        store.add_comment(task.id, "oleg", "First")
        store.add_comment(task.id, "rex", "Second")
        comments = store.get_comments(task.id)
        assert len(comments) == 2
        # Newest first
        assert comments[0].content == "Second"

    def test_get_comments_empty(self, store):
        task = store.create("Task")
        assert store.get_comments(task.id) == []

    def test_delete_comment(self, store):
        task = store.create("Task")
        c = store.add_comment(task.id, "oleg", "Temp")
        assert store.delete_comment(c.id) is True
        assert store.get_comments(task.id) == []

    def test_to_dict(self, store):
        task = store.create("Task", tags=["a"], blocked_by=[1, 2], project_id=5)
        d = task.to_dict()
        assert d["project_id"] == 5
        assert d["tags"] == ["a"]
        assert d["blocked_by"] == [1, 2]


class TestTaskAPI:
    def _make_client(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from pinky_daemon.api import create_api
        app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
        return TestClient(app), path

    def test_create_task(self):
        client, path = self._make_client()
        resp = client.post("/tasks", json={"title": "Test task", "priority": "high"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Test task"
        assert data["priority"] == "high"
        assert data["id"] > 0
        os.unlink(path)

    def test_list_tasks(self):
        client, path = self._make_client()
        client.post("/tasks", json={"title": "A"})
        client.post("/tasks", json={"title": "B"})
        resp = client.get("/tasks")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2
        os.unlink(path)

    def test_get_task(self):
        client, path = self._make_client()
        created = client.post("/tasks", json={"title": "Detail"}).json()
        resp = client.get(f"/tasks/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["task"]["title"] == "Detail"
        os.unlink(path)

    def test_update_task(self):
        client, path = self._make_client()
        created = client.post("/tasks", json={"title": "Old"}).json()
        resp = client.put(f"/tasks/{created['id']}", json={"title": "New", "status": "in_progress"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "New"
        assert resp.json()["status"] == "in_progress"
        os.unlink(path)

    def test_delete_task(self):
        client, path = self._make_client()
        created = client.post("/tasks", json={"title": "Gone"}).json()
        resp = client.delete(f"/tasks/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        os.unlink(path)

    def test_task_stats(self):
        client, path = self._make_client()
        client.post("/tasks", json={"title": "A", "status": "pending"})
        client.post("/tasks", json={"title": "B", "status": "in_progress"})
        resp = client.get("/tasks/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["by_status"]["pending"] == 1
        os.unlink(path)

    def test_projects_crud(self):
        client, path = self._make_client()
        # Create
        resp = client.post("/projects", json={"name": "Big Project"})
        assert resp.status_code == 200
        pid = resp.json()["id"]
        # List
        resp = client.get("/projects")
        assert resp.json()["count"] == 1
        # Get with tasks
        client.post("/tasks", json={"title": "In project", "project_id": pid})
        resp = client.get(f"/projects/{pid}")
        assert resp.json()["task_count"] == 1
        # Delete
        resp = client.delete(f"/projects/{pid}")
        assert resp.json()["deleted"] is True
        os.unlink(path)

    def test_task_comments(self):
        client, path = self._make_client()
        task = client.post("/tasks", json={"title": "Commented"}).json()
        tid = task["id"]
        # Add comment
        resp = client.post(f"/tasks/{tid}/comments", json={"author": "oleg", "content": "On it"})
        assert resp.status_code == 200
        assert resp.json()["author"] == "oleg"
        # List comments
        resp = client.get(f"/tasks/{tid}/comments")
        assert resp.json()["count"] == 1
        os.unlink(path)

    def test_filter_by_agent(self):
        client, path = self._make_client()
        client.post("/tasks", json={"title": "Oleg task", "assigned_agent": "oleg"})
        client.post("/tasks", json={"title": "Rex task", "assigned_agent": "rex"})
        resp = client.get("/tasks?assigned_agent=oleg")
        assert resp.json()["count"] == 1
        assert resp.json()["tasks"][0]["assigned_agent"] == "oleg"
        os.unlink(path)
