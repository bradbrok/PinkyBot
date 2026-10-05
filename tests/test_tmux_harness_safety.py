"""Test-process socket containment, checked before any real tmux integration."""

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from pinky_daemon import tmux_session
from tests.tmux_isolated_env_support import LaunchProbe
from tests.tmux_socket_support import check_tmux_argv, private_labels, private_socket


@pytest.mark.parametrize("route", [[], ["-L", "default"], ["-L", "pinkybot"],
    ["-Lpinkybot"], ["-L", "unregistered-test"], ["-S", "/tmp/unowned-test"],
    ["-S/tmp/unowned-test"]])
@pytest.mark.parametrize("api", ["sync", "async"])
async def test_unowned_routes_refused_before_exec(route, api):
    with pytest.raises(RuntimeError, match="explicit private socket"):
        if api == "sync":
            subprocess.run(["tmux", *route, "has-session"], env={}, timeout=1)
        else:
            await asyncio.create_subprocess_exec("tmux", *route, "has-session", env={})


def test_registered_default_route_requires_its_explicit_root():
    with private_labels("default") as root:
        command = ["tmux", "-L", "default", "has-session"]
        check_tmux_argv(command, {"TMUX_TMPDIR": root})
        for env in ({}, {"TMUX_TMPDIR": "/tmp"}, {"TMUX": root + "/socket,1,1"}):
            with pytest.raises(RuntimeError):
                check_tmux_argv(command, env)
    with pytest.raises(RuntimeError):
        check_tmux_argv(command, {"TMUX_TMPDIR": root})


@pytest.mark.parametrize("joined", [False, True])
def test_registered_socket_and_scrollback_option(joined):
    with private_socket() as socket:
        route = ["-S" + socket] if joined else ["-S", socket]
        check_tmux_argv(["tmux", *route, "capture-pane", "-S", "-100"], {})
    with pytest.raises(RuntimeError):
        check_tmux_argv(["tmux", *route, "has-session"], {})


@pytest.mark.parametrize("prefix", [["env", "-i"], ["runuser", "-u", "synthetic", "--"],
    ["podman", "exec", "synthetic", "--"], ["sudo", "--"]])
def test_wrapped_route_cannot_escape(prefix):
    with private_labels("test-owned") as root:
        with pytest.raises(RuntimeError):
            check_tmux_argv([*prefix, "tmux", "-L", "test-owned", "has-session"],
                            {"TMUX_TMPDIR": root})


def test_env_reset_with_explicit_private_routing_is_allowed():
    with private_labels("test-owned") as root:
        check_tmux_argv(["env", "-i", "TMUX_TMPDIR=" + root, "tmux", "-L",
                         "test-owned", "has-session"], {})


def test_production_label_refused_even_with_owned_root():
    with pytest.raises(AssertionError):
        with private_labels("pinkybot"):
            pass
    with private_socket() as socket:
        with pytest.raises(RuntimeError):
            check_tmux_argv(["tmux", "-Lpinkybot", "-S", socket, "has-session"], {})


def test_shell_cannot_hide_tmux():
    for args in (["sh", "-c", "exec tmux has-session"], "tmux has-session",
                 "echo $(tmux has-session)", "true; /usr/bin/tmux has-session"):
        with pytest.raises(RuntimeError):
            check_tmux_argv(args, {})


def test_shell_loader_python_source_is_not_a_tmux_command():
    check_tmux_argv(["/bin/sh", "-c", "python3 -c 'print(\"tmux-launch-env\")'"], {})


def test_launch_probe_keeps_private_root_and_uses_installed_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.tmux_isolated_env_support.shutil.which", lambda _: "/fake/tmux")
    monkeypatch.setattr(subprocess, "run", Mock())
    probe = LaunchProbe(tmp_path, monkeypatch)
    try:
        assert probe.seed["TMUX_TMPDIR"] == str(probe.socket_root)
        assert os.environ["TMUX_TMPDIR"] == str(probe.socket_root)
        assert "PYTHONPATH" not in probe.seed
        assert Path(tmux_session.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1]
        assert probe.socket_root.stat().st_mode & 0o777 == 0o700
        assert len(os.fsencode(probe.socket.resolve())) < 100
        check_tmux_argv(["tmux", "-S", str(probe.socket), "has-session"], probe.seed)
    finally:
        probe._socket_owner.__exit__(None, None, None)


async def test_launch_probe_clean_payload_retains_private_root(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.tmux_isolated_env_support.shutil.which", lambda _: "/fake/tmux")
    monkeypatch.setattr(subprocess, "run", Mock())
    probe = LaunchProbe(tmp_path, monkeypatch)
    captured = {}

    async def fake_spawn(session):
        captured.update(session._build_repl_env())
        probe.names_path.write_text("[]")

    monkeypatch.setattr(tmux_session.TmuxSession, "_spawn_tmux_repl", fake_spawn)
    try:
        await probe.launch()
        assert captured["TMUX_TMPDIR"] == str(probe.socket_root)
        assert "PYTHONPATH" not in captured
    finally:
        probe._socket_owner.__exit__(None, None, None)
