"""#463 phase 2: only the owner (web UI session) may edit or delete memories.

An agent holding a VALID internal HMAC signature is authenticated, but it is
not the owner: PATCH and DELETE on a memory must be refused for it. Reading
stays open — agents browse their own memories through these routes.

These run against the REAL auth middleware (``create_api``), not a stand-in
app, because the owner/agent distinction is stamped there. ``real_auth`` opts
out of the conftest fixture that injects a valid session cookie into every
TestClient — with the cookie present every request would look like the owner
and the test would prove nothing.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from pinky_memory.store import ReflectionStore
from pinky_memory.types import Reflection, ReflectionType

pytestmark = pytest.mark.real_auth

AGENT = "engineer"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real app + a real per-agent memory.db holding one reflection."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("PINKY_SESSION_SECRET", "test-session-secret")
    monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)

    working_dir = tmp_path / AGENT
    (working_dir / "data").mkdir(parents=True)
    store = ReflectionStore(str(working_dir / "data" / "memory.db"))
    reflection = store.insert(
        Reflection(type=ReflectionType.fact, content="original", salience=3)
    )
    store.close()

    app = create_api(max_sessions=10, default_working_dir=str(tmp_path), db_path=db_path)
    app.state.agents.register(AGENT, model="opus", working_dir=str(working_dir))

    yield TestClient(app), reflection.id
    os.unlink(db_path)


def _agent_headers(client, method: str, path: str) -> dict[str, str]:
    secret = client.app.state.agents.get_signing_key(AGENT) or "test-session-secret"
    return build_internal_auth_headers(
        secret, agent_name=AGENT, method=method, path=path
    )


def _login_as_owner(client) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie("test-session-secret"))


def test_agent_cannot_patch_memory(env):
    client, memory_id = env
    path = f"/agents/{AGENT}/memories/{memory_id}"
    resp = client.patch(
        path, json={"content": "rewritten"}, headers=_agent_headers(client, "PATCH", path)
    )
    assert resp.status_code == 403


def test_agent_cannot_delete_memory(env):
    client, memory_id = env
    path = f"/agents/{AGENT}/memories/{memory_id}"
    resp = client.delete(path, headers=_agent_headers(client, "DELETE", path))
    assert resp.status_code == 403


def test_agent_can_still_read_memory(env):
    client, memory_id = env
    path = f"/agents/{AGENT}/memories/{memory_id}"
    resp = client.get(path, headers=_agent_headers(client, "GET", path))
    assert resp.status_code == 200
    assert resp.json()["content"] == "original"


def test_owner_session_may_patch_memory(env):
    client, memory_id = env
    path = f"/agents/{AGENT}/memories/{memory_id}"
    _login_as_owner(client)
    resp = client.patch(path, json={"content": "rewritten"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "rewritten"


def test_owner_session_may_delete_memory(env):
    client, memory_id = env
    path = f"/agents/{AGENT}/memories/{memory_id}"
    _login_as_owner(client)
    resp = client.delete(path)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
