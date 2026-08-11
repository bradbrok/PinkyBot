"""Tests for the unified context-window resolver."""

import json

import pytest

from pinky_daemon.context_window import (
    DEFAULT_CONTEXT_WINDOW,
    configured_context_window,
    resolve_context_window,
)

ENV_KEY = "PINKY_MODEL_CONTEXT_SIZES"


# --- Tier 1: trust the harness-reported window ------------------------------


def test_tier1_reported_window_is_trusted_not_inflated():
    """The core regression guard: a large-context model reporting 200k must
    NOT be inflated to 1M. When a 1M window is not active the harness reports
    the true 200k, and inflating it makes the percentage read ~5x low."""
    assert resolve_context_window("claude-opus-4-8", reported_max=200_000) == 200_000


def test_tier1_genuine_large_report_is_trusted():
    assert (
        resolve_context_window("claude-opus-4-8", reported_max=1_000_000)
        == 1_000_000
    )


def test_tier1_reported_wins_over_configured():
    # Even for a model with a configured window, a live harness report wins.
    assert resolve_context_window("gpt-5.6-luna", reported_max=128_000) == 128_000


# --- Tier 2: configurable per-model map -------------------------------------


def test_tier2_builtin_substring_match():
    # No harness report -> built-in map, matched as a substring.
    assert resolve_context_window("gpt-5.6-luna-max-fast") == 272_000


def test_tier2_env_override_registers_new_model(monkeypatch):
    monkeypatch.setenv(ENV_KEY, '{"gpt-5.6-terra": 300000}')
    assert resolve_context_window("gpt-5.6-terra") == 300_000


def test_tier2_env_override_wins_over_builtin(monkeypatch):
    monkeypatch.setenv(ENV_KEY, '{"gpt-5.6-luna": 500000}')
    assert resolve_context_window("gpt-5.6-luna") == 500_000


def test_tier2_malformed_env_falls_back_to_builtin(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "not-json{")
    # Must not raise, and must still resolve built-ins.
    assert resolve_context_window("gpt-5.6-luna") == 272_000


def test_tier2_env_non_object_ignored(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "[1, 2, 3]")
    assert resolve_context_window("gpt-5.6-luna") == 272_000


def test_tier2_env_bad_size_skipped(monkeypatch):
    # Non-positive / non-numeric sizes are dropped, not fatal.
    monkeypatch.setenv(ENV_KEY, '{"gpt-5.6-terra": "huge", "gpt-5.6-sol": -1}')
    assert resolve_context_window("gpt-5.6-terra") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window("gpt-5.6-sol") == DEFAULT_CONTEXT_WINDOW


def test_env_specific_key_wins_over_builtin_family(monkeypatch):
    # Regression: an env key that fully names a model must win even though the
    # built-in map has a broader family substring ("opus") that also matches.
    monkeypatch.setenv(ENV_KEY, '{"claude-opus-4-8": 1000000}')
    assert resolve_context_window("claude-opus-4-8") == 1_000_000


def test_env_longest_key_wins_over_broader_env_key(monkeypatch):
    # Within the env override, the most specific (longest) matching key wins,
    # independent of JSON/dict ordering.
    monkeypatch.setenv(ENV_KEY, '{"gpt-5.6": 100000, "gpt-5.6-luna-max": 500000}')
    assert resolve_context_window("gpt-5.6-luna-max") == 500_000
    # A model that only matches the broader key still resolves to it.
    assert resolve_context_window("gpt-5.6-plain") == 100_000


def test_env_overflow_value_is_skipped_not_fatal(monkeypatch):
    # Regression: a value that overflows int() (1e309 -> inf) must not crash the
    # resolver; other good entries in the same object are preserved.
    monkeypatch.setenv(ENV_KEY, '{"badinf": 1e309, "gpt-5.6-terra": 300000}')
    assert resolve_context_window("gpt-5.6-terra") == 300_000
    assert resolve_context_window("badinf-model") == DEFAULT_CONTEXT_WINDOW


def test_env_bool_and_float_rejected(monkeypatch):
    # bool (JSON true -> 1) and float (123.75) are not valid window sizes.
    monkeypatch.setenv(
        ENV_KEY, '{"m-bool": true, "m-float": 123.75, "m-ok": 300000}'
    )
    assert resolve_context_window("m-bool") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window("m-float") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window("m-ok") == 300_000


def test_env_integer_string_accepted(monkeypatch):
    # A digits-only string is a friendly, valid way to write a size.
    monkeypatch.setenv(ENV_KEY, '{"gpt-5.6-terra": "300000"}')
    assert resolve_context_window("gpt-5.6-terra") == 300_000


def test_env_out_of_range_value_skipped(monkeypatch):
    # Absurdly large values are rejected (garbage guard).
    monkeypatch.setenv(ENV_KEY, '{"gpt-5.6-terra": 999999999999}')
    assert resolve_context_window("gpt-5.6-terra") == DEFAULT_CONTEXT_WINDOW


def test_env_pathological_digit_strings_skipped(monkeypatch):
    # str.isdigit() accepts inputs int() rejects: an over-long digit string
    # (Python 3.11+ integer-string conversion cap), a unicode digit, and a
    # multi-sign string. Each must be skipped non-fatally, preserving good
    # siblings in the same object.
    payload = json.dumps(
        {
            "bad-long": "9" * 5000,  # exceeds int() digit cap
            "bad-plus": "++123",  # multi-sign
            "bad-unicode": "²",  # superscript 2 — isdigit() True, int() raises
            "gpt-5.6-terra": 300000,  # good sibling must survive
        }
    )
    monkeypatch.setenv(ENV_KEY, payload)
    assert resolve_context_window("gpt-5.6-terra") == 300_000
    assert resolve_context_window("bad-long") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window("bad-plus") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window("bad-unicode") == DEFAULT_CONTEXT_WINDOW


# --- Tier 3: conservative default -------------------------------------------


def test_tier3_unknown_model_default():
    assert resolve_context_window("some-unlisted-model") == DEFAULT_CONTEXT_WINDOW


def test_tier3_empty_model_and_no_report_default():
    assert resolve_context_window("") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window("", reported_max=0) == DEFAULT_CONTEXT_WINDOW


def test_zero_or_negative_report_falls_through():
    # A zero/negative report is treated as "no report", not a real window.
    assert resolve_context_window("gpt-5.6-luna", reported_max=0) == 272_000
    assert resolve_context_window("unknown", reported_max=-5) == DEFAULT_CONTEXT_WINDOW


# --- configured_context_window direct ---------------------------------------


def test_configured_returns_zero_when_unmatched():
    # "default" is never matched as a substring; unmatched -> 0 (caller decides).
    assert configured_context_window("totally-unknown") == 0


def test_configured_default_key_not_substring_matched():
    # A model literally containing "default" still shouldn't match the
    # "default" seed key as a window (it is the Tier-3 fallback, not a match).
    assert configured_context_window("default") == 0


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-5.6-luna", 272_000),
        ("GPT-5.6-LUNA-MAX", 272_000),  # case-insensitive
        # A Claude model on the no-report path matches the built-in "opus"
        # seed -> a conservative 200k (the old 1M override is gone).
        ("claude-opus-4-8", 200_000),
        ("unlisted-xyz", 0),  # nothing matches -> caller falls back
    ],
)
def test_configured_cases(model, expected):
    assert configured_context_window(model) == expected
