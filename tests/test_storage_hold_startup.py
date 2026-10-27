"""Scoped findings require an explicit operator decision during deployment."""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from pinky_daemon import api
from pinky_self.server import create_server
from tests.test_startup_check import command_tree as command_tree
from tests.test_startup_check import report_of, run_check, snapshot
from tests.test_startup_preflight import clean_report
from tests.test_startup_preflight import deployment as deployment


def scoped_finding(root, *, kind="directory_outside_root", scope="sample"):
    return dict(kind=kind, path=str(root / "agents/sample/data/escape"),
                resolved_path=str(root.parent / "outside"), detail="fixture escape", scope=scope)


@pytest.mark.parametrize("layout,rc", [("clean", 0), ("scoped", 3), ("mixed", 1)])
def test_real_command_distinguishes_scoped_and_fatal_layouts(command_tree, tmp_path, layout, rc):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if layout != "clean":
        owned = root / "agents/sample/data"
        owned.mkdir(parents=True)
        (owned / "escape").symlink_to(outside, target_is_directory=True)
    if layout == "mixed":
        (root / "fatal").symlink_to(outside, target_is_directory=True)
    before = snapshot(root)
    result = run_check(command_tree, root / "conversations.db")
    assert result.returncode == rc, result.stderr + result.stdout
    report = report_of(result, root, ok=layout == "clean")
    assert snapshot(root) == before
    scoped = [row for row in report["findings"] if row.get("scope")]
    assert len(scoped) == (0 if layout == "clean" else 1)
    assert all(row["scope"] == "sample" for row in scoped)


@pytest.mark.parametrize("rc", [1, 3])
def test_target_report_preserves_scope_and_future_report_fields(tmp_path, rc):
    report = clean_report(tmp_path, ok=False, findings=[scoped_finding(tmp_path)],
                          future_detail={"fixture": True})
    assert api._startup_target_report(json.dumps(report), rc) == report


@pytest.mark.parametrize("kind", ["alias_of_registered_store", "uninspectable_in_store_footprint", "future_kind"])
def test_exit_three_never_accepts_scope_on_an_ineligible_kind(tmp_path, kind):
    report = clean_report(tmp_path, ok=False, findings=[scoped_finding(tmp_path, kind=kind)])
    with pytest.raises(ValueError):
        api._startup_target_report(json.dumps(report), 3)


@pytest.mark.parametrize("entry", ["update", "restart"])
@pytest.mark.parametrize("accept", [False, True])
@pytest.mark.parametrize("force", [False, True])
async def test_current_preflight_requires_explicit_scoped_acceptance(deployment, entry, accept, force):
    fx = deployment
    directory = fx.data / "agents/sample/data"
    directory.mkdir(parents=True)
    outside = fx.data.parent / "outside"
    outside.mkdir()
    (directory / "escape").symlink_to(outside, target_is_directory=True)
    fx.set_target(report=clean_report(fx.data))
    kwargs = {"force": force} if entry == "update" else {}
    if accept:
        kwargs["accept_findings"] = True
    try:
        result = await getattr(fx, entry)(**kwargs)
    except TypeError as exc:
        pytest.fail(f"Explicit acceptance must be available: {exc}")
    assert result["preflight"]["ok"] is False
    assert result["preflight"]["findings"][0].get("scope") == "sample"
    assert result.get("restarting", False) is accept, result
    assert bool(fx.scheduled) is accept
    if accept:
        assert result["preflight"]["accepted"] is True
    else:
        assert "error" in result and not fx.children


@pytest.mark.parametrize("accept", [False, True])
async def test_target_scoped_refusal_undo_and_explicit_acceptance(deployment, accept):
    fx = deployment
    finding = scoped_finding(fx.data)
    fx.set_target(report=clean_report(fx.data, ok=False, findings=[finding]), rc=3)
    kwargs = {"accept_findings": True} if accept else {}
    try:
        result = await fx.update(**kwargs)
    except TypeError as exc:
        pytest.fail(f"Explicit acceptance must be available: {exc}")
    assert result["target_check"]["rc"] == 3
    assert result["target_check"]["findings"] == [finding]
    if accept:
        assert result["restarting"] is True and result["target_check"]["accepted"] is True
        assert result["target_check"]["ok"] is False
        assert fx.git("rev-parse", "HEAD") == fx.target and fx.scheduled
    else:
        assert result["target_check"]["reason"] == "findings"
        assert result["undo"]["ok"] is True and not fx.scheduled
        assert fx.git("rev-parse", "HEAD") == fx.previous


@pytest.mark.parametrize("kind,scope", [("directory_outside_root", None),
                                        ("alias_of_registered_store", "sample"),
                                        ("uninspectable_in_store_footprint", "sample"),
                                        ("future_kind", "sample")])
async def test_acceptance_cannot_bypass_fatal_or_unknown_target_findings(deployment, kind, scope):
    fx = deployment
    finding = scoped_finding(fx.data, kind=kind, scope=scope)
    fx.set_target(report=clean_report(fx.data, ok=False, findings=[finding]), rc=1)
    try:
        result = await fx.update(force=True, accept_findings=True)
    except TypeError as exc:
        pytest.fail(f"Explicit acceptance must be available: {exc}")
    assert "error" in result and result["undo"]["ok"] is True
    assert result["target_check"]["findings"] == [finding] and not fx.scheduled
    assert fx.git("rev-parse", "HEAD") == fx.previous


@pytest.mark.parametrize("tool", ["check_for_updates", "update_and_restart", "restart_daemon"])
@pytest.mark.parametrize("accept", [False, True])
def test_self_tools_forward_acceptance_and_render_complete_findings(tmp_path, monkeypatch, tool, accept):
    server = create_server(agent_name="sample", api_url="http://fixture.invalid", tool_gates=["admin"])
    fn = next(t.fn for t in server._tool_manager.list_tools() if t.name == tool)
    parameters = inspect.signature(fn).parameters
    assert "accept_findings" in parameters and parameters["accept_findings"].default is False
    seen = []
    finding = scoped_finding(tmp_path)

    def urlopen(request, **kwargs):
        seen.append(request.full_url)
        response = MagicMock()
        response.read.return_value = json.dumps({
            "updated": True, "restarting": True,
            "preflight": {"ok": False, "findings": [finding], "accepted": accept},
        }).encode()
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    text = fn(accept_findings=accept)
    assert parse_qs(urlsplit(seen[0]).query).get("accept_findings") == (["true"] if accept else None)
    assert finding["path"] in text and finding["resolved_path"] in text and finding["detail"] in text
    assert "sample" in text
    if accept:
        assert "accepted" in text.lower()
