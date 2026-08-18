"""Attended, daemon-down restoration of verified SQLite store snapshots."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pinky_daemon.storage_observability import emit_storage_event
from pinky_daemon.store_authority import (
    StoreAuthorityError,
    assert_no_open_store_descriptors,
    store_authority_lock,
)
from pinky_daemon.store_manifest import StoreManifestProvider

_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

STORE_SCHEMA_SENTINELS: dict[str, tuple[str, ...]] = {
    "sessions": ("sessions",),
    "session_events": ("session_events",),
    "conversations": ("messages", "messages_fts"),
    "analytics": ("analytics_session_facts",),
    "agents": ("agents",),
    "agent_signing_keys": ("agent_signing_keys",),
    "audit": ("audit_log",),
    "agent_comms": ("messages", "inbox"),
    "activity": ("activity_log",),
    "message_context": ("message_contexts",),
    "dream_state": ("dream_state",),
    "skills": ("skills",),
    "plugins": ("plugin_state",),
    "outreach_config": ("platforms",),
    "tasks": ("projects", "tasks"),
    "research": ("research_topics", "research_briefs"),
    "presentations": ("presentations", "presentation_versions"),
    "apps": ("apps",),
    "triggers": ("triggers",),
    "mesh": ("mesh_messages",),
    "kb": ("raw_sources", "wiki_pages"),
    "librarian_state": ("librarian_state",),
    "voice": ("call_request", "voice_call_session"),
    "user_profiles": ("user_profiles",),
}


class StoreRestoreError(RuntimeError):
    """Base error for attended store restoration."""


class StoreRestoreSelectionError(StoreRestoreError):
    """Raised when a requested manifest target is absent or unsafe."""


class StoreRestoreSafetyError(StoreRestoreError):
    """Raised when the daemon-down safety proof cannot be established."""


class StoreRestoreVerificationError(StoreRestoreError):
    """Raised when a static restore candidate fails integrity or schema checks."""


@dataclass(frozen=True, slots=True)
class StoreRestoreResult:
    logical_names: tuple[str, ...]
    corrupt_path: str
    verification: str


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    dev: int
    ino: int
    mode: int


@dataclass(frozen=True, slots=True)
class _SelectedStore:
    target: Path
    logical_names: tuple[str, ...]


def restore_store(
    *,
    db_path: str | os.PathLike[str],
    logical_name: str,
    snapshot_path: str | os.PathLike[str],
    manifest_provider: StoreManifestProvider,
) -> StoreRestoreResult:
    """Preserve one corrupt manifest target and atomically install a snapshot."""
    bounded_name = logical_name if logical_name in STORE_SCHEMA_SENTINELS else "unregistered"
    try:
        with store_authority_lock(db_path):
            result = _restore_under_authority(
                db_path=db_path,
                logical_name=logical_name,
                snapshot_path=snapshot_path,
                manifest_provider=manifest_provider,
            )
    except (StoreRestoreSelectionError, StoreRestoreSafetyError):
        emit_storage_event(
            "restore",
            logical_name=bounded_name,
            outcome="refused",
            timestamp=_timestamp(),
        )
        raise
    except StoreRestoreVerificationError:
        emit_storage_event(
            "restore",
            logical_name=bounded_name,
            outcome="failed",
            timestamp=_timestamp(),
        )
        raise
    except StoreAuthorityError as exc:
        emit_storage_event(
            "restore",
            logical_name=bounded_name,
            outcome="refused",
            timestamp=_timestamp(),
        )
        raise StoreRestoreSafetyError("exclusive daemon-store authority is unavailable") from exc
    except (OSError, sqlite3.Error) as exc:
        emit_storage_event(
            "restore",
            logical_name=bounded_name,
            outcome="failed",
            timestamp=_timestamp(),
        )
        raise StoreRestoreError("store restoration failed") from exc

    emit_storage_event(
        "restore",
        logical_name=bounded_name,
        outcome="success",
        timestamp=_timestamp(),
    )
    return result


def _restore_under_authority(
    *,
    db_path: str | os.PathLike[str],
    logical_name: str,
    snapshot_path: str | os.PathLike[str],
    manifest_provider: StoreManifestProvider,
) -> StoreRestoreResult:
    selected = _select_target(
        db_path,
        logical_name,
        manifest_provider=manifest_provider,
    )
    target = selected.target
    target_paths = _target_and_sidecars(target)
    target_identities = _capture_identities(target_paths)

    snapshot = _select_snapshot(Path(snapshot_path))
    snapshot_identity = _identity(snapshot)
    if (snapshot_identity.dev, snapshot_identity.ino) in {
        (identity.dev, identity.ino) for identity in target_identities.values()
    }:
        raise StoreRestoreSelectionError("snapshot aliases the selected target")
    _verify_static_store(snapshot, selected.logical_names)

    descriptor, temp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.restore-",
        suffix=".tmp",
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(snapshot, temp_path)
        os.chmod(temp_path, stat.S_IMODE(snapshot_identity.mode))
        _fsync_file(temp_path)
        if _identity(snapshot) != snapshot_identity:
            raise StoreRestoreSafetyError("snapshot identity changed during restore")
        _verify_static_store(temp_path, selected.logical_names)

        corrupt_path = _next_corrupt_path(target)
        try:
            assert_no_open_store_descriptors(target_paths)
        except StoreAuthorityError as exc:
            raise StoreRestoreSafetyError("store descriptor safety proof failed") from exc
        _assert_unchanged(target, target_identities)
        os.replace(target, corrupt_path)
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            sidecar = Path(os.fspath(target) + suffix)
            if sidecar in target_identities:
                os.replace(sidecar, Path(os.fspath(corrupt_path) + suffix))
        os.replace(temp_path, target)
        _fsync_directory(target.parent)
        _verify_static_store(target, selected.logical_names)
        return StoreRestoreResult(
            logical_names=selected.logical_names,
            corrupt_path=os.fspath(corrupt_path),
            verification="ok",
        )
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _select_target(
    db_path: str | os.PathLike[str],
    logical_name: str,
    *,
    manifest_provider: StoreManifestProvider,
) -> _SelectedStore:
    manifest = manifest_provider(db_path)
    if logical_name not in manifest or logical_name not in STORE_SCHEMA_SENTINELS:
        raise StoreRestoreSelectionError("logical store is not registered")
    target = Path(manifest[logical_name].path)
    canonical_target = os.path.realpath(target)
    logical_names = tuple(
        candidate_name
        for candidate_name, candidate in manifest.items()
        if os.path.realpath(candidate.path) == canonical_target
    )
    if not logical_names or any(name not in STORE_SCHEMA_SENTINELS for name in logical_names):
        raise StoreRestoreSelectionError("manifest store cohort has no schema sentinel")
    root = Path(os.path.realpath(os.fspath(db_path))).parent
    if not _is_under_root(root, target):
        raise StoreRestoreSelectionError("manifest target is outside the daemon store root")
    try:
        path_stat = target.lstat()
    except FileNotFoundError as exc:
        raise StoreRestoreSelectionError("manifest target is absent") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise StoreRestoreSelectionError("manifest target is not a regular file")
    if os.path.realpath(target) != os.fspath(target):
        raise StoreRestoreSelectionError("manifest target does not retain its canonical identity")
    return _SelectedStore(target=target, logical_names=logical_names)


def _select_snapshot(snapshot: Path) -> Path:
    try:
        snapshot_stat = snapshot.lstat()
    except FileNotFoundError as exc:
        raise StoreRestoreSelectionError("snapshot is absent") from exc
    if stat.S_ISLNK(snapshot_stat.st_mode) or not stat.S_ISREG(snapshot_stat.st_mode):
        raise StoreRestoreSelectionError("snapshot is not a regular file")
    if os.path.realpath(snapshot) != os.path.abspath(snapshot):
        raise StoreRestoreSelectionError("snapshot does not retain its canonical identity")
    return Path(os.path.realpath(snapshot))


def _target_and_sidecars(target: Path) -> list[Path]:
    paths = [target]
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(os.fspath(target) + suffix)
        try:
            sidecar_stat = sidecar.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(sidecar_stat.st_mode) or not stat.S_ISREG(sidecar_stat.st_mode):
            raise StoreRestoreSelectionError("SQLite sidecar is not a regular file")
        paths.append(sidecar)
    return paths


def _capture_identities(paths: list[Path]) -> dict[Path, _PathIdentity]:
    return {path: _identity(path) for path in paths}


def _assert_unchanged(target: Path, expected: dict[Path, _PathIdentity]) -> None:
    current_paths = _target_and_sidecars(target)
    if set(current_paths) != set(expected):
        raise StoreRestoreSafetyError("SQLite sidecar set changed during restore")
    if any(_identity(path) != identity for path, identity in expected.items()):
        raise StoreRestoreSafetyError("selected store identity changed during restore")


def _identity(path: Path) -> _PathIdentity:
    path_stat = path.lstat()
    return _PathIdentity(path_stat.st_dev, path_stat.st_ino, path_stat.st_mode)


def _verify_static_store(path: Path, logical_names: tuple[str, ...]) -> None:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute("PRAGMA quick_check").fetchall()
            if rows != [("ok",)]:
                raise StoreRestoreVerificationError("snapshot quick_check failed")
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        raise StoreRestoreVerificationError("snapshot SQLite verification failed") from exc
    required = {
        table for logical_name in logical_names for table in STORE_SCHEMA_SENTINELS[logical_name]
    }
    if not required <= available:
        raise StoreRestoreVerificationError("snapshot does not match the selected store schema")


def _next_corrupt_path(target: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = target.with_name(f"{target.name}.corrupt-{timestamp}")
    for suffix in ("", *_SQLITE_SIDECAR_SUFFIXES):
        if os.path.lexists(os.fspath(candidate) + suffix):
            raise StoreRestoreSafetyError("corrupt preservation path already exists")
    return candidate


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _is_under_root(root: Path, path: Path) -> bool:
    try:
        return os.path.commonpath((os.fspath(root), os.fspath(path))) == os.fspath(root)
    except ValueError:
        return False
