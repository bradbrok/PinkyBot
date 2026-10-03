"""Clone requests resolve public metadata to the correct live session."""

from types import SimpleNamespace

import claude_agent_sdk
import pytest
from fastapi.testclient import TestClient

from pinky_daemon.sessions import Session
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth


@pytest.fixture
def clone_endpoint(daemon, monkeypatch):
    d = daemon("shadow")
    forked = []
    sent = []

    def fork(sdk_id, **kwargs):
        forked.append((sdk_id, kwargs))
        return SimpleNamespace(session_id="forked-transcript")

    async def send(session, content):
        sent.append((session.id, content, session._sdk_session_id))

    monkeypatch.setattr(claude_agent_sdk, "fork_session", fork)
    monkeypatch.setattr(Session, "send", send)
    client = TestClient(d.app)

    def post(actor="normal", **body):
        path = f"/agents/{actor}/clone-worker"
        return client.post(
            path, headers=signed(d, "POST", path, actor), json={"task": "copy task", **body}
        )

    yield SimpleNamespace(d=d, manager=d.app.state.manager, post=post, forked=forked, sent=sent)
    client.close()


def main_session(endpoint, **kwargs):
    session = endpoint.manager.create(
        session_id="source-main",
        agent_name="normal",
        session_type="main",
        working_dir=str(endpoint.d.root / "source-work"),
        system_prompt="Source prompt retained by the clone.",
        **kwargs,
    )
    session._sdk_session_id = "source-transcript"
    return session


@pytest.mark.parametrize("title", ["", "Independent branch"])
def test_clone_uses_live_source_and_dispatches_once(clone_endpoint, title):
    e = clone_endpoint
    e.manager.create(session_id="other-main", agent_name="peer", session_type="main")
    e.manager.create(session_id="own-worker", agent_name="normal", session_type="worker")
    closed = e.manager.create(session_id="closed-main", agent_name="normal", session_type="main")
    e.manager.delete(closed.id)
    main = main_session(e)
    assert all(isinstance(row.session_type, str) for row in e.manager.list())
    response = e.post(title=title)
    assert response.status_code == 200, response.text
    result = response.json()
    worker = e.manager.get(result["worker_session_id"])
    assert worker is not None and worker.id != main.id
    assert result == {
        "worker_session_id": worker.id,
        "forked_sdk_session_id": "forked-transcript",
        "agent": "normal",
        "task": "copy task",
    }
    assert e.forked == [
        (
            "source-transcript",
            {"directory": main.working_dir, "title": title or "Worker clone for: copy task"},
        )
    ]
    assert (worker.working_dir, worker._system_prompt) == (
        main.working_dir,
        main._system_prompt,
    )
    assert worker.session_type.value == "worker" and worker.agent_name == "normal"
    assert worker.auto_restart is False
    assert worker._sdk_session_id == "forked-transcript"
    assert e.sent == [(worker.id, "copy task", "forked-transcript")]
    assert main._sdk_session_id == "source-transcript"


@pytest.mark.parametrize("kind", ["absent", "other-owner", "worker", "closed", "no-transcript"])
def test_clone_unavailable_source_has_no_side_effects(clone_endpoint, kind):
    e = clone_endpoint
    if kind in {"closed", "no-transcript"}:
        main = main_session(e)
        if kind == "closed":
            e.manager.delete(main.id)
        else:
            main._sdk_session_id = ""
    elif kind != "absent":
        e.manager.create(
            session_id="unrelated",
            agent_name="peer" if kind == "other-owner" else "normal",
            session_type="main" if kind == "other-owner" else "worker",
        )
    before = [row.id for row in e.manager.list()]
    response = e.post()
    assert response.status_code == 400, response.text
    assert (
        "no SDK transcript" in response.json()["detail"]
        if kind == "no-transcript"
        else ("no active main session" in response.json()["detail"])
    )
    assert [row.id for row in e.manager.list()] == before
    assert not e.forked and not e.sent


def test_clone_fork_failure_does_not_create_worker(clone_endpoint, monkeypatch):
    e = clone_endpoint
    main_session(e)

    def missing_transcript(*args, **kwargs):
        raise FileNotFoundError("transcript unavailable")

    monkeypatch.setattr(claude_agent_sdk, "fork_session", missing_transcript)
    response = e.post()
    assert response.status_code == 500
    assert response.json() == {"detail": "Fork failed: transcript unavailable"}
    assert [row.id for row in e.manager.list()] == ["source-main"]
    assert not e.sent


def test_clone_isolated_enforce_refuses_before_fork(clone_endpoint, monkeypatch):
    e = clone_endpoint
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "enforce")
    main = e.manager.create(agent_name="tenant", session_type="main")
    main._sdk_session_id = "source-transcript"
    response = e.post("tenant")
    assert response.status_code == 403
    assert len(e.manager.list()) == 1
    assert not e.forked and not e.sent
