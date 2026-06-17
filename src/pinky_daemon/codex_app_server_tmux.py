"""tmux supervisor for the Design-A ``codex app-server`` shim (#791).

The native ``codex app-server --listen unix://`` + ``proxy`` transport is
silent (experimental, never responds — verified against codex-cli 0.125.0), so
the daemon talks to a plain stdio ``codex app-server`` fronted by our own
byte-transparent UDS bridge (``codex_app_server_shim``), and we run that bridge
under tmux. tmux gives the same live-substrate / inspectable-pane win the
claude REPL already gets (#98 family), and decouples the codex child's lifetime
from the daemon process.

What this supervisor owns:

  * spawn the shim in a detached tmux session ``pinky-codex-as-<agent>``,
    passing the full daemon env via tmux ``-e`` (tmux drops parent env, so it
    must be injected explicitly — parity with the subprocess path's
    ``{**os.environ}`` so CODEX_HOME/HOME/XDG/proxy/cert all reach the
    grandchild ``codex`` the shim spawns, not just the shim; see _build_env)
  * idempotent pre-start cleanup: kill a stale tmux session + unlink a stale
    socket so a crashed predecessor can't wedge bind()
  * readiness = socket ACCEPT only. We open the one allowed connection and
    HAND IT to the client — we do NOT throw a probe ``initialize`` at it, which
    would burn the child's single-use ``initialize`` (per-process; a 2nd one
    returns -32600). The real end-to-end gate stays the existing single
    ``.initialize()`` in CodexSession._ensure_app_server; a dead child shows up
    there as an EOF/timeout and terminalizes the turn.

The returned proc adapter (:class:`_TmuxAppServerProc`) quacks like
``asyncio.subprocess.Process`` so CodexSession's teardown paths
(``.returncode`` / ``.pid`` / ``.kill()`` / ``.wait()``) map onto tmux teardown
with no changes to those call sites.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import sys
import tempfile
from collections.abc import Callable

from pinky_daemon.codex_app_server import (
    _STREAM_LIMIT,
    CodexAppServerClient,
    NotificationHandler,
    ServerRequestHandler,
)
from pinky_daemon.command_runner import LocalCommandRunner
from pinky_daemon.streaming_session import _log
from pinky_daemon.tmux_session import _TmuxControl

# Generous: covers tmux new-session + python import + shim bind. The shim binds
# its socket before any slow work, so accept usually lands in well under a
# second; 30s only defends a pathological cold start.
_READINESS_TIMEOUT = 30.0
_READINESS_POLL = 0.1


class _TmuxAppServerProc:
    """Process-shaped adapter over a tmux-hosted shim.

    CodexSession treats the app-server "process" as an
    ``asyncio.subprocess.Process``: it reads ``.returncode`` (the reuse check in
    _ensure_app_server, and the guard in _teardown_app_server) and ``.pid`` (a
    log line), and calls ``.kill()`` then ``await .wait()`` to tear it down.
    Here the real process tree lives inside a tmux session, so those map onto
    ``CodexAppServerSupervisor.teardown()``.

    ``returncode`` stays ``None`` until we tear down. That matches a live,
    not-yet-reaped subprocess: if the codex child crashes under us, returncode
    is still None and the connection is reused once, the next request fails, and
    _exec_codex_app_server's handlers call _teardown_app_server -> respawn. We
    can't poll tmux liveness from a *sync* property, and proactively reflecting
    it would buy only one saved failed turn, so we keep the simple invariant.
    """

    def __init__(self, supervisor: CodexAppServerSupervisor, *, pid: int) -> None:
        self._sup = supervisor
        self.pid = pid
        self.returncode: int | None = None

    def kill(self) -> None:
        # Mirror Process.kill(): request, don't block. The real async teardown
        # (kill tmux session + unlink socket) is awaited in wait(), which
        # _teardown_app_server always calls immediately after kill().
        self._sup.request_kill()

    async def wait(self) -> int:
        await self._sup.teardown()
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


class CodexAppServerSupervisor:
    """Owns the tmux session + socket lifecycle for one agent's app-server."""

    def __init__(
        self,
        agent_name: str,
        *,
        working_dir: str,
        openai_api_key: str = "",
        log: Callable[[str], None] = _log,
    ) -> None:
        self.agent_name = agent_name
        self._log = log
        self._openai_api_key = openai_api_key
        self._sock_dir = self._resolve_sock_dir(agent_name, working_dir)
        self.sock_path = os.path.join(self._sock_dir, "app.sock")
        self._tmux = _TmuxControl(self.session_name, command_runner=LocalCommandRunner())
        self._kill_requested = False

    @staticmethod
    def _resolve_sock_dir(agent_name: str, working_dir: str) -> str:
        """Socket dir inside the agent's working dir (keeps it within the agent
        boundary — forward-compatible with #149 isolation — and under our own
        perms). Absolute so bind()/connect() don't depend on cwd.

        macOS AF_UNIX paths cap near 104 chars; a long working_dir would
        otherwise surface as a 30s readiness timeout. If the in-boundary path is
        too long, fall back to a short, agent-stable ``/tmp`` runtime dir so the
        transport still works (the #149 isolated path will need an in-boundary
        socket regardless — tracked separately)."""
        in_boundary = os.path.abspath(os.path.join(working_dir or ".", ".codex-app-server"))
        if len(os.path.join(in_boundary, "app.sock")) <= 100:
            return in_boundary
        digest = hashlib.sha1(in_boundary.encode()).hexdigest()[:10]
        return os.path.join(tempfile.gettempdir(), f"pinky-codex-as-{digest}")

    @property
    def session_name(self) -> str:
        return f"pinky-codex-as-{self.agent_name}"

    async def start(
        self,
        *,
        notification_handler: NotificationHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> tuple[CodexAppServerClient, _TmuxAppServerProc]:
        """Launch the shim under tmux and return a started, UN-initialized
        client plus its process adapter. Raises on tmux failure or readiness
        timeout. The caller (CodexSession) performs the single ``initialize``."""
        self._kill_requested = False

        # Idempotent pre-start cleanup: a crashed predecessor can leave a live
        # tmux session and/or a stale socket; either would wedge a fresh start.
        await self._tmux.kill_session()
        self._unlink_sock()

        os.makedirs(self._sock_dir, exist_ok=True)
        try:
            os.chmod(self._sock_dir, 0o700)  # fail-closed dir; shim 0600s the file
        except OSError:
            pass

        command = " ".join(
            shlex.quote(p)
            for p in [sys.executable, "-m", "pinky_daemon.codex_app_server_shim", self.sock_path]
        )
        result = await self._tmux.new_session(
            cwd=self._sock_dir, command=command, env=self._build_env()
        )
        if not result.ok:
            raise RuntimeError(
                f"codex app-server tmux new-session failed for {self.agent_name}: "
                f"{result.stderr.strip()}"
            )

        reader, writer = await self._await_accept()

        client = CodexAppServerClient(
            reader,
            writer,
            stderr=None,  # child stderr is the tmux pane, not a pipe we drain
            notification_handler=notification_handler,
            server_request_handler=server_request_handler,
            log=self._log,
        )
        client.start()
        self._log(
            f"codex[{self.agent_name}]: tmux app-server ready "
            f"(session={self.session_name} sock={self.sock_path})"
        )
        return client, _TmuxAppServerProc(self, pid=0)

    # tmux-internal vars that must not leak into a nested session's children.
    _ENV_DROP = frozenset({"TMUX", "TMUX_PANE"})

    def _build_env(self) -> dict[str, str]:
        """Full daemon-env parity for the tmux session — NOT just PATH.

        tmux ``new-session`` drops the parent process env (only ``-e KEY=VAL``
        survive), and the tmux *server's* env is not the daemon's. The
        direct-subprocess app-server path passes ``env={**os.environ}``, so the
        codex child sees the daemon's full config; we must reproduce that here or
        the child silently runs under different CODEX_HOME / HOME / XDG_* / proxy
        / cert / OPENAI_*/CODEX_* settings. That breaks auth and — critically —
        item G: a fresh child's ``thread/resume`` only finds the prior thread if
        it points at the SAME Codex home/session store (Murzik, #792 P1).

        We propagate the entire daemon env (overlaying the configured key for
        item H), minus tmux-internal vars and any value tmux ``-e`` can't carry
        (newlines), which are pathological for env anyway.
        """
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            if key in self._ENV_DROP:
                continue
            if "\n" in value or "\r" in value:
                self._log(
                    f"codex[{self.agent_name}]: dropping multiline env {key!r} "
                    f"(cannot pass via tmux -e)"
                )
                continue
            env[key] = value
        if self._openai_api_key:
            env["OPENAI_API_KEY"] = self._openai_api_key
        return env

    async def _await_accept(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Readiness probe: poll until the shim's socket ACCEPTS our connection.

        The first successful connect IS the daemon's one allowed connection — we
        return it, never a throwaway (the shim rejects a 2nd connection, and a
        throwaway initialize would consume the child's single-use one).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _READINESS_TIMEOUT
        last_err: Exception | None = None
        while loop.time() < deadline:
            try:
                return await asyncio.open_unix_connection(self.sock_path, limit=_STREAM_LIMIT)
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last_err = exc
                await asyncio.sleep(_READINESS_POLL)
        raise TimeoutError(
            f"codex app-server shim for {self.agent_name} did not accept on "
            f"{self.sock_path} within {_READINESS_TIMEOUT}s (last: {last_err})"
        )

    def request_kill(self) -> None:
        self._kill_requested = True

    async def teardown(self) -> None:
        """Idempotent: kill the tmux session and unlink the socket."""
        try:
            await self._tmux.kill_session()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            self._log(f"codex[{self.agent_name}]: tmux app-server kill_session failed: {exc}")
        self._unlink_sock()

    def stats(self) -> dict:
        return {
            "app_server_mode": "tmux",
            "tmux_session": self.session_name,
            "sock_path": self.sock_path,
        }

    def _unlink_sock(self) -> None:
        try:
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
        except OSError:
            pass
