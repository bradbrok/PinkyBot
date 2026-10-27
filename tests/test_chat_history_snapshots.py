"""History readers keep stable iteration inputs while loop-thread state changes."""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from pinky_daemon.api import create_api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.sessions import Checkpoint, Session, SessionMessage
from tests.conftest import TEST_SESSION_SECRET


@pytest.fixture
def history_app(tmp_path):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    app.state.agents.register("reader", working_dir=str(tmp_path / "reader"))
    for name in ["reader-first", "reader-other"]:
        app.state.manager.create(
            session_id=name, agent_name="reader", working_dir=str(tmp_path),
        )
    app.state.conversation_store.append("reader-first", "user", "needle own")
    yield app
    app.state.dream_runner.close()
    app.state.conversation_store.close()
    app.state.agents.close()
    app.state.store_catalog.close()


async def interleave_history_read(app, query, entered, release, mutate):
    cookie = create_session_cookie(TEST_SESSION_SECRET)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        cookies={SESSION_COOKIE_NAME: cookie},
    ) as client:
        pending = asyncio.create_task(
            client.get("/agents/reader/chat-history", params={"q": query}),
        )
        try:
            async with asyncio.timeout(10):
                while not entered.is_set():
                    if pending.done():
                        pytest.fail(f"history ended before worker checkpoint: {pending.result()}")
                    await asyncio.sleep(0.001)
            await mutate(client)
        finally:
            release.set()
            results = await asyncio.gather(pending, return_exceptions=True)
        response = results[0]
        assert isinstance(response, httpx.Response), repr(response)
        assert response.status_code == 200, response.text
        assert response.json()["count"] == 1
        assert response.json()["messages"][0]["content"] == "needle own"


@pytest.mark.parametrize("query", ["", "needle"], ids=["history", "search"])
@pytest.mark.parametrize("operation", ["create", "delete"])
async def test_session_mutation_during_history_collection(
    history_app, monkeypatch, query, operation,
):
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    original_info = Session.info.fget

    def paused_info(self):
        if self.id == "reader-first" and threading.get_ident() != loop_thread:
            entered.set()
            assert release.wait(10), "test writer did not finish"
        return original_info(self)

    monkeypatch.setattr(Session, "info", property(paused_info))

    async def mutate(client):
        if operation == "create":
            response = await client.post("/sessions", json={"session_id": "reader-new"})
            assert history_app.state.manager.get("reader-new") is not None
        else:
            response = await client.delete("/sessions/reader-other")
            assert history_app.state.manager.get("reader-other") is None
        assert response.status_code == 200, response.text

    await interleave_history_read(history_app, query, entered, release, mutate)


@pytest.mark.parametrize("query", ["", "needle"], ids=["history", "search"])
@pytest.mark.parametrize("operation", ["append", "remove"])
@pytest.mark.parametrize("checkpoint", [False, True], ids=["initial", "checkpoint"])
async def test_session_history_mutation_during_info_collection(
    history_app, monkeypatch, query, operation, checkpoint,
):
    session = history_app.state.manager.get("reader-first")
    first = SessionMessage("user", "aaaa", timestamp=20)
    second = SessionMessage("assistant", "bbbbbbbb", timestamp=30)
    session.history[:] = [first, second]
    if checkpoint:
        session.history.insert(0, SessionMessage("user", "old message", timestamp=1))
        session.checkpoints.append(Checkpoint("summary", 1, timestamp=10))
    expected_pct = 3 / session.max_tokens * 100
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    original_getattribute = SessionMessage.__getattribute__
    original_info = Session.info.fget
    observed = []
    # Pause the filtered traversal itself, or the consumer of unfiltered history.
    field = "timestamp" if checkpoint else "estimated_tokens"

    def paused_read(self, name):
        if (self is first and name == field and threading.get_ident() != loop_thread
                and not entered.is_set()):
            entered.set()
            assert release.wait(10), "test writer did not finish"
        return original_getattribute(self, name)

    def record_info(self):
        info = original_info(self)
        if self is session:
            observed.append(info.context_used_pct)
        return info

    monkeypatch.setattr(SessionMessage, "__getattribute__", paused_read)
    monkeypatch.setattr(Session, "info", property(record_info))

    async def mutate(client):
        if operation == "append":
            session.history.append(SessionMessage("user", "x" * 400, timestamp=40))
        else:
            assert session.history.pop() is second

    await interleave_history_read(history_app, query, entered, release, mutate)
    assert observed[0] == expected_pct
