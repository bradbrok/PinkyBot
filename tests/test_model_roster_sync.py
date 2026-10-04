"""Roster admission, cadence and late-worker safety on synthetic stores."""

from __future__ import annotations

import asyncio
import socket
import threading
import urllib.error
import urllib.request
from unittest.mock import Mock

import pytest

from pinky_daemon import runtime_model_catalog
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.pricing import lookup_rate
from pinky_daemon.streaming_session import is_1m_model
from tests._model_roster_local import (
    SONNET,
    add,
    apply,
    document,
    encode,
    last_good,
    model_row,
    new_model,
    snapshot,
    status,
)
from tests.test_model_roster_fetch import (
    FINAL,
    URL,
    Clock,
    error_type,
    make_service,
    required,
    result,
    sync_module,
)


async def until(predicate):
    try:
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.001)
    except TimeoutError:
        raise AssertionError("Roster transition did not occur") from None


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_MODEL_ROSTER_SYNC", "on")
    instance = AgentRegistry(str(tmp_path / "agents.db"))
    runtime_model_catalog.bind_registry(instance)
    try:
        yield instance
    finally:
        runtime_model_catalog.reset_for_tests()
        instance.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Sync tests require an offline getter")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def assert_error(exc, code):
    assert getattr(exc, "status_code", None) == code
    assert isinstance(getattr(exc, "code", None), str) and exc.code


def prime():
    bare = SONNET.split("/", 1)[1]
    return lookup_rate(bare), is_1m_model(bare)


@pytest.mark.asyncio
async def test_success_applies_exact_raw_bytes_on_loop_and_updates_primed_cache(
    registry, monkeypatch
):
    value = document()
    model_row(value).update(context_window=800_000, is_1m=False)
    model_row(value)["pricing"]["input"] = 7.0
    raw = encode(value) + b"\n "
    old_rate, old_1m = prime()
    assert old_rate["input"] != 7.0 and old_1m
    getter_threads, apply_threads = [], []
    loop_thread = threading.get_ident()
    original = registry.apply_model_roster

    def getter(url, *, timeout):
        getter_threads.append(threading.get_ident())
        return result(raw, FINAL)

    def observed_apply(document, **kwargs):
        apply_threads.append((threading.get_ident(), document, kwargs))
        return original(document, **kwargs)

    monkeypatch.setattr(registry, "apply_model_roster", observed_apply)
    service = make_service(registry, getter=getter, url=URL)
    try:
        report = await required(service, "sync")(dry_run=False)
        assert report["revision"] == 2 and report["revision_gate"] == "accepted"
        assert getter_threads and getter_threads[0] != loop_thread
        assert apply_threads == [(loop_thread, raw, {"source": FINAL, "dry_run": False})]
        assert registry.get_setting("model_roster.last_applied_document").encode() == raw
        assert status(registry)["source"] == FINAL
        assert prime()[0]["input"] == 7.0 and not prime()[1]
    finally:
        await required(service, "close")()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision,gate", [(2, "equal"), (1, "lower")])
async def test_equal_and_lower_remote_revisions_keep_last_good(registry, revision, gate):
    apply(registry, document(2))
    before, cached = last_good(registry), prime()
    value = document(revision)
    model_row(value)["pricing"]["input"] = 99.0
    service = make_service(registry, getter=lambda url, **kw: result(encode(value)))
    try:
        report = await required(service, "sync")(dry_run=False)
        assert report["revision_gate"] == gate
        assert last_good(registry) == before and prime() == cached
    finally:
        await required(service, "close")()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"response-private-text",
        b'{"revision":2,"revision":3}',
        b'{"schema":1}',
        b"[]",
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
async def test_parser_rejection_retains_last_good_and_dry_run_is_pure(
    registry,
    monkeypatch,
    capsys,
    raw,
    dry_run,
):
    before, good, cached = snapshot(registry), last_good(registry), prime()
    invalidate = Mock()
    monkeypatch.setattr(runtime_model_catalog, "invalidate", invalidate)
    service = make_service(
        registry, getter=lambda url, **kw: result(raw), url=URL + "?private-query-marker"
    )
    try:
        with pytest.raises(error_type()) as caught:
            await required(service, "sync")(dry_run=dry_run)
        assert_error(caught.value, 502)
        assert last_good(registry) == good and prime() == cached
        invalidate.assert_not_called()
        if dry_run:
            assert snapshot(registry) == before
        else:
            assert status(registry)["last_attempt_at"] > 0 and status(registry)["last_error"]
        rendered = str(caught.value) + status(registry)["last_error"] + capsys.readouterr().err
        assert "private-query-marker" not in rendered and "response-private-text" not in rendered
    finally:
        await required(service, "close")()


