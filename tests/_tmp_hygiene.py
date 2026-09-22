"""Repair deliberately restricted test artifacts without following symlinks."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def restore_tree_readability(root: Path) -> int:
    """Restore owner read/access top-down, returning the number of changed entries."""
    changed = 0
    pending = [root]
    while pending:
        path = pending.pop()
        mode = path.lstat().st_mode
        is_directory = stat.S_ISDIR(mode)
        required = stat.S_IRUSR | stat.S_IXUSR if is_directory else stat.S_IRUSR
        if mode & required != required:
            repaired = stat.S_IMODE(mode) | (0o700 if is_directory else 0o600)
            if stat.S_ISLNK(mode) and hasattr(os, "lchmod"):
                os.lchmod(path, repaired)
            else:
                os.chmod(path, repaired, follow_symlinks=False)
            changed += 1
        if is_directory:
            # Repair before listing, including root itself. lstat above keeps
            # directory symlinks (and dangling links) out of the traversal.
            pending.extend(path.iterdir())
    return changed
