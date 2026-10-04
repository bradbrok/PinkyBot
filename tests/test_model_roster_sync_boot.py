"""Remote task lifetime follows the real readiness and shutdown callbacks."""

from __future__ import annotations

import asyncio
import threading
import urllib.error
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import api, claude_runner
from tests import test_startup_api_readiness as boot_harness
from tests._model_roster_local import document, encode, status
from tests.test_model_roster_fetch import required, result
from tests.test_model_roster_sync import until
from tests.test_startup_api_readiness import _run_boot
from tests.test_startup_api_readiness_r1 import POLLERS, _configure_pollers


@pytest.fixture
def boot_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_MODEL_ROSTER_SYNC", "on")
    monkeypatch.setattr(claude_runner, "_find_claude_binary", lambda: "/offline/claude")
    run = boot_harness.boot_run.__wrapped__(tmp_path, monkeypatch)
    try:
        yield run
    finally:
        run.app.state.store_catalog.close()


def service_of(run):
    service = getattr(run.app.state, "model_roster_sync", None)
    assert service is not None, "API must construct its inert roster service before startup"
    return service


@pytest.mark.parametrize("late", [False, True])
def test_one_task_starts_after_replay_and_listener_ready(boot_run, tmp_path, monkeypatch, late):
    service = service_of(boot_run)
    boot_run.late_bind = late
    if late:
        monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "0.03")
    calls = []
    service.getter = Mock(side_effect=AssertionError("First 60-second delay was bypassed"))
    start = required(service, "start")

    def observed_start():
        calls.append(
            (boot_run.server.phase, boot_run.server.started, boot_run.app.state.api_readiness.ready)
        )
        return start()

    monkeypatch.setattr(service, "start", observed_start)

    async def before_bind():
        assert calls == [] and getattr(boot_run.app.state, "model_roster_sync_task", None) is None
        assert status(boot_run.app.state.agents)["last_applied_revision"] == 1

    async def after_ready():
        await boot_run.app.state.api_readiness._after_ready_task
        task = getattr(boot_run.app.state, "model_roster_sync_task", None)
        assert isinstance(task, asyncio.Task) and not task.done()
        assert calls == [(2, True, True)]
        assert start() is task, "The exactly-once latch must reuse the existing loop"
        service.getter.assert_not_called()

    boot_run.before_bind_hook = before_bind
    boot_run.after_ready_hook = after_ready
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert len(calls) == 1 and boot_run.app.state.model_roster_sync_task.done()


@pytest.mark.parametrize("step", ["migration", "reconcile"])
def test_replay_failure_still_starts_roster_retry_and_pollers(
    boot_run,
    tmp_path,
    monkeypatch,
    step,
):
    service = service_of(boot_run)
    starts, retry = _configure_pollers(boot_run, monkeypatch)
    if step == "migration":
        monkeypatch.setattr(
            api,
            "_resume_grandfather_migration",
            AsyncMock(side_effect=RuntimeError("replay test failure")),
        )
    else:
        monkeypatch.setattr(
            boot_run.app.state.broker,
            "reconcile_approved_pending_messages",
            AsyncMock(side_effect=RuntimeError("replay test failure")),
        )
    factory = Mock(wraps=required(service, "start"))
    monkeypatch.setattr(service, "start", factory)
    service.getter = Mock(side_effect=AssertionError("Unexpected boot fetch"))

    async def after_ready():
        await asyncio.gather(
            boot_run.app.state.api_readiness._after_ready_task, return_exceptions=True
        )
        await boot_run.checkpoint()
        factory.assert_called_once_with()
        retry.assert_called_once_with()
        assert sorted(name for name, _ in starts) == sorted(cls.__name__ for cls in POLLERS)

    boot_run.after_ready_hook = after_ready
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")


def test_roster_factory_failure_preserves_retry_and_poller_start(
    boot_run,
    tmp_path,
    monkeypatch,
    capsys,
):
    service = service_of(boot_run)
    starts, retry = _configure_pollers(boot_run, monkeypatch)
    factory = Mock(side_effect=RuntimeError("factory-private-marker"))
    monkeypatch.setattr(service, "start", factory)

    async def after_ready():
        await boot_run.app.state.api_readiness._after_ready_task
        await boot_run.checkpoint()
        factory.assert_called_once_with()
        retry.assert_called_once_with()
        assert len(starts) == len(POLLERS)

    boot_run.after_ready_hook = after_ready
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert "factory-private-marker" not in capsys.readouterr().err


def test_off_has_no_loop_or_worker_and_preserves_bundled_boot(boot_run, tmp_path, monkeypatch):
    service = service_of(boot_run)
    service.enabled = False
    service.getter = Mock(side_effect=AssertionError("Off must never fetch"))

    async def after_ready():
        await boot_run.app.state.api_readiness._after_ready_task
        assert getattr(boot_run.app.state, "model_roster_sync_task", None) is None
        assert status(boot_run.app.state.agents)["last_applied_revision"] == 1
        service.getter.assert_not_called()

    boot_run.after_ready_hook = after_ready
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")