@pytest.mark.asyncio
async def test_final_url_is_validated_again_before_parse_or_apply(registry, monkeypatch):
    before = last_good(registry)
    apply_spy = Mock(wraps=registry.apply_model_roster)
    monkeypatch.setattr(registry, "apply_model_roster", apply_spy)
    service = make_service(
        registry,
        getter=lambda url, **kw: result(encode(document()), "https://example.invalid/private"),
    )
    try:
        with pytest.raises(error_type()) as caught:
            await required(service, "sync")(dry_run=False)
        assert_error(caught.value, 502)
        assert last_good(registry) == before
        apply_spy.assert_not_called()
    finally:
        await required(service, "close")()


def test_public_failure_facade_delegates_without_new_sql(registry, monkeypatch):
    before = snapshot(registry)
    private = Mock()
    monkeypatch.setattr(registry, "_record_model_roster_error", private)
    failure = RuntimeError("fixed_failure_code")
    required(registry, "record_model_roster_sync_error")(failure)
    private.assert_called_once_with(failure)
    assert snapshot(registry) == before


@pytest.mark.asyncio
async def test_preapply_failure_uses_public_facade_once_on_loop_with_fixed_text(
    registry,
    monkeypatch,
    capsys,
):
    facade = required(registry, "record_model_roster_sync_error")
    observations = []
    loop_thread = threading.get_ident()

    def record(exc):
        observations.append((threading.get_ident(), str(exc)))
        return facade(exc)

    def getter(url, **kwargs):
        raise urllib.error.URLError("transport-private-marker")

    monkeypatch.setattr(registry, "record_model_roster_sync_error", record)
    service = make_service(registry, getter=getter, url=URL + "?query-private-marker")
    before = registry._model_roster_errors
    try:
        with pytest.raises(error_type()) as caught:
            await required(service, "sync")(dry_run=False)
        assert_error(caught.value, 502)
        assert len(observations) == 1 and observations[0][0] == loop_thread
        assert registry._model_roster_errors == before + 1
        text = status(registry)["last_error"] + capsys.readouterr().err
        assert "transport-private-marker" not in text and "query-private-marker" not in text
        assert observations[0][1] == status(registry)["last_error"]
    finally:
        await required(service, "close")()


@pytest.mark.asyncio
async def test_apply_refusal_records_once_through_unchanged_b1_path(registry, monkeypatch):
    custom = new_model("collision-model")
    custom["provider"] = "custom"
    add(registry, custom)
    value = document()
    value["models"].append(new_model("collision-model"))
    good, cached = last_good(registry), prime()
    facade = required(registry, "record_model_roster_sync_error")
    spy = Mock(wraps=facade)
    monkeypatch.setattr(registry, "record_model_roster_sync_error", spy)
    before = registry._model_roster_errors
    service = make_service(registry, getter=lambda url, **kw: result(encode(value)))
    try:
        with pytest.raises(error_type()) as caught:
            await required(service, "sync")(dry_run=False)
        assert_error(caught.value, 502)
        assert registry._model_roster_errors == before + 1
        assert "collision-model" in status(registry)["last_error"]
        assert last_good(registry) == good and prime() == cached
        spy.assert_not_called()
    finally:
        await required(service, "close")()


