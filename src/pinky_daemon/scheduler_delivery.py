"""Shared finite scheduler busy-wait policy."""

import math
import os


def scheduler_busy_delay(value=None) -> float:
    """Resolve an explicit setting or environment value, defaulting to 600s."""
    if value is None or value == "":
        value = os.environ.get("SCHEDULER_BUSY_DELIVER_AFTER_S", "600")
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = 600.0
    return delay if math.isfinite(delay) and delay > 0 else 600.0
