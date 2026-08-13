"""Flag-gated per-agent Codex home preparation and rollout migration."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

PER_AGENT_CODEX_HOME_ENV = "PINKY_CODEX_PER_AGENT_HOME"
MANAGED_CONFIG_SENTINEL = "# pinkybot-managed-codex-home-v1"
ROLLOUT_MIGRATION_MARKER = ".pinkybot-rollout-migration-v1.json"
MIGRATION_LOCK_TIMEOUT_ENV = "PINKY_CODEX_HOME_LOCK_TIMEOUT_SECONDS"

_MIGRATION_TEMP_PREFIX = ".pinkybot-rollout-migration-"
_MIGRATION_TEMP_SUFFIX = ".tmp"
_MIGRATION_CLAIM_PREFIX = ".pinkybot-rollout-claim-"
_MIGRATION_QUARANTINE_PREFIX = ".pinkybot-rollout-quarantine-"
_MIGRATION_LOCK_NAME = ".pinkybot-rollout-migration.lock"
_MIGRATION_LOCK_ASIDE_PREFIX = ".pinkybot-rollout-lock-aside-"
_PRESERVATION_RESERVATION_SUFFIX = ".reserve"
_DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS = 10.0
_MIGRATION_LOCK_RETRY_SECONDS = 0.05

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class _RolloutCandidate:
    """A canonical rollout path and an optional crash-recovery claim."""

    canonical: Path
    claim: Path | None = None


@dataclass(frozen=True)
class _MoveResult:
    moved: int
    claimed_sources: frozenset[Path]


def per_agent_codex_home_enabled() -> bool:
    """Return whether per-agent Codex homes are explicitly enabled."""
    return os.environ.get(PER_AGENT_CODEX_HOME_ENV, "").strip() == "1"


def shared_codex_home() -> Path:
    """Return the user-level Codex home used when isolation is disabled."""
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _agent_working_dir(agent: object | None) -> Path:
    raw = (getattr(agent, "working_dir", "") or "").strip()
    if not raw:
        raise RuntimeError("per-agent Codex home requires an agent working directory")
    return Path(raw).expanduser().resolve()


def codex_home_for(agent: object | None = None) -> Path:
    """Resolve the Codex home shared by every reader for an agent scope.

    The feature is a strict production no-op while disabled: the daemon-level
    ``CODEX_HOME`` (or Codex's normal user-home fallback) is returned exactly as
    before. When enabled, an explicit agent override wins; otherwise the home
    lives inside the resolved agent working directory.
    """
    if not per_agent_codex_home_enabled():
        return shared_codex_home()

    override = (getattr(agent, "codex_home", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _agent_working_dir(agent) / ".codex"


def _managed_config(working_dir: Path) -> str:
    project_key = json.dumps(str(working_dir))
    return (
        f"{MANAGED_CONFIG_SENTINEL}\n"
        "# Generated for an isolated agent session.\n\n"
        "[features]\n"
        "apps = false\n"
        "plugins = false\n\n"
        f"[projects.{project_key}]\n"
        'trust_level = "trusted"\n'
    )


def _write_managed_config(codex_home: Path, working_dir: Path, log: LogFn) -> None:
    config_path = codex_home / "config.toml"
    expected = _managed_config(working_dir)
    try:
        config_stat = config_path.lstat()
    except FileNotFoundError:
        config_stat = None
    if config_stat is not None:
        # Check the directory entry itself before any target-following call.
        # Path.exists() is false for a dangling symlink; checking it first
        # would let write_text() follow the link and create/mutate its target.
        if stat.S_ISLNK(config_stat.st_mode) or config_stat.st_nlink > 1:
            log(
                f"codex-home: preserving linked config at {config_path}; "
                "per-agent safety settings were not rewritten"
            )
            return
        if not stat.S_ISREG(config_stat.st_mode):
            log(
                f"codex-home: preserving non-regular config at {config_path}; "
                "per-agent safety settings were not rewritten"
            )
            return
        existing = config_path.read_text(encoding="utf-8")
        if not existing.startswith(MANAGED_CONFIG_SENTINEL):
            log(
                f"codex-home: preserving unmanaged config at {config_path}; "
                "per-agent safety settings were not rewritten"
            )
            return
        if existing == expected:
            return
    config_path.write_text(expected, encoding="utf-8")
    config_path.chmod(0o600)


def _ensure_auth_link(codex_home: Path, auth_source: Path) -> None:
    auth_path = codex_home / "auth.json"
    auth_target = auth_source.resolve(strict=True)
    for _attempt in range(3):
        try:
            auth_stat = auth_path.lstat()
        except FileNotFoundError:
            try:
                auth_path.symlink_to(auth_target)
                return
            except FileExistsError:
                # A concurrent prepare created the entry; classify it again.
                continue
        if not stat.S_ISLNK(auth_stat.st_mode):
            raise RuntimeError(
                f"refusing Codex spawn: {auth_path} is not the managed auth symlink"
            )
        try:
            if auth_path.resolve(strict=True) == auth_target:
                return
        except OSError:
            pass
        try:
            auth_path.unlink()
        except FileNotFoundError:
            # A concurrent prepare already replaced the stale/dangling entry.
            continue
    raise RuntimeError(f"refusing Codex spawn: auth symlink at {auth_path} did not converge")


def _rollout_cwd(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload") or {}
                cwd = payload.get("cwd")
                return cwd if isinstance(cwd, str) else ""
    except (OSError, UnicodeError):
        return ""
    return ""


def _path_entry_exists(path: Path) -> bool:
    """Return whether a directory entry exists without following symlinks."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _file_signature(path: Path) -> tuple[int, str]:
    """Return an exact size+digest signature for a regular rollout file."""
    file_stat = path.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise OSError(f"rollout is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _rollout_is_valid(
    path: Path,
    target_cwd: str,
    *,
    expected_signature: tuple[int, str] | None = None,
) -> bool:
    """Validate a migration final without trusting its name alone."""
    try:
        rollout_cwd = _rollout_cwd(path)
        if not rollout_cwd or os.path.realpath(rollout_cwd) != target_cwd:
            return False
        signature = _file_signature(path)
    except OSError:
        return False
    return expected_signature is None or signature == expected_signature


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes after replace/unlink operations."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _migration_lock_timeout_seconds() -> float:
    """Return the bounded destination-lock acquisition window."""
    raw = os.environ.get(MIGRATION_LOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid {MIGRATION_LOCK_TIMEOUT_ENV}: {raw!r}") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError(f"invalid {MIGRATION_LOCK_TIMEOUT_ENV}: {raw!r}")
    return timeout


def _migration_lock_entry_kind(entry_stat: os.stat_result) -> str:
    if stat.S_ISLNK(entry_stat.st_mode) or entry_stat.st_nlink > 1:
        return "linked"
    if not stat.S_ISREG(entry_stat.st_mode):
        return "non-regular"
    return ""


@contextmanager
def _reserve_preservation_destination(
    source: Path,
    name_prefix: str,
) -> Iterator[Path]:
    """Reserve a collision-free preservation name without replacing data.

    Every attempt gets a fresh UUID and an adjacent O_EXCL reservation.  A
    pre-existing preservation artifact or reservation loses that attempt and
    forces a new name.  The reservation serializes cooperating preservers while
    ``os.rename`` moves the entry to a path proven unused; unlike ``os.replace``,
    the move can never intentionally overwrite a retained artifact.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec
    while True:
        destination = source.with_name(
            f"{name_prefix}{source.name}-{uuid.uuid4().hex}"
        )
        reservation = destination.with_name(
            f".{destination.name}{_PRESERVATION_RESERVATION_SUFFIX}"
        )
        try:
            reservation_fd = os.open(reservation, flags, 0o600)
        except FileExistsError:
            continue
        os.close(reservation_fd)
        if _path_entry_exists(destination):
            reservation.unlink()
            continue
        try:
            yield destination
        finally:
            try:
                reservation.unlink()
            except FileNotFoundError:
                pass
        return


def _rename_migration_lock_aside(
    lock_path: Path,
    entry_stat: os.stat_result,
    kind: str,
    log: LogFn,
) -> bool:
    """Preserve a rejected lock entry without following it."""
    try:
        current_stat = lock_path.lstat()
    except FileNotFoundError:
        return False
    if (current_stat.st_dev, current_stat.st_ino) != (
        entry_stat.st_dev,
        entry_stat.st_ino,
    ):
        return False
    with _reserve_preservation_destination(
        lock_path,
        _MIGRATION_LOCK_ASIDE_PREFIX,
    ) as aside:
        os.rename(lock_path, aside)
    _fsync_directory(lock_path.parent)
    log(f"codex-home: renamed {kind} migration lock entry {lock_path} aside as {aside}")
    return True


def _open_migration_lock(lock_path: Path, log: LogFn) -> BinaryIO:
    """Open a stable regular lock entry without following links."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("migration lock path resolution requires O_NOFOLLOW")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)

    for _attempt in range(8):
        try:
            entry_stat = lock_path.lstat()
        except FileNotFoundError:
            entry_stat = None
        if entry_stat is not None:
            kind = _migration_lock_entry_kind(entry_stat)
            if kind:
                _rename_migration_lock_aside(lock_path, entry_stat, kind, log)
                continue

        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
                continue
            raise

        try:
            opened_stat = os.fstat(fd)
            current_stat = lock_path.lstat()
            kind = _migration_lock_entry_kind(opened_stat)
            same_entry = (opened_stat.st_dev, opened_stat.st_ino) == (
                current_stat.st_dev,
                current_stat.st_ino,
            )
            if not kind and same_entry:
                return os.fdopen(fd, "a+b")
        except FileNotFoundError:
            kind = ""
            current_stat = None
            same_entry = False
        except BaseException:
            os.close(fd)
            raise

        os.close(fd)
        if kind and current_stat is not None and same_entry:
            _rename_migration_lock_aside(lock_path, current_stat, kind, log)

    raise RuntimeError(f"migration lock path resolution did not converge: {lock_path}")


@contextmanager
def _acquire_migration_lock(lock_path: Path, log: LogFn) -> Iterator[BinaryIO]:
    """Acquire the destination writer lock within a fixed wall-clock bound."""
    timeout = _migration_lock_timeout_seconds()
    deadline = time.monotonic() + timeout
    lock_handle = _open_migration_lock(lock_path, log)
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(
                    lock_handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    message = (
                        "codex-home: migration lock deadline exceeded after "
                        f"{timeout:g}s at {lock_path}; refusing rollout migration"
                    )
                    log(message)
                    raise RuntimeError(message)
                time.sleep(min(_MIGRATION_LOCK_RETRY_SECONDS, remaining))
        yield lock_handle
    finally:
        if acquired:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def _sweep_migration_temps(destination_sessions: Path, log: LogFn) -> None:
    """Remove private copy temps abandoned before an atomic install."""
    if not destination_sessions.exists():
        return
    pattern = f"{_MIGRATION_TEMP_PREFIX}*{_MIGRATION_TEMP_SUFFIX}"
    for abandoned in destination_sessions.glob(f"**/{pattern}"):
        try:
            abandoned.unlink()
            log(f"codex-home: removed abandoned rollout migration temp {abandoned}")
        except FileNotFoundError:
            continue


def _quarantine_rollout(destination: Path, log: LogFn) -> Path:
    """Atomically move an invalid final aside so it cannot shadow discovery."""
    with _reserve_preservation_destination(
        destination,
        _MIGRATION_QUARANTINE_PREFIX,
    ) as quarantine:
        os.rename(destination, quarantine)
    _fsync_directory(destination.parent)
    log(f"codex-home: quarantined invalid rollout migration final {destination} as {quarantine}")
    return quarantine


def _install_rollout(
    claim: Path,
    destination: Path,
    target_cwd: str,
    *,
    log: LogFn,
) -> bool:
    """Install one rollout with crash recovery and concurrent convergence.

    ``claim`` is a private name established by atomically renaming the canonical
    source before this function is entered.  All reads, verification, and
    cleanup stay on that private name.  A writer arriving after the rename must
    create a fresh canonical file, which this migration never removes and a
    later prepare can migrate independently.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    if _path_entry_exists(destination):
        try:
            claim_signature = _file_signature(claim)
        except FileNotFoundError:
            if _rollout_is_valid(destination, target_cwd):
                log(
                    f"codex-home: rollout migration converged at {destination}; "
                    "private claim was already consumed"
                )
                return True
            raise
        if _rollout_is_valid(
            destination,
            target_cwd,
            expected_signature=claim_signature,
        ):
            try:
                claim.unlink()
                _fsync_directory(claim.parent)
            except FileNotFoundError:
                pass
            log(f"codex-home: rollout migration converged at {destination}")
            return True
        _quarantine_rollout(destination, log)

    temp_fd, temp_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=_MIGRATION_TEMP_PREFIX,
        suffix=_MIGRATION_TEMP_SUFFIX,
    )
    temp_path = Path(temp_name)
    try:
        copied_digest = hashlib.sha256()
        copied_size = 0
        with claim.open("rb") as source_handle, os.fdopen(temp_fd, "wb") as temp_handle:
            temp_fd = -1
            while chunk := source_handle.read(1024 * 1024):
                temp_handle.write(chunk)
                copied_digest.update(chunk)
                copied_size += len(chunk)
            temp_handle.flush()
            os.fsync(temp_handle.fileno())
        copied_signature = (copied_size, copied_digest.hexdigest())

        # A descriptor opened before the claim rename can still change the
        # claimed inode.  Detect that shape before install and leave the claim
        # intact for retry; name-based late writers use the fresh canonical path.
        if _file_signature(claim) != copied_signature:
            raise RuntimeError(f"rollout claim changed during migration: {claim}")
        if not _rollout_is_valid(
            temp_path,
            target_cwd,
            expected_signature=copied_signature,
        ):
            raise RuntimeError(f"rollout migration temp failed validation: {temp_path}")

        os.replace(temp_path, destination)
        _fsync_directory(destination.parent)
        if not _rollout_is_valid(
            destination,
            target_cwd,
            expected_signature=copied_signature,
        ):
            _quarantine_rollout(destination, log)
            raise RuntimeError(f"rollout migration final failed validation: {destination}")

        # Removal targets only the private claim.  There is deliberately no
        # check-then-remove pair on the canonical writer path.
        claim.unlink()
        _fsync_directory(claim.parent)
        return True
    except FileNotFoundError:
        # A crash-recovery claim may already have been consumed by a concurrent
        # process. Count a valid final as convergence instead of raising.
        if _rollout_is_valid(destination, target_cwd):
            log(
                f"codex-home: rollout migration converged at {destination}; "
                "private claim was already consumed"
            )
            return True
        raise
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _claim_original_path(claim: Path) -> Path | None:
    """Decode the canonical source path represented by a private claim."""
    if not claim.name.startswith(_MIGRATION_CLAIM_PREFIX):
        return None
    encoded = claim.name[len(_MIGRATION_CLAIM_PREFIX) :]
    claim_id, separator, original_name = encoded.partition("-")
    if (
        not separator
        or len(claim_id) != 32
        or any(character not in "0123456789abcdef" for character in claim_id)
        or not original_name.startswith("rollout-")
        or not original_name.endswith(".jsonl")
    ):
        return None
    return claim.with_name(original_name)


def _claim_rollout(source: Path) -> Path | None:
    """Atomically remove the canonical writer target before migration reads."""
    while True:
        claim = source.with_name(
            f"{_MIGRATION_CLAIM_PREFIX}{uuid.uuid4().hex}-{source.name}"
        )
        if _path_entry_exists(claim):
            continue
        try:
            os.rename(source, claim)
        except FileNotFoundError:
            return None
        _fsync_directory(source.parent)
        return claim


def _snapshot_rollout_candidates(source_sessions: Path) -> list[_RolloutCandidate]:
    """Return recoverable claims first, then live canonical rollout names."""
    claims: list[_RolloutCandidate] = []
    claim_pattern = f"**/{_MIGRATION_CLAIM_PREFIX}*-rollout-*.jsonl"
    for claim in sorted(source_sessions.glob(claim_pattern)):
        canonical = _claim_original_path(claim)
        if canonical is not None:
            claims.append(_RolloutCandidate(canonical=canonical, claim=claim))
    canonical = [
        _RolloutCandidate(path)
        for path in sorted(source_sessions.glob("**/rollout-*.jsonl"))
    ]
    return [*claims, *canonical]


def _retry_rollout_candidates(
    source_sessions: Path,
    relative_sources: list[str],
) -> list[_RolloutCandidate]:
    """Resolve marker-pinned canonical paths and their private crash claims."""
    candidates: list[_RolloutCandidate] = []
    seen: set[tuple[Path, Path | None]] = set()
    for raw_relative in relative_sources:
        relative = Path(raw_relative)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.name.startswith("rollout-")
            or not relative.name.endswith(".jsonl")
        ):
            continue
        canonical = source_sessions / relative
        claim_pattern = f"{_MIGRATION_CLAIM_PREFIX}*-{canonical.name}"
        for claim in sorted(canonical.parent.glob(claim_pattern)):
            if _claim_original_path(claim) == canonical:
                key = (canonical, claim)
                if key not in seen:
                    candidates.append(_RolloutCandidate(canonical, claim))
                    seen.add(key)
        if _path_entry_exists(canonical):
            key = (canonical, None)
            if key not in seen:
                candidates.append(_RolloutCandidate(canonical))
                seen.add(key)
    return candidates


def move_matching_rollouts(
    source_home: Path,
    destination_home: Path,
    working_dir: str | Path,
    *,
    log: LogFn,
) -> int:
    """Move rollouts for one resolved working directory between Codex homes."""
    source_sessions = source_home / "sessions"
    destination_sessions = destination_home / "sessions"
    target_cwd = os.path.realpath(str(working_dir))
    # Snapshot before locking so a contender that saw the same source before
    # the winner claimed it can still converge on the installed final. Private
    # crash claims lead the list so a newer canonical recreation wins last.
    candidates = _snapshot_rollout_candidates(source_sessions)
    destination_sessions.mkdir(parents=True, exist_ok=True)
    lock_path = destination_sessions / _MIGRATION_LOCK_NAME
    with _acquire_migration_lock(lock_path, log):
        # Serializing destination writers makes temp cleanup race-free and
        # works across daemon processes. The kernel releases flock on crash,
        # after which the successor sweeps the abandoned private temp.
        _sweep_migration_temps(destination_sessions, log)
        return _move_rollout_candidates(
            candidates,
            source_sessions,
            destination_sessions,
            target_cwd,
            log=log,
        ).moved


def _move_rollout_candidates(
    candidates: list[_RolloutCandidate],
    source_sessions: Path,
    destination_sessions: Path,
    target_cwd: str,
    *,
    log: LogFn,
) -> _MoveResult:
    """Move a pre-lock snapshot while holding the destination writer lock."""
    moved = 0
    claimed_sources: set[Path] = set()
    for candidate in candidates:
        source = candidate.canonical
        relative = source.relative_to(source_sessions)
        destination = destination_sessions / relative
        inspection_path = candidate.claim or source
        rollout_cwd = _rollout_cwd(inspection_path)
        if not rollout_cwd:
            if not _path_entry_exists(inspection_path) and _rollout_is_valid(
                destination, target_cwd
            ):
                log(
                    f"codex-home: rollout migration converged at {destination}; "
                    "source was already claimed"
                )
                moved += 1
            continue
        if os.path.realpath(rollout_cwd) != target_cwd:
            continue
        claim = candidate.claim or _claim_rollout(source)
        if claim is None:
            if _rollout_is_valid(destination, target_cwd):
                log(
                    f"codex-home: rollout migration converged at {destination}; "
                    "source was already claimed"
                )
                moved += 1
            continue
        claimed_sources.add(source)
        if _install_rollout(claim, destination, target_cwd, log=log):
            moved += 1
    return _MoveResult(moved=moved, claimed_sources=frozenset(claimed_sources))


def _write_migration_marker(
    codex_home: Path,
    moved: int,
    *,
    retry_sources: list[str],
) -> None:
    """Durably commit the one-time migration gate after a successful scan."""
    marker = codex_home / ROLLOUT_MIGRATION_MARKER
    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "moved_count": moved,
        # Exact paths claimed during the one-time bridge remain eligible for a
        # bounded retry. A writer recreating one of those canonical names after
        # the atomic claim is therefore migrated on the next prepare without
        # reopening the shared store to arbitrary late rollouts.
        "retry_sources": retry_sources,
    }
    fd, temp_name = tempfile.mkstemp(
        dir=codex_home,
        prefix=f".{ROLLOUT_MIGRATION_MARKER}-",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, marker)
        _fsync_directory(codex_home)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _read_marker_retry_sources(marker: Path) -> list[str]:
    """Read retry paths from a managed marker; malformed markers fail closed."""
    try:
        marker_stat = marker.lstat()
        if not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_nlink > 1:
            return []
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    raw_sources = payload.get("retry_sources")
    if not isinstance(raw_sources, list):
        return []
    return [source for source in raw_sources if isinstance(source, str)]


def _run_initial_rollout_migration(
    shared_home: Path,
    resolved_home: Path,
    working_dir: Path,
    *,
    log: LogFn,
) -> int:
    """Run and durably gate the shared-store import exactly once.

    The fast marker check happens before the shared directory is scanned. A
    contender that began during the first migration may have already captured
    the same source list; the destination lock lets it observe the committed
    marker and count the winner's validated finals without touching shared
    storage again. The marker is written before the winner releases that lock,
    closing the scan-to-marker race between concurrent spawns.
    """
    marker = resolved_home / ROLLOUT_MIGRATION_MARKER
    if _path_entry_exists(marker):
        retry_sources = _read_marker_retry_sources(marker)
        if not retry_sources:
            log(
                f"codex-home: migration marker present at {marker}; "
                "shared rollout store not scanned"
            )
            return 0
        source_sessions = shared_home / "sessions"
        destination_sessions = resolved_home / "sessions"
        candidates = _retry_rollout_candidates(source_sessions, retry_sources)
        if not candidates:
            log(
                f"codex-home: migration marker present at {marker}; "
                "no claimed source path requires retry"
            )
            return 0
        target_cwd = os.path.realpath(str(working_dir))
        lock_path = destination_sessions / _MIGRATION_LOCK_NAME
        with _acquire_migration_lock(lock_path, log):
            _sweep_migration_temps(destination_sessions, log)
            result = _move_rollout_candidates(
                candidates,
                source_sessions,
                destination_sessions,
                target_cwd,
                log=log,
            )
        log(
            f"codex-home: migration marker retry converged on "
            f"{result.moved} rollout(s)"
        )
        return result.moved

    source_sessions = shared_home / "sessions"
    destination_sessions = resolved_home / "sessions"
    candidates = _snapshot_rollout_candidates(source_sessions)
    target_cwd = os.path.realpath(str(working_dir))
    lock_path = destination_sessions / _MIGRATION_LOCK_NAME
    with _acquire_migration_lock(lock_path, log):
        if _path_entry_exists(marker):
            converged = sum(
                _rollout_is_valid(
                    destination_sessions
                    / candidate.canonical.relative_to(source_sessions),
                    target_cwd,
                )
                for candidate in candidates
            )
            log(
                f"codex-home: concurrent rollout migration converged on "
                f"{converged} installed rollout(s); marker already committed"
            )
            return converged
        _sweep_migration_temps(destination_sessions, log)
        result = _move_rollout_candidates(
            candidates,
            source_sessions,
            destination_sessions,
            target_cwd,
            log=log,
        )
        retry_sources = sorted(
            str(source.relative_to(source_sessions))
            for source in result.claimed_sources
        )
        _write_migration_marker(
            resolved_home,
            result.moved,
            retry_sources=retry_sources,
        )
        return result.moved


def prepare_agent_codex_home(agent: object, *, log: LogFn) -> Path:
    """Prepare the isolated home before a Codex process is spawned.

    With the flag disabled this function performs no filesystem work. With it
    enabled, auth absence is a hard error before launch, config generation is
    non-destructive, and cwd-matched rollouts move into the isolated store.
    """
    codex_home = codex_home_for(agent)
    if not per_agent_codex_home_enabled():
        return codex_home

    working_dir = _agent_working_dir(agent)
    shared_home = shared_codex_home().expanduser().resolve()
    resolved_home = codex_home.expanduser().resolve()
    if resolved_home == shared_home:
        raise RuntimeError("refusing Codex spawn: per-agent CODEX_HOME resolves to the shared home")

    auth_source = shared_home / "auth.json"
    if not auth_source.is_file() or not os.access(auth_source, os.R_OK):
        raise RuntimeError(
            f"refusing Codex spawn: shared auth file is absent or unreadable: {auth_source}"
        )

    resolved_home.mkdir(parents=True, exist_ok=True)
    resolved_home.chmod(0o700)
    sessions = resolved_home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    sessions.chmod(0o700)
    _write_managed_config(resolved_home, working_dir, log)
    _ensure_auth_link(resolved_home, auth_source)

    moved = _run_initial_rollout_migration(
        shared_home,
        resolved_home,
        working_dir,
        log=log,
    )
    log(f"codex-home: migrated {moved} cwd-matched rollout(s) into {resolved_home / 'sessions'}")
    return resolved_home


def rollback_agent_rollouts(
    working_dir: str | Path,
    agent_home: str | Path,
    *,
    shared_home: str | Path | None = None,
    log: LogFn,
) -> int:
    """Move one agent's rollouts back to the user-level Codex store."""
    source = Path(agent_home).expanduser().resolve()
    destination = (
        Path(shared_home).expanduser().resolve()
        if shared_home is not None
        else shared_codex_home().expanduser().resolve()
    )
    moved = move_matching_rollouts(
        source,
        destination,
        working_dir,
        log=log,
    )
    log(f"codex-home: rolled back {moved} cwd-matched rollout(s) into {destination / 'sessions'}")
    return moved
