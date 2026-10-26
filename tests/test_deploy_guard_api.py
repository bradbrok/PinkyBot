"""The update endpoint arms only checked releases and exposes safe status."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from tests.test_deploy_guard import TOKEN, guard_module, load, record, save
from tests.test_deploy_guard import harness as _harness
from tests.test_startup_preflight import clean_report
from tests.test_startup_preflight import deployment as _deployment

harness = _harness
deployment = _deployment


@pytest.mark.parametrize("outcome", ["restart", "failed-check", "preview", "no-restart"])
async def test_arm_only_after_successful_target_check_and_frontend(
    deployment, monkeypatch, outcome
):
    g = guard_module()
    fx = deployment
    if outcome == "failed-check":
        fx.set_target(
            report=clean_report(
                fx.data,
                ok=False,
                findings=[dict(kind="directory_outside_root", path=str(fx.data / "outside"))],
            ),
            rc=1,
        )
    else:
        fx.set_target(report=clean_report(fx.data))
    if outcome == "no-restart":
        fx.target = fx.previous
    arm, events = g.arm_deploy, []

    def capture(*args, **kwargs):
        assert not fx.scheduled
        assert fx.children and not kwargs.get("dry_run", False)
        events.append("armed")
        return arm(*args, **kwargs)

    monkeypatch.setattr(g, "arm_deploy", capture)
    body = await fx.update(dry_run=outcome == "preview")
    expected = outcome == "restart"
    assert bool(events) is expected
    assert (fx.data / "deploy/deploy.json").exists() is expected
    assert bool(fx.scheduled) is expected
    if expected:
        rec = load(fx.data / "deploy")
        assert rec["previous_ref"] == fx.previous and rec["target_ref"] == fx.target
        assert rec["state"] == "armed" and body["guard"]["armed"] is True
    if outcome == "failed-check":
        assert body["undo"]["ok"] and fx.git("rev-parse", "HEAD") == fx.previous


@pytest.mark.parametrize("state", ["armed-current", "confirmed", "armed-other", "off"])
async def test_pending_deploy_declines_before_fetch_or_preflight(deployment, monkeypatch, state):
    g = guard_module()
    fx = deployment
    fx.set_target(report=clean_report(fx.data))
    save(
        fx.data / "deploy",
        "deploy.json",
        record(
            fx.tree,
            target_ref=fx.target if state == "armed-other" else fx.previous,
            previous_ref=fx.previous,
            state="confirmed" if state == "confirmed" else "armed",
        ),
    )
    if state == "off":
        monkeypatch.setenv("PINKY_DEPLOY_GUARD", "0")
    fx.commands.clear()
    result = await fx.update(force=True)
    if state == "armed-current":
        assert result == {"error": "previous deploy is not confirmed yet"}
        assert not fx.scheduled and not fx.children
        assert not any(cmd[1] in {"fetch", "checkout"} for cmd in fx.commands)
    else:
        assert result["updated"] is True and fx.scheduled
        assert result["guard"]["armed"] is (state != "off")
    assert g is not None


@pytest.mark.parametrize("restore_fails", [False, True])
async def test_arm_write_failure_reuses_undo_without_restart(
    deployment, monkeypatch, restore_fails
):
    g = guard_module()
    fx = deployment
    fx.set_target(report=clean_report(fx.data))
    fx.fail_restore = restore_fails

    def unwritable(*args, **kwargs):
        raise PermissionError(TOKEN)

    monkeypatch.setattr(g, "arm_deploy", unwritable)
    result = await fx.update(force_deps=True)
    assert result["error"] and "guard" in result["error"]
    assert result["undo"]["ok"] is not restore_fails
    assert result["undo"]["deps_restored"] is not restore_fails
    assert fx.git("rev-parse", "HEAD") == fx.previous
    assert [ref for ref, _ in fx.installs] == [fx.target, fx.previous]
    assert not fx.scheduled and not (fx.data / "deploy/deploy.json").exists()
    assert TOKEN not in json.dumps(result)


async def test_equal_refs_with_rebuilt_dependencies_are_armed(deployment):
    guard_module()
    fx = deployment
    fx.set_target(report=clean_report(fx.data))
    fx.target = fx.previous
    result = await fx.update(force_deps=True)
    rec = load(fx.data / "deploy")
    assert result["restarting"] and rec["previous_ref"] == rec["target_ref"] == fx.previous
    assert rec["deps_rebuilt"] is True


def test_route_refresh_tracks_shared_token_and_destination_changes(deployment):
    g = guard_module()
    fx = deployment
    agents = fx.app.state.agents
    agents.register("sender", enabled=True)
    token = agents.create_bot_token("fixture-bot", "telegram", TOKEN)
    agents.set_token(
        "sender", "telegram", "", token_ref=token["id"], settings={"account_id": "fixture-account"}
    )
    client = TestClient(fx.app)
    destination = dict(
        platform="telegram", account_id="fixture-account", conversation_id="fixture-chat"
    )
    answer = client.put(
        "/system/owner-notification-destinations", json={"destinations": [destination]}
    )
    assert answer.status_code == 200
    state = fx.data / "deploy"
    assert load(state, "alert-route.json")["routes"][0]["token"] == TOKEN
    updated = "fixture-rotated-secret-marker"
    assert client.put(f"/bot-tokens/{token['id']}", json={"token": updated}).status_code == 200
    assert load(state, "alert-route.json")["routes"][0]["token"] == updated
    destination["conversation_id"] = "fixture-chat-2"
    assert (
        client.put(
            "/system/owner-notification-destinations", json={"destinations": [destination]}
        ).status_code
        == 200
    )
    assert load(state, "alert-route.json")["routes"][0]["conversation_id"] == "fixture-chat-2"
    status = client.get("/admin/update/status").json()
    assert TOKEN not in json.dumps(status) and updated not in json.dumps(status)
    assert set(status["guard"]["alert_route"]) == {"present", "platforms", "written_at"}
    assert client.delete(f"/bot-tokens/{token['id']}").status_code == 200
    assert not (state / "alert-route.json").exists()
    assert g is not None


async def test_arming_refreshes_route_and_status_discloses_frontend_limit(deployment):
    g = guard_module()
    fx = deployment
    agents = fx.app.state.agents
    agents.register("sender", enabled=True)
    agents.set_token("sender", "telegram", TOKEN, settings={"account_id": "fixture-account"})
    agents.set_owner_notification_destinations(
        [dict(platform="telegram", account_id="fixture-account", conversation_id="fixture-chat")]
    )
    fx.set_target(report=clean_report(fx.data))
    await fx.update()
    state = fx.data / "deploy"
    assert load(state, "alert-route.json")["routes"][0]["token"] == TOKEN
    rec = load(state)
    rec.update(state="rolled_back", rollback={"tries": 1, "deps_error": None})
    save(state, "deploy.json", rec)
    endpoint = next(r.endpoint for r in fx.app.routes if r.path == "/admin/update/status")
    status = await endpoint()
    assert "newer" in status["guard"]["frontend_warning"]
    assert TOKEN not in json.dumps(status)
    assert g is not None


@pytest.mark.parametrize("delivered", [True, False])
async def test_ready_recovery_uses_normal_owner_and_internal_then_fallback(harness, delivered):
    h = harness(
        rec={
            "state": "rolled_back",
            "requested_by": "requester",
            "rollback": {"tries": 1, "deps_error": None},
        }
    )
    h.runner.head = "a" * 40
    h.ctx.activate()
    owner, internal = AsyncMock(return_value=delivered), AsyncMock(return_value=True)
    await h.ctx.ready(owner_notify=owner, internal_notify=internal)
    assert load(h.state)["state"] == "reported"
    owner.assert_awaited_once()
    internal.assert_awaited_once_with("requester", owner.call_args.args[0])
    assert len(h.alerts) == int(not delivered)
    if not delivered:
        assert h.alerts == [owner.call_args.args[0]]


async def test_ready_confirmation_and_hourly_route_refresh_work_while_off(harness, monkeypatch):
    h = harness(rec={})
    (h.state / "guard.off").touch()
    h.ctx.activate()
    calls = []

    def routes():
        calls.append(1)
        return [
            dict(
                platform="telegram",
                account_id="fixture-account",
                conversation_id="fixture-chat",
                token=TOKEN,
            )
        ]

    monkeypatch.setattr(h.g, "ROUTE_REFRESH_SECONDS", 0.005)
    await h.ctx.ready(route_provider=routes)
    assert load(h.state)["state"] == "confirmed"
    assert (h.state / "alert-route.json").stat().st_mode & 0o777 == 0o600
    await asyncio.sleep(0.02)
    assert len(calls) >= 2
    assert not h.alerts


def test_ready_hook_composes_with_existing_replay():
    from pinky_daemon import api

    tree = ast.parse(Path(api.__file__).read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "after_ready"
    ]
    assert len(calls) == 1
    callback_name = calls[0].args[0].id
    callback = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == callback_name
    )
    names = {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(callback)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    }
    assert {"mark_ready", "_finish_startup_replay"} <= names


def test_fixture_path_guard_rejects_symlinked_live_roots(tmp_path):
    from tests import conftest

    helper = getattr(conftest, "_assert_private_fixture_paths", None)
    assert helper is not None, "fixture roots must be checked before use"
    live = tmp_path / "configured-live"
    live.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(live, target_is_directory=True)
    with pytest.raises(RuntimeError, match="live data root"):
        helper([alias / "fixture"], [live])
    helper([tmp_path / "private"], [live])
