"""Keep unrelated cyclic collection outside short wall-clock measurements."""

import gc
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def gc_quiet() -> Iterator[None]:
    """Collect before timing starts, then restore the caller's automatic-GC state."""
    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
