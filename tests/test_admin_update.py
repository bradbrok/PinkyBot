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
            r = client.post("/admin/update?branch=beta&force_deps=true")
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
            r = client.post("/admin/update?branch=beta&force=true&force_deps=true")
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
            r = client.post("/admin/update?branch=beta")
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
            r = client.post("/admin/update?branch=beta&force_deps=true")
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
            r = client.post("/admin/update?branch=beta")
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
            r = client.post("/admin/update?branch=beta")
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
            r = client.post("/admin/update?branch=beta")
        assert r.status_code == 200
        body = r.json()
        # No reinstall (no drift, no diff, no force), and the failure didn't
        # propagate as a 500.
        assert body.get("updated") is True
        assert body.get("deps_drift") == []
