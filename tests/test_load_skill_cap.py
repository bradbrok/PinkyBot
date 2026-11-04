"""load_skill with an optional per-agent skill load cap (tool, setting, prompt)."""

from __future__ import annotations

import io
import json
import sqlite3
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry
from pinky_self.server import create_server
from tests.skill_sections_support import FIXTURES, LONG, today_output

AGENT = "cap-tester"
TOOL_DESCRIPTION = (
    "Load a skill's instructions. Usually returns the whole skill. If your agent has a skill "
    "load cap and the skill is longer, you get its contents list, the first part and every "
    "section marked [always]; pass section=\"<number or heading>\" for another part, or "
    "section=\"all\" for everything."
)


def _tool():
    srv = create_server(agent_name=AGENT, api_url="http://localhost:9999", tool_gates=[])
    return next(t for t in srv._tool_manager.list_tools() if t.name == "load_skill")


class FakeApi:
    """Answers GET /skills/{name} and GET /agents/{AGENT}; records every request."""

    def __init__(self, skill, agent):
        self.skill = skill
        self.agent = agent
        self.calls = []

    def __call__(self, req, timeout=30):
        path = urllib.parse.urlsplit(req.full_url).path
        self.calls.append((req.get_method(), path))
        if path.startswith("/skills/"):
            payload = self.skill
        elif path == f"/agents/{AGENT}":
            payload = self.agent
        else:
            raise AssertionError(f"unexpected request {path}")
        if isinstance(payload, BaseException):
            raise payload
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        return io.BytesIO(payload)


def _run(name, description, directive, agent, **kwargs):
    api = FakeApi({"name": name, "description": description, "directive": directive}, agent)
    with patch("urllib.request.urlopen", side_effect=api):
        out = _tool().fn(skill_name=name, **kwargs)
    return out, api.calls


def _http_error(code):
    return urllib.error.HTTPError(
        "http://localhost:9999/x", code, "error", {}, io.BytesIO(b"fixture error")
    )


# ── Tool: unchanged output ─────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs", [{}, {"section": ""}], ids=["no-section", "empty-section"])
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_cap_zero_is_byte_identical_to_today(name, kwargs):
    description, directive = FIXTURES[name]
    out, _ = _run(name, description, directive, {"name": AGENT, "skill_load_cap": 0}, **kwargs)
    assert out == today_output(name, description, directive)


@pytest.mark.parametrize("cap", [0, 2000, 20000])
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_section_all_is_byte_identical_at_any_cap(name, cap):
    description, directive = FIXTURES[name]
    out, _ = _run(name, description, directive, {"skill_load_cap": cap}, section="all")
    assert out == today_output(name, description, directive)


def test_not_found_and_empty_texts_unchanged():
    for kwargs in ({}, {"section": "all"}, {"section": "2"}):
        api = FakeApi(_http_error(404), {"skill_load_cap": 2000})
        with patch("urllib.request.urlopen", side_effect=api):
            assert _tool().fn(skill_name="missing", **kwargs) == "Skill 'missing' not found."
        api = FakeApi({"name": "empty", "directive": ""}, {"skill_load_cap": 2000})
        with patch("urllib.request.urlopen", side_effect=api):
            assert _tool().fn(skill_name="empty", **kwargs) == (
                "Skill 'empty' has no instructions body."
            )


def test_tool_description_and_parameters():
    tool = _tool()
    assert tool.description == TOOL_DESCRIPTION
    assert list(tool.parameters["properties"]) == ["skill_name", "section"]
    assert tool.parameters["properties"]["section"]["default"] == ""
    assert tool.parameters["required"] == ["skill_name"]


# ── Tool: capped output ────────────────────────────────────────────────────


def test_cap_set_returns_contents_and_first_part():
    from pinky_daemon.skill_sections import render_default

    out, calls = _run("long", "A long skill", LONG, {"skill_load_cap": 20000})
    assert out == render_default("long", "A long skill", LONG, 20000)
    assert out.startswith("# Skill: long\nA long skill\n\n[Partial load: ")
    assert "5. Reference - " in out
    assert "\n## Rules [always]\n" in out
    assert "\n## Reference\n" not in out
    assert calls == [("GET", "/skills/long"), ("GET", f"/agents/{AGENT}")]


