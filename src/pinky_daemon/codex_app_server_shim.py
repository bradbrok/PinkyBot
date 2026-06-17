"""Design-A UDS<->stdio bridge for tmux-backed ``codex app-server`` (#791).

``codex app-server`` only speaks JSON-RPC over **stdio** — its experimental
``--listen unix://`` + ``proxy`` path accepts the connection but never
responds (verified silent against codex-cli 0.125.0, ×2 with Murzik). So we
front a plain stdio ``codex app-server`` child with our own ~70-line Unix-
socket bridge and run that under tmux, giving the same live-substrate /
per-turn-warmth win the claude REPL gets (#98) without the dead native path.

**Why the child can't be warm across reconnects.** ``initialize`` is
per-CHILD-PROCESS: a 2nd ``initialize`` on the same child returns
``-32600 "Already initialized"`` (Murzik). So this bridge does NOT keep the
child alive across daemon-client reconnects. The child's lifetime is COUPLED
to the single daemon connection:

  * spawn one plain ``codex app-server`` child (the proven stdio mode)
  * bind our UDS, accept exactly ONE daemon connection
  * byte-bridge both ways — the child stays warm ACROSS TURNS within that one
    connection (the #98 win), torn down only when the connection ends
  * client EOF (disconnect / force_restart / idle_sleep) -> kill child, exit
  * child stdout EOF (child died) -> drop the client socket, kill child, exit
  * a 2nd concurrent connection is rejected (no multiplexing onto one stdio)
  * NO in-shim respawn: the daemon's CodexAppServerSupervisor owns recovery

The bridge is **byte-transparent**: it ``read()``s fixed chunks and forwards
them verbatim, never parsing lines. That sidesteps the client reader's 10 MiB
NDJSON frame limit entirely — frame boundaries are the daemon's concern, not
the bridge's. The child's stderr inherits this process's stderr -> the tmux
pane/log; the child's stdout is reserved for JSON frames and is never tee'd to
the pane.

Usage: ``python -m pinky_daemon.codex_app_server_shim <sock_path>``
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys

# The stdio child this bridge fronts. Overridable via env for tests (point it
# at a fake JSON echo server — CI has no ``codex``) and as an operator escape
# hatch (e.g. an absolute codex path). Default is the proven stdio invocation.
_DEFAULT_CHILD_CMD = ("codex", "app-server")
# Verbatim forwarding chunk. Decoupled from any JSON frame size — the bridge
# never parses, so a frame may span many chunks with no special handling.
_READ_CHUNK = 65536
# Match codex_app_server._STREAM_LIMIT so the child-side pipe can hold a large
# tool-result frame without the subprocess transport raising LimitOverrunError.
_CHILD_STREAM_LIMIT = 10 * 1024 * 1024


def _child_cmd() -> list[str]:
    override = os.environ.get("PINKY_CODEX_APP_SERVER_CMD", "").strip()
    return shlex.split(override) if override else list(_DEFAULT_CHILD_CMD)


async def _run(sock_path: str) -> int:
    sock_dir = os.path.dirname(sock_path)
    if sock_dir:
        os.makedirs(sock_dir, exist_ok=True)
    # Stale-socket unlink: a prior shim that was SIGKILLed (no clean exit) can
    # leave the socket file behind; bind() would then fail with EADDRINUSE.
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    child = await asyncio.create_subprocess_exec(
        *_child_cmd(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # inherit -> tmux pane/log; never tee'd into the JSON stream
        limit=_CHILD_STREAM_LIMIT,
    )

    state = {"connected": False}

    def _teardown_and_exit(reason: str) -> None:
        """Synchronous, terminal teardown — the only exit path.

        Run inline the instant a bridge ends; we deliberately do NOT bounce
        back through ``await``/the loop teardown. Two asyncio hazards on this
        Python/macOS made the graceful path unreliable: ``await child.wait()``
        after ``child.kill()`` intermittently stalls, and the listening-server
        + subprocess-transport combo wedges ``asyncio.run``'s unwind for 20s+.
        This process exists only to serve ONE connection's lifetime, so once
        the child is signalled and the socket removed there is nothing to
        preserve — we ``os._exit``. SIGKILL is uncatchable so the child is
        dead-on-arrival; on our exit it reparents to launchd/init which reaps
        the zombie (no orphan). Under tmux this ends the pane command -> the
        session closes; the supervisor's kill_session is idempotent anyway.
        """
        print(f"shim: bridge ended ({reason}); tearing down child + socket", flush=True)
        try:
            if child.returncode is None:
                os.kill(child.pid, signal.SIGKILL)
        except (ProcessLookupError, Exception):  # noqa: BLE001 — best-effort
            pass
        try:
            if os.path.exists(sock_path):
                os.unlink(sock_path)
        except OSError:
            pass
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if state["connected"]:
            # Single active client: never interleave two daemons onto one
            # stdio child (responses are correlated by id within one peer).
            print("shim: rejecting concurrent connection", flush=True)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        state["connected"] = True
        print("shim: daemon connected", flush=True)

        async def c2s() -> str:
            while True:
                data = await reader.read(_READ_CHUNK)
                if not data:
                    return "client_eof"
                child.stdin.write(data)
                await child.stdin.drain()

        async def s2c() -> str:
            while True:
                data = await child.stdout.read(_READ_CHUNK)
                if not data:
                    return "child_eof"
                writer.write(data)
                await writer.drain()

        c = asyncio.create_task(c2s())
        s = asyncio.create_task(s2c())
        try:
            done, _pending = await asyncio.wait({c, s}, return_when=asyncio.FIRST_COMPLETED)
            reason = next(iter(done)).result()
        except Exception as exc:  # noqa: BLE001 — any bridge fault ends the connection
            reason = f"bridge_error:{exc}"
        # Terminal: hard-exit inline. No task-cancel / writer-close dance needed
        # — os._exit drops every fd and task at once.
        _teardown_and_exit(reason)

    server = await asyncio.start_unix_server(handle, path=sock_path)
    # Fail-closed perms: only the owning uid may connect. The supervisor also
    # 0700s the parent dir. (#149-isolated agents run the shim under their own
    # uid and the daemon connects as a different uid — that path needs the
    # socket inside the agent boundary; see the supervisor TODO. Increment-1 is
    # non-isolated, so daemon-uid == shim-uid and 0600 is exactly right.)
    try:
        os.chmod(sock_path, 0o600)
    except OSError:
        pass
    print(f"shim: listening on {sock_path} child_pid={child.pid}", flush=True)

    # Also exit if the child dies before any daemon ever connects (e.g. a bad
    # OPENAI_API_KEY makes app-server exit immediately): the supervisor's
    # accept-readiness would otherwise wait on a socket whose child is gone.
    async def _watch_child_preconnect() -> None:
        await child.wait()
        if not state["connected"]:
            _teardown_and_exit("child_exit_preconnect")

    asyncio.create_task(_watch_child_preconnect())

    # Live until a bridge (or the pre-connect watcher) hard-exits the process.
    async with server:
        await asyncio.Event().wait()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python -m pinky_daemon.codex_app_server_shim <sock_path>",
            flush=True,
        )
        return 2
    return asyncio.run(_run(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
