"""Shared structured-log schema for the watchdog decision points (#230).

All three watchdog paths emit ONE consistent ``key=value`` line so decisions are
greppable and auditable side-by-side:

  * the per-session in-flight watchdog (``TmuxSession._inflight_watchdog`` —
    force_restart / growing / idle-reconcile),
  * the daemon ``SessionWatchdog`` (warn / recover / inflight-skip), and
  * the scheduler idle-sleep (``_check_idle_sessions`` — sleep / inflight-skip).

The schema (key set + ordering + rounding) lives here, in ONE place, while each
path keeps its own decision logic and passes only the fields it has. Centralising
the schema — not the decisions — is the shape Murzik signed off on for #230: a
reader greps ``watchdog_decision`` and sees the same keys from every path.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("pinky.watchdog").info

# Canonical field order. A path omits any field it doesn't have (None is dropped),
# so every emitted line is a stable subset of this ordering.
_FIELDS = (
    "watchdog",  # session | idle_sleep | inflight — which watchdog acted
    "agent",
    "label",
    "decision",  # skip | warn | recover | sleep | restart | reconcile | no_op
    "reason",
    "state",
    "pending",
    "turns",
    "current_activity",
    "last_active_age_s",
    "progress_stale_s",
    "idle_timeout_s",
    "warn_after_s",
    "recover_after_s",
    "inflight_turns",
    "inflight_active",
    "inflight_liveness_reason",
    "inflight_liveness_age_s",
)


def format_watchdog_decision(**fields) -> str:
    """Render the standard ``watchdog_decision`` line from the given fields.

    Fields absent or ``None`` are dropped; floats are rounded to 1 dp so the line
    stays stable and readable. Unknown keys (not in ``_FIELDS``) are ignored so a
    caller can't silently pollute the schema.
    """
    parts = []
    for key in _FIELDS:
        val = fields.get(key)
        if val is None:
            continue
        if isinstance(val, float):
            val = round(val, 1)
        parts.append(f"{key}={val}")
    return "watchdog_decision " + " ".join(parts)


def log_watchdog_decision(**fields) -> None:
    """Emit the standard structured decision line at INFO on ``pinky.watchdog``."""
    _log(format_watchdog_decision(**fields))
