"""Explicit manifest providers for fleet and standalone-tenant SQLite stores."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from pinky_daemon.store_catalog import StoreIntegrityTarget

FLEET_MANIFEST_KIND = "fleet"
STANDALONE_TENANT_MANIFEST_KIND = "standalone-tenant"
StoreManifest = dict[str, StoreIntegrityTarget]
StoreManifestProvider = Callable[[str | os.PathLike[str]], StoreManifest]


def _authoritative(logical_name: str, path: str) -> StoreIntegrityTarget:
    return StoreIntegrityTarget(
        logical_name=logical_name,
        path=path,
        criticality="authoritative",
        recovery="snapshot",
    )


def derive_fleet_store_manifest(
    db_path: str | os.PathLike[str],
) -> StoreManifest:
    """Return the single path/recovery manifest for API boot-owned stores."""
    base = os.path.realpath(os.fspath(db_path))
    data_dir = Path(base).parent
    agents_path = base.replace(".db", "_agents.db")
    return {
        "sessions": _authoritative("sessions", base.replace(".db", "_sessions.db")),
        "session_events": _authoritative("session_events", base.replace(".db", "_sessions.db")),
        "conversations": _authoritative("conversations", base),
        "analytics": _authoritative("analytics", base.replace(".db", "_analytics.db")),
        "agents": _authoritative("agents", agents_path),
        "agent_signing_keys": _authoritative("agent_signing_keys", agents_path),
        "audit": _authoritative("audit", base.replace(".db", "_audit.db")),
        "agent_comms": _authoritative("agent_comms", base.replace(".db", "_agent_comms.db")),
        "activity": _authoritative("activity", base.replace(".db", "_activity.db")),
        "message_context": _authoritative(
            "message_context", base.replace(".db", "_message_context.db")
        ),
        "dream_state": _authoritative("dream_state", base.replace(".db", "_dream_state.db")),
        "skills": _authoritative("skills", base.replace(".db", "_skills.db")),
        "plugins": _authoritative("plugins", base.replace(".db", "_plugins.db")),
        "outreach_config": _authoritative("outreach_config", base.replace(".db", "_outreach.db")),
        "tasks": _authoritative("tasks", base.replace(".db", "_tasks.db")),
        "research": _authoritative("research", base.replace(".db", "_research.db")),
        "presentations": _authoritative("presentations", base.replace(".db", "_presentations.db")),
        "apps": _authoritative("apps", base.replace(".db", "_apps.db")),
        "triggers": _authoritative("triggers", base.replace(".db", "_triggers.db")),
        "mesh": _authoritative("mesh", base.replace(".db", "_mesh.db")),
        "kb": _authoritative("kb", os.fspath(data_dir / "kb" / "kb.db")),
        "librarian_state": StoreIntegrityTarget(
            logical_name="librarian_state",
            path=base.replace(".db", "_librarian_state.db"),
            criticality="derived",
            recovery="rebuild",
        ),
        "voice": _authoritative("voice", os.fspath(data_dir / "voice_calls.db")),
        "user_profiles": _authoritative("user_profiles", os.fspath(data_dir / "user_profiles.db")),
    }


def derive_standalone_tenant_store_manifest(
    db_path: str | os.PathLike[str],
) -> StoreManifest:
    """Return the explicit one-store manifest for a tenant-owned keystore."""
    path = os.path.realpath(os.fspath(db_path))
    return {
        "agent_signing_keys": _authoritative("agent_signing_keys", path),
    }


def derive_standalone_tenant_store_manifest_for_agent(
    agent_name: str,
) -> StoreManifest:
    """Derive the canonical standalone manifest for one registered Unix user."""
    from pinky_daemon.provisioning import UnixUserPaths

    paths = UnixUserPaths.for_agent(agent_name)
    return derive_standalone_tenant_store_manifest(paths.keystore)


def manifest_provider_for_kind(kind: str) -> StoreManifestProvider:
    """Resolve an explicit manifest kind; never infer one from a path."""
    if kind == FLEET_MANIFEST_KIND:
        return derive_fleet_store_manifest
    if kind == STANDALONE_TENANT_MANIFEST_KIND:
        return derive_standalone_tenant_store_manifest
    raise ValueError(
        f"unknown manifest kind {kind!r}; expected "
        f"{FLEET_MANIFEST_KIND!r} or {STANDALONE_TENANT_MANIFEST_KIND!r}"
    )
