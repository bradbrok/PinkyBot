"""Bounded verification owns cleanup; ordinary calls preserve cancellation."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon.command_runner import LocalCommandRunner


@pytest.mark.parametrize("failure", ["oversize", "cancel"])
async def test_bounded_failure_kills_reaps_and_drains_owned_child(monkeypatch, failure):
    real_create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    children = []

    async def create(*args, **kwargs):
        child = await real_create(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    script = "import sys,time;"
    if failure == "oversize":
        script += "sys.stdout.buffer.write(b'x'*100000);sys.stdout.flush();"
    script += "time.sleep(60)"
    task = asyncio.create_task(LocalCommandRunner().run(
        [sys.executable, "-I", "-c", script], max_output_bytes=1024, timeout=5,
    ))
    try:
        await asyncio.wait_for(started.wait(), 3)
        if failure == "cancel":
            await asyncio.sleep(0)
            task.cancel()
        expected = asyncio.CancelledError if failure == "cancel" else ExceptionGroup
        with pytest.raises(expected):
            await asyncio.wait_for(task, 3)
        child = children[0]
        assert child.returncode is not None, "bounded failure left its child running"
        assert child.stdout.at_eof() and child.stderr.at_eof(), "bounded failure left unread pipes"
    finally:
        # Mutation failures must also leave only reaped test-owned children.
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for child in children:
            if child.returncode is None:
                child.kill()
            await asyncio.wait_for(child.communicate(), 2)


@pytest.mark.parametrize("failure", ["cancel", "timeout"])
async def test_unbounded_calls_keep_original_kill_contract(monkeypatch, failure):
    started = asyncio.Event()

    async def communicate(**kwargs):
        started.set()
        await asyncio.Event().wait()

    child = SimpleNamespace(communicate=communicate, kill=Mock(), wait=AsyncMock())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    task = asyncio.create_task(LocalCommandRunner().run(
        ["synthetic-client"], timeout=0.01 if failure == "timeout" else None,
    ))
    await started.wait()
    if failure == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else asyncio.TimeoutError):
        await task
    assert child.kill.call_count == (1 if failure == "timeout" else 0)
    child.wait.assert_not_awaited()
