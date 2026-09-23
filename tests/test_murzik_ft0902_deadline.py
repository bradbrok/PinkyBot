"""The one-second hang guard must reject an ignored init deadline."""
import asyncio

import pytest
from test_codex_app_server_phase1 import (
    _session,
    test_init_failure_degrades_to_exec_without_terminalizing,
)


@pytest.mark.asyncio
async def test_hang_case_rejects_ignored_015_second_init_deadline(monkeypatch, tmp_path):
    original = _session
    sessions = []

    def mutant_session(*args, **kwargs):
        session = original(*args, **kwargs)
        assert session._app_server_init_timeout == 0.2
        session._app_server_init_timeout = 1.5
        sessions.append(session)
        return session

    monkeypatch.setattr("test_codex_app_server_phase1._session", mutant_session)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await test_init_failure_degrades_to_exec_without_terminalizing(
                monkeypatch, tmp_path, "hang-init", "timeout", 0.2, 0
            )
    finally:
        for session in sessions:
            await session._teardown_app_server()
