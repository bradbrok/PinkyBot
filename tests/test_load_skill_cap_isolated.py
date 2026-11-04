"""An isolated agent's capped load_skill reads only its own agent record."""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api
from pinky_self.server import create_server
from tests.isolated_policy_support import daemon as daemon
from tests.skill_sections_support import LONG, today_output

pytestmark = pytest.mark.real_auth

SKILLS_READ_LINE = (
    "isolation: WOULD DENY READ GET /skills/{name} for tenant reason=pending-scope repeats=0"
)


@pytest.fixture
def logs(monkeypatch):
    messages = []
    monkeypatch.setattr(api, "_log", lambda text, **kwargs: messages.append(str(text)))
    return messages


def _load_skill_through_daemon(d, monkeypatch, **kwargs):
    """Run the real tool with its HTTP calls answered by the daemon app."""
    client = TestClient(d.app)
    calls = []

    def urlopen(req, timeout=30):
        del timeout
        method = req.get_method()
        calls.append((method, req.selector))
        response = client.request(
            method, req.selector, headers=dict(req.header_items()), content=req.data,
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, "error", {}, io.BytesIO(response.content)
            )
        return io.BytesIO(response.content)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    srv = create_server(
        agent_name="tenant",
        api_url="http://testserver",
        signing_key_resolver=d.agents.get_signing_key,
    )
    tool = next(t for t in srv._tool_manager.list_tools() if t.name == "load_skill")
    try:
        return tool.fn(skill_name="long-fixture", **kwargs), calls
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_isolated_agent_capped_load_skill_works_without_a_read_line(
    daemon, monkeypatch, logs, mode
):
    d = daemon(mode)
    assert d.agents.get("tenant").isolated
    d.skills.register("long-fixture", description="Long fixture", directive=LONG)
    d.agents.update("tenant", skill_load_cap=2000)
    logs.clear()

    out, calls = _load_skill_through_daemon(d, monkeypatch)

    assert calls == [("GET", "/skills/long-fixture"), ("GET", "/agents/tenant")]
    assert out.startswith("# Skill: long-fixture\nLong fixture\n\n[Partial load: ")
    assert "(cap 2000)" in out
    assert "\n## Rules [always]\n" in out
    assert "\n## Setup\n" not in out
    # The skill read is already on the pending-scope list (and proves the read
    # diagnostics are on); the agent's read of its own record adds no line.
    assert [line for line in logs if line.startswith("isolation: WOULD DENY READ ")] == [
        SKILLS_READ_LINE
    ]
    assert not [line for line in logs if line.startswith("isolation:") and "/agents/" in line]


def test_isolated_agent_section_all_is_todays_output(daemon, monkeypatch, logs):
    d = daemon("enforce")
    d.skills.register("long-fixture", description="Long fixture", directive=LONG)
    d.agents.update("tenant", skill_load_cap=2000)

    out, calls = _load_skill_through_daemon(d, monkeypatch, section="all")

    assert calls == [("GET", "/skills/long-fixture")]
    assert out == today_output("long-fixture", "Long fixture", LONG)
