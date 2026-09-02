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
# same _log_turn_cost_and_analytics as Claude tmux). Official API rates
# (developers.openai.com, verified 2026-07-10): gpt-5.6-sol / gpt-5.5 at
# $5/$30 (cached input $0.50), gpt-5.3-codex at $1.75/$14 (cached $0.175).
# Cache-write tariffs differ per model page: sol bills 1.25x input ($6.25);
# the gpt-5.5 and gpt-5.3-codex pages list no write tariff (murzik #861 P2).


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


def test_gpt_56_sol_cache_write_billed_at_1_25x_input() -> None:
    """The sol model page documents cache writes at 1.25x the uncached input
    rate ($6.25/Mtok). OpenAI has no 5m/1h TTL split, so both positions carry
    the same tariff (murzik #861 P2 — was wrongly $0)."""
    rate = lookup_rate("gpt-5.6-sol")
    assert rate["cache_write_5m"] == 6.25
    assert rate["cache_write_1h"] == 6.25
    cost = compute_turn_cost_usd(
        "gpt-5.6-sol",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_5m_tokens=1_000_000,
        cache_creation_1h_tokens=1_000_000,
    )
    assert cost == pytest.approx(12.5)
    # Usage-dict entry point (aggregate falls back to the 1h position).
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 1_000_000}
    assert compute_cost_from_usage("gpt-5.6-sol", usage) == pytest.approx(6.25)


def test_gpt_55_and_codex_cache_write_bills_zero() -> None:
    """The gpt-5.5 and gpt-5.3-codex model pages list no cache-write tariff —
    cache_creation tokens contribute $0 at both positions."""
    for model in ("gpt-5.5", "gpt-5.3-codex"):
        cost = compute_turn_cost_usd(
            model,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_5m_tokens=1_000_000,
            cache_creation_1h_tokens=1_000_000,
        )
        assert cost == 0.0, model
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_creation_input_tokens": 1_000_000}
        assert compute_cost_from_usage(model, usage) == 0.0, model


@pytest.fixture
def bound_runtime_catalog():
    from pinky_daemon import runtime_model_catalog

    runtime_model_catalog.reset_for_tests()
    try:
        yield runtime_model_catalog
    finally:
        runtime_model_catalog.reset_for_tests()


def _add_runtime_model(registry, model_id: str, *, input_price: float) -> None:
    registry.add_model(
        provider="anthropic",
        model_id=model_id,
        input_price=input_price,
        output_price=2.0,
        cached_input_price=0.25,
        cache_write_5m_price=1.25,
        cache_write_1h_price=2.0,
    )


def test_runtime_only_model_prices_all_five_token_buckets(
    tmp_path,
    bound_runtime_catalog,
) -> None:
    from pinky_daemon.agent_registry import AgentRegistry

    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        registry.add_model(
            provider="custom",
            model_id="runtime-five-rate-model",
            input_price=1.0,
            output_price=2.0,
            cached_input_price=0.25,
            cache_write_5m_price=1.25,
            cache_write_1h_price=2.0,
        )
        bound_runtime_catalog.bind_registry(registry)
        cost = compute_turn_cost_usd(
            "runtime-five-rate-model[1m]",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_creation_5m_tokens=1_000_000,
            cache_creation_1h_tokens=1_000_000,
        )
        assert cost == pytest.approx(6.5)
    finally:
        registry.close()


def test_registry_rate_overrides_static_and_crud_invalidates_cache(
    tmp_path,
    bound_runtime_catalog,
) -> None:
    from pinky_daemon.agent_registry import AgentRegistry

    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        bound_runtime_catalog.bind_registry(registry)
        _add_runtime_model(registry, "claude-opus-4-8", input_price=7.0)
        first = lookup_rate("claude-opus-4-8[1m]")
        assert first is not None and first["input"] == 7.0

        _add_runtime_model(registry, "claude-opus-4-8", input_price=9.0)
        second = lookup_rate("claude-opus-4-8")
        assert second is not None and second["input"] == 9.0
    finally:
        registry.close()


