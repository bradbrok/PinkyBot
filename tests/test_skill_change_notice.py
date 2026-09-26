"""A skill text refresh tells the skill's live owner sessions to reload it."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.broker import InjectResult, MessageBroker
from pinky_daemon.routes import skills as skill_routes
from pinky_daemon.skill_store import newest_change_line

NAME = "notice-fixture"
OWNER = "owner-agent"
OTHER = "other-agent"
BODY = "# Fixture\n\nDo the thing.\n\n## Changes\n\n- 2026-09-26 08:00 PT: first entry.\n"
NEW_BODY = (
    "# Fixture\n\nDo the new thing.\n\n## Changes\n"
    "- 2026-09-26 09:00 PT: step 2 now does the new thing.\n"
    "- 2026-09-26 08:00 PT: first entry.\n\n## Source\n- somewhere\n"
)


def test_newest_change_line_takes_first_bullet_after_blank_lines():
    assert newest_change_line(BODY) == "2026-09-26 08:00 PT: first entry."
    assert newest_change_line(NEW_BODY) == "2026-09-26 09:00 PT: step 2 now does the new thing."


def test_newest_change_line_empty_without_a_bullet_in_the_section():
    assert newest_change_line("# X\n\nno log") == ""
    assert newest_change_line("## Changes\n\n## Source\n- not a change\n") == ""
    assert newest_change_line("") == ""


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_SESSION_SECRET", "notice-fixture-session-secret")
    sent = []

    async def fake_inject(self, from_agent, to_agent, message):
        sent.append((from_agent, to_agent, message))
        return InjectResult(delivered=True, confirmed=False)

    monkeypatch.setattr(MessageBroker, "inject_agent_message", fake_inject)
    app = create_api(
        max_sessions=10, default_working_dir=str(tmp_path), db_path=str(tmp_path / "test.db")
    )
    with TestClient(app) as client:
        assert client.post(
            "/auth/setup", json={"password": "fixture-password", "next": "/"}
        ).status_code == 200
        for name in (OWNER, OTHER):
            app.state.agents.register(name, model="sonnet", working_dir=str(tmp_path / name))
        store = skill_routes._skills
        store.register(NAME, description="Fixture", directive=BODY, skill_type="skill",
                       category="skill")
        assert store.assign_to_agent(OWNER, NAME, assigned_by="user")
        yield client, sent


def _wait_for(sent, n, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and len(sent) < n:
        time.sleep(0.05)
    return sent


def test_text_change_notifies_owner_only_with_newest_change(app_client):
    client, sent = app_client
    r = client.put(f"/skills/{NAME}", json={"directive": NEW_BODY, "approval_ref": "test"})
    assert r.status_code == 200, r.text
    _wait_for(sent, 1)
    time.sleep(0.2)
    assert [to for _, to, _ in sent] == [OWNER]
    frm, _, text = sent[0]
    assert frm == "system"
    assert "step 2 now does the new thing" in text
    assert f"load_skill('{NAME}')" in text


def test_unchanged_text_sends_nothing(app_client):
    client, sent = app_client
    r = client.put(f"/skills/{NAME}", json={"directive": BODY, "approval_ref": "test"})
    assert r.status_code == 200, r.text
    time.sleep(0.4)
    assert sent == []


def test_notice_failure_never_fails_the_refresh(app_client, monkeypatch):
    client, _ = app_client

    def boom(name, directive):
        raise RuntimeError("hook down")

    monkeypatch.setattr(skill_routes, "_on_text_changed", boom)
    r = client.put(f"/skills/{NAME}", json={"directive": NEW_BODY, "approval_ref": "test"})
    assert r.status_code == 200, r.text
    assert skill_routes._skills.get(NAME).directive == NEW_BODY
