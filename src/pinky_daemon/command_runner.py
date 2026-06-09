"""CommandRunner — the subprocess-execution seam for #149 phase-3.

A ``CommandRunner`` runs an argv and returns its result. It exists so the
*identity* a subprocess runs under can be swapped without touching the call
sites that build the command:

  - :class:`LocalCommandRunner` runs argv verbatim under the daemon's own
    OS user. This is the default everywhere and reproduces the exact
    pre-#149-phase-3 behavior (a plain ``asyncio.create_subprocess_exec``).
  - :class:`RunuserCommandRunner` runs argv as another OS user via
    ``runuser -u <user> --`` (Linux only). This is how an ``isolation_mode
    ="unix_user"`` tenant's tmux server + REPL get launched under their own
    ``pinky-<agent>`` uid, so the agent's process cannot read the daemon's
    files or other tenants' homes.
  - :class:`ContainerCommandRunner` runs argv INSIDE an agent's container via
    ``podman exec <container> --`` (rootless Podman). This is how an
    ``isolation_mode="container"`` tenant's tmux server + REPL run inside their
    own container — own filesystem, own CLIs, own credentials — while the
    daemon drives them from the host. Same wrap-and-delegate shape as runuser.

The seam lives behind ``_TmuxControl`` (tmux_session.py): that class builds
``tmux …`` argvs and delegates the actual exec to its injected runner. A
local agent gets a LocalCommandRunner (no change); a unix_user agent gets a
RunuserCommandRunner bound to its OS user. The provisioner that creates that
OS user, and the systemd supervision unit, land in inc3c — this module is
only the execution half and is fully testable on macOS via the argv-wrapping
contract (no Linux/root needed to assert command shapes).

Design note — why timeout+kill lives in the runner: callers want one
consistent "run argv, bound the wait, kill on timeout" primitive regardless
of which identity it runs under. Centralizing it here means the runuser
wrapper inherits the exact same timeout semantics as the local path for free.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CommandResult:
    """Outcome of one subprocess invocation. ``stdout``/``stderr`` are raw
    bytes — the caller decides how to decode (``_TmuxControl`` decodes utf-8
    with ``errors='replace'``)."""

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(ABC):
    """Runs an argv under some OS identity and returns its result."""

    @abstractmethod
    async def run(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        """Execute ``argv``, optionally feeding ``stdin``.

        ``timeout`` (seconds) bounds the wait; on expiry the child is killed
        and ``asyncio.TimeoutError`` propagates, matching the prior inline
        behavior in ``_TmuxControl._run``.
        """


class LocalCommandRunner(CommandRunner):
    """Run argv verbatim under the daemon's own user — the default.

    Byte-for-byte the behavior ``_TmuxControl._run`` had inline before the
    seam existed: ``create_subprocess_exec`` with piped stdout/stderr, a
    bounded wait, and a kill-on-timeout that re-raises ``TimeoutError``.
    """

    async def run(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        return CommandResult(
            returncode=proc.returncode or 0,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )


class RunuserCommandRunner(CommandRunner):
    """Run argv as another OS user via ``runuser`` (Linux only).

    ``runuser -u <user> -- <argv...>`` switches uid/gid to ``username``
    without a PAM session or password prompt (it requires the caller to be
    root, which the daemon is when managing unix_user tenants). The actual
    exec is delegated to an inner runner — :class:`LocalCommandRunner` by
    default — so timeout/kill semantics are shared with the local path and
    tests can inject a recording double.

    The ``--`` terminator is mandatory: it stops ``runuser`` from
    interpreting any of the wrapped argv (e.g. a leading ``-x``) as its own
    option. The wrapped argv is passed as separate args, never shell-joined,
    so there is no quoting/injection surface.
    """

    def __init__(
        self,
        username: str,
        *,
        runuser_binary: str = "runuser",
        inner: CommandRunner | None = None,
    ) -> None:
        if not username:
            raise ValueError("RunuserCommandRunner requires a non-empty username")
        self.username = username
        self.runuser_binary = runuser_binary
        self._inner = inner or LocalCommandRunner()

    def wrap(self, argv: list[str]) -> list[str]:
        """Return ``argv`` prefixed with the runuser invocation."""
        return [self.runuser_binary, "-u", self.username, "--", *argv]

    async def run(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        return await self._inner.run(self.wrap(argv), timeout=timeout, stdin=stdin)


class ContainerCommandRunner(CommandRunner):
    """Run argv INSIDE an agent's container via ``podman exec`` (or docker).

    This is how an ``isolation_mode="container"`` tenant's tmux server + claude
    REPL get driven: the daemon stays on the host, and every tmux control
    command (``new-session``, ``send-keys``, ``capture-pane`` …) is exec'd into
    the agent's already-running container. So the whole interactive session —
    tmux, claude, and whatever CLIs the operator's image provides — lives inside
    the container, while the daemon orchestrates it from outside. Exactly the
    shape of :class:`RunuserCommandRunner`, with ``podman exec <container>``
    standing in for ``runuser -u <user>``.

    The container must already be running (the ``ContainerProvisioner`` creates
    it; the activation increment starts it on session connect). The actual exec
    is delegated to an inner runner — :class:`LocalCommandRunner` by default —
    so timeout/kill semantics are shared with the local path and tests can
    inject a recording double.

    The ``--`` terminator after the flags is deliberate: it stops the container
    CLI from interpreting the container name or any wrapped arg (e.g. a leading
    ``-l`` on ``tmux send-keys``) as one of its own options. The wrapped argv is
    passed as separate args, never shell-joined, so there is no quoting surface.
    """

    def __init__(
        self,
        container: str,
        *,
        user: str | None = None,
        workdir: str | None = None,
        container_binary: str = "podman",
        inner: CommandRunner | None = None,
    ) -> None:
        if not container:
            raise ValueError("ContainerCommandRunner requires a non-empty container")
        self.container = container
        self.user = user
        self.workdir = workdir
        self.container_binary = container_binary
        self._inner = inner or LocalCommandRunner()

    def wrap(self, argv: list[str]) -> list[str]:
        """Return ``argv`` prefixed with the ``podman exec`` invocation."""
        cmd = [self.container_binary, "exec"]
        if self.user is not None:
            cmd += ["-u", self.user]
        if self.workdir is not None:
            cmd += ["-w", self.workdir]
        cmd += ["--", self.container, *argv]
        return cmd

    async def run(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        return await self._inner.run(self.wrap(argv), timeout=timeout, stdin=stdin)
