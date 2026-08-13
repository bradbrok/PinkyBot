"""Regression coverage for suite-wide ambient environment isolation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert os.environ["PINKY_DREAM_TRANSPORT"] == "sdk"
    assert os.environ["PINKY_AUTH_DENY_DEFAULT"] == "shadow"
    assert "PINKY_CONTAINER_RUNTIME" not in os.environ


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
