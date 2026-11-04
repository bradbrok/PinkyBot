"""A retained link's parent must remain identifiable before a hold can clear."""

from __future__ import annotations

import copy
import os
from types import SimpleNamespace

import httpx
import pytest

from pinky_daemon.api import create_api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from tests.test_store_catalog_symlink_limit import _long_alias_tree

PARENT_FIELDS = ("parent_path", "parent_dev", "parent_ino")


@pytest.fixture
async def parent_hold(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    app = create_api(db_path=str(root / "conversations.db"))
    catalog = app.state.store_catalog
    try:
        _, alias, directory = _long_alias_tree(tmp_path)
        for name in ("bob", "alice"):
            app.state.agents.register(name, working_dir=str(root / "agents" / name))
        app.state.agents.set_main_agent("bob")
        outside = tmp_path / "outside"
        outside.mkdir()
        leaf = directory / "escape"
        leaf.symlink_to(outside, target_is_directory=True)
        original_parent = directory.stat()
        catalog.shutdown(deadline_seconds=5)
        app = create_api(db_path=str(root / "conversations.db"))
        catalog = app.state.store_catalog
        findings = copy.deepcopy(app.state.storage_holds["alice"])
        assert len(findings) == 1
        assert findings[0]["path"] == str(alias / "escape")
        identity = (leaf.lstat().st_dev, leaf.lstat().st_ino)
        relocated = root / "agents" / "alice" / "data" / "links"
        directory.parent.rename(relocated)
        alias.unlink()
        alias.symlink_to(relocated / "d0", target_is_directory=True)
        leaf = relocated / "d48" / "escape"
        outside.rmdir()
        scan = catalog.inspect_layout(scope="alice", registered_agents={"alice", "bob"})
        assert scan.findings == []
        assert not any(warning.startswith("could not inspect") for warning in scan.warnings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={
                SESSION_COOKIE_NAME: create_session_cookie(os.environ["PINKY_SESSION_SECRET"])
            },
        ) as client:
            yield SimpleNamespace(
                app=app,
                catalog=catalog,
                client=client,
                root=root,
                alias=alias,
                leaf=leaf,
                identity=identity,
                finding=findings[0],
                original_parent_path=str(directory),
                original_parent_identity=(original_parent.st_dev, original_parent.st_ino),
            )
    finally:
        catalog.shutdown(deadline_seconds=5)


async def _recheck(h):
    response = await h.client.post("/agents/alice/storage/recheck")
    assert response.status_code == 200, response.text
    return response.json()


def _retained(h, body, before, leaf=None):
    leaf = h.leaf if leaf is None else leaf
    metadata = leaf.lstat()
    assert leaf.is_symlink()
    assert (metadata.st_dev, metadata.st_ino) == h.identity
    assert body["cleared"] is False
    assert body["reason"] == "incomplete"
    assert body["storage_hold"]["held"] is True
    assert h.app.state.storage_holds == before


@pytest.mark.parametrize(
    "change",
    ["unchanged", "leaf-removed", "parent-missing", "parent-file", "parent-clean", "parent-loop"],
)
async def test_parent_alias_changes_cannot_prove_leaf_removal(parent_hold, change):
    h = parent_hold
    before = copy.deepcopy(h.app.state.storage_holds)
    if change == "leaf-removed":
        h.leaf.unlink()
    elif change.startswith("parent-"):
        h.alias.unlink()
        if change == "parent-file":
            h.alias.touch()
        elif change == "parent-clean":
            clean = h.alias.parent / "clean"
            clean.mkdir()
            h.alias.symlink_to(clean, target_is_directory=True)
        elif change == "parent-loop":
            h.alias.symlink_to(h.alias.name, target_is_directory=True)

    body = await _recheck(h)

    if change == "leaf-removed":
        assert body["cleared"] is True
        assert "alice" not in h.app.state.storage_holds
    else:
        _retained(h, body, before)


def _arm_after_scan(h, monkeypatch, action, *, after_lstat=False):
    original_scan = h.catalog.inspect_layout
    original_lstat = os.lstat
    armed, events = [], []

    def scan(**kwargs):
        result = original_scan(**kwargs)
        assert result.findings == []
        assert not any(warning.startswith("could not inspect") for warning in result.warnings)
        armed.append(True)
        return result

    def lstat(path, *args, **kwargs):
        physical = os.fspath(path) == str(h.leaf)
        anchored = (
            os.fspath(path) == h.leaf.name
            and kwargs.get("dir_fd") is not None
            and (os.fstat(kwargs["dir_fd"]).st_dev, os.fstat(kwargs["dir_fd"]).st_ino)
            == h.original_parent_identity
        )
        if armed and not events and (physical or anchored):
            if after_lstat:
                metadata = original_lstat(path, *args, **kwargs)
            action()
            events.append(True)
            if after_lstat:
                return metadata
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(h.catalog, "inspect_layout", scan)
    monkeypatch.setattr(os, "lstat", lstat)
    return events


@pytest.mark.parametrize(
    "change", ["parent-missing", "parent-file", "parent-loop", "parent-clean-after-lstat"],
)
async def test_parent_changes_during_leaf_check_keep_hold(parent_hold, monkeypatch, change):
    h = parent_hold
    before = copy.deepcopy(h.app.state.storage_holds)
    moved = h.leaf.parent.with_name("moved")
    if change == "parent-clean-after-lstat":
        (h.leaf.parent / "destination").symlink_to(h.finding["resolved_path"])
        h.leaf.unlink()
        h.leaf.symlink_to("destination")
        h.identity = (h.leaf.lstat().st_dev, h.leaf.lstat().st_ino)

    def change_parent():
        if change == "parent-loop":
            h.alias.unlink()
            h.alias.symlink_to(h.alias.name, target_is_directory=True)
        else:
            h.leaf.parent.rename(moved)
            if change == "parent-file":
                h.leaf.parent.touch()
            elif change == "parent-clean-after-lstat":
                h.leaf.parent.mkdir()
                (h.leaf.parent / "destination").mkdir()

    with monkeypatch.context() as patch:
        events = _arm_after_scan(
            h, patch, change_parent,
            after_lstat=change in {"parent-loop", "parent-clean-after-lstat"},
        )
        body = await _recheck(h)

    assert events == [True]
    _retained(h, body, before, h.leaf if change == "parent-loop" else moved / "escape")


@pytest.mark.parametrize("change", ["parent-missing", "parent-file", "leaf-removed"])
async def test_old_hold_without_parent_fields_keeps_existing_record(parent_hold, change):
    h = parent_hold
    old = {key: value for key, value in h.finding.items() if key not in PARENT_FIELDS}
    h.app.state.storage_holds = {"alice": [copy.deepcopy(old)]}
    before = copy.deepcopy(h.app.state.storage_holds)
    response = await h.client.get("/agents/alice")
    assert response.json()["storage_hold"]["findings"] == [old]
    if change == "leaf-removed":
        h.leaf.unlink()
    else:
        h.alias.unlink()
        if change == "parent-file":
            h.alias.touch()

    body = await _recheck(h)

    if change == "leaf-removed":
        assert body["cleared"] is True
    else:
        _retained(h, body, before)
        assert body["storage_hold"]["findings"] == [old]


async def test_hold_records_original_parent_identity_and_preserves_public_view(parent_hold):
    h = parent_hold
    assert h.finding.get("parent_path") == h.original_parent_path
    assert (h.finding.get("parent_dev"), h.finding.get("parent_ino")) == h.original_parent_identity
    response = await h.client.get("/agents/alice")
    plain = {key: value for key, value in h.finding.items() if key not in PARENT_FIELDS}
    assert response.json()["storage_hold"]["findings"] == [plain]


async def test_different_directory_at_same_parent_path_keeps_hold(parent_hold):
    h = parent_hold
    before = copy.deepcopy(h.app.state.storage_holds)
    moved = h.leaf.parent.with_name("original-parent")
    h.leaf.parent.rename(moved)
    h.leaf.parent.mkdir()
    metadata = h.leaf.parent.stat()
    assert (metadata.st_dev, metadata.st_ino) != h.original_parent_identity

    body = await _recheck(h)

    _retained(h, body, before, moved / "escape")


async def test_target_loop_created_after_leaf_lstat_keeps_hold(parent_hold, monkeypatch):
    h = parent_hold
    before = copy.deepcopy(h.app.state.storage_holds)
    target = h.alias.parent / "target"
    target.symlink_to(h.finding["resolved_path"])
    h.leaf.unlink()
    h.leaf.symlink_to(target)
    identity = (h.leaf.lstat().st_dev, h.leaf.lstat().st_ino)

    def loop_target():
        target.unlink()
        target.symlink_to(target.name)

    with monkeypatch.context() as patch:
        events = _arm_after_scan(h, patch, loop_target, after_lstat=True)
        body = await _recheck(h)

    assert events == [True]
    assert (h.leaf.lstat().st_dev, h.leaf.lstat().st_ino) == identity
    assert body["cleared"] is False
    assert body["reason"] == "incomplete"
    assert h.app.state.storage_holds == before


@pytest.mark.parametrize("replacement", ["regular", "inward"])
async def test_repaired_leaf_in_same_parent_clears_hold(parent_hold, replacement):
    h = parent_hold
    h.leaf.unlink()
    if replacement == "regular":
        h.leaf.touch()
    else:
        h.leaf.symlink_to(h.root / "agents" / "alice", target_is_directory=True)

    body = await _recheck(h)

    assert body["cleared"] is True
    assert "alice" not in h.app.state.storage_holds
