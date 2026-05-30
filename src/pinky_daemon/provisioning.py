"""Agent OS-level provisioning — the runtime-sandbox seam for #149 phase 3.

An *isolated* agent (the daemon-authz ``isolated`` flag, #635) is denied
cross-agent actions inside the daemon. That is the authorization half of
isolation. This module is the *runtime* half: how an isolated tenant's
process is actually sandboxed at the operating-system level.

``isolation_mode`` on the Agent selects the strategy:

  - ``"local"``     — in-process under the daemon's own OS user. No OS
                      sandbox; the only boundary is the daemon-authz
                      ``isolated`` flag. This is the default and the
                      current (pre-#149-phase-3) behavior for every agent.
  - ``"unix_user"`` — the agent gets its own ``pinky-<agent>`` OS user with
                      a private home, working dir, signing key, and MCP
                      config, and its runtime runs under that uid. EXEC of
                      this path is Linux/systemd-only. inc3a shipped the
                      interface; inc3c (this) ships the real
                      :class:`UnixUserProvisioner` + the systemd unit
                      template — but **dormant**: ``get_provisioner`` still
                      fails closed for ``unix_user`` (see below).

A ``AgentProvisioner`` owns the lifecycle of those OS resources
(provision / deprovision / introspect) and contributes any extra process
environment the runtime needs (``runtime_env``). The companion seam — how
the runtime *command* is launched under the right uid — lives behind the
CommandRunner in ``tmux_session`` (the ``RunuserCommandRunner``, inc3b); a
provisioner never spawns the agent itself, it only prepares the ground.

Design constraints (why this exists now, before activation):
  - Additive and inert: nothing in the daemon lifecycle constructs a
    ``UnixUserProvisioner`` in inc3c. Wiring provisioning into
    registration/start/retire — and flipping ``get_provisioner`` to hand one
    back — is a later "atomic activation" increment that lands the lifecycle
    call sites and the ``RunuserCommandRunner`` injection *together*.
  - Fail-closed on the un-activated path: ``get_provisioner("unix_user")``
    raises rather than silently degrading to local. This is also the
    operational dormancy guarantee — the #642 respawn guard
    (``_isolation_block_reason`` in api.py) blocks any ``unix_user``-labeled
    agent from (re)spawning precisely *because* this factory raises. Shipping
    the provisioner class without flipping the factory means a half-wired
    ``unix_user`` agent can never launch under the daemon uid with none of
    the requested isolation.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids a runtime import cycle
    from .agent_registry import Agent


# Canonical set of recognized isolation modes. Mirrors the validator on
# ``RegisterAgentRequest.isolation_mode`` (api_models.py) — duplicated here so
# in-process callers of ``get_provisioner`` get the same guarantee independent
# of the request layer.
LOCAL = "local"
UNIX_USER = "unix_user"
KNOWN_MODES = frozenset({LOCAL, UNIX_USER})

# OS-user naming + filesystem layout for unix_user tenants. The username is
# ``pinky-<agent>`` so every managed account shares a greppable prefix and can
# never collide with a human login. Home lives under ``UNIX_USER_HOME_ROOT``.
UNIX_USER_PREFIX = "pinky-"
UNIX_USER_HOME_ROOT = "/home"
# Login shell for managed accounts: nologin. These users exist to *own* files
# and run a supervised tmux/REPL via runuser; they are never meant to hold an
# interactive shell session, so deny one outright.
UNIX_USER_SHELL = "/usr/sbin/nologin"

# Permission bits (octal). Private dirs are 0700, secrets 0600 — owner-only,
# so even another managed tenant on the same host can't read across.
DIR_MODE = 0o700
SECRET_MODE = 0o600


@dataclass
class ProvisionResult:
    """Outcome of a provision / deprovision call.

    ``created``/``removed`` enumerate the OS resources touched, in the order
    they were touched, so the UnixUserProvisioner can drive partial-failure
    rollback (undo in reverse) and so callers can audit exactly what changed.
    For the no-op LocalProvisioner both lists are always empty.

    Resource tokens are ``"user:<name>"`` or ``"path:<abs>"`` so rollback can
    dispatch on kind without re-deriving the layout.
    """

    ok: bool = True
    mode: str = LOCAL
    created: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    message: str = ""


class AgentProvisioner(ABC):
    """Owns the OS-level resources backing one isolation strategy.

    Implementations must be **idempotent**: ``provision`` on an already-
    provisioned agent is a successful no-op, and ``deprovision`` on an
    already-clean agent is likewise. This keeps daemon restarts and retry
    loops safe — the daemon may call these on every startup.
    """

    #: Stable identifier matching the agent's ``isolation_mode`` value.
    mode: str = LOCAL

    @abstractmethod
    def provision(self, agent: "Agent") -> ProvisionResult:
        """Idempotently create the OS resources this agent needs to run."""

    @abstractmethod
    def deprovision(self, agent: "Agent") -> ProvisionResult:
        """Idempotently tear down OS resources created by ``provision``."""

    @abstractmethod
    def is_provisioned(self, agent: "Agent") -> bool:
        """True iff the agent's OS resources currently exist."""

    @abstractmethod
    def runtime_env(self, agent: "Agent") -> dict[str, str]:
        """Extra process env to merge into the agent's runtime (e.g. HOME)."""