@pytest.mark.parametrize(
    "agent",
    [
        _http_error(500),
        _http_error(403),
        OSError("connection refused"),
        {"error": "fixture"},
        {"name": AGENT},
        {"name": AGENT, "skill_load_cap": None},
        {"name": AGENT, "skill_load_cap": "20000"},
        {"name": AGENT, "skill_load_cap": 1999},
        {"name": AGENT, "skill_load_cap": 200001},
        {"name": AGENT, "skill_load_cap": -1},
        {"name": AGENT, "skill_load_cap": True},
        {"name": AGENT, "skill_load_cap": 20000.0},
        [AGENT],
        b"not json",
    ],
    ids=[
        "http-500", "http-403", "network", "error-body", "missing", "null", "string",
        "1999", "200001", "negative", "bool", "float", "list", "not-json",
    ],
)
def test_cap_read_failure_gives_full_text(agent):
    assert len(LONG) > 20000
    out, calls = _run("long", "A long skill", LONG, agent)
    assert out == today_output("long", "A long skill", LONG)
    assert calls == [("GET", "/skills/long"), ("GET", f"/agents/{AGENT}")]


@pytest.mark.parametrize("section", ["all", "1", "Setup", "Nope", " "])
def test_cap_is_read_only_for_the_default_load(section):
    out, calls = _run(
        "long", "A long skill", LONG, AssertionError("cap must not be read"), section=section
    )
    assert calls == [("GET", "/skills/long")]


@pytest.mark.parametrize("cap", [0, 2000])
def test_section_argument_works_at_any_cap(cap):
    out, _ = _run("long", "A long skill", LONG, {"skill_load_cap": cap}, section="Setup")
    assert out.startswith("# Skill: long - section 2 of 5: Setup\n\n## Setup\n")
    out, _ = _run("long", "A long skill", LONG, {"skill_load_cap": cap}, section="4")
    assert out.startswith("# Skill: long - section 4 of 5: Rules [always]\n\n## Rules [always]\n")


# ── Setting: registry and API ──────────────────────────────────────────────


@pytest.fixture
def registry(tmp_path):
    reg = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    yield reg
    reg.close()


def test_new_agent_defaults_to_zero(registry, tmp_path):
    agent = registry.register(AGENT, working_dir=str(tmp_path / "agent"))
    assert agent.skill_load_cap == 0
    assert agent.to_dict()["skill_load_cap"] == 0


def test_existing_db_migrates_to_zero(tmp_path):
    db_path = tmp_path / "agents.db"
    legacy = AgentRegistry(db_path=str(db_path))
    try:
        legacy.register(AGENT, display_name="Legacy", working_dir=str(tmp_path / "agent"))
    finally:
        legacy.close()
    with sqlite3.connect(db_path) as db:
        if "skill_load_cap" in {row[1] for row in db.execute("PRAGMA table_info(agents)")}:
            db.execute("ALTER TABLE agents DROP COLUMN skill_load_cap")
    upgraded = AgentRegistry(db_path=str(db_path))
    try:
        columns = {row[1]: row for row in upgraded._db.execute("PRAGMA table_info(agents)")}
        assert columns["skill_load_cap"][2:5] == ("INTEGER", 1, "0")
        assert upgraded.get(AGENT).skill_load_cap == 0
        assert upgraded.get(AGENT).display_name == "Legacy"
    finally:
        upgraded.close()


def test_registry_roundtrip(registry, tmp_path):
    registry.register(AGENT, working_dir=str(tmp_path / "agent"))
    registry.update(AGENT, skill_load_cap=20000)
    assert registry.get(AGENT).skill_load_cap == 20000
    registry.register(AGENT, display_name="Other change")
    assert registry.get(AGENT).skill_load_cap == 20000
    registry.register(AGENT, skill_load_cap=0)
    assert registry.get(AGENT).skill_load_cap == 0


def test_registry_create_with_cap(registry, tmp_path):
    agent = registry.register(AGENT, working_dir=str(tmp_path / "agent"), skill_load_cap=2000)
    assert agent.skill_load_cap == 2000
    assert registry.get(AGENT).to_dict()["skill_load_cap"] == 2000


@pytest.mark.parametrize("method", ["register", "reregister", "update"])
@pytest.mark.parametrize("cap", [1, 1999, 200001, -1, True, "x", 2000.0, None])
def test_registry_refuses_invalid_caps(registry, tmp_path, method, cap):
    if method != "register":
        registry.register(AGENT, working_dir=str(tmp_path / "agent"), skill_load_cap=20000)
    writer = registry.update if method == "update" else registry.register
    kwargs = {"working_dir": str(tmp_path / "agent")} if method == "register" else {}
    with pytest.raises(ValueError, match="skill_load_cap"):
        writer(AGENT, skill_load_cap=cap, display_name="Must not persist", **kwargs)
    if method == "register":
        assert registry.get(AGENT) is None
    else:
        assert registry.get(AGENT).skill_load_cap == 20000
        assert registry.get(AGENT).display_name == ""


