"""Stage-2 skill tool-grant policy regression tests (#691 PR-B)."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.skill_loader import ParsedSkill, parse_skill_md, register_discovered_skills
from pinky_daemon.skill_store import SkillStore


@pytest.fixture
def store(tmp_path):
    value = SkillStore(db_path=str(tmp_path / "skills.db"))
    yield value
    value.close()


def _write_skill_md(tmp_path, allowed_tools_yaml: str):
    skill_dir = tmp_path / "policy-fixture"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\n"
        "name: policy-fixture\n"
        "description: policy parser fixture\n"
        f"allowed-tools: {allowed_tools_yaml}\n"
        "---\n"
        "Policy fixture body.\n",
        encoding="utf-8",
    )
    return parse_skill_md(path)


def test_classifier_accepts_contract_grammar_and_classifies_marginal_power():
    from pinky_daemon.skill_tool_policy import classify_tool_pattern

    cases = [
        ("Read", "reader", {}, "skill", "Read", False),
        ("Agent(worker role)", "agents", {}, "skill", "Agent", False),
        (
            "mcp__pinky-self__create_task",
            "planner",
            {},
            "skill",
            "mcp__pinky-self__create_task",
            False,
        ),
        ("Bash", "shell", {}, "skill", "Bash", True),
        ("Bash(git log:*)", "shell", {}, "skill", "Bash", True),
        (
            "Bash(git log --format=%h,%s:*)",
            "shell",
            {},
            "skill",
            "Bash",
            True,
        ),
        ("mcp__calendar__*", "calendar", {"command": "calendar"}, "skill", "mcp__calendar__*", False),
        (
            "mcp__configured-name__*",
            "calendar",
            {"command": "calendar", "name": "configured-name"},
            "skill",
            "mcp__configured-name__*",
            True,
        ),
        (
            "mcp__plugin-demo__*",
            "demo",
            {"command": "demo"},
            "plugin",
            "mcp__plugin-demo__*",
            False,
        ),
        ("plugin_demo_search", "demo", {}, "plugin", "plugin_demo_search", False),
    ]
    for pattern, skill_name, mcp_config, skill_type, tool_name, privileged in cases:
        result = classify_tool_pattern(
            pattern,
            skill_name=skill_name,
            mcp_server_config=mcp_config,
            skill_type=skill_type,
        )

        assert result.tool_name == tool_name
        assert result.privileged is privileged


def test_classifier_rejects_corrupt_or_ambiguous_tokens():
    from pinky_daemon.skill_tool_policy import ToolPatternValidationError, classify_tool_pattern

    patterns = [
        "",
        " Read",
        "Read ",
        "Read\n",
        "Read Grep",
        "Read,Grep",
        "['mcp__pinky-self__create_task',",
        '"Read"',
        "'Read'",
        "Bash(git log:*",
        "Bash(git log:*))",
        "Bash(git [log]:*)",
        "Bash(git 'log':*)",
        "Bash(git log:*)trailer",
    ]
    for pattern in patterns:
        with pytest.raises(ToolPatternValidationError):
            classify_tool_pattern(
                pattern,
                skill_name="policy-fixture",
                mcp_server_config={},
                skill_type="skill",
            )


def test_parser_accepts_yaml_list_without_stringifying_it(tmp_path):
    parsed = _write_skill_md(
        tmp_path,
        "\n  - mcp__pinky-self__create_task\n  - mcp__pinky-self__complete_task",
    )

    assert parsed is not None
    assert parsed.allowed_tools == [
        "mcp__pinky-self__create_task",
        "mcp__pinky-self__complete_task",
    ]


def test_parser_accepts_space_delimited_scalar_with_balanced_specifier(tmp_path):
    parsed = _write_skill_md(tmp_path, '"Read Bash(git log:*) mcp__pinky-self__create_task"')

    assert parsed is not None
    assert parsed.allowed_tools == [
        "Read",
        "Bash(git log:*)",
        "mcp__pinky-self__create_task",
    ]


def test_agent_originated_baseline_pattern_still_revokes_self_grant(store):
    """Stage 2 must not weaken #1206's ANY-capability predicate."""
    store.register("baseline-only", tool_patterns=["Read"], self_assignable=True)
    assert store.assign_to_agent("alice", "baseline-only", assigned_by="self") is True

    skill = store.register(
        "baseline-only",
        tool_patterns=["Read"],
        self_assignable=True,
        agent_originated=True,
    )
    assignment = store._db.execute(
        "SELECT enabled FROM agent_skills WHERE agent_name=? AND skill_name=?",
        ("alice", "baseline-only"),
    ).fetchone()

    assert skill.self_assignable is False
    assert assignment == (0,)


