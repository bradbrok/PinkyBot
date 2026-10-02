"""GC isolation must unwind even when a timed operation fails or is cancelled."""

import asyncio
import gc
from contextlib import nullcontext

import pytest

from tests._gc_quiet import gc_quiet


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("error", [None, RuntimeError, asyncio.CancelledError])
def test_gc_quiet_restores_prior_state(enabled, error):
    original = gc.isenabled()
    try:
        if enabled:
            gc.enable()
        else:
            gc.disable()
        expected = pytest.raises(error) if error else nullcontext()
        with expected:
            with gc_quiet():
                assert not gc.isenabled()
                if error:
                    raise error("timed operation interrupted")
        assert gc.isenabled() == enabled
    finally:
        if original:
            gc.enable()
        else:
            gc.disable()
