"""Explicit manifest providers for fleet and standalone-tenant SQLite stores."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from pinky_daemon.store_catalog import StoreIntegrityTarget, default_store_connection_policy

FLEET_MANIFEST_KIND = "fleet"
STANDALONE_TENANT_MANIFEST_KIND = "standalone-tenant"
StoreManifest = dict[str, StoreIntegrityTarget]
StoreManifestProvider = Callable[[str | os.PathLike[str]], StoreManifest]


def _store(
    logical_name: str,
    path: str,
    *,
    criticality: str,
    recovery: str = "snapshot",
    journal_mode: str | None = None,
) -> StoreIntegrityTarget:
    return StoreIntegrityTarget(
        logical_name=logical_name,
        path=path,
        criticality=criticality,
        recovery=recovery,
        journal_mode=journal_mode,
        connection_policy=default_store_connection_policy(logical_name),
    )


def derive_fleet_store_manifest(
    db_path: str | os.PathLike[str],
) -> StoreManifest:
    """Return the single path/recovery manifest for API boot-owned stores."""
    base = os.path.realpath(os.fspath(db_path))
    data_dir = Path(base).parent
    agents_path = base.replace(".db", "_agents.db")
    return {
        "sessions": _store(
            "sessions", base.replace(".db", "_sessions.db"), criticality="delivery"
        ),
        "session_events": _store(
            "session_events", base.replace(".db", "_sessions.db"), criticality="telemetry"
        ),
        "conversations": _store("conversations", base, criticality="memory"),
        "analytics": _store(
            "analytics", base.replace(".db", "_analytics.db"), criticality="telemetry"
        ),
        "agents": _store(
            "agents", agents_path, criticality="delivery", journal_mode="truncate"
        ),
        "agent_signing_keys": _store(
            "agent_signing_keys",
            agents_path,
            criticality="authority",
            journal_mode="truncate",
        ),
        "audit": _store("audit", base.replace(".db", "_audit.db"), criticality="memory"),
        "agent_comms": _store(
            "agent_comms", base.replace(".db", "_agent_comms.db"), criticality="delivery"
        ),
        "activity": _store(
            "activity", base.replace(".db", "_activity.db"), criticality="telemetry"
        ),
        "message_context": _store(
            "message_context",
            base.replace(".db", "_message_context.db"),
            criticality="delivery",
        ),
        "dream_state": _store(
            "dream_state", base.replace(".db", "_dream_state.db"), criticality="memory"
        ),
        "skills": _store(
            "skills", base.replace(".db", "_skills.db"), criticality="authority"
        ),
        "plugins": _store(
            "plugins", base.replace(".db", "_plugins.db"), criticality="authority"
        ),
        "outreach_config": _store(
            "outreach_config", base.replace(".db", "_outreach.db"), criticality="authority"
        ),
        "tasks": _store("tasks", base.replace(".db", "_tasks.db"), criticality="memory"),
        "research": _store(
            "research", base.replace(".db", "_research.db"), criticality="memory"
        ),
        "presentations": _store(
            "presentations", base.replace(".db", "_presentations.db"), criticality="memory"
        ),
        "apps": _store("apps", base.replace(".db", "_apps.db"), criticality="memory"),
        "triggers": _store(
            "triggers", base.replace(".db", "_triggers.db"), criticality="delivery"
        ),
        "mesh": _store("mesh", base.replace(".db", "_mesh.db"), criticality="delivery"),
        "kb": _store(
            "kb", os.fspath(data_dir / "kb" / "kb.db"), criticality="memory"
        ),
        "librarian_state": _store(
            "librarian_state",
            base.replace(".db", "_librarian_state.db"),
            criticality="telemetry",
            recovery="rebuild",
        ),
        "voice": _store(
            "voice", os.fspath(data_dir / "voice_calls.db"), criticality="delivery"
        ),
        "user_profiles": _store(
            "user_profiles", os.fspath(data_dir / "user_profiles.db"), criticality="memory"
        ),
    }


def derive_standalone_tenant_store_manifest(
    db_path: str | os.PathLike[str],
) -> StoreManifest:
    """Return the explicit one-store manifest for a tenant-owned keystore."""
    path = os.path.realpath(os.fspath(db_path))
    return {
        "agent_signing_keys": _store(
            "agent_signing_keys",
            path,
            criticality="authority",
            journal_mode="delete",
        ),
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
