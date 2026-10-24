"""Agent content is pruned while daemon paths retain filesystem validation."""

from __future__ import annotations

import errno
import os

import pytest

from pinky_daemon import api, store_catalog
from pinky_daemon.store_catalog import StoreCatalog, StoreCatalogError, inspect_filesystem_layout
from tests.test_boot_layout_walk import _ReferenceInspector


def _write(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def _inspect(root, identities=None):
    return inspect_filesystem_layout(root, identities or {}, silence_allowlist={})


@pytest.mark.parametrize("name", ["drafts", "batch-17", ".scratch", "reports.db"])
def test_boot_skips_arbitrary_agent_clutter(tmp_path, monkeypatch, name):
    root = tmp_path / "data"
    clutter = root / "agents" / "a" / name
    leaf = clutter.joinpath(*(f"level-{i}" for i in range(40)))
    _write(leaf, "orphan.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (leaf / "escape").symlink_to(outside, target_is_directory=True)
    scanned = []
    original = os.scandir

    def scandir(path):
        scanned.append(os.fspath(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    catalog = StoreCatalog(root, silence_allowlist={})
    assert catalog.validate() == []
    assert str(clutter) not in scanned
    assert not any(path.startswith(str(clutter) + os.sep) for path in scanned)
    assert _inspect(root).skipped_unmanaged_subtrees == 1


@pytest.mark.parametrize("name", [
    "data", "output", "attachments", "dreams", ".claude", ".claude-local",
    ".claude-container", ".codex", ".codex-app-server",
])
def test_daemon_owned_directories_keep_escape_and_stray_detection(tmp_path, name):
    root = tmp_path / "data"
    managed = root / "agents" / "a" / name
    stray = _write(managed, "nested/orphan.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = managed / "escape"
    link.symlink_to(outside, target_is_directory=True)
    report = _inspect(root)
    assert [item["kind"] for item in report.findings] == ["directory_outside_root"]
    assert report.findings[0]["path"] == str(link)
    assert len(report.warnings) == 1 and str(stray) in report.warnings[0]
    assert report.skipped_unmanaged_subtrees == 0
    with pytest.raises(StoreCatalogError, match="directory symlink escapes"):
        StoreCatalog(root, silence_allowlist={}).validate()


@pytest.mark.parametrize("name", [".claude", "output"])
def test_owned_directory_alias_keeps_its_clutter_target_visible(tmp_path, name):
    root = tmp_path / "data"
    clutter = root / "agents/a/000-batch"
    stray = _write(clutter, "stray.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (clutter / "escape").symlink_to(outside, target_is_directory=True)
    (clutter.parent / name).symlink_to(clutter, target_is_directory=True)
    report = _inspect(root)
    assert [item["kind"] for item in report.findings] == ["directory_outside_root"]
    assert len(report.warnings) == 1 and str(stray) in report.warnings[0]
    assert report.skipped_unmanaged_subtrees == 0


@pytest.mark.parametrize("registered", ["present", "absent", "directory"])
def test_registered_store_keeps_clutter_ancestor_visible(tmp_path, registered):
    root = tmp_path / "data"
    clutter = root / "agents/a/batch"
    store = clutter / "nested/store.data"
    if registered == "present":
        _write(clutter, "nested/store.data")
    else:
        store.parent.mkdir(parents=True)
    _write(clutter, "stray.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (clutter / "escape").symlink_to(outside, target_is_directory=True)
    path = clutter if registered == "directory" else store
    identities = store_catalog.read_store_identities([str(path)])
    report = _inspect(root, identities)
    assert [item["kind"] for item in report.findings] == ["directory_outside_root"]
    assert len(report.warnings) == 1 and "stray.db" in report.warnings[0]
    assert report.skipped_unmanaged_subtrees == 0


def test_store_in_sibling_does_not_claim_clutter(tmp_path):
    clutter = tmp_path / "agents/a/batch"
    _write(clutter, "stray.db")
    registered = _write(tmp_path, "agents/a/batch-copy/store.data")
    report = _inspect(tmp_path, store_catalog.read_store_identities([str(registered)]))
    assert report.findings == [] and report.warnings == []
    assert report.skipped_unmanaged_subtrees == 1


@pytest.mark.parametrize("relative", [
    "outside-agents/clutter", "nested/agents/a/clutter", "agents/a/data/clutter",
])
def test_other_directory_layouts_are_still_walked(tmp_path, relative):
    directory = tmp_path / "data" / relative
    _write(directory, "stray.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "escape").symlink_to(outside, target_is_directory=True)
    report = _inspect(tmp_path / "data")
    assert [item["kind"] for item in report.findings] == ["directory_outside_root"]
    assert len(report.warnings) == 1 and "stray.db" in report.warnings[0]
    assert report.skipped_unmanaged_subtrees == 0


def test_agent_root_files_and_outward_directory_link_are_checked(tmp_path):
    root = tmp_path / "data"
    _write(root, "agents/a/stray.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "agents/a/freeform-link"
    link.symlink_to(outside, target_is_directory=True)
    report = _inspect(root)
    assert report.findings[0]["path"] == str(link)
    assert report.findings[0]["kind"] == "directory_outside_root"
    assert len(report.warnings) == 1 and "stray.db" in report.warnings[0]
    assert report.skipped_unmanaged_subtrees == 0


def test_workspace_and_other_clutter_are_counted_once_per_resolved_directory(tmp_path):
    _write(tmp_path, "agents/a/workspace/hidden.db")
    _write(tmp_path, "agents/a/batch/hidden.db")
    (tmp_path / "000-link").symlink_to(
        tmp_path / "agents/a/workspace", target_is_directory=True,
    )
    report = _inspect(tmp_path)
    assert report.findings == [] and report.warnings == []
    assert report.skipped_unmanaged_subtrees == 2


def test_memory_component_paths_are_claimed_but_neighbors_warn(tmp_path):
    _write(tmp_path, "agents/a/data/memory.db")
    _write(tmp_path, "agents/b/data/memory.db")
    stray = _write(tmp_path, "agents/a/data/stray.db")
    catalog = StoreCatalog(tmp_path, silence_allowlist={})
    catalog.register("temporary", ":memory:", journal_mode="memory", owner="fixture")
    warnings = catalog.reconcile_filesystem()
    assert len(warnings) == 1 and str(stray) in warnings[0]
    assert catalog.snapshot()[0].is_memory is True
    assert catalog.inspect_layout().warnings == warnings


@pytest.mark.parametrize("relative", [
    "memory.db", "other/data/memory.db", "agents/a/data/nested/memory.db",
    "agents/a/data/memory-copy.db", "agents/a/memory.db",
])
def test_memory_ownership_matches_only_the_conventional_component_path(tmp_path, relative):
    stray = _write(tmp_path, relative)
    report = _inspect(tmp_path)
    assert len(report.warnings) == 1 and str(stray) in report.warnings[0]


@pytest.mark.parametrize("alias", ["hardlink", "outward", "internal"])
def test_memory_claim_keeps_alias_and_escape_checks(tmp_path, alias):
    root = tmp_path / "data"
    memory = root / "agents/a/data/memory.db"
    memory.parent.mkdir(parents=True)
    if alias == "hardlink":
        store = _write(root, "registered.db")
        os.link(store, memory)
        identities = store_catalog.read_store_identities([str(store)])
        report = _inspect(root, identities)
        assert [item["kind"] for item in report.findings] == ["alias_of_registered_store"]
    elif alias == "outward":
        store = _write(tmp_path, "outside/memory.db")
        memory.symlink_to(store)
        report = _inspect(root)
        assert [item["kind"] for item in report.findings] == ["database_outside_root"]
    else:
        store = _write(root, "agents/a/data/stray.db")
        memory.symlink_to(store)
        report = _inspect(root)
        assert report.warnings and all("stray.db" in warning for warning in report.warnings)


def test_boot_skip_summary_reaches_stderr_without_info_logging(tmp_path, monkeypatch, capsys):
    root = tmp_path / "data"
    _write(root, "agents/a/batch/hidden.db")
    # Boot owns a direct log sink before Uvicorn configures logging handlers.
    monkeypatch.setattr(store_catalog.logger, "disabled", True)
    app = api.create_api(db_path=str(root / "conversations.db"))
    try:
        stderr = capsys.readouterr().err
        assert stderr.count("STORE CATALOG: skipped 1 unmanaged subtree(s)") == 1
        phase = [line for line in stderr.splitlines() if "phase store_catalog_validate" in line]
        assert len(phase) == 1 and "skipped=1" in phase[0]
    finally:
        app.state.store_catalog.shutdown(deadline_seconds=5)


def _assert_alias_findings_match_reference(root, registered):
    identities = store_catalog.read_store_identities([registered])
    before = _ReferenceInspector(root, identities, {}).inspect()
    after = _inspect(root, identities)
    assert {item["kind"] for item in before.findings} == {
        "directory_outside_root", "alias_of_registered_store",
    }
    assert (after.findings, after.warnings) == (before.findings, before.warnings)
    assert after.skipped_unmanaged_subtrees == 0
    return after


@pytest.mark.timeout(5)
@pytest.mark.parametrize("alias", [
    ".claude", ".claude/link", ".claude/nested/link", ".codex/cache/link", "data/links/target",
])
def test_nested_owned_alias_keeps_target_findings(tmp_path, alias):
    root = tmp_path / "data"
    clutter = _write(root, "agents/a/000-clutter/orphan.db").parent
    registered = _write(root, "registered.db")
    os.link(registered, clutter / "alias.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (clutter / "escape").symlink_to(outside, target_is_directory=True)
    link = root / "agents/a" / alias
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(clutter, target_is_directory=True)
    report = _assert_alias_findings_match_reference(root, registered)
    assert len(report.warnings) == 1 and "orphan.db" in report.warnings[0]


@pytest.mark.timeout(5)
@pytest.mark.parametrize("order", ["owned-root-chain", "unowned-first", "owned-first"])
def test_owned_context_survives_alias_hops_and_cycle(tmp_path, order):
    root = tmp_path / "data"
    first = root / "agents/a/100-first"
    inner = first if order == "owned-root-chain" else first / "inner"
    inner.mkdir(parents=True)
    second = _write(root, "agents/a/200-second/orphan.db").parent
    registered = _write(root, "registered.db")
    os.link(registered, second / "alias.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (second / "escape").symlink_to(outside, target_is_directory=True)
    (second / "cycle").symlink_to(first, target_is_directory=True)
    (inner / "next").symlink_to(second, target_is_directory=True)
    owned = root / "agents/a/.claude"
    if order == "owned-root-chain":
        owned.symlink_to(first, target_is_directory=True)
    else:
        owned.mkdir()
        (owned / "link").symlink_to(inner, target_is_directory=True)
        name = "000-deep" if order == "unowned-first" else "zzz-deep"
        (root / name).symlink_to(inner, target_is_directory=True)
    report = _assert_alias_findings_match_reference(root, registered)
    assert len(report.warnings) == 1 and "orphan.db" in report.warnings[0]


@pytest.mark.timeout(5)
@pytest.mark.parametrize("order", ["unowned-first", "owned-first"])
def test_stronger_owned_visit_resumes_children_without_duplicate_warnings(tmp_path, order):
    root = tmp_path / "data"
    owned = root / "agents/a/.claude"
    owned.mkdir(parents=True)
    agent = root / "agents/b"
    clutter = _write(agent, "000-clutter/orphan.db").parent
    _write(agent, "top.db")
    registered = _write(root, "registered.db")
    os.link(registered, clutter / "alias.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (clutter / "escape").symlink_to(outside, target_is_directory=True)
    (clutter / "cycle").symlink_to(agent, target_is_directory=True)
    (owned / "link").symlink_to(agent, target_is_directory=True)
    name = "000-early" if order == "unowned-first" else "zzz-late"
    (root / name).symlink_to(agent, target_is_directory=True)
    report = _assert_alias_findings_match_reference(root, registered)
    assert len(report.warnings) == 2


def test_external_alias_to_clutter_retains_base_inspection(tmp_path):
    root = tmp_path / "data"
    clutter = _write(root, "agents/a/000-clutter/orphan.db").parent
    registered = _write(root, "registered.db")
    os.link(registered, clutter / "alias.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (clutter / "escape").symlink_to(outside, target_is_directory=True)
    (root / "000-alias").symlink_to(clutter, target_is_directory=True)
    _assert_alias_findings_match_reference(root, registered)


def test_registered_clutter_context_keeps_aliased_agent_children_visible(tmp_path):
    root = tmp_path / "data"
    registered = _write(root, "agents/a/batch/store.db")
    clutter = _write(root, "agents/b/000-clutter/orphan.db").parent
    os.link(registered, clutter / "alias.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (clutter / "escape").symlink_to(outside, target_is_directory=True)
    (registered.parent / "link").symlink_to(clutter.parent, target_is_directory=True)
    _assert_alias_findings_match_reference(root, registered)


def test_owned_revisit_reports_persistent_directory_stat_error_once(tmp_path, monkeypatch):
    root = tmp_path / "data"
    inner = root / "agents/b/100-inner"
    denied = inner / "denied"
    denied.mkdir(parents=True)
    owned = root / "agents/a/.claude"
    owned.mkdir(parents=True)
    (owned / "link").symlink_to(inner, target_is_directory=True)
    (root / "000-early").symlink_to(inner, target_is_directory=True)
    original = os.stat

    def metadata(path, *args, **kwargs):
        if not isinstance(path, int) and os.fspath(path) == str(denied):
            raise PermissionError(errno.EACCES, "fixture denied", str(denied))
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", metadata)
        after = _inspect(root)
    error = PermissionError(errno.EACCES, "fixture denied", str(denied))
    # Pin the optimized base's directory diagnostic; the older reference walk
    # stats children before queuing them and labels this "file or directory".
    assert after.findings == []
    assert after.warnings == [
        "could not inspect database directory outside registered store footprint; "
        f"skipping: path={str(denied)!r} error=PermissionError: {error}",
    ]


def test_owned_revisit_retries_directory_after_transient_metadata_error(tmp_path, monkeypatch):
    root = tmp_path / "data"
    inner = root / "agents/b/100-inner"
    denied = _write(inner, "denied/orphan.db").parent
    registered = _write(root, "registered.db")
    os.link(registered, denied / "alias.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    (denied / "escape").symlink_to(outside, target_is_directory=True)
    owned = root / "agents/a/.claude"
    owned.mkdir(parents=True)
    (owned / "link").symlink_to(inner, target_is_directory=True)
    (root / "000-early").symlink_to(inner, target_is_directory=True)
    identities = store_catalog.read_store_identities([registered])
    original = os.stat
    attempts = 0

    def metadata(path, *args, **kwargs):
        nonlocal attempts
        if not isinstance(path, int) and os.fspath(path) == str(denied):
            attempts += 1
            if attempts == 1:
                raise PermissionError(errno.EACCES, "fixture denied", str(denied))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", metadata)
    report = _inspect(root, identities)
    assert attempts == 2
    assert {item["kind"] for item in report.findings} == {
        "directory_outside_root", "alias_of_registered_store",
    }
    assert sum("could not inspect" in warning for warning in report.warnings) == 1
    assert sum("orphan.db" in warning for warning in report.warnings) == 1
    assert report.skipped_unmanaged_subtrees == 0


def _directory_spelling_alias(monkeypatch, directory, mode, *, backing=None):
    """Expose alternate metadata spelling while realpath retains the caller's case."""
    alternate = directory.with_name(directory.name.lower())
    if mode == "native":
        if not alternate.exists():
            pytest.skip("temporary volume is case-sensitive; stubbed coverage runs on every platform")
        assert alternate.samefile(directory)
        return alternate

    real_stat, realpath = os.stat, os.path.realpath
    backing = directory if backing is None else backing

    def metadata(path, *args, **kwargs):
        if not isinstance(path, int):
            value = os.fspath(path)
            if value == str(alternate) or value.startswith(str(alternate) + os.sep):
                path = str(backing) + value[len(str(alternate)):]
        return real_stat(path, *args, **kwargs)

    def resolve(path, *args, **kwargs):
        value = os.fspath(path)
        if any(value == str(parent) or value.startswith(str(parent) + os.sep)
               for parent in (directory, alternate)):
            return os.path.abspath(value)
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", metadata)
    monkeypatch.setattr(os.path, "realpath", resolve)
    return alternate


@pytest.mark.parametrize("mode", ["native", "stubbed"])
@pytest.mark.parametrize("registered", [False, True])
def test_owned_child_case_alias_is_walked(tmp_path, monkeypatch, mode, registered):
    root = tmp_path / "root"
    directory = root / "agents/a/DATA"
    live = _write(directory, "live.db")
    orphan = _write(directory, "orphan.db")
    alternate = _directory_spelling_alias(monkeypatch, directory, mode)
    declared = alternate / "live.db"
    assert os.path.realpath(declared) == str(declared)
    assert alternate.stat().st_ino == directory.stat().st_ino
    identities = store_catalog.read_store_identities([declared]) if registered else {}
    report = _inspect(root, identities)
    assert report.skipped_unmanaged_subtrees == 0
    assert sum(str(orphan) in warning for warning in report.warnings) == 1
    assert len(report.warnings) == (1 if registered else 2)
    assert [(item["kind"], item["path"]) for item in report.findings] == (
        [("alias_of_registered_store", str(live))] if registered else []
    )


@pytest.mark.parametrize("mode", ["native", "stubbed"])
@pytest.mark.parametrize("present", [False, True])
def test_registered_ancestor_case_alias_is_walked(tmp_path, monkeypatch, mode, present):
    root = tmp_path / "root"
    directory = root / "agents/a/BATCH"
    orphan = _write(directory, "orphan.db")
    nested = directory / "nested"
    nested.mkdir()
    live = nested / "live.db"
    if present:
        live.touch()
    alternate = _directory_spelling_alias(monkeypatch, directory, mode)
    declared = alternate / "nested/live.db"
    assert os.path.realpath(declared) == str(declared)
    identities = store_catalog.read_store_identities([declared])
    assert (identities[str(declared)] is not None) == present
    report = _inspect(root, identities)
    assert report.skipped_unmanaged_subtrees == 0
    assert len(report.warnings) == 1 and str(orphan) in report.warnings[0]
    assert [(item["kind"], item["path"]) for item in report.findings] == (
        [("alias_of_registered_store", str(live))] if present else []
    )


def test_distinct_case_sensitive_identities_keep_child_unmanaged(tmp_path, monkeypatch):
    root = tmp_path / "root"
    directory = _write(root, "agents/a/DATA/orphan.db").parent
    backing = _write(tmp_path, "lowercase-backing/live.db").parent
    alternate = _directory_spelling_alias(
        monkeypatch, directory, "stubbed", backing=backing,
    )
    upper_stat, lower_stat = directory.stat(), alternate.stat()
    assert (upper_stat.st_dev, upper_stat.st_ino) != (lower_stat.st_dev, lower_stat.st_ino)
    identities = store_catalog.read_store_identities([alternate / "live.db"])
    report = _inspect(root, identities)
    assert report.findings == [] and report.warnings == []
    assert report.skipped_unmanaged_subtrees == 1


def _workspace_case_fixture(tmp_path, monkeypatch, mode, spelling):
    root = tmp_path / "root"
    name = "WORKSPACE" if spelling == "upper" else "workspace"
    orphan = _write(root, f"agents/a/{name}/orphan.db")
    directory = orphan.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = directory / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    realpath = os.path.realpath
    alternate = _directory_spelling_alias(monkeypatch, directory, mode)
    preserving_realpath = os.path.realpath
    if mode == "stubbed":
        def resolve(path, *args, **kwargs):
            if os.path.islink(path):
                return realpath(path, *args, **kwargs)
            return preserving_realpath(path, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", resolve)
    actual_stat, alternate_stat = directory.stat(), alternate.stat()
    assert (actual_stat.st_dev, actual_stat.st_ino) == (alternate_stat.st_dev, alternate_stat.st_ino)
    assert os.path.realpath(alternate) == str(alternate)
    assert os.path.realpath(escape) == str(outside)
    return root, directory, alternate, orphan, escape


@pytest.mark.parametrize("spelling", ["upper", "lower"])
@pytest.mark.parametrize("mode", ["native", "stubbed"])
@pytest.mark.parametrize("registered_sibling", [False, True])
def test_workspace_case_alias_keeps_skip_precedence(
    tmp_path, monkeypatch, spelling, mode, registered_sibling,
):
    root, directory, _, _, _ = _workspace_case_fixture(tmp_path, monkeypatch, mode, spelling)
    sibling = _write(root, "registered.db") if registered_sibling else None
    identities = store_catalog.read_store_identities([sibling]) if sibling else {}
    original, scanned = os.scandir, []

    def scandir(path):
        scanned.append(os.fspath(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    report = _inspect(root, identities)
    assert report.skipped_unmanaged_subtrees == 1
    assert report.findings == [] and report.warnings == []
    assert not any(path == str(directory) or path.startswith(str(directory) + os.sep)
                   for path in scanned)


@pytest.mark.parametrize("spelling", ["upper", "lower"])
@pytest.mark.parametrize("mode", ["native", "stubbed"])
@pytest.mark.parametrize("registered", ["present", "absent", "directory"])
def test_workspace_case_alias_with_registered_store_is_walked(
    tmp_path, monkeypatch, spelling, mode, registered,
):
    root, directory, alternate, orphan, escape = _workspace_case_fixture(
        tmp_path, monkeypatch, mode, spelling,
    )
    live = directory / "nested/live.db"
    if registered == "present":
        _write(directory, "nested/live.db")
    elif registered == "absent":
        live.parent.mkdir()
    declared = alternate if registered == "directory" else alternate / "nested/live.db"
    identities = store_catalog.read_store_identities([declared])
    report = _inspect(root, identities)
    assert report.skipped_unmanaged_subtrees == 0
    assert len(report.warnings) == 1 and str(orphan) in report.warnings[0]
    expected = [("directory_outside_root", str(escape))]
    if registered == "present" and spelling == "upper":
        expected.append(("alias_of_registered_store", str(live)))
    assert [(item["kind"], item["path"]) for item in report.findings] == expected


@pytest.mark.parametrize("mode", ["native", "stubbed"])
def test_registered_workspace_case_alias_promotes_cached_agent_children(tmp_path, monkeypatch, mode):
    root, directory, alternate, orphan, escape = _workspace_case_fixture(
        tmp_path, monkeypatch, mode, "upper",
    )
    _write(directory, "live.data")
    other = _write(root, "agents/b/batch/other.db")
    (directory / "link").symlink_to(other.parent.parent, target_is_directory=True)
    (root / "000-early").symlink_to(other.parent.parent, target_is_directory=True)
    identities = store_catalog.read_store_identities([alternate / "live.data"])
    report = _inspect(root, identities)
    assert report.skipped_unmanaged_subtrees == 0
    assert [(item["kind"], item["path"]) for item in report.findings] == [
        ("directory_outside_root", str(escape)),
    ]
    assert len(report.warnings) == 2
    assert all(any(str(path) in warning for warning in report.warnings) for path in (orphan, other))
