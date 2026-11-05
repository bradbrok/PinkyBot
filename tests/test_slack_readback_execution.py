"""Readback execution ownership, capacity, credential scope and privacy contracts."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
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


async def _until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 3)


@pytest.mark.parametrize("expire", [False, True])
async def test_four_readback_workers_isolate_default_and_bound_fifth(monkeypatch, expire):
    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=4, thread_name_prefix="unrelated")
    loop.set_default_executor(default)
    releases = [threading.Event() for _ in range(4)]
    entered = [threading.Event() for _ in range(4)]
    fifth_started = threading.Event()
    submitted = asyncio.Event()
    calls = []
    tasks = []
    original_submit = loop.run_in_executor
    submissions = []

    def observe_submit(executor, call, *args):
        submissions.append(executor)
        if len(submissions) == 5:
            submitted.set()
        return original_submit(executor, call, *args)

    monkeypatch.setattr(loop, "run_in_executor", observe_submit)

    def block(index, **kwargs):
        calls.append((index, threading.current_thread().name))
        entered[index].set()
        assert releases[index].wait(8), "test must release every blocked worker"
        return index

    def fifth(**kwargs):
        fifth_started.set()
        return 5

    try:
        for index in range(4):
            tasks.append(asyncio.create_task(slack_readback._Reader().call(block, index)))
        await _until(lambda: all(event.is_set() for event in entered))
        reader = slack_readback._Reader()
        if expire:
            reader.deadline = slack_readback.monotonic() + 0.15
        pending = asyncio.create_task(reader.call(fifth))
        tasks.append(pending)
        await asyncio.wait_for(submitted.wait(), 3)
        # A default-executor marker must finish while ALL read workers remain blocked.
        marker = original_submit(None, lambda: threading.current_thread().name)
        done, _ = await asyncio.wait({marker}, timeout=1)
        assert marker in done, "blocked readbacks must not occupy default-executor workers"
        assert marker.result().startswith("unrelated")
        assert not fifth_started.is_set()
        assert all(executor is not None for executor in submissions)
        assert len({id(executor) for executor in submissions}) == 1
        assert all(name.startswith("slack-readback") for _, name in calls)
        if expire:
            with pytest.raises(slack_readback._DeadlineError):
                await asyncio.wait_for(pending, 1)
        else:
            releases[0].set()
            assert await asyncio.wait_for(pending, 3) == 5
            assert fifth_started.is_set()
    finally:
        for event in releases:
            event.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        # Flush every executor that received work, after all blocked functions finish.
        executors = {id(executor): executor for executor in submissions}
        for executor in executors.values():
            await asyncio.wait_for(original_submit(executor, lambda: None), 3)
        default.shutdown(wait=True, cancel_futures=True)
    assert fifth_started.is_set() is (not expire)


async def test_running_timeout_keeps_worker_occupied_until_function_finishes():
    releases = [threading.Event() for _ in range(4)]
    entered = [threading.Event() for _ in range(4)]
    fifth_started = threading.Event()
    tasks = []

    def block(index, **kwargs):
        entered[index].set()
        assert releases[index].wait(8)

    def fifth(**kwargs):
        fifth_started.set()

    try:
        for index in range(4):
            reader = slack_readback._Reader()
            if index == 0:
                reader.deadline = slack_readback.monotonic() + 0.15
            tasks.append(asyncio.create_task(reader.call(block, index)))
        await _until(lambda: all(event.is_set() for event in entered))
        with pytest.raises(slack_readback._DeadlineError):
            await asyncio.wait_for(tasks[0], 1)
        waiting = asyncio.create_task(slack_readback._Reader().call(fifth))
        tasks.append(waiting)
        await asyncio.sleep(0.05)
        assert not fifth_started.is_set()
        releases[0].set()
        await asyncio.wait_for(waiting, 3)
        assert fifth_started.is_set()
    finally:
        for release in releases:
            release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


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


async def test_queued_http_timeout_is_recomputed_when_worker_starts(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(slack_readback, "monotonic", lambda: clock.now)
    loop = asyncio.get_running_loop()
    submit = loop.run_in_executor
    submitted = asyncio.Event()
    count = 0

    def observe(executor, call, *args):
        nonlocal count
        count += 1
        future = submit(executor, call, *args)
        if count == 5:
            submitted.set()
        return future

    monkeypatch.setattr(loop, "run_in_executor", observe)
    release = threading.Event()
    entered = [threading.Event() for _ in range(4)]
    observed = []
    tasks = []

    def block(index, **kwargs):
        entered[index].set()
        assert release.wait(8)

    def record_timeout(*, timeout):
        observed.append(timeout)

    try:
        for index in range(4):
            tasks.append(asyncio.create_task(slack_readback._Reader().call(block, index)))
        await _until(lambda: all(event.is_set() for event in entered))
        tasks.append(asyncio.create_task(slack_readback._Reader().call(record_timeout)))
        await asyncio.wait_for(submitted.wait(), 3)
        clock.now = 145.0
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert observed == [15.0]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


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
    ))

    def limited(**kwargs):
        calls.append(kwargs["timeout"])
        raise SlackRateLimitError(1)

    with pytest.raises(slack_readback._DeadlineError):
        await reader.call(limited)
    assert calls == [remaining]
    assert not waits
