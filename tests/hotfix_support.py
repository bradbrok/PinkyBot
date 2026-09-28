"""Scratch-only fixtures for authorization boundary integration tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pinky_daemon import api
from pinky_daemon.auth import build_internal_auth_headers


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", True)
    monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", "enforce")
    monkeypatch.setenv("PINKY_TOOL_POLICY", "off")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    apps = []

    def build(mode="enforce"):
        if mode is None:
            monkeypatch.delenv("PINKY_ISOLATED_POLICY_MODE", raising=False)
        else:
            monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
        root = tmp_path / f"app-{len(apps)}"
        root.mkdir()
        app = api.create_api(db_path=str(root / "memory.db"), default_working_dir=str(root))
        apps.append(app)
        agents = app.state.agents
        for name, isolated in (("tenant", True), ("normal", False), ("peer", True),
                               ("dreamer", False)):
            work = root / name
            work.mkdir()
            agents.register(name, isolated=isolated, working_dir=str(work))
        return SimpleNamespace(app=app, agents=agents, root=root)

    yield build
    for app in reversed(apps):
        app.state.store_catalog.close()


def signed(d, method, path, name="tenant"):
    return build_internal_auth_headers(
        d.agents.get_signing_key(name), agent_name=name, method=method, path=path
    )

