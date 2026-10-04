"""Per-turn token-cost pricing for the daemon.

The SDK transport gets ``total_cost_usd`` for free on each
``ResultMessage``. The tmux transport does not — Claude Code runs under
a subscription and the transcript carries only token *counts*, never a
dollar figure. To reach Analytics / lifetime-cost parity (issue #648)
we have to compute the cost ourselves from the token counts and a rate
table.

This module is the version-controlled twin of the rate table that
``scripts/burn_cost_report.py`` reads from
``~/.pinkybot/burn/rates/*.jsonl`` for the post-hoc snapshot overlay.
The post-hoc tool keeps its rates out-of-tree so historical snapshots
can be repriced without a code change; the *live* daemon path needs the
rates compiled in so a fresh deploy prices turns correctly with no
external file dependency. The two must agree on the math — the cost
breakdown here mirrors ``compute_row_cost`` exactly (split 5m/1h
cache-write billing, 1h fallback when the split is absent).

All prices are USD per million tokens (``usd_per_mtok``). Cache-creation
("write") tokens are billed at one of two rates depending on the
ephemeral TTL the API chose for that prompt prefix:

* 5-minute ephemeral cache → ``cache_write_5m`` (1.25× base input)
* 1-hour   ephemeral cache → ``cache_write_1h`` (2× base input)

Cache-*read* tokens are billed at the cheap ``cache_read`` rate.
"""

from __future__ import annotations

from pinky_daemon.model_roster import load_bundled
from pinky_daemon.runtime_model_catalog import (
    ModelCatalogReadError,
    lookup_model,
    strip_tier,
)

_M = 1_000_000

# Preserve historical pricing for identifiers absent from the bundled catalog.
_LEGACY_RATES = {
    "claude-haiku-3-5": {
        "input": 0.80, "output": 4.00, "cache_read": 0.08,
        "cache_write_5m": 1.00, "cache_write_1h": 1.60,
    },
    "claude-opus-4": {
        "input": 15.00, "output": 75.00, "cache_read": 1.50,
        "cache_write_5m": 18.75, "cache_write_1h": 30.00,
    },
    "claude-opus-4-1": {
        "input": 15.00, "output": 75.00, "cache_read": 1.50,
        "cache_write_5m": 18.75, "cache_write_1h": 30.00,
    },
    "claude-sonnet-4": {
        "input": 3.00, "output": 15.00, "cache_read": 0.30,
        "cache_write_5m": 3.75, "cache_write_1h": 6.00,
    },
    "gpt-5.3-codex": {
        "input": 1.75, "output": 14.00, "cache_read": 0.175,
        "cache_write_5m": 0.0, "cache_write_1h": 0.0,
    },
}


def _build_rate_table() -> dict[str, dict[str, float]]:
    rates = dict(_LEGACY_RATES)
    pool: dict[tuple[float, float, float, float, float], dict[str, float]] = {}
    for row in load_bundled().models:
        if row.model_id in _LEGACY_RATES:
            raise ValueError(f"Bundled roster overlaps legacy rate id: {row.model_id}")
        price = row.pricing
        key = (price.input, price.output, price.cached_input,
               price.cache_write_5m, price.cache_write_1h)
        if key not in pool:
            pool[key] = {
                "input": key[0], "output": key[1], "cache_read": key[2],
                "cache_write_5m": key[3], "cache_write_1h": key[4],
            }
        rates[row.model_id] = pool[key]
    return rates


# Equal-priced catalog entries share a plain dictionary; consumers only read it.
RATE_TABLE: dict[str, dict[str, float]] = _build_rate_table()
_FABLE_51 = RATE_TABLE["claude-fable-5-1"]  # Anchor: claude-fable-5-1.
_OPUS_55 = RATE_TABLE["claude-opus-5-5"]  # Anchor: claude-opus-5-5.
_OPUS_STD = RATE_TABLE["claude-opus-5"]  # Anchor: claude-opus-5.


