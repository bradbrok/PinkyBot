"""Retry and diagnostics coverage for the best-effort ferry listener."""

from __future__ import annotations

import asyncio

import pytest

from pinky_daemon.api import create_api
from pinky_daemon.ferry.listener import FerryListenerState, serve_ferry_with_retry


class _FakeFerryServer:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.serve_calls = 0
        self.should_exit = False
        self.started = False
        self.up = asyncio.Event()

    async def startup(self) -> None:
        if self.serve_calls <= self.failures:
            raise OSError(f"bind failure {self.serve_calls}")
        self.started = True

    async def serve(self) -> None:
        self.serve_calls += 1
        await self.startup()
        self.up.set()
        while not self.should_exit:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_bind_failures_back_off_then_listener_recovers() -> None:
    server = _FakeFerryServer(failures=5)
    state = FerryListenerState(bind="100.64.0.8:8899")
    waits: list[float] = []
    logs: list[str] = []

    async def _wait(delay: float) -> None:
        waits.append(delay)
        await asyncio.sleep(0)

    task = asyncio.create_task(
        serve_ferry_with_retry(server, state, wait=_wait, log=logs.append)
    )
    await asyncio.wait_for(server.up.wait(), timeout=1)

    assert server.serve_calls == 6
    assert waits == [5.0, 15.0, 30.0, 60.0, 60.0]
    assert state.snapshot() == {
        "state": "up",
        "bind": "100.64.0.8:8899",
        "last_error": "",
        "retry_count": 5,
        "since": state.since,
    }
    assert sum("Ferry listener failed attempt" in line for line in logs) == 5
    assert "after 5 retries" in logs[-1]

    server.should_exit = True
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_uvicorn_bind_system_exit_is_retried() -> None:
    server = _FakeFerryServer(failures=0)
    state = FerryListenerState(bind="100.64.0.8:8899")
    original_startup = server.startup
    waits: list[float] = []

    async def _startup() -> None:
        if server.serve_calls == 1:
            raise SystemExit(1)
        await original_startup()

    async def _wait(delay: float) -> None:
        waits.append(delay)
        await asyncio.sleep(0)

    server.startup = _startup
    task = asyncio.create_task(
        serve_ferry_with_retry(server, state, wait=_wait, log=lambda _line: None)
    )
    await asyncio.wait_for(server.up.wait(), timeout=1)

    assert waits == [5.0]
    assert state.state == "up"
    assert state.retry_count == 1

    server.should_exit = True
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_should_exit_stops_listener_during_retry_wait() -> None:
    server = _FakeFerryServer(failures=100)
    state = FerryListenerState(bind="100.64.0.8:8899")
    waits: list[float] = []

    async def _wait(delay: float) -> None:
        assert state.state == "retrying"
        assert state.retry_count == 1
        assert state.last_error == "OSError: bind failure 1"
        waits.append(delay)
        server.should_exit = True
        await asyncio.sleep(0)

    await serve_ferry_with_retry(server, state, wait=_wait, log=lambda _line: None)

    assert server.serve_calls == 1
    assert waits == [5.0]
    assert state.state == "dead"
    assert state.retry_count == 1
    assert state.last_error == "OSError: bind failure 1"


@pytest.mark.asyncio
async def test_ferry_failures_never_interrupt_main_api_task() -> None:
    server = _FakeFerryServer(failures=3)
    state = FerryListenerState(bind="100.64.0.8:8899")
    stop_main = asyncio.Event()
    main_started = asyncio.Event()

    async def _main_api() -> None:
        main_started.set()
        await stop_main.wait()

    main_task = asyncio.create_task(_main_api())
    await main_started.wait()

    async def _wait(_delay: float) -> None:
        assert not main_task.done()
        await asyncio.sleep(0)

    ferry_task = asyncio.create_task(
        serve_ferry_with_retry(server, state, wait=_wait, log=lambda _line: None)
    )
    await asyncio.wait_for(server.up.wait(), timeout=1)

    assert state.state == "up"
    assert state.retry_count == 3
    assert not main_task.done()

    server.should_exit = True
    stop_main.set()
    await asyncio.gather(main_task, ferry_task)


@pytest.mark.parametrize("listener_status", ["up", "retrying", "disabled", "dead"])
@pytest.mark.asyncio
async def test_mesh_diagnostics_surfaces_listener_state(
    listener_status: str, monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("PINKYBOT_FERRY_ENABLED", raising=False)
    app = create_api(
        default_working_dir=str(tmp_path),
        db_path=str(tmp_path / f"{listener_status}.db"),
    )
    listener = app.state.ferry_listener
    listener.update(
        listener_status,
        bind="100.64.0.8:8899",
        last_error="bind unavailable" if listener_status == "retrying" else "",
        retry_count=2,
    )
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/mesh/diagnostics"
    )

    diagnostics = await endpoint()

    assert diagnostics["ferry_listener"] == {
        "state": listener_status,
        "bind": "100.64.0.8:8899",
        "last_error": "bind unavailable" if listener_status == "retrying" else "",
        "retry_count": 2,
        "since": listener.since,
    }
