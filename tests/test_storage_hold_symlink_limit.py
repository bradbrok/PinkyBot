"""Recorded outward links retain storage holds beyond the kernel alias limit."""

from __future__ import annotations

import copy
import errno
import os

import httpx
import pytest

from pinky_daemon.api import create_api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.store_catalog import LayoutInspection
from tests.test_store_catalog_symlink_limit import _long_alias_tree


@pytest.mark.parametrize("check_errno", [None, errno.EACCES, errno.ELOOP])
async def test_long_alias_missing_target_keeps_hold_until_link_removed(
    tmp_path, monkeypatch, check_errno,
):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    app = create_api(db_path=str(root / "conversations.db"))
    catalog = app.state.store_catalog
    try:
        _, alias, directory = _long_alias_tree(tmp_path)
        app.state.agents.register("bob", working_dir=str(root / "agents" / "bob"))
        app.state.agents.register("alice", working_dir=str(root / "agents" / "alice"))
        app.state.agents.set_main_agent("bob")
        outside = tmp_path / "outside"
        outside.mkdir()
        physical_link = directory / "escape"
        physical_link.symlink_to(outside, target_is_directory=True)
        report = catalog.inspect_layout(registered_agents={"alice"})
        findings = [finding for finding in report.findings if finding.get("scope") == "alice"]
        assert len(findings) == 1
        assert findings[0]["path"] == str(alias / "escape")
        assert findings[0]["kind"] == "directory_outside_root"
        app.state.storage_holds = {"alice": copy.deepcopy(findings)}
        before = copy.deepcopy(app.state.storage_holds)
        link_identity = physical_link.lstat().st_ino
        relocated = root / "agents" / "alice" / "data" / "links"
        directory.parent.rename(relocated)
        alias.unlink()
        alias.symlink_to(relocated / "d0", target_is_directory=True)
        physical_link = relocated / "d48" / "escape"
        assert physical_link.lstat().st_ino == link_identity
        outside.rmdir()
        assert physical_link.is_symlink() and not physical_link.exists()
        scoped = catalog.inspect_layout(scope="alice", registered_agents={"alice", "bob"})
        assert scoped.findings == []
        assert not any(warning.startswith("could not inspect") for warning in scoped.warnings)
        original_lstat = os.lstat

        def denied_lstat(path, *args, **kwargs):
            if os.fspath(path) == str(physical_link):
                raise OSError(check_errno, os.strerror(check_errno), os.fspath(path))
            return original_lstat(path, *args, **kwargs)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            cookies={SESSION_COOKIE_NAME: create_session_cookie(os.environ["PINKY_SESSION_SECRET"])},
        ) as client:
            with monkeypatch.context() as patch:
                if check_errno is not None:
                    patch.setattr(catalog, "inspect_layout", lambda **kwargs: LayoutInspection([], []))
                    patch.setattr(os, "lstat", denied_lstat)
                response = await client.post("/agents/alice/storage/recheck")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["cleared"] is False
            assert body["reason"] == "incomplete"
            assert body["storage_hold"]["held"] is True
            assert app.state.storage_holds == before
            if check_errno is not None:
                assert body["error"] == ("PermissionError" if check_errno == errno.EACCES else "OSError")

            physical_link.unlink()
            response = await client.post("/agents/alice/storage/recheck")
            assert response.status_code == 200, response.text
            assert response.json()["cleared"] is True
            assert "alice" not in app.state.storage_holds
    finally:
        catalog.shutdown(deadline_seconds=5)
