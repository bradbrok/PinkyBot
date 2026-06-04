"""Filesystem hardening for the daemon's SQLite databases.

The SQLite files under ``data/`` hold plaintext secrets — bot tokens,
per-agent signing keys, user profiles, encrypted keypairs, token hashes.
They are created with the process umask (commonly ``0644``, i.e.
world-readable), which on a shared host exposes those secrets to any
local reader. This module locks each database to owner-only (``0600``),
along with its SQLite ``-wal`` / ``-shm`` / ``-journal`` sidecars (which
can mirror recently-written pages, secrets included), and tightens the
``identity/`` directory holding the crown-jewel stores to ``0700``.

The sweep is idempotent and cheap (a ``stat`` per file, a ``chmod`` only
when the mode actually differs), so it runs once on daemon startup. It
is best-effort: a file that has vanished or can't be chmod'd is logged
and skipped rather than aborting startup. Symlinks are never followed.

Mirrors the ``SECRET_MODE`` / ``DIR_MODE`` precedent in
:mod:`pinky_daemon.provisioning`.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger("pinky.db_security")

# Owner read/write only.
DB_FILE_MODE = 0o600
# Owner read/write/execute only — for directories holding secret DBs.
SECRET_DIR_MODE = 0o700

# SQLite sidecars that can contain copies of DB page data (incl. secrets).
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _chmod_if_needed(path: Path, mode: int) -> int:
    """chmod ``path`` to ``mode`` only if it differs. Best-effort.

    Returns 1 if the mode was changed, else 0. Missing files, symlinks,
    and chmod failures are skipped (failures logged at debug) so this
    never raises into startup or a hot path.
    """
    try:
        st = path.lstat()
    except OSError:
        return 0
    # Never chmod through a symlink — it would retarget the link's victim.
    if stat.S_ISLNK(st.st_mode):
        return 0
    if stat.S_IMODE(st.st_mode) == mode:
        return 0
    try:
        os.chmod(path, mode)
        return 1
    except OSError as exc:
        logger.debug("db_security: could not chmod %s to %o: %s", path, mode, exc)
        return 0


def harden_db_file(db_path: str | Path) -> int:
    """Lock ``db_path`` and its SQLite sidecars to ``0600`` (best-effort).

    Returns the number of files whose mode was changed. Safe to call from
    a store's ``__init__`` right after the DB is opened — idempotent and
    silent on already-correct or absent files.
    """
    base = Path(db_path)
    changed = _chmod_if_needed(base, DB_FILE_MODE)
    for sfx in _SIDECAR_SUFFIXES:
        changed += _chmod_if_needed(base.with_name(base.name + sfx), DB_FILE_MODE)
    return changed


def sweep_db_permissions(data_dir: str | Path) -> int:
    """Lock every SQLite DB under ``data_dir`` to ``0600`` + sidecars.

    Also tightens the ``identity/`` subdirectory (encrypted signing keys,
    bearer-token hashes) to ``0700`` when present. Idempotent and
    best-effort; intended to run once on daemon startup, after the stores
    have created their files. Returns the count of files/dirs chmod'd.
    """
    root = Path(data_dir)
    if not root.is_dir():
        return 0
    changed = 0
    for db in root.rglob("*.db"):
        changed += harden_db_file(db)
    identity_dir = root / "identity"
    if identity_dir.is_dir():
        changed += _chmod_if_needed(identity_dir, SECRET_DIR_MODE)
    if changed:
        logger.info("db_security: hardened %d file(s)/dir(s) under %s", changed, root)
    return changed
