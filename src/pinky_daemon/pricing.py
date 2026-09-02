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

from pinky_daemon.runtime_model_catalog import (
    ModelCatalogReadError,
    lookup_model,
    strip_tier,
)

_M = 1_000_000

# --------------------------------------------------------------------- #
# Rate table — keep in sync with ~/.pinkybot/burn/rates/anthropic_*.jsonl
# (the snapshot overlay's out-of-tree copy). All values usd_per_mtok.
# --------------------------------------------------------------------- #

# Standard Opus tier (4.5 and newer): the price has been flat across
# 4.5 → 4.6 → 4.7 → 4.8 → 5. 1M context is sold at the same per-token price
# as 200K, so no tier split is needed here.
_OPUS_STD = {
    "input": 5.00,
    "output": 25.00,
    "cache_read": 0.50,
    "cache_write_5m": 6.25,
    "cache_write_1h": 10.00,
}
# Fable / Mythos 5 (2026-06-09): the new flagship family. 1M context,
# priced at $10 in / $50 out per Mtok; same cache ratios as Opus
# (read 0.1×, write-5m 1.25×, write-1h 2×). Mythos shares Fable's pricing.
_FABLE = {
    "input": 10.00,
    "output": 50.00,
    "cache_read": 1.00,
    "cache_write_5m": 12.50,
    "cache_write_1h": 20.00,
}
# Fable / Mythos 5.1 (2026-09-01): extends Fable 5 at the same $10/$50 per
# Mtok, but cache reads are 4× cheaper ($0.25 vs $1.00). 1M context, 128K
# output. Mythos 5.1 shares Fable 5.1's pricing.
_FABLE_51 = {
    "input": 10.00,
    "output": 50.00,
    "cache_read": 0.25,
    "cache_write_5m": 12.50,
    "cache_write_1h": 20.00,
}
# Pre-4.5 Opus (4, 4.1): the old 3×-more-expensive tier. Kept for
# historical-transcript pricing accuracy.
_OPUS_LEGACY = {
    "input": 15.00,
    "output": 75.00,
    "cache_read": 1.50,
    "cache_write_5m": 18.75,
    "cache_write_1h": 30.00,
}
_SONNET = {
    "input": 3.00,
    "output": 15.00,
    "cache_read": 0.30,
    "cache_write_5m": 3.75,
    "cache_write_1h": 6.00,
}
_HAIKU_45 = {
    "input": 1.00,
    "output": 5.00,
    "cache_read": 0.10,
    "cache_write_5m": 1.25,
    "cache_write_1h": 2.00,
}
_HAIKU_35 = {
    "input": 0.80,
    "output": 4.00,
    "cache_read": 0.08,
    "cache_write_5m": 1.00,
    "cache_write_1h": 1.60,
}
# OpenAI / Codex family (#860): powers the live tmux cost path for codex
# agents (CodexTmuxSession rides the same _log_turn_cost_and_analytics as
# Claude tmux). Official OpenAI API rates (developers.openai.com, verified
# 2026-07-10); codex agents run on sign-in subscription, so as with CC
# subscription agents these are notional usage-value figures (#648).
# Cache-write billing DIFFERS per model page: gpt-5.6-sol documents cache
# writes at 1.25x the uncached input rate ($6.25/Mtok); the gpt-5.5 and
# gpt-5.3-codex pages list no write tariff, so those bill $0. OpenAI has no
# 5m/1h TTL split — the documented tariff goes in both positions. Cached
# reads use the published cached-input rate. Must mirror the analytics
# openai seeds (test_seed_pricing_matches_rate_table, #669 — that guard
# pins input/output/cache_read; the seed table has no write column).
_GPT_56_SOL = {
    "input": 5.00,
    "output": 30.00,
    "cache_read": 0.50,
    "cache_write_5m": 6.25,  # 1.25x input per the sol model page
    "cache_write_1h": 6.25,
}
_GPT_55 = {
    "input": 5.00,
    "output": 30.00,
    "cache_read": 0.50,
    "cache_write_5m": 0.0,
    "cache_write_1h": 0.0,
}
_GPT_53_CODEX = {
    "input": 1.75,
    "output": 14.00,
    "cache_read": 0.175,
    "cache_write_5m": 0.0,
    "cache_write_1h": 0.0,
}
# OpenAI publishes no separate cache-write tariff for the GPT-5.4 family.
# Writes therefore carry no premium; cached reads use the published
# cached-input rate.
_GPT_54 = {
    "input": 1.75,
    "output": 14.00,
    "cache_read": 0.175,
    "cache_write_5m": 0.0,
    "cache_write_1h": 0.0,
}
_GPT_54_MINI = {
    "input": 0.25,
    "output": 2.00,
    "cache_read": 0.025,
    "cache_write_5m": 0.0,
    "cache_write_1h": 0.0,
}
_GPT_54_NANO = {
    "input": 0.05,
    "output": 0.40,
    "cache_read": 0.005,
    "cache_write_5m": 0.0,
    "cache_write_1h": 0.0,
}

