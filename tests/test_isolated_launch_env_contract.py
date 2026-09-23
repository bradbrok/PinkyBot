"""Isolated launches no longer inherit the daemon environment."""

import json

import pytest

from tests.tmux_isolated_env_support import DAEMON_NAMES, SYNTHETIC_VALUES, Registry, launch_probe


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
@pytest.mark.parametrize("status", ["isolated", "unknown"])
async def test_isolated_child_has_no_daemon_names(tmp_path, monkeypatch, kind, status):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        names = await probe.launch(kind, registry=Registry(status))
        assert "EXPLICIT_ALLOWED_NAME" in names
        assert not DAEMON_NAMES & names, "isolated child inherited daemon names"
        assert "PINKY_AGENT_KEY" in names


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
async def test_isolated_child_has_scoped_hook_identity(tmp_path, monkeypatch, kind):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        names = await probe.launch(kind)
        assert "PINKY_AGENT_KEY" in names


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
async def test_intentional_empty_override_stays_present(tmp_path, monkeypatch, kind):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        names = await probe.launch(kind)
        assert "CLAUDE_CODE_OAUTH_TOKEN" in names
        assert "CLAUDE_CODE_OAUTH_TOKEN" in json.loads(probe.empty_names_path.read_text())


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
async def test_nonisolated_child_retains_parity(tmp_path, monkeypatch, kind):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        names = await probe.launch(kind, registry=Registry("not_isolated"))
        assert DAEMON_NAMES <= names
        assert "EXPLICIT_ALLOWED_NAME" in names


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
@pytest.mark.parametrize("forbidden", ["PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"])
async def test_daemon_only_payload_is_refused_loudly(tmp_path, monkeypatch, kind, forbidden):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        with pytest.raises(PermissionError, match="daemon-only"):
            await probe.launch(kind, forbidden=forbidden)
        assert not probe.names_path.exists(), "refused payload spawned a child"
        logs = "\n".join(probe.logs)
        assert "ERROR" in logs and forbidden in logs
        assert all(value not in logs for value in SYNTHETIC_VALUES.values())


@pytest.mark.parametrize("mode", ["container", "unix_user"])
async def test_nonlocal_mode_requires_clean_child_despite_false_isolated_flag(
    tmp_path, monkeypatch, mode,
):
    async with launch_probe(tmp_path, monkeypatch, mode="enforce") as probe:
        names = await probe.launch(registry=Registry("not_isolated", mode=mode))
        assert not DAEMON_NAMES & names
