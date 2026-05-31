"""Thinking-effort vocabulary + ``ultracode`` resolution (#151).

Single source of truth for the effort levels shared across the transports
(SDK + tmux), the API validators, the ``set_thinking_effort`` MCP tool, and
the effort-drift hook.

``ultracode`` is a PinkyBot-native effort tier modeled on Claude Code's
``/effort ultracode`` — "xhigh effort plus standing dynamic-workflow
orchestration." Two facts about the CLI shape the implementation:

  1. The ``--effort`` startup flag (and SDK ``options.effort``) REJECT
     ``ultracode`` — they accept only low/medium/high/xhigh/max. ultracode
     is activatable only via the interactive ``/effort`` command, and the
     CLI never persists it.
  2. ultracode's reasoning level *is* xhigh.

So PinkyBot RESOLVES ultracode to ``xhigh`` for the actual effort knob
(``--effort`` / ``options.effort`` / the drift-hook expectation) and layers
the workflow-orchestration behavior in via a system-prompt directive
(see ``agent_registry.build_system_prompt``). This keeps ultracode working
on every CLI version — including ones predating native support — without
ever feeding the CLI a value its flag parser rejects.
"""

from __future__ import annotations

# Levels the CLI ``--effort`` flag / SDK ``options.effort`` accept natively.
CLI_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Full set the set_thinking_effort tool / session-override endpoint accept:
# the CLI levels plus "auto" (adaptive — clears the override) and the
# PinkyBot-native "ultracode" tier.
EFFORT_LEVELS: tuple[str, ...] = (*CLI_EFFORT_LEVELS, "auto", "ultracode")

# The reasoning level ultracode resolves to for the real effort knob.
ULTRACODE_BASE_EFFORT = "xhigh"


def is_ultracode(level: str | None) -> bool:
    """True when ``level`` is the ultracode tier (case/space tolerant)."""
    return (level or "").strip().lower() == "ultracode"


def resolve_cli_effort(level: str | None) -> str | None:
    """Map a Pinky effort level to a value the CLI/SDK effort knob accepts.

    ``ultracode`` → ``xhigh`` (the CLI flag rejects "ultracode"). Every other
    value — including ``None``, ``""``, and ``"auto"`` — passes through
    unchanged; callers already treat those as "leave effort adaptive / don't
    set the flag."
    """
    if is_ultracode(level):
        return ULTRACODE_BASE_EFFORT
    return level
