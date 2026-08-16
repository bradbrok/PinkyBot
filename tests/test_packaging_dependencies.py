"""Packaging contracts for dependencies required by daemon runtime paths."""

import tomllib
from pathlib import Path


def _project() -> dict:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    return tomllib.loads(pyproject.read_text())["project"]


def test_slack_socket_mode_dependencies_are_in_base_install() -> None:
    project = _project()

    assert "slack-sdk>=3.0" in project["dependencies"]
    assert "aiohttp>=3.9" in project["dependencies"]
    assert project["optional-dependencies"]["slack"] == []


def test_buzz_dependencies_are_in_base_install() -> None:
    project = _project()

    assert "coincurve>=21.0" in project["dependencies"]
    assert "httpx>=0.28" in project["dependencies"]
    assert "websockets>=15.0" in project["dependencies"]
    assert "pynacl>=1.5" in project["dependencies"]
    assert project["optional-dependencies"]["buzz"] == []
    assert "buzz" in project["optional-dependencies"]["all"][0]


def test_claude_agent_sdk_excludes_allowed_tools_injection_versions() -> None:
    assert "claude-agent-sdk>=0.2.138,<0.3" in _project()["dependencies"]