class LocalProvisioner(AgentProvisioner):
    """Default strategy: no OS sandbox — runs under the daemon's user.

    Every method is an intentional no-op. This is the behavior every agent
    has today; ``isolation_mode="local"`` simply names it explicitly. An
    agent is always considered "provisioned" because there is nothing to set
    up, and it contributes no extra environment.
    """

    mode = LOCAL

    def provision(self, agent: "Agent") -> ProvisionResult:
        return ProvisionResult(ok=True, mode=LOCAL, message="local: no OS resources to provision")

    def deprovision(self, agent: "Agent") -> ProvisionResult:
        return ProvisionResult(ok=True, mode=LOCAL, message="local: no OS resources to deprovision")

    def is_provisioned(self, agent: "Agent") -> bool:
        return True

    def runtime_env(self, agent: "Agent") -> dict[str, str]:
        return {}


# --------------------------------------------------------------------------- #
# unix_user provisioning (#149 inc3c)
# --------------------------------------------------------------------------- #


class ProvisionError(RuntimeError):
    """A privileged provisioning step failed. Carries the resources created so
    far so the caller can audit what rollback had to undo."""

    def __init__(self, message: str, *, created: list[str] | None = None) -> None:
        super().__init__(message)
        self.created = created or []


@dataclass(frozen=True)
class UnixUserPaths:
    """Resolved filesystem layout for one ``pinky-<agent>`` tenant.

    Centralizing path derivation keeps provision/deprovision/is_provisioned/
    runtime_env in agreement and gives tests one place to assert the layout.
    Everything lives under ``home`` so teardown is a single ``userdel --remove``.
    """

    username: str
    home: str
    workdir: str
    data_dir: str
    config_dir: str
    keystore: str
    mcp_json: str

    @classmethod
    def for_agent(
        cls,
        agent_name: str,
        *,
        home_root: str = UNIX_USER_HOME_ROOT,
        prefix: str = UNIX_USER_PREFIX,
    ) -> "UnixUserPaths":
        username = f"{prefix}{agent_name}"
        home = str(Path(home_root) / username)
        data_dir = str(Path(home) / "data")
        return cls(
            username=username,
            home=home,
            workdir=str(Path(home) / "workdir"),
            data_dir=data_dir,
            config_dir=str(Path(home) / ".claude"),
            # Single-agent signing-key store — see UnixUserProvisioner docstring.
            keystore=str(Path(data_dir) / "agent_keys.db"),
            mcp_json=str(Path(home) / "workdir" / ".mcp.json"),
        )


