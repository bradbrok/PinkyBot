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
  - ``"container"`` — the agent runs inside its own (rootless Podman)
                      container: own filesystem, own home VOLUME (persists CLI
                      OAuth state for a durable per-employee login), own
                      signing-key secret. Pinky owns isolation + lifecycle only
                      and is tool-agnostic — the image is operator-supplied
                      (bring-your-own; Pinky bakes in no CLIs). Ships the real
                      :class:`ContainerProvisioner` + ``ContainerCommandRunner``
                      but **dormant**: ``get_provisioner`` fails closed for
                      ``container`` until lifecycle activation. Strictly opt-in.

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
CONTAINER = "container"
KNOWN_MODES = frozenset({LOCAL, UNIX_USER, CONTAINER})

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
# Atomic-create flags for secret-bearing files: O_EXCL fails closed on a
# pre-existing file and O_NOFOLLOW never follows a planted symlink (same threat
# model as key_store / db_security / fs_security).
_SECRET_OPEN_FLAGS = (
    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
)

# Container naming + in-container layout for the ``container`` isolation mode.
# One container, one home volume, and one signing-key secret per agent, all
# sharing the greppable ``pinky-<agent>`` prefix. Runtime is Podman (rootless)
# by default; ``CONTAINER_BINARY`` is injectable for docker / CI doubles.
CONTAINER_PREFIX = "pinky-"
CONTAINER_BINARY = "podman"
# Opt-in runtime gate. Container isolation stays fail-closed (dormant) until an
# operator sets this on the daemon host — "" = OFF, "docker" = docker, any other
# truthy value ("podman"/"1"/...) = podman. Default OFF, so registering/labeling
# a container agent is allowed but it cannot RUN until the host is set up.
# NOTE: "docker" selects the docker CLI for exec paths, but get_provisioner
# rejects it for now (provisioning relies on podman-only secret delivery).
CONTAINER_RUNTIME_ENV = "PINKY_CONTAINER_RUNTIME"
# In-container HOME. Everything the tenant persists — including any CLI's OAuth
# state under ~/.config — lives here and is backed by the per-agent home VOLUME,
# so a tenant's logins survive container restart/rebuild. Pinky bakes NO tools
# into the image: which CLIs exist inside is entirely the operator's image.
CONTAINER_HOME = "/home/agent"
# Claude Code config dir for container agents, placed INSIDE the bind-mounted
# working_dir so it resolves to the SAME absolute path on host and in-container.
# This is what makes the host-side transcript tailer (response pipeline), the
# `--continue` prior-transcript check, and `claude login` credential durability
# work for container agents: transcripts/config/creds land on the host disk via
# the existing same-path workdir mount — no extra mount, no path translation.
# The home VOLUME still backs ~/.config etc. for any other CLIs in the image.
CONTAINER_CONFIG_DIRNAME = ".claude-container"
# Conservative default resource caps for container tenants (the Pi 5 shares
# 8GB with a POS stack). Operator-overridable per host; set to "0" to disable.
CONTAINER_MEMORY_ENV = "PINKY_CONTAINER_MEMORY"
CONTAINER_MEMORY_DEFAULT = "2g"
CONTAINER_PIDS_ENV = "PINKY_CONTAINER_PIDS_LIMIT"
CONTAINER_PIDS_DEFAULT = "2048"
# Fleet default bring-your-own image for one-click containerize: when an operator
# clicks "Containerize" without supplying an override, this image is used. Empty
# (default) means there is no fleet default — the operator must supply an image
# per agent, and preflight reports default_image=null. A canonical Pinky runtime
# image is a product follow-up; this env keeps v1 a controlled, opt-in rollout.
CONTAINER_DEFAULT_IMAGE_ENV = "PINKY_CONTAINER_DEFAULT_IMAGE"
# In-container tooling a containerized tenant MUST have on PATH to host a tmux +
# claude session. The bring-your-own image is operator-supplied, so a one-click
# containerize probes for these before persisting isolation_mode=container — a
# missing binary means the image can't run the agent and the flip is refused
# (rather than stranding the agent in BOOT_FAILED at next spawn). Mirrors the
# session-side contract check in tmux_session._check_container_image_contract.
CONTAINER_REQUIRED_BINARIES = ("tmux", "claude", "python3")


