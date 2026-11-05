"""Readback execution ownership, capacity, credential scope and privacy contracts."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import slack_readback
from pinky_daemon.api import create_api
from pinky_outreach import slack as slack_module
from pinky_outreach.slack import SlackRateLimitError
from tests.test_slack_readback import (
    BOT,
    CHANNEL,
    HISTORY_PATH,
    LATEST,
    MESSAGE_PATH,
    PARENT,
    SECRET,
    TOKEN,
    TS,
    _get,
    _history,
    _message,
    _page,
)
from tests.test_slack_readback import (
    gateway as gateway,
)
from tests.test_slack_readback_admission import (
    _BlockedCalls,
    _count,
    _outcome,
)
from tests.test_slack_readback_admission import admission_pool as admission_pool

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("history", [False, True])
async def test_selection_on_loop_and_http_on_readback_workers(history):
    loop_thread = threading.get_ident()
    selected, called = [], []

    class Adapter:
        bot_token = TOKEN

        def get_bot_info(self, **kwargs):
            called.append(("auth.test", threading.get_ident(), threading.current_thread().name))
            return BOT

        def _request_form(self, method, **kwargs):
            called.append((method, threading.get_ident(), threading.current_thread().name))
            return _page()

    def select():
        selected.append(threading.get_ident())
        return Adapter()

    if history:
        result = await slack_readback.read_history(select, CHANNEL, PARENT, LATEST, 1)
        assert result["complete"] is True
    else:
        result = await slack_readback.read_message(select, CHANNEL, TS, None)
        assert result["found"] is False
    assert selected == [loop_thread]
    assert [method for method, _, _ in called] == ["auth.test", "conversations.history"]
    assert all(thread != loop_thread for _, thread, _ in called)
    assert all(name.startswith("slack-readback") for _, _, name in called)


@pytest.mark.parametrize("history", [False, True])
async def test_retry_wait_uses_loop_without_worker(monkeypatch, history):
    loop_thread = threading.get_ident()
    waits, calls = [], []

    def old_sleep(seconds):
        waits.append((seconds, threading.get_ident(), "worker"))

    async def async_sleep(seconds):
        waits.append((seconds, threading.get_ident(), "async"))
        await asyncio.sleep(0)

    # Observe either implementation on the same frozen baseline test bytes.
    monkeypatch.setattr(slack_readback, "sleep", old_sleep, raising=False)
    monkeypatch.setattr(slack_readback, "asyncio", SimpleNamespace(
        to_thread=asyncio.to_thread, wait_for=asyncio.wait_for,
        get_running_loop=asyncio.get_running_loop, sleep=async_sleep,
        shield=asyncio.shield, wrap_future=asyncio.wrap_future,
    ))

    class Adapter:
        bot_token = TOKEN

        def get_bot_info(self, **kwargs):
            return BOT

        def _request_form(self, method, **kwargs):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                raise SlackRateLimitError(1)
            return _page()

    if history:
        result = await slack_readback.read_history(Adapter, CHANNEL, PARENT, LATEST, 1)
        assert result["complete"] is True
    else:
        result = await slack_readback.read_message(Adapter, CHANNEL, TS, None)
        assert result["found"] is False
    assert len(calls) == 2 and all(thread != loop_thread for thread in calls)
    assert waits == [(1, loop_thread, "async")]


@pytest.mark.parametrize("expire", [False, True])
async def test_four_readback_workers_isolate_default_and_bound_fifth(admission_pool, expire):
    blocked = _BlockedCalls(admission_pool)
    started = []
    try:
        await blocked.start()
        reader = slack_readback._Reader()
        if expire:
            reader.deadline = slack_readback.monotonic() + 0.15
        result = await _outcome(reader.call(lambda **kw: started.append(True)))
        assert type(result).__name__ == "_BusyError"
        assert len(admission_pool.futures) == 4 and started == []
        assert admission_pool._work_queue.empty()
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), 1) == "available"
    finally:
        await blocked.close()


async def test_running_timeout_keeps_worker_occupied_until_function_finishes(admission_pool):
    blocked = _BlockedCalls(admission_pool)
    try:
        await blocked.start(timeout_first=True)
        with pytest.raises(slack_readback._DeadlineError):
            await asyncio.wait_for(blocked.tasks[0], 1)
        result = await _outcome(slack_readback._Reader().call(lambda **kw: "fifth"))
        assert type(result).__name__ == "_BusyError"
        assert len(admission_pool.futures) == 4 and _count() == 4
        assert not admission_pool.futures[0].done()
        signal = asyncio.Event()
        loop = asyncio.get_running_loop()
        admission_pool.futures[0].add_done_callback(lambda _: loop.call_soon_threadsafe(signal.set))
        blocked.releases[0].set()
        await asyncio.wait_for(signal.wait(), 2)
        assert await slack_readback._Reader().call(lambda **kw: "fifth") == "fifth"
        assert len(admission_pool.futures) == 5 and _count() == 3
    finally:
        await blocked.close()


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("field", [
    "ts", "thread_ts", "user", "bot_id", "app_id", "subtype", "edited.ts", "edited.user",
    "bot.user_id", "bot.bot_id", "bot.team_id",
])
def test_returned_metadata_rejects_token_in_every_string(gateway, caplog, capsys, history, field):
    message = _message(edited={"ts": LATEST, "user": "U999"})
    if field.startswith("bot."):
        gateway.auth.append({"ok": True, **BOT, field.split(".")[1]: TOKEN})
    elif field.startswith("edited."):
        message["edited"][field.split(".")[1]] = TOKEN
    else:
        message[field] = TOKEN
    gateway.outcomes.append(_page([message]))
    response = _history(gateway) if history else _get(gateway)
    assert response.status_code == 502
    assert response.json() == {"detail": {"code": "slack_error", "error": "invalid_response"}}
    captured = capsys.readouterr()
    assert TOKEN not in response.text + caplog.text + captured.out + captured.err


@pytest.mark.parametrize("history", [False, True])
def test_real_registry_never_borrows_another_agent_or_account(tmp_path, monkeypatch, history):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_SESSION_SECRET", SECRET)
    app = create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    registry = app.state.agents
    for name in ("sample", "other"):
        registry.register(name, working_dir=str(tmp_path / name))
    registry.set_token("other", "slack", "xoxb-other-fixture", settings={"account_id": "TOTHER"})
    own = registry.create_bot_token("own", "slack", TOKEN)
    selected, calls = [], []

    class Adapter:
        def __init__(self, token):
            self.bot_token = token
            selected.append(token)

        def get_bot_info(self, **kwargs):
            calls.append((self.bot_token, "auth.test"))
            return BOT

        def _request_form(self, method, **kwargs):
            calls.append((self.bot_token, method))
            return _page()

    monkeypatch.setattr(slack_module, "SlackAdapter", Adapter)
    state = SimpleNamespace(app=app, client=TestClient(app))
    path = HISTORY_PATH if history else MESSAGE_PATH
    params = {"oldest": PARENT, "latest": LATEST} if history else {}
    try:
        # Neither another agent's inline token nor an unassigned bot-token row is a fallback.
        assert _get(state, path, **params).status_code == 409
        assert _get(state, path, **params, account_id="TOTHER").status_code == 409
        assert not selected and not calls
        registry.set_token("sample", "slack", "", token_ref=own["id"],
                           settings={"account_id": "TOWN"})
        assert _get(state, path, **params, account_id="TOTHER").status_code == 409
        assert not selected and not calls
        assert _get(state, path, **params, account_id="TOWN").status_code == 200
        assert selected == [TOKEN]
        assert calls == [(TOKEN, "auth.test"), (TOKEN, "conversations.history")]
        other_path = path.replace("/sample/", "/other/")
        assert _get(state, other_path, caller="other", **params,
                    account_id="TOTHER").status_code == 200
        assert selected == [TOKEN, "xoxb-other-fixture"]
    finally:
        state.client.close()
        app.state.store_catalog.close()


@pytest.mark.parametrize("history", [False, True])
def test_each_http_timeout_tracks_remaining_budget(gateway, monkeypatch, history):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(slack_readback, "monotonic", lambda: clock.now)
    timeouts = []

    def before(method):
        request = gateway.calls[-1][1]
        timeouts.append(request.extensions["timeout"])
        clock.now += 40 if method == "auth.test" else 5

    gateway.before = before
    if history:
        gateway.outcomes.extend([_page(more=True, cursor="next"), _page()])
    response = _history(gateway) if history else _get(gateway)
    assert response.status_code == 200
    expected = [30.0, 20.0, 15.0] if history else [30.0, 20.0]
    assert len(timeouts) == len(expected)
    for options, seconds in zip(timeouts, expected, strict=True):
        assert options and all(0 < value <= 30 for value in options.values())
        assert set(options.values()) == {seconds}


async def test_queued_http_timeout_is_recomputed_when_worker_starts(monkeypatch, admission_pool):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(slack_readback, "monotonic", lambda: clock.now)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    admission_pool.releases.append(release)
    observed = []

    def before(index):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    admission_pool.before = before
    task = asyncio.create_task(slack_readback._Reader().call(
        lambda *, timeout: observed.append(timeout)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clock.now = 145.0
        release.set()
        await asyncio.wait_for(task, 2)
        assert observed == [15.0]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)


@pytest.mark.parametrize("remaining", [1.0, 0.5])
async def test_retry_at_or_beyond_remaining_budget_never_sleeps(monkeypatch, remaining):
    monkeypatch.setattr(slack_readback, "monotonic", lambda: 100.0)
    reader = slack_readback._Reader()
    reader.deadline = 100.0 + remaining
    waits, calls = [], []

    def sync_sleep(seconds):
        waits.append(seconds)

    async def async_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(slack_readback, "sleep", sync_sleep, raising=False)
    monkeypatch.setattr(slack_readback, "asyncio", SimpleNamespace(
        to_thread=asyncio.to_thread, wait_for=asyncio.wait_for,
        get_running_loop=asyncio.get_running_loop, sleep=async_sleep,
        shield=asyncio.shield, wrap_future=asyncio.wrap_future,
    ))

    def limited(**kwargs):
        calls.append(kwargs["timeout"])
        raise SlackRateLimitError(1)

    with pytest.raises(slack_readback._DeadlineError):
        await reader.call(limited)
    assert calls == [remaining]
    assert not waits
