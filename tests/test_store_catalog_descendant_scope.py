"""Directory aliases retain the ownership scopes of their descendants."""

import os
from collections import Counter
from pathlib import Path

import pytest

from pinky_daemon import store_catalog as stores


def inspect(root, registered=("alice",)):
    return stores.inspect_filesystem_layout(
        root, {}, silence_allowlist={}, registered_agents=registered
    )


@pytest.mark.parametrize("alias_name", ["000-shared", "zzz-shared"])
def test_alias_of_agents_parent_keeps_both_descendant_scopes(tmp_path, alias_name):
    root = tmp_path / "root"
    data = root / "agents/alice/data"
    data.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "escape").symlink_to(outside, target_is_directory=True)
    alias = root / alias_name
    alias.symlink_to(root / "agents", target_is_directory=True)
    report = inspect(root)
    actual = {(f["path"], f.get("scope")) for f in report.findings}
    expected = {(str(data / "escape"), "alice"), (str(alias / "alice/data/escape"), None)}
    assert actual == expected


@pytest.mark.parametrize("alias_name", ["000-shared", "zzz-shared"])
def test_control_alias_of_agent_home_keeps_both_scopes(tmp_path, alias_name):
    root = tmp_path / "root"
    data = root / "agents/alice/data"
    data.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "escape").symlink_to(outside, target_is_directory=True)
    alias = root / alias_name
    alias.symlink_to(root / "agents/alice", target_is_directory=True)
    actual = {(f["path"], f.get("scope")) for f in inspect(root).findings}
    assert actual == {(str(data / "escape"), "alice"), (str(alias / "data/escape"), None)}


def _reconcile_alias_case(tmp_path, target):
    root = tmp_path / "root"
    data = root / "agents/alice/data"
    data.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "escape").symlink_to(outside, target_is_directory=True)
    (root / "zzz-shared").symlink_to(root / target, target_is_directory=True)
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    try:
        with pytest.raises(stores.StoreCatalogError, match="symlink escapes"):
            catalog.reconcile_filesystem(scoped_hold=True, registered_agents={"alice"})
    finally:
        catalog.close()


def test_agents_parent_alias_requires_daemon_wide_refusal(tmp_path):
    _reconcile_alias_case(tmp_path, "agents")


def test_control_agent_home_alias_keeps_daemon_wide_refusal(tmp_path):
    _reconcile_alias_case(tmp_path, "agents/alice")


@pytest.mark.timeout(5)
@pytest.mark.parametrize("alias_name", ["000-loop", "zzz-loop"])
def test_root_alias_retains_descendant_scopes_and_bounded_cycles(tmp_path, monkeypatch, alias_name):
    root = tmp_path / "root"
    data = root / "agents/alice/data"
    data.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "escape").symlink_to(outside, target_is_directory=True)
    alias = root / alias_name
    alias.symlink_to(root, target_is_directory=True)
    calls = Counter()
    original = os.scandir

    def scandir(path):
        calls[os.fspath(path)] += 1
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    report = inspect(root)

    assert {(finding["path"], finding.get("scope")) for finding in report.findings} == {
        (str(data / "escape"), "alice"),
        (str(alias / "agents/alice/data/escape"), None),
    }
    assert len(report.findings) == 2
    assert calls[str(root)] == 2
    assert calls[str(root / "agents")] == 2
    catalog = stores.StoreCatalog(expected_root=root, silence_allowlist={})
    try:
        with pytest.raises(stores.StoreCatalogError, match="symlink escapes"):
            catalog.reconcile_filesystem(scoped_hold=True, registered_agents={"alice"})
    finally:
        catalog.close()


@pytest.mark.timeout(5)
def test_agent_link_to_agents_container_keeps_all_descendants_in_its_scope(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("alice", "bob"):
        data = root / "agents" / name / "data"
        data.mkdir(parents=True)
        (data / "escape").symlink_to(outside, target_is_directory=True)
    link = root / "agents/alice/link"
    link.symlink_to(root / "agents", target_is_directory=True)
    registered = root / "agents/bob/data/tasks.db"
    registered.touch()

    report = stores.inspect_filesystem_layout(
        root,
        stores.read_store_identities([registered]),
        silence_allowlist={},
        registered_agents={"alice", "bob"},
    )

    assert {(finding["path"], finding.get("scope")) for finding in report.findings} == {
        (str(root / "agents/alice/data/escape"), "alice"),
        (str(root / "agents/bob/data/escape"), "bob"),
        (str(link / "bob/data/escape"), "alice"),
    }
    descendants = [
        finding for finding in report.findings if finding["path"].startswith(str(link) + os.sep)
    ]
    assert descendants and all(finding.get("scope") == "alice" for finding in descendants)
