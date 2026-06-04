"""Owner-only filesystem hardening for SQLite secret stores.

Stand-alone (stdlib only) so the identity stores stay free of daemon /
HTTP deps. Shared by both the daemon's startup sweep
(:mod:`pinky_daemon.db_security`) and the identity stores at init time,
so the no-follow semantics live in exactly one place.

The chmod is done through an ``O_NOFOLLOW`` file descriptor and
``fchmod`` rather than a path-based ``os.chmod``: if the final path
component is a symlink the open fails (``ELOOP``), so we never chmod a
link's victim, and operating on the fd removes the ``lstat``->``chmod``
TOCTOU race (the mode is read and set on the same open handle).

Best-effort throughout: missing files, symlinks, non-regular files, and
platforms lacking ``O_NOFOLLOW`` / ``fchmod`` are skipped without
raising. On a platform that can't do it safely we harden *nothing*
rather than risk following a link — defense-in-depth must never make
things worse.

Scope: ``O_NOFOLLOW`` guards only the **final** path component — the
file or directory being hardened. Intermediate/parent directories are
trusted: they are part of the daemon-owned data tree, and the startup
sweep canonicalizes its root before walking. Planting a symlink *above*
the leaf requires write access to that tree, at which point the secrets
are already exposed — outside this module's threat model (passive local
readers on a shared host).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

#: Owner read/write only — for files holding secret material.
SECRET_FILE_MODE = 0o600
#: Owner read/write/execute only — for directories holding secret stores.
SECRET_DIR_MODE = 0o700

#: A SQLite DB plus the sidecars that can mirror its page data (the empty
#: suffix is the main file itself). ``-wal`` / ``-shm`` / ``-journal`` can
#: all contain recently-written pages, secrets included.
_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")

# O_NOFOLLOW: open fails with ELOOP if the final component is a symlink.
# O_NONBLOCK: never block opening a FIFO/device that shouldn't be here.
# O_DIRECTORY: open fails if the target isn't a directory (for dir mode).
# Each falls back to 0 if the platform lacks it.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

# We only act when we can do it SAFELY: fchmod to operate on the fd, and a
# real O_NOFOLLOW so the open can't traverse a symlink. Missing either
# (e.g. Windows) → skip hardening entirely. PinkyBot runs on macOS/Linux,
# where both are present.
_CAN_HARDEN = hasattr(os, "fchmod") and bool(_NOFOLLOW)


def _fchmod_nofollow(path: Path, mode: int, *, want_dir: bool) -> int:
    """chmod ``path`` to ``mode`` via an ``O_NOFOLLOW`` fd. Best-effort.

    Returns 1 if the mode was changed, else 0. Skips (returns 0) on: a
    platform that can't harden safely, a missing path, a symlink at the
    target, a file/dir type mismatch, an already-correct mode, or any
    ``OSError``.
    """
    if not _CAN_HARDEN:
        return 0
    flags = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | (_DIRECTORY if want_dir else 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        # Missing, a symlink (ELOOP), or not a directory when one was required.
        return 0
    try:
        st = os.fstat(fd)
        if want_dir != stat.S_ISDIR(st.st_mode):
            return 0
        if not want_dir and not stat.S_ISREG(st.st_mode):
            return 0  # only regular files — skip FIFOs/devices/etc.
        if stat.S_IMODE(st.st_mode) == mode:
            return 0
        os.fchmod(fd, mode)
        return 1
    except OSError:
        return 0
    finally:
        os.close(fd)


def harden_secret_file(db_path: str | Path) -> int:
    """Lock a SQLite DB file and its sidecars to ``0600``.

    Safe to call from a store's ``__init__`` right after the DB is opened
    — idempotent, never opens the target through a final-component
    symlink, never raises. Returns the number of files whose mode was
    changed.
    """
    base = Path(db_path)
    return sum(
        _fchmod_nofollow(base.with_name(base.name + sfx), SECRET_FILE_MODE, want_dir=False)
        for sfx in _SIDECAR_SUFFIXES
    )


def harden_secret_dir(dir_path: str | Path) -> int:
    """Lock a directory holding secret stores to ``0700``. Returns 1 if changed."""
    return _fchmod_nofollow(Path(dir_path), SECRET_DIR_MODE, want_dir=True)
