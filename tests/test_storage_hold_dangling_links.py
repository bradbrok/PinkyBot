"""A recorded outward link keeps its hold when only the target disappears."""

from __future__ import annotations

import copy
import os

import httpx
import pytest

from pinky_daemon.api import create_api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie


@pytest.mark.parametrize("entry", ["directory", "nested", "nested-with-other"])
async def test_regression_missing_target_does_not_clear_recorded_outward_link(tmp_path, entry):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    app = create_api(db_path=str(root / "conversations.db"))
    registry = app.state.agents
    for name in ("bob", "alice"):
        directory = root / "agents" / name
        directory.mkdir(parents=True, exist_ok=True)
        registry.register(name, working_dir=str(directory))
    registry.set_main_agent("bob")
    outside = tmp_path / "outside"
    outside.mkdir()
    if entry == "directory":
        link = root / "agents" / "alice"
        link.rename(root / "previous")
    else:
        link = root / "agents" / "alice" / "data" / "escape"
        link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    catalog = app.state.store_catalog
    report = catalog.inspect_layout(registered_agents={"alice", "bob"})
    findings = [row for row in report.findings if row.get("scope") == "alice"]
    assert len(findings) == 1
    app.state.storage_holds = {"alice": copy.deepcopy(findings)}
    before = copy.deepcopy(app.state.storage_holds)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            cookies={SESSION_COOKIE_NAME: create_session_cookie(os.environ["PINKY_SESSION_SECRET"])},
        ) as client:
            response = await client.post("/agents/alice/storage/recheck")
            assert response.status_code == 200, response.text
            assert response.json()["cleared"] is False
            assert response.json()["reason"] == "findings"
            assert app.state.storage_holds == before

            outside.rmdir()
            assert os.path.lexists(link) and link.is_symlink() and not link.exists()
            other = None
            if entry == "nested-with-other":
                target = tmp_path / "other"
                target.mkdir()
                other = link.with_name("other")
                other.symlink_to(target, target_is_directory=True)

            response = await client.post("/agents/alice/storage/recheck")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["cleared"] is False
            assert body["reason"] == "incomplete"
            assert body["storage_hold"]["held"] is True
            assert app.state.storage_holds == before

            link.unlink()
            if other is not None:
                other.unlink()
            response = await client.post("/agents/alice/storage/recheck")
            assert response.status_code == 200, response.text
            assert response.json()["cleared"] is True
            assert "alice" not in app.state.storage_holds
    finally:
        catalog.shutdown(deadline_seconds=5)
