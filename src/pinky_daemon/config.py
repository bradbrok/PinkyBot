"""Deployment posture configuration.

Single source of truth for the security posture PinkyBot is running under.
Resolved once at startup, cached on ``app.state.deployment_mode``, and consumed
by the hardening middleware introduced in the rest of the #433 track:

* #436 — LAN-only mode + private-CIDR middleware + bind defaults
* #437 — Security headers middleware
* #439 — Elevated-session protection for dangerous routes
* #441 — Refuse-to-boot guards for insecure web-mode configs

Each downstream feature should branch on ``app.state.deployment_mode`` rather
than reading its own env knob, so operator intent is encoded in exactly one
place.

Modes
-----
``trusted``
    Default. Matches today's Mac Mini deployment: trusted LAN, single shared
    cookie session, no extra network filter, no bearer-token API auth.

``lan``
    LAN appliance posture (Raspberry Pi, NAS, home server). Cookie auth still
    required, plus a private-CIDR middleware to refuse non-private clients.

``web``
    Internet-exposed VPS. Bind defaults to loopback (assumes a reverse proxy
    in front), bearer-token API auth is enabled, admin routes require an
    elevated session, full security headers, refuse-to-boot guards apply.

Resolution rules
----------------
* Source: ``PINKY_DEPLOYMENT_MODE`` env var.
* Empty/unset → ``trusted`` (back-compat).
* Case-insensitive, surrounding whitespace stripped.
* Invalid value → :class:`InvalidDeploymentModeError` (fail closed; do **not**
  silently fall back to ``trusted``). The daemon entrypoint is expected to
  catch this and abort startup with a useful message.

Part of #433 (Hardening: PinkyBot for web-exposed deployments). Issue #434.
"""

from __future__ import annotations

import os
from enum import Enum

__all__ = [
    "DEPLOYMENT_MODE_ENV",
    "DeploymentMode",
    "InvalidDeploymentModeError",
    "default_bind_host",
    "resolve_deployment_mode",
    "warn_if_insecure_exposure",
]

#: Canonical env var name. Imported by other modules so the spelling lives in
#: one place.
DEPLOYMENT_MODE_ENV = "PINKY_DEPLOYMENT_MODE"


class DeploymentMode(str, Enum):
    """Operator-declared deployment posture."""

    TRUSTED = "trusted"
    LAN = "lan"
    WEB = "web"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


_VALID_VALUES = tuple(m.value for m in DeploymentMode)


class InvalidDeploymentModeError(ValueError):
    """Raised when ``PINKY_DEPLOYMENT_MODE`` has an unrecognized value.

    Fail-closed posture: a typo like ``PINKY_DEPLOYMENT_MODE=prod`` must abort
    startup, not silently fall back to ``trusted`` and hand the operator a
    false sense of hardening.
    """


def resolve_deployment_mode(env: dict | None = None) -> DeploymentMode:
    """Resolve the deployment mode from ``PINKY_DEPLOYMENT_MODE``.

    Pure function. Reads from the supplied mapping (defaults to ``os.environ``),
    normalises case + whitespace, returns the matching :class:`DeploymentMode`.

    Raises:
        InvalidDeploymentModeError: when the env var is set but doesn't match
            one of the canonical values. No aliases are accepted (no "prod",
            no "production"). Add aliases only if we commit to supporting them
            long-term.
    """
    source = env if env is not None else os.environ
    raw = (source.get(DEPLOYMENT_MODE_ENV) or "").strip().lower()
    if not raw:
        return DeploymentMode.TRUSTED
    try:
        return DeploymentMode(raw)
    except ValueError as e:
        raise InvalidDeploymentModeError(
            f"{DEPLOYMENT_MODE_ENV}={raw!r} is not a valid deployment mode. "
            f"Expected one of: {', '.join(_VALID_VALUES)}."
        ) from e


def default_bind_host(mode: DeploymentMode) -> str:
    """Default ``--host`` for a given mode.

    Used by the daemon entrypoint when the operator did **not** pass an
    explicit ``--host`` (i.e. ``--host`` was left at its sentinel ``None``).
    The asymmetry between operator-supplied and inherited defaults matters for
    #436 (bind defaults) and #441 (refuse-to-boot guards), which need to tell
    "operator chose 0.0.0.0" from "old hardcoded default".

    * ``web`` → ``127.0.0.1`` — assumes a reverse proxy is in front. Refuses
      to bind public interfaces by default.
    * ``trusted`` / ``lan`` → ``0.0.0.0`` — back-compat with today's Mac Mini
      and Pi-fleet deployments.
    """
    if mode is DeploymentMode.WEB:
        return "127.0.0.1"
    return "0.0.0.0"


def warn_if_insecure_exposure(mode: DeploymentMode, host: str) -> str | None:
    """Build a loud warning string when ``trusted`` mode binds all interfaces.

    Today's Mac Mini deployment runs ``trusted`` + ``0.0.0.0`` and there's no
    intent to break that. But if PinkyBot ends up on an internet-reachable host
    by accident (or a fresh operator copies the README config), the operator
    should see a screaming line in the logs at startup rather than discovering
    the exposure later.

    Returns ``None`` when no warning is needed. Caller is responsible for
    surfacing the string (typically ``print(... , file=sys.stderr)``).
    """
    if mode is DeploymentMode.TRUSTED and host == "0.0.0.0":
        return (
            f"⚠️  PINKY_DEPLOYMENT_MODE={mode.value} + --host=0.0.0.0: "
            "the API is bound to all interfaces under the trusted-LAN posture. "
            "If this host is internet-reachable, set "
            f"{DEPLOYMENT_MODE_ENV}=web and put PinkyBot behind a reverse proxy."
        )
    return None
