"""Tests for POST /admin/update — covers force flag behavior."""

from __future__ import annotations

import os
import subprocess as sp
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient


def _make_client():
    from pinky_daemon.api import create_api
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
    return TestClient(app)


class _GitMock:
    """Programmable subprocess.check_output side_effect for git commands.

    Records every call so tests can assert on order/presence.
    """

    def __init__(
        self,
        *,
        dirty_files: list[str] | None = None,
        before_hash: str = "abc1234",
        after_hash: str = "def5678",
        branch: str = "beta",
    ):
        self.calls: list[list[str]] = []
        self.dirty_files = dirty_files or []
        self.before_hash = before_hash
        self.after_hash = after_hash
        self.branch = branch
        self._hash_call = 0

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))

        # git rev-parse --short HEAD — first call returns before, second returns after
        if cmd[:3] == ["git", "rev-parse", "--short"]:
            self._hash_call += 1
            val = self.before_hash if self._hash_call == 1 else self.after_hash
            return (val + "\n").encode()

        # git describe --tags --exact-match HEAD — pretend untagged
        if cmd[:2] == ["git", "describe"]:
            raise sp.CalledProcessError(128, cmd, output=b"")

        # git fetch origin --tags <branch>
        if cmd[:3] == ["git", "fetch", "origin"]:
            return b""

        # git tag --sort=... -l ... (only for main / use_release_tags)
        if cmd[:2] == ["git", "tag"]:
            return b""

        # git rev-parse --is-shallow-repository
        if cmd[:3] == ["git", "rev-parse", "--is-shallow-repository"]:
            return b"false\n"

        # git rev-parse --abbrev-ref HEAD
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return (self.branch + "\n").encode()

        # git diff --name-only HEAD  (used by force-mode dirty check)
        if cmd[:4] == ["git", "diff", "--name-only", "HEAD"]:
            joined = "\n".join(self.dirty_files)
            if joined:
                joined += "\n"
            return joined.encode()

        # git checkout -- .  (force reset of tracked files)
        if cmd[:3] == ["git", "checkout", "--"]:
            return b""

        # git checkout <branch>  (used when on detached HEAD / wrong branch)
        if cmd[:2] == ["git", "checkout"]:
            return b""

        # git pull origin <branch>
        if cmd[:3] == ["git", "pull", "origin"]:
            return b""

        # git log --oneline ...  (commit summary or dry-run pending list)
        if cmd[:3] == ["git", "log", "--oneline"]:
            return b"abc1234 feat: example\n"

        # git diff --name-only <before> <after> -- pyproject.toml
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return b""

        return b""

    def did_force_reset(self) -> bool:
        return any(c[:4] == ["git", "checkout", "--", "."] for c in self.calls)

    def did_pull(self) -> bool:
        return any(c[:3] == ["git", "pull", "origin"] for c in self.calls)

    def did_clean(self) -> bool:
        return any(c[:2] == ["git", "clean"] for c in self.calls)


class TestAdminUpdateForce:
    """Verify the force=True flag from task #77."""

    def test_force_false_is_default(self):
        """No force param → no `git checkout -- .` is invoked."""
        gm = _GitMock(dirty_files=["frontend-dist/index.html"])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=beta")
        assert r.status_code == 200
        body = r.json()
        assert body.get("updated") is True
        assert body.get("forced_reset") is False
        assert body.get("forced_files") == []
        assert not gm.did_force_reset()
        assert gm.did_pull()

    def test_force_true_resets_dirty_tracked_files(self):
        """force=True with a dirty tree → checkout -- . runs before pull."""
        dirty = ["frontend-dist/index.html", "frontend-dist/assets/app.js"]
        gm = _GitMock(dirty_files=dirty)
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=beta&force=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("forced_reset") is True
        assert body.get("forced_files") == dirty
        assert gm.did_force_reset()

        # Critical ordering: reset must happen BEFORE the pull
        checkout_idx = next(
            i for i, c in enumerate(gm.calls) if c[:4] == ["git", "checkout", "--", "."]
        )
        pull_idx = next(
            i for i, c in enumerate(gm.calls) if c[:3] == ["git", "pull", "origin"]
        )
        assert checkout_idx < pull_idx, "force reset must precede git pull"

    def test_force_true_clean_tree_no_reset(self):
        """force=True but tree is clean → no checkout invoked, forced_reset=False."""
        gm = _GitMock(dirty_files=[])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=beta&force=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("forced_reset") is False
        assert body.get("forced_files") == []
        assert not gm.did_force_reset()
        assert gm.did_pull()

    def test_force_never_runs_git_clean(self):
        """Untracked files must be preserved — never invoke `git clean`."""
        gm = _GitMock(dirty_files=["some/file.py"])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            client.post("/admin/update?branch=beta&force=true")
        assert not gm.did_clean(), "force must not delete untracked files"

    def test_force_ignored_in_dry_run(self):
        """dry_run=True returns before the destructive force block runs."""
        gm = _GitMock(dirty_files=["frontend-dist/index.html"])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=beta&force=true&dry_run=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("dry_run") is True
        # No destructive ops in dry_run, regardless of force
        assert not gm.did_force_reset()
        assert not gm.did_pull()


class TestAdminUpdateBaseline:
    """Sanity coverage for the existing endpoint paths."""

    def test_dry_run_reports_pending_commits(self):
        gm = _GitMock(dirty_files=[])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=beta&dry_run=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("dry_run") is True
        assert body.get("branch") == "beta"
        # _GitMock returns one canned "feat: example" commit for git log
        assert body.get("pending_commits") == 1
        assert body.get("up_to_date") is False

    def test_pull_failure_returns_error(self):
        """If git pull errors, endpoint returns {'error': ...}."""

        def fail_on_pull(cmd, **kwargs):
            if cmd[:3] == ["git", "pull", "origin"]:
                raise sp.CalledProcessError(1, cmd, output=b"merge conflict in foo")
            # Delegate everything else to a clean mock
            return _GitMock(dirty_files=[])(cmd, **kwargs)

        with (
            patch("subprocess.check_output", side_effect=fail_on_pull),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=beta")
        assert r.status_code == 200
        body = r.json()
        assert "error" in body
        assert "git pull failed" in body["error"]
