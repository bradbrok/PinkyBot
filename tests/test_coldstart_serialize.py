"""Cold-start serialization gate (#202).

A fleet that shares ``$HOME`` shares one Claude OAuth credentials file whose
refresh token is single-use (rotated on every refresh). When several agents
boot a fresh ``claude`` at once — the daemon-restart boot loop or a watchdog
batch-resurrection — their concurrent token refreshes race: one wins the
rotation and the rest die with a now-invalid refresh token, de-authing most of
the fleet.

``_coldstart_gate`` serializes the spawn→ready window behind a process-global
lock (plus a short post-ready settle so the just-booted process flushes its
rotated token before the next boot starts). It is flag-gated
(``PINKY_COLDSTART_SERIALIZE``) and defaults OFF.

These tests drive ``_bounded_cold_start_connect`` (which wraps its connect in
the gate) with a fake session that records boot concurrency and timing.
"""
from __future__ import annotations

import asyncio

import pytest

from pinky_daemon import api


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    """Each test starts with a fresh, unbound process-global gate."""
    monkeypatch.setattr(api, "_coldstart_lock", None)
    monkeypatch.setattr(api, "_coldstart_lock_loop", None)
    yield


class _Recorder:
    def __init__(self, loop):
        self._loop = loop
        self.active = 0
        self.max_active = 0
        self.events: list[tuple[str, str, float]] = []

    def mark(self, kind: str, name: str) -> None:
        self.events.append((kind, name, self._loop.time()))


class _FakeBootSession:
    """connect() that tracks concurrency; can run fast or wedge (for timeout)."""

    def __init__(self, rec: _Recorder, name: str, *, delay: float = 0.05, fail: bool = False):
        self.rec = rec
        self.name = name
        self.delay = delay
        self.fail = fail
        self.disconnect_called = False

    async def connect(self) -> None:
        self.rec.active += 1
        self.rec.max_active = max(self.rec.max_active, self.rec.active)
        self.rec.mark("start", self.name)
        try:
            if self.fail:
                await asyncio.sleep(3600)  # cancelled by the umbrella timeout
            await asyncio.sleep(self.delay)
        finally:
            self.rec.active -= 1
        self.rec.mark("end", self.name)

    async def disconnect(self) -> None:
        self.disconnect_called = True


async def _boot(ss, name: str):
    await api._bounded_cold_start_connect(ss, agent_name=name, label="main")


@pytest.mark.asyncio
async def test_serialized_boots_never_overlap(monkeypatch):
    monkeypatch.setattr(api, "COLDSTART_SERIALIZE", True)
    monkeypatch.setattr(api, "COLDSTART_SETTLE_SEC", 0.0)
    rec = _Recorder(asyncio.get_running_loop())
    a = _FakeBootSession(rec, "A", delay=0.05)
    b = _FakeBootSession(rec, "B", delay=0.05)
    await asyncio.gather(_boot(a, "A"), _boot(b, "B"))
    assert rec.max_active == 1, f"serialized boots must not overlap (max_active={rec.max_active})"
    kinds = [(k, n) for (k, n, _t) in rec.events]
    assert kinds in (
        [("start", "A"), ("end", "A"), ("start", "B"), ("end", "B")],
        [("start", "B"), ("end", "B"), ("start", "A"), ("end", "A")],
    ), f"boots interleaved: {kinds}"


@pytest.mark.asyncio
async def test_disabled_gate_allows_overlap_and_creates_no_lock(monkeypatch):
    monkeypatch.setattr(api, "COLDSTART_SERIALIZE", False)
    rec = _Recorder(asyncio.get_running_loop())
    a = _FakeBootSession(rec, "A", delay=0.1)
    b = _FakeBootSession(rec, "B", delay=0.1)
    await asyncio.gather(_boot(a, "A"), _boot(b, "B"))
    assert rec.max_active == 2, "without serialization, concurrent boots should overlap"
    assert api._coldstart_lock is None, "disabled gate must not create a lock"


@pytest.mark.asyncio
async def test_settle_delays_the_next_boot(monkeypatch):
    monkeypatch.setattr(api, "COLDSTART_SERIALIZE", True)
    monkeypatch.setattr(api, "COLDSTART_SETTLE_SEC", 0.3)
    loop = asyncio.get_running_loop()
    rec = _Recorder(loop)
    a = _FakeBootSession(rec, "A", delay=0.0)
    b = _FakeBootSession(rec, "B", delay=0.0)
    ta = asyncio.create_task(_boot(a, "A"))
    await asyncio.sleep(0.02)  # ensure A grabs the lock first
    tb = asyncio.create_task(_boot(b, "B"))
    await asyncio.gather(ta, tb)
    starts = {n: t for (k, n, t) in rec.events if k == "start"}
    # A is instantaneous but holds the lock through the 0.3s settle, so B's
    # boot can't start until ~settle after A's.
    assert starts["B"] - starts["A"] >= 0.25, (
        f"settle did not hold the gate (gap={starts['B'] - starts['A']:.3f}s)"
    )


@pytest.mark.asyncio
async def test_failed_boot_skips_settle_and_releases_lock(monkeypatch):
    monkeypatch.setattr(api, "COLDSTART_SERIALIZE", True)
    # A huge settle that would hang the test if (wrongly) applied after a failure.
    monkeypatch.setattr(api, "COLDSTART_SETTLE_SEC", 10.0)
    loop = asyncio.get_running_loop()
    rec = _Recorder(loop)
    bad = _FakeBootSession(rec, "BAD", fail=True)
    t0 = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await api._bounded_cold_start_connect(
            bad, agent_name="BAD", label="main", timeout=0.05
        )
    elapsed = loop.time() - t0
    assert elapsed < 1.0, f"failed boot must skip the 10s settle (took {elapsed:.2f}s)"
    assert bad.disconnect_called, "umbrella cleanup must still run under the gate"
    assert not api._get_coldstart_lock().locked(), "lock must be released after a failed boot"


@pytest.mark.asyncio
async def test_lock_is_singleton_within_a_loop():
    assert api._get_coldstart_lock() is api._get_coldstart_lock()


def test_default_is_off():
    """Ships disabled — enabled per-fleet via env, soaked before flipping default."""
    import os

    assert os.environ.get("PINKY_COLDSTART_SERIALIZE") in (None, "", "0", "false", "off", "no")
    assert api.COLDSTART_SERIALIZE is False
