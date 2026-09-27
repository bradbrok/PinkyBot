"""Regression: reserved names retained by an existing tmux server."""
import pytest

from tests import test_codex_mcp_header_env as supplied

harness = supplied.harness


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


@pytest.mark.parametrize('codex_headers', [False, True])
@pytest.mark.parametrize('empty_header', [False, True])
async def test_empty_launch_scrubs_only_when_codex_policy_is_enabled(
    harness, codex_headers, empty_header,
):
    from pinky_daemon.tmux_session import _TmuxControl
    from tests.tmux_env_support import LaunchRecorder, probe_command

    home, _ = harness
    recorder = LaunchRecorder(home)
    control = _TmuxControl('empty-header-policy', command_runner=recorder)
    own = supplied.PREFIX + 'CURRENT_AUTHORIZATION'
    await control.new_session(
        cwd=str(home), command=probe_command(home),
        env={own: ''} if empty_header else {}, codex_headers=codex_headers,
    )
    key = supplied.PREFIX + 'UNCONFIGURED_AUTHORIZATION'
    pane = supplied.run_pane(recorder.tmux_calls[-1], home, inherited={key: 'inert-stale'})
    child_env = supplied.child_payload(pane)['env']
    assert (key in child_env) is not codex_headers
    if empty_header:
        assert child_env[own] == ''


async def test_real_shared_tmux_server_keeps_current_headers_out_of_global_env(harness):
    import asyncio
    import json
    import os
    import shlex
    import shutil
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from pinky_daemon.tmux_session import _TmuxControl

    binary = shutil.which('tmux')
    if binary is None:
        pytest.skip('real tmux unavailable')
    home, _ = harness
    stale = supplied.PREFIX + 'AGENT_A_AUTHORIZATION'
    own = supplied.PREFIX + 'AGENT_B_AUTHORIZATION'
    server_env = {'HOME': str(home), 'PATH': os.defpath, stale: 'inert-agent-a'}
    socket_root = Path(tempfile.mkdtemp(prefix='header-server-', dir='/tmp'))
    socket = socket_root / 'server.sock'
    base = [binary, '-S', str(socket)]
    output = home / 'child-observations.json'
    code = (
        'import json,os,pathlib; '
        f'pathlib.Path({str(output)!r}).write_text(json.dumps({{'
        f'"stale_present": {stale!r} in os.environ, '
        f'"own_matches": os.environ.get({own!r}) == "inert-agent-b"}}))'
    )
    try:
        subprocess.run(base + ['new-session', '-d', '-s', 'keeper', 'sleep 30'],
                       env=server_env, capture_output=True, check=True, timeout=5)
        control = _TmuxControl('agent-b', tmux_binary=binary, socket_path=str(socket))
        result = await control.new_session(
            cwd=str(home), command=shlex.join([sys.executable, '-I', '-c', code]),
            env={own: 'inert-agent-b'}, codex_headers=True,
        )
        assert result.ok
        for _ in range(200):
            if output.exists():
                break
            await asyncio.sleep(0.01)
        assert json.loads(output.read_text()) == {'stale_present': False, 'own_matches': True}
        # Inspect names only; never retain or report global environment values.
        global_result = subprocess.run(base + ['show-environment', '-g'], env=server_env,
                                       capture_output=True, check=True, timeout=5)
        names = {line.split(b'=', 1)[0] for line in global_result.stdout.splitlines()}
        assert stale.encode() in names
        assert own.encode() not in names
    finally:
        subprocess.run(base + ['kill-server'], env=server_env, capture_output=True, timeout=5)
        shutil.rmtree(socket_root)
