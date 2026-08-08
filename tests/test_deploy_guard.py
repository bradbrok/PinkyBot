"""Tests for the deploy guard-rail on POST /admin/update (#425).

Two independent layers protect a machine whose checkout has diverged from
the deploy target:

1. ``.pinky-deploy-lock`` — an explicit, untracked lock file stating intent.
2. divergence detection — refuses when HEAD carries commits the deploy ref
   does not, i.e. when the checkout would silently drop local work.

Neither layer is bypassed by ``force``: force means "skip release
verification", not "permission to deploy". Only the API-level
``override_guard`` flag (deliberately NOT exposed through the pinky-self MCP
tool) disarms them.
"""

from __future__ import annotations

import os
import subprocess as sp
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.self_update import DeployDecision

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = REPO_ROOT / ".pinky-deploy-lock"


@pytest.fixture(autouse=True)
def _stub_resolve_and_verify():
    """Treat resolve+verify as a verified black box (see test_admin_update)."""
    with patch(
        "pinky_daemon.self_update.resolve_and_verify",
        return_value=DeployDecision(ref="26.06.109", kind="release", verified=True),
    ):
        yield


@pytest.fixture
def deploy_lock():
    """Create a real lock file in the repo root, always cleaned up."""
    created: list[Path] = []

    def _create(reason: str = "#425 — awaiting scope decision"):
        LOCK_PATH.write_text(reason, encoding="utf-8")
        created.append(LOCK_PATH)
        return LOCK_PATH

    yield _create
    for p in created:
        p.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _no_stray_lock():
    """Guard: the repo must not carry a lock file into unrelated tests."""
    assert not LOCK_PATH.exists(), (
        f"{LOCK_PATH} exists before the test — a previous run leaked it"
    )
    yield


def _make_client():
    from pinky_daemon.api import create_api
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
    return TestClient(app)


class _GuardGitMock:
    """Programmable git mock that can present a divergent HEAD.

    ``local_only`` is the list of commit SHAs reachable from HEAD but not from
    the deploy ref — exactly what ``git rev-list <ref>..HEAD`` prints.
    """

    def __init__(self, *, local_only: list[str] | None = None,
                 dirty_files: list[str] | None = None):
        self.calls: list[list[str]] = []
        self.local_only = local_only or []
        self.dirty_files = dirty_files or []
        self._hash_call = 0

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))

        if cmd[:3] == ["git", "rev-parse", "--short"]:
            self._hash_call += 1
            return (b"abc1234\n" if self._hash_call == 1 else b"def5678\n")
        if cmd[:2] == ["git", "describe"]:
            raise sp.CalledProcessError(128, cmd, output=b"")
        if cmd[:3] == ["git", "rev-parse", "--is-shallow-repository"]:
            return b"false\n"
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return b"main\n"
        if cmd[:2] == ["git", "rev-list"]:
            joined = "\n".join(self.local_only)
            return (joined + "\n").encode() if joined else b""
        if cmd[:4] == ["git", "diff", "--name-only", "HEAD"]:
            joined = "\n".join(self.dirty_files)
            return (joined + "\n").encode() if joined else b""
        if cmd[:3] == ["git", "log", "--oneline"]:
            return b"abc1234 feat: example\n"
        return b""

    def did_fetch(self) -> bool:
        return any(c[:3] == ["git", "fetch", "origin"] for c in self.calls)

    def did_force_reset(self) -> bool:
        return any(c[:5] == ["git", "checkout", "HEAD", "--", "."] for c in self.calls)

    def did_deploy_checkout(self, ref: str = "26.06.109") -> bool:
        return any(c[:2] == ["git", "checkout"] and len(c) == 3 and c[2] == ref
                   for c in self.calls)


def _post(query: str, gm: _GuardGitMock):
    with (
        patch("subprocess.check_output", side_effect=gm),
        patch("shutil.which", return_value=None),
        patch("os.kill"),
    ):
        client = _make_client()
        # create_api itself shells out to git (version banner); drop those so
        # the recorded calls are exactly what the update pipeline ran.
        gm.calls.clear()
        return client.post(f"/admin/update?{query}")


