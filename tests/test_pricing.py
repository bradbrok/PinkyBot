"""Unit tests for ``pinky_daemon.pricing`` (issue #648).

The expected dollar figures are hand-computed from the in-tree rate
table, which mirrors ``scripts/burn_cost_report.compute_row_cost`` (split
5m/1h cache-write billing, 1h fallback when the split is absent). All
rates are USD per million tokens.

Standard Opus tier (4.5+): input 5.00, output 25.00, cache_read 0.50,
cache_write_5m 6.25, cache_write_1h 10.00.
"""

from __future__ import annotations

import pytest

from pinky_daemon.pricing import (
    compute_cost_from_usage,
    compute_turn_cost_usd,
    lookup_rate,
)


def test_pure_input_output_opus() -> None:
    # 1M input @ $5 + 1M output @ $25 = $30.00
    cost = compute_turn_cost_usd(
        "claude-opus-4-8",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
    )
    assert cost == pytest.approx(30.0)


def test_split_cache_write_rates_opus() -> None:
    # cache_read 1M @ $0.50 + cw5m 1M @ $6.25 + cw1h 1M @ $10 = $16.75
    cost = compute_turn_cost_usd(
        "claude-opus-4-8",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_creation_5m_tokens=1_000_000,
        cache_creation_1h_tokens=1_000_000,
    )
    assert cost == pytest.approx(16.75)


def test_sonnet_rates() -> None:
    # 2M input @ $3 + 1M output @ $15 = $21.00
    cost = compute_turn_cost_usd(
        "claude-sonnet-4-6",
        input_tokens=2_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
    )
    assert cost == pytest.approx(21.0)


def test_haiku_rates() -> None:
    cost = compute_turn_cost_usd(
        "claude-haiku-4-5",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
    )
    assert cost == pytest.approx(1.0)


def test_legacy_opus_is_three_x() -> None:
    # Pre-4.5 Opus billed at the 3x tier: 1M input @ $15.
    cost = compute_turn_cost_usd(
        "claude-opus-4-1",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
    )
    assert cost == pytest.approx(15.0)


def test_unknown_model_costs_zero() -> None:
    assert lookup_rate("gpt-5-turbo") is None
    cost = compute_turn_cost_usd(
        "gpt-5-turbo",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
    )
    assert cost == 0.0


def test_tier_suffix_is_stripped() -> None:
    # A tiered id ("...[1m]") must resolve to the same rate as the bare id.
    assert lookup_rate("claude-opus-4-8[1m]") is lookup_rate("claude-opus-4-8")
    cost = compute_cost_from_usage(
        "claude-opus-4-8[1m]",
        {"input_tokens": 1_000_000, "output_tokens": 0},
    )
    assert cost == pytest.approx(5.0)


def test_fable_and_mythos_5_rates() -> None:
    # Claude Fable 5 / Mythos 5 (2026-06-09): $10 in / $50 out per Mtok.
    for model in ("claude-fable-5", "claude-mythos-5"):
        cost = compute_turn_cost_usd(
            model,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=0,
            cache_creation_5m_tokens=0,
            cache_creation_1h_tokens=0,
        )
        assert cost == pytest.approx(60.0), model
    rate = lookup_rate("claude-fable-5")
    assert rate["input"] == 10.0
    assert rate["output"] == 50.0
    assert rate["cache_read"] == 1.0  # 0.1x input
    assert rate["cache_write_5m"] == 12.5  # 1.25x input
    assert rate["cache_write_1h"] == 20.0  # 2x input


def test_cost_from_usage_with_transcript_split() -> None:
    """Transcript-shape usage with the nested 5m/1h breakdown."""
    usage = {
        "input_tokens": 10_000,
        "output_tokens": 500,
        "cache_read_input_tokens": 2_000,
        "cache_creation_input_tokens": 3_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 1_000,
            "ephemeral_1h_input_tokens": 2_000,
        },
    }
    # 10000*5 + 500*25 + 2000*0.5 + 1000*6.25 + 2000*10, all /1e6
    expected = (
        10_000 / 1e6 * 5
        + 500 / 1e6 * 25
        + 2_000 / 1e6 * 0.5
        + 1_000 / 1e6 * 6.25
        + 2_000 / 1e6 * 10
    )
    assert compute_cost_from_usage("claude-opus-4-8", usage) == pytest.approx(expected)


def test_cost_from_usage_falls_back_to_1h_without_split() -> None:
    """No nested split ⇒ bill the whole cache_creation aggregate at 1h."""
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
    }
    # All 1M cache-creation tokens at the 1h rate ($10).
    assert compute_cost_from_usage("claude-opus-4-8", usage) == pytest.approx(10.0)


def test_cost_from_usage_accepts_sdk_short_keys() -> None:
    """SDK shortened key shape (cache_write_tokens / cache_read_tokens)."""
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_tokens": 1_000_000,
        "cache_write_tokens": 1_000_000,
    }
    # input 1M@5 + cache_read 1M@0.5 + cache_write(all 1h) 1M@10 = 15.5
    assert compute_cost_from_usage("claude-opus-4-8", usage) == pytest.approx(15.5)


def test_cost_from_usage_tolerates_malformed() -> None:
    """Garbage values must yield 0.0, never raise."""
    assert compute_cost_from_usage("claude-opus-4-8", {"input_tokens": "abc"}) == 0.0
    assert compute_cost_from_usage("claude-opus-4-8", None) == 0.0
    assert compute_cost_from_usage("claude-opus-4-8", {"input_tokens": None}) == 0.0


def test_zero_usage_is_zero_cost() -> None:
    assert compute_cost_from_usage("claude-opus-4-8", {}) == 0.0


# ── OpenAI / Codex family (#860) ───────────────────────────────────────────
# Powers the live tmux cost path for codex agents (CodexTmuxSession rides the
# same _log_turn_cost_and_analytics as Claude tmux). Official API rates:
# gpt-5.6-sol / gpt-5.5 at $5/$30 (cached input $0.50), gpt-5.3-codex at
# $1.75/$14 (cached $0.175). OpenAI bills no cache WRITE.


def test_gpt_frontier_rates() -> None:
    # 1M input @ $5 + 1M output @ $30 + 1M cached-read @ $0.50 = $35.50
    for model in ("gpt-5.6-sol", "gpt-5.5"):
        cost = compute_turn_cost_usd(
            model,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_creation_5m_tokens=0,
            cache_creation_1h_tokens=0,
        )
        assert cost == pytest.approx(35.5), model
    assert lookup_rate("gpt-5.6-sol") is lookup_rate("gpt-5.5")


def test_gpt_53_codex_rates() -> None:
    # 1M input @ $1.75 + 1M output @ $14 + 1M cached-read @ $0.175 = $15.925
    cost = compute_turn_cost_usd(
        "gpt-5.3-codex",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
    )
    assert cost == pytest.approx(15.925)


def test_gpt_cache_write_bills_zero() -> None:
    """OpenAI has no cache-write charge — cache_creation tokens on a gpt
    model must contribute $0 at both the 5m and 1h positions."""
    cost = compute_turn_cost_usd(
        "gpt-5.6-sol",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_5m_tokens=1_000_000,
        cache_creation_1h_tokens=1_000_000,
    )
    assert cost == 0.0
    # And through the usage-dict entry point (1h fallback path).
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 1_000_000}
    assert compute_cost_from_usage("gpt-5.6-sol", usage) == 0.0