# Bare-model-id → rate dict. Add new model ids here on each release.
RATE_TABLE: dict[str, dict[str, float]] = {
    "claude-fable-5": _FABLE,
    "claude-mythos-5": _FABLE,
    "claude-fable-5-1": _FABLE_51,
    "claude-mythos-5-1": _FABLE_51,
    "claude-opus-5": _OPUS_STD,
    "claude-opus-4-8": _OPUS_STD,
    "claude-opus-4-7": _OPUS_STD,
    "claude-opus-4-6": _OPUS_STD,
    "claude-opus-4-5": _OPUS_STD,
    "claude-opus-4-1": _OPUS_LEGACY,
    "claude-opus-4": _OPUS_LEGACY,
    # Sonnet 5 (2026-06): the current Sonnet tier. Standard pricing is
    # $3/$15 per Mtok — identical to the flat ``_SONNET`` tier. NOTE:
    # introductory pricing of $2/$10 applies through 2026-08-31, then
    # reverts to standard. We deliberately use the durable standard rate
    # here: this table only powers the tmux/subscription cost ESTIMATE
    # (those agents aren't billed per-token), and a hard-coded intro rate
    # would silently over-discount every turn after the revert date.
    "claude-sonnet-5": _SONNET,
    "claude-sonnet-4-6": _SONNET,
    "claude-sonnet-4-5": _SONNET,
    "claude-sonnet-4": _SONNET,
    "claude-haiku-4-5": _HAIKU_45,
    "claude-haiku-3-5": _HAIKU_35,
    # Codex fleet models (#860). gpt-5.6-sol is the current fleet model
    # (2026-07-09→); gpt-5.5 the previous one; gpt-5.3-codex kept because the
    # analytics seed carries it (parity-pinned).
    "gpt-5.6-sol": _GPT_56_SOL,
    # gpt-daybreak-blue-latest: Daybreak Access alias — officially "currently
    # point[s] to gpt-5.6-sol", with pricing "adjusted to match each
    # underlying model" (developers.openai.com/api/docs/pricing, verified
    # 2026-09-01). Sol's dict verbatim; update together with the catalog and
    # analytics seeds when OpenAI repoints the alias.
    "gpt-daybreak-blue-latest": _GPT_56_SOL,
    "gpt-5.5": _GPT_55,
    "gpt-5.4": _GPT_54,
    "gpt-5.4-mini": _GPT_54_MINI,
    "gpt-5.4-nano": _GPT_54_NANO,
    "gpt-5.3-codex": _GPT_53_CODEX,
}


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
        return {
            "input": float(row["input_price"]),
            "output": float(row["output_price"]),
            "cache_read": float(row["cached_input_price"]),
            "cache_write_5m": float(row["cache_write_5m_price"]),
            "cache_write_1h": float(row["cache_write_1h_price"]),
        }
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