class TestDeployLockLayer:
    """Layer 1 — the explicit lock file."""

    def test_lock_file_blocks_update(self, deploy_lock):
        deploy_lock()
        gm = _GuardGitMock()
        r = _post("branch=main", gm)
        assert r.status_code == 200
        body = r.json()
        assert body.get("blocked_by") == "deploy_lock"
        assert "error" in body
        assert not gm.did_deploy_checkout()

    def test_lock_blocks_before_touching_git(self, deploy_lock):
        """The refusal must land before the pipeline runs any git command."""
        deploy_lock()
        gm = _GuardGitMock()
        _post("branch=main", gm)
        git_calls = [c for c in gm.calls if c[:1] == ["git"]]
        assert git_calls == [], f"update touched git while locked: {git_calls}"

    def test_lock_reason_is_reported(self, deploy_lock):
        deploy_lock("#425 — awaiting Mirko's scope decision")
        gm = _GuardGitMock()
        body = _post("branch=main", gm).json()
        assert body.get("lock_reason") == "#425 — awaiting Mirko's scope decision"

    def test_force_does_not_bypass_lock(self, deploy_lock):
        """force means 'skip release verification', not 'permission to deploy'."""
        deploy_lock()
        gm = _GuardGitMock(dirty_files=["src/pinky_daemon/api.py"])
        body = _post("branch=main&force=true", gm).json()
        assert body.get("blocked_by") == "deploy_lock"
        assert not gm.did_force_reset(), "force must not discard local work while locked"
        assert not gm.did_deploy_checkout()

    def test_dry_run_is_allowed_while_locked(self, deploy_lock):
        """Preview is read-only — it must still work, and say it would be blocked."""
        deploy_lock()
        gm = _GuardGitMock()
        body = _post("branch=main&dry_run=true", gm).json()
        assert body.get("dry_run") is True
        assert body.get("blocked_by") == "deploy_lock"
        assert not gm.did_deploy_checkout()

    def test_override_guard_bypasses_lock(self, deploy_lock):
        deploy_lock()
        gm = _GuardGitMock()
        body = _post("branch=main&override_guard=true", gm).json()
        assert body.get("updated") is True
        assert gm.did_deploy_checkout()

    def test_no_lock_file_update_proceeds(self):
        gm = _GuardGitMock()
        body = _post("branch=main", gm).json()
        assert body.get("updated") is True
        assert body.get("blocked_by") is None
        assert gm.did_deploy_checkout()


class TestDivergenceLayer:
    """Layer 2 — refuse when the checkout would drop local commits."""

    def test_divergent_head_blocks_update(self):
        gm = _GuardGitMock(local_only=["a" * 40, "b" * 40])
        body = _post("branch=main", gm).json()
        assert body.get("blocked_by") == "divergent_head"
        assert body.get("local_only_count") == 2
        assert not gm.did_deploy_checkout()

    def test_aligned_head_proceeds(self):
        """No local-only commits → no-op, deploy runs normally."""
        gm = _GuardGitMock(local_only=[])
        body = _post("branch=main", gm).json()
        assert body.get("updated") is True
        assert gm.did_deploy_checkout()

    def test_force_does_not_bypass_divergence(self):
        gm = _GuardGitMock(local_only=["c" * 40], dirty_files=["src/pinky_daemon/api.py"])
        body = _post("branch=main&force=true", gm).json()
        assert body.get("blocked_by") == "divergent_head"
        assert not gm.did_force_reset(), (
            "the divergence refusal must land before force discards tracked files"
        )
        assert not gm.did_deploy_checkout()

    def test_override_guard_bypasses_divergence(self):
        gm = _GuardGitMock(local_only=["d" * 40])
        body = _post("branch=main&override_guard=true", gm).json()
        assert body.get("updated") is True
        assert gm.did_deploy_checkout()

    def test_dry_run_reports_divergence_without_acting(self):
        gm = _GuardGitMock(local_only=["e" * 40])
        body = _post("branch=main&dry_run=true", gm).json()
        assert body.get("dry_run") is True
        assert body.get("blocked_by") == "divergent_head"
        assert body.get("local_only_count") == 1
        assert not gm.did_deploy_checkout()

    def test_divergence_error_names_the_inspect_command(self):
        """The operator must be told how to see what would be lost."""
        gm = _GuardGitMock(local_only=["f" * 40])
        body = _post("branch=main", gm).json()
        assert "26.06.109..HEAD" in body.get("error", "")
