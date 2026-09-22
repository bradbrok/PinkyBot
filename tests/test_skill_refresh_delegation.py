"""Approval-backed text refresh preserves catalog authority and audit evidence."""

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.routes import skills as skill_routes
from pinky_daemon.skill_loader import parse_skill_md, register_discovered_skills
from pinky_daemon.skill_store import SkillStore

pytestmark = pytest.mark.real_auth
NAME = "refresh-fixture"
ORIGIN = "origin-agent"
ASSIGNED = "assigned-agent"
REF = "external-approval-42"


def _hash(description, directive):
    return hashlib.sha256(f"{description}\n{directive}".encode()).hexdigest()


def _write(path, *, body="Original directive.", tools="Read", description="Fixture description"):
    path.write_text(
        f"---\nname: {NAME}\ndescription: {description}\nallowed-tools: {tools}\n---\n{body}\n"
    )


def _signed(client, method, path, body=None, *, agent=ORIGIN):
    headers = build_internal_auth_headers(
        client.app.state.agents.get_signing_key(agent),
        agent_name=agent,
        method=method,
        path=path,
    )
    return client.request(
        method, path, headers=headers, **({"json": body} if body is not None else {})
    )


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_SESSION_SECRET", "refresh-fixture-session-secret")
    app = create_api(
        max_sessions=10, default_working_dir=str(tmp_path), db_path=str(tmp_path / "test.db")
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/auth/setup", json={"password": "fixture-password", "next": "/"}
            ).status_code
            == 200
        )
        for name in (ORIGIN, ASSIGNED, "other-agent"):
            app.state.agents.register(name, model="sonnet", working_dir=str(tmp_path / name))
        monkeypatch.setattr(skill_routes, "_pinky_root", tmp_path)
        path = tmp_path / "skills" / NAME / "SKILL.md"
        path.parent.mkdir(parents=True)
        _write(path)
        store = skill_routes._skills
        store.register(
            NAME,
            description="Fixture description",
            directive="Original directive.",
            skill_type="skill",
            origin_agent=ORIGIN,
            category="skill",
            tool_patterns=["Read"],
            config={"location": str(path)},
        )
        assert store.assign_to_agent(ASSIGNED, NAME, assigned_by="user")
        yield SimpleNamespace(client=client, store=store, path=path)


def _delegate(catalog):
    response = catalog.client.put(f"/skills/{NAME}/refresh-delegate", json={"agent": ORIGIN})
    assert response.status_code == 200, response.text


def _audit(catalog, **kwargs):
    response = catalog.client.get(f"/skills/{NAME}/refresh-audit", **kwargs)
    assert response.status_code == 200, response.text
    return response.json()["audit"]


def test_operator_controls_delegate_and_rejects_invalid_targets(catalog):
    c = catalog.client
    _delegate(catalog)
    assert c.get(f"/skills/{NAME}").json()["refresh_delegate"] == ORIGIN
    assert _signed(c, "PUT", f"/skills/{NAME}/refresh-delegate", {"agent": ""}).status_code == 403
    assert c.put(f"/skills/{NAME}/refresh-delegate", json={"agent": ASSIGNED}).status_code == 400
    catalog.store.register("not-markdown", skill_type="custom")
    assert c.put("/skills/not-markdown/refresh-delegate", json={"agent": ""}).status_code == 400
    assert c.put("/skills/absent/refresh-delegate", json={"agent": ""}).status_code == 404
    assert (
        c.put(f"/skills/{NAME}/refresh-delegate", json={"agent": ""}).json()["refresh_delegate"]
        == ""
    )


