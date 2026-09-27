"""Regression: reserved names retained by an existing tmux server."""
import pytest

from tests import test_codex_mcp_header_env as supplied
from tests.test_codex_mcp_header_env import harness  # noqa: F401


@pytest.mark.parametrize('clean', [False, True])
@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('resume', [False, True])
async def test_tmux_server_stale_header_does_not_reach_codex(
    harness, monkeypatch, clean, remote, resume,
):
    real_run_pane = supplied.run_pane

    def contaminated_pane(argv, home, inherited=None):
        inherited = dict(inherited or {})
        inherited['PINKY_MCP_HDR_PREVIOUS_AUTHORIZATION'] = 'inert-inherited-marker'
        result = real_run_pane(argv, home, inherited=inherited)
        payload = supplied.child_payload(result)
        assert 'PINKY_MCP_HDR_PREVIOUS_AUTHORIZATION' not in payload['env'], (
            'Reserved header from the tmux server survived into the provider environment'
        )
        return result

    monkeypatch.setattr(supplied, 'run_pane', contaminated_pane)
    await supplied.test_tmux_stages_headers_privately_then_shell_delivers_and_unlinks(
        harness, monkeypatch, remote, clean, resume,
    )


async def test_agent_b_drops_agent_a_headers_from_shared_server(harness, monkeypatch):
    real_run_pane = supplied.run_pane

    def shared_server_pane(argv, home, inherited=None):
        server_env = dict(inherited or {})
        server_env['PINKY_MCP_HDR_AGENT_A_AUTHORIZATION'] = 'inert-agent-a-marker'
        result = real_run_pane(argv, home, inherited=server_env)
        payload = supplied.child_payload(result)
        assert 'PINKY_MCP_HDR_AGENT_A_AUTHORIZATION' not in payload['env']
        assert {k: v for k, v in payload['env'].items() if k.startswith(supplied.PREFIX)} == (
            supplied.EXPECTED
        )
        return result

    monkeypatch.setattr(supplied, 'run_pane', shared_server_pane)
    await supplied.test_tmux_stages_headers_privately_then_shell_delivers_and_unlinks(
        harness, monkeypatch, False, False, False,
    )
