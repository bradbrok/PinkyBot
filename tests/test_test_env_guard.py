"""Regression coverage for suite-wide ambient environment isolation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
)

# Bound at module collection time, BEFORE the autouse fixture patches
# ``claude_agent_sdk.ClaudeSDKClient``. This is exactly the bypass the guard
# must defeat: a reference captured this early holds the real class, so the
# package-attribute patch never sees it — only the transport-level guard does.
from claude_agent_sdk.client import ClaudeSDKClient as _CollectionTimeClientAlias

import tests.conftest as suite_conftest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DREAM_TEST = (
    "tests/test_api.py::TestAPI::"
    "test_manual_dream_uses_full_persisted_conversation_history"
)
_SANITIZER_TEST = "tests/test_test_env_guard.py::test_runtime_env_is_deterministic"


class _TransportItem:
    def __init__(self, *, marked: bool):
        self.marked = marked

    def get_closest_marker(self, name: str):
        if name == "real_transport" and self.marked:
            return object()
        return None


def test_runtime_env_is_deterministic():
    assert os.environ["HOME"] == suite_conftest.TEST_HOME
    assert os.environ["CLAUDE_CONFIG_DIR"] == suite_conftest.TEST_CLAUDE_CONFIG_DIR
    assert (
        os.environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"]
        == suite_conftest.TEST_CLAUDE_SECURESTORAGE_DIR
    )
    assert (
        os.environ["ANTHROPIC_CONFIG_DIR"]
        == suite_conftest.TEST_ANTHROPIC_CONFIG_DIR
    )
    assert os.environ["CODEX_HOME"] == suite_conftest.TEST_CODEX_HOME
    assert Path(os.environ["HOME"]).is_dir()
    assert Path(os.environ["CLAUDE_CONFIG_DIR"]).is_dir()
    assert Path(os.environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"]).is_dir()
    assert Path(os.environ["ANTHROPIC_CONFIG_DIR"]).is_dir()
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert os.environ["ANTHROPIC_API_KEY"] == ""
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == ""
    assert os.environ["PINKY_DREAM_TRANSPORT"] == "sdk"
    assert os.environ["PINKY_AUTH_DENY_DEFAULT"] == "shadow"
    assert os.environ["PINKY_TEST_TRANSPORT_GUARD"] == "1"
    assert "PINKY_CONTAINER_RUNTIME" not in os.environ


def test_query_entrypoint_is_blocked_before_spawn():
    """The public query() path — used by SDKRunner — hits the guard.

    This exercises the ACTUAL escape from the review: ``query()`` builds its own
    ``SubprocessCLITransport`` and the package-attribute ``ClaudeSDKClient``
    patch never sees it. Driving query() (not constructing the transport by
    hand) locks the real path, so a permitted SDK update that reroutes query's
    transport can't leave this test falsely green while the credential path
    reopens.
    """
    import asyncio

    async def _drive_query() -> None:
        async for _message in query(
            prompt="isolation probe", options=ClaudeAgentOptions()
        ):
            pass

    with pytest.raises(
        pytest.fail.Exception,
        match="blocked a real Claude CLI subprocess spawn",
    ):
        asyncio.run(_drive_query())


def test_preimport_client_alias_is_blocked_at_transport():
    """A ClaudeSDKClient captured at collection time still fails at the transport.

    ``_CollectionTimeClientAlias`` was bound at module import, before the autouse
    fixture patched ``claude_agent_sdk.ClaudeSDKClient`` — so it holds the real
    class and bypasses the attribute patch. Construction succeeds, but
    ``connect`` reaches the guarded transport and fails loudly instead of
    spawning the real CLI.
    """
    import asyncio

    client = _CollectionTimeClientAlias(ClaudeAgentOptions())
    with pytest.raises(
        pytest.fail.Exception,
        match="blocked a real Claude CLI subprocess spawn",
    ):
        asyncio.run(client.connect())


def test_inherited_securestorage_dir_cannot_reauthenticate():
    """A hostile inherited CLAUDE_SECURESTORAGE_CONFIG_DIR is overridden.

    Pointing that var at a populated directory re-authenticated the bundled CLI
    before this scrub covered it. The suite must replace any inherited value
    with its fresh empty dir; a subprocess run with a hostile value set proves
    the override holds rather than leaking through.
    """
    env = os.environ.copy()
    env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = "/nonexistent/hostile/securestorage"
    env["ANTHROPIC_CONFIG_DIR"] = "/nonexistent/hostile/anthropic-config"
    env.pop("PINKY_TEST_REAL_TRANSPORT", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            _SANITIZER_TEST,
            (
                "tests/test_test_env_guard.py::"
                "test_bundled_claude_cli_cannot_authenticate_under_test_env"
            ),
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "2 passed" in output


def test_bundled_claude_cli_cannot_authenticate_under_test_env():
    """The suite environment must never expose usable Claude credentials."""
    transport = SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())
    cli_path = transport._find_bundled_cli()
    assert cli_path is not None, "claude-agent-sdk wheel has no bundled CLI"

    result = subprocess.run(
        [cli_path, "auth", "status", "--json"],
        cwd=_REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    auth_status = json.loads(result.stdout)
    assert auth_status["loggedIn"] is False, output
    assert auth_status["authMethod"] == "none", output


def test_unpatched_sdk_construction_fails_loudly():
    from claude_agent_sdk import ClaudeSDKClient

    with pytest.raises(
        pytest.fail.Exception,
        match="unpatched _ensure_streaming_session boundary",
    ):
        ClaudeSDKClient(ClaudeAgentOptions())


def test_real_transport_marker_requires_process_opt_in(monkeypatch):
    monkeypatch.setattr(suite_conftest, "_REAL_TRANSPORT_OPTED_IN", False)

    with pytest.raises(pytest.skip.Exception):
        suite_conftest.pytest_runtest_setup(_TransportItem(marked=True))


def test_process_opt_in_only_applies_to_marked_tests(monkeypatch):
    item = _TransportItem(marked=False)
    monkeypatch.setattr(suite_conftest, "_REAL_TRANSPORT_OPTED_IN", False)
    suite_conftest.pytest_runtest_setup(item)

    monkeypatch.setattr(suite_conftest, "_REAL_TRANSPORT_OPTED_IN", True)
    suite_conftest.pytest_runtest_setup(_TransportItem(marked=True))


def test_manual_dream_ignores_ambient_real_transport():
    """An ambient live transport cannot bypass the dream test's SDK mock."""
    env = os.environ.copy()
    env["PINKY_DREAM_TRANSPORT"] = "tmux"
    env["PINKY_AUTH_DENY_DEFAULT"] = "enforce"
    env["PINKY_CONTAINER_RUNTIME"] = "podman"
    env.pop("PINKY_TEST_REAL_TRANSPORT", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            _DREAM_TEST,
            _SANITIZER_TEST,
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "2 passed" in output