class SleepGate:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.queue = asyncio.Queue()

    async def __call__(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.calls.append(delay)
        await self.queue.put((delay, future))
        await future

    async def next(self):
        try:
            return await asyncio.wait_for(self.queue.get(), 5)
        except TimeoutError:
            raise AssertionError("Roster loop did not schedule its next delay") from None

    def advance(self, pair):
        delay, future = pair
        self.clock.now += delay
        future.set_result(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("jitter", [0.0, 1800.0])
@pytest.mark.parametrize("fails", [False, True])
async def test_sixty_second_first_delay_and_start_based_daily_cadence(registry, jitter, fails):
    clock = Clock()
    sleep = SleepGate(clock)
    calls = []

    def getter(url, **kwargs):
        calls.append(clock.now)
        clock.now += 30
        if fails:
            raise urllib.error.URLError("fixed failure")
        return result(encode(document()))

    service = make_service(
        registry, getter=getter, monotonic=clock, sleep=sleep, jitter=lambda: jitter
    )
    task = asyncio.create_task(required(service, "run")())
    try:
        first = await sleep.next()
        assert first[0] == 60 and calls == []
        sleep.advance(first)
        second = await sleep.next()
        assert calls == [160.0] and second[0] == 84600 + jitter - 30
        sleep.advance(second)
        third = await sleep.next()
        assert calls == [160.0, 160.0 + 84600 + jitter]
        assert third[0] == second[0], "Failure must not cause a tight retry"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await required(service, "close")()


@pytest.mark.asyncio
async def test_manual_sync_does_not_reset_pending_daily_deadline(registry):
    clock, calls = Clock(), []
    sleep = SleepGate(clock)

    def getter(url, **kwargs):
        calls.append(clock.now)
        return result(encode(document()))

    service = make_service(registry, getter=getter, monotonic=clock, sleep=sleep, jitter=lambda: 0)
    task = asyncio.create_task(required(service, "run")())
    try:
        sleep.advance(await sleep.next())
        daily = await sleep.next()
        clock.now += 10
        await required(service, "sync")(dry_run=True)
        assert len(sleep.calls) == 2 and calls == [160.0, 170.0]
        clock.now -= 10
        sleep.advance(daily)
        await sleep.next()
        assert calls[-1] == 160.0 + 84600
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await required(service, "close")()


@pytest.mark.asyncio
async def test_overdue_scheduled_work_has_no_catchup_burst(registry):
    clock = Clock()
    sleep = SleepGate(clock)

    def getter(url, **kwargs):
        clock.now += 3 * 86400
        return result(encode(document()))

    service = make_service(registry, getter=getter, monotonic=clock, sleep=sleep, jitter=lambda: 0)
    task = asyncio.create_task(required(service, "run")())
    try:
        sleep.advance(await sleep.next())
        delay, _ = await sleep.next()
        assert 0 < delay <= 86400, "A missed day must not create immediate catch-up attempts"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await required(service, "close")()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["timeout", "cancel", "close"])
@pytest.mark.parametrize("late_error", [False, True])
async def test_retained_worker_refuses_new_work_and_discards_late_completion(
    registry,
    ending,
    late_error,
):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def getter(url, **kwargs):
        calls.append(url)
        entered.set()
        assert release.wait(5), "Test must release its worker"
        if late_error and len(calls) == 1:
            raise urllib.error.URLError("late-private-marker")
        return result(encode(document()))

    service = make_service(registry, getter=getter, timeout=0.03 if ending == "timeout" else 10)
    good = last_good(registry)
    task = asyncio.create_task(required(service, "sync")(dry_run=False))
    try:
        await until(entered.is_set)
        if ending == "timeout":
            with pytest.raises(error_type()) as caught:
                await task
            assert_error(caught.value, 504)
        else:
            if ending == "close":
                required(service, "mark_closing")()
                await required(service, "close")()
            else:
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        after = snapshot(registry)
        with pytest.raises(error_type()) as caught:
            await required(service, "sync")(dry_run=True)
        assert_error(caught.value, 503 if ending == "close" else 409)
        assert len(calls) == 1 and snapshot(registry) == after
        assert last_good(registry) == good
        release.set()
        worker = getattr(service, "worker_task", None)
        assert isinstance(worker, asyncio.Task), "A pending worker needs a strong service reference"
        await asyncio.gather(worker, return_exceptions=True)
        assert (
            snapshot(registry) == after
            and "late-private-marker" not in status(registry)["last_error"]
        )
        if ending != "close":
            await required(service, "sync")(dry_run=True)
            assert len(calls) == 2
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await required(service, "close")()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_off_captures_configuration_and_never_attempts_fetch(registry, monkeypatch, dry_run):
    monkeypatch.setenv("PINKY_MODEL_ROSTER_SYNC", " OFF ")
    monkeypatch.setenv("PINKY_MODEL_ROSTER_URL", URL)
    getter = Mock(return_value=result(encode(document())))
    service = make_service(registry, getter=getter)
    monkeypatch.setenv("PINKY_MODEL_ROSTER_SYNC", "on")
    monkeypatch.setenv("PINKY_MODEL_ROSTER_URL", FINAL)
    before = snapshot(registry)
    try:
        assert service.enabled is False and service.url == URL
        with pytest.raises(error_type()) as caught:
            await required(service, "sync")(dry_run=dry_run)
        assert_error(caught.value, 409)
        getter.assert_not_called()
        assert snapshot(registry) == before
    finally:
        await required(service, "close")()


def test_unset_switch_and_url_capture_production_defaults(registry, monkeypatch):
    monkeypatch.delenv("PINKY_MODEL_ROSTER_SYNC", raising=False)
    monkeypatch.delenv("PINKY_MODEL_ROSTER_URL", raising=False)
    service = make_service(registry, getter=Mock())
    assert service.enabled is True
    assert service.url == (
        "https://raw.githubusercontent.com/bradbrok/PinkyBot/main/src/pinky_daemon/catalog/models.json"
    )


def test_default_deadline_first_delay_and_uniform_jitter(registry, monkeypatch):
    module = sync_module()
    draw = Mock(return_value=321.0)
    monkeypatch.setattr(module.random, "uniform", draw)
    service = make_service(registry, getter=Mock())
    assert service.timeout == 10 and service.first_delay == 60
    assert service.jitter() == 321.0
    draw.assert_called_once_with(0, 1800)


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_unexpected_worker_failure_is_fixed_and_releases_admission(
    registry, monkeypatch, capsys, caplog, dry_run
):
    getter = Mock(side_effect=[RuntimeError("private-marker"), result(encode(document()))])
    facade = Mock(wraps=required(registry, "record_model_roster_sync_error"))
    monkeypatch.setattr(registry, "record_model_roster_sync_error", facade)
    service = make_service(registry, getter=getter)
    before, good, cached = snapshot(registry), last_good(registry), prime()
    errors = registry._model_roster_errors
    try:
        failure = None
        try:
            await required(service, "sync")(dry_run=dry_run)
        except Exception as exc:
            failure = exc
        assert isinstance(failure, error_type()), "Unexpected workers need a fixed sync error"
        assert_error(failure, 502)
        assert failure.code == "fetch_failed"
        assert last_good(registry) == good and prime() == cached
        assert facade.call_count == (0 if dry_run else 1)
        assert registry._model_roster_errors == errors + (0 if dry_run else 1)
        if dry_run:
            assert snapshot(registry) == before
        rendered = str(failure) + str(failure.detail()) + status(registry)["last_error"]
        rendered += capsys.readouterr().err + "".join(r.getMessage() for r in caplog.records)
        assert "private-marker" not in rendered
        after = snapshot(registry)
        report = await required(service, "sync")(dry_run=True)
        assert report["revision"] == 2 and getter.call_count == 2
        assert snapshot(registry) == after and facade.call_count == (0 if dry_run else 1)
    finally:
        await required(service, "close")()


@pytest.mark.asyncio
async def test_scheduled_unexpected_failure_keeps_cadence_with_one_fixed_log(
    registry, monkeypatch, capsys, caplog
):
    clock, calls = Clock(), []
    sleep = SleepGate(clock)
    service = make_service(registry, getter=Mock(), monotonic=clock, sleep=sleep, jitter=lambda: 0)

    async def attempt(*, dry_run):
        assert dry_run is False
        calls.append(clock.now)
        clock.now += 30
        if len(calls) == 1:
            raise RuntimeError("private-marker")
        return {}

    monkeypatch.setattr(service, "sync", attempt)
    before = snapshot(registry)
    task = asyncio.create_task(required(service, "run")())
    try:
        sleep.advance(await sleep.next())
        await until(lambda: task.done() or len(sleep.calls) == 2)
        assert not task.done(), "An unexpected attempt must not terminate the daily loop"
        second = await sleep.next()
        assert second[0] == 84600 - 30 and calls == [160.0]
        stderr = capsys.readouterr().err
        assert stderr.count("ERROR model roster: scheduled_attempt_failed") == 1
        assert "private-marker" not in stderr + "".join(r.getMessage() for r in caplog.records)
        sleep.advance(second)
        third = await sleep.next()
        assert calls == [160.0, 160.0 + 84600] and third[0] == second[0]
        assert snapshot(registry) == before
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await required(service, "close")()


@pytest.mark.asyncio
@pytest.mark.parametrize("switch,enabled", [
    ("0", False), ("false", False), (" FALSE ", False),
    (None, True), ("on", True), ("1", True),
])
async def test_default_on_switch_accepts_all_disabled_spellings(
    registry, monkeypatch, switch, enabled
):
    if switch is None:
        monkeypatch.delenv("PINKY_MODEL_ROSTER_SYNC", raising=False)
    else:
        monkeypatch.setenv("PINKY_MODEL_ROSTER_SYNC", switch)
    getter = Mock(return_value=result(encode(document())))
    service = make_service(registry, getter=getter)
    before = snapshot(registry)
    try:
        assert service.enabled is enabled, "Use the shared default-on kill-switch convention"
        if enabled:
            await required(service, "sync")(dry_run=True)
            assert getter.call_count == 1
        else:
            assert required(service, "start")() is None
            with pytest.raises(error_type()) as caught:
                await required(service, "sync")(dry_run=False)
            assert_error(caught.value, 409)
            getter.assert_not_called()
        assert snapshot(registry) == before
    finally:
        await required(service, "close")()
