"""Verify the hang case rejects an init deadline longer than configured."""
import asyncio

import pytest

from . import test_codex_app_server_phase1 as phase1


@pytest.mark.asyncio
async def test_hang_case_rejects_longer_init_deadline(monkeypatch, tmp_path):
    original = phase1._session
    sessions = []

    def extended_timeout_session(*args, **kwargs):
        session = original(*args, **kwargs)
        assert session._app_server_init_timeout == 0.2
        session._app_server_init_timeout = 1.5
        sessions.append(session)
        return session

    monkeypatch.setattr(phase1, "_session", extended_timeout_session)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await phase1.test_init_failure_degrades_to_exec_without_terminalizing(
                monkeypatch, tmp_path, "hang-init", "timeout", 0.2, 0
            )
    finally:
        for session in sessions:
            await session._teardown_app_server()
