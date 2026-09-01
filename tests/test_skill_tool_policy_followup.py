"""Follow-up regression tests for stage-2 skill tool-grant policy."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import _seed_core_skills, create_api
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.routes import skills as skill_routes
from pinky_daemon.skill_loader import discover_all_skills, register_discovered_skills
from pinky_daemon.skill_store import SkillStore
from pinky_daemon.skill_tool_policy import (
    filter_skill_tool_grants,
    has_privileged_tool_grant,
)

pytestmark = pytest.mark.real_auth


def test_seeded_core_skill_tool_patterns_are_non_privileged(tmp_path):
    store = SkillStore(str(tmp_path / "skills.db"))
    try:
        _seed_core_skills(store)

        core_skills = store.list(category="core")
        assert core_skills
        for skill in core_skills:
            assert has_privileged_tool_grant(
                skill.tool_patterns,
                skill_name=skill.name,
                mcp_server_config=skill.mcp_server_config,
                skill_type=skill.skill_type,
            ) is False, skill.name
    finally:
        store.close()


def test_seeded_shared_core_patterns_materialize_without_policy_warnings(tmp_path):
    store = SkillStore(str(tmp_path / "skills.db"))
    agent_name = "fresh-agent"
    try:
        _seed_core_skills(store)
        assert store._db.execute(
            "SELECT 1 FROM agent_skills WHERE agent_name=?",
            (agent_name,),
        ).fetchone() is None

        core_skills = store.list(category="core")
        expected_patterns = [
            pattern
            for skill in core_skills
            for pattern in skill.tool_patterns
        ]
        materialized = store.materialize_for_agent(agent_name)
        warnings: list[str] = []

        assert filter_skill_tool_grants(
            materialized["tool_grants"],
            agent_name=agent_name,
            warn=warnings.append,
        ) == expected_patterns
        assert warnings == []
    finally:
        store.close()


def test_core_seed_converges_legacy_memory_patterns_surgically(tmp_path):
    store = SkillStore(str(tmp_path / "skills.db"))
    custom_mcp_config = {
        "command": "custom-memory-server",
        "args": ["--db", "custom.db"],
    }
    try:
        store.register(
            "pinky-memory",
            skill_type="mcp_tool",
            category="core",
            shared=True,
            self_assignable=False,
            mcp_server_config=custom_mcp_config,
            tool_patterns=["mcp__pinky-memory__*", "mcp__memory__*"],
        )

        _seed_core_skills(store)

        memory = store.get("pinky-memory")
        assert memory is not None
        assert memory.tool_patterns == ["mcp__pinky-memory__*"]
        assert memory.mcp_server_config == custom_mcp_config
        assert memory.privileged_tool_opt_in is False
        assert has_privileged_tool_grant(
            memory.tool_patterns,
            skill_name=memory.name,
            mcp_server_config=memory.mcp_server_config,
            skill_type=memory.skill_type,
        ) is False

        core_skills = store.list(category="core")
        expected_patterns = [
            pattern
            for skill in core_skills
            for pattern in skill.tool_patterns
        ]
        warnings: list[str] = []
        materialized = store.materialize_for_agent("fresh-agent")
        assert filter_skill_tool_grants(
            materialized["tool_grants"],
            agent_name="fresh-agent",
            warn=warnings.append,
        ) == expected_patterns
        assert warnings == []
    finally:
        store.close()


def test_second_core_seed_pass_does_not_rewrite_unchanged_rows(tmp_path):
    store = SkillStore(str(tmp_path / "skills.db"))
    try:
        _seed_core_skills(store)
        before = {
            skill.name: skill.updated_at
            for skill in store.list(category="core")
        }
        assert before

        time.sleep(0.01)
        _seed_core_skills(store)

        assert {
            skill.name: skill.updated_at
            for skill in store.list(category="core")
        } == before
    finally:
        store.close()


def _make_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("PINKY_SESSION_SECRET", "followup-policy-secret")
    monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
    app = create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=str(tmp_path / "conversations.db"),
    )
    app.state.agents.register(
        "tenant",
        model="opus",
        isolated=True,
        working_dir=str(tmp_path / "tenant"),
    )
    client = TestClient(app)
    setup = client.post(
        "/auth/setup",
        json={"password": "hunter22", "next": "/"},
    )
    assert setup.status_code == 200, setup.text
    monkeypatch.setattr(skill_routes, "_pinky_root", Path(tmp_path))
    return client


def _signed_request(
    client: TestClient,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    agent_name: str = "tenant",
):
    signing_key = client.app.state.agents.get_signing_key(agent_name)
    assert signing_key
    headers = build_internal_auth_headers(
        signing_key,
        agent_name=agent_name,
        method=method,
        path=path,
    )
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


def _skill_md(name: str, *, allowed_tools: str = "Bash", body: str = "Upload fixture.") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: upload fixture\n"
        f"allowed-tools: {allowed_tools}\n"
        "---\n"
        f"{body}\n"
    )


def _register_agent(client: TestClient, tmp_path: Path, name: str) -> None:
    client.app.state.agents.register(
        name,
        model="opus",
        working_dir=str(tmp_path / name),
    )


def _stub_git_clone(
    monkeypatch,
    *,
    skill_name: str = "",
    allowed_tools: str = "Bash",
    fail_after_write: bool = False,
) -> list[Path]:
    created_targets: list[Path] = []

    def fake_git_run(args, **kwargs):
        target = Path(args[2]) if "-C" in args else Path(args[-1])
        created_targets.append(target)
        if skill_name:
            parsed_name = skill_name
        elif "-C" in args:
            parsed_name = target.name
        else:
            parsed_name = str(args[-2]).rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            _skill_md(parsed_name, allowed_tools=allowed_tools),
            encoding="utf-8",
        )
        if fail_after_write:
            raise subprocess.CalledProcessError(1, args, stderr=b"clone failed")
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_git_run)
    return created_targets


def _assignment_row(agent_name: str, skill_name: str):
    return skill_routes._skills._db.execute(
        """SELECT agent_name, skill_name, enabled, assigned_by, config_overrides
           FROM agent_skills WHERE agent_name=? AND skill_name=?""",
        (agent_name, skill_name),
    ).fetchone()


def _upload_skill(client: TestClient, source: str, name: str):
    if source == "from-md":
        return client.post("/skills/from-md", json={"content": _skill_md(name)})
    assert source == "from-git"
    return client.post(
        "/skills/from-git",
        json={"url": f"https://github.com/test/{name}"},
    )


@pytest.mark.parametrize("source", ["from-md", "from-git"])
def test_operator_upload_requires_separate_privileged_self_assignment_opt_in(
    monkeypatch, tmp_path, source
):
    client = _make_client(monkeypatch, tmp_path)
    _stub_git_clone(monkeypatch)
    try:
        new_name = f"{source.removeprefix('from-')}-new-privileged"
        uploaded = _upload_skill(client, source, new_name)

        assert uploaded.status_code == 200, uploaded.text
        new_skill = client.get(f"/skills/{new_name}").json()
        assert new_skill["self_assignable"] is False
        assert new_skill["privileged_tool_opt_in"] is False

        existing_name = f"{source.removeprefix('from-')}-existing-privileged"
        opted_in = client.post(
            "/skills",
            json={
                "name": existing_name,
                "skill_type": "skill",
                "tool_patterns": ["Bash"],
                "self_assignable": True,
            },
        )
        assert opted_in.status_code == 200, opted_in.text
        assert opted_in.json()["privileged_tool_opt_in"] is True

        reuploaded = _upload_skill(client, source, existing_name)

        assert reuploaded.status_code == 200, reuploaded.text
        existing_skill = client.get(f"/skills/{existing_name}").json()
        assert existing_skill["self_assignable"] is True
        assert existing_skill["privileged_tool_opt_in"] is True
    finally:
        client.close()


def test_imported_privileged_skill_accepts_only_operator_self_assignment_opt_in(
    monkeypatch, tmp_path
):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = "md-explicit-opt-in"
    try:
        _register_agent(client, tmp_path, "internal")
        uploaded = _upload_skill(client, "from-md", skill_name)

        assert uploaded.status_code == 200, uploaded.text
        catalog = client.get(f"/skills/{skill_name}").json()
        assert catalog["self_assignable"] is False
        assert catalog["privileged_tool_opt_in"] is False

        operator_opt_in = client.put(
            f"/skills/{skill_name}",
            json={"self_assignable": True},
        )

        assert operator_opt_in.status_code == 200, operator_opt_in.text
        assert operator_opt_in.json()["self_assignable"] is True
        assert operator_opt_in.json()["privileged_tool_opt_in"] is True

        internal_opt_in = _signed_request(
            client,
            "PUT",
            f"/skills/{skill_name}",
            {"self_assignable": True},
            agent_name="internal",
        )

        assert internal_opt_in.status_code == 403, internal_opt_in.text
        assert internal_opt_in.json()["detail"] == "operator-owned skill"
        unchanged = client.get(f"/skills/{skill_name}").json()
        assert unchanged["self_assignable"] is True
        assert unchanged["privileged_tool_opt_in"] is True
    finally:
        client.close()


def test_peer_signed_assignment_cannot_spoof_user_provenance_for_restricted_skill(
    monkeypatch, tmp_path
):
    client = _make_client(monkeypatch, tmp_path)
    try:
        _register_agent(client, tmp_path, "target")
        _register_agent(client, tmp_path, "requester")
        skill_name = "peer-restricted"
        created = client.post(
            "/skills",
            json={"name": skill_name, "tool_patterns": ["Bash"]},
        )
        assert created.status_code == 200, created.text

        denied = _signed_request(
            client,
            "POST",
            f"/agents/target/skills/{skill_name}",
            {"assigned_by": "user"},
            agent_name="requester",
        )

        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"] == "signed agents may only assign self-assignable skills"
        assert _assignment_row("target", skill_name) is None
    finally:
        client.close()


def test_peer_signed_assignment_records_verified_peer_provenance(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    try:
        _register_agent(client, tmp_path, "target")
        _register_agent(client, tmp_path, "requester")
        skill_name = "peer-allowed"
        created = client.post(
            "/skills",
            json={
                "name": skill_name,
                "tool_patterns": ["Bash"],
                "self_assignable": True,
            },
        )
        assert created.status_code == 200, created.text

        assigned = _signed_request(
            client,
            "POST",
            f"/agents/target/skills/{skill_name}",
            {"assigned_by": "user"},
            agent_name="requester",
        )

        assert assigned.status_code == 200, assigned.text
        assert _assignment_row("target", skill_name)[3] == "requester"
    finally:
        client.close()


def test_self_signed_assignment_keeps_self_provenance(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    try:
        _register_agent(client, tmp_path, "target")
        skill_name = "self-allowed"
        created = client.post(
            "/skills",
            json={"name": skill_name, "tool_patterns": ["Read"], "self_assignable": True},
        )
        assert created.status_code == 200, created.text

        assigned = _signed_request(
            client,
            "POST",
            f"/agents/target/skills/{skill_name}",
            {"assigned_by": "user"},
            agent_name="target",
        )

        assert assigned.status_code == 200, assigned.text
        assert _assignment_row("target", skill_name)[3] == "self"
    finally:
        client.close()


def test_operator_assignment_ignores_body_provenance(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    try:
        _register_agent(client, tmp_path, "target")
        skill_name = "operator-assigned"
        created = client.post(
            "/skills",
            json={"name": skill_name, "tool_patterns": ["Read"], "self_assignable": True},
        )
        assert created.status_code == 200, created.text

        assigned = client.post(
            f"/agents/target/skills/{skill_name}",
            json={"assigned_by": "self"},
        )

        assert assigned.status_code == 200, assigned.text
        assert _assignment_row("target", skill_name)[3] == "user"
    finally:
        client.close()


@pytest.mark.parametrize("write_route", ["post", "put", "from-md", "from-git"])
def test_agent_catalog_write_routes_preserve_operator_owned_rows(
    monkeypatch, tmp_path, write_route
):
    client = _make_client(monkeypatch, tmp_path)
    _stub_git_clone(monkeypatch)
    try:
        _register_agent(client, tmp_path, "target")
        _register_agent(client, tmp_path, "writer")
        skill_name = f"protected-{write_route}"
        created = client.post(
            "/skills",
            json={
                "name": skill_name,
                "description": "operator-owned",
                "tool_patterns": ["Bash"],
                "self_assignable": True,
                "shared": True,
            },
        )
        assert created.status_code == 200, created.text
        assigned = _signed_request(
            client,
            "POST",
            f"/agents/target/skills/{skill_name}",
            {"assigned_by": "user"},
            agent_name="target",
        )
        assert assigned.status_code == 200, assigned.text
        before_catalog = client.get(f"/skills/{skill_name}").json()
        before_assignment = _assignment_row("target", skill_name)

        if write_route == "post":
            response = _signed_request(
                client,
                "POST",
                "/skills",
                {"name": skill_name, "description": "changed"},
                agent_name="writer",
            )
        elif write_route == "put":
            response = _signed_request(
                client,
                "PUT",
                f"/skills/{skill_name}",
                {"description": "changed"},
                agent_name="writer",
            )
        elif write_route == "from-md":
            response = _signed_request(
                client,
                "POST",
                "/skills/from-md",
                {"content": _skill_md(skill_name)},
                agent_name="writer",
            )
        else:
            response = _signed_request(
                client,
                "POST",
                "/skills/from-git",
                {"url": f"https://github.com/test/{skill_name}"},
                agent_name="writer",
            )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "operator-owned skill"
        assert client.get(f"/skills/{skill_name}").json() == before_catalog
        assert _assignment_row("target", skill_name) == before_assignment
    finally:
        client.close()


@pytest.mark.parametrize("write_route", ["post", "put", "from-md", "from-git"])
def test_signed_writer_cannot_overwrite_locked_operator_catalog_row(
    monkeypatch, tmp_path, write_route
):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = f"locked-operator-{write_route}"
    created_targets = _stub_git_clone(monkeypatch, skill_name=skill_name)
    try:
        _register_agent(client, tmp_path, "writer")
        created = client.post(
            "/skills",
            json={
                "name": skill_name,
                "description": "operator-owned",
                "skill_type": "skill",
                "mcp_server_config": {"command": "trusted-command"},
                "tool_patterns": [f"mcp__{skill_name}__*"],
                "self_assignable": False,
                "shared": False,
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["privileged_tool_opt_in"] is False
        before = client.get(f"/skills/{skill_name}").json()

        if write_route == "post":
            denied = _signed_request(
                client,
                "POST",
                "/skills",
                {
                    "name": skill_name,
                    "description": "replacement",
                    "mcp_server_config": {"command": "untrusted-command"},
                },
                agent_name="writer",
            )
        elif write_route == "put":
            denied = _signed_request(
                client,
                "PUT",
                f"/skills/{skill_name}",
                {
                    "directive": "replacement directive",
                    "mcp_server_config": {"command": "untrusted-command"},
                },
                agent_name="writer",
            )
        elif write_route == "from-md":
            denied = _signed_request(
                client,
                "POST",
                "/skills/from-md",
                {"content": _skill_md(skill_name)},
                agent_name="writer",
            )
        else:
            denied = _signed_request(
                client,
                "POST",
                "/skills/from-git",
                {"url": f"https://github.com/test/{skill_name}-repo"},
                agent_name="writer",
            )

        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"] == "operator-owned skill"
        assert client.get(f"/skills/{skill_name}").json() == before
        if write_route == "from-git":
            assert created_targets
            assert all(not target.exists() for target in created_targets)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("DELETE", ""),
        ("POST", "/enable"),
        ("POST", "/disable"),
    ],
    ids=["delete", "enable", "disable"],
)
def test_signed_writer_cannot_mutate_operator_skill_state(
    monkeypatch, tmp_path, method, suffix
):
    client = _make_client(monkeypatch, tmp_path)
    try:
        _register_agent(client, tmp_path, "writer")
        skill_name = f"operator-state-{suffix.removeprefix('/') or 'delete'}"
        created = client.post(
            "/skills",
            json={"name": skill_name, "description": "operator-owned"},
        )
        assert created.status_code == 200, created.text
        before = client.get(f"/skills/{skill_name}").json()

        denied = _signed_request(
            client,
            method,
            f"/skills/{skill_name}{suffix}",
            agent_name="writer",
        )
        after = client.get(f"/skills/{skill_name}")

        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"] == "operator-owned skill"
        assert after.status_code == 200, after.text
        assert after.json() == before
    finally:
        client.close()


def test_signed_writer_cannot_disable_operator_plugin(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)

    class PluginManagerProbe:
        disabled = False

        def get(self, name):
            return SimpleNamespace(error="")

        def disable(self, name):
            self.disabled = True
            return True

    plugins = PluginManagerProbe()
    monkeypatch.setattr(skill_routes, "_plugins", plugins)
    try:
        _register_agent(client, tmp_path, "writer")
        skill_name = "operator-plugin"
        created = client.post(
            "/skills",
            json={"name": skill_name, "skill_type": "plugin"},
        )
        assert created.status_code == 200, created.text
        before = client.get(f"/skills/{skill_name}").json()

        denied = _signed_request(
            client,
            "POST",
            f"/plugins/{skill_name}/disable",
            agent_name="writer",
        )

        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"] == "operator-owned skill"
        assert plugins.disabled is False
        assert client.get(f"/skills/{skill_name}").json() == before
    finally:
        client.close()


def test_agent_put_rejects_shared_skill_without_privileged_opt_in(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    try:
        _register_agent(client, tmp_path, "writer")
        skill_name = "shared-catalog-row"
        created = client.post(
            "/skills",
            json={
                "name": skill_name,
                "tool_patterns": ["Read"],
                "self_assignable": True,
                "shared": True,
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["privileged_tool_opt_in"] is False
        before = client.get(f"/skills/{skill_name}").json()

        denied = _signed_request(
            client,
            "PUT",
            f"/skills/{skill_name}",
            {"description": "changed"},
            agent_name="writer",
        )

        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"] == "operator-owned skill"
        assert client.get(f"/skills/{skill_name}").json() == before
    finally:
        client.close()


def test_agent_put_rejects_core_skill(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    try:
        _register_agent(client, tmp_path, "writer")
        before = client.get("/skills/pinky-self").json()

        denied = _signed_request(
            client,
            "PUT",
            "/skills/pinky-self",
            {"description": "changed"},
            agent_name="writer",
        )

        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"] == "operator-owned skill"
        assert client.get("/skills/pinky-self").json() == before
    finally:
        client.close()


def test_agent_can_create_and_update_plain_catalog_draft(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    try:
        _register_agent(client, tmp_path, "writer")
        _register_agent(client, tmp_path, "peer")
        skill_name = "plain-draft"
        created = _signed_request(
            client,
            "POST",
            "/skills",
            {"name": skill_name, "description": "before"},
            agent_name="writer",
        )
        assert created.status_code == 200, created.text
        assert created.json()["origin_agent"] == "writer"

        updated = _signed_request(
            client,
            "PUT",
            f"/skills/{skill_name}",
            {"description": "after"},
            agent_name="writer",
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["description"] == "after"
        assert updated.json()["shared"] is False
        assert updated.json()["privileged_tool_opt_in"] is False
        assert updated.json()["origin_agent"] == "writer"

        peer_denied = _signed_request(
            client,
            "PUT",
            f"/skills/{skill_name}",
            {"description": "peer replacement"},
            agent_name="peer",
        )
        assert peer_denied.status_code == 403, peer_denied.text

        operator_update = client.put(
            f"/skills/{skill_name}",
            json={"description": "operator update", "shared": True},
        )
        assert operator_update.status_code == 200, operator_update.text
        assert operator_update.json()["origin_agent"] == "writer"

        belt_denied = _signed_request(
            client,
            "PUT",
            f"/skills/{skill_name}",
            {"description": "signed shared-row replacement"},
            agent_name="writer",
        )
        assert belt_denied.status_code == 403, belt_denied.text
    finally:
        client.close()


@pytest.mark.parametrize("source", ["from-md", "discover"])
def test_signed_filesystem_skill_creation_persists_origin(monkeypatch, tmp_path, source):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = f"signed-{source}"
    try:
        _register_agent(client, tmp_path, "writer")
        content = _skill_md(skill_name, allowed_tools="Read")
        if source == "from-md":
            created = _signed_request(
                client,
                "POST",
                "/skills/from-md",
                {"content": content},
                agent_name="writer",
            )
        else:
            skill_dir = tmp_path / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            created = _signed_request(
                client,
                "POST",
                "/skills/discover",
                agent_name="writer",
            )

        assert created.status_code == 200, created.text
        catalog = client.get(f"/skills/{skill_name}").json()
        assert catalog["origin_agent"] == "writer"
        assert catalog["shared"] is False
        assert catalog["self_assignable"] is False
    finally:
        client.close()


def test_refused_from_git_leaves_no_files_and_discovery_preserves_operator_row(
    monkeypatch, tmp_path
):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = "guarded-git-skill"
    created_targets = _stub_git_clone(monkeypatch, skill_name=skill_name)
    try:
        _register_agent(client, tmp_path, "writer")
        created = client.post(
            "/skills",
            json={
                "name": skill_name,
                "description": "operator-owned",
                "skill_type": "skill",
                "tool_patterns": ["Bash"],
                "self_assignable": True,
                "shared": True,
            },
        )
        assert created.status_code == 200, created.text
        before = client.get(f"/skills/{skill_name}").json()

        refused = _signed_request(
            client,
            "POST",
            "/skills/from-git",
            {"url": "https://github.com/test/guarded-repo"},
            agent_name="writer",
        )
        discovered = discover_all_skills(project_root=str(tmp_path))
        register_discovered_skills(skill_routes._skills, discovered)
        after = client.get(f"/skills/{skill_name}").json()

        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == "operator-owned skill"
        assert created_targets
        assert all(not target.exists() for target in created_targets)
        assert after == before
    finally:
        client.close()


def test_failed_from_git_clone_removes_staging_files(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    created_targets = _stub_git_clone(
        monkeypatch,
        skill_name="failed-clone",
        fail_after_write=True,
    )
    try:
        failed = client.post(
            "/skills/from-git",
            json={"url": "https://github.com/test/failed-clone"},
        )

        assert failed.status_code == 400, failed.text
        assert created_targets
        assert all(not target.exists() for target in created_targets)
    finally:
        client.close()


def test_signed_from_git_origin_and_clamps_survive_boot_discovery(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = "signed-git-skill"
    _stub_git_clone(monkeypatch, skill_name=skill_name, allowed_tools="Read")
    try:
        _register_agent(client, tmp_path, "writer")
        installed = _signed_request(
            client,
            "POST",
            "/skills/from-git",
            {"url": "https://github.com/test/signed-git-repo"},
            agent_name="writer",
        )
        assert installed.status_code == 200, installed.text

        skill_md = tmp_path / "skills" / "signed-git-repo" / "SKILL.md"
        assert skill_md.is_file()
        skill_md.write_text(
            _skill_md(
                skill_name,
                allowed_tools="Read",
                body="Changed before boot discovery.",
            ),
            encoding="utf-8",
        )
        discovered = discover_all_skills(project_root=str(tmp_path))
        result = register_discovered_skills(skill_routes._skills, discovered)
        catalog = client.get(f"/skills/{skill_name}").json()

        assert result["updated"] == [skill_name]
        assert catalog["origin_agent"] == "writer"
        assert catalog["shared"] is False
        assert catalog["privileged_tool_opt_in"] is False
        assert catalog["self_assignable"] is False
    finally:
        client.close()


def test_operator_put_resets_opt_in_only_when_tool_set_changes(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = "operator-pattern-update"
    try:
        created = client.post(
            "/skills",
            json={
                "name": skill_name,
                "tool_patterns": ["Bash"],
                "self_assignable": True,
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["privileged_tool_opt_in"] is True

        same_set = client.put(
            f"/skills/{skill_name}",
            json={"tool_patterns": ["Bash"]},
        )
        assert same_set.status_code == 200, same_set.text
        assert same_set.json()["privileged_tool_opt_in"] is True
        assert same_set.json()["self_assignable"] is True

        widened = client.put(
            f"/skills/{skill_name}",
            json={"tool_patterns": ["Bash", "Write"]},
        )
        assert widened.status_code == 200, widened.text
        assert widened.json()["privileged_tool_opt_in"] is False
        assert widened.json()["self_assignable"] is False
    finally:
        client.close()


def test_enabled_assignment_refuses_globally_disabled_catalog_skill(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = "globally-disabled-skill"
    try:
        _register_agent(client, tmp_path, "target")
        created = client.post(
            "/skills",
            json={
                "name": skill_name,
                "enabled": False,
                "tool_patterns": ["Read"],
                "self_assignable": True,
            },
        )
        assert created.status_code == 200, created.text

        refused = client.post(
            f"/agents/target/skills/{skill_name}",
            json={"assigned_by": "user"},
        )

        assert refused.status_code == 400, refused.text
        assert _assignment_row("target", skill_name) is None
    finally:
        client.close()


def _seed_hostile_disabled_self_assignment(client: TestClient, skill_name: str) -> None:
    store = skill_routes._skills
    store.register(
        skill_name,
        tool_patterns=["Bash"],
        self_assignable=True,
        privileged_tool_opt_in=True,
    )
    store._db.execute(
        "UPDATE skills SET self_assignable=1, privileged_tool_opt_in=0 WHERE name=?",
        (skill_name,),
    )
    store._db.execute(
        """INSERT INTO agent_skills
           (agent_name, skill_name, enabled, assigned_by, config_overrides, assigned_at)
           VALUES (?, ?, 0, 'self', '{}', ?)""",
        ("tenant", skill_name, time.time()),
    )
    store._db.commit()


def _assignment_enabled(skill_name: str) -> int:
    row = skill_routes._skills._db.execute(
        "SELECT enabled FROM agent_skills WHERE agent_name=? AND skill_name=?",
        ("tenant", skill_name),
    ).fetchone()
    assert row is not None
    return row[0]


def test_self_enable_uses_effective_self_assignable_and_preserves_disabled_row(
    monkeypatch, tmp_path
):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = "hostile-enable-row"
    try:
        _seed_hostile_disabled_self_assignment(client, skill_name)

        denied = _signed_request(
            client,
            "POST",
            f"/agents/tenant/skills/{skill_name}/enable",
        )

        assert denied.status_code == 403, denied.text
        assert _assignment_enabled(skill_name) == 0
    finally:
        client.close()


def test_self_assign_uses_effective_self_assignable_and_returns_403(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    skill_name = "hostile-assign-row"
    try:
        _seed_hostile_disabled_self_assignment(client, skill_name)

        denied = _signed_request(
            client,
            "POST",
            f"/agents/tenant/skills/{skill_name}",
            {"assigned_by": "user"},
        )

        assert denied.status_code == 403, denied.text
        assert _assignment_enabled(skill_name) == 0
    finally:
        client.close()


@pytest.mark.parametrize("control", ["\x00", "\x1b"])
def test_classifier_rejects_all_control_characters_inside_specifier(control):
    from pinky_daemon.skill_tool_policy import (
        ToolPatternValidationError,
        classify_tool_pattern,
    )

    with pytest.raises(ToolPatternValidationError):
        classify_tool_pattern(
            f"Bash(git{control}status:*)",
            skill_name="control-probe",
            mcp_server_config={},
            skill_type="custom",
        )


def test_baseline_classifier_matches_streaming_allowlist_except_dead_alias():
    """Pin baseline parity after excluding the documented dead memory alias."""
    from pinky_daemon.skill_tool_policy import (
        _BASELINE_MCP_PREFIXES,
        _BASELINE_TOOLS,
        classify_tool_pattern,
    )
    from pinky_daemon.streaming_session import DEFAULT_STREAMING_ALLOWED_TOOLS

    dead_alias = "mcp__memory__*"
    # The default still carries this dead alias pending #1213. It must not become
    # a classifier exemption for a skill claiming that server name.
    baseline_patterns = [
        pattern for pattern in DEFAULT_STREAMING_ALLOWED_TOOLS if pattern != dead_alias
    ]
    classifications = [
        classify_tool_pattern(
            pattern,
            skill_name="serverless-custom",
            mcp_server_config={},
            skill_type="custom",
        )
        for pattern in baseline_patterns
    ]

    assert all(not classification.privileged for classification in classifications)
    assert (
        classify_tool_pattern(
            dead_alias,
            skill_name="memory",
            mcp_server_config={},
            skill_type="custom",
        ).privileged
        is True
    )
    assert len(_BASELINE_TOOLS) + len(_BASELINE_MCP_PREFIXES) == len(
        baseline_patterns
    )