def lookup_rate(model_id: str) -> dict[str, float] | None:
    """Resolve a model through the runtime catalog, then the static fallback."""
    base = strip_tier(model_id)
    try:
        row = lookup_model(base)
    except ModelCatalogReadError:
        static = RATE_TABLE.get(base)
        if static is not None:
            return static
        raise
    if row is not None:
        runtime_rate = {
            "input": float(row["input_price"]),
            "output": float(row["output_price"]),
            "cache_read": float(row["cached_input_price"]),
            "cache_write_5m": float(row["cache_write_5m_price"]),
            "cache_write_1h": float(row["cache_write_1h_price"]),
        }
        static = RATE_TABLE.get(base)
        return static if runtime_rate == static else runtime_rate
    return RATE_TABLE.get(base)


def compute_turn_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_5m_tokens: int,
    cache_creation_1h_tokens: int,
) -> float:
    """Cost (USD) for one turn's token counts under ``model``'s rates.

    Returns ``0.0`` when the model is unknown (no rate row) — mirrors
    ``burn_cost_report``'s "missing rate ⇒ skip" behaviour rather than
    guessing. The caller may log the miss so a new model id gets added
    to ``RATE_TABLE``.

    The 5m/1h split mirrors ``scripts/burn_cost_report.compute_row_cost``
    exactly so the live figure agrees with the post-hoc snapshot overlay.
    """
    rate = lookup_rate(model)
    if rate is None:
        return 0.0
    inp = int(input_tokens or 0)
    out = int(output_tokens or 0)
    cr = int(cache_read_tokens or 0)
    cc_5m = int(cache_creation_5m_tokens or 0)
    cc_1h = int(cache_creation_1h_tokens or 0)
    return (
        inp / _M * rate["input"]
        + out / _M * rate["output"]
        + cr / _M * rate["cache_read"]
        + cc_5m / _M * rate["cache_write_5m"]
        + cc_1h / _M * rate["cache_write_1h"]
    )


def _split_cache_creation(usage: dict) -> tuple[int, int]:
    """Extract (5m, 1h) cache-creation token counts from a usage block.

    Claude transcripts carry the split under
    ``usage.cache_creation.{ephemeral_5m_input_tokens,
    ephemeral_1h_input_tokens}``. When that nested breakdown is absent we
    fall back to billing the entire ``cache_creation_input_tokens``
    aggregate at the 1h rate — the conservative (over-estimating) choice
    that ``burn_cost_report`` makes for pre-split snapshots.
    """
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        cc_5m = split.get("ephemeral_5m_input_tokens")
        cc_1h = split.get("ephemeral_1h_input_tokens")
        if cc_5m is not None or cc_1h is not None:
            return int(cc_5m or 0), int(cc_1h or 0)
    # No split present — bill all cache-creation at 1h (worst case).
    total = usage.get("cache_creation_input_tokens") or usage.get(
        "cache_write_tokens"
    ) or 0
    try:
        return 0, int(total or 0)
    except (TypeError, ValueError):
        return 0, 0


def compute_cost_from_usage(model: str, usage: dict) -> float:
    """Cost (USD) for a raw transcript/SDK usage dict under ``model``.

    Tolerant of both the Claude transcript key shape
    (``cache_creation_input_tokens`` / ``cache_read_input_tokens``) and
    the SDK's shortened forms (``cache_write_tokens`` /
    ``cache_read_tokens``). Malformed values are treated as zero rather
    than raising — pricing is best-effort telemetry, never a hard
    dependency of the turn pipeline.
    """
    if not isinstance(usage, dict):
        return 0.0
    try:
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cr = int(
            usage.get("cache_read_input_tokens")
            or usage.get("cache_read_tokens")
            or 0
        )
    except (TypeError, ValueError):
        return 0.0
    cc_5m, cc_1h = _split_cache_creation(usage)
    return compute_turn_cost_usd(
        model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_creation_5m_tokens=cc_5m,
        cache_creation_1h_tokens=cc_1h,
    )
