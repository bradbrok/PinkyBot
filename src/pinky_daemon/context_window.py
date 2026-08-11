"""Single source of truth for a session's context-window size.

The context gauge (warn/restart timing, ``context_status`` percentage) needs
one number: how many tokens fit in this session's window. Historically that
number was computed independently at several call sites, each with its own
model-name heuristic — including a ``1M-model → 1,000,000`` override that
assumed any large-context model reporting <=200k was under-reporting. That
override is wrong when a large window is *not* actually active for the
session: the harness then reports the true 200k, the override inflates the
denominator ~5x, and the percentage reads ~5x low, so an agent trusting the
gauge runs past the real limit. The mirror failure hits models with no
harness-reported window and no table entry: they fall to the default and the
gauge over-reports, restarting early.

``resolve_context_window`` replaces all of that with three tiers:

1. Trust the harness-reported window when present. It reflects the real
   session cap in both directions, with no model-name guessing.
2. Otherwise use a per-model map that is augmentable at runtime via the
   ``PINKY_MODEL_CONTEXT_SIZES`` environment variable — so a new model's
   window is a config change, not a code change.
3. Otherwise fall back to a conservative default. A smaller-than-real window
   only costs an earlier restart; a larger-than-real window risks a silent
   wall, so the safe direction is to under-estimate.
"""

from __future__ import annotations

import json
import os
import sys

DEFAULT_CONTEXT_WINDOW = 200_000


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

# Environment override: a JSON object mapping a case-insensitive model-name
# substring to its window size, e.g. ``{"gpt-5.6-terra": 300000}``. Entries
# here win over the built-in map, so an operator can register a new model's
# window without a code change.
_ENV_OVERRIDE_KEY = "PINKY_MODEL_CONTEXT_SIZES"

# Remember malformed override values we've already complained about so a bad
# env var logs once, not on every gauge tick.
_warned_bad_env: set[str] = set()

# Sanity ceiling for a configured window — far above any real model window,
# low enough to reject absurd/garbage values.
_MAX_REASONABLE_WINDOW = 100_000_000


def _coerce_window(value: object) -> int | None:
    """Coerce a single override value to a positive int, or ``None`` to skip.

    Accepts a JSON integer or an all-digits string; rejects ``bool`` (a JSON
    ``true``/``false`` would otherwise become ``1``/``0``), ``float`` (e.g.
    ``123.75`` or ``1e309`` -> ``inf``), ``null``, nested structures,
    non-numeric strings, and non-positive or out-of-range values. Never raises,
    so one bad entry is skipped rather than breaking the whole override.
    """
    # bool is an int subclass — exclude it before the int check.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        size = value
    elif isinstance(value, str) and value.strip().lstrip("+").isdigit():
        size = int(value.strip())
    else:
        return None
    if 0 < size <= _MAX_REASONABLE_WINDOW:
        return size
    return None


def _env_override_map() -> dict[str, int]:
    """Parse ``PINKY_MODEL_CONTEXT_SIZES`` defensively.

    Returns an empty map (never raises) when the variable is absent, empty, or
    malformed; a malformed value is logged once.
    """
    raw = os.environ.get(_ENV_OVERRIDE_KEY, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
    except (ValueError, TypeError) as exc:
        if raw not in _warned_bad_env:
            _warned_bad_env.add(raw)
            _log(
                f"context_window: ignoring malformed {_ENV_OVERRIDE_KEY} "
                f"({type(exc).__name__}: {exc}) — falling back to built-in sizes"
            )
        return {}
    # Valid JSON object: keep the good entries, skip any bad one individually
    # rather than discarding the whole override for one typo.
    out: dict[str, int] = {}
    for key, value in parsed.items():
        size = _coerce_window(value)
        if size is not None:
            out[str(key).lower()] = size
    return out


def _builtin_sizes() -> dict[str, int]:
    """The built-in per-model seed map.

    Imported lazily to keep this module free of the ``sessions`` import graph
    (mirrors the existing lazy-import pattern at the transport call sites).
    """
    try:
        from pinky_daemon.sessions import MODEL_CONTEXT_SIZES

        return dict(MODEL_CONTEXT_SIZES)
    except Exception:  # pragma: no cover - defensive; sessions always imports
        return {"default": DEFAULT_CONTEXT_WINDOW}


def _longest_substring_match(model: str, mapping: dict[str, int]) -> int:
    """Return the value whose key is the LONGEST substring of ``model``.

    The ``"default"`` key is never matched as a substring. Longest-match makes
    a more specific key (e.g. ``gpt-5.6-luna-max``) win over a broader one
    (``gpt-5.6-luna``) regardless of dict order. Returns ``0`` when nothing
    matches.
    """
    best_len = -1
    best_size = 0
    for key, size in mapping.items():
        if key == "default" or not key:
            continue
        if key in model and len(key) > best_len:
            best_len = len(key)
            best_size = size
    return best_size


def configured_context_window(model_id: str) -> int:
    """Return the configured window for ``model_id`` by substring match.

    The ``PINKY_MODEL_CONTEXT_SIZES`` env override is consulted first and wins
    over the built-in seed map; within each tier the longest (most specific)
    matching key wins. The ``"default"`` key is never matched as a substring.
    Returns ``0`` when nothing matches, letting the caller decide the fallback.
    """
    model = (model_id or "").lower()
    if not model:
        return 0
    env_hit = _longest_substring_match(model, _env_override_map())
    if env_hit:
        return env_hit
    return _longest_substring_match(model, _builtin_sizes())


def resolve_context_window(model_id: str, *, reported_max: int = 0) -> int:
    """Resolve the token window to use for ``model_id``'s context gauge.

    Tier 1: trust the harness-reported effective window (``reported_max``) when
    it is a positive number — it reflects the real session cap in both
    directions, so no model-name heuristic is needed.

    Tier 2: no harness report (e.g. a transport that reports no window) — use
    the configurable per-model map (:func:`configured_context_window`).

    Tier 3: nothing known — a conservative default. Under-estimating only costs
    an earlier restart; over-estimating risks a silent wall.
    """
    if reported_max and reported_max > 0:
        return reported_max
    configured = configured_context_window(model_id)
    if configured:
        return configured
    return DEFAULT_CONTEXT_WINDOW
