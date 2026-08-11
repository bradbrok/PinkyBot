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
    for key, size in parsed.items():
        try:
            size_int = int(size)
        except (ValueError, TypeError):
            continue
        if size_int > 0:
            out[str(key).lower()] = size_int
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


def configured_context_window(model_id: str) -> int:
    """Return the configured window for ``model_id`` by substring match.

    Merges the built-in map with the ``PINKY_MODEL_CONTEXT_SIZES`` env override
    (env wins). The ``"default"`` key is never matched as a substring. Returns
    ``0`` when nothing matches, letting the caller decide the fallback.
    """
    model = (model_id or "").lower()
    if not model:
        return 0
    merged = _builtin_sizes()
    merged.update(_env_override_map())
    for key, size in merged.items():
        if key != "default" and key and key in model:
            return size
    return 0


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
