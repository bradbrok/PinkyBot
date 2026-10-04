"""Independent PR B expectations; no production implementation in this helper."""

import os
import re
from unittest.mock import patch

from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_dream_runner import TmuxDreamConfig, TmuxDreamRunner
from pinky_daemon.tmux_session import TmuxSession

BASE_NAMES = set("""PATH HOME USER LOGNAME SHELL TERM LANG TZ TMPDIR HTTP_PROXY
HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR
NODE_EXTRA_CA_CERTS REQUESTS_CA_BUNDLE""".split())
STANDARD_DIRS = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                 "/usr/local/sbin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
CANARY = "synthetic-private-" + "b" * 39
FAMILIES = ("claude", "codex", "app_server", "dream")


def allowed(names, *, globals=False):
    extras = {"TMUX_TMPDIR", "PWD"} if globals else {"TMUX_TMPDIR"}
    return all(n in BASE_NAMES | extras or
               (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) and n.startswith(("LC_", "XDG_"))) for n in names)


def no_values(*outputs):
    if any(CANARY in str(output) for output in outputs):
        raise AssertionError("protected synthetic value appeared in diagnostics or argv")


def owner(kind, root, *, agent="test-agent", registry=None):
    config = StreamingSessionConfig(agent_name=agent, working_dir=str(root))
    if kind == "claude":
        return TmuxSession(config, registry=registry)
    if kind == "codex":
        return CodexTmuxSession(config, registry=registry)
    if kind == "app_server":
        # app.sock is unrelated to the tmux route; avoid constructor fallback
        # directories during no-process contract tests.
        with patch.object(CodexAppServerSupervisor, "_resolve_sock_dir",
                          return_value=(str(root / "app-server"), False)):
            return CodexAppServerSupervisor(agent, working_dir=str(root), registry=registry)
    return TmuxDreamRunner(TmuxDreamConfig(working_dir=str(root)), agent_name=agent)


def control(owner):
    return getattr(owner, "_control", None) or owner._tmux


def seed(monkeypatch, root):
    for name in list(os.environ):
        monkeypatch.delenv(name)
    home = root / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    values = {"HOME": str(home), "USER": "synthetic-user", "LOGNAME": "synthetic-user", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
              "TERM": "xterm-256color", "SHELL": "/bin/sh", "TMPDIR": str(root),
              "PINKY_SESSION_SECRET": CANARY, "UNLISTED_DAEMON_NAME": CANARY,
              "BASH_ENV": CANARY, "TMUX_PANE": CANARY,
              "PINKY_CODEX_MCP_HEADER_AGENT_AUTHORIZATION": CANARY}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values
