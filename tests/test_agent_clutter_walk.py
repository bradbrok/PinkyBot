"""Agent content is pruned while daemon paths retain filesystem validation."""

from __future__ import annotations

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
