"""Core-skill protection guards (#727).

The four core skills (file-access, pinky-memory, pinky-messaging,
pinky-self) are shared+core and underpin every agent. Before this guard
only the per-agent DELETE endpoint blocked touching them; the per-agent
*disable* opt-out (api.py) and the *global* disable/delete catalog routes
(routes/skills.py) were unguarded — a single toggle could strip an
agent's own pinky-self (self-management / recovery tools) or, globally,
knock a core skill out for the whole fleet. These tests pin the guard on
every mutation path while leaving ordinary skills untouched.
"""

from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient


def _make_app(db_path: str):
    from pinky_daemon.api import create_api
    return create_api(max_sessions=10, default_working_dir="/tmp", db_path=db_path)


CORE_SKILLS = ["pinky-self", "pinky-memory", "pinky-messaging", "file-access"]


def _with_client(fn):
    with tempfile.TemporaryDirectory() as td:
        app = _make_app(os.path.join(td, "test.db"))
        with TestClient(app) as client:
            fn(client)


def test_core_skills_are_seeded():
    """Sanity: the four core skills exist and are categorized 'core'."""
    def check(client):
        for name in CORE_SKILLS:
            r = client.get(f"/skills/{name}")
            assert r.status_code == 200, (name, r.status_code, r.text)
            assert r.json()["category"] == "core", name
    _with_client(check)


def test_per_agent_disable_core_skill_rejected():
    """POST /agents/{a}/skills/{core}/disable must 400 and create no opt-out."""
    def check(client):
        client.post("/agents", json={"name": "guard-agent", "model": "sonnet"})
        for name in CORE_SKILLS:
            r = client.post(f"/agents/guard-agent/skills/{name}/disable")
            assert r.status_code == 400, (name, r.status_code, r.text)
            assert "core skill" in r.text, (name, r.text)
        # The dangerous path was the shared opt-out: it would write a
        # disabled assignment row. Confirm none was created — every core
        # skill is still effectively enabled for the agent.
        skills = client.get(
            "/agents/guard-agent/skills?enabled_only=false"
        ).json()["skills"]
        by_name = {s["name"]: s for s in skills}
        for name in CORE_SKILLS:
            assert by_name[name]["effective_enabled"] is True, name
    _with_client(check)


def test_global_disable_core_skill_rejected():
    """POST /skills/{core}/disable must 400 and leave the skill enabled."""
    def check(client):
        for name in CORE_SKILLS:
            r = client.post(f"/skills/{name}/disable")
            assert r.status_code == 400, (name, r.status_code, r.text)
            assert "core skill" in r.text, (name, r.text)
            assert client.get(f"/skills/{name}").json()["enabled"] is True, name
    _with_client(check)


def test_global_delete_core_skill_rejected():
    """DELETE /skills/{core} must 400 and leave the skill registered."""
    def check(client):
        for name in CORE_SKILLS:
            r = client.delete(f"/skills/{name}")
            assert r.status_code == 400, (name, r.status_code, r.text)
            assert "core skill" in r.text, (name, r.text)
            assert client.get(f"/skills/{name}").status_code == 200, name
    _with_client(check)


def test_noncore_skill_disable_and_delete_still_work():
    """Regression: the guard must not block ordinary (non-core) skills."""
    def check(client):
        client.post("/skills", json={
            "name": "test-noncore",
            "description": "non-core skill for regression coverage",
            "category": "general",
        })
        client.post("/agents", json={"name": "regular-agent", "model": "sonnet"})
        client.post(
            "/agents/regular-agent/skills/test-noncore",
            json={"assigned_by": "user"},
        )

        # Per-agent disable works for a non-core skill.
        r = client.post("/agents/regular-agent/skills/test-noncore/disable")
        assert r.status_code == 200, r.text

        # Global disable works.
        r = client.post("/skills/test-noncore/disable")
        assert r.status_code == 200, r.text
        assert client.get("/skills/test-noncore").json()["enabled"] is False

        # Global delete works.
        r = client.delete("/skills/test-noncore")
        assert r.status_code == 200, r.text
        assert client.get("/skills/test-noncore").status_code == 404
    _with_client(check)
