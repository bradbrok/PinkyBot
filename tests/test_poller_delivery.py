"""Tests for the shared poller delivery-task helper.

Fire-and-forget create_task calls keep only a weak reference (the task can
be GC'd mid-flight) and swallow exceptions until garbage collection. The
helper must hold a strong reference until done and surface failures in the
poller log immediately.
"""

from __future__ import annotations

import asyncio

import pytest

from pinky_daemon.pollers import _DELIVERY_TASKS, _deliver_in_background


class TestDeliverInBackground:
    @pytest.mark.asyncio
    async def test_failure_is_logged_with_prefix(self, capsys):
        async def _boom():
            raise RuntimeError("broker exploded")

        task = _deliver_in_background(_boom(), "broker-poller[ivan]")
        assert task in _DELIVERY_TASKS

        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)  # let the done callback run

        captured = capsys.readouterr()
        assert "broker-poller[ivan]: broker delivery failed" in captured.err
        assert "broker exploded" in captured.err
        assert task not in _DELIVERY_TASKS

    @pytest.mark.asyncio
    async def test_success_logs_nothing_and_releases_reference(self, capsys):
        async def _ok():
            return 42

        task = _deliver_in_background(_ok(), "telegram-poller")
        assert task in _DELIVERY_TASKS

        assert await task == 42
        await asyncio.sleep(0)

        captured = capsys.readouterr()
        assert "delivery failed" not in captured.err
        assert task not in _DELIVERY_TASKS
