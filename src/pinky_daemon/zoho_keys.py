"""Per-agent Zoho HMAC key derivation for MCP spawning.

This is the daemon side of the per-agent-keys design (closes the bash
escape hatch where any agent with the shared ``ZOHO_API_SECRET`` can
sign requests as a different agent). See ``olegbrok/zoho-crm-cli#10``
for the threat model and full design.

Algorithm
=========

The daemon holds a master secret in ``~/PinkyBot/.master-key`` (chmod
600) and an instance_id in ``data/.instance-id`` (UUID, rotates with
the daemon process). For each agent's MCP spawn we derive

    agent_key = HMAC_SHA256(master, "zoho|" + agent_name + "|" + instance_id)

The Mac-side Zoho server (``zoho_crm/server.py``) independently
re-derives the expected key from ``agent_name`` + ``instance_id`` (the
latter sent in the ``X-Pinky-Instance`` header) and verifies the
signature. The derived key is the only thing handed to the agent's MCP
subprocess — never the master.

Isolation properties
====================

Per-agent env injection on the MCP subprocess is isolation-equivalent
to FD-passing **only when ``kernel.yama.ptrace_scope=2`` is in
effect**. Without it, same-uid sibling processes can read each other's
``/proc/<pid>/environ`` and steal derived keys. With it, only
``CAP_SYS_PTRACE`` (which agent processes don't hold) can.

ptrace_scope=2 is therefore a *hard deployment requirement*, not a
nice-to-have. See ``docs/deployment-per-agent-keys.md``.

Cross-repo invariant
====================

The derivation algorithm here **must** match
``zoho_crm.agent_auth.derive_agent_key`` byte-for-byte. There is a
parity test in ``tests/test_zoho_keys.py`` that imports both and
asserts equality on a fixed vector.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger("pinky.zoho_keys")

# Domain separator for the HMAC derivation. MUST match
# zoho_crm.agent_auth._DERIVE_VERSION. Bump only if the derivation
# changes in a backwards-incompatible way.
_DERIVE_VERSION = "zoho"


def derive_zoho_agent_key(
    master_secret: str, agent_name: str, instance_id: str
) -> str:
    """Derive a per-agent HMAC key from the master secret.

    See module docstring for algorithm. Raises ``ValueError`` on any
    empty input — defensive against silent fall-through to a weak key.
    """
    if not master_secret or not agent_name or not instance_id:
        raise ValueError(
            "master_secret, agent_name, and instance_id are all required"
        )
    payload = f"{_DERIVE_VERSION}|{agent_name}|{instance_id}".encode("utf-8")
    return hmac.new(
        master_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()


def load_master_secret(path: Path | str | None = None) -> str:
    """Load the Zoho master secret from disk.

    Default path is ``~/PinkyBot/.master-key``. Returns an empty string
    if the file is absent or unreadable — caller decides whether that's
    an error (production) or fine (dev without per-agent keys yet).

    Logs a warning if the file is found but has mode bits other than
    0600 — secrets on disk should be mode 0600 (or the file should not
    exist at all).
    """
    if path is None:
        path = Path.home() / "PinkyBot" / ".master-key"
    else:
        path = Path(path)
    if not path.exists():
        return ""
    try:
        # File-mode sanity check — warn (don't fail) on overly permissive
        # bits. We don't auto-chmod because deployment scripts should
        # set perms explicitly.
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:  # group or world bits set
            log.warning(
                "Zoho master key at %s has loose perms (0%o); "
                "should be 0600. Production deployments must fix this.",
                path,
                mode,
            )
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.error("Failed to read Zoho master key at %s: %s", path, exc)
        return ""


def load_or_create_instance_id(path: Path | str | None = None) -> str:
    """Load the daemon instance UUID, generating + persisting on first call.

    Default path is ``<repo>/data/.instance-id``. The instance_id
    survives daemon restarts so derived keys remain valid across them;
    rotate by deleting the file before restart (e.g. for emergency key
    revocation).
    """
    if path is None:
        # data/ lives at repo root by convention. We're in src/pinky_daemon/,
        # so go up two levels.
        path = Path(__file__).resolve().parents[2] / "data" / ".instance-id"
    else:
        path = Path(path)

    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError as exc:
            log.warning(
                "Failed to read instance_id at %s (regenerating): %s",
                path,
                exc,
            )

    new_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write with mode 0600 from the start.
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, new_id.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        # Persistence failure is non-fatal — we can still return the
        # generated id for this run. But it'll rotate on next restart,
        # which is sub-optimal. Surface the warning loudly.
        log.error(
            "Failed to persist instance_id at %s; will regenerate next "
            "restart: %s",
            path,
            exc,
        )
    return new_id


class ZohoKeyDeriver:
    """Lazy-loaded master + instance_id with one-shot derivation per agent.

    Cached at daemon startup so the master secret is read once. Per-agent
    derivation is cheap (single HMAC) — no caching needed.

    Returns ``None`` from ``derive_for(agent)`` when no master is
    configured. That's the signal to fall back to the legacy
    shared-secret path during migration.
    """

    def __init__(
        self,
        *,
        master_path: Path | str | None = None,
        instance_path: Path | str | None = None,
    ) -> None:
        self._master = load_master_secret(master_path)
        self._instance_id = (
            load_or_create_instance_id(instance_path)
            if self._master
            else ""
        )

    @property
    def enabled(self) -> bool:
        """True when per-agent derivation is configured (master loaded)."""
        return bool(self._master and self._instance_id)

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def derive_for(self, agent_name: str) -> str | None:
        """Derive the per-agent key for ``agent_name``.

        Returns ``None`` when per-agent keys aren't configured (no master
        secret on disk) — caller falls back to legacy auth.
        """
        if not self.enabled:
            return None
        return derive_zoho_agent_key(
            self._master, agent_name, self._instance_id
        )


# Module-level singleton, loaded on first import. Set to None to force
# re-initialization (e.g. after rotating .master-key without daemon restart,
# which is unsupported but useful in tests).
_default_deriver: ZohoKeyDeriver | None = None


def get_default_deriver() -> ZohoKeyDeriver:
    """Return the process-wide deriver, constructing it once."""
    global _default_deriver
    if _default_deriver is None:
        _default_deriver = ZohoKeyDeriver()
    return _default_deriver


def reset_default_deriver_for_tests() -> None:
    """Test hook to clear the cached deriver."""
    global _default_deriver
    _default_deriver = None
