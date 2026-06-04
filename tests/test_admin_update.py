"""Tests for POST /admin/update — covers force flag behavior."""

from __future__ import annotations

import os
import subprocess as sp
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.self_update import DeployDecision


@pytest.fixture(autouse=True)
def _stub_resolve_and_verify():
    """Default: the deploy-target resolver returns a verified release.

    admin_update's resolve+verify is unit-tested in test_self_update.py;
    these endpoint tests treat it as a verified black box and focus on the
    deploy mechanics (fetch, checkout, deps, frontend, force-reset, restart).
    """
    with patch(
        "pinky_daemon.self_update.resolve_and_verify",
        return_value=DeployDecision(ref="26.06.109", kind="release", verified=True),
    ):
        yield


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
        branch: str = "main",
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

    def did_deploy_checkout(self, ref: str = "26.06.109") -> bool:
        """True if the deploy ref was checked out (not the `checkout -- .` reset)."""
        return any(c[:2] == ["git", "checkout"] and len(c) == 3 and c[2] == ref
                   for c in self.calls)

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
            r = client.post("/admin/update?branch=main")
        assert r.status_code == 200
        body = r.json()
        assert body.get("updated") is True
        assert body.get("forced_reset") is False
        assert body.get("forced_files") == []
        assert not gm.did_force_reset()
        assert gm.did_deploy_checkout()

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
            r = client.post("/admin/update?branch=main&force=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("forced_reset") is True
        assert body.get("forced_files") == dirty
        assert gm.did_force_reset()

        # Critical ordering: reset must happen BEFORE the deploy checkout
        reset_idx = next(
            i for i, c in enumerate(gm.calls) if c[:4] == ["git", "checkout", "--", "."]
        )
        deploy_idx = next(
            i for i, c in enumerate(gm.calls)
            if c[:2] == ["git", "checkout"] and len(c) == 3 and c[2] == "26.06.109"
        )
        assert reset_idx < deploy_idx, "force reset must precede the deploy checkout"

    def test_force_true_clean_tree_no_reset(self):
        """force=True but tree is clean → no checkout invoked, forced_reset=False."""
        gm = _GitMock(dirty_files=[])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main&force=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("forced_reset") is False
        assert body.get("forced_files") == []
        assert not gm.did_force_reset()
        assert gm.did_deploy_checkout()

    def test_force_never_runs_git_clean(self):
        """Untracked files must be preserved — never invoke `git clean`."""
        gm = _GitMock(dirty_files=["some/file.py"])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            client.post("/admin/update?branch=main&force=true")
        assert not gm.did_clean(), "force must not delete untracked files"

    def test_force_ignored_in_dry_run(self):
        """dry_run=True returns before the destructive force block runs."""
        gm = _GitMock(dirty_files=["frontend-dist/index.html"])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main&force=true&dry_run=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("dry_run") is True
        # No destructive ops in dry_run, regardless of force
        assert not gm.did_force_reset()
        assert not gm.did_deploy_checkout()


class TestAdminUpdateBaseline:
    """Sanity coverage for the existing endpoint paths."""

    def test_dry_run_reports_pending_commits(self):
        gm = _GitMock(dirty_files=[])
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main&dry_run=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("dry_run") is True
        assert body.get("branch") == "main"
        # _GitMock returns one canned "feat: example" commit for git log
        assert body.get("pending_commits") == 1
        assert body.get("up_to_date") is False

    def test_checkout_failure_returns_error(self):
        """If the deploy checkout errors, endpoint returns {'error': ...}."""

        def fail_on_checkout(cmd, **kwargs):
            if cmd[:2] == ["git", "checkout"] and len(cmd) == 3 and cmd[2] == "26.06.109":
                raise sp.CalledProcessError(1, cmd, output=b"checkout would overwrite foo")
            # Delegate everything else to a clean mock
            return _GitMock(dirty_files=[])(cmd, **kwargs)

        with (
            patch("subprocess.check_output", side_effect=fail_on_checkout),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main")
        assert r.status_code == 200
        body = r.json()
        assert "error" in body
        assert "git checkout" in body["error"] and "failed" in body["error"]

    def test_successful_frontend_rebuild_writes_manifest_and_status(self):
        """When npm build succeeds, /admin/update writes and returns the build manifest."""
        gm = _GitMock(dirty_files=[])
        manifest = {"git_hash": "def5678", "assets": ["index.js"]}
        frontend_status = {"status": "ok", "message": "fresh"}

        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("pinky_daemon.api._check_installed_deps_drift", return_value=[]),
            patch("shutil.which", return_value="/usr/bin/npm"),
            patch("pinky_daemon.api._write_frontend_build_manifest",
                  return_value=manifest) as write_manifest,
            patch("pinky_daemon.api._frontend_build_status",
                  return_value=frontend_status),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main")

        assert r.status_code == 200
        body = r.json()
        assert body["frontend_rebuilt"] is True
        assert body["frontend_manifest"] == manifest
        assert body["frontend_status"] == frontend_status
        write_manifest.assert_called_once()
        assert write_manifest.call_args.kwargs["git_hash"] == "def5678"

    def test_successful_frontend_rebuild_reports_manifest_error_separately(self):
        """A post-build manifest failure should not be mislabeled as a build failure."""
        gm = _GitMock(dirty_files=[])
        frontend_status = {"status": "unverified", "message": "manifest missing"}

        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("pinky_daemon.api._check_installed_deps_drift", return_value=[]),
            patch("shutil.which", return_value="/usr/bin/npm"),
            patch("pinky_daemon.api._write_frontend_build_manifest",
                  side_effect=OSError("disk full")) as write_manifest,
            patch("pinky_daemon.api._frontend_build_status",
                  return_value=frontend_status),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main")

        assert r.status_code == 200
        body = r.json()
        assert body["frontend_rebuilt"] is True
        assert body["frontend_manifest"] is None
        assert body["frontend_error"] == "Frontend build manifest failed: disk full"
        assert body["frontend_status"] == frontend_status
        write_manifest.assert_called_once()


class TestFrontendBuildManifest:
    """Frontend build-manifest guard for untracked frontend-dist deployments."""

    def _write_frontend_fixture(
        self,
        tmp: str,
        *,
        with_dist: bool = True,
        with_manifest: bool = False,
        manifest_git_hash: str = "abc1234",
        manifest_lock_hash: str | None = None,
        missing_asset: bool = False,
    ) -> Path:
        repo = Path(tmp)
        (repo / "frontend-svelte").mkdir(parents=True)
        (repo / "frontend-svelte" / "package-lock.json").write_text('{"lockfileVersion":3}\n')
        if not with_dist:
            return repo

        dist = repo / "frontend-dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text(
            '<script type="module" src="/assets/index-test.js"></script>\n'
            '<link rel="stylesheet" href="/assets/index-test.css">\n'
        )
        if not missing_asset:
            (assets / "index-test.js").write_text("console.log('ok')\n")
            (assets / "index-test.css").write_text("body{}\n")
        if with_manifest:
            from pinky_daemon.api import _sha256_file

            lock_hash = manifest_lock_hash or _sha256_file(
                repo / "frontend-svelte" / "package-lock.json"
            )
            (dist / "build-manifest.json").write_text(
                "{"
                f'"git_hash":"{manifest_git_hash}",'
                f'"package_lock_sha256":"{lock_hash}",'
                '"assets":["index-test.js","index-test.css"],'
                '"version":1'
                "}\n"
            )
        return repo

    def test_frontend_status_missing_when_dist_absent(self):
        from pinky_daemon.api import _frontend_build_status

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._write_frontend_fixture(tmp, with_dist=False)
            status = _frontend_build_status(str(repo), current_git_hash="abc1234")

        assert status["status"] == "missing"
        assert "frontend-dist does not exist" in status["message"]

    def test_frontend_status_unverified_when_manifest_absent(self):
        from pinky_daemon.api import _frontend_build_status

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._write_frontend_fixture(tmp, with_dist=True, with_manifest=False)
            status = _frontend_build_status(str(repo), current_git_hash="abc1234")

        assert status["status"] == "unverified"
        assert status["manifest_exists"] is False

    def test_frontend_status_stale_when_manifest_hash_differs(self):
        from pinky_daemon.api import _frontend_build_status

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._write_frontend_fixture(
                tmp, with_dist=True, with_manifest=True, manifest_git_hash="old1111",
            )
            status = _frontend_build_status(str(repo), current_git_hash="new2222")

        assert status["status"] == "stale"
        assert status["built_git_hash"] == "old1111"
        assert status["current_git_hash"] == "new2222"
        assert "git_hash" in status["stale_reasons"]

    def test_frontend_status_stale_when_manifest_assets_differ(self):
        from pinky_daemon.api import _frontend_build_status

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._write_frontend_fixture(
                tmp, with_dist=True, with_manifest=True, manifest_git_hash="abc1234",
            )
            # Use the actual current lock hash so only the asset-list
            # check drives staleness.
            from pinky_daemon.api import _sha256_file

            lock_hash = _sha256_file(repo / "frontend-svelte" / "package-lock.json")
            (repo / "frontend-dist" / "build-manifest.json").write_text(
                "{"
                '"git_hash":"abc1234",'
                f'"package_lock_sha256":"{lock_hash}",'
                '"assets":["old.js"],'
                '"version":1'
                "}\n"
            )
            status = _frontend_build_status(str(repo), current_git_hash="abc1234")

        assert status["status"] == "stale"
        assert status["built_assets"] == ["old.js"]
        assert status["assets"] == ["index-test.js", "index-test.css"]
        assert "assets" in status["stale_reasons"]

    def test_frontend_status_unverified_when_manifest_incomplete(self):
        from pinky_daemon.api import _frontend_build_status, _sha256_file

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._write_frontend_fixture(
                tmp, with_dist=True, with_manifest=True, manifest_git_hash="abc1234",
            )
            lock_hash = _sha256_file(repo / "frontend-svelte" / "package-lock.json")
            (repo / "frontend-dist" / "build-manifest.json").write_text(
                "{"
                '"git_hash":"abc1234",'
                f'"package_lock_sha256":"{lock_hash}",'
                '"version":1'
                "}\n"
            )
            status = _frontend_build_status(str(repo), current_git_hash="abc1234")

        assert status["status"] == "unverified"
        assert "incomplete" in status["message"]

    def test_frontend_status_broken_when_index_references_missing_asset(self):
        from pinky_daemon.api import _frontend_build_status

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._write_frontend_fixture(
                tmp, with_dist=True, with_manifest=False, missing_asset=True,
            )
            status = _frontend_build_status(str(repo), current_git_hash="abc1234")

        assert status["status"] == "broken"
        assert status["missing_assets"] == ["index-test.js", "index-test.css"]

    def test_write_frontend_manifest_records_hash_and_assets(self):
        from pinky_daemon.api import _frontend_build_status, _write_frontend_build_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._write_frontend_fixture(tmp, with_dist=True, with_manifest=False)
            manifest = _write_frontend_build_manifest(
                str(repo), git_hash="abc1234", built_at=123.0,
            )
            status = _frontend_build_status(str(repo), current_git_hash="abc1234")

        assert manifest["git_hash"] == "abc1234"
        assert manifest["built_at"] == 123.0
        assert manifest["assets"] == ["index-test.js", "index-test.css"]
        assert status["status"] == "ok"

    def test_admin_update_status_endpoint_exposes_frontend_status(self):
        client = _make_client()
        with patch(
            "pinky_daemon.api._frontend_build_status",
            return_value={"status": "unverified", "message": "test"},
        ):
            r = client.get("/admin/update/status")

        assert r.status_code == 200
        body = r.json()
        assert body["frontend"]["status"] == "unverified"


class TestAdminUpdateForceDepsIntegration:
    """force_deps (PR #323) and force (PR #390) are orthogonal — verify they cooperate."""

    def _track_pip_calls(self, gm):
        """Wrap _GitMock so we also log any pip-install invocation."""
        gm.pip_calls: list[list[str]] = []
        original_call = gm.__call__

        def wrapped(cmd, **kwargs):
            cmd_list = list(cmd)
            # Detect pip install: either `/path/to/pip install ...` or
            # `python -m pip install ...`. The first arg is the executable.
            is_pip = (
                "install" in cmd_list
                and (
                    cmd_list[0].endswith("/pip")
                    or cmd_list[0] == "pip"
                    or "pip" in cmd_list  # for `python -m pip ...` form
                )
            )
            if is_pip:
                gm.pip_calls.append(cmd_list)
                return b""
            return original_call(cmd, **kwargs)

        return wrapped

    def test_force_deps_triggers_pip_install_even_on_clean_pull(self):
        """force_deps=True → pip install runs even when nothing changed in git."""
        gm = _GitMock(dirty_files=[], before_hash="same1", after_hash="same1")
        wrapped = self._track_pip_calls(gm)
        with (
            patch("subprocess.check_output", side_effect=wrapped),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main&force_deps=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("deps_rebuilt") is True
        assert body.get("deps_error") is None
        assert len(gm.pip_calls) == 1, f"expected exactly 1 pip install call, got {gm.pip_calls}"
        # Should target the editable install
        assert "-e" in gm.pip_calls[0]
        assert ".[all]" in gm.pip_calls[0]

    def test_force_and_force_deps_combine(self):
        """Both flags together: dirty tree gets reset AND deps get rebuilt."""
        gm = _GitMock(dirty_files=["frontend-dist/index.html"])
        wrapped = self._track_pip_calls(gm)
        with (
            patch("subprocess.check_output", side_effect=wrapped),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main&force=true&force_deps=true")
        assert r.status_code == 200
        body = r.json()
        assert body.get("forced_reset") is True
        assert body.get("forced_files") == ["frontend-dist/index.html"]
        assert body.get("deps_rebuilt") is True
        assert gm.did_force_reset()
        assert len(gm.pip_calls) == 1

    def test_no_force_deps_skips_pip_when_pyproject_unchanged(self):
        """Default behavior: don't reinstall when pyproject.toml didn't change.

        The drift detector is also a reinstall trigger (see
        TestInstalledDepsDriftDetection) so we patch it to report no drift —
        this test pins the pyproject-diff axis specifically.
        """
        gm = _GitMock(dirty_files=[])
        wrapped = self._track_pip_calls(gm)
        with (
            patch("subprocess.check_output", side_effect=wrapped),
            patch("pinky_daemon.api._check_installed_deps_drift",
                  return_value=[]),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main")
        body = r.json()
        assert body.get("deps_rebuilt") is False
        assert gm.pip_calls == []

    def test_deps_error_surfaced_when_pip_fails(self):
        """If pip install errors out, deps_error is populated and surfaced."""
        gm = _GitMock(dirty_files=[], before_hash="same1", after_hash="same1")

        def fail_on_pip(cmd, **kwargs):
            cmd_list = list(cmd)
            is_pip = (
                "install" in cmd_list
                and (cmd_list[0].endswith("/pip") or cmd_list[0] == "pip" or "pip" in cmd_list)
            )
            if is_pip:
                raise sp.CalledProcessError(1, cmd, output=b"ERROR: package not found")
            return gm(cmd, **kwargs)

        with (
            patch("subprocess.check_output", side_effect=fail_on_pip),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main&force_deps=true")
        body = r.json()
        assert body.get("deps_rebuilt") is False
        assert body.get("deps_error")
        assert "pip install failed" in body["deps_error"]


class TestInstalledDepsDriftDetection:
    """Direct unit tests for `_check_installed_deps_drift`.

    The helper compares installed package versions to pyproject.toml pins to
    catch the "pyproject bumped earlier, routine restart skipped reinstall"
    case that previously needed manual force_deps=True.
    """

    def _write_pyproject(self, tmp_dir: str, deps: list[str],
                        optional: dict | None = None) -> None:
        from pathlib import Path
        # TOML literal strings (single-quoted) — no escape processing,
        # so embedded double quotes (env markers like `platform_system == "x"`)
        # don't need escaping.
        content = "[project]\nname = 'test'\nversion = '0.0.0'\ndependencies = [\n"
        for d in deps:
            content += f"  '{d}',\n"
        content += "]\n"
        if optional:
            content += "\n[project.optional-dependencies]\n"
            for group, group_deps in optional.items():
                content += f"{group} = [\n"
                for d in group_deps:
                    content += f"  '{d}',\n"
                content += "]\n"
        (Path(tmp_dir) / "pyproject.toml").write_text(content)

    def test_empty_drift_when_all_pins_satisfied(self):
        """A pyproject that depends only on packages whose installed versions
        satisfy the pins returns an empty list."""
        from pinky_daemon.api import _check_installed_deps_drift

        # `pytest` must be installed (we're literally running under it),
        # so any-version pin should be satisfied.
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pyproject(tmp, ["pytest"])
            drifts = _check_installed_deps_drift(tmp)
        assert drifts == []

    def test_pin_higher_than_installed_yields_drift_entry(self):
        """When pyproject pins a version above what's installed, that package
        shows up in the drift list with the installed version recorded."""
        from importlib.metadata import version as _v

        from pinky_daemon.api import _check_installed_deps_drift

        installed = _v("pytest")
        # Construct a pin that the installed version cannot satisfy.
        # Bump major+1 to keep things robust against future pytest releases.
        major = int(installed.split(".")[0]) + 1
        unsatisfiable_pin = f"pytest>={major}.0.0"

        with tempfile.TemporaryDirectory() as tmp:
            self._write_pyproject(tmp, [unsatisfiable_pin])
            drifts = _check_installed_deps_drift(tmp)

        assert len(drifts) == 1
        entry = drifts[0]
        assert entry["package"] == "pytest"
        assert entry["installed"] == installed
        assert str(major) in entry["specifier"]

    def test_missing_package_records_installed_none(self):
        """A pyproject dep that isn't installed at all shows up with
        installed=None — distinct from a version mismatch."""
        from pinky_daemon.api import _check_installed_deps_drift

        ghost_pkg = "definitely-not-a-real-package-9b3c"
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pyproject(tmp, [ghost_pkg])
            drifts = _check_installed_deps_drift(tmp)

        assert len(drifts) == 1
        assert drifts[0]["package"] == ghost_pkg
        assert drifts[0]["installed"] is None

    def test_optional_dependencies_are_inspected(self):
        """Drift in `project.optional-dependencies` groups is reported just
        like core dependencies — keeps `pip install -e .[all]` honest."""
        from pinky_daemon.api import _check_installed_deps_drift

        ghost_pkg = "definitely-not-a-real-package-opt-7a2d"
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pyproject(
                tmp, deps=["pytest"], optional={"extra": [ghost_pkg]}
            )
            drifts = _check_installed_deps_drift(tmp)

        ghost_entries = [d for d in drifts if d["package"] == ghost_pkg]
        assert len(ghost_entries) == 1
        assert ghost_entries[0]["installed"] is None

    def test_markers_excluding_current_env_are_skipped(self):
        """Deps with environment markers that don't match the current
        interpreter must be skipped — otherwise a Windows-only pin would
        spuriously fire on macOS."""
        from pinky_daemon.api import _check_installed_deps_drift

        # platform_system="DefinitelyNotARealOS" never matches — package
        # should be skipped, not reported as missing.
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pyproject(
                tmp,
                ['ghost-pkg-marker-3f01 ; platform_system == "DefinitelyNotARealOS"'],
            )
            drifts = _check_installed_deps_drift(tmp)

        assert drifts == []

    def test_unparseable_dependency_is_skipped_silently(self):
        """A malformed line in pyproject must not crash the whole check;
        unparseable entries are silently skipped so well-formed deps still
        get inspected."""
        from pinky_daemon.api import _check_installed_deps_drift

        with tempfile.TemporaryDirectory() as tmp:
            # First line is junk, second is a real (satisfied) pin.
            self._write_pyproject(tmp, ["===not a requirement===", "pytest"])
            drifts = _check_installed_deps_drift(tmp)
        assert drifts == []

    def test_drift_triggers_reinstall_in_admin_update(self):
        """When the drift check reports any entry, admin_update kicks off
        pip install even if pyproject.toml didn't change in this pull and
        force_deps=False."""
        gm = _GitMock(dirty_files=[], before_hash="same1", after_hash="same1")

        fake_drift = [{
            "package": "claude-agent-sdk",
            "specifier": ">=0.1.77",
            "installed": "0.1.68",
        }]

        pip_calls: list[list[str]] = []

        def record_subprocess(cmd, **kwargs):
            cmd_list = list(cmd)
            is_pip = (
                "install" in cmd_list
                and (cmd_list[0].endswith("/pip") or cmd_list[0] == "pip"
                     or "pip" in cmd_list)
            )
            if is_pip:
                pip_calls.append(cmd_list)
                return b""
            return gm(cmd, **kwargs)

        with (
            patch("subprocess.check_output", side_effect=record_subprocess),
            patch("pinky_daemon.api._check_installed_deps_drift",
                  return_value=fake_drift),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main")
        body = r.json()
        assert body.get("deps_rebuilt") is True
        assert body.get("deps_drift") == fake_drift
        assert pip_calls, "drift should have triggered pip install"

    def test_no_drift_no_diff_no_force_means_no_reinstall(self):
        """The drift signal is additive — when there's no drift AND no
        pyproject change AND no force_deps, pip is not invoked."""
        gm = _GitMock(dirty_files=[], before_hash="same1", after_hash="same1")

        pip_calls: list[list[str]] = []

        def record_subprocess(cmd, **kwargs):
            cmd_list = list(cmd)
            is_pip = (
                "install" in cmd_list
                and (cmd_list[0].endswith("/pip") or cmd_list[0] == "pip"
                     or "pip" in cmd_list)
            )
            if is_pip:
                pip_calls.append(cmd_list)
                return b""
            return gm(cmd, **kwargs)

        with (
            patch("subprocess.check_output", side_effect=record_subprocess),
            patch("pinky_daemon.api._check_installed_deps_drift",
                  return_value=[]),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main")
        body = r.json()
        assert body.get("deps_rebuilt") is False
        assert body.get("deps_drift") == []
        assert pip_calls == []

    def test_drift_check_failure_is_non_fatal(self):
        """If the drift check itself raises (e.g. pyproject parse fails),
        admin_update keeps going — drift just can't contribute to the
        reinstall decision."""
        gm = _GitMock(dirty_files=[], before_hash="same1", after_hash="same1")

        def boom(_repo_dir):
            raise RuntimeError("simulated drift-check failure")

        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("pinky_daemon.api._check_installed_deps_drift",
                  side_effect=boom),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update?branch=main")
        assert r.status_code == 200
        body = r.json()
        # No reinstall (no drift, no diff, no force), and the failure didn't
        # propagate as a 500.
        assert body.get("updated") is True
        assert body.get("deps_drift") == []


class TestTrunkBasedChannel:
    """The beta channel was removed in the trunk-based migration (#450).

    Only branch=main / channel=stable should be accepted; everything else
    must 400. Legacy PINKYBOT_CHANNEL=beta is coerced (logged) to stable.
    """

    def test_admin_update_rejects_beta_branch(self):
        """branch=beta must 400 — beta channel was removed."""
        client = _make_client()
        r = client.post("/admin/update?branch=beta")
        assert r.status_code == 400
        assert "beta" in r.json()["detail"].lower() or "main" in r.json()["detail"].lower()

    def test_admin_update_rejects_arbitrary_branch(self):
        """Only 'main' or empty is accepted."""
        client = _make_client()
        r = client.post("/admin/update?branch=feature/foo")
        assert r.status_code == 400

    def test_admin_update_empty_branch_defaults_to_main(self):
        """No branch arg → defaults to main (release tags)."""
        gm = _GitMock(dirty_files=[], branch="main")
        with (
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update")
        assert r.status_code == 200
        assert gm.did_deploy_checkout()

    def test_admin_update_coerces_legacy_beta_env(self):
        """PINKYBOT_CHANNEL=beta env should not crash; coerced to stable."""
        gm = _GitMock(dirty_files=[], branch="main")
        with (
            patch.dict(os.environ, {"PINKYBOT_CHANNEL": "beta"}),
            patch("subprocess.check_output", side_effect=gm),
            patch("shutil.which", return_value=None),
            patch("os.kill"),
        ):
            client = _make_client()
            r = client.post("/admin/update")
        assert r.status_code == 200
        # Should operate on main, not beta: fetch targets main + deploy checkout ran.
        fetch_calls = [c for c in gm.calls if c[:3] == ["git", "fetch", "origin"]]
        assert fetch_calls and fetch_calls[0][-1] == "main"
        assert gm.did_deploy_checkout()

    def test_admin_channel_get_always_returns_stable(self):
        client = _make_client()
        r = client.get("/admin/channel")
        assert r.status_code == 200
        body = r.json()
        assert body == {"channel": "stable", "branch": "main"}

    def test_admin_channel_get_coerces_legacy_beta_env(self):
        with patch.dict(os.environ, {"PINKYBOT_CHANNEL": "beta"}):
            client = _make_client()
            r = client.get("/admin/channel")
        assert r.status_code == 200
        assert r.json() == {"channel": "stable", "branch": "main"}

    def test_admin_channel_post_rejects_beta(self):
        client = _make_client()
        r = client.post("/admin/channel?channel=beta")
        assert r.status_code == 400

    def test_admin_channel_post_rejects_arbitrary(self):
        client = _make_client()
        r = client.post("/admin/channel?channel=edge")
        assert r.status_code == 400