def test_delegated_put_updates_text_with_exact_audit_and_approval(catalog):
    _delegate(catalog)
    before = catalog.store.get(NAME)
    response = _signed(
        catalog.client,
        "PUT",
        f"/skills/{NAME}",
        {"directive": "Approved directive.", "approval_ref": REF},
    )
    assert response.status_code == 200, response.text
    assert response.json()["last_approval_ref"] == REF
    assert catalog.store.get(NAME).directive == "Approved directive."
    audit = _audit(catalog)
    assert len(audit) == 1
    assert audit[0]["actor"] == ORIGIN
    assert audit[0]["path"] == "put"
    assert audit[0]["approval_ref"] == REF
    assert audit[0]["before_hash"] == _hash(before.description, before.directive)
    assert audit[0]["after_hash"] == _hash(before.description, "Approved directive.")
    assert audit[0]["fields"] == ["directive"]
    for agent in (ORIGIN, ASSIGNED):
        assert (
            _signed(catalog.client, "GET", f"/skills/{NAME}/refresh-audit", agent=agent).status_code
            == 200
        )
    assert (
        _signed(
            catalog.client, "GET", f"/skills/{NAME}/refresh-audit", agent="other-agent"
        ).status_code
        == 403
    )


def test_delegated_put_requires_nonempty_approval(catalog):
    _delegate(catalog)
    before = catalog.store.get(NAME).to_dict()
    for approval in (None, "", "   "):
        body = {"directive": "Unapproved edit."}
        if approval is not None:
            body["approval_ref"] = approval
        response = _signed(catalog.client, "PUT", f"/skills/{NAME}", body)
        assert response.status_code == 403
        assert response.json()["detail"] == "approval_ref required for delegated refresh"
    assert catalog.store.get(NAME).to_dict() == before
    assert _audit(catalog) == []


def test_delegated_put_refuses_every_nontext_field(catalog):
    _delegate(catalog)
    before = catalog.store.get(NAME).to_dict()
    changes = {
        "tool_patterns": ["Write"],
        "shared": True,
        "self_assignable": True,
        "category": "changed",
        "enabled": False,
        "config": {"location": "different"},
        "mcp_server_config": {"command": "fixture"},
        "requires": ["another-skill"],
        "version": "2",
        "skill_type": "custom",
        "file_templates": {"fixture": "text"},
        "default_config": {"value": 1},
        "privileged_tool_opt_in": True,
    }
    for field, value in changes.items():
        response = _signed(
            catalog.client, "PUT", f"/skills/{NAME}", {field: value, "approval_ref": REF}
        )
        assert response.status_code == 403, (field, response.text)
        assert response.json()["detail"] == "delegated refresh is text-only"
        assert catalog.store.get(NAME).to_dict() == before
    assert _audit(catalog) == []


