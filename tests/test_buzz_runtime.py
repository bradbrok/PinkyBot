"""Boot dependency refusal tests for Buzz platform registration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pinky_daemon.buzz_runtime import register_buzz_outbound_platforms
from pinky_outreach.buzz_dependency import missing_buzz_dependencies


class _Registry:
    def __init__(self):
        self.refused = []
        self.ready = []

    def list_buzz_identities(self, *, enabled_only=False):
        assert enabled_only is True
        return [{"agent": "barsik"}]

    def mark_buzz_dependency_refused(self, agent, reason):
        self.refused.append((agent, reason))

    def mark_buzz_dependency_ready(self, agent):
        self.ready.append(agent)


def test_dependency_probe_detects_present_but_unloadable_native_module(monkeypatch):
    def fake_import(name):
        if name == "coincurve":
            raise OSError("native extension could not load")
        return object()

    monkeypatch.setattr("pinky_outreach.buzz_dependency.importlib.import_module", fake_import)

    assert missing_buzz_dependencies() == ("coincurve",)


@pytest.mark.asyncio
async def test_missing_dependency_refuses_registration_and_notifies_owner(monkeypatch):
    registry = _Registry()
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "pinky_daemon.buzz_runtime.missing_buzz_dependencies",
        lambda: ("coincurve",),
    )

    result = await register_buzz_outbound_platforms(registry, notify)

    assert result == {
        "registered": 0,
        "refused": 1,
        "missing_dependencies": ["coincurve"],
    }
    assert registry.refused == [("barsik", "missing:coincurve")]
    assert registry.ready == []
    notify.assert_awaited_once()
    assert "REFUSED" in notify.await_args.args[1]
    assert "coincurve" in notify.await_args.args[1]


@pytest.mark.asyncio
async def test_healed_dependencies_restore_registration(monkeypatch):
    registry = _Registry()
    notify = AsyncMock()
    monkeypatch.setattr(
        "pinky_daemon.buzz_runtime.missing_buzz_dependencies",
        lambda: (),
    )

    result = await register_buzz_outbound_platforms(registry, notify)

    assert result["registered"] == 1
    assert result["refused"] == 0
    assert registry.ready == ["barsik"]
    assert registry.refused == []
    notify.assert_not_awaited()