def test_unready_shutdown_never_creates_remote_task(boot_run, tmp_path, monkeypatch):
    service = service_of(boot_run)
    boot_run.stop_before_bind = True
    factory = Mock(wraps=required(service, "start"))
    monkeypatch.setattr(service, "start", factory)
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    factory.assert_not_called()
    assert service.closing and getattr(boot_run.app.state, "model_roster_sync_task", None) is None


def test_shutdown_marks_closing_before_its_first_await(boot_run, tmp_path, monkeypatch):
    service = service_of(boot_run)
    gate = boot_run.app.state.api_readiness
    close = gate.close
    observed = []

    async def gate_close(reason="daemon shutdown"):
        if reason == "daemon shutdown":
            observed.append(service.closing)
        assert service.closing, "Closing must precede readiness.close's first suspension"
        return await close(reason)

    monkeypatch.setattr(gate, "close", gate_close)
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert observed == [True]


@pytest.mark.parametrize("origin", ["manual", "scheduled"])
@pytest.mark.parametrize("late_error", [False, True])
def test_shutdown_drains_operation_before_store_close_and_discards_worker(
    boot_run,
    tmp_path,
    monkeypatch,
    origin,
    late_error,
):
    service = service_of(boot_run)
    entered, release = threading.Event(), threading.Event()
    operation = []
    after_store_close = []
    registry = boot_run.app.state.agents
    apply = registry.apply_model_roster
    record = registry._record_model_roster_error
    closed = False

    def getter(url, **kwargs):
        entered.set()
        assert release.wait(5), "The shutdown witness must release its worker"
        if late_error:
            raise urllib.error.URLError("late-shutdown-private-marker")
        return result(encode(document()))

    def observe_apply(*args, **kwargs):
        after_store_close.append(("apply", closed))
        return apply(*args, **kwargs)

    def observe_record(*args, **kwargs):
        after_store_close.append(("record", closed))
        return record(*args, **kwargs)

    service.getter = getter
    if origin == "scheduled":
        service.first_delay = 0
    monkeypatch.setattr(registry, "apply_model_roster", observe_apply)
    monkeypatch.setattr(registry, "_record_model_roster_error", observe_record)
    shutdown = boot_run.app.state.store_catalog.shutdown

    def catalog_shutdown(**kwargs):
        nonlocal closed
        assert service.closing
        loop = getattr(boot_run.app.state, "model_roster_sync_task", None)
        assert loop is None or loop.done()
        assert not operation or operation[0].done(), "Manual owner must be gathered before DB close"
        outcome = shutdown(**kwargs)
        closed = True
        release.set()
        return outcome

    monkeypatch.setattr(boot_run.app.state.store_catalog, "shutdown", catalog_shutdown)

    async def after_ready():
        await boot_run.app.state.api_readiness._after_ready_task
        if origin == "manual":
            operation.append(asyncio.create_task(required(service, "sync")(dry_run=False)))
        await until(entered.is_set)
        assert status(registry)["last_applied_revision"] == 1

    boot_run.after_ready_hook = after_ready
    try:
        _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
        assert closed and after_store_close == []
    finally:
        release.set()


def test_unattached_embedder_only_schedules_and_cannot_resurrect_after_close(
    boot_run,
    tmp_path,
    monkeypatch,
):
    service = service_of(boot_run)
    boot_run.embedder = True
    factory = Mock(wraps=required(service, "start"))
    monkeypatch.setattr(service, "start", factory)
    service.getter = Mock(side_effect=AssertionError("Embedder blocked on a fetch"))
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    factory.assert_called_once_with()
    assert service.closing
    assert required(service, "start")() is None
    service.getter.assert_not_called()


def test_done_loop_is_not_silently_restarted(boot_run, tmp_path, monkeypatch):
    service = service_of(boot_run)
    run = AsyncMock()
    monkeypatch.setattr(service, "run", run)

    async def after_ready():
        await boot_run.app.state.api_readiness._after_ready_task
        task = boot_run.app.state.model_roster_sync_task
        await task
        assert required(service, "start")() is task
        run.assert_awaited_once_with()

    boot_run.after_ready_hook = after_ready
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")


@pytest.mark.parametrize("unattached", [False, True])
def test_replay_finally_cannot_create_loop_after_service_is_closing(
    boot_run,
    tmp_path,
    monkeypatch,
    unattached,
):
    service = service_of(boot_run)
    boot_run.embedder = unattached
    factory = Mock(wraps=required(service, "start"))
    monkeypatch.setattr(service, "start", factory)

    async def reconcile():
        required(service, "mark_closing")()
        return 0

    monkeypatch.setattr(boot_run.app.state.broker, "reconcile_approved_pending_messages", reconcile)
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    factory.assert_not_called()
    assert getattr(boot_run.app.state, "model_roster_sync_task", None) is None


def test_partial_app_shutdown_tolerates_missing_roster_slots(boot_run, tmp_path, monkeypatch):
    service_of(boot_run)
    del boot_run.app.state.model_roster_sync
    if hasattr(boot_run.app.state, "model_roster_sync_task"):
        del boot_run.app.state.model_roster_sync_task
    _run_boot(boot_run, tmp_path, monkeypatch, "claude-sdk")
    assert getattr(boot_run.app.state, "model_roster_sync_task", None) is None