def container_config_dir(working_dir: str) -> str:
    """The CLAUDE_CONFIG_DIR used inside a container agent's runtime — a
    host-visible path inside the (same-path bind-mounted) working_dir."""
    return str(Path(working_dir) / CONTAINER_CONFIG_DIRNAME)


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
        # readable even for the instant between create and chmod, a pre-existing
        # file fails closed, and (O_NOFOLLOW) a planted symlink is never followed.
        fd = os.open(path, _SECRET_OPEN_FLAGS, SECRET_MODE)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, SECRET_MODE)

    def write_keystore(self, path: str, agent_name: str, signing_key: str) -> None:
        # Create the file 0600 BEFORE sqlite opens it, so the DB (which holds a
        # signing secret) is never briefly group/world-readable. O_EXCL+O_NOFOLLOW:
        # fail closed on a pre-existing file or planted symlink.
        fd = os.open(path, _SECRET_OPEN_FLAGS, SECRET_MODE)
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


# --------------------------------------------------------------------------- #
# container provisioning (#149 / container isolation_mode)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContainerNames:
    """Resolved Podman object names + in-container paths for one tenant.

    Centralizing derivation keeps provision / deprovision / is_provisioned /
    runtime_env in agreement and gives tests one place to assert the layout
    (mirrors :class:`UnixUserPaths`). One container, one home volume, one
    signing-key secret per agent, all under the ``pinky-<agent>`` prefix.
    """

    container: str
    volume: str
    secret: str
    home: str
    workdir: str
    config_dir: str

    @classmethod
    def for_agent(
        cls,
        agent_name: str,
        *,
        prefix: str = CONTAINER_PREFIX,
        home: str = CONTAINER_HOME,
    ) -> "ContainerNames":
        base = f"{prefix}{agent_name}"
        return cls(
            container=base,
            volume=f"{base}-home",
            secret=f"{base}-key",
            home=home,
            workdir=str(Path(home) / "workdir"),
            config_dir=str(Path(home) / ".claude"),
        )


@runtime_checkable
class ContainerOps(Protocol):
    """The privileged container-runtime seam (mirrors :class:`ProvisionOps`).

    Every Podman mutation a :class:`ContainerProvisioner` performs goes through
    this protocol so the whole provisioner is testable on macOS — and without a
    real container runtime — by injecting a recording double. ``run`` is the
    single command channel (matches the CommandRunner seam's "assert command
    shapes" contract); the signing-key secret gets its own ``write_secret``
    because its *content* must never be passed on an argv (process-list leak).
    """

    def run(self, argv: list[str]) -> None:
        """Run a podman command; raise on non-zero exit."""

    def image_exists(self, ref: str) -> bool: ...

    def volume_exists(self, name: str) -> bool: ...

    def secret_exists(self, name: str) -> bool: ...

    def container_exists(self, name: str) -> bool: ...

    def container_image(self, name: str) -> str:
        """The image ref container ``name`` was created from ("" if unknown)."""

    def write_secret(self, name: str, content: str) -> None:
        """Create a Podman secret ``name`` from ``content`` via stdin (no argv leak)."""


