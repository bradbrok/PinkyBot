"""Shared cron-field parsing helpers used by scheduler and agent_registry.

Extracted here to break the import cycle:
  scheduler  → agent_registry  (top-level)
  agent_registry → scheduler._field_matches  (would be cyclic)
"""

from __future__ import annotations

# Standard cron name tokens. Day-of-week values match isoweekday() % 7
# (0=Sunday ... 6=Saturday); month names map to 1-12.
_CRON_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _cron_int(token: str) -> int:
    """Parse a cron token: a plain integer or a 3-letter day/month name."""
    token = token.strip().lower()
    if token in _CRON_NAMES:
        return _CRON_NAMES[token]
    return int(token)


def _part_matches(part: str, value: int, lo: int, hi: int) -> bool:
    """Match a single part of a cron field (e.g., '*/5', '1-3', 'mon-fri/2', '7')."""
    if part == "*":
        return True

    step = 1
    if "/" in part:
        part, step_str = part.split("/", 1)
        step = int(step_str)
        if step <= 0:
            raise ValueError(f"invalid cron step: {step_str!r}")
        if part == "*":
            return value % step == 0
        if "-" not in part:
            start = _cron_int(part)
            return value >= start and (value - start) % step == 0

    if "-" in part:
        start_str, end_str = part.split("-", 1)
        start = _cron_int(start_str)
        end = _cron_int(end_str)
        return start <= value <= end and (value - start) % step == 0

    return value == _cron_int(part)


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """Check if a single cron field matches a value."""
    for part in field.split(","):
        if _part_matches(part.strip(), value, lo, hi):
            return True
    return False