def test_delete_invalidates_cached_runtime_rate(
    tmp_path,
    bound_runtime_catalog,
) -> None:
    from pinky_daemon.agent_registry import AgentRegistry
    from pinky_daemon.runtime_model_catalog import ModelCatalogError

    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        _add_runtime_model(registry, "runtime-deleted-model", input_price=3.0)
        bound_runtime_catalog.bind_registry(registry)
        assert lookup_rate("runtime-deleted-model")["input"] == 3.0
        assert registry.delete_model("runtime-deleted-model") is True
        with pytest.raises(ModelCatalogError, match="is inactive"):
            lookup_rate("runtime-deleted-model")
    finally:
        registry.close()


def test_known_incomplete_registry_model_raises_instead_of_pricing_zero(
    tmp_path,
    bound_runtime_catalog,
) -> None:
    from pinky_daemon.agent_registry import AgentRegistry

    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        _add_runtime_model(registry, "runtime-partial-model", input_price=1.0)
        bound_runtime_catalog.bind_registry(registry)
        registry._db.execute(
            "UPDATE models SET cache_write_1h_price=NULL "
            "WHERE id='anthropic/runtime-partial-model'"
        )
        registry._db.commit()
        bound_runtime_catalog.invalidate()

        with pytest.raises(
            bound_runtime_catalog.ModelCatalogError,
            match="runtime-partial-model.*cache_write_1h_price",
        ):
            compute_turn_cost_usd(
                "runtime-partial-model",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_5m_tokens=0,
                cache_creation_1h_tokens=0,
            )
    finally:
        registry.close()


def test_truly_unknown_model_stays_zero_with_registry_bound(
    tmp_path,
    bound_runtime_catalog,
) -> None:
    from pinky_daemon.agent_registry import AgentRegistry

    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        bound_runtime_catalog.bind_registry(registry)
        assert lookup_rate("runtime-unknown-model") is None
        assert compute_turn_cost_usd(
            "runtime-unknown-model",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=0,
            cache_creation_5m_tokens=0,
            cache_creation_1h_tokens=0,
        ) == 0.0
    finally:
        registry.close()


def test_unbound_or_unavailable_registry_uses_static_fallback(
    bound_runtime_catalog,
    capsys,
) -> None:
    static = lookup_rate("claude-opus-4-8")
    assert static is not None and static["input"] == 5.0

    class UnavailableRegistry:
        def get_model(self, _model_id):
            raise RuntimeError("registry unavailable")

    bound_runtime_catalog.bind_registry(UnavailableRegistry())
    fallback = lookup_rate("claude-opus-4-8")
    assert fallback is not None and fallback["input"] == 5.0
    stderr = capsys.readouterr().err
    assert "ERROR" in stderr
    assert "claude-opus-4-8" in stderr
    assert "registry unavailable" in stderr


def test_unavailable_registry_without_static_rate_raises_catalog_error(
    bound_runtime_catalog,
) -> None:
    class UnavailableRegistry:
        def get_model(self, _model_id):
            raise RuntimeError("registry unavailable")

    bound_runtime_catalog.bind_registry(UnavailableRegistry())
    with pytest.raises(
        bound_runtime_catalog.ModelCatalogError,
        match="runtime-only-unreachable-model.*registry unavailable",
    ):
        lookup_rate("runtime-only-unreachable-model")