class SystemContainerOps:
    """Real :class:`ContainerOps` — drives the ``podman`` CLI via subprocess.

    Runs only on the container host that actually owns the tenants. Never
    exercised in unit tests; correctness here is by inspection, while the
    provisioner *logic* is covered via a recording double.
    """

    def __init__(self, binary: str = CONTAINER_BINARY) -> None:
        self._bin = binary

    def _missing_binary(self) -> ProvisionError:
        # FileNotFoundError from subprocess means the container CLI itself is
        # absent — translate to a clean ProvisionError so provision()/register
        # return an actionable message instead of an unrolled 500.
        return ProvisionError(
            f"container binary {self._bin!r} not found on this host — install it "
            f"(rootless podman) or unset {CONTAINER_RUNTIME_ENV}"
        )

    def run(self, argv: list[str]) -> None:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True)
        except FileNotFoundError:
            raise self._missing_binary() from None
        if proc.returncode != 0:
            raise ProvisionError(
                f"command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stderr.strip()}"
            )

    def _exists(self, kind: str, name: str) -> bool:
        # `podman <kind> inspect NAME` exits 0 iff the object exists; absence is
        # a non-zero exit, not an error. Uniform across image/volume/secret/
        # container, so one helper covers them all.
        try:
            return (
                subprocess.run(
                    [self._bin, kind, "inspect", name], capture_output=True
                ).returncode
                == 0
            )
        except FileNotFoundError:
            raise self._missing_binary() from None

    def container_image(self, name: str) -> str:
        # `.Config.Image` is the create-time image REFERENCE on both Podman and
        # Docker (`.ImageName` is Podman-only); used for image-drift detection.
        try:
            proc = subprocess.run(
                [self._bin, "container", "inspect", "--format", "{{.Config.Image}}", name],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            raise self._missing_binary() from None
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()

    def image_exists(self, ref: str) -> bool:
        return self._exists("image", ref)

    def volume_exists(self, name: str) -> bool:
        return self._exists("volume", name)

    def secret_exists(self, name: str) -> bool:
        return self._exists("secret", name)

    def container_exists(self, name: str) -> bool:
        return self._exists("container", name)

    def write_secret(self, name: str, content: str) -> None:
        # `podman secret create NAME -` reads the secret from stdin, so the key
        # never appears on an argv / in the host process list.
        try:
            proc = subprocess.run(
                [self._bin, "secret", "create", name, "-"],
                input=content.encode("utf-8"),
                capture_output=True,
            )
        except FileNotFoundError:
            raise self._missing_binary() from None
        if proc.returncode != 0:
            raise ProvisionError(
                f"podman secret create {name} failed ({proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()}"
            )


class ContainerProvisioner(AgentProvisioner):
    """Provision a dedicated Podman container + home volume + key secret for an
    ``isolation_mode="container"`` tenant.

    Pinky owns ISOLATION + LIFECYCLE only and is deliberately TOOL-AGNOSTIC.
    The image is OPERATOR-SUPPLIED (bring-your-own via ``image_provider``):
    Pinky pulls it, never builds it, and bakes in no CLIs — whatever tooling
    (gcloud / gh / kubectl / nothing) lives inside is entirely the operator's
    image. Per agent this creates:

      - a home VOLUME (``pinky-<agent>-home``) mounted at the in-container HOME,
        so the tenant's CLI OAuth state (e.g. ``~/.config/<cli>``) persists
        across restart/rebuild — the basis for a durable per-employee login;
      - a signing-key SECRET (``pinky-<agent>-key``) created from stdin so the
        key never hits an argv (same intent as the unix_user keystore);
      - the CONTAINER itself (``pinky-<agent>``), created **stopped** via
        ``podman create``. Starting/stopping it on session connect/idle and
        injecting a :class:`ContainerCommandRunner` (so the tmux server + claude
        REPL run *inside* it) is the activation increment's job.

    Container isolation is strictly OPT-IN. ``get_provisioner`` stays
    **fail-closed** for ``"container"`` (like ``"unix_user"``), so the #642
    respawn guard blocks a container-labeled agent from launching until
    activation wires the lifecycle. Construct this class directly; tests inject
    a recording :class:`ContainerOps`, so none of this needs a real Podman.

    **Idempotent** + **rollback**: identical contract to
    :class:`UnixUserProvisioner` — a re-provision of a ready tenant is a no-op,
    and a mid-provision failure tears down (in reverse) only what this call
    built.
    """

    mode = CONTAINER

    def __init__(
        self,
        *,
        ops: ContainerOps | None = None,
        image_provider: Callable[["Agent"], str] | None = None,
        signing_key_provider: Callable[[str], str] | None = None,
        prefix: str = CONTAINER_PREFIX,
        home: str = CONTAINER_HOME,
        binary: str = CONTAINER_BINARY,
    ) -> None:
        self._ops: ContainerOps = ops or SystemContainerOps(binary)
        # Where the operator's image reference comes from. The persisted
        # ``container_image`` agent field + register/update wiring land with the
        # activation increment (mirrors _default_mcp_json); until then the
        # default reads a forward-compatible attribute, so tests inject directly.
        self._image_provider = image_provider or _agent_container_image
        self._signing_key_provider = signing_key_provider or _no_signing_key
        self._prefix = prefix
        self._home = home
        self._binary = binary

    def names(self, agent: "Agent") -> ContainerNames:
        return ContainerNames.for_agent(agent.name, prefix=self._prefix, home=self._home)

    def _host_workdir(self, agent: "Agent") -> str:
        """The agent's working_dir as the SESSION will see it: absolute paths
        are symlink-resolved to match the api factory's
        ``str(Path(working_dir).resolve())`` — the mount, ``exec -w``,
        CLAUDE_CONFIG_DIR, trust seed, and tailer slug must all agree on ONE
        canonical path or hooks/transcripts silently miss."""
        wd = (agent.working_dir or "").strip()
        if wd and Path(wd).is_absolute():
            return str(Path(wd).resolve())
        return wd

    def _config_dir_for(self, agent: "Agent", n: ContainerNames) -> str:
        """CLAUDE_CONFIG_DIR for this tenant: inside the same-path-mounted
        working_dir when one is configured (host-visible — transcripts, trust,
        creds), else the home-volume fallback. Only an ABSOLUTE working_dir
        qualifies — a relative one can't be same-path bind-mounted."""
        wd = self._host_workdir(agent)
        if wd and Path(wd).is_absolute():
            return container_config_dir(wd)
        return n.config_dir

    def is_provisioned(self, agent: "Agent") -> bool:
        # The static per-agent resources the runtime depends on: the home
        # volume, the key secret, and the (created, possibly-stopped) container.
        # The image is implied — `podman create` could not have built the
        # container without it. Starting it is a separate runtime concern.
        n = self.names(agent)
        return (
            self._ops.volume_exists(n.volume)
            and self._ops.secret_exists(n.secret)
            and self._ops.container_exists(n.container)
        )

    def provision(self, agent: "Agent") -> ProvisionResult:
        n = self.names(agent)
        # Reconcile, not all-or-nothing: skip resources that already exist so
        # provision() repairs a partial tenant, and track only what THIS call
        # built so rollback never tears down a pre-existing resource. The
        # is_provisioned probe sits INSIDE the try: a missing/broken container
        # binary surfaces as a clean ok=False result, not an unrolled 500
        # bubbling out of the register endpoint.
        created: list[str] = []
        try:
            if self.is_provisioned(agent):
                return ProvisionResult(
                    ok=True,
                    mode=CONTAINER,
                    message=f"container: {n.container} already provisioned",
                )

            image = self._image_provider(agent)
            if not image:
                return ProvisionResult(
                    ok=False,
                    mode=CONTAINER,
                    message=(
                        f"container provision of {n.container} failed: no container_image "
                        f"configured for {agent.name!r} (bring-your-own image is required)"
                    ),
                )

            # 1. Home volume — persistent CLI/OAuth state.
            if not self._ops.volume_exists(n.volume):
                self._ops.run([self._binary, "volume", "create", n.volume])
                created.append(f"volume:{n.volume}")
            # 2. Signing-key secret (content via stdin, never an argv).
            if not self._ops.secret_exists(n.secret):
                key = self._signing_key_provider(agent.name)
                if not key:
                    raise ProvisionError(f"no signing key available for {agent.name!r}")
                self._ops.write_secret(n.secret, key)
                created.append(f"secret:{n.secret}")
            # 3. Ensure the operator's image is present (pull if missing). Images
            #    are shared infra, so this is NOT tracked as a per-agent resource
            #    to tear down on rollback/deprovision.
            if not self._ops.image_exists(image):
                self._ops.run([self._binary, "pull", image])
            # 4. The container — created STOPPED. Start/stop on session
            #    connect/idle is the activation increment's responsibility.
            if not self._ops.container_exists(n.container):
                self._ops.run(self._create_argv(agent, n, image))
                created.append(f"container:{n.container}")
        except Exception as e:
            removed = self._rollback(created)
            return ProvisionResult(
                ok=False,
                mode=CONTAINER,
                created=created,
                removed=removed,
                message=f"container provision of {n.container} failed: {e}",
            )

        return ProvisionResult(
            ok=True, mode=CONTAINER, created=created,
            message=f"container: provisioned {n.container}",
        )

    def _create_argv(self, agent: "Agent", n: ContainerNames, image: str) -> list[str]:
        # `podman create` (not `run`): the container exists but stays stopped
        # until the session connects. A tool-agnostic keep-alive entrypoint lets
        # the daemon `podman exec` tmux into it regardless of the image's own
        # CMD — Pinky asserts nothing about the image beyond "can run sleep".
        #
        # Engaged-path wiring (host-validated 2026-06-02 on rootless Podman):
        #   - Bind the agent's host working_dir at the SAME absolute path inside
        #     the container. The daemon regenerates CLAUDE.md/.mcp.json/.claude
        #     hooks there, and settings.json wires hook scripts by ABSOLUTE host
        #     path — mounting them anywhere else would silently no-op every hook.
        #     Same-path means `tmux new-session -c <working_dir>` resolves
        #     identically on host and in-container (no cwd translation needed).
        #   - `--userns=keep-id` (Podman): runs the tenant as the host daemon's
        #     own uid, so (a) claude accepts --dangerously-skip-permissions (it
        #     refuses to run as root) and (b) the bind-mounted, daemon-owned files
        #     stay readable+writable from inside the container.
        #   - `--add-host=host.containers.internal:host-gateway`: lets the
        #     rootless container reach the daemon API + shared MCP on the host.
        # The in-container .mcp.json (SSE → host.containers.internal) and the
        # ~/.claude trust seed are handled by the daemon (api._write_mcp_json /
        # TmuxSession), not here.
        host_workdir = self._host_workdir(agent)
        argv = [self._binary, "create", "--name", n.container, "--restart", "no"]
        # keep-id is Podman-specific; rootless Docker maps to the host user already.
        if "podman" in self._binary:
            argv += ["--userns=keep-id"]
        argv += ["--add-host=host.containers.internal:host-gateway"]
        # Resource caps — an unbounded tenant could starve the host (the Pi 5
        # shares 8GB with a POS stack). Env-overridable; "0" disables a cap.
        memory = os.environ.get(CONTAINER_MEMORY_ENV, CONTAINER_MEMORY_DEFAULT).strip()
        if memory and memory != "0":
            argv += ["--memory", memory]
        pids = os.environ.get(CONTAINER_PIDS_ENV, CONTAINER_PIDS_DEFAULT).strip()
        if pids and pids != "0":
            argv += ["--pids-limit", pids]
        argv += ["-v", f"{n.volume}:{n.home}"]
        # Only an absolute working_dir can be same-path bind-mounted (registry
        # rows are absolute in practice; a relative one would mount garbage).
        if host_workdir and Path(host_workdir).is_absolute():
            argv += ["-v", f"{host_workdir}:{host_workdir}"]
        # CLAUDE_CONFIG_DIR lives INSIDE the same-path-mounted working_dir (not
        # the home volume) so transcripts/config/creds are host-visible at the
        # identical absolute path — the host-side transcript tailer, the
        # --continue check, and `claude login` durability all depend on this.
        config_dir = self._config_dir_for(agent, n)
        argv += [
            "--secret", f"{n.secret},type=mount",
            "-e", f"HOME={n.home}",
            "-e", f"CLAUDE_CONFIG_DIR={config_dir}",
            "-e", f"PINKY_AGENT_NAME={agent.name}",
            # The image's claude is root-installed (npm prefix) while the
            # tenant runs as the keep-id user — self-update can never succeed
            # and just spams "Auto-update failed" in the REPL. The image is
            # the upgrade unit; updates ship by changing container_image.
            "-e", "DISABLE_AUTOUPDATER=1",
            "--entrypoint", "sleep",
            image, "infinity",
        ]
        return argv

    def deprovision(self, agent: "Agent", *, remove_volume: bool = False) -> ProvisionResult:
        # Container + secret are cheap and recreatable → always removed. The home
        # VOLUME holds the tenant's persisted CLI logins, so it is KEPT by
        # default; pass remove_volume=True for a full purge (on HARD delete —
        # NOT on retire, which is a soft delete that preserves data).
        n = self.names(agent)
        removed: list[str] = []
        if self._ops.container_exists(n.container):
            self._ops.run([self._binary, "rm", "-f", n.container])
            removed.append(f"container:{n.container}")
        if self._ops.secret_exists(n.secret):
            self._ops.run([self._binary, "secret", "rm", n.secret])
            removed.append(f"secret:{n.secret}")
        if remove_volume and self._ops.volume_exists(n.volume):
            self._ops.run([self._binary, "volume", "rm", n.volume])
            removed.append(f"volume:{n.volume}")
        suffix = "" if remove_volume else " (home volume preserved)"
        return ProvisionResult(
            ok=True, mode=CONTAINER, removed=removed,
            message=f"container: deprovisioned {n.container}{suffix}",
        )

    def runtime_env(self, agent: "Agent") -> dict[str, str]:
        """Process env for the tenant's in-container runtime. ``HOME`` confines
        shell/CLI state to the home volume; ``CLAUDE_CONFIG_DIR`` points inside
        the same-path-mounted working_dir (host-visible — see ``_create_argv``);
        the signing key is delivered via the mounted secret, not env."""
        n = self.names(agent)
        return {
            "HOME": n.home,
            "CLAUDE_CONFIG_DIR": self._config_dir_for(agent, n),
            "PINKY_AGENT_NAME": agent.name,
            "DISABLE_AUTOUPDATER": "1",
        }

    def start(self, agent: "Agent") -> None:
        """Start the agent's (already-provisioned) container. Idempotent —
        ``podman start`` on a running container is a successful no-op."""
        self._ops.run([self._binary, "start", self.names(agent).container])

    def stop(self, agent: "Agent") -> None:
        """Stop the agent's container. The home volume persists its state (incl.
        CLI OAuth logins), so a later start resumes a fully-configured tenant."""
        self._ops.run([self._binary, "stop", self.names(agent).container])

    def ensure_started(self, agent: "Agent") -> None:
        """Idempotently provision (if needed) then start the container, so the
        daemon can ``podman exec`` the tmux server into it. Raises
        :class:`ProvisionError` if provisioning fails — a missing/stopped
        container can't host a session. Called at session cold-start, BEFORE the
        first ``podman exec``."""
        if not self.is_provisioned(agent):
            result = self.provision(agent)
            if not result.ok:
                raise ProvisionError(result.message)
        else:
            self._recreate_if_image_changed(agent)
        self.start(agent)

    def _recreate_if_image_changed(self, agent: "Agent") -> None:
        """Recreate the container when the agent's configured ``container_image``
        no longer matches what the existing container was created from —
        otherwise an image change on a provisioned agent silently never applies.
        The home volume and key secret survive (only the container is replaced).
        Probe failures are tolerated (older ops doubles / inspect hiccups → keep
        the existing container); an actual recreate failure raises."""
        probe = getattr(self._ops, "container_image", None)
        if probe is None:
            return
        desired = (self._image_provider(agent) or "").strip()
        if not desired:
            return
        n = self.names(agent)
        try:
            current = (probe(n.container) or "").strip()
        except Exception:
            return
        if not current or current == desired:
            return
        # Pull the NEW image BEFORE destroying the working container — a
        # typo'd ref or registry outage must leave the agent running on the
        # old image (with a raised, actionable error), not container-less.
        if not self._ops.image_exists(desired):
            self._ops.run([self._binary, "pull", desired])
        self._ops.run([self._binary, "rm", "-f", n.container])
        result = self.provision(agent)
        if not result.ok:
            raise ProvisionError(result.message)

    def verify_runnable(self, agent: "Agent") -> ProvisionResult:
        """Probe that a STARTED container can actually host a tenant: the
        bring-your-own image must provide every binary in
        :data:`CONTAINER_REQUIRED_BINARIES` (tmux / claude / python3) on PATH.

        This is the runnability half of a "verified" provision — ``provision``
        only ``podman create``s the container (stopped) and ``is_provisioned``
        only checks the resources EXIST, neither proves the image can run the
        agent. A one-click containerize gates the DB flip on this so a bad image
        is refused up front instead of stranding the agent in BOOT_FAILED at
        next spawn (mirrors tmux_session._check_container_image_contract, but
        callable from the provisioning layer with no live session). The caller
        must ``start``/``ensure_started`` the container first. Returns
        ``ok=False`` with an actionable message on a missing binary (or if the
        exec itself fails); never raises.
        """
        n = self.names(agent)
        required = CONTAINER_REQUIRED_BINARIES
        # One `sh -c` AND-chain: exits 0 iff every binary resolves. ContainerOps
        # .run raises on a non-zero exit, so a missing binary surfaces as the
        # except below. Args go through argv (no shell on the host side) — the
        # only shell is the in-container `sh` evaluating a fixed, static string.
        check = " && ".join(f"command -v {c} >/dev/null 2>&1" for c in required)
        try:
            self._ops.run([self._binary, "exec", n.container, "sh", "-c", check])
        except Exception as e:
            image = self._image_provider(agent) or "<unset>"
            return ProvisionResult(
                ok=False,
                mode=CONTAINER,
                message=(
                    f"container image {image!r} for {agent.name!r} is not runnable: "
                    f"missing required tooling (needs {', '.join(required)} on PATH) "
                    f"or exec failed: {e}"
                ),
            )
        return ProvisionResult(
            ok=True,
            mode=CONTAINER,
            message=f"container {n.container} is runnable ({', '.join(required)} present)",
        )

    def _rollback(self, created: list[str]) -> list[str]:
        """Undo ``created`` resources in reverse; best-effort, never raises."""
        removed: list[str] = []
        for token in reversed(created):
            kind, _, value = token.partition(":")
            try:
                if kind == "container":
                    if self._ops.container_exists(value):
                        self._ops.run([self._binary, "rm", "-f", value])
                elif kind == "secret":
                    if self._ops.secret_exists(value):
                        self._ops.run([self._binary, "secret", "rm", value])
                elif kind == "volume":
                    if self._ops.volume_exists(value):
                        self._ops.run([self._binary, "volume", "rm", value])
                removed.append(token)
            except Exception:
                # Best-effort — a stuck resource is surfaced via the returned
                # ProvisionResult, not raised.
                continue
        return removed


def _agent_container_image(agent: "Agent") -> str:
    """Default image provider: read the agent's configured image.

    The persisted ``container_image`` field + register/update wiring land with
    the activation increment (this mirrors how ``_default_mcp_json`` is a
    placeholder until real per-tenant MCP wiring lands). Until then this reads a
    forward-compatible attribute — so the moment the column exists it just
    works — and tests inject ``image_provider`` directly.
    """
    return getattr(agent, "container_image", "") or ""


def container_runtime_enabled() -> bool:
    """True iff the operator has opted into the container runtime via
    ``PINKY_CONTAINER_RUNTIME`` (default OFF → container mode stays fail-closed).

    This is the activation gate: container lifecycle/exec only engages on a host
    that has actually been set up with a (rootless Podman) runtime. Off by
    default, so no behavior changes anywhere until an operator flips it.
    """
    return bool(os.environ.get(CONTAINER_RUNTIME_ENV, "").strip())


def container_runtime_binary() -> str:
    """The container CLI selected by ``PINKY_CONTAINER_RUNTIME``: ``"docker"``
    for ``"docker"``, else ``"podman"`` (for ``"podman"``/``"1"``/any other
    truthy value). Only meaningful when :func:`container_runtime_enabled`."""
    val = os.environ.get(CONTAINER_RUNTIME_ENV, "").strip().lower()
    return "docker" if val == "docker" else CONTAINER_BINARY


def container_runtime_supported() -> bool:
    """True iff the host's selected container runtime is one we can actually
    provision against today — i.e. enabled AND podman (docker is rejected by
    :func:`get_provisioner` because provisioning relies on podman-only secret
    delivery). Preflight + the one-click endpoints use this to fail fast with an
    actionable message rather than 500-ing at provision time on a docker host."""
    return container_runtime_enabled() and container_runtime_binary() == CONTAINER_BINARY


def container_default_image() -> str:
    """The fleet default bring-your-own image for one-click containerize
    (``PINKY_CONTAINER_DEFAULT_IMAGE``), or ``""`` if none is configured — in
    which case the operator must supply a per-agent override. Never a baked
    constant: a canonical image is a product follow-up, and this keeps v1 an
    opt-in, host-configured rollout."""
    return os.environ.get(CONTAINER_DEFAULT_IMAGE_ENV, "").strip()


def get_provisioner(
    isolation_mode: str,
    *,
    signing_key_provider: "Callable[[str], str] | None" = None,
) -> AgentProvisioner:
    """Return the provisioner for ``isolation_mode``.

    ``"local"`` → the no-op :class:`LocalProvisioner`. ``"unix_user"`` is
    recognized but still **fails closed** (its lifecycle activation is a
    separate, Linux/systemd increment). ``"container"`` is **gated**: it returns
    a real :class:`ContainerProvisioner` only when :func:`container_runtime_enabled`
    (``PINKY_CONTAINER_RUNTIME`` set), and otherwise fails closed — so the #642
    respawn guard keeps blocking container agents on any host that hasn't opted
    into a runtime. ``signing_key_provider`` is threaded into the
    ContainerProvisioner for provision/deprovision; the runnability check passes
    none (it only needs the factory to not raise). Any other value is rejected.
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
    if isolation_mode == CONTAINER:
        if container_runtime_enabled():
            binary = container_runtime_binary()
            if binary == "docker":
                # Provisioning is podman-only today: signing-key delivery uses
                # `secret create` + `create --secret ...,type=mount`, which
                # docker only supports for swarm services. Fail closed with an
                # actionable message instead of an opaque mid-provision error.
                raise NotImplementedError(
                    f"{CONTAINER_RUNTIME_ENV}='docker' is not supported yet: container "
                    "provisioning relies on podman-only secret delivery (podman secret "
                    "create / --secret). Set the env to 'podman' (rootless) to activate "
                    "container isolation."
                )
            return ContainerProvisioner(
                signing_key_provider=signing_key_provider,
                binary=binary,
            )
        raise NotImplementedError(
            "isolation_mode='container' is implemented (ContainerProvisioner) but "
            f"the container runtime is not enabled: set {CONTAINER_RUNTIME_ENV} "
            "(e.g. 'podman') on the daemon host to activate. get_provisioner stays "
            "fail-closed so the #642 respawn guard keeps blocking container until then."
        )
    raise ValueError(
        f"unknown isolation_mode {isolation_mode!r}; expected one of {sorted(KNOWN_MODES)}"
    )
