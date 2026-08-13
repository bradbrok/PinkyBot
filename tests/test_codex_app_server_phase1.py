"""Phase-1 app-server safety regressions through the command-override fake."""

from __future__ import annotations

import asyncio
import shlex
import sys
import time
from pathlib import Path

import pytest

from pinky_daemon.codex_session import CodexSession, CodexTurnResult
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.transport_state import SessionState

_FAKE = Path(__file__).with_name("_fake_app_server.py")


async def _noop() -> None:
    return None


def _session(
    monkeypatch,
    tmp_path: Path,
    *,
    mode: str,
    agent_name: str = "phase1-agent",
    init_timeout: float = 0.2,
) -> CodexSession:
    working_dir = tmp_path / agent_name
    working_dir.mkdir()
    monkeypatch.delenv("PINKY_CODEX_APP_SERVER", raising=False)
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER_AGENTS", agent_name)
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER_INIT_TIMEOUT", str(init_timeout))
    monkeypatch.setenv(
        "PINKY_CODEX_APP_SERVER_CMD",
        shlex.join([sys.executable, str(_FAKE), "--mode", mode]),
    )
    session = CodexSession(
        StreamingSessionConfig(
            agent_name=agent_name,
            label="main",
            working_dir=str(working_dir),
            provider_url="codex_cli",
            provider_key="test-key",
        )
    )
    # connect() should exercise only substrate/state behavior in these tests.
    session._start_worker = lambda: None  # type: ignore[assignment]
    session._enqueue_wake = _noop  # type: ignore[assignment]
    session._analytics_session_started = lambda: None  # type: ignore[assignment]
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("hang-init", "timeout"),
        ("error-init", "error"),
        ("die-pre-init", "error"),
    ],
)
async def test_init_failure_degrades_to_exec_without_terminalizing(
    monkeypatch, tmp_path, mode, reason
):
    logs: list[str] = []
    monkeypatch.setattr("pinky_daemon.codex_session._log", logs.append)
    session = _session(monkeypatch, tmp_path, mode=mode)

    await asyncio.wait_for(session.connect(), timeout=1)

    assert session.state == SessionState.CONNECTED
    assert session._use_app_server is False
    assert session._app_server_degraded is True
    assert session._app_client is None
    assert session._app_proc is None
    assert session.stats["app_server_mode"] == "degraded_exec"
    assert any(
        f"app_server_init_failed reason={reason} agent=phase1-agent" in line
        for line in logs
    )
    assert any("app_server_degraded_to_exec agent=phase1-agent" in line for line in logs)


@pytest.mark.asyncio
async def test_init_failure_delivers_same_turn_via_exec(monkeypatch, tmp_path):
    session = _session(
        monkeypatch, tmp_path, mode="hang-init", init_timeout=0.1
    )
    legacy_prompts: list[str] = []

    async def fake_exec(prompt: str, **_kwargs) -> CodexTurnResult:
        legacy_prompts.append(prompt)
        return CodexTurnResult(text_parts=["legacy reply"])

    session._exec_codex = fake_exec  # type: ignore[assignment]
    result = await asyncio.wait_for(
        session._exec_codex_app_server("keep working"), timeout=1
    )

    assert legacy_prompts == ["keep working"]
    assert result.text_parts == ["legacy reply"]
    assert result.failed is False
    assert session._use_app_server is False
    assert session._app_server_degraded is True


