"""A skill text refresh tells the skill's live owner sessions to reload it."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.broker import InjectResult, MessageBroker
from pinky_daemon.routes import skills as skill_routes
from pinky_daemon.skill_store import CHANGE_LINE_MAX, change_bullets, new_change_line

NAME = "notice-fixture"
OWNER = "owner-agent"
OTHER = "other-agent"
BODY = "# Fixture\n\nDo the thing.\n\n## Changes\n\n- 2026-09-26 08:00 PT: first entry.\n"
NEW_BODY = (
    "# Fixture\n\nDo the new thing.\n\n## Changes\n"
    "- 2026-09-26 09:00 PT: step 2 now does the new thing.\n"
    "- 2026-09-26 08:00 PT: first entry.\n\n## Source\n- somewhere\n"
)


OLDEST_FIRST = BODY + "- 2026-09-26 09:00 PT: appended at the end.\n"


def test_change_bullets_reads_the_section_only():
    assert change_bullets(NEW_BODY) == [
        "2026-09-26 09:00 PT: step 2 now does the new thing.",
        "2026-09-26 08:00 PT: first entry.",
    ]
    assert change_bullets("# X\n\nno log") == []
    assert change_bullets("## Changes\n\n## Source\n- not a change\n") == []
    assert change_bullets("") == []


def test_new_change_line_finds_the_added_entry_in_either_order():
    assert new_change_line(BODY, NEW_BODY) == "2026-09-26 09:00 PT: step 2 now does the new thing."
    assert new_change_line(BODY, OLDEST_FIRST) == "2026-09-26 09:00 PT: appended at the end."


def test_new_change_line_empty_when_no_entry_was_added():
    edited = BODY.replace("Do the thing.", "Do the thing carefully.")
    assert new_change_line(BODY, edited) == ""


def test_new_change_line_is_capped():
    long_body = BODY + "- " + "x" * 1000 + "\n"
    line = new_change_line(BODY, long_body)
    assert len(line) == CHANGE_LINE_MAX and line.endswith("...")


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


def test_noop_refresh_is_dropped_before_the_hook(app_client, monkeypatch):
    calls = []
    monkeypatch.setattr(skill_routes, "_on_text_changed", calls.append)
    same = skill_routes._skills.get(NAME)
    assert skill_routes._text_change(same, same) is None
    skill_routes._notify_text_changed([skill_routes._text_change(same, same), None])
    assert calls == []


def test_text_plus_metadata_change_still_notifies(app_client):
    client, sent = app_client
    r = client.put(f"/skills/{NAME}", json={"directive": NEW_BODY, "version": "9.9.9",
                                            "approval_ref": "test"})
    assert r.status_code == 200, r.text
    _wait_for(sent, 1)
    assert [to for _, to, _ in sent] == [OWNER]


def test_metadata_only_change_sends_nothing(app_client):
    client, sent = app_client
    r = client.put(f"/skills/{NAME}", json={"version": "9.9.9", "approval_ref": "test"})
    assert r.status_code == 200, r.text
    time.sleep(0.4)
    assert sent == []


def test_shared_skill_reaches_unassigned_agents_but_not_opt_outs(app_client):
    client, sent = app_client
    store = skill_routes._skills
    store.register("shared-fixture", description="Shared", directive=BODY, skill_type="skill",
                   category="skill", shared=True)
    assert store.assign_to_agent(OWNER, "shared-fixture", assigned_by="user")
    assert store.set_agent_skill_enabled(OWNER, "shared-fixture", False)  # opt out
    r = client.put("/skills/shared-fixture", json={"directive": NEW_BODY, "approval_ref": "test"})
    assert r.status_code == 200, r.text
    _wait_for(sent, 1)
    time.sleep(0.2)
    assert OTHER in [to for _, to, _ in sent]
    assert OWNER not in [to for _, to, _ in sent]


def test_several_changes_make_one_message_per_agent(app_client):
    client, sent = app_client
    store = skill_routes._skills
    store.register("second-fixture", description="Second", directive=BODY, skill_type="skill",
                   category="skill")
    assert store.assign_to_agent(OWNER, "second-fixture", assigned_by="user")
    changes = [(NAME, BODY, NEW_BODY), ("second-fixture", BODY, NEW_BODY)]
    client.portal.call(skill_routes._on_text_changed, changes)  # on the app's event loop
    _wait_for(sent, 1)
    time.sleep(0.3)
    assert [to for _, to, _ in sent] == [OWNER]
    text = sent[0][2]
    assert f"load_skill('{NAME}')" in text and "load_skill('second-fixture')" in text


def test_notice_failure_never_fails_the_refresh(app_client, monkeypatch):
    client, _ = app_client

    def boom(changes):
        raise RuntimeError("hook down")

    monkeypatch.setattr(skill_routes, "_on_text_changed", boom)
    r = client.put(f"/skills/{NAME}", json={"directive": NEW_BODY, "approval_ref": "test"})
    assert r.status_code == 200, r.text
    assert skill_routes._skills.get(NAME).directive == NEW_BODY
