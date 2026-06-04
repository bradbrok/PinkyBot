"""Filesystem hardening for the daemon's SQLite databases.

The SQLite files under ``data/`` hold plaintext secrets — bot tokens,
per-agent signing keys, user profiles, encrypted keypairs, token hashes.
They are created with the process umask (commonly ``0644``, i.e.
world-readable), which on a shared host exposes those secrets to any
local reader. This module locks each database to owner-only (``0600``),
along with its SQLite ``-wal`` / ``-shm`` / ``-journal`` sidecars, and
tightens the ``identity/`` directory holding the crown-jewel stores to
``0700``.

The actual chmod is delegated to :mod:`pinky_identity.fs_security`, which
does it through an ``O_NOFOLLOW`` fd so a final-component symlink is never
followed (leaf-only — see that module for the precise scope and threat
model) and there is no ``lstat``->``chmod`` race. The sweep here just
enumerates, canonicalizing its root first; it
is idempotent and cheap, so it runs once on daemon startup. Best-effort —
a file that has vanished or can't be chmod'd is skipped rather than
aborting startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pinky_identity.fs_security import (
    SECRET_DIR_MODE,
    SECRET_FILE_MODE,
    harden_secret_dir,
    harden_secret_file,
)

logger = logging.getLogger("pinky.db_security")

# Back-compat aliases for daemon-side callers/tests. SECRET_DIR_MODE and
# harden_secret_* are re-exported via the import above.
DB_FILE_MODE = SECRET_FILE_MODE
harden_db_file = harden_secret_file

__all__ = [
    "DB_FILE_MODE",
    "SECRET_DIR_MODE",
    "SECRET_FILE_MODE",
    "harden_db_file",
    "harden_secret_dir",
    "harden_secret_file",
    "sweep_db_permissions",
]


def sweep_db_permissions(data_dir: str | Path) -> int:
    """Lock every SQLite DB under ``data_dir`` to ``0600`` + sidecars.

    Also tightens the ``identity/`` subdirectory (encrypted signing keys,
    bearer-token hashes) to ``0700`` when present. Idempotent and
    best-effort; intended to run once on daemon startup, after the stores
    have created their files. Returns the count of files/dirs chmod'd.
    """
    # Canonicalize the root so a non-resolved/symlinked root argument can't
    # steer the sweep; O_NOFOLLOW then guards each leaf below it.
    root = Path(data_dir).resolve()
    if not root.is_dir():
        return 0
    changed = 0
    for db in root.rglob("*.db"):
        changed += harden_secret_file(db)
    identity_dir = root / "identity"
    if identity_dir.is_dir():
        changed += harden_secret_dir(identity_dir)
    if changed:
        # Log that the sweep acted, but pass no values: the count flows out
        # of harden_secret_*(), and the scanner's sensitive-data heuristic
        # keys on "secret" in the name and false-flags logging it.
        logger.info("db_security: tightened SQLite file permissions on startup")
    return changed
