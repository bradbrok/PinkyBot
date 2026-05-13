"""Refuse-to-boot guards for insecure web-mode configurations (#441).

When ``PINKY_DEPLOYMENT_MODE=web``, the daemon must not start with a
configuration that's obviously unsafe for an internet-exposed deployment.
Loud failure beats silent surprise: an operator who pastes a config into a
VPS and runs the daemon should get a single rejection with the full list
of fixes needed, not discover three months later that their session
secret was the placeholder.

This module ships the guards that **don't depend on unshipped pieces of
the #433 track**. The remaining guards land alongside their owning PR
(``users`` table → #435; ``PINKY_UI_PASSWORD`` legacy check → #435; audit
emission for overrides → #440). Each is marked with a TODO referencing
its owner.

Usage
-----
:func:`assert_web_mode_safe` is invoked from
``pinky_daemon.__main__._run_api`` after argparse + mode resolution and
before ``uvicorn.run``. It raises :class:`BootGuardError` listing every
failed guard. The CLI catches the error, prints the message, and exits
with code 3 — distinct from the existing exit code 2 used for invalid
``PINKY_DEPLOYMENT_MODE`` (#434).

Override mechanism
------------------
Each guard has a specific override env var
(``PINKY_ALLOW_INSECURE_<NAME>=true``). There is **no** global escape
hatch by design — operators that need to bypass a guard for an emergency
must do so per-guard, and each override is logged loudly at startup.
Once #440 lands, overrides will also emit an audit event.

Guards (this PR)
----------------
* ``SESSION_SECRET`` — ``PINKY_SESSION_SECRET`` unset, shorter than 32
  chars, or one of a small set of obvious placeholder values.
* ``PUBLIC_BIND_NO_TLS`` — bound to ``0.0.0.0`` (or any non-loopback
  host) without ``PINKY_BEHIND_TLS_PROXY=true`` to acknowledge that TLS
  is being terminated upstream.
* ``MISSING_TRUSTED_PROXIES`` — ``PINKY_TRUSTED_PROXIES`` empty when
  ``PINKY_BEHIND_TLS_PROXY=true``. Without it, audit and rate-limit
  attribution silently flatten to ``127.0.0.1``.
* ``DB_PERMISSIONS`` — DB file or its parent directory is world- or
  group-readable / writable (mode bits & ``0o077`` != 0).

Warnings (non-fatal, this PR)
-----------------------------
* ``RUNNING_AS_ROOT`` — daemon process UID == 0.

Deferred to follow-up PRs
-------------------------
* ``ADMIN_USER_EXISTS`` — needs the ``users`` table from #435.
* ``LEGACY_UI_PASSWORD`` — needs the migration story in #435.
* ``MULTI_WORKER_NO_REDIS`` — needs the rate-limit backend selection
  from #438.
* Audit emission for overrides — needs the audit writer from #440.

Part of #433. Issue #441.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pinky_daemon.config import DeploymentMode

__all__ = [
    "BOOT_GUARD_EXIT_CODE",
    "BootGuardError",
    "GuardResult",
    "assert_web_mode_safe",
    "format_guard_error",
    "run_warning_guards",
]

logger = logging.getLogger(__name__)

#: Exit code when a boot guard fails. Distinct from invalid-mode (2)
#: established in #434.
BOOT_GUARD_EXIT_CODE = 3


# Placeholder session-secret values that should be rejected even when long
# enough. These are not exhaustive, but they catch the obvious copy-from-
# README cases.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "changeme",
        "change-me",
        "secret",
        "pinky",
        "password",
        "default",
        "admin",
        "test",
    }
)


@dataclass(frozen=True)
class GuardResult:
    """Outcome of a single boot guard.

    ``ok=True`` means the guard passed. ``ok=False`` with a populated
    ``message`` is a fatal config problem; the operator sees the message in
    the rejection block. ``override_used=True`` is for fatal-but-overridden
    cases (logged loudly).
    """

    name: str
    ok: bool
    message: str = ""
    override_used: bool = False


class BootGuardError(RuntimeError):
    """Raised when at least one fatal boot guard fails.

    Carries the list of failures so the CLI can format a single rejection
    block. The message is preformatted via :func:`format_guard_error`.
    """

    def __init__(self, results: list[GuardResult]):
        self.results = results
        super().__init__(format_guard_error(results))


# ── Individual guards ────────────────────────────────────────────


def _override_active(env: dict, name: str) -> bool:
    raw = (env.get(f"PINKY_ALLOW_INSECURE_{name}") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def check_session_secret(env: dict) -> GuardResult:
    name = "SESSION_SECRET"
    secret = (env.get("PINKY_SESSION_SECRET") or "").strip()
    if not secret:
        message = (
            "PINKY_SESSION_SECRET is unset. Set it to a 32+ char "
            "random value (e.g. `python -c 'import secrets; "
            "print(secrets.token_urlsafe(48))'`)."
        )
        return _maybe_override(env, name, message)
    if len(secret) < 32:
        message = (
            f"PINKY_SESSION_SECRET is too short ({len(secret)} chars; "
            "need 32+)."
        )
        return _maybe_override(env, name, message)
    lowered = secret.lower()
    if lowered in _PLACEHOLDER_SECRETS:
        message = (
            f"PINKY_SESSION_SECRET is set to the placeholder value "
            f"{secret!r}. Generate a random value before deploying."
        )
        return _maybe_override(env, name, message)
    # Trivial repetition (all same character) — caught even when long
    # enough.
    if len(set(secret)) <= 2:
        message = (
            "PINKY_SESSION_SECRET has too little entropy "
            "(uses 2 or fewer distinct characters)."
        )
        return _maybe_override(env, name, message)
    return GuardResult(name=name, ok=True)


def check_public_bind_requires_tls_marker(
    env: dict, host: str
) -> GuardResult:
    """Refuse 0.0.0.0 (or any non-loopback bind) without TLS marker."""
    name = "PUBLIC_BIND_NO_TLS"
    # Loopback binds are always fine: no internet exposure regardless of
    # TLS marker.
    if host in ("127.0.0.1", "::1", "localhost"):
        return GuardResult(name=name, ok=True)
    behind_tls = (
        env.get("PINKY_BEHIND_TLS_PROXY", "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    if behind_tls:
        return GuardResult(name=name, ok=True)
    message = (
        f"web mode is bound to {host} without PINKY_BEHIND_TLS_PROXY=true. "
        "Either bind to 127.0.0.1 (and let a TLS-terminating reverse proxy "
        "forward), or set PINKY_BEHIND_TLS_PROXY=true to acknowledge TLS "
        "is terminated upstream."
    )
    return _maybe_override(env, name, message)


def check_trusted_proxies_when_behind_tls(env: dict) -> GuardResult:
    """When behind a TLS proxy, the trusted-proxy list must be populated.

    Otherwise audit and rate-limit attribution flatten to 127.0.0.1 because
    the canonical client-IP resolver (#442) refuses to honor XFF/Forwarded
    from an untrusted peer.
    """
    name = "MISSING_TRUSTED_PROXIES"
    behind_tls = (
        env.get("PINKY_BEHIND_TLS_PROXY", "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    if not behind_tls:
        return GuardResult(name=name, ok=True)
    if (env.get("PINKY_TRUSTED_PROXIES") or "").strip():
        return GuardResult(name=name, ok=True)
    message = (
        "PINKY_BEHIND_TLS_PROXY=true but PINKY_TRUSTED_PROXIES is empty. "
        "Set PINKY_TRUSTED_PROXIES to the CIDR(s) of your reverse proxy "
        "(e.g. PINKY_TRUSTED_PROXIES=127.0.0.1/32) so client-IP attribution "
        "works. Without this, audit and rate-limit will see only the proxy."
    )
    return _maybe_override(env, name, message)


def check_db_permissions(
    env: dict, db_path: str | os.PathLike
) -> GuardResult:
    """Refuse to start when the DB or its parent dir has loose permissions.

    On Windows ``stat`` mode bits don't carry POSIX semantics; the guard
    no-ops there.
    """
    name = "DB_PERMISSIONS"
    if sys.platform == "win32":
        return GuardResult(name=name, ok=True)

    path = Path(db_path)
    target = path if path.exists() else path.parent
    # If neither exists yet, the daemon hasn't initialised — skip. The
    # next boot after init will catch it.
    if not target.exists():
        return GuardResult(name=name, ok=True)
    mode = target.stat().st_mode & 0o777
    # Reject group or world read/write/exec bits.
    if mode & 0o077:
        message = (
            f"{target} has loose permissions ({oct(mode)}); should be 0o700 "
            "(directory) or 0o600 (file). Run "
            f"`chmod go-rwx {target}` and any sibling secret files."
        )
        return _maybe_override(env, name, message)
    return GuardResult(name=name, ok=True)


def _maybe_override(env: dict, name: str, message: str) -> GuardResult:
    """Convert a fatal message into a passing-but-overridden result if the
    operator set the per-guard override."""
    if _override_active(env, name):
        logger.warning(
            "boot_guards: override active for %s — %s",
            name,
            message,
        )
        return GuardResult(name=name, ok=True, override_used=True)
    return GuardResult(name=name, ok=False, message=message)


# ── Warning guards (non-fatal) ────────────────────────────────────


def warn_running_as_root(env: dict) -> str | None:
    """Return a warning string when the daemon is running as UID 0.

    Skipped on platforms without ``geteuid``.
    """
    if not hasattr(os, "geteuid"):
        return None
    if os.geteuid() != 0:
        return None
    if _override_active(env, "RUNNING_AS_ROOT"):
        return None
    return (
        "boot_guards: warning: daemon is running as root. Strongly "
        "recommended to drop privileges or run under a dedicated service "
        "user. Set PINKY_ALLOW_INSECURE_RUNNING_AS_ROOT=true to silence."
    )


def run_warning_guards(env: dict | None = None) -> list[str]:
    """Run all non-fatal warning guards. Return any messages produced."""
    source = env if env is not None else os.environ
    out: list[str] = []
    for guard in (warn_running_as_root,):
        msg = guard(source)
        if msg:
            out.append(msg)
    return out


# ── Top-level entrypoint ─────────────────────────────────────────


def _gather_web_guards(
    env: dict, host: str, db_path: str | os.PathLike
) -> Iterable[GuardResult]:
    yield check_session_secret(env)
    yield check_public_bind_requires_tls_marker(env, host)
    yield check_trusted_proxies_when_behind_tls(env)
    yield check_db_permissions(env, db_path)


def assert_web_mode_safe(
    mode: DeploymentMode,
    host: str,
    db_path: str | os.PathLike,
    env: dict | None = None,
) -> None:
    """Run all web-mode boot guards. No-op outside ``web`` mode.

    Raises:
        BootGuardError: if any guard fails. The error message is the
            preformatted rejection block (see :func:`format_guard_error`)
            and ``error.results`` carries the structured per-guard
            outcomes.
    """
    if mode is not DeploymentMode.WEB:
        return
    source = env if env is not None else os.environ
    results = list(_gather_web_guards(source, host, db_path))
    failed = [r for r in results if not r.ok]
    if failed:
        raise BootGuardError(failed)
    # Emit any warning guards even on success.
    for warning in run_warning_guards(source):
        logger.warning(warning)


def format_guard_error(results: list[GuardResult]) -> str:
    """Format a list of failing guards as a single stderr-friendly block.

    Mirrors the format defined in #441's spec.
    """
    lines = [
        "ERROR: refusing to start in 'web' deployment mode with insecure config:"
    ]
    for r in results:
        lines.append(f"  - [{r.name}] {r.message}")
    lines.append("")
    lines.append(
        "Fix the above and restart, or set PINKY_DEPLOYMENT_MODE=trusted "
        "for the legacy posture."
    )
    lines.append(
        "Per-guard overrides exist for emergencies "
        "(PINKY_ALLOW_INSECURE_<NAME>=true) and are logged loudly."
    )
    return "\n".join(lines)
