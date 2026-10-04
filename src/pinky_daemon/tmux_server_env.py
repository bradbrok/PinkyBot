"""Captured routing and a constructed client environment for managed tmux."""

from __future__ import annotations

import json
import os
import pwd
import re
import sys
from dataclasses import dataclass
from types import SimpleNamespace

from pinky_daemon.isolated_launch_env import LaunchConfigError, LaunchEnvError
from pinky_daemon.tmux_launch_env_loader import is_base_name

STANDARD_PATH = (
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
)
MAX_ENV_BYTES = 65536
_CAPABILITIES: dict[tuple, bool] = {}
_WARNED: set[tuple] = set()


def _value(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\0\r\n"):
        raise LaunchConfigError("invalid tmux base environment")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise LaunchConfigError("invalid tmux base environment") from None
    return value


def _absolute(value: str) -> str:
    if not os.path.isabs(_value(value)):
        raise LaunchConfigError("invalid tmux base path")
    return value


def pane_path(daemon: dict[str, str]) -> str:
    explicit = "PINKY_TMUX_PANE_PATH" in daemon
    source = daemon.get("PINKY_TMUX_PANE_PATH") if explicit else daemon.get("PATH")
    parts = [] if source is None else [_absolute(p) for p in source.split(os.pathsep)]
    if not explicit:
        parts.extend(STANDARD_PATH)
    return os.pathsep.join(dict.fromkeys(parts))


def client_environment(daemon: dict[str, str]) -> dict[str, str]:
    env = {name: _value(value) for name, value in daemon.items() if is_base_name(name)}
    account = None
    if any(name not in env for name in ("HOME", "USER", "LOGNAME")):
        account = pwd.getpwuid(os.geteuid())
    env.setdefault("HOME", account.pw_dir if account else "")
    env.setdefault("USER", account.pw_name if account else "")
    env.setdefault("LOGNAME", account.pw_name if account else "")
    env.setdefault("SHELL", "/bin/sh")
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("LANG", "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8")
    env["PATH"] = pane_path(daemon)
    for name in ("HOME", "SHELL", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        if name in env:
            _absolute(env[name])
    for name in ("USER", "LOGNAME", "TERM", "LANG"):
        if not env[name] or any(c.isspace() for c in env[name]):
            raise LaunchConfigError("invalid tmux base identity or terminal")
    if "TMUX_TMPDIR" in daemon:
        env["TMUX_TMPDIR"] = _absolute(daemon["TMUX_TMPDIR"])
    return env


@dataclass(frozen=True)
class ServerConfig:
    label: str
    client_env: dict[str, str]

    @classmethod
    def capture(cls, *, label: str | None = None) -> ServerConfig:
        daemon = dict(os.environ)
        if label is None:
            label = daemon.get("PINKY_TMUX_SOCKET", "pinkybot")
            if label and (not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", label) or label in {".", "..", "default"}):
                raise LaunchConfigError("invalid tmux socket configuration")
            label = label or "default"
        return cls(label, client_environment(daemon))

    def key(self, control) -> tuple:
        return control.tmux_binary, str(control._local_socket_path())

    async def verify(self, control, *, clean: bool, log) -> None:
        key = self.key(control)
        if self.label == "default":
            if clean:
                raise LaunchEnvError("isolated launch requires a managed tmux server")
            self._warn(key, "shared tmux compatibility", (), log)
            return
        foreign = set()
        try:
            if key not in _CAPABILITIES:
                version = await control._run_raw("-V", max_output_bytes=MAX_ENV_BYTES)
                text = version.stdout.decode("utf-8", errors="strict").strip()
                match = re.fullmatch(r"tmux (\d+)\.(\d+)[a-z]?", text)
                _CAPABILITIES[key] = bool(version.ok and match and tuple(map(int, match.groups())) >= (3, 2))
            if not _CAPABILITIES[key]:
                raise ValueError("hidden environment unsupported")
            for args in (("show-environment", "-g"), ("show-environment", "-g", "-h")):
                result = await control._run_raw(*args, max_output_bytes=MAX_ENV_BYTES)
                if len(result.stdout) + len(result.stderr) > MAX_ENV_BYTES:
                    raise ValueError("environment read too large")
                stdout = result.stdout.decode("utf-8", errors="strict")
                stderr = result.stderr.decode("utf-8", errors="strict")
                if not result.ok:
                    decoded = SimpleNamespace(returncode=result.returncode, stdout=stdout, stderr=stderr)
                    # Reuse the strict control's absence classification. A
                    # missing socket has no globals; the constructed client
                    # environment is the only input to a new server.
                    if (control._server_absence_is_reported(decoded) or
                            (result.returncode == 1 and not stdout and control._server_socket_is_missing())):
                        continue
                    raise ValueError("environment read failed")
                # Multiline values can add phantom names. This intentionally
                # fails closed instead of trying to interpret shell quoting.
                for line in stdout.splitlines():
                    if not line:
                        continue
                    name = line.split("=", 1)[0] if "=" in line else line[1:] if line.startswith("-") else ""
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                        raise ValueError("invalid environment name")
                    if not is_base_name(name) and name not in {"TMUX_TMPDIR", "PWD"}:
                        foreign.add(name)
        except Exception:
            if clean:
                raise LaunchEnvError("tmux server environment verification failed") from None
            self._warn(key, "tmux server environment read failed", (), log)
            return
        if foreign:
            if clean:
                raise LaunchEnvError("tmux server environment refused")
            self._warn(key, "tmux server environment names", tuple(sorted(foreign)), log)

    @staticmethod
    def _warn(key, reason, names, log):
        identity = (key, reason, names)
        if identity not in _WARNED:
            _WARNED.add(identity)
            log("WARNING " + reason + (": " + json.dumps(names) if names else ""))


def normalize_codex_path(env, control):
    config = getattr(control, "server_config", None)
    if isinstance(config, ServerConfig):
        from pinky_daemon.command_runner import LocalCommandRunner

        if type(control._runner) is LocalCommandRunner:
            env["PATH"] = config.client_env["PATH"]
    return env