@pytest.fixture
def client(tmp_path):
    from pinky_daemon.api import create_api

    app = create_api(
        max_sessions=10, default_working_dir=str(tmp_path), db_path=str(tmp_path / "test.db"),
    )
    test_client = TestClient(app)
    created = test_client.post(
        "/agents", json={"name": AGENT, "working_dir": str(tmp_path / "agent")}
    )
    assert created.status_code == 200, created.text
    try:
        yield test_client
    finally:
        test_client.close()
        app.state.store_catalog.close()


def test_api_get_shows_default(client):
    assert client.get(f"/agents/{AGENT}").json()["skill_load_cap"] == 0


@pytest.mark.parametrize("cap", [0, 2000, 200000])
def test_put_accepts_valid_caps(client, cap):
    response = client.put(f"/agents/{AGENT}", json={"skill_load_cap": cap})
    assert response.status_code == 200, response.text
    assert response.json()["skill_load_cap"] == cap
    assert client.get(f"/agents/{AGENT}").json()["skill_load_cap"] == cap
    assert client.app.state.agents.get(AGENT).skill_load_cap == cap


@pytest.mark.parametrize(
    "cap", [1, 1999, 200001, -1, "x", True, "20000", 20000.0, 20000.5, [20000]]
)
def test_put_refuses_other_values_with_a_plain_message(client, cap):
    assert client.put(f"/agents/{AGENT}", json={"skill_load_cap": 20000}).status_code == 200
    response = client.put(f"/agents/{AGENT}", json={"skill_load_cap": cap})
    assert response.status_code == 422
    assert "skill_load_cap must be 0 (whole skill) or a whole number from 2000 to 200000" in (
        response.text
    )
    assert client.app.state.agents.get(AGENT).skill_load_cap == 20000


def test_put_null_leaves_cap_unchanged(client):
    assert client.put(f"/agents/{AGENT}", json={"skill_load_cap": 20000}).status_code == 200
    response = client.put(f"/agents/{AGENT}", json={"skill_load_cap": None})
    assert response.status_code == 200
    assert response.json()["skill_load_cap"] == 20000


def test_settings_list_shows_cap(client):
    assert client.put(f"/agents/{AGENT}", json={"skill_load_cap": 20000}).status_code == 200
    rows = client.get("/settings/heartbeat").json()["agents"]
    assert next(row for row in rows if row["name"] == AGENT)["skill_load_cap"] == 20000


# ── Prompt line ────────────────────────────────────────────────────────────


class _Skills:
    def materialize_for_agent(self, name):
        return {"catalog": [{"name": "alpha"}, {"name": "beta"}]}


class _Profiles:
    def get_all_users(self):
        return []


TODAY_BLOCK = (
    "## Skills\n"
    "You have 2 skills equipped. "
    "Call `list_my_skills()` for descriptions, "
    "`load_skill(\"name\")` for full instructions.\n\n"
    "Equipped: alpha, beta"
)
CAPPED_BLOCK = (
    "## Skills\n"
    "You have 2 skills equipped. "
    "Call `list_my_skills()` for descriptions, "
    "`load_skill(\"name\")` for instructions (a long skill comes back as a contents list "
    "plus its first part; ask for more with section=).\n\n"
    "Equipped: alpha, beta"
)


def _prompt(registry, tmp_path, cap):
    registry.register(AGENT, working_dir=str(tmp_path / "agent"), soul="Fixture soul.")
    registry.update(AGENT, skill_load_cap=cap)
    return registry.build_system_prompt(
        AGENT, skill_store=_Skills(), user_profile_store=_Profiles(),
    )


def _has_part(prompt, part):
    return f"\n\n{part}\n\n" in f"\n\n{prompt}\n\n"


def test_prompt_cap_zero_keeps_todays_skills_block(registry, tmp_path):
    prompt = _prompt(registry, tmp_path, 0)
    assert _has_part(prompt, TODAY_BLOCK)
    assert prompt.count("## Skills") == 1


@pytest.mark.parametrize("cap", [2000, 20000, 200000])
def test_prompt_cap_set_uses_the_new_line(registry, tmp_path, cap):
    prompt = _prompt(registry, tmp_path, cap)
    assert _has_part(prompt, CAPPED_BLOCK)
    assert "for full instructions" not in prompt


def test_prompt_out_of_range_stored_cap_keeps_todays_line(registry, tmp_path):
    prompt_before = _prompt(registry, tmp_path, 0)
    registry._db.execute("UPDATE agents SET skill_load_cap=5 WHERE name=?", (AGENT,))
    registry._db.commit()
    prompt = registry.build_system_prompt(
        AGENT, skill_store=_Skills(), user_profile_store=_Profiles(),
    )
    assert _has_part(prompt, TODAY_BLOCK)
    assert prompt == prompt_before