def _ensure_test_opt_in_column(store):
    columns = {row[1] for row in store._db.execute("PRAGMA table_info(skills)")}
    if "privileged_tool_opt_in" not in columns:
        store._db.execute(
            "ALTER TABLE skills ADD COLUMN privileged_tool_opt_in INTEGER NOT NULL DEFAULT 0"
        )
        store._db.commit()


def test_assignment_gate_recomputes_privilege_instead_of_trusting_flag(store):
    store.register("hostile-row", tool_patterns=["Bash"], self_assignable=True)
    _ensure_test_opt_in_column(store)
    store._db.execute(
        "UPDATE skills SET self_assignable=1, privileged_tool_opt_in=0 WHERE name=?",
        ("hostile-row",),
    )
    store._db.commit()

    assert store.assign_to_agent("alice", "hostile-row", assigned_by="self") is False
    assert store.is_assigned("alice", "hostile-row") is False


def test_assignment_gate_honors_explicit_privileged_opt_in(store):
    store.register("opted-in", tool_patterns=["Bash"], self_assignable=True)
    _ensure_test_opt_in_column(store)
    store._db.execute(
        "UPDATE skills SET self_assignable=1, privileged_tool_opt_in=1 WHERE name=?",
        ("opted-in",),
    )
    store._db.commit()

    assert store.assign_to_agent("alice", "opted-in", assigned_by="self") is True
    assert store.is_assigned("alice", "opted-in") is True


def test_merge_drops_privileged_grants_with_absent_provenance():
    from pinky_daemon.skill_tool_policy import filter_skill_tool_grants

    for assigned_by in (None, "", "unknown"):
        warnings = []
        filtered = filter_skill_tool_grants(
            [
                {
                    "skill_name": "provenance-probe",
                    "pattern": "Bash",
                    "assigned_by": assigned_by,
                    "privileged_tool_opt_in": True,
                    "mcp_server_config": {},
                    "skill_type": "skill",
                }
            ],
            agent_name="alice",
            warn=warnings.append,
        )

        assert filtered == []
        assert len(warnings) == 1
        warning = warnings[0]
        assert "alice" in warning
        assert "provenance-probe" in warning
        assert "Bash" in warning


def test_merge_self_privileged_grant_is_gated_by_persisted_opt_in():
    from pinky_daemon.skill_tool_policy import filter_skill_tool_grants

    for opt_in, expected in ((False, []), (True, ["Bash"])):
        warnings = []
        filtered = filter_skill_tool_grants(
            [
                {
                    "skill_name": "self-probe",
                    "pattern": "Bash",
                    "assigned_by": "self",
                    "privileged_tool_opt_in": opt_in,
                    "mcp_server_config": {},
                    "skill_type": "skill",
                }
            ],
            agent_name="alice",
            warn=warnings.append,
        )

        assert filtered == expected
        assert bool(warnings) is (not opt_in)


@pytest.mark.parametrize(
    ("assigned_by", "opt_in", "expected"),
    [
        ("user", False, ["Bash"]),
        ("self", False, []),
        ("peer-agent", False, []),
        ("SELF", False, []),
        ("system", False, []),
        ("shared", False, []),
        ("", False, []),
        (None, False, []),
        ("self", True, ["Bash"]),
        ("peer-agent", True, ["Bash"]),
    ],
)
def test_merge_requires_user_provenance_or_opt_in_for_privileged_grants(
    assigned_by, opt_in, expected
):
    from pinky_daemon.skill_tool_policy import filter_skill_tool_grants

    assert filter_skill_tool_grants(
        [
            {
                "skill_name": "provenance-probe",
                "pattern": "Bash",
                "assigned_by": assigned_by,
                "privileged_tool_opt_in": opt_in,
                "mcp_server_config": {},
                "skill_type": "skill",
            }
        ],
        agent_name="alice",
    ) == expected