@runtime_checkable
class ProvisionOps(Protocol):
    """The privileged-operations seam.

    Every OS mutation a provisioner performs goes through this protocol so the
    whole provisioner is testable on macOS (and without root) by injecting a
    recording double — no real ``useradd``/``chown`` ever runs in a unit test.
    ``run`` is the single command channel (matches the CommandRunner seam's
    "assert command shapes" review contract); secret *content* gets its own
    methods because secrets must never be passed on an argv (process-list leak).
    """

    def run(self, argv: list[str]) -> None:
        """Run a privileged command; raise on non-zero exit."""

    def user_exists(self, username: str) -> bool: ...

    def path_exists(self, path: str) -> bool: ...

    def write_secret_file(self, path: str, content: str) -> None:
        """Create ``path`` with 0600 perms and write ``content`` (no argv leak)."""

    def write_keystore(self, path: str, agent_name: str, signing_key: str) -> None:
        """Create a single-row ``agent_signing_keys`` SQLite at ``path`` (0600)."""


class SystemProvisionOps:
    """Real :class:`ProvisionOps` — performs OS mutations via subprocess + os.

    Runs only on the Linux host that actually owns the tenants (the daemon is
    root there). Never exercised in unit tests; correctness here is by
    inspection, while the provisioner *logic* is covered via a recording double.
    """

    def run(self, argv: list[str]) -> None:
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise ProvisionError(
                f"command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stderr.strip()}"
            )

    def user_exists(self, username: str) -> bool:
        # getent returns 0 iff the user exists; absence is exit 2, not an error.
        return subprocess.run(["getent", "passwd", username], capture_output=True).returncode == 0

    def path_exists(self, path: str) -> bool:
        return os.path.exists(path)

    def write_secret_file(self, path: str, content: str) -> None:
        # Open with O_CREAT|O_EXCL|O_WRONLY at 0600 so the secret is never world-
        # readable even for the instant between create and chmod.
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, SECRET_MODE)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, SECRET_MODE)

    def write_keystore(self, path: str, agent_name: str, signing_key: str) -> None:
        # Create the file 0600 BEFORE sqlite opens it, so the DB (which holds a
        # signing secret) is never briefly group/world-readable.
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, SECRET_MODE)
        os.close(fd)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_signing_keys ("
                "agent_name TEXT PRIMARY KEY, signing_key TEXT NOT NULL, created_at REAL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO agent_signing_keys "
                "(agent_name, signing_key, created_at) VALUES (?, ?, ?)",
                (agent_name, signing_key, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(path, SECRET_MODE)


class UnixUserProvisioner(AgentProvisioner):
    """Provision a dedicated ``pinky-<agent>`` OS user + private home (#149 inc3c).

    Creates, for ``isolation_mode="unix_user"`` tenants on Linux:

      - an OS account ``pinky-<agent>`` (nologin shell, own home),
      - private home / workdir / data / ``.claude`` config dirs at 0700,
      - a **single-agent signing-key store** — a one-row ``agent_signing_keys``
        SQLite at 0600. This is the crux of the isolation: the tenant's
        request-time key resolver (``auth.make_db_signing_key_resolver``) is
        pointed at *this* DB via ``PINKY_AGENTS_DB`` in :meth:`runtime_env`, not
        the fleet ``conversations_agents.db`` which holds **every** agent's key.
        Same resolver seam, single-agent source — exactly the swap the #644
        resolver docstring reserves for inc3c.
      - a 0600 ``.mcp.json``.

    All resources live under the home, so teardown is one ``userdel --remove``.
    Every mutation goes through an injected :class:`ProvisionOps`, defaulting to
    :class:`SystemProvisionOps`; tests inject a recording double.

    **Idempotent**: a re-provision of an already-set-up agent is a successful
    no-op. **Rollback**: if any step fails mid-provision, every resource created
    so far is torn down in reverse before the error propagates — no half-built
    account is ever left behind.

    Note this class is *not* yet handed out by :func:`get_provisioner` (it stays
    fail-closed; see module docstring). Construct it directly. Activation —
    wiring it into the daemon lifecycle alongside the ``RunuserCommandRunner`` —
    is a later increment.
    """

    mode = UNIX_USER

    def __init__(
        self,
        *,
        ops: ProvisionOps | None = None,
        signing_key_provider: Callable[[str], str] | None = None,
        mcp_json_provider: Callable[["Agent", UnixUserPaths], str] | None = None,
        home_root: str = UNIX_USER_HOME_ROOT,
        prefix: str = UNIX_USER_PREFIX,
        shell: str = UNIX_USER_SHELL,
    ) -> None:
        self._ops: ProvisionOps = ops or SystemProvisionOps()
        # Where the single-agent key comes from. The daemon passes
        # registry.get_or_create_signing_key so the value matches what the
        # fleet authority issued; default raises to prevent provisioning a
        # tenant with no usable key by accident.
        self._signing_key_provider = signing_key_provider or _no_signing_key
        self._mcp_json_provider = mcp_json_provider or _default_mcp_json
        self._home_root = home_root
        self._prefix = prefix
        self._shell = shell

    def paths(self, agent: "Agent") -> UnixUserPaths:
        return UnixUserPaths.for_agent(
            agent.name, home_root=self._home_root, prefix=self._prefix
        )

    def is_provisioned(self, agent: "Agent") -> bool:
        # "Ready" means EVERY resource the runtime depends on exists — the
        # account, all private dirs, the keystore, AND .mcp.json. A weaker
        # check (e.g. user+keystore only) would let provision() early-return on
        # a half-built tenant and hand activation a missing WorkingDirectory /
        # CLAUDE_CONFIG_DIR / .mcp.json. (@murzik #646)
        p = self.paths(agent)
        return (
            self._ops.user_exists(p.username)
            and all(
                self._ops.path_exists(d) for d in (p.workdir, p.data_dir, p.config_dir)
            )
            and self._ops.path_exists(p.keystore)
            and self._ops.path_exists(p.mcp_json)
        )

    def provision(self, agent: "Agent") -> ProvisionResult:
        p = self.paths(agent)
        if self.is_provisioned(agent):
            return ProvisionResult(
                ok=True, mode=UNIX_USER, message=f"unix_user: {p.username} already provisioned"
            )

        # Reconcile, not all-or-nothing: each step is skipped if its resource
        # already exists, so provision() repairs a partial tenant (builds only
        # the missing pieces). ``created`` tracks ONLY what THIS call made, so
        # rollback never tears down a pre-existing resource. (@murzik #646)
        created: list[str] = []
        try:
            # 1. The OS account (also creates its home dir).
            if not self._ops.user_exists(p.username):
                self._ops.run([
                    "useradd", "--create-home", "--home-dir", p.home,
                    "--shell", self._shell, p.username,
                ])
                created.append(f"user:{p.username}")
            # Always (re)assert home perms — useradd's default umask leaves 0755,
            # and self-healing a drifted-perms home is cheap and idempotent.
            self._chmod_chown(p.home, p.username, DIR_MODE)

            # 2. Private working dirs, each 0700 and agent-owned.
            for d in (p.workdir, p.data_dir, p.config_dir):
                if not self._ops.path_exists(d):
                    self._ops.run(["mkdir", "-p", d])
                    created.append(f"path:{d}")
                self._chmod_chown(d, p.username, DIR_MODE)

            # 3. Single-agent signing-key store (0600). NOT the fleet DB.
            if not self._ops.path_exists(p.keystore):
                key = self._signing_key_provider(agent.name)
                if not key:
                    raise ProvisionError(f"no signing key available for {agent.name!r}")
                self._ops.write_keystore(p.keystore, agent.name, key)
                self._ops.run(["chown", f"{p.username}:{p.username}", p.keystore])
                created.append(f"path:{p.keystore}")

            # 4. The agent's .mcp.json (0600).
            if not self._ops.path_exists(p.mcp_json):
                self._ops.write_secret_file(p.mcp_json, self._mcp_json_provider(agent, p))
                self._ops.run(["chown", f"{p.username}:{p.username}", p.mcp_json])
                created.append(f"path:{p.mcp_json}")
        except Exception as e:
            # Partial-failure rollback: undo what we built, in reverse, then
            # surface the original failure with the rollback trail attached.
            removed = self._rollback(created)
            msg = f"unix_user provision of {p.username} failed: {e}"
            return ProvisionResult(
                ok=False, mode=UNIX_USER, created=created, removed=removed, message=msg
            )

        return ProvisionResult(
            ok=True, mode=UNIX_USER, created=created,
            message=f"unix_user: provisioned {p.username}",
        )

    def deprovision(self, agent: "Agent") -> ProvisionResult:
        p = self.paths(agent)
        if not self._ops.user_exists(p.username):
            return ProvisionResult(
                ok=True, mode=UNIX_USER, message=f"unix_user: {p.username} already absent"
            )
        # userdel --remove deletes the account AND its home subtree (workdir,
        # data, keystore, .mcp.json all live under it), so one call suffices.
        self._ops.run(["userdel", "--remove", p.username])
        return ProvisionResult(
            ok=True, mode=UNIX_USER, removed=[f"user:{p.username}"],
            message=f"unix_user: deprovisioned {p.username}",
        )

    def runtime_env(self, agent: "Agent") -> dict[str, str]:
        """Process env for the tenant's runtime.

        ``PINKY_AGENTS_DB`` points at the **single-agent** keystore, so the
        request-time resolver reads only this tenant's key and never the fleet
        DB. ``HOME``/``CLAUDE_CONFIG_DIR`` confine config + Claude trust to the
        private home.
        """
        p = self.paths(agent)
        return {
            "HOME": p.home,
            "CLAUDE_CONFIG_DIR": p.config_dir,
            "PINKY_AGENTS_DB": p.keystore,
            "PINKY_AGENT_USER": p.username,
        }

    # -- internals ---------------------------------------------------------- #

    def _chmod_chown(self, path: str, username: str, mode: int) -> None:
        self._ops.run(["chmod", _octal(mode), path])
        self._ops.run(["chown", f"{username}:{username}", path])

    def _rollback(self, created: list[str]) -> list[str]:
        """Undo ``created`` resources in reverse; best-effort, never raises."""
        removed: list[str] = []
        for token in reversed(created):
            kind, _, value = token.partition(":")
            try:
                if kind == "user":
                    if self._ops.user_exists(value):
                        self._ops.run(["userdel", "--remove", value])
                elif kind == "path":
                    if self._ops.path_exists(value):
                        self._ops.run(["rm", "-rf", value])
                removed.append(token)
            except Exception:
                # Swallow — rollback is best-effort; a stuck resource is logged
                # by the caller via the returned ProvisionResult, not raised.
                continue
        return removed


def _octal(mode: int) -> str:
    """Render a perms int as a 4-digit octal string for chmod (0o700 -> '0700')."""
    return format(mode, "04o")


def _no_signing_key(agent_name: str) -> str:
    raise ProvisionError(
        f"UnixUserProvisioner needs a signing_key_provider; none supplied for {agent_name!r}"
    )


def _default_mcp_json(agent: "Agent", paths: UnixUserPaths) -> str:
    """Minimal placeholder .mcp.json. The activation increment that wires real
    per-tenant MCP servers will supply a richer provider; this keeps the seam
    honest (a 0600 file the tenant owns) without pretending to be the final
    config."""
    import json

    return json.dumps({"mcpServers": {}}, indent=2)


def get_provisioner(isolation_mode: str) -> AgentProvisioner:
    """Return the provisioner for ``isolation_mode``.

    ``"local"`` → the no-op :class:`LocalProvisioner`. ``"unix_user"`` is a
    recognized mode and its provisioner (:class:`UnixUserProvisioner`) now
    exists, but this factory still **fails closed** for it: activation — wiring
    the provisioner into the daemon lifecycle together with the
    ``RunuserCommandRunner`` — is a later increment. Raising here is the
    dormancy guarantee: the #642 respawn guard blocks a ``unix_user`` agent
    from launching *because* this raises, so the half-wired path can never run
    under the daemon uid with none of the requested isolation. Any other value
    is rejected as unknown.
    """
    if isolation_mode == LOCAL:
        return LocalProvisioner()
    if isolation_mode == UNIX_USER:
        raise NotImplementedError(
            "isolation_mode='unix_user' is implemented (UnixUserProvisioner) but "
            "not yet activated: lifecycle wiring + RunuserCommandRunner injection "
            "land in a later #149 increment. get_provisioner stays fail-closed so "
            "the #642 respawn guard keeps blocking unix_user until then."
        )
    raise ValueError(
        f"unknown isolation_mode {isolation_mode!r}; expected one of {sorted(KNOWN_MODES)}"
    )
