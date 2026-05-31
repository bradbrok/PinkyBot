"""ultracode effort tier (#151).

ultracode = xhigh reasoning + standing workflow orchestration. The CLI's
``--effort`` flag rejects the literal "ultracode" (only low/medium/high/
xhigh/max), so PinkyBot resolves it to xhigh for the effort knob and layers
the workflow-by-default behavior in via a system-prompt directive.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from pinky_daemon.agent_registry import ULTRACODE_DIRECTIVE, AgentRegistry
from pinky_daemon.effort import (
    CLI_EFFORT_LEVELS,
    EFFORT_LEVELS,
    ULTRACODE_BASE_EFFORT,
    is_ultracode,
    resolve_cli_effort,
)

# ── effort.py vocabulary + resolution ──────────────────────────────────────


def test_ultracode_is_pinky_native_not_cli_native():
    """The CLI flag rejects 'ultracode'; it must NOT be in the CLI set, but
    must be in the full Pinky effort vocabulary."""
    assert "ultracode" in EFFORT_LEVELS
    assert "ultracode" not in CLI_EFFORT_LEVELS


def test_resolve_ultracode_maps_to_xhigh():
    assert resolve_cli_effort("ultracode") == ULTRACODE_BASE_EFFORT == "xhigh"


def test_resolve_is_case_and_space_tolerant():
    assert resolve_cli_effort("  UltraCode ") == "xhigh"
    assert is_ultracode(" ULTRACODE ")


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max", "auto"])
def test_resolve_passes_through_non_ultracode(level):
    assert resolve_cli_effort(level) == level


def test_resolve_none_passes_through():
    assert resolve_cli_effort(None) is None


def test_is_ultracode_negatives():
    for v in (None, "", "max", "xhigh", "ultra", "code"):
        assert not is_ultracode(v)


# ── build_system_prompt directive injection ────────────────────────────────


@pytest.fixture
def registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    r = AgentRegistry(db_path=path)
    yield r
    r.close()
    os.unlink(path)


def test_directive_injected_when_default_effort_is_ultracode(registry):
    registry.register("nova", model="opus", soul="I am Nova.", thinking_effort="ultracode")
    prompt = registry.build_system_prompt("nova")
    assert ULTRACODE_DIRECTIVE in prompt
    assert "Ultracode Mode" in prompt


def test_directive_absent_for_non_ultracode_default(registry):
    registry.register("nova", model="opus", soul="I am Nova.", thinking_effort="max")
    prompt = registry.build_system_prompt("nova")
    assert ULTRACODE_DIRECTIVE not in prompt
    assert "Ultracode Mode" not in prompt


def test_explicit_effort_param_overrides_default(registry):
    """A session-override effort passed by the caller drives the directive,
    independent of the stored default."""
    registry.register("nova", model="opus", soul="I am Nova.", thinking_effort="medium")
    # Session running ultracode even though default is medium.
    prompt = registry.build_system_prompt("nova", effort="ultracode")
    assert ULTRACODE_DIRECTIVE in prompt


def test_explicit_non_ultracode_param_suppresses_ultracode_default(registry):
    """Passing a concrete non-ultracode effort suppresses the directive even
    when the stored default is ultracode (session dialed back down)."""
    registry.register("nova", model="opus", soul="I am Nova.", thinking_effort="ultracode")
    prompt = registry.build_system_prompt("nova", effort="high")
    assert ULTRACODE_DIRECTIVE not in prompt