def test_discovery_repairs_patterns_even_when_body_is_unchanged(store):
    broken = ["['mcp__pinky-self__create_task',", "'mcp__pinky-self__complete_task']"]
    repaired = ["mcp__pinky-self__create_task", "mcp__pinky-self__complete_task"]
    store.register(
        "project-management",
        description="project fixture",
        skill_type="skill",
        directive="same body",
        tool_patterns=broken,
        self_assignable=True,
    )
    parsed = ParsedSkill(
        name="project-management",
        description="project fixture",
        body="same body",
        location="/snapshot/skills/project-management/SKILL.md",
        base_dir="/snapshot/skills/project-management",
        allowed_tools=repaired,
    )

    result = register_discovered_skills(store, [parsed], overwrite=False)

    assert result == {"registered": [], "skipped": [], "updated": ["project-management"]}
    assert store.get("project-management").tool_patterns == repaired


def test_discovery_invalidates_opt_in_when_privileged_patterns_change(store):
    store.register(
        "operator-reviewed",
        description="before",
        skill_type="skill",
        directive="before body",
        tool_patterns=["Bash"],
        self_assignable=True,
        privileged_tool_opt_in=True,
    )
    changed = ParsedSkill(
        name="operator-reviewed",
        description="after",
        body="after body",
        location="/snapshot/skills/operator-reviewed/SKILL.md",
        base_dir="/snapshot/skills/operator-reviewed",
        allowed_tools=["Bash(git log:*)"],
    )
    fresh = ParsedSkill(
        name="fresh-privileged",
        description="fresh",
        body="fresh body",
        location="/snapshot/skills/fresh-privileged/SKILL.md",
        base_dir="/snapshot/skills/fresh-privileged",
        allowed_tools=["Bash"],
    )

    result = register_discovered_skills(store, [changed, fresh], overwrite=False)
    reviewed = store.get("operator-reviewed")
    discovered = store.get("fresh-privileged")

    assert result["updated"] == ["operator-reviewed"]
    assert result["registered"] == ["fresh-privileged"]
    assert reviewed.self_assignable is False
    assert reviewed.privileged_tool_opt_in is False
    assert discovered.self_assignable is False
    assert discovered.privileged_tool_opt_in is False


def test_register_inherits_opt_in_only_for_unchanged_pattern_set(store):
    store.register(
        "operator-reviewed",
        tool_patterns=["Bash"],
        self_assignable=True,
        privileged_tool_opt_in=True,
    )

    unchanged = store.register(
        "operator-reviewed",
        tool_patterns=["Bash"],
        self_assignable=True,
        privileged_tool_opt_in=None,
    )
    widened = store.register(
        "operator-reviewed",
        tool_patterns=["Bash", "Write"],
        self_assignable=True,
        privileged_tool_opt_in=None,
    )

    assert unchanged.self_assignable is True
    assert unchanged.privileged_tool_opt_in is True
    assert widened.self_assignable is False
    assert widened.privileged_tool_opt_in is False


