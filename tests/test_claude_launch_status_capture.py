"""Host launch diagnostics must use the captured status without a second lookup."""

from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import isolated_launch_env, tmux_session
from pinky_daemon.tmux_session import TmuxCommandResult
from tests.test_claude_host_env_payload import clean_daemon as clean_daemon
from tests.test_claude_host_env_payload import host_session
from tests.test_claude_launch_authority import authority as authority


class LateStatusLookupError(RuntimeError):
    """Synthetic failure from a lookup after the launch snapshot was captured."""


@pytest.mark.parametrize("mode", ("off", "shadow"))
@pytest.mark.parametrize("late_lookup", ("conflict", "raises"))
async def test_host_status_diagnostics_use_captured_policy(authority, mode, late_lookup):
    h = authority
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    h.registry.key = ""
    owner = host_session(h.root, registry=h.registry)
    snapshots, report_statuses, delivered = [], [], []
    capture = owner._launch_env_policy
    report = isolated_launch_env.report_shadow
    late_status = Mock(
        return_value="isolated",
        side_effect=LateStatusLookupError("synthetic late status failure")
        if late_lookup == "raises"
        else None,
    )

    def capture_then_poison_status():
        policy = capture()
        snapshots.append(policy)
        h.patch.setattr(owner, "_isolation_status", late_status)
        return policy

    def observe_report(**kwargs):
        report_statuses.append(kwargs["status"])
        report(**kwargs)

    async def spawn(**kwargs):
        delivered.append(kwargs["env"])
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    h.patch.setattr(owner, "_launch_env_policy", capture_then_poison_status)
    h.patch.setattr(isolated_launch_env, "report_shadow", observe_report)
    h.patch.setattr(owner._tmux, "new_session", spawn)
    h.patch.setattr(owner._tmux, "has_session", AsyncMock(side_effect=[False, True]))
    for name in (
        "_ensure_container_started",
        "_reap_retained_spawn_cleanup_debt",
        "_seed_container_trust",
        "_seed_container_home_creds",
        "_stop_tailer",
        "_start_tailer",
    ):
        h.patch.setattr(owner, name, AsyncMock())
    h.patch.setattr(owner, "_container_agent", lambda **kwargs: None)
    h.patch.setattr(owner, "_select_command_runner", lambda *args: owner._tmux._runner)
    h.patch.setattr(owner, "_prepare_tmux_spawn", lambda: None)
    h.patch.setattr(owner, "_build_claude_cmd", lambda: "/bin/true")
    h.patch.setattr(tmux_session, "_seed_claude_trust_file", lambda *args: False)
    h.patch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)

    lookup_crashed = False
    try:
        await owner._spawn_tmux_repl()
    except LateStatusLookupError:
        lookup_crashed = True
    assert not lookup_crashed, "host launch crashed on a late status lookup"
    assert len(snapshots) == 1, "host did not capture exactly one policy"
    assert len(delivered) == 1, "host did not reach the launch seam"
    captured_status = snapshots[0].status
    conflicting_control = captured_status != late_status.return_value
    assert conflicting_control, "late-status control does not conflict with the snapshot"
    reports_match = report_statuses == [captured_status]
    assert reports_match, "shadow reporter received a status outside the launch snapshot"
    prefix = f"tmux[{owner.agent_name}]: launch signing key unavailable; status="
    keyless_logs = [line for line in h.logs if line.startswith(prefix)]
    logs_match = keyless_logs == [prefix + captured_status] * 2
    assert logs_match, "preflight or launch signing diagnostic used a late status"
    assert late_status.call_count == 0, "host reread isolation status after capture"
