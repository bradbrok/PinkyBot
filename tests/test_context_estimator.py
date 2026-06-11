"""Tests for shared context estimation helpers."""

from __future__ import annotations

from types import SimpleNamespace

from pinky_daemon.context_estimator import ContextTextEstimator


class _Store:
    def __init__(self, *contents: str) -> None:
        self.contents = contents
        self.calls: list[tuple[str, int]] = []

    def get_history(self, session_id: str, limit: int = 1000):
        self.calls.append((session_id, limit))
        return [SimpleNamespace(content=content) for content in self.contents]


class _BrokenStore:
    def get_history(self, session_id: str, limit: int = 1000):
        raise RuntimeError("db unavailable")


def test_estimates_internal_texts():
    estimator = ContextTextEstimator(chars_per_token=4)

    estimator.record_internal_text("")
    estimator.record_internal_text("abcd")
    estimator.record_internal_text("abcdefghi")

    assert estimator.estimated_tokens(session_id="s") == 3


def test_estimates_store_history_with_internal_texts():
    estimator = ContextTextEstimator(chars_per_token=4)
    estimator.record_internal_text("abcdefgh")
    store = _Store("abcd", "", "abcdefghijkl")

    total = estimator.estimated_tokens(
        session_id="agent-main",
        conversation_store=store,
        history_limit=50,
    )

    assert total == 6
    assert store.calls == [("agent-main", 50)]


def test_returns_internal_estimate_when_store_read_fails():
    estimator = ContextTextEstimator(chars_per_token=4)
    estimator.record_internal_text("abcdefgh")

    assert estimator.estimated_tokens(
        session_id="agent-main",
        conversation_store=_BrokenStore(),
    ) == 2


def test_context_info_shape_and_percentage():
    estimator = ContextTextEstimator(chars_per_token=4)
    estimator.record_internal_text("abcdefgh")

    info = estimator.context_info(session_id="agent-main", max_tokens=10)

    assert info == {
        "total_tokens": 2,
        "max_tokens": 10,
        "percentage": 20.0,
        "categories": [],
        "mcp_tools": [],
    }


def test_context_info_percentage_clamped_at_100():
    """#745: the estimate sums all persisted history and never learns about
    a runtime's internal compaction (Codex CLI), so it drifts past the
    window size — murzik's gauge read 108%. A live context can't exceed
    its window; clamp the reported percentage."""
    estimator = ContextTextEstimator(chars_per_token=4)
    estimator.record_internal_text("x" * 80)  # 20 tokens vs max 10

    info = estimator.context_info(session_id="agent-main", max_tokens=10)

    assert info["total_tokens"] == 20  # raw estimate stays honest
    assert info["percentage"] == 100.0  # display gauge is clamped
