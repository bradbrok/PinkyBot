"""Boot timing covers integrity, the catalog walk and process-to-ready time."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
import time

import pytest

from pinky_daemon import __main__ as entry
from pinky_daemon import api
from pinky_daemon import store_authority


class _Clock:
    def __init__(self, value=0.0):
        self.value = value

    def monotonic(self):
        return self.value

    def __getattr__(self, name):
        return getattr(time, name)


def test_api_logs_integrity_and_catalog_durations(tmp_path, monkeypatch):
    clock, messages = _Clock(), []
    preflight = api.DaemonStoreCatalog.preflight_integrity
    validate = api.DaemonStoreCatalog.validate

    def integrity(self, *args, **kwargs):
        result = preflight(self, *args, **kwargs)
        clock.value += 0.25
        return result

    def layout(self, *args, **kwargs):
        result = validate(self, *args, **kwargs)
        clock.value += 0.125
        return result

    monkeypatch.setattr(api, "time", clock)
    monkeypatch.setattr(api, "_log", messages.append)
    monkeypatch.setattr(api.DaemonStoreCatalog, "preflight_integrity", integrity)
    monkeypatch.setattr(api.DaemonStoreCatalog, "validate", layout)
    app = api.create_api(db_path=str(tmp_path / "data/conversations.db"))
    try:
        assert messages.count("startup: phase preflight_integrity 250 ms") == 1
        assert messages.count("startup: phase store_catalog_validate 125 ms") == 1
    finally:
        app.state.store_catalog.shutdown(deadline_seconds=5)


def test_process_stamp_precedes_store_authority_wait(monkeypatch):
    events = []
    args = SimpleNamespace(db_path="unused")

    @contextmanager
    def authority(path):
        events.append(("authority", getattr(args, "_startup_started_at", None)))
        yield

    def serve(arguments):
        events.append(("serve", getattr(arguments, "_startup_started_at", None)))

    monkeypatch.setattr(entry, "time", _Clock(10), raising=False)
    monkeypatch.setattr(store_authority, "store_authority_lock", authority)
    monkeypatch.setattr(entry, "_run_api_with_authority", serve)
    entry._run_api(args)
    assert events == [("authority", 10), ("serve", 10)]


def test_entry_point_hands_process_stamp_to_api(tmp_path, monkeypatch):
    import uvicorn

    app = api.create_api(db_path=str(tmp_path / "data/conversations.db"))
    observed = []

    class StopBeforeListener(Exception):
        pass

    def server(config):
        observed.append(getattr(app.state, "startup_started_at", None))
        raise StopBeforeListener

    monkeypatch.setattr(api, "create_api", lambda **kwargs: app)
    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: None)
    monkeypatch.setattr(uvicorn, "Server", server)
    args = SimpleNamespace(working_dir=str(tmp_path), max_sessions=1,
                           db_path=str(tmp_path / "data/conversations.db"),
                           host="127.0.0.1", port=0, _startup_started_at=7.0)
    try:
        with pytest.raises(StopBeforeListener):
            entry._run_api_with_authority(args)
        assert observed == [7.0]
    finally:
        app.state.store_catalog.shutdown(deadline_seconds=5)


async def test_total_boot_time_is_logged_before_background_sweep(tmp_path, monkeypatch):
    from tests.test_boot_permission_sweep import _Child, _background_task, _output

    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setenv("PINKY_LOG_ROTATION", "off")
    app = api.create_api(db_path=str(tmp_path / "data/conversations.db"))
    app.state.startup_started_at = 100.0
    monkeypatch.setattr(api, "time", _Clock(103.0))
    events = []
    monkeypatch.setattr(api, "_log", events.append)
    original = asyncio.create_subprocess_exec

    async def create(*args, **kwargs):
        if "pinky_daemon.db_security" in args:
            events.append("permission-child")
            return _Child(_output())
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    try:
        async with app.router.lifespan_context(app):
            expected = "startup: phase startup_complete 3000 ms"
            assert events.count(expected) == 1
            await asyncio.wait_for(_background_task(app), 5)
            assert events.index(expected) < events.index("permission-child")
    finally:
        app.state.store_catalog.shutdown(deadline_seconds=5)
