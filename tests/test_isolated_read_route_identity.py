"""Read ownership follows the resolved parameterized route."""

import pytest

from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_read_enforce import _read

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("name", ["retired", "presence"])
def test_literal_caller_name_cannot_authorize_a_static_collection(daemon, monkeypatch, name):
    instance = daemon()
    work = instance.root / name
    work.mkdir()
    instance.agents.register(name, isolated=True, working_dir=str(work))
    instance.agents.update("peer", soul="private-retired-fixture")
    if name == "retired":
        assert instance.agents.retire("peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(instance, "/agents/" + name, name=name)
    assert response.status_code == 403


@pytest.fixture
def identity_logs(monkeypatch):
    from pinky_daemon import api

    lines = []
    monkeypatch.setattr(api, "_log", lambda text, **kwargs: lines.append(str(text)))
    return lines


def _named_caller(daemon, name, isolated=True):
    instance = daemon()
    work = instance.root / name
    work.mkdir()
    instance.agents.register(name, isolated=isolated, working_dir=str(work))
    return instance


@pytest.mark.parametrize("name", ["retired", "presence"])
@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_static_collections_follow_the_shadow_read_policy(
    daemon, monkeypatch, identity_logs, name, mode,
):
    instance = _named_caller(daemon, name)
    if name == "retired":
        assert instance.agents.retire("peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    identity_logs.clear()
    response = _read(instance, "/agents/" + name, name=name)
    assert response.status_code == 200
    assert [line for line in identity_logs if " READ " in line] == ([
        f"isolation: WOULD DENY READ GET /agents/{name} for {name} reason=unlisted repeats=0"
    ] if mode == "shadow" else [])


@pytest.mark.parametrize("name", ["retired", "presence"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_static_collections_remain_available_to_normal_callers(
    daemon, monkeypatch, identity_logs, name, mode,
):
    instance = _named_caller(daemon, name, isolated=False)
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    identity_logs.clear()
    assert _read(instance, "/agents/" + name, name=name).status_code == 200
    assert not [line for line in identity_logs if " READ " in line]


@pytest.mark.parametrize("name", ["retired", "presence"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_static_word_caller_keeps_its_own_parameterized_context(
    daemon, monkeypatch, identity_logs, name, mode,
):
    instance = _named_caller(daemon, name)
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    identity_logs.clear()
    assert _read(instance, f"/agents/{name}/context", name=name).status_code == 200
    assert not [line for line in identity_logs if " READ " in line]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", [
    "/agents/retired", "/agents/retired/", "/agents//retired", "/agents/%72etired",
])
def test_static_collection_spellings_never_grant_self_access(
    daemon, monkeypatch, identity_logs, method, path,
):
    instance = _named_caller(daemon, "retired")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    identity_logs.clear()
    assert _read(instance, path, method=method, name="retired").status_code == 403
    assert [line for line in identity_logs if " READ " in line] == [
        f"isolation: DENY READ {method} /agents/retired for retired reason=unlisted repeats=0"
    ]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path,get_status,head_status", [
    ("/agents/retired/context", 200, 405),
    ("/agents/retired/context/", 307, 307),
    ("/agents//retired/context", 404, 404),
    ("/agents/%72etired/context", 200, 405),
])
def test_parameterized_self_spellings_keep_the_existing_dispatch(
    daemon, monkeypatch, identity_logs, mode, method, path, get_status, head_status,
):
    instance = _named_caller(daemon, "retired")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    identity_logs.clear()
    response = _read(instance, path, method=method, name="retired")
    assert response.status_code == (get_status if method == "GET" else head_status)
    assert not [line for line in identity_logs if " READ " in line]


@pytest.mark.parametrize("root", ["/proxy", "/agents/retired/proxy"])
@pytest.mark.parametrize("path,status", [
    ("/agents/retired", 403), ("/agents/retired/context", 200),
])
def test_mounted_prefix_does_not_change_the_resolved_owner(
    daemon, monkeypatch, root, path, status,
):
    instance = _named_caller(daemon, "retired")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert _read(instance, path, name="retired", root_path=root).status_code == status


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_future_static_route_is_not_a_self_route(daemon, monkeypatch, method):
    from fastapi.routing import APIRoute

    instance = _named_caller(daemon, "snapshot")
    hits = []

    async def snapshot():
        hits.append(True)
        return {"value": "private-snapshot-fixture"}

    instance.app.router.routes.insert(0, APIRoute("/agents/snapshot", snapshot, methods=["GET", "HEAD"]))
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(instance, "/agents/snapshot", name="snapshot", method=method)
    assert response.status_code == 403
    assert hits == []


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_self_route_uses_the_actual_parameter_name(daemon, monkeypatch, identity_logs, mode, method):
    instance = daemon()

    async def owned(target: str):
        return {"agent": target}

    instance.app.add_api_route("/agents/{target:str}/fixture-owned", owned, methods=["GET", "HEAD"])
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    identity_logs.clear()
    response = _read(instance, "/agents/tenant/fixture-owned", method=method)
    assert response.status_code == 200
    assert not [line for line in identity_logs if " READ " in line]


@pytest.mark.parametrize("mode,status", [("off", 404), ("shadow", 404), ("enforce", 403)])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_unresolved_agent_path_is_not_a_self_route(
    daemon, monkeypatch, identity_logs, mode, status, method,
):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    identity_logs.clear()
    assert _read(instance, "/agents/tenant/missing-fixture", method=method).status_code == status
    assert [line for line in identity_logs if " READ " in line] == ([] if mode == "off" else [
        f"isolation: {'DENY' if mode == 'enforce' else 'WOULD DENY'} READ {method} "
        "<unknown> for tenant reason=unlisted repeats=0"
    ])