def _create_legacy_policy_database(path):
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE skills (
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            skill_type TEXT NOT NULL DEFAULT 'custom',
            version TEXT NOT NULL DEFAULT '0.1.0',
            enabled INTEGER NOT NULL DEFAULT 1,
            config TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            mcp_server_config TEXT NOT NULL DEFAULT '{}',
            tool_patterns TEXT NOT NULL DEFAULT '[]',
            directive TEXT NOT NULL DEFAULT '',
            requires TEXT NOT NULL DEFAULT '[]',
            self_assignable INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT 'general',
            shared INTEGER NOT NULL DEFAULT 0,
            file_templates TEXT NOT NULL DEFAULT '{}',
            default_config TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE agent_skills (
            agent_name TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            assigned_by TEXT NOT NULL DEFAULT 'user',
            config_overrides TEXT NOT NULL DEFAULT '{}',
            assigned_at REAL NOT NULL,
            PRIMARY KEY (agent_name, skill_name),
            FOREIGN KEY (skill_name) REFERENCES skills(name) ON DELETE CASCADE
        );
        """
    )
    skill_rows = [
        ("code-editing", ["Edit", "Write", "Bash"]),
        ("file-access", ["Read", "Glob", "Grep"]),
    ]
    for name, patterns in skill_rows:
        db.execute(
            """INSERT INTO skills
               (name, description, skill_type, enabled, config, created_at, updated_at,
                mcp_server_config, tool_patterns, self_assignable)
               VALUES (?, ?, 'skill', 1, '{}', 1, 1, '{}', ?, 1)""",
            (name, name, json.dumps(patterns)),
        )
    db.executemany(
        """INSERT INTO agent_skills
           (agent_name, skill_name, enabled, assigned_by, config_overrides, assigned_at)
           VALUES (?, ?, 1, ?, '{}', 1)""",
        [
            ("alice", "code-editing", "self"),
            ("bob", "code-editing", "user"),
            ("alice", "file-access", "self"),
        ],
    )
    db.commit()
    db.close()


def test_migration_flips_privileged_catalog_and_self_rows_only_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy-skills.db"
    _create_legacy_policy_database(path)

    migrated = SkillStore(db_path=str(path))
    first = migrated._db.execute(
        """SELECT s.name, s.self_assignable, s.privileged_tool_opt_in,
                  a.agent_name, a.assigned_by, a.enabled
           FROM skills s LEFT JOIN agent_skills a ON a.skill_name=s.name
           ORDER BY s.name, a.agent_name"""
    ).fetchall()
    migrated.close()

    assert first == [
        ("code-editing", 0, 0, "alice", "self", 0),
        ("code-editing", 0, 0, "bob", "user", 1),
        ("file-access", 1, 0, "alice", "self", 1),
    ]

    reopened = SkillStore(db_path=str(path))
    second = reopened._db.execute(
        """SELECT s.name, s.self_assignable, s.privileged_tool_opt_in,
                  a.agent_name, a.assigned_by, a.enabled
           FROM skills s LEFT JOIN agent_skills a ON a.skill_name=s.name
           ORDER BY s.name, a.agent_name"""
    ).fetchall()
    reopened.close()

    assert second == first


def _make_policy_client(monkeypatch, tmp_path):
    from pinky_daemon.api import create_api

    monkeypatch.setenv("PINKY_SESSION_SECRET", "policy-secret")
    monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
    app = create_api(
        max_sessions=10,
        default_working_dir=str(tmp_path),
        db_path=str(tmp_path / "conversations.db"),
    )
    client = TestClient(app)
    setup = client.post(
        "/auth/setup",
        json={"password": "hunter22", "next": "/"},
    )
    assert setup.status_code == 200, setup.text
    return client


def test_api_rejects_malformed_update_before_mutating_existing_row(monkeypatch, tmp_path):
    client = _make_policy_client(monkeypatch, tmp_path)
    try:
        created = client.post(
            "/skills",
            json={
                "name": "atomic-policy",
                "description": "before",
                "tool_patterns": ["Read"],
                "self_assignable": True,
            },
        )
        assert created.status_code == 200, created.text
        before = client.get("/skills/atomic-policy").json()

        response = client.put(
            "/skills/atomic-policy",
            json={
                "description": "must not commit",
                "tool_patterns": ["['mcp__pinky-self__create_task',"],
            },
        )

        assert response.status_code == 422, response.text
        assert client.get("/skills/atomic-policy").json() == before
    finally:
        client.close()


def test_api_operator_opt_in_must_be_explicit_and_survives_unrelated_update(
    monkeypatch, tmp_path
):
    client = _make_policy_client(monkeypatch, tmp_path)
    try:
        implicit = client.post(
            "/skills",
            json={"name": "implicit-privileged", "tool_patterns": ["Bash"]},
        )
        explicit = client.post(
            "/skills",
            json={
                "name": "explicit-privileged",
                "tool_patterns": ["Bash(git log:*)"],
                "self_assignable": True,
            },
        )
        updated = client.put(
            "/skills/explicit-privileged",
            json={"description": "changed without replaying consent"},
        )

        assert implicit.status_code == 200, implicit.text
        assert implicit.json()["self_assignable"] is False
        assert implicit.json()["privileged_tool_opt_in"] is False
        assert explicit.status_code == 200, explicit.text
        assert explicit.json()["self_assignable"] is True
        assert explicit.json()["privileged_tool_opt_in"] is True
        assert updated.status_code == 200, updated.text
        assert updated.json()["self_assignable"] is True
        assert updated.json()["privileged_tool_opt_in"] is True
    finally:
        client.close()


def test_streaming_merge_enforces_provenance_and_opt_in_effects(
    monkeypatch, tmp_path, capsys
):
    from pinky_daemon import streaming_session
    from pinky_daemon.routes import skills as skill_routes
    from pinky_daemon.transport_state import SessionState

    async def fake_connect(self):
        self._state_machine._state = SessionState.CONNECTED

    monkeypatch.setattr(streaming_session.StreamingSession, "connect", fake_connect)
    client = _make_policy_client(monkeypatch, tmp_path)
    try:
        created = client.post(
            "/agents",
            json={
                "name": "policy-agent",
                "model": "sonnet",
                "working_dir": str(tmp_path / "policy-agent"),
            },
        )
        assert created.status_code == 200, created.text
        policy_store = skill_routes._skills
        _ensure_test_opt_in_column(policy_store)
        cases = [
            ("self-denied", "Bash", "self", 0),
            ("self-opted-in", "Write", "self", 1),
            ("unknown-denied", "Edit", "unknown", 1),
            ("operator-pass", "mcp__operator-probe__*", "user", 0),
            ("peer-pass", "mcp__peer-probe__*", "peer-agent", 0),
            ("baseline-self-pass", "mcp__pinky-self__create_task", "self", 0),
            ("malformed-denied", "['mcp__bad__*',", "user", 0),
        ]
        for skill_name, pattern, assigned_by, opt_in in cases:
            policy_store.register(
                skill_name,
                tool_patterns=["Read"],
                self_assignable=False,
            )
            policy_store._db.execute(
                """UPDATE skills
                   SET tool_patterns=?, self_assignable=1, privileged_tool_opt_in=?
                   WHERE name=?""",
                (json.dumps([pattern]), opt_in, skill_name),
            )
            policy_store._db.execute(
                """INSERT INTO agent_skills
                   (agent_name, skill_name, enabled, assigned_by, config_overrides, assigned_at)
                   VALUES ('policy-agent', ?, 1, ?, '{}', 1)""",
                (skill_name, assigned_by),
            )
        policy_store._db.commit()

        capsys.readouterr()
        woke = client.post("/agents/policy-agent/wake?prompt=policy")
        assert woke.status_code == 200, woke.text
        session = client.app.state.broker._streaming["policy-agent"]["main"]
        effective = session._config.allowed_tools

        assert "Bash" not in effective
        assert "Write" in effective
        assert "Edit" not in effective
        assert "mcp__operator-probe__*" in effective
        assert "mcp__peer-probe__*" in effective
        assert "mcp__pinky-self__create_task" in effective
        assert "['mcp__bad__*'," not in effective

        warnings = capsys.readouterr().err
        for skill_name, pattern in [
            ("self-denied", "Bash"),
            ("unknown-denied", "Edit"),
            ("malformed-denied", "['mcp__bad__*',"),
        ]:
            assert "policy-agent" in warnings
            assert skill_name in warnings
            assert pattern in warnings
    finally:
        client.close()


def test_own_server_exemption_uses_materialized_skill_name_not_config_name(store):
    own = store.register(
        "calendar",
        skill_type="skill",
        mcp_server_config={"command": "calendar", "name": "configured-name"},
        tool_patterns=["mcp__calendar__*"],
        self_assignable=True,
    )
    wrong = store.register(
        "wrong-prefix",
        skill_type="skill",
        mcp_server_config={"command": "calendar", "name": "configured-name"},
        tool_patterns=["mcp__configured-name__*"],
        self_assignable=True,
    )
    assert store.assign_to_agent("alice", "calendar", assigned_by="self") is True
    materialized = store.materialize_for_agent("alice")

    assert own.self_assignable is True
    assert own.privileged_tool_opt_in is False
    assert "calendar" in materialized["mcp_servers"]
    assert "mcp__calendar__*" in materialized["tool_patterns"]
    assert wrong.self_assignable is False
    assert wrong.privileged_tool_opt_in is False
