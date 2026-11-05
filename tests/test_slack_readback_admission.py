"""Outstanding read capacity follows submitted functions through caller cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest

from pinky_daemon import slack_readback
from pinky_outreach.slack import SlackError
from tests.test_slack_readback import (
    BOT,
    CHANNEL,
    HISTORY_PATH,
    LATEST,
    MESSAGE_PATH,
    PARENT,
    TEXT,
    TS,
    _headers,
    _message,
    _page,
)
from tests.test_slack_readback import gateway as gateway

pytestmark = pytest.mark.real_auth


def _count():
    return getattr(slack_readback, "_OUTSTANDING", -1)


class _Pool(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=4, thread_name_prefix="slack-readback-test")
        self.futures = []
        self.releases = []
        self.before = None
        self.fail_submit = False

    def submit(self, fn, *args, **kwargs):
        if self.fail_submit:
            raise RuntimeError("submit refused")
        index = len(self.futures)

        def invoke():
            if self.before:
                self.before(index)
            return fn(*args, **kwargs)

        future = super().submit(invoke)
        self.futures.append(future)
        return future


@pytest.fixture
def admission_pool(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(slack_readback, "_EXECUTOR", pool)
    try:
        yield pool
    finally:
        for release in pool.releases:
            release.set()
        pool.shutdown(wait=True)


def _finished(futures):
    loop = asyncio.get_running_loop()
    signals = []
    for future in futures:
        signal = asyncio.Event()
        # Installed after submission, hence after the reader's release callback.
        future.add_done_callback(lambda _, signal=signal: loop.call_soon_threadsafe(signal.set))
        signals.append(signal)
    return asyncio.wait_for(asyncio.gather(*(signal.wait() for signal in signals)), 3)


class _BlockedCalls:
    def __init__(self, pool):
        self.pool = pool
        self.tasks = []
        self.releases = []
        self.entered = asyncio.Queue()
        self.loop = asyncio.get_running_loop()

    async def start(self, count=4, *, timeout_first=False):
        for index in range(count):
            release = threading.Event()
            self.releases.append(release)
            self.pool.releases.append(release)

            def block(*, timeout, release=release, index=index):
                self.loop.call_soon_threadsafe(self.entered.put_nowait, index)
                assert release.wait(10), "blocked function was not released"
                return index

            reader = slack_readback._Reader()
            if timeout_first and index == 0:
                reader.deadline = slack_readback.monotonic() + 0.2
            self.tasks.append(asyncio.create_task(reader.call(block)))
        for _ in range(count):
            await asyncio.wait_for(self.entered.get(), 3)

    async def close(self):
        completed = _finished(self.pool.futures)
        for release in self.releases:
            release.set()
        await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 3)
        await completed
        # Drain baseline cancelled items during cleanup only, after all assertions.
        await asyncio.wrap_future(ThreadPoolExecutor.submit(self.pool, lambda: None))


async def _outcome(awaitable):
    try:
        return await asyncio.wait_for(awaitable, 0.5)
    except Exception as exc:
        return exc


@pytest.mark.parametrize("phase", [
    "message-auth", "message-page", "history-auth", "history-first", "history-later",
])
async def test_busy_routes_preserve_http_contract(gateway, monkeypatch, admission_pool, phase):
    pool = admission_pool
    blocked = _BlockedCalls(pool)
    history = phase.startswith("history")
    prior = _message()
    gateway.outcomes.append(_page([prior], more=True, cursor="next"))
    original = slack_readback._Reader.call

    async def call(reader, function, *args, **kwargs):
        result = await original(reader, function, *args, **kwargs)
        if ((phase in ("message-page", "history-first")
             and function.__name__ == "get_bot_info")
                or (phase == "history-later" and function.__name__ == "_request_form")):
            await blocked.start()
        return result

    monkeypatch.setattr(slack_readback._Reader, "call", call)
    path = HISTORY_PATH + "?" + urlencode({"oldest": PARENT, "latest": LATEST}) if history else MESSAGE_PATH
    try:
        if phase.endswith("auth"):
            await blocked.start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                    base_url="http://test") as client:
            response = await _outcome(client.get(path, headers=_headers(gateway, path)))
        assert isinstance(response, httpx.Response), "busy response must not wait for a worker"
        if phase == "history-later":
            assert response.status_code == 200
            assert response.json() == {
                "complete": False, "stopped_reason": "busy", "pages": 1,
                "channel": CHANNEL, "oldest": PARENT, "latest": LATEST,
                "threads_included": False, "bot": BOT, "messages": [{
                    "ts": TS, "thread_ts": None, "user": "U123", "bot_id": None,
                    "app_id": None, "subtype": None, "edited": None, "reply_count": 0,
                    "files_count": 0, "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
                    "text_length": len(TEXT), "ours": True,
                }],
            }
        else:
            assert response.status_code == 503
            assert response.json() == {"detail": {"code": "slack_busy"}}
        expected = [] if phase.endswith("auth") else ["auth.test"]
        if phase == "history-later":
            expected.append("conversations.history")
        assert [method for method, _, _ in gateway.calls] == expected
        assert len(pool.futures) == 4 + len(expected), "busy must not submit another function"
        assert pool._work_queue.empty()
        marker = await asyncio.wait_for(asyncio.to_thread(lambda: "default available"), 1)
        assert marker == "default available"
    finally:
        await blocked.close()


async def test_timeout_and_repeated_cancel_retain_four_reservations(admission_pool):
    pool = admission_pool
    blocked = _BlockedCalls(pool)
    extra = []
    try:
        await blocked.start(timeout_first=True)
        with pytest.raises(slack_readback._DeadlineError):
            await asyncio.wait_for(blocked.tasks[0], 1)
        for task in blocked.tasks[1:]:
            task.cancel()
            task.cancel()
        await asyncio.gather(*blocked.tasks[1:], return_exceptions=True)
        assert _count() == 4
        assert len(pool.futures) == 4 and not any(f.cancelled() for f in pool.futures)
        for _ in range(3):
            result = await _outcome(slack_readback._Reader().call(lambda **kw: None))
            assert type(result).__name__ == "_BusyError"
        assert len(pool.futures) == 4
        completion = asyncio.Event()
        loop = asyncio.get_running_loop()
        pool.futures[0].add_done_callback(lambda _: loop.call_soon_threadsafe(completion.set))
        blocked.releases[0].set()
        await asyncio.wait_for(completion.wait(), 3)
        assert _count() == 3
        replacement = _BlockedCalls(pool)
        extra.append(replacement)
        await replacement.start(1)
        assert _count() == 4 and len(pool.futures) == 5
        result = await _outcome(slack_readback._Reader().call(lambda **kw: None))
        assert type(result).__name__ == "_BusyError"
        assert len(pool.futures) == 5
    finally:
        for group in [blocked, *extra]:
            for release in group.releases:
                release.set()
        for group in [blocked, *extra]:
            await group.close()
    assert _count() == 0 and pool._work_queue.empty()


async def test_cancel_before_dispatch_does_not_cancel_raw_future(monkeypatch):
    class Pending:
        def __init__(self):
            self.items = []
            self.submitted = asyncio.Queue()

        def submit(self, fn, *args):
            future = Future()
            self.items.append((future, lambda: fn(*args)))
            self.submitted.put_nowait(future)
            return future

        def finish(self, index):
            future, invoke = self.items[index]
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(invoke())
                except Exception as exc:
                    future.set_exception(exc)

    pool = Pending()
    monkeypatch.setattr(slack_readback, "_EXECUTOR", pool)
    tasks, dispatched = [], []
    try:
        for index in range(4):
            tasks.append(asyncio.create_task(slack_readback._Reader().call(
                lambda *, timeout, index=index: dispatched.append(index))))
        for _ in range(4):
            await asyncio.wait_for(pool.submitted.get(), 1)
        for task in tasks:
            task.cancel()
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not any(future.cancelled() for future, _ in pool.items)
        assert _count() == 4 and dispatched == []
        result = await _outcome(slack_readback._Reader().call(lambda **kw: None))
        assert type(result).__name__ == "_BusyError" and len(pool.items) == 4
        pool.finish(0)
        assert _count() == 3 and dispatched == [0]
        tasks.append(asyncio.create_task(slack_readback._Reader().call(lambda **kw: "new")))
        await asyncio.wait_for(pool.submitted.get(), 1)
        assert _count() == 4 and len(pool.items) == 5
        for index in range(1, 5):
            pool.finish(index)
        assert await tasks[-1] == "new"
        assert _count() == 0 and dispatched == [0, 1, 2, 3]
    finally:
        for index, (future, _) in enumerate(pool.items):
            if not future.done():
                pool.finish(index)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("failure", [False, True])
async def test_completed_function_releases_capacity(admission_pool, failure):
    def call(*, timeout):
        if failure:
            raise SlackError("invalid_auth")
        return "ok"

    result = await _outcome(slack_readback._Reader().call(call))
    assert isinstance(result, SlackError) if failure else result == "ok"
    assert _count() == 0
    assert await slack_readback._Reader().call(lambda **kw: "next") == "next"
    assert _count() == 0


async def test_submit_refusal_keeps_reservation(gateway, monkeypatch):
    assert _count() == 0
    pool = _Pool()
    monkeypatch.setattr(slack_readback, "_EXECUTOR", pool)
    pool.fail_submit = True
    dispatched = []
    try:
        for _ in range(4):
            with pytest.raises(RuntimeError, match="submit refused"):
                await slack_readback._Reader().call(lambda **kw: dispatched.append(True))
        assert _count() == 4
        assert pool.futures == [] and pool._work_queue.empty() and dispatched == []
        pool.fail_submit = False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                    base_url="http://test") as client:
            response = await client.get(MESSAGE_PATH, headers=_headers(gateway, MESSAGE_PATH))
        assert response.status_code == 503
        assert response.json() == {"detail": {"code": "slack_busy"}}
        assert gateway.calls == [] and pool.futures == [] and _count() == 4
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        with slack_readback._LOCK:
            slack_readback._OUTSTANDING = 0


async def test_failed_worker_start_bounds_real_submit_queue(gateway, monkeypatch):
    assert _count() == 0
    pool = ThreadPoolExecutor(max_workers=4)
    monkeypatch.setattr(slack_readback, "_EXECUTOR", pool)
    dispatched, attempts, outcomes = [], [], []
    failure = RuntimeError("worker start failed")

    def cannot_start_worker():
        attempts.append(True)
        raise failure

    monkeypatch.setattr(pool, "_adjust_thread_count", cannot_start_worker)
    try:
        for _ in range(8):
            outcomes.append(await _outcome(slack_readback._Reader().call(
                lambda **kw: dispatched.append(True))))
        assert pool._work_queue.qsize() <= 4
        assert pool._work_queue.qsize() == 4 and _count() == 4
        assert len(attempts) == 4 and dispatched == []
        assert all(result is failure for result in outcomes[:4])
        assert all(isinstance(result, slack_readback._BusyError) for result in outcomes[4:])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                    base_url="http://test") as client:
            response = await client.get(MESSAGE_PATH, headers=_headers(gateway, MESSAGE_PATH))
        assert response.status_code == 503
        assert response.json() == {"detail": {"code": "slack_busy"}}
        assert gateway.calls == [] and len(attempts) == 4 and pool._work_queue.qsize() == 4
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        with slack_readback._LOCK:
            slack_readback._OUTSTANDING = 0


async def test_failed_fourth_worker_start_keeps_only_one_queued_call(monkeypatch):
    assert _count() == 0
    pool = _Pool()
    monkeypatch.setattr(slack_readback, "_EXECUTOR", pool)
    blocked = _BlockedCalls(pool)
    dispatched, attempts, outcomes = [], [], []
    failure = RuntimeError("worker start failed")

    def cannot_start_worker():
        attempts.append(True)
        raise failure

    try:
        await blocked.start(3)
        assert _count() == 3 and len(pool.futures) == 3 and pool._work_queue.empty()
        monkeypatch.setattr(pool, "_adjust_thread_count", cannot_start_worker)
        for _ in range(8):
            outcomes.append(await _outcome(slack_readback._Reader().call(
                lambda **kw: dispatched.append(True))))
        assert pool._work_queue.qsize() <= 1
        assert pool._work_queue.qsize() == 1 and _count() == 4
        assert len(attempts) == 1 and dispatched == [] and len(pool.futures) == 3
        assert outcomes[0] is failure
        assert all(isinstance(result, slack_readback._BusyError) for result in outcomes[1:])
        # Cancel queued work before unblocking the three existing workers.
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        for release in blocked.releases:
            release.set()
        try:
            await asyncio.wait_for(asyncio.gather(*blocked.tasks, return_exceptions=True), 3)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            with slack_readback._LOCK:
                slack_readback._OUTSTANDING = 0


@pytest.mark.parametrize("failure", [False, True])
def test_already_done_future_releases_once_without_deadlock(failure):
    script = '''
import asyncio, json
from concurrent.futures import Future
from pinky_daemon import slack_readback as m
observed = []
class Immediate:
    def submit(self, fn, *args):
        observed.append(getattr(m, "_OUTSTANDING", -1))
        f = Future()
        if FAILURE:
            f.set_exception(ValueError("provider failed"))
        else:
            f.set_result(fn(*args))
        return f
m._EXECUTOR = Immediate()
async def run():
    results = []
    for _ in range(2):
        try:
            results.append(await m._Reader().call(lambda **kw: "ok"))
        except ValueError as exc:
            results.append(str(exc))
        results.append(getattr(m, "_OUTSTANDING", -1))
    return results
print(json.dumps([asyncio.run(run()), observed]))
'''.replace("FAILURE", repr(failure))
    try:
        result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                text=True, timeout=4, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pytest.fail("completed-future callback deadlocked")
    assert result.returncode == 0, result.stderr
    value = "provider failed" if failure else "ok"
    assert json.loads(result.stdout) == [[value, 0, value, 0], [1, 1]]


def test_completion_after_originating_loop_closes(admission_pool, caplog):
    pool = admission_pool
    loop = asyncio.new_event_loop()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    pool.releases.append(release)
    started = asyncio.Event()

    def block(*, timeout):
        entered.set()
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10)

    task = loop.create_task(slack_readback._Reader().call(block))
    try:
        loop.run_until_complete(asyncio.wait_for(started.wait(), 2))
        assert entered.is_set()
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        pool.futures[0].add_done_callback(lambda _: finished.set())
        loop.close()
        assert _count() == 1 and not pool.futures[0].cancelled()
        release.set()
        assert finished.wait(3)
        assert _count() == 0
        assert asyncio.run(slack_readback._Reader().call(lambda **kw: "new loop")) == "new loop"
        assert _count() == 0
        assert "exception calling callback" not in caplog.text.lower()
    finally:
        release.set()
        if not loop.is_closed():
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()


@pytest.mark.parametrize("phase", [
    "message-auth", "message-page", "history-auth", "history-first", "history-later",
])
@pytest.mark.parametrize("expired", [False, True])
async def test_worker_start_deadline_through_routes(
    gateway, monkeypatch, admission_pool, phase, expired,
):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(slack_readback, "monotonic", lambda: clock.now)
    pool = admission_pool
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    pool.releases.append(release)
    target = 0 if phase.endswith("auth") else 2 if phase == "history-later" else 1

    def before(index):
        if index == target:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)

    pool.before = before
    history = phase.startswith("history")
    gateway.outcomes.extend([_page([_message()], more=True, cursor="next"), _page()])
    path = HISTORY_PATH + "?" + urlencode({"oldest": PARENT, "latest": LATEST}) if history else MESSAGE_PATH
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                base_url="http://test") as client:
        task = asyncio.create_task(client.get(path, headers=_headers(gateway, path)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            clock.now = 161.0 if expired else 145.0
            release.set()
            response = await asyncio.wait_for(task, 2)
            if expired:
                assert len(gateway.calls) == target, "expired invocation must not dispatch HTTP"
                if phase in ("history-first", "history-later"):
                    assert response.status_code == 200
                    body = response.json()
                    assert body["complete"] is False and body["stopped_reason"] == "deadline"
                    assert body["pages"] == target - 1
                    assert len(body["messages"]) == target - 1
                    if target == 2:
                        assert body["messages"][0]["ts"] == TS
                        assert body["messages"][0]["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
                else:
                    assert response.status_code == 504
                    assert response.json() == {"detail": {"code": "slack_unreachable"}}
            else:
                assert response.status_code == 200
                for index, (_, request, _) in enumerate(gateway.calls):
                    assert set(request.extensions["timeout"].values()) == {30.0 if index < target else 15.0}
                assert response.json()["complete" if history else "found"] is True
            assert getattr(slack_readback, "_OUTSTANDING", 0) == 0
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
