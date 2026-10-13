"""The startup inspection runs beside live stores without opening them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def command_tree(tmp_path: Path) -> Path:
    import pinky_daemon

    tree = tmp_path / "release"
    source = Path(pinky_daemon.__file__).resolve().parents[1]
    shutil.copytree(source, tree / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (tree / "src" / "sitecustomize.py").write_text(
        "import os, sys\n"
        "import pinky_daemon\n"
        "print('IMPORTED_PACKAGE=' + pinky_daemon.__file__, file=sys.stderr)\n"
        "root = os.environ['CHECK_DATA_ROOT']\n"
        "def audit(event, args):\n"
        "    if event in ('sqlite3.connect', 'socket.bind', 'subprocess.Popen', 'fcntl.flock'):\n"
        "        raise RuntimeError('forbidden startup-check effect: ' + event)\n"
        "    if event in ('open', 'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir'):\n"
        "        for arg in args[:2] if event == 'os.rename' else args[:1]:\n"
        "            if isinstance(arg, (str, bytes, os.PathLike)):\n"
        "                path = os.path.abspath(os.fsdecode(arg))\n"
        "                if path == root or path.startswith(root + os.sep):\n"
        "                    raise RuntimeError('forbidden data-root access: ' + event)\n"
        "sys.addaudithook(audit)\n"
    )
    return tree


def run_check(tree: Path, db_path: Path) -> subprocess.CompletedProcess[str]:
    root = Path(os.path.realpath(db_path)).parent
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(tree / "src"),
        PYTHONNOUSERSITE="1",
        CHECK_DATA_ROOT=str(root),
    )
    result = subprocess.run(
        [sys.executable, "-m", "pinky_daemon", "--mode", "check-startup", "--db-path", str(db_path)],
        cwd=tree,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    expected_import = str(tree / "src" / "pinky_daemon" / "__init__.py")
    assert f"IMPORTED_PACKAGE={expected_import}" in result.stderr
    assert "forbidden startup-check effect" not in result.stderr
    assert "forbidden data-root access" not in result.stderr
    return result


def report_of(result: subprocess.CompletedProcess[str], root: Path, *, ok: bool) -> dict:
    report = json.loads(result.stdout)
    assert set(report) == {"ok", "root", "checked", "findings", "warnings_count", "warnings"}
    assert report["ok"] is ok
    assert report["root"] == os.path.realpath(root)
    assert report["checked"] == ["import", "layout"]
    assert isinstance(report["findings"], list)
    assert type(report["warnings_count"]) is int
    assert all(isinstance(warning, str) for warning in report["warnings"])
    assert len(report["warnings"]) == min(20, report["warnings_count"])
    return report


def snapshot(root: Path) -> list[tuple[str, int, int, int]]:
    paths = [root, *sorted(root.rglob("*"))]
    return [
        (str(path.relative_to(root)), st.st_size, st.st_mtime_ns, st.st_ino)
        for path in paths
        for st in [path.lstat()]
    ]


def quiet_check(tree: Path, root: Path, *, mode="check-startup", flags=()):
    site = tree / "src/sitecustomize.py"
    site.write_text(
        "import pinky_daemon\n"
        f"assert pinky_daemon.__file__ == {str(tree / 'src/pinky_daemon/__init__.py')!r}\n"
    )
    env = dict(os.environ, PYTHONPATH=str(tree / "src"))
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, *flags, "-m", "pinky_daemon", "--mode", mode,
         "--db-path", str(root / "conversations.db")],
        cwd=tree, env=env, capture_output=True, text=True, timeout=45,
    )


@pytest.mark.parametrize("stage", ["import", "layout"])
@pytest.mark.parametrize("effect", ["pass", "raise SystemExit(0)", "raise SystemExit(7)",
                                    "raise RuntimeError('first\\nsecond')",
                                    "print('synthetic diagnostic'); raise SystemExit(0)"])
def test_startup_check_incomplete_exits(command_tree, tmp_path, stage, effect):
    root = tmp_path / "data"
    root.mkdir()
    application = command_tree / "src/pinky_daemon/api.py"
    if stage == "layout":
        effect = ("from pinky_daemon import store_catalog\n"
                  "def inspect(*args, **kwargs):\n    " + effect + "\n"
                  "store_catalog.inspect_filesystem_layout = inspect\n") if effect != "pass" else effect
    with application.open("a") as stream:
        stream.write("\n" + effect + "\n")
    result = quiet_check(command_tree, root)
    if effect == "pass":
        assert result.returncode == 0 and json.loads(result.stdout)["ok"]
        assert result.stderr == ""
    else:
        assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
        assert result.stdout == ""
        assert len(result.stderr.splitlines()) == 1
        assert result.stderr.startswith("startup check could not complete: ")


@pytest.mark.parametrize("stage", ["import", "layout"])
def test_startup_check_does_not_catch_interrupt(command_tree, tmp_path, stage):
    root = tmp_path / "data"
    root.mkdir()
    effect = "raise KeyboardInterrupt()"
    if stage == "layout":
        effect = ("from pinky_daemon import store_catalog\n"
                  "def inspect(*args, **kwargs):\n    " + effect + "\n"
                  "store_catalog.inspect_filesystem_layout = inspect\n")
    with (command_tree / "src/pinky_daemon/api.py").open("a") as stream:
        stream.write("\n" + effect + "\n")
    result = quiet_check(command_tree, root)
    assert result.returncode != 2 and result.stdout == ""
    assert "KeyboardInterrupt" in result.stderr
    assert "startup check could not complete" not in result.stderr


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses file permissions")
@pytest.mark.parametrize("readable", [False, True])
def test_startup_check_dotenv_permissions(command_tree, tmp_path, readable):
    root = tmp_path / "data"
    root.mkdir()
    dotenv = command_tree / ".env"
    dotenv.write_text("SYNTHETIC_CHECK_SETTING=fixture\n")
    dotenv.chmod(0o600 if readable else 0o000)
    try:
        result = quiet_check(command_tree, root)
    finally:
        dotenv.chmod(0o600)
    assert result.returncode == (0 if readable else 2), result.stderr
    if readable:
        assert json.loads(result.stdout)["ok"] and result.stderr == ""
    else:
        assert result.stdout == ""
        assert len(result.stderr.splitlines()) == 1
        assert result.stderr.startswith("startup check could not complete: PermissionError:")


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses file permissions")
@pytest.mark.parametrize("mode", ["api", "poll"])
def test_other_modes_keep_dotenv_failure(command_tree, tmp_path, mode):
    dotenv = command_tree / ".env"
    dotenv.write_text("SYNTHETIC_CHECK_SETTING=fixture\n")
    dotenv.chmod(0o000)
    try:
        result = quiet_check(command_tree, tmp_path, mode=mode)
    finally:
        dotenv.chmod(0o600)
    assert result.returncode == 1 and result.stdout == ""
    assert "Traceback" in result.stderr and "PermissionError" in result.stderr
    assert "startup check could not complete" not in result.stderr


@pytest.mark.parametrize("source_inside_root", [False, True])
def test_startup_check_without_bytecode_preserves_root(command_tree, tmp_path, source_inside_root):
    root = command_tree if source_inside_root else tmp_path / "data"
    root.mkdir(exist_ok=True)
    # Allow source reads when the data root contains the application itself.
    site = command_tree / "src/sitecustomize.py"
    site.write_text(
        "import os, sys, pinky_daemon\n"
        f"assert pinky_daemon.__file__ == {str(command_tree / 'src/pinky_daemon/__init__.py')!r}\n"
        "def audit(event, args):\n"
        "    if event in ('sqlite3.connect', 'socket.bind', 'subprocess.Popen', 'fcntl.flock'):\n"
        "        raise RuntimeError('forbidden startup-check effect: ' + event)\n"
        "sys.addaudithook(audit)\n"
    )
    before = snapshot(root)
    env = dict(os.environ, PYTHONPATH=str(command_tree / "src"))
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pinky_daemon", "--mode", "check-startup",
         "--db-path", str(root / "conversations.db")],
        cwd=command_tree, env=env, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["ok"], result.stderr
    assert result.stderr == ""
    assert snapshot(root) == before


def test_startup_check_clean_root(command_tree: Path, tmp_path: Path):
    from pinky_daemon.store_catalog import DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST

    root = tmp_path / "data"
    root.mkdir()
    db_path = root / "conversations.db"
    db_path.write_bytes(b"metadata-only fixture")
    alias = tmp_path / "database-link"
    alias.symlink_to(db_path)
    before = snapshot(root)
    result = run_check(command_tree, alias)
    assert result.returncode == 0, result.stderr
    report = report_of(result, root, ok=True)
    assert report["findings"] == []
    assert report["warnings_count"] == len(DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST)
    assert all("dead silence-allowlist entry" in warning for warning in report["warnings"])
    assert snapshot(root) == before


@pytest.mark.parametrize("outside", [False, True])
def test_startup_check_reports_layout_findings(command_tree: Path, tmp_path: Path, outside: bool):
    root = tmp_path / "data"
    root.mkdir()
    target = (tmp_path if outside else root) / "target"
    target.mkdir()
    link = root / "linked-directory"
    link.symlink_to(target, target_is_directory=True)
    result = run_check(command_tree, root / "conversations.db")
    assert result.returncode == int(outside), result.stderr
    report = report_of(result, root, ok=not outside)
    assert len(report["findings"]) == int(outside)
    if outside:
        finding = report["findings"][0]
        assert finding["kind"] == "directory_outside_root"
        assert finding["path"] == str(link)
        assert finding["resolved_path"] == str(target)
        assert "directory symlink escapes expected root" in finding["detail"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses directory permissions")
@pytest.mark.parametrize("searchable", [False, True])
def test_startup_check_cannot_stat_manifest(command_tree: Path, tmp_path: Path, searchable: bool):
    root = tmp_path / "data"
    root.mkdir()
    db_path = root / "conversations.db"
    db_path.write_bytes(b"metadata-only fixture")
    root.chmod(0o700 if searchable else 0o000)
    try:
        result = run_check(command_tree, db_path)
        assert result.returncode == (0 if searchable else 2), result.stderr
        if searchable:
            assert report_of(result, root, ok=True)["findings"] == []
        else:
            assert "PermissionError" in result.stderr
            assert str(root) in result.stderr
    finally:
        root.chmod(0o700)


@pytest.mark.parametrize("raises", [False, True])
def test_startup_check_import_failure(command_tree: Path, tmp_path: Path, raises: bool):
    root = tmp_path / "data"
    root.mkdir()
    api_path = command_tree / "src" / "pinky_daemon" / "api.py"
    with api_path.open("a") as stream:
        stream.write("\nraise RuntimeError('synthetic import failure')\n" if raises else "\npass\n")
    result = run_check(command_tree, root / "conversations.db")
    assert result.returncode == (2 if raises else 0), result.stderr
    if raises:
        assert "RuntimeError" in result.stderr
        assert "synthetic import failure" in result.stderr
    else:
        assert report_of(result, root, ok=True)["findings"] == []


@pytest.mark.parametrize("noisy", [False, True])
def test_startup_check_import_stdout_is_diagnostic(command_tree: Path, tmp_path: Path, noisy: bool):
    root = tmp_path / "data"
    root.mkdir()
    api_path = command_tree / "src" / "pinky_daemon" / "api.py"
    with api_path.open("a") as stream:
        stream.write("\nprint('synthetic import diagnostic')\n" if noisy else "\npass\n")
    result = run_check(command_tree, root / "conversations.db")
    assert result.returncode == 0, result.stderr
    assert report_of(result, root, ok=True)["findings"] == []
    assert ("synthetic import diagnostic" in result.stderr) is noisy
    assert "synthetic import diagnostic" not in result.stdout


@pytest.mark.parametrize("count", [0, 25])
def test_startup_check_warning_limit(command_tree: Path, tmp_path: Path, count: int):
    from pinky_daemon.store_catalog import DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST

    root = tmp_path / "data"
    root.mkdir()
    for number in range(count):
        (root / f"unclaimed-{number:02}.db").write_bytes(b"unused")
    result = run_check(command_tree, root / "conversations.db")
    assert result.returncode == 0, result.stderr
    report = report_of(result, root, ok=True)
    assert report["warnings_count"] == count + len(DEFAULT_FILESYSTEM_SILENCE_ALLOWLIST)
    assert report["findings"] == []


def test_startup_check_does_not_truncate_findings(command_tree: Path, tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    for number in range(25):
        (root / f"link-{number:02}").symlink_to(target, target_is_directory=True)
    result = run_check(command_tree, root / "conversations.db")
    assert result.returncode == 1, result.stderr
    report = report_of(result, root, ok=False)
    assert len(report["findings"]) == 25
    assert {finding["path"] for finding in report["findings"]} == {
        str(root / f"link-{number:02}") for number in range(25)
    }


def test_startup_check_preserves_live_store_state(command_tree: Path, tmp_path: Path):
    from pinky_daemon.api import create_api

    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    db_path = root / "conversations.db"
    app = create_api(
        db_path=str(db_path),
        default_working_dir=str(tmp_path),
        access_log_path=str(tmp_path / "access.log"),
    )
    try:
        store = app.state.conversation_store
        store.append("synthetic-session", "user", "before inspection")
        before = snapshot(root)
        result = run_check(command_tree, db_path)
        assert result.returncode == 0, result.stderr
        assert report_of(result, root, ok=True)["findings"] == []
        assert snapshot(root) == before
        store.append("synthetic-session", "assistant", "after inspection")
        assert [message.content for message in store.get_history("synthetic-session")] == [
            "before inspection", "after inspection",
        ]
    finally:
        app.state.store_catalog.close()


@pytest.mark.parametrize("unsafe", [False, True])
@pytest.mark.parametrize("kind", ["directory", "database", "multiple", "footprint", "alias", "store_directory_link"])
def test_layout_findings_match_across_startup_preflight_and_command(
    command_tree: Path, tmp_path: Path, kind: str, unsafe: bool,
):
    from pinky_daemon import api
    from pinky_daemon.store_catalog import StoreCatalog, StoreCatalogError

    if kind == "footprint" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    db_path = root / "conversations.db"
    db_path.write_bytes(b"metadata-only fixture")
    if kind == "store_directory_link":
        store_directory = root / "kb-real"
        store_directory.mkdir()
        store_file = store_directory / "kb.db"
        store_file.write_bytes(b"metadata-only fixture")
        (root / "kb").symlink_to(store_directory, target_is_directory=True)
        if unsafe:
            os.link(store_file, root / "unregistered.db")
    catalog = StoreCatalog(expected_root=root)
    for name, target in api._derive_api_store_manifest(str(db_path)).items():
        catalog.register(name, target.path, journal_mode="delete", owner="fixture")
    if kind in {"directory", "multiple"}:
        count = 2 if kind == "multiple" else 1
        for number in range(count):
            target = (tmp_path if unsafe else root) / f"target-{number}"
            target.mkdir()
            (root / f"link-{number}").symlink_to(target, target_is_directory=True)
    elif kind == "database":
        target = (tmp_path if unsafe else root) / "target-file"
        target.write_bytes(b"metadata-only fixture")
        (root / "linked.db").symlink_to(target)
    elif kind == "alias":
        alias = root / "unregistered.db"
        if unsafe:
            os.link(db_path, alias)
        else:
            alias.write_bytes(b"different inode")
    elif kind == "footprint":
        root.chmod(0o100 if unsafe else 0o700)
    try:
        report = catalog.inspect_layout()
        preflight = api._startup_layout_preflight(catalog)
        result = run_check(command_tree, db_path)
        assert result.returncode == int(unsafe), result.stderr
        command_report = report_of(result, root, ok=not unsafe)
        assert report.findings == preflight["findings"] == command_report["findings"]
        assert len(report.warnings) == preflight["warnings_count"] == command_report["warnings_count"]
        assert report.warnings[:20] == command_report["warnings"]
        assert preflight["ok"] is not unsafe
        assert len(report.findings) == ((2 if kind == "multiple" else 1) if unsafe else 0)
        if kind == "store_directory_link" and unsafe:
            assert report.findings[0]["kind"] == "alias_of_registered_store"
            assert report.findings[0]["path"] == str(root / "unregistered.db")
        if unsafe:
            with pytest.raises(StoreCatalogError) as caught:
                catalog.reconcile_filesystem()
            assert report.findings[0]["detail"] in str(caught.value)
        else:
            catalog.reconcile_filesystem()
    finally:
        root.chmod(0o700)
        catalog.close()