def test_registry_read_failure_does_not_poison_rate_cache(
    bound_runtime_catalog,
) -> None:
    class RecoveringRegistry:
        calls = 0

        def get_model(self, model_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("registry unavailable")
            return {
                "id": f"custom/{model_id}",
                "provider": "custom",
                "model_id": model_id,
                "input_price": 7.0,
                "output_price": 8.0,
                "cached_input_price": 0.5,
                "cache_write_5m_price": 1.0,
                "cache_write_1h_price": 2.0,
                "pricing_status": "complete",
            }

    registry = RecoveringRegistry()
    bound_runtime_catalog.bind_registry(registry)
    with pytest.raises(bound_runtime_catalog.ModelCatalogError):
        lookup_rate("runtime-only-unreachable-model")
    recovered = lookup_rate("runtime-only-unreachable-model")
    assert recovered is not None and recovered["input"] == 7.0
    assert registry.calls == 2


def test_binding_second_registry_discards_first_registry_snapshot(
    tmp_path,
    bound_runtime_catalog,
) -> None:
    from pinky_daemon.agent_registry import AgentRegistry

    first = AgentRegistry(db_path=str(tmp_path / "first.db"))
    second = AgentRegistry(db_path=str(tmp_path / "second.db"))
    try:
        _add_runtime_model(first, "runtime-rebind-model", input_price=3.0)
        _add_runtime_model(second, "runtime-rebind-model", input_price=8.0)
        bound_runtime_catalog.bind_registry(first)
        assert lookup_rate("runtime-rebind-model")["input"] == 3.0
        bound_runtime_catalog.bind_registry(second)
        first.close()
        assert lookup_rate("runtime-rebind-model")["input"] == 8.0
    finally:
        second.close()


@pytest.mark.parametrize(
    ("model_id", "runtime_only"),
    (
        ("claude-opus-4-8", False),
        ("runtime-deleted-model", True),
    ),
)
def test_soft_deleted_model_is_known_inactive_after_cache_invalidation(
    tmp_path,
    bound_runtime_catalog,
    model_id,
    runtime_only,
) -> None:
    from pinky_daemon.agent_registry import AgentRegistry

    registry = AgentRegistry(db_path=str(tmp_path / "deleted-models.db"))
    try:
        if runtime_only:
            _add_runtime_model(registry, model_id, input_price=7.0)
        bound_runtime_catalog.bind_registry(registry)
        assert lookup_rate(model_id) is not None
        assert registry.delete_model(model_id) is True
        full_id = f"anthropic/{model_id}"
        with pytest.raises(
            bound_runtime_catalog.ModelCatalogError,
            match=rf"{full_id} is inactive",
        ):
            lookup_rate(model_id)
    finally:
        registry.close()


def test_bound_absent_model_keeps_static_or_unknown_fallback(
    tmp_path,
    bound_runtime_catalog,
) -> None:
    from pinky_daemon.agent_registry import AgentRegistry
    from pinky_daemon.pricing import RATE_TABLE

    registry = AgentRegistry(db_path=str(tmp_path / "absent-models.db"))
    try:
        bound_runtime_catalog.bind_registry(registry)
        assert registry.get_model("gpt-5.3-codex") is None
        assert lookup_rate("gpt-5.3-codex") == RATE_TABLE["gpt-5.3-codex"]
        assert lookup_rate("runtime-never-known-model") is None
        assert compute_turn_cost_usd(
            "runtime-never-known-model",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=0,
            cache_creation_5m_tokens=0,
            cache_creation_1h_tokens=0,
        ) == 0.0
    finally:
        registry.close()


@pytest.mark.parametrize(
    ("model_id", "expected"),
    (
        ("claude-opus-4-8[1m]", "claude-opus-4-8"),
        ("x[1M]  ", "x"),
        ("x", "x"),
        ("", ""),
        (None, ""),
        ("[1m]", ""),
        ("a]", "a]"),
        ("a[", "a["),
        ("a[]", "a[]"),
        ("a[b]c]", "a[b]c]"),
        ("a[b][1m]", "a[b]"),
        ("a[[b]", "a["),
        ("a[[]", "a[[]"),
    ),
)
def test_strip_tier_preserves_last_non_empty_bracket_group_semantics(
    model_id,
    expected,
) -> None:
    from pinky_daemon.runtime_model_catalog import strip_tier

    assert strip_tier(model_id) == expected


def test_strip_tier_handles_long_open_bracket_input_within_50ms() -> None:
    import signal
    import time

    from pinky_daemon.runtime_model_catalog import strip_tier

    model_id = "[" * 200_000

    def fail_if_slow(_signum, _frame):
        raise TimeoutError("strip_tier exceeded 50 ms")

    previous_handler = signal.signal(signal.SIGALRM, fail_if_slow)
    signal.setitimer(signal.ITIMER_REAL, 0.05)
    started = time.perf_counter()
    try:
        assert strip_tier(model_id) == model_id
        elapsed = time.perf_counter() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    assert elapsed < 0.05
