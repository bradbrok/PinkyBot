"""Follow-up regression tests for stage-2 skill tool-grant policy."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.routes import skills as skill_routes

pytestmark = pytest.mark.real_auth


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


def _skill_md(name: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: privileged upload fixture\n"
        "allowed-tools: Bash\n"
        "---\n"
        "Privileged upload fixture.\n"
    )


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

    def fake_git_run(args, **kwargs):
        target = Path(args[2]) if "-C" in args else Path(args[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(_skill_md(target.name), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_git_run)
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
        client.app.state.agents.register(
            "internal",
            model="opus",
            working_dir=str(tmp_path / "internal"),
        )
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

        assert internal_opt_in.status_code == 200, internal_opt_in.text
        assert internal_opt_in.json()["self_assignable"] is False
        assert internal_opt_in.json()["privileged_tool_opt_in"] is False
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


def test_baseline_classifier_matches_default_streaming_allowlist():
    from pinky_daemon.skill_tool_policy import (
        _BASELINE_MCP_PREFIXES,
        _BASELINE_TOOLS,
        classify_tool_pattern,
    )
    from pinky_daemon.streaming_session import DEFAULT_STREAMING_ALLOWED_TOOLS

    classifications = [
        classify_tool_pattern(
            pattern,
            skill_name="serverless-custom",
            mcp_server_config={},
            skill_type="custom",
        )
        for pattern in DEFAULT_STREAMING_ALLOWED_TOOLS
    ]

    assert all(not classification.privileged for classification in classifications)
    assert len(_BASELINE_TOOLS) + len(_BASELINE_MCP_PREFIXES) == len(
        DEFAULT_STREAMING_ALLOWED_TOOLS
    )
