"""Cold-start connect umbrella (#110).

`_start_streaming_session` registers a session in `broker._streaming` only
AFTER `ss.connect()` returns, and the SessionWatchdog samples only
`broker._streaming` — so a cold start that wedges *inside* connect() (BOOTING,
before registration) is invisible to the watchdog and would hang the start
path indefinitely. `_bounded_cold_start_connect` caps that wedge at the start
site and guarantees best-effort cleanup so the caller never registers a
half-started session.

These tests exercise the extracted helper directly (the start site itself is a
deep closure inside the app factory). The fake session mirrors the real
`connect()` cancellation contract:
  - cancel while BOOTING  → connect()'s BaseException handler drives DEAD
  - cancel after CONNECTED → handler is bypassed (state stays CONNECTED);
                             only the start-site disconnect() cleans up
"""
from __future__ import annotations

import asyncio

import pytest

from pinky_daemon.api import (
    COLD_START_CONNECT_TIMEOUT_SEC,
    _bounded_cold_start_connect,
)
from pinky_daemon.transport_state import SessionState


class _FakeColdStartSession:
    """Fake session whose connect() can wedge in BOOTING or post-CONNECTED."""

    def __init__(
        self,
        *,
        mode: str = "wedge_booting",
        disconnect_raises: bool = False,
    ) -> None:
        # mode: "wedge_booting" | "wedge_after_connected" | "fast"
        self._mode = mode
        self._disconnect_raises = disconnect_raises
        self.state = SessionState.UNINITIALIZED
        self.connect_started = False
        self.disconnect_called = False

    async def connect(self) -> None:
        self.connect_started = True
        if self._mode == "fast":
            self.state = SessionState.CONNECTED
            return
        if self._mode == "wedge_after_connected":
            # Mimic BOOT_COMPLETE → CONNECTED, then hang before returning.
            self.state = SessionState.CONNECTED
            await asyncio.sleep(3600)  # cancelled by wait_for; state stays CONNECTED
            return
        # Default: enter BOOTING and wedge. On cancellation, mimic connect()'s
        # BaseException → BOOT_FAILED → DEAD completion.
        self.state = SessionState.BOOTING
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.state = SessionState.DEAD
            raise

    async def disconnect(self) -> None:
        self.disconnect_called = True
        if self._disconnect_raises:
            raise RuntimeError("disconnect boom")


@pytest.mark.asyncio
async def test_cold_start_timeout_wedged_in_booting():
    """connect() wedges in BOOTING → TimeoutError, cleanup ran, lands DEAD."""
    ss = _FakeColdStartSession(mode="wedge_booting")
    with pytest.raises(asyncio.TimeoutError):
        await _bounded_cold_start_connect(
            ss, agent_name="a", label="main", timeout=0.05
        )
    assert ss.connect_started
    assert ss.disconnect_called, "start-site cleanup must run on timeout"
    # connect()'s own BOOT_FAILED handler drove the lifecycle to DEAD.
    assert ss.state == SessionState.DEAD


@pytest.mark.asyncio
async def test_cold_start_timeout_after_boot_complete_pre_register():
    """connect() reached CONNECTED then hung pre-return → defensive disconnect.

    This is the non-obvious window: cancellation lands after BOOT_COMPLETE, so
    connect()'s BaseException→DEAD handler is bypassed (state stays CONNECTED).
    Without the start-site disconnect() we'd discard an unregistered-but-
    connected client.
    """
    ss = _FakeColdStartSession(mode="wedge_after_connected")
    with pytest.raises(asyncio.TimeoutError):
        await _bounded_cold_start_connect(
            ss, agent_name="a", label="main", timeout=0.05
        )
    assert ss.state == SessionState.CONNECTED  # past the BOOT_FAILED catch
    assert ss.disconnect_called, "defensive cleanup must run even when CONNECTED"


@pytest.mark.asyncio
async def test_cold_start_connect_success_no_cleanup():
    """A connect() that completes in time returns cleanly, no disconnect()."""
    ss = _FakeColdStartSession(mode="fast")
    await _bounded_cold_start_connect(
        ss, agent_name="a", label="main", timeout=5
    )
    assert ss.state == SessionState.CONNECTED
    assert not ss.disconnect_called


@pytest.mark.asyncio
async def test_cold_start_timeout_disconnect_error_does_not_mask_timeout():
    """A failing cleanup disconnect() must not swallow/replace the TimeoutError."""
    ss = _FakeColdStartSession(mode="wedge_booting", disconnect_raises=True)
    with pytest.raises(asyncio.TimeoutError):
        await _bounded_cold_start_connect(
            ss, agent_name="a", label="main", timeout=0.05
        )
    assert ss.disconnect_called


def test_cold_start_timeout_default_is_sane():
    """Umbrella sits above tmux's 60s spawn bound, below the 4min transition warn."""
    assert COLD_START_CONNECT_TIMEOUT_SEC > 60.0
    assert COLD_START_CONNECT_TIMEOUT_SEC < 240.0
