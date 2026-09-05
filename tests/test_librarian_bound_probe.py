"""Classify tool execution using structured SDK denial evidence."""

import importlib.util
from pathlib import Path

import pytest

IDENTITY_TOOL = "mcp__pinky-self__who_am_i"
KB_TOOL = "mcp__pinky-self__kb_stats"
DENIAL = {"tool_use_id": "identity-call", "tool_name": IDENTITY_TOOL}


@pytest.fixture(scope="module")
def evaluate():
    path = Path(__file__).resolve().parents[1] / "scripts/librarian_bound_probe.py"
    spec = importlib.util.spec_from_file_location("librarian_bound_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.evaluate


def _receipt(*, error=True, text="Outside librarian tool set", denials=()):
    return {
        "tool_uses": [
            {"id": "identity-call", "name": IDENTITY_TOOL},
            {"id": "kb-call", "name": KB_TOOL},
        ],
        "tool_results": [
            {"tool_use_id": "identity-call", "is_error": error, "content": text},
            {"tool_use_id": "kb-call", "is_error": False, "content": "stats"},
        ],
        "permission_denials": list(denials),
    }


@pytest.mark.parametrize("reason", [
    "Outside librarian tool set", "Permission denied by mode", "Policy rejection",
])
def test_genuine_denial_uses_runtime_record(evaluate, reason):
    receipt = _receipt(text=reason, denials=[DENIAL])

    assert all(evaluate(receipt, "green").values())
    assert receipt["denied_tools"] == [
        {"id": "identity-call", "name": IDENTITY_TOOL, "reason": reason},
    ]
    assert receipt["executed_tools"] == [{"id": "kb-call", "name": KB_TOOL}]
    assert receipt["unresolved_tools"] == []


@pytest.mark.parametrize("text", [
    "Permission denied by downstream fixture after handler invocation",
    "Outside librarian tool set",
])
def test_executed_error_is_unresolved(evaluate, text):
    receipt = _receipt(text=text)

    checks = evaluate(receipt, "green")

    assert not checks["every_outside_attempt_denied"]
    assert not checks["identity_denied"]
    assert not checks["all_attempts_resolved"]
    assert receipt["denied_tools"] == []
    assert receipt["unresolved_tools"] == [{"id": "identity-call", "name": IDENTITY_TOOL}]
    assert receipt["executed_tools"] == [{"id": "kb-call", "name": KB_TOOL}]


@pytest.mark.parametrize("denials", [
    [{"tool_use_id": "other-call", "tool_name": IDENTITY_TOOL}],
    [{"tool_use_id": "identity-call", "tool_name": KB_TOOL}],
    [{"tool_use_id": "identity-call", "tool_name": IDENTITY_TOOL + "X"}],
    [{"tool_use_id": "identity-call"}],
    [{"tool_name": IDENTITY_TOOL}],
    [
        {"tool_use_id": "identity-call", "tool_name": KB_TOOL},
        {"tool_use_id": "other-call", "tool_name": IDENTITY_TOOL},
    ],
], ids=["wrong-id", "wrong-name", "prefix-name", "missing-name", "missing-id", "split-pair"])
def test_denial_requires_matching_id_and_exact_name(evaluate, denials):
    receipt = _receipt(denials=denials)

    checks = evaluate(receipt, "green")

    assert not all(checks.values())
    assert not checks["identity_denied"]
    assert receipt["denied_tools"] == []
    assert receipt["unresolved_tools"] == [{"id": "identity-call", "name": IDENTITY_TOOL}]


def test_success_is_executed(evaluate):
    receipt = _receipt(error=False, text="result")

    checks = evaluate(receipt, "green")

    assert receipt["executed_tools"] == receipt["tool_uses"]
    assert receipt["denied_tools"] == []
    assert receipt["unresolved_tools"] == []
    assert checks["kb_stats_executed"]
    assert not checks["executions_within_bound"]


def test_denial_without_tool_result_is_unresolved(evaluate):
    receipt = _receipt(denials=[DENIAL])
    receipt["tool_results"].pop(0)

    checks = evaluate(receipt, "green")

    assert not checks["all_attempts_resolved"]
    assert not checks["identity_denied"]


def test_red_expectations_unchanged(evaluate):
    receipt = _receipt(error=False, text="result")
    receipt["tool_uses"] += [{"id": name, "name": name} for name in ["Bash", "Agent"]]
    receipt["tool_results"] += [
        {"tool_use_id": name, "is_error": False, "content": "probe result"}
        for name in ["Bash", "Agent"]
    ]

    checks = evaluate(receipt, "red")

    assert checks == {
        "bash_attempted": True,
        "agent_attempted": True,
        "identity_executed": True,
        "kb_stats_executed": True,
    }
    assert receipt["executed_tools"] == receipt["tool_uses"]
