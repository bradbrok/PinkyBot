"""Test-owned short socket roots, isolated from the production /tmp backlog."""

import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import codex_app_server_tmux as mod


@pytest.fixture
def codex_socket_sandbox(monkeypatch):
    original = tempfile.mkdtemp
    root = Path(original(prefix="k1380-", dir=os.environ.get("K1380_SOCKET_BASE", "/tmp")))
    identity = root.lstat()
    created = []

    def owned_mkdtemp(suffix=None, prefix=None, dir=None):
        if dir == "/tmp" and prefix and prefix.startswith("pinky-codex-as-"):
            directory = original(suffix=suffix, prefix=prefix, dir=str(root))
            created.append(Path(directory))
            return directory
        return original(suffix=suffix, prefix=prefix, dir=dir)

    monkeypatch.setattr(mod.tempfile, "mkdtemp", owned_mkdtemp)
    try:
        yield SimpleNamespace(root=root, created=created)
    finally:
        current = root.lstat()
        assert stat.S_ISDIR(current.st_mode)
        assert (current.st_uid, current.st_dev, current.st_ino) == (
            os.getuid(), identity.st_dev, identity.st_ino,
        )
        shutil.rmtree(root)
