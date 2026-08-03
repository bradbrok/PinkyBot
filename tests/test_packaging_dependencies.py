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


def test_claude_agent_sdk_excludes_pretooluse_bypass_version() -> None:
    assert "claude-agent-sdk>=0.2.127,<0.3" in _project()["dependencies"]
