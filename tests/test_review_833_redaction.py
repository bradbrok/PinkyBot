"""Nonblocking legacy redaction audit; synthetic marker, no credentials."""

import pytest

from tests.test_codex_mcp_attach_loud import harness as _harness

harness = _harness


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["status", "recover", "alert"])
async def test_legacy_errors_do_not_emit_url_values(harness, path):
    h = harness
    h.add(mcp_recover=True)
    h.app.state.agents.register("test-agent", heartbeat_interval=60)
    marker = "https://example.invalid/review-only?token=synthetic"
    if path == "status":
        h.launch()

        def fail_status(name):
            raise RuntimeError(marker)

        h.watchdog._mcp_bind_status_fn = fail_status
    else:

        async def fail_recover(*args):
            raise RuntimeError(marker)

        if path == "recover":
            h.watchdog._mcp_recover_fn = fail_recover
        else:
            h.watchdog._alert_fn = fail_recover
        await h.watchdog._sweep()
        h.clock.advance(240)
    await h.watchdog._sweep()
    leaked = marker in h.caplog.text
    h.caplog.clear()
    assert not leaked, "legacy fallback emitted an exception URL value"
