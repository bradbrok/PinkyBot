"""Synthetic runner and shell probes for launch-environment boundary tests."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from pinky_daemon.command_runner import CommandResult, LocalCommandRunner


class LaunchRecorder(LocalCommandRunner):
    """Execute staging helpers, but record tmux commands without starting a server."""

    def __init__(self, home: Path):
        self.home = home
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.tmux_calls: list[list[str]] = []
        self.fail_staging = False

    async def run(self, argv, *, timeout=None, stdin_data=None):
        argv = list(argv)
        self.calls.append((argv, stdin_data))
        inner = argv
        if argv[0] == "podman":
            inner = argv[argv.index("--") + 2 :]
        elif argv[0] == "runuser":
            inner = argv[argv.index("--") + 1 :]
        if "new-session" in inner:
            self.tmux_calls.append(inner)
            return CommandResult(0, b"", b"")
        if self.fail_staging:
            return CommandResult(1, b"", b"synthetic-stage-failure")
        proc = subprocess.run(
            inner,
            input=stdin_data,
            capture_output=True,
            timeout=timeout or 5,
            env={"HOME": str(self.home), "PATH": os.environ.get("PATH", os.defpath)},
        )
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def env_pairs(argv):
    return dict(arg.split("=", 1) for i, arg in enumerate(argv) if i and argv[i - 1] == "-e")


def secret_files(home: Path, sentinel: str) -> list[Path]:
    return [p for p in home.rglob("*") if p.is_file() and sentinel.encode() in p.read_bytes()]


def probe_command(home: Path) -> str:
    code = (
        "import json,os,pathlib; "
        "root=pathlib.Path(os.environ['HOME']); "
        "print(json.dumps({'env':dict(os.environ), "
        "'files_at_exec':[str(p) for p in root.rglob('*') if p.is_file()]}))"
    )
    return shlex.join([sys.executable, "-c", code])


def run_pane(argv, home: Path, inherited=None):
    env = {"HOME": str(home), "PATH": os.defpath, **(inherited or {}), **env_pairs(argv)}
    return subprocess.run(
        ["/bin/sh", "-c", argv[-1]],
        env=env,
        capture_output=True,
        timeout=5,
    )


def child_payload(result):
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads(result.stdout)
