"""Deadline and lifetime behavior of the main-listener delivery barrier."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import api_readiness
from pinky_daemon.api_readiness import ApiReadiness, DeferredPrompt


@pytest.mark.asyncio
async def test_unattached_embedder_is_open_without_waiting_or_a_monitor():
    gate = ApiReadiness()
    gate.start()
    task = asyncio.create_task(gate.wait("embedder"))
    try:
        await asyncio.sleep(0)
        assert task.done(), "An embedder without a listener owner must deliver immediately"
        assert task.result() is True
        assert gate.can_submit("embedder") is True
        assert gate._monitor_task is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("raw", (None, "", "invalid", "nan", "inf", "0", "-1"))
@pytest.mark.asyncio
async def test_default_and_invalid_caps_use_six_hundred_seconds(monkeypatch, capsys, raw):
    if raw is None:
        monkeypatch.delenv("PINKY_API_READINESS_CAP_SEC", raising=False)
    else:
        monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", raw)
    gate = ApiReadiness()
    gate.attach(SimpleNamespace(started=False, should_exit=False))
    gate.start()
    try:
        assert gate.cap_seconds == 600
        if raw is not None:
            assert "WARNING" in capsys.readouterr().err
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_one_shared_deadline_refuses_old_waiters_and_late_ready_opens_only_new(
    monkeypatch, capsys,
):
    clock = [100.0]
    monkeypatch.setattr(api_readiness, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "0.5")
    gate = ApiReadiness()
    server = SimpleNamespace(started=False, should_exit=False)
    gate.attach(server)
    gate.start()
    callback = AsyncMock()
    gate.after_ready(callback)
    gate.after_ready(callback)
    first = asyncio.create_task(gate.wait("first"))
    second = None
    try:
        await asyncio.sleep(0)
        clock[0] = 100.3
        second = asyncio.create_task(gate.wait("second"))
        await asyncio.sleep(0)
        assert gate.started_at == 100.0
        clock[0] = 100.6
        gate._refresh()
        assert await first is False
        assert await second is False
        assert gate.refused_count == 2
        assert await gate.wait("after-cap") is False
        assert gate.refused_count == 3
        assert not gate.closed and callback.await_count == 0
        server.started = True
        gate._refresh()
        assert await gate.wait("new-after-ready") is True
        await gate._after_ready_task
        gate._refresh()
        assert await first is False and await second is False
        assert callback.await_count == 1
        output = capsys.readouterr().err
        assert "ERROR" in output and "source=first,second" in output
        assert "WARNING" in output and "ready after cap" in output
    finally:
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None),
                             return_exceptions=True)
        await gate.close()


@pytest.mark.asyncio
async def test_should_exit_refuses_waiters_and_never_reopens_after_a_late_flag(monkeypatch, capsys):
    monkeypatch.setenv("PINKY_API_READINESS_CAP_SEC", "10")
    gate = ApiReadiness()
    server = SimpleNamespace(started=False, should_exit=False)
    gate.attach(server)
    gate.start()
    callback = AsyncMock()
    gate.after_ready(callback)
    task = asyncio.create_task(gate.wait("internal"))
    try:
        await asyncio.sleep(0)
        server.should_exit = True
        gate._refresh()
        assert await task is False
        server.started = True
        assert await gate.wait("late") is False
        assert callback.await_count == 0 and gate.closed
        assert "ERROR" in capsys.readouterr().err
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gate.close()
    assert gate._monitor_task.done() and gate._after_ready_task.done()


def test_deferred_wake_reserves_preview_without_consuming_until_render():
    calls = []
    prompt = DeferredPrompt("preview", lambda: calls.append("build") or "committed wake")
    assert isinstance(prompt, str) and str(prompt) == "preview" and calls == []
    assert prompt.render() == "committed wake"
    assert prompt.render() == "committed wake" and calls == ["build"]
