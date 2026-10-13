"""Admin tool rendering and request deadlines with a recording HTTP stand-in."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from pinky_self.server import create_server


@pytest.fixture
def admin_tools(monkeypatch):
    state = SimpleNamespace(response={}, calls=[])

    def request(req, *, timeout):
        state.calls.append((req, timeout))
        return io.BytesIO(json.dumps(state.response).encode())

    monkeypatch.setattr("urllib.request.urlopen", request)
    server = create_server(
        agent_name="fixture-caller",
        api_url="http://fixture.invalid",
        tool_gates=["admin"],
    )
    state.tools = {tool.name: tool.fn for tool in server._tool_manager.list_tools()}
    return state


def _findings(tmp_path):
    kinds = (
        "directory_outside_root",
        "database_outside_root",
        "uninspectable_in_store_footprint",
        "alias_of_registered_store",
    )
    return [
        {
            "kind": kinds[index % len(kinds)],
            "path": str(tmp_path / "root" / f"entry-{index:02}.db"),
            "resolved_path": (
                None if index == 2 else str(tmp_path / "outside" / f"target-{index:02}.db")
            ),
            "detail": f"Layout detail for entry {index:02}",
        }
        for index in range(25)
    ]


def _success(tool):
    if tool == "check_for_updates":
        return {"up_to_date": True, "current_hash": "1111111", "branch": "main"}
    if tool == "update_and_restart":
        return {
            "updated": True,
            "before_hash": "1111111",
            "after_hash": "2222222",
            "restarting": True,
        }
    return {"restarting": True, "git_hash": "1111111"}


@pytest.mark.parametrize("tool", ["check_for_updates", "update_and_restart", "restart_daemon"])
@pytest.mark.parametrize("declined", [False, True])
def test_admin_tool_text_preserves_all_layout_findings(admin_tools, tmp_path, tool, declined):
    findings = _findings(tmp_path) if declined else []
    response = _success(tool)
    response["preflight"] = {"ok": not declined, "findings": findings, "warnings_count": 37}
    if declined:
        response["error"] = "startup layout pre-flight failed"
        response["staying_on_version"] = "3333333"
    admin_tools.response = response

    text = admin_tools.tools[tool]()

    assert "37" in text
    if declined:
        assert response["error"] in text
        assert "3333333" in text
        for finding in findings:
            for key in ("kind", "path", "detail"):
                assert finding[key] in text
            if finding["resolved_path"] is not None:
                assert finding["resolved_path"] in text
        assert "unresolved" in text.lower()
    else:
        assert "passed" in text.lower()
        assert "failed" not in text.lower()
        if tool == "check_for_updates":
            assert "Already up to date at 1111111 on main." in text
        else:
            assert "restarting" in text.lower()


@pytest.mark.parametrize("up_to_date", [False, True])
def test_preview_retains_findings_on_each_success_branch(admin_tools, tmp_path, up_to_date):
    findings = _findings(tmp_path)
    admin_tools.response = {
        "up_to_date": up_to_date,
        "current_hash": "1111111",
        "pending_commits": 1,
        "commits": ["Fixture change"],
        "preflight": {"ok": False, "findings": findings, "warnings_count": 0},
    }

    text = admin_tools.tools["check_for_updates"]()

    for finding in findings:
        assert finding["path"] in text
    assert ("Already up to date" if up_to_date else "Updates available") in text


@pytest.mark.parametrize("tool", ["check_for_updates", "update_and_restart", "restart_daemon"])
@pytest.mark.parametrize("incomplete", [False, True])
def test_admin_tool_text_distinguishes_incomplete_checks(
    admin_tools, tmp_path, tool, incomplete,
):
    response = _success(tool)
    reason = f"PermissionError: {tmp_path / 'root' / 'store.db'}"
    if incomplete:
        response.update({
            "error": "startup layout pre-flight could not complete",
            "preflight": {"ok": False, "incomplete": True, "reason": reason, "findings": []},
            "staying_on_version": "4444444",
        })
    else:
        response["preflight"] = {"ok": True, "findings": [], "warnings_count": 0}
    admin_tools.response = response

    text = admin_tools.tools[tool]()

    if incomplete:
        assert "incomplete" in text.lower()
        assert reason in text
        assert "4444444" in text
    else:
        assert "passed" in text.lower()
        assert reason not in text
        assert "incomplete" not in text.lower()


@pytest.mark.parametrize("tool", ["check_for_updates", "update_and_restart", "restart_daemon"])
def test_admin_tool_text_accepts_future_minimal_findings(admin_tools, tmp_path, tool):
    finding = {"kind": "future_layout_rule", "path": str(tmp_path / "root" / "future.db")}
    admin_tools.response = {
        "error": "startup layout pre-flight failed",
        "preflight": {"ok": False, "findings": [finding], "warnings_count": 0},
    }

    text = admin_tools.tools[tool]()

    assert finding["kind"] in text
    assert finding["path"] in text


@pytest.mark.parametrize(
    "reason,rc",
    [
        ("findings", 1),
        ("incomplete", 2),
        ("timeout", None),
        ("spawn_failed", None),
        ("invalid_report", 0),
        ("source_unreadable", 128),
    ],
)
@pytest.mark.parametrize("restored", [False, True])
def test_update_tool_reports_target_check_and_undo(admin_tools, tmp_path, reason, rc, restored):
    previous_ref = "1" * 40
    target_ref = "2" * 40
    finding = {"kind": "future_target_rule", "path": str(tmp_path / "root" / "future.db")}
    error = None if restored else "The working tree still holds the target commit."
    stderr_tail = "Child diagnostic: " + "x" * 480
    admin_tools.response = {
        "error": "target release failed its startup check",
        "target_check": {"reason": reason, "rc": rc, "findings": [finding],
                         "stderr_tail": stderr_tail},
        "undo": {"checked_out": previous_ref if restored else None, "ok": restored,
                 "deps_restored": restored, "error": error},
        "staying_on_version": previous_ref if restored else target_ref,
    }

    text = admin_tools.tools["update_and_restart"]()

    assert reason in text
    assert finding["kind"] in text
    assert finding["path"] in text
    assert stderr_tail in text
    assert "Undo" in text
    assert ("succeeded" if restored else "failed") in text.lower()
    assert "dependencies" in text.lower()
    assert (previous_ref if restored else target_ref) in text
    if rc is not None:
        assert f"{rc}" in text
    if not restored:
        assert error in text
        assert "next restart would start it" in text.lower()


@pytest.mark.parametrize("deps_restored", [None, False, True])
def test_update_tool_distinguishes_dependency_restore_states(admin_tools, deps_restored):
    admin_tools.response = {
        "error": "target release failed its startup check",
        "target_check": {"reason": "incomplete", "rc": 2, "findings": [], "stderr_tail": ""},
        "undo": {"checked_out": "1" * 40, "ok": deps_restored is not False,
                 "deps_restored": deps_restored, "error": None},
    }

    text = admin_tools.tools["update_and_restart"]().lower()

    expected = {None: "not needed", False: "failed", True: "restored"}[deps_restored]
    assert f"dependencies: {expected}" in text


@pytest.mark.parametrize("checkout_confirmed", [False, True])
def test_update_tool_distinguishes_requested_and_confirmed_undo(admin_tools, checkout_confirmed):
    previous_ref = "1" * 40
    warning = (
        "Undo checkout is not confirmed. The working tree still holds the target "
        "commit; the next restart would start it."
    )
    response = {
        "error": "target release failed its startup check",
        "target_check": {"reason": "incomplete", "rc": 2, "findings": [], "stderr_tail": ""},
        "undo": {"checked_out": previous_ref, "ok": False, "deps_restored": False,
                 "error": "fixture undo failure"},
    }
    if not checkout_confirmed:
        response["undo_warning"] = warning
    admin_tools.response = response

    text = admin_tools.tools["update_and_restart"]()

    assert previous_ref in text
    assert "Undo: failed" in text
    assert "Dependencies: failed" in text
    assert (f"Checked out: {previous_ref}" in text) is checkout_confirmed
    assert (warning in text) is (not checkout_confirmed)


@pytest.mark.parametrize("skipped", [False, True])
def test_update_tool_reports_skipped_target_check(admin_tools, skipped):
    response = _success("update_and_restart")
    if skipped:
        response["target_check"] = {"skipped": "target has no startup check"}
    admin_tools.response = response

    text = admin_tools.tools["update_and_restart"]()

    assert "restarting" in text.lower()
    assert ("target has no startup check" in text) is skipped


@pytest.mark.parametrize("preflight", [False, True])
def test_preview_preflight_is_explicit_and_sets_its_deadline(admin_tools, preflight):
    admin_tools.response = _success("check_for_updates")

    admin_tools.tools["check_for_updates"](preflight=preflight)

    assert len(admin_tools.calls) == 1
    request, timeout = admin_tools.calls[0]
    query = parse_qs(urlsplit(request.full_url).query)
    assert query == ({"dry_run": ["true"], "preflight": ["true"]}
                     if preflight else {"dry_run": ["true"]})
    assert timeout == (1800 if preflight else 30)


@pytest.mark.parametrize(
    "tool,timeout", [("update_and_restart", 1800), ("restart_daemon", 1800),
                     ("check_for_updates", 30), ("load_my_context", 30)],
)
def test_admin_tools_allow_startup_check_request_time(admin_tools, tool, timeout):
    admin_tools.response = _success(tool)

    admin_tools.tools[tool]()

    assert len(admin_tools.calls) == 1
    assert admin_tools.calls[0][1] == timeout
