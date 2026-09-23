"""Measure interpreter-added environment keys independently of launch staging."""

import os
import shlex
import subprocess

from tests.tmux_env_support import child_payload, probe_command, run_pane


def test_plain_interpreter_and_shell_control_agree_on_added_keys(tmp_path, record_property):
    base = {"HOME": str(tmp_path), "PATH": os.defpath}
    command = probe_command(tmp_path)
    direct = child_payload(subprocess.run(
        shlex.split(command), env=base, capture_output=True, timeout=5,
    ))["env"]
    control = child_payload(run_pane([command], tmp_path))["env"]
    added = set(direct) - set(base)
    record_property("interpreter_added_keys", sorted(added))
    assert {key: direct[key] for key in base} == base
    assert {key: control[key] for key in base} == base
    assert added <= set(control) - set(base)
    assert not any(key.startswith("__PINKY_LAUNCH_") for key in direct)
    assert not any(key.startswith("__PINKY_LAUNCH_") for key in control)