@pytest.mark.asyncio
async def test_hung_init_does_not_convoy_a_second_session(monkeypatch, tmp_path):
    slow = _session(
        monkeypatch,
        tmp_path,
        mode="hang-init",
        agent_name="slow-agent",
        init_timeout=0.4,
    )
    slow_task = asyncio.create_task(slow.connect())
    for _ in range(100):
        if slow._app_proc is not None:
            break
        await asyncio.sleep(0.005)
    assert slow._app_proc is not None

    fast = _session(
        monkeypatch,
        tmp_path,
        mode="happy",
        agent_name="fast-agent",
        init_timeout=0.4,
    )
    started = time.monotonic()
    await asyncio.wait_for(fast.connect(), timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 0.4
    assert fast.state == SessionState.CONNECTED
    assert fast._use_app_server is True
    await asyncio.wait_for(slow_task, timeout=1)
    assert slow.state == SessionState.CONNECTED
    assert slow._app_server_degraded is True
    await fast._teardown_app_server()


@pytest.mark.asyncio
async def test_serialized_boot_convoy_is_capped_by_init_bound(monkeypatch, tmp_path):
    slow = _session(
        monkeypatch,
        tmp_path,
        mode="hang-init",
        agent_name="serialized-slow",
        init_timeout=0.15,
    )
    fast = _session(
        monkeypatch,
        tmp_path,
        mode="happy",
        agent_name="serialized-fast",
        init_timeout=0.15,
    )

    started = time.monotonic()
    await slow.connect()
    await fast.connect()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert slow._app_server_degraded is True
    assert fast.state == SessionState.CONNECTED
    await fast._teardown_app_server()


@pytest.mark.asyncio
async def test_happy_0144_turn_and_counters(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path, mode="happy")
    try:
        assert await session._ensure_app_server() is True
        result = await asyncio.wait_for(
            session._exec_codex_app_server("hello"), timeout=1
        )
        assert result.failed is False
        assert result.text_parts == ["fake app-server reply"]
        assert session.codex_session_id.startswith("fake-thread-")
        assert session.stats["app_server_counters"] == {
            "turns_ok": 1,
            "turns_failed": 0,
            "respawns": 0,
        }
    finally:
        await session._teardown_app_server()


@pytest.mark.asyncio
async def test_error_notification_terminally_resolves_turn(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path, mode="error-notification")
    try:
        assert await session._ensure_app_server() is True
        result = await asyncio.wait_for(
            session._exec_codex_app_server("fail tightly"), timeout=1
        )
        assert result.failed is True
        assert result.errors == ["scripted terminal error"]
        assert session.stats["app_server_counters"]["turns_failed"] == 1
    finally:
        await session._teardown_app_server()


@pytest.mark.asyncio
async def test_child_eof_after_acceptance_fails_promptly_and_respawns(
    monkeypatch, tmp_path
):
    logs: list[str] = []
    monkeypatch.setattr("pinky_daemon.codex_session._log", logs.append)
    session = _session(monkeypatch, tmp_path, mode="eof-after-acceptance")
    receipt = asyncio.get_running_loop().create_future()
    acceptance_order: list[bool] = []

    def persist_exact_fire() -> bool:
        acceptance_order.append(receipt.done())
        return True

    started = time.monotonic()
    first = await asyncio.wait_for(
        session._exec_codex_app_server(
            "accepted then eof",
            scheduler_delivery=receipt,
            scheduler_accept=persist_exact_fire,
        ),
        timeout=1,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1
    assert first.failed is True
    assert first.errors == ["app-server transport closed: connection closed"]
    assert acceptance_order == [False]
    assert await receipt is True
    assert session._app_client is None
    assert session._app_proc is None
    assert any(
        "app_server_transport_closed agent=phase1-agent active_turn=true" in line
        for line in logs
    )

    # The failed transport was fully cleared. A later turn gets a fresh child
    # and retains the existing respawn-on-next-turn behavior.
    session._app_server_command = (
        sys.executable,
        str(_FAKE),
        "--mode",
        "happy",
    )
    second = await asyncio.wait_for(
        session._exec_codex_app_server("fresh child"), timeout=1
    )
    assert second.failed is False
    assert second.text_parts == ["fake app-server reply"]
    assert session.stats["app_server_counters"] == {
        "turns_ok": 1,
        "turns_failed": 1,
        "respawns": 1,
    }
    await session._teardown_app_server()


@pytest.mark.asyncio
async def test_child_exit_after_turn_respawns_on_next_turn(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path, mode="exit-after-turn")
    try:
        first = await asyncio.wait_for(
            session._exec_codex_app_server("first"), timeout=1
        )
        assert first.failed is False
        first_proc = session._app_proc
        assert first_proc is not None
        await asyncio.wait_for(first_proc.wait(), timeout=1)

        second = await asyncio.wait_for(
            session._exec_codex_app_server("second"), timeout=1
        )
        assert second.failed is False
        assert session.stats["app_server_counters"] == {
            "turns_ok": 2,
            "turns_failed": 0,
            "respawns": 1,
        }
    finally:
        await session._teardown_app_server()


@pytest.mark.asyncio
async def test_flag_off_never_enters_app_server_bringup(monkeypatch, tmp_path):
    monkeypatch.delenv("PINKY_CODEX_APP_SERVER", raising=False)
    monkeypatch.delenv("PINKY_CODEX_APP_SERVER_AGENTS", raising=False)
    # Malformed app-server-only knobs must be inert while selection is off.
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER_INIT_TIMEOUT", "not-a-number")
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER_CMD", "'unterminated")
    session = CodexSession(
        StreamingSessionConfig(
            agent_name="off-agent",
            working_dir=str(tmp_path),
            provider_url="codex_cli",
        )
    )

    async def forbidden() -> bool:  # pragma: no cover - must stay untouched
        raise AssertionError("flag-off path touched app-server")

    session._ensure_app_server = forbidden  # type: ignore[assignment]
    await session._bring_up_substrate()
    assert session._use_app_server is False
    assert "app_server_mode" not in session.stats