def test_origin_without_delegate_keeps_cross_assignment_guard(catalog):
    response = _signed(
        catalog.client, "PUT", f"/skills/{NAME}", {"directive": "Changed.", "approval_ref": REF}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "skill assigned to another agent"
    assert catalog.client.get(f"/skills/{NAME}").json()["refresh_delegate"] == ""
    assert _audit(catalog) == []


def test_operator_discover_refresh_updates_once_and_audits(catalog):
    before = catalog.store.get(NAME)
    _write(catalog.path, body="Disk edit.")
    response = catalog.client.post("/skills/discover", json={"refresh": True, "approval_ref": REF})
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == [
        {
            "name": NAME,
            "before_hash": _hash(before.description, before.directive),
            "after_hash": _hash(before.description, "Disk edit."),
        }
    ]
    assert catalog.store.get(NAME).directive == "Disk edit."
    assert catalog.store.get(NAME).last_approval_ref == REF
    audit = _audit(catalog)
    assert len(audit) == 1 and audit[0]["actor"] == "user" and audit[0]["path"] == "discover"
    second = catalog.client.post("/skills/discover", json={"refresh": True}).json()
    assert NAME in second["unchanged"] and second["updated"] == []
    assert len(_audit(catalog)) == 1


def test_operator_discover_refuses_tool_drift_without_mutation(catalog):
    before = catalog.store.get(NAME).to_dict()
    _write(catalog.path, body="Changed text.", tools="Write")
    response = catalog.client.post("/skills/discover", json={"refresh": True, "approval_ref": REF})
    assert response.status_code == 200, response.text
    assert response.json()["refused"] == [
        {"name": NAME, "reason": "tool_patterns differ", "fields": ["tool_patterns"]}
    ]
    assert catalog.store.get(NAME).to_dict() == before
    assert _audit(catalog) == []


def test_signed_discover_refresh_requires_delegation_and_approval(catalog):
    _write(catalog.path, body="Delegated disk edit.")
    response = _signed(
        catalog.client, "POST", "/skills/discover", {"refresh": True, "approval_ref": REF}
    )
    assert response.status_code == 403
    _delegate(catalog)
    assert _signed(catalog.client, "POST", "/skills/discover", {"refresh": True}).status_code == 403
    response = _signed(
        catalog.client, "POST", "/skills/discover", {"refresh": True, "approval_ref": REF}
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"][0]["name"] == NAME
    assert catalog.store.get(NAME).directive == "Delegated disk edit."
    assert _audit(catalog)[0]["actor"] == ORIGIN


def test_disk_drift_tracks_edits_refresh_and_missing_files(catalog):
    c = catalog.client
    assert c.get(f"/skills/{NAME}").json()["disk_drift"] is False
    _write(catalog.path, body="Disk edit.")
    assert c.get(f"/skills/{NAME}").json()["disk_drift"] is True
    assert c.post("/skills/discover", json={"refresh": True}).status_code == 200
    assert c.get(f"/skills/{NAME}").json()["disk_drift"] is False
    catalog.path.unlink()
    assert c.get(f"/skills/{NAME}").json()["disk_drift"] is False
    catalog.path.write_text("not frontmatter")
    assert c.get(f"/skills/{NAME}").json()["disk_drift"] is False


def test_startup_discovery_does_not_refresh_existing_text(catalog):
    before = catalog.store.get(NAME).to_dict()
    _write(catalog.path, body="Unapproved startup edit.")
    result = register_discovered_skills(
        catalog.store, [parse_skill_md(catalog.path)], overwrite=False
    )
    assert result["drifted"] == [{"name": NAME, "fields": ["directive"]}]
    assert catalog.store.get(NAME).to_dict() == before


def test_register_preserves_delegate_and_last_approval(catalog):
    _delegate(catalog)
    response = catalog.client.put(
        f"/skills/{NAME}", json={"directive": "Approved edit.", "approval_ref": REF}
    )
    assert response.status_code == 200
    catalog.store.register(NAME, description="Registered again.", skill_type="skill")
    skill = catalog.store.get(NAME)
    assert skill.refresh_delegate == ORIGIN
    assert skill.last_approval_ref == REF


def test_old_database_migrates_refresh_columns_and_audit_table(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE skills (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', skill_type TEXT NOT NULL DEFAULT 'custom', version TEXT NOT NULL DEFAULT '0.1.0', enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        db.execute("INSERT INTO skills(name, created_at, updated_at) VALUES ('old-skill', 1, 1)")
    store = SkillStore(str(path))
    try:
        columns = {row[1] for row in store._db.execute("PRAGMA table_info(skills)")}
        assert {"refresh_delegate", "last_approval_ref"} <= columns
        assert store.get("old-skill").refresh_delegate == ""
        assert store.get("old-skill").last_approval_ref == ""
        assert store.list_refresh_audit("old-skill") == []
    finally:
        store.close()


def test_explicit_discovery_overwrite_still_updates(catalog):
    _write(catalog.path, body="Explicit overwrite.")
    result = register_discovered_skills(
        catalog.store, [parse_skill_md(catalog.path)], overwrite=True
    )
    assert result["updated"] == [NAME]
    assert catalog.store.get(NAME).directive == "Explicit overwrite."


def test_startup_clamp_convergence_preserves_text_patterns_and_config(catalog):
    store = catalog.store
    with store._db:
        store._db.execute(
            "UPDATE skills SET shared=1, privileged_tool_opt_in=1, self_assignable=1 WHERE name=?",
            (NAME,),
        )
    before = store._db.execute(
        "SELECT description, directive, tool_patterns, config FROM skills WHERE name=?", (NAME,)
    ).fetchone()
    _write(catalog.path, body="Unapproved body.", tools="Write")
    register_discovered_skills(store, [parse_skill_md(catalog.path)], overwrite=False)
    after = store._db.execute(
        "SELECT description, directive, tool_patterns, config FROM skills WHERE name=?", (NAME,)
    ).fetchone()
    assert after == before
    skill = store.get(NAME)
    assert not skill.shared and not skill.privileged_tool_opt_in and not skill.self_assignable


def test_refresh_text_and_audit_rollback_together(catalog, monkeypatch):
    before = catalog.store.get(NAME).to_dict()

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(catalog.store, "_insert_refresh_audit", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit unavailable"):
        catalog.store.refresh_text(
            NAME,
            description="Changed.",
            directive="Changed.",
            actor="user",
            path="put",
            approval_ref=REF,
        )
    assert catalog.store.get(NAME).to_dict() == before
    assert catalog.store.list_refresh_audit(NAME) == []


def test_operator_put_audits_without_approval_and_strips_provided_ref(catalog):
    response = catalog.client.put(f"/skills/{NAME}", json={"directive": "Operator edit."})
    assert response.status_code == 200
    first = _audit(catalog)[0]
    assert first["actor"] == "user" and first["approval_ref"] == ""
    response = catalog.client.put(
        f"/skills/{NAME}",
        json={"description": "Approved description.", "approval_ref": f"  {REF}  "},
    )
    assert response.status_code == 200
    assert response.json()["last_approval_ref"] == REF
    assert _audit(catalog)[0]["approval_ref"] == REF
    for route in (f"/skills/{NAME}", "/skills/discover"):
        method = "PUT" if route != "/skills/discover" else "POST"
        assert (
            catalog.client.request(method, route, json={"approval_ref": "x" * 201}).status_code
            == 422
        )


def test_delegation_compares_values_and_never_bypasses_origin(catalog):
    _delegate(catalog)
    before = catalog.store.get(NAME)
    response = _signed(
        catalog.client,
        "PUT",
        f"/skills/{NAME}",
        {
            "directive": "Approved text.",
            "category": before.category,
            "tool_patterns": before.tool_patterns,
            "approval_ref": REF,
        },
    )
    assert response.status_code == 200, response.text
    assert _audit(catalog)[0]["fields"] == ["directive"]
    catalog.store.set_refresh_delegate(NAME, ASSIGNED)
    response = _signed(
        catalog.client,
        "PUT",
        f"/skills/{NAME}",
        {
            "directive": "Wrong origin.",
            "approval_ref": REF,
        },
        agent=ASSIGNED,
    )
    assert response.status_code == 403
    assert catalog.store.get(NAME).directive == "Approved text."


def test_discovery_without_refresh_reports_drift_without_writing(catalog):
    _write(catalog.path, body="Unapproved file edit.")
    before = catalog.store.get(NAME).to_dict()
    response = catalog.client.post("/skills/discover")
    assert response.status_code == 200
    assert response.json()["drifted"] == [{"name": NAME, "fields": ["directive"]}]
    assert catalog.store.get(NAME).to_dict() == before
    assert _audit(catalog) == []


def test_tool_only_drift_is_visible_and_refused(catalog):
    _write(catalog.path, tools="Write")
    assert catalog.client.get(f"/skills/{NAME}").json()["disk_drift"] is True
    before = catalog.store.get(NAME).to_dict()
    response = catalog.client.post("/skills/discover", json={"refresh": True})
    assert response.json()["refused"][0]["fields"] == ["tool_patterns"]
    assert catalog.store.get(NAME).to_dict() == before


def test_invalid_disk_patterns_report_drift_and_cannot_skip_clamps(catalog):
    store = catalog.store
    with store._db:
        store._db.execute("UPDATE skills SET shared=1, self_assignable=1 WHERE name=?", (NAME,))
    _write(catalog.path, tools='["Read,Grep"]')
    result = register_discovered_skills(store, [parse_skill_md(catalog.path)], overwrite=False)
    assert result["drifted"] == [{"name": NAME, "fields": ["tool_patterns"]}]
    skill = store.get(NAME)
    assert skill.tool_patterns == ["Read"]
    assert skill.directive == "Original directive."
    assert not skill.shared and not skill.self_assignable
