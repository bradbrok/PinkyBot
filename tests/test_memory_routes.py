"""Tests for the write half of pinky_daemon.routes.memory (#463).

The browsing routes were already there; these cover the two mutating routes
the UI needs — PATCH (edit content) and DELETE (soft by default, hard on
demand) — including the guarantees that make them safe to expose in a UI:
a missing memory 404s instead of silently succeeding, an empty edit is
refused, and the default delete stays reversible.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pinky_daemon.routes import memory as memory_routes
from pinky_memory.store import ReflectionStore
from pinky_memory.types import Reflection, ReflectionType


@pytest.fixture
def client(tmp_path):
    """App with only the memory router, backed by a real per-agent memory.db."""
    working_dir = tmp_path / "agent"
    (working_dir / "data").mkdir(parents=True)
    ReflectionStore(str(working_dir / "data" / "memory.db")).close()

    agents = SimpleNamespace(
        get=lambda name: (
            SimpleNamespace(working_dir=str(working_dir)) if name == "engineer" else None
        )
    )
    memory_routes.set_dependencies(
        agents=agents,
        store=None,
        collect_agent_session_ids=lambda name: set(),
        resolve_agent_history=lambda name, **kw: [],
    )
    app = FastAPI()

    # The real auth middleware (api.py) stamps who authenticated; this app has
    # no middleware, so stand in for it. Default "owner" — these tests cover the
    # routes' own behaviour, not the owner-only guard (see
    # test_memory_write_owner_only.py for that, against the real middleware).
    @app.middleware("http")
    async def _stamp_actor(request, call_next):
        request.state.auth_actor = request.headers.get("x-test-actor", "owner")
        return await call_next(request)

    app.include_router(memory_routes.router)
    return TestClient(app), working_dir


def _store(working_dir: Path) -> ReflectionStore:
    return ReflectionStore(str(working_dir / "data" / "memory.db"))


def _insert(working_dir: Path, content: str = "original") -> str:
    store = _store(working_dir)
    r = store.insert(Reflection(type=ReflectionType.fact, content=content, salience=3))
    store.close()
    return r.id


class TestPatchMemory:
    def test_updates_content(self, client):
        api, wd = client
        mid = _insert(wd)

        resp = api.patch(f"/agents/engineer/memories/{mid}", json={"content": "rewritten"})

        assert resp.status_code == 200
        assert resp.json()["content"] == "rewritten"
        store = _store(wd)
        assert store.get(mid).content == "rewritten"
        store.close()

    def test_missing_memory_404s(self, client):
        api, _ = client
        resp = api.patch("/agents/engineer/memories/nope", json={"content": "x"})
        assert resp.status_code == 404

    def test_blank_content_is_refused(self, client):
        """An empty edit would wipe the memory through a route meant to fix a
        typo — refuse it and leave the stored content untouched."""
        api, wd = client
        mid = _insert(wd, "keep me")

        resp = api.patch(f"/agents/engineer/memories/{mid}", json={"content": "   "})

        assert resp.status_code == 400
        store = _store(wd)
        assert store.get(mid).content == "keep me"
        store.close()

    def test_unknown_agent_404s(self, client):
        api, _ = client
        resp = api.patch("/agents/ghost/memories/abc", json={"content": "x"})
        assert resp.status_code == 404


class TestDeleteMemory:
    def test_default_is_soft_and_reversible(self, client):
        api, wd = client
        mid = _insert(wd)

        resp = api.delete(f"/agents/engineer/memories/{mid}")

        assert resp.status_code == 200
        assert resp.json()["hard"] is False
        store = _store(wd)
        row = store.get(mid)
        assert row is not None, "soft delete must keep the row"
        assert row.active is False
        # The archive event is what makes it undoable.
        event_id = store._conn.execute(
            "SELECT id FROM memory_events WHERE event_type = 'archive'"
        ).fetchone()[0]
        assert store.revert_memory_event(event_id) is True
        assert store.get(mid).active is True
        store.close()

    def test_hard_removes_the_row(self, client):
        api, wd = client
        mid = _insert(wd)

        resp = api.delete(f"/agents/engineer/memories/{mid}?hard=true")

        assert resp.status_code == 200
        assert resp.json()["hard"] is True
        store = _store(wd)
        assert store.get(mid) is None
        store.close()

    def test_missing_memory_404s_soft(self, client):
        api, _ = client
        assert api.delete("/agents/engineer/memories/nope").status_code == 404

    def test_missing_memory_404s_hard(self, client):
        api, _ = client
        assert api.delete("/agents/engineer/memories/nope?hard=true").status_code == 404

    def test_already_archived_is_not_re_archived(self, client):
        """Deleting twice must not stack archive events — the second call has
        nothing left to archive and the undo of the first stays the undo."""
        api, wd = client
        mid = _insert(wd)
        api.delete(f"/agents/engineer/memories/{mid}")
        api.delete(f"/agents/engineer/memories/{mid}")

        store = _store(wd)
        count = store._conn.execute(
            "SELECT count(*) FROM memory_events WHERE event_type = 'archive'"
        ).fetchone()[0]
        store.close()
        assert count == 1


class TestOwnerOnlyGuard:
    """#463 phase 2: a non-owner caller is refused before the store is touched."""

    def test_agent_actor_cannot_patch(self, client):
        api, wd = client
        mid = _insert(wd)
        resp = api.patch(
            f"/agents/engineer/memories/{mid}",
            json={"content": "rewritten"},
            headers={"x-test-actor": "agent"},
        )
        assert resp.status_code == 403
        store = _store(wd)
        assert store.get(mid).content == "original"
        store.close()

    def test_agent_actor_cannot_delete(self, client):
        api, wd = client
        mid = _insert(wd)
        resp = api.delete(
            f"/agents/engineer/memories/{mid}", headers={"x-test-actor": "agent"}
        )
        assert resp.status_code == 403
        store = _store(wd)
        assert store.get(mid).active is True
        store.close()
