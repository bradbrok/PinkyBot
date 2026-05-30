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
                      this path is Linux/systemd-only and lands in inc3b;
                      this module ships only the interface + the no-op
                      ``LocalProvisioner`` in inc3a.

A ``AgentProvisioner`` owns the lifecycle of those OS resources
(provision / deprovision / introspect) and contributes any extra process
environment the runtime needs (``runtime_env``). The companion seam — how
the runtime *command* is launched under the right uid — lives behind the
CommandRunner in ``tmux_session`` (inc3b); a provisioner never spawns the
agent itself, it only prepares the ground.

Design constraints (why this exists now, before any real impl):
  - Additive and inert: nothing in the daemon lifecycle calls a provisioner
    in inc3a. Wiring provisioning into registration/start/retire is inc3b.
  - Fail-closed on the unimplemented path: ``get_provisioner("unix_user")``
    raises rather than silently degrading to local — an operator who asks
    for OS isolation must not get a no-op by accident.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids a runtime import cycle
    from .agent_registry import Agent


# Canonical set of recognized isolation modes. Mirrors the validator on
# ``RegisterAgentRequest.isolation_mode`` (api_models.py) — duplicated here so
# in-process callers of ``get_provisioner`` get the same guarantee independent
# of the request layer.
LOCAL = "local"
UNIX_USER = "unix_user"
KNOWN_MODES = frozenset({LOCAL, UNIX_USER})


@dataclass
class ProvisionResult:
    """Outcome of a provision / deprovision call.

    ``created``/``removed`` enumerate the OS resources touched, in the order
    they were touched, so inc3b's UnixUserProvisioner can drive partial-
    failure rollback (undo in reverse) and so callers can audit exactly what
    changed. For the no-op LocalProvisioner both lists are always empty.
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


def get_provisioner(isolation_mode: str) -> AgentProvisioner:
    """Return the provisioner for ``isolation_mode``.

    ``"local"`` → the no-op :class:`LocalProvisioner`. ``"unix_user"`` is a
    recognized mode but its provisioner lands in #149 inc3b, so it raises
    :class:`NotImplementedError` rather than silently degrading to local —
    an operator asking for OS isolation must never get a no-op by accident.
    Any other value is rejected as unknown.
    """
    if isolation_mode == LOCAL:
        return LocalProvisioner()
    if isolation_mode == UNIX_USER:
        raise NotImplementedError(
            "isolation_mode='unix_user' provisioning lands in #149 inc3b "
            "(Linux/systemd OS-user sandbox); not available yet"
        )
    raise ValueError(
        f"unknown isolation_mode {isolation_mode!r}; expected one of {sorted(KNOWN_MODES)}"
    )
