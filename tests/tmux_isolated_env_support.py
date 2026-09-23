"""Private-server launch probes with synthetic inputs and names-only output."""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pinky_daemon import codex_app_server_tmux, codex_session, codex_tmux_session, tmux_session
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession, _TmuxControl

DAEMON_NAMES = {"PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET", "HRPOS_PASSWORD"}
SYNTHETIC_VALUES = {name: "synthetic-only-" + name.lower() for name in DAEMON_NAMES}


class Registry:
    def __init__(self, status="isolated", key="synthetic-agent-key", mode="local"):
        self.status, self.key, self.mode = status, key, mode

    def get(self, name):
        if self.status == "unknown":
            raise RuntimeError("synthetic registry unavailable")
        return SimpleNamespace(
            isolated=self.status == "isolated", isolation_mode=self.mode,
            tool_policy_enabled=False, dedicated_config_dir=False,
        )

    def get_signing_key(self, name):
        return self.key


class LaunchProbe:
    def __init__(self, root, monkeypatch):
        self.root, self.monkeypatch = root, monkeypatch
        self.logs = []
        self.tmux = shutil.which("tmux")
        assert self.tmux, "real tmux is required for the child environment contract"
        self.home = root / "home"
        self.home.mkdir(mode=0o700)
        self.names_path = root / "child-names.json"
        self.empty_names_path = root / "child-empty-names.json"
        self.bin = root / "bin"
        self.bin.mkdir()
        # Fake only the provider executable. App-server still uses the real shim
        # and socket lifecycle; all three paths use real tmux and the JSON loader.
        code = (
            "import json,os,pathlib,sys,time\n"
            f"pathlib.Path({str(self.empty_names_path)!r}).write_text("
            "json.dumps(sorted(k for k,v in os.environ.items() if v == '')))\n"
            f"pathlib.Path({str(self.names_path)!r}).write_text(json.dumps(sorted(os.environ)))\n"
            "if 'app-server' in sys.argv:\n"
            " for line in sys.stdin:\n"
            "  frame=json.loads(line)\n"
            "  if 'id' in frame: print(json.dumps({'id':frame['id'],'result':{}}),flush=True)\n"
            "else: time.sleep(60)\n"
        )
        for name in ("claude", "codex"):
            script = self.bin / name
            script.write_text(f"#!{sys.executable}\n" + code)
            script.chmod(0o700)
        for name in list(os.environ):
            monkeypatch.delenv(name)
        self.seed = {
            "HOME": str(self.home), "PATH": str(self.bin) + os.pathsep + os.defpath,
            "LANG": "C.UTF-8", "TERM": "xterm", **SYNTHETIC_VALUES,
            "EXPLICIT_ALLOWED_NAME": "synthetic-allowed",
            "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-inherited-token",
            "PYTHONPATH": str(Path(tmux_session.__file__).resolve().parents[1]),
        }
        for key, value in self.seed.items():
            monkeypatch.setenv(key, value)
        for mod in (tmux_session, codex_tmux_session, codex_app_server_tmux, codex_session):
            monkeypatch.setattr(mod, "_log", self.logs.append)
        self.socket_root = Path(tempfile.mkdtemp(prefix="isolated-env-", dir="/tmp"))
        self.socket = self.socket_root / "tmux.sock"
        subprocess.run(
            [self.tmux, "-f", "/dev/null", "-S", str(self.socket),
             "new-session", "-d", "-s", "seed", "sleep 60"],
            env=self.seed, check=True, capture_output=True, timeout=5,
        )
        self.control = _TmuxControl("probe", tmux_binary=self.tmux, socket_path=str(self.socket))
        self.supervisor = None
        self.client = None

    async def launch(
        self, kind="claude", *, registry=None, session=None, forbidden=None,
        provider_key="synthetic-provider-key", provider_url="", empty_token=True,
    ):
        m = self.monkeypatch
        registry = registry if registry is not None else Registry()
        config = StreamingSessionConfig(
            agent_name="test-tenant", working_dir=str(self.root),
            provider_key=provider_key, provider_url=provider_url,
        )
        if kind == "app_server":
            m.setenv("PINKY_CODEX_APP_SERVER", "1")
            m.setenv("PINKY_CODEX_TMUX_APP_SERVER", "1")
            owner = CodexSession(config, registry=registry)
            self.supervisor = owner._app_supervisor
            assert self.supervisor is not None
            self.supervisor._tmux = self.control
            self.supervisor._log = self.logs.append
            target, builder = self.supervisor, "_build_env"
        else:
            cls = TmuxSession if kind == "claude" else CodexTmuxSession
            session = session or cls(config, registry=registry, tmux_control=self.control)
            session._config.working_dir = str(self.root)
            session._tmux = self.control
            for name in ("_ensure_container_started", "_reap_retained_spawn_cleanup_debt",
                         "_seed_container_trust", "_seed_container_home_creds", "_stop_tailer",
                         "_start_tailer"):
                m.setattr(session, name, AsyncMock())
            m.setattr(session, "_container_agent", lambda **kwargs: None)
            m.setattr(session, "_select_command_runner", lambda *args: self.control._runner)
            m.setattr(session, "_prepare_tmux_spawn", lambda: None)
            m.setattr(session, "_spawn_cleanup_state_dir", lambda: self.home)
            m.setattr(session, "_build_claude_cmd", lambda: shlex.quote(str(self.bin / kind)))
            if kind == "codex":
                m.setattr(session, "_codex_dismiss_nux_and_ready", AsyncMock())
            m.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0.01)
            target, builder = session, "_build_repl_env"
        real_builder = getattr(target, builder)

        def explicit_payload(**kwargs):
            env = real_builder(**kwargs)
            # Exercise explicit payload delivery without implementing registry
            # grants here. PYTHONPATH binds the real shim to this test checkout.
            env["EXPLICIT_ALLOWED_NAME"] = "synthetic-allowed"
            if empty_token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = ""
            env["PYTHONPATH"] = self.seed["PYTHONPATH"]
            if forbidden:
                env[forbidden] = SYNTHETIC_VALUES[forbidden]
            return env

        m.setattr(target, builder, explicit_payload)
        if kind == "app_server":
            self.client, _ = await self.supervisor.start()
        else:
            await session._spawn_tmux_repl()
        for _ in range(300):
            if self.names_path.exists():
                names = json.loads(self.names_path.read_text())
                assert isinstance(names, list) and all(isinstance(n, str) for n in names)
                return set(names)
            await asyncio.sleep(0.01)
        raise AssertionError("child did not produce a names-only environment report")

    async def close(self):
        if self.client is not None:
            await self.client.close()
        if self.supervisor is not None:
            await self.supervisor.teardown()
        subprocess.run([self.tmux, "-S", str(self.socket), "kill-server"],
                       env=self.seed, capture_output=True, timeout=5)
        shutil.rmtree(self.socket_root)


@asynccontextmanager
async def launch_probe(root, monkeypatch, *, mode=None):
    probe = LaunchProbe(root, monkeypatch)
    try:
        if mode is not None:
            monkeypatch.setenv("PINKY_ISOLATED_ENV", mode)
        if mode == "enforce":
            grants = root / "grants.json"
            grants.write_text("{}")
            grants.chmod(0o600)
            monkeypatch.setenv("PINKY_ISOLATED_ENV_GRANTS_FILE", str(grants))
        yield probe
    finally:
        await probe.close()
