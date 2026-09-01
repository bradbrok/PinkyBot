"""Central policy for tool grants contributed by skills.

Catalog writes validate the small allowlist-pattern grammar strictly.  Runtime
materialization is deliberately lenient: legacy or manually-corrupted rows are
dropped with a warning instead of preventing an agent from starting.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class ToolPatternValidationError(ValueError):
    """A tool allowlist pattern is malformed or ambiguous."""


@dataclass(frozen=True)
class ToolPatternClassification:
    """The parsed tool name and whether the pattern grants privileged access."""

    tool_name: str
    privileged: bool


_TOOL_PATTERN_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.\-*]*)(?:\(([^()\[\]'\"]+)\))?$")
_BASELINE_TOOLS = frozenset({"Read", "Glob", "Grep", "Agent"})
_BASELINE_MCP_PREFIXES = (
    "mcp__pinky-memory__",
    "mcp__pinky-self__",
    "mcp__pinky-messaging__",
)
_ABSENT_PROVENANCE = frozenset({"", "unknown"})


def split_tool_pattern_scalar(value: str) -> list[str]:
    """Split a space-delimited ``allowed-tools`` scalar.

    Whitespace inside one balanced parenthesized specifier belongs to that
    pattern.  Validation remains the classifier's job so callers receive one
    consistent error for malformed parentheses, quotes, brackets, or commas.
    """

    patterns: list[str] = []
    start: int | None = None
    depth = 0

    for index, char in enumerate(value):
        if char.isspace() and depth == 0:
            if start is not None:
                patterns.append(value[start:index])
                start = None
            continue

        if start is None:
            start = index
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1

    if start is not None:
        patterns.append(value[start:])
    return patterns


def classify_tool_pattern(
    pattern: str,
    *,
    skill_name: str,
    mcp_server_config: Mapping[str, Any] | None,
    skill_type: str,
) -> ToolPatternClassification:
    """Validate and classify one skill-contributed tool allowlist pattern."""

    if not isinstance(pattern, str) or not pattern or pattern != pattern.strip():
        raise ToolPatternValidationError(f"invalid tool pattern: {pattern!r}")
    if any(ord(char) < 32 or ord(char) == 0x7F for char in pattern):
        raise ToolPatternValidationError(f"invalid tool pattern: {pattern!r}")

    match = _TOOL_PATTERN_RE.fullmatch(pattern)
    if match is None:
        raise ToolPatternValidationError(f"invalid tool pattern: {pattern!r}")

    tool_name = match.group(1)
    privileged = not _is_baseline_or_own_tool(
        tool_name,
        skill_name=skill_name,
        mcp_server_config=mcp_server_config or {},
        skill_type=skill_type,
    )
    return ToolPatternClassification(tool_name=tool_name, privileged=privileged)


def validate_tool_patterns(
    patterns: Iterable[str],
    *,
    skill_name: str,
    mcp_server_config: Mapping[str, Any] | None,
    skill_type: str,
) -> list[ToolPatternClassification]:
    """Strictly validate every pattern and return their classifications."""

    return [
        classify_tool_pattern(
            pattern,
            skill_name=skill_name,
            mcp_server_config=mcp_server_config,
            skill_type=skill_type,
        )
        for pattern in patterns
    ]


def has_privileged_tool_grant(
    patterns: Iterable[str],
    *,
    skill_name: str,
    mcp_server_config: Mapping[str, Any] | None,
    skill_type: str,
) -> bool:
    """Return whether any pattern is privileged, treating malformed input as privileged."""

    for pattern in patterns:
        try:
            classification = classify_tool_pattern(
                pattern,
                skill_name=skill_name,
                mcp_server_config=mcp_server_config,
                skill_type=skill_type,
            )
        except ToolPatternValidationError:
            return True
        if classification.privileged:
            return True
    return False


def filter_skill_tool_grants(
    grants: Iterable[Mapping[str, Any]],
    *,
    agent_name: str,
    warn: Callable[[str], None] | None = None,
) -> list[str]:
    """Apply provenance and opt-in policy to stored skill tool grants.

    Unknown legacy rows fail closed only when they would elevate access.
    Privileged grants require an operator assignment or explicit catalog opt-in.
    """

    accepted: list[str] = []
    seen: set[str] = set()

    for grant in grants:
        pattern = grant.get("pattern")
        skill_name = str(grant.get("skill_name") or "")
        try:
            classification = classify_tool_pattern(
                pattern,
                skill_name=skill_name,
                mcp_server_config=grant.get("mcp_server_config") or {},
                skill_type=str(grant.get("skill_type") or "custom"),
            )
        except ToolPatternValidationError as exc:
            _warn_drop(warn, agent_name, skill_name, pattern, str(exc))
            continue

        assigned_by_raw = grant.get("assigned_by")
        assigned_by = assigned_by_raw.strip() if isinstance(assigned_by_raw, str) else ""
        if classification.privileged and assigned_by in _ABSENT_PROVENANCE:
            _warn_drop(warn, agent_name, skill_name, pattern, "missing assignment provenance")
            continue
        if (
            classification.privileged
            and assigned_by != "user"
            and not bool(grant.get("privileged_tool_opt_in"))
        ):
            _warn_drop(warn, agent_name, skill_name, pattern, "grant lacks operator opt-in")
            continue

        if pattern not in seen:
            seen.add(pattern)
            accepted.append(pattern)

    return accepted


def _is_baseline_or_own_tool(
    tool_name: str,
    *,
    skill_name: str,
    mcp_server_config: Mapping[str, Any],
    skill_type: str,
) -> bool:
    if tool_name in _BASELINE_TOOLS or tool_name.startswith(_BASELINE_MCP_PREFIXES):
        return True

    if skill_type == "plugin" and (
        tool_name.startswith(f"mcp__plugin-{skill_name}__")
        or tool_name.startswith(f"plugin_{skill_name}_")
    ):
        return True

    return bool(mcp_server_config) and tool_name.startswith(f"mcp__{skill_name}__")


def _warn_drop(
    warn: Callable[[str], None] | None,
    agent_name: str,
    skill_name: str,
    pattern: object,
    reason: str,
) -> None:
    if warn is not None:
        warn(
            "skill_tool_policy: dropped tool grant "
            f"agent={agent_name!r} skill={skill_name!r} pattern={pattern!r}: {reason}"
        )
