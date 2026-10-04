"""Real middleware and roster routes over synthetic per-app registries."""

from __future__ import annotations

import asyncio
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api, claude_runner, runtime_model_catalog
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from pinky_daemon.model_roster import load_bundled
from pinky_daemon.pricing import lookup_rate
from tests._model_roster_local import (
    DOCUMENT_KEY,
    SONNET,
    add,
    document,
    encode,
    last_good,
    model_row,
    new_model,
    owned,
    snapshot,
    status,
)
from tests.conftest import TEST_SESSION_SECRET
from tests.test_model_roster_fetch import FINAL, URL, required, result
from tests.test_model_roster_sync import until

pytestmark = pytest.mark.real_auth


@pytest.fixture
def apps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    monkeypatch.setattr(claude_runner, "_find_claude_binary", lambda: "/offline/claude")
    built = []

    def no_network(*args, **kwargs):
        raise AssertionError("Route tests must not contact a roster host")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    def build(*, mode="shadow", enabled=False, getter=None, url=URL):
        monkeypatch.setenv("PINKY_ISOLATED_POLICY_MODE", mode)
        monkeypatch.setenv("PINKY_MODEL_ROSTER_SYNC", "on" if enabled else "off")
        monkeypatch.setenv("PINKY_MODEL_ROSTER_URL", url)
        root = tmp_path / f"app-{len(built)}"
        root.mkdir()
        app = api.create_api(db_path=str(root / "agents.db"), default_working_dir=str(root))
        built.append(app)
        registry = app.state.agents
        for name, isolated in (("tenant", True), ("normal", False)):
            work = root / name
            work.mkdir()
            registry.register(name, isolated=isolated, working_dir=str(work))
        if getter is not None:
            service = getattr(app.state, "model_roster_sync", None)
            assert service is not None, "API must own a per-app roster sync service"
            service.getter = getter
        return SimpleNamespace(app=app, registry=registry)

    yield build
    for app in reversed(built):
        app.state.store_catalog.close()


@contextmanager
def client_for(app, **kwargs):
    # Exercise ASGI middleware without starting unrelated lifespan services.
    client = TestClient(app, **kwargs)
    try:
        yield client
    finally:
        client.close()


@contextmanager
def browser(app):
    with client_for(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
        yield client


def signed(d, method, path, name="tenant"):
    return build_internal_auth_headers(
        d.registry.get_signing_key(name), agent_name=name, method=method, path=path
    )


def fixed_error(response, expected, *secrets):
    assert response.status_code == expected, response.text
    body = response.json()
    body = body.get("detail", body)
    assert isinstance(body, dict) and set(body) == {"code", "message"}
    assert isinstance(body["code"], str) and body["code"]
    assert isinstance(body["message"], str) and body["message"]
    for secret in secrets:
        assert secret not in response.text
    return body


def test_status_literal_precedes_both_catchalls_and_is_read_only(apps, monkeypatch):
    d = apps()
    before = snapshot(d.registry)
    invalidate = Mock()
    monkeypatch.setattr(runtime_model_catalog, "invalidate", invalidate)
    with browser(d.app) as client:
        response = client.get("/models/roster")
    assert response.status_code == 200, "Status must not dispatch as bare model id 'roster'"
    value = response.json()
    assert {key: value[key] for key in status(d.registry)} == status(d.registry)
    assert value["url"] == URL and value["enabled"] is False
    assert value["bundled_revision"] == load_bundled().revision
    assert DOCUMENT_KEY not in response.text and "last_applied_document" not in response.text
    positions = {
        route.path: index
        for index, route in enumerate(d.app.routes)
        if hasattr(route, "path") and route.path.startswith("/models/")
    }
    catchalls = [
        index
        for index, route in enumerate(d.app.routes)
        if getattr(route, "path", "") == "/models/{model_id:path}"
    ]
    for path in ("/models/roster", "/models/roster/sync", "/models/roster/release"):
        assert path in positions and positions[path] < min(catchalls)
    assert snapshot(d.registry) == before
    invalidate.assert_not_called()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/models/roster", None),
        ("/models/roster/sync", {"dry_run": "wrong"}),
        ("/models/roster/release", {"id": 7}),
    ],
)
def test_unauthenticated_request_is_401_before_body_validation(apps, path, body):
    d = apps()
    before = snapshot(d.registry)
    with client_for(d.app) as client:
        response = client.request("GET" if body is None else "POST", path, json=body)
    assert response.status_code == 401
    assert snapshot(d.registry) == before


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/models/roster"),
        ("POST", "/models/roster/sync"),
        ("POST", "/models/roster/release"),
        ("DELETE", "/models/roster/unused"),
        ("PUT", "/models/roster"),
        ("PATCH", "/models/roster/nested/unused"),
        ("OPTIONS", "/models/roster"),
        ("HEAD", "/models/roster"),
    ],
)
def test_isolated_roster_subtree_is_always_denied(apps, monkeypatch, mode, method, path):
    d = apps(mode=mode)
    before = snapshot(d.registry)
    apply_spy = Mock(wraps=d.registry.apply_model_roster)
    release_spy = Mock(wraps=d.registry.release_model_roster_fields)
    monkeypatch.setattr(d.registry, "apply_model_roster", apply_spy)
    monkeypatch.setattr(d.registry, "release_model_roster_fields", release_spy)
    with client_for(d.app) as client:
        response = client.request(
            method,
            path,
            headers=signed(d, method, path),
            json={"dry_run": False, "id": SONNET, "fields": "all"},
        )
    assert response.status_code == 403
    assert snapshot(d.registry) == before
    apply_spy.assert_not_called()
    release_spy.assert_not_called()


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize(
    "raw_path,scope_path,root_path",
    [
        ("/models//roster/sync", "/models//roster/sync", ""),
        ("/mounted/models/roster", "/mounted/models/roster", "/mounted"),
        ("/models/%72oster", "/models/roster", ""),
    ],
)
def test_canonical_dispatch_boundary_denies_without_path_rewrite(
    apps,
    mode,
    raw_path,
    scope_path,
    root_path,
):
    d = apps(mode=mode)
    with client_for(d.app, root_path=root_path) as client:
        response = client.get(raw_path, headers=signed(d, "GET", scope_path))
    assert response.status_code == 403


def test_isolated_signature_is_denied_even_with_valid_browser_cookie(apps):
    d = apps(mode="off")
    with browser(d.app) as client:
        response = client.get("/models/roster", headers=signed(d, "GET", "/models/roster"))
    assert response.status_code == 403


@pytest.mark.parametrize("lookup", ["missing", "error"])
def test_verified_key_with_unavailable_caller_fails_closed(apps, monkeypatch, lookup):
    d = apps(mode="off")
    headers = signed(d, "GET", "/models/roster")
    original = d.registry.get

    def get(name):
        if name == "tenant":
            if lookup == "error":
                raise sqlite3.OperationalError("caller-private-marker")
            return None
        return original(name)

    monkeypatch.setattr(d.registry, "get", get)
    with client_for(d.app) as client:
        response = client.get("/models/roster", headers=headers)
    assert response.status_code == 403 and "caller-private-marker" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/models/rosterish", "/models/%2572oster", "/models"])
async def test_sibling_and_double_encoded_paths_keep_baseline_access(apps, path):
    d = apps(mode="off")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=d.app), base_url="http://testserver",
    ) as client:
        response = await client.get(path, headers=signed(d, "GET", path))
    assert response.status_code == (200 if path == "/models" else 404)


def test_nonisolated_signed_status_and_full_id_lookup_remain_available(apps):
    d = apps(mode="enforce")
    with client_for(d.app) as client:
        status_response = client.get(
            "/models/roster", headers=signed(d, "GET", "/models/roster", "normal")
        )
        model = client.get(
            "/models/" + SONNET, headers=signed(d, "GET", "/models/" + SONNET, "normal")
        )
    assert status_response.status_code == 200 and model.status_code == 200
    assert model.json()["id"] == SONNET


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"dry_run": "false"},
        {"dry_run": 0},
        {"dry_run": None},
        {"dry_run": []},
        {"dry_run": False, "extra": "request-private-marker"},
    ],
)
def test_sync_requires_explicit_strict_bool_before_work(apps, body):
    d = apps()
    before = snapshot(d.registry)
    with browser(d.app) as client:
        fixed_error(client.post("/models/roster/sync", json=body), 422, "request-private-marker")
    assert snapshot(d.registry) == before


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"id": SONNET},
        {"fields": "all"},
        {"id": 7, "fields": []},
        {"id": SONNET, "fields": "input_price"},
        {"id": SONNET, "fields": [7]},
        {"id": SONNET, "fields": ["unknown-private-marker"]},
        {"id": SONNET, "fields": [], "extra": "request-private-marker"},
        {"id": "bare-model", "fields": []},
        {"id": "a/b/c", "fields": []},
        {"id": "/missing-provider", "fields": []},
        {"id": "custom/", "fields": []},
        {"id": " custom/model", "fields": []},
        {"id": "custom/model name", "fields": []},
    ],
)
def test_release_body_is_strict_without_alias_or_coercion(apps, body):
    d = apps()
    before = snapshot(d.registry)
    with browser(d.app) as client:
        fixed_error(
            client.post("/models/roster/release", json=body),
            422,
            "unknown-private-marker",
            "request-private-marker",
        )
    assert snapshot(d.registry) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_off_manual_sync_is_conflict_without_attempt(apps, dry_run):
    d = apps()
    before = snapshot(d.registry)
    with browser(d.app) as client:
        fixed_error(client.post("/models/roster/sync", json={"dry_run": dry_run}), 409)
    assert snapshot(d.registry) == before


def test_legacy_discovery_and_full_id_lookup_keep_their_existing_routes(apps):
    d = apps()
    with browser(d.app) as client:
        model = client.get("/models/" + SONNET)
        discovery = client.post("/models/sync", json={})
    assert model.status_code == 200 and model.json()["id"] == SONNET
    assert discovery.status_code == 400


@pytest.mark.parametrize("dry_run", [False, True])
def test_sync_returns_real_apply_or_preview_report(apps, dry_run):
    value = document()
    model_row(value)["pricing"]["input"] = 7.0
    getter = Mock(return_value=result(encode(value) + b"\n", FINAL))
    d = apps(enabled=True, getter=getter)
    runtime_model_catalog.bind_registry(d.registry)
    before = snapshot(d.registry)
    with browser(d.app) as client:
        response = client.post("/models/roster/sync", json={"dry_run": dry_run})
    assert response.status_code == 200
    report = response.json()
    assert report["revision"] == 2 and report["dry_run"] is dry_run
    assert report["revision_gate"] == "accepted" and "rows" in report
    getter.assert_called_once()
    if dry_run:
        assert snapshot(d.registry) == before
    else:
        assert d.registry.get_setting(DOCUMENT_KEY).encode() == encode(value) + b"\n"
        assert lookup_rate(SONNET.split("/", 1)[1])["input"] == 7.0


@pytest.mark.parametrize("operation", ["update", "insert"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unstorable_revision_route_has_fixed_parse_refused_502(
    apps, monkeypatch, capsys, operation, dry_run
):
    value = document(2**63)
    model_row(value)["pricing"]["input"] = 7.0
    if operation == "insert":
        value["models"].append(new_model("signed-limit-insert"))
    d = apps(enabled=True, getter=lambda url, **kw: result(encode(value)))
    before, good = snapshot(d.registry), last_good(d.registry)
    errors = d.registry._model_roster_errors
    recorder = Mock(wraps=d.registry.record_model_roster_sync_error)
    applier = Mock(wraps=d.registry.apply_model_roster)
    monkeypatch.setattr(d.registry, "record_model_roster_sync_error", recorder)
    monkeypatch.setattr(d.registry, "apply_model_roster", applier)
    with client_for(d.app, raise_server_exceptions=False) as client:
        client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
        response = client.post("/models/roster/sync", json={"dry_run": dry_run})
    assert fixed_error(response, 502) == {
        "code": "parse_refused", "message": "The roster document was refused."
    }
    applier.assert_not_called()
    assert snapshot(d.registry)["models"] == before["models"]
    assert last_good(d.registry) == good
    assert d.registry._model_roster_errors == errors + (not dry_run)
    if dry_run:
        recorder.assert_not_called()
        assert snapshot(d.registry) == before
    else:
        recorder.assert_called_once()
        assert recorder.call_args.args[0].code == "parse_refused"
        assert status(d.registry)["last_error"] == "parse_refused"
        assert capsys.readouterr().err == "ERROR model roster: parse_refused\n"


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("url", 400),
        ("network", 502),
        ("parser", 502),
        ("apply", 502),
        ("storage", 503),
        ("closing", 503),
    ],
)
def test_http_failures_have_fixed_bodies_and_apply_is_recorded_once(
    apps, monkeypatch, capsys, fault, expected
):
    value = document()

    def getter(url, **kwargs):
        if fault == "network":
            raise urllib.error.URLError("transport-private-marker")
        return result(b"response-private-marker" if fault == "parser" else encode(value))

    d = apps(
        enabled=True,
        getter=getter,
        url="https://example.invalid/a?query-private-marker" if fault == "url" else URL,
    )
    if fault == "apply":
        custom = new_model("collision-private-id")
        custom["provider"] = "custom"
        add(d.registry, custom)
        value["models"].append(new_model("collision-private-id"))
    if fault == "storage":
        monkeypatch.setattr(
            d.registry,
            "apply_model_roster",
            Mock(side_effect=sqlite3.OperationalError("storage-private-marker")),
        )
    if fault == "closing":
        required(d.app.state.model_roster_sync, "mark_closing")()
    good = last_good(d.registry)
    errors = d.registry._model_roster_errors
    with browser(d.app) as client:
        fixed_error(
            client.post("/models/roster/sync", json={"dry_run": False}),
            expected,
            "query-private-marker",
            "transport-private-marker",
            "response-private-marker",
            "storage-private-marker",
            "collision-private-id",
        )
    assert last_good(d.registry) == good
    stderr = capsys.readouterr().err
    for marker in (
        "query-private-marker",
        "transport-private-marker",
        "response-private-marker",
        "storage-private-marker",
    ):
        assert marker not in stderr
    if fault == "apply":
        assert d.registry._model_roster_errors == errors + 1
        assert "collision-private-id" in status(d.registry)["last_error"]


def test_release_storage_failure_is_fixed_503_without_log_or_write(apps, monkeypatch, capsys):
    d = apps()
    before = snapshot(d.registry)
    monkeypatch.setattr(
        d.registry,
        "release_model_roster_fields",
        Mock(side_effect=sqlite3.OperationalError("storage-private-marker")),
    )
    with browser(d.app) as client:
        fixed_error(
            client.post("/models/roster/release", json={"id": SONNET, "fields": "all"}),
            503,
            "storage-private-marker",
        )
    assert snapshot(d.registry) == before
    assert "storage-private-marker" not in capsys.readouterr().err


def test_unexpected_worker_route_has_fixed_502_and_can_sync_again(apps, capsys, caplog):
    getter = Mock(side_effect=[RuntimeError("private-marker"), result(encode(document()))])
    d = apps(enabled=True, getter=getter)
    good, errors = last_good(d.registry), d.registry._model_roster_errors
    with browser(d.app) as client:
        response = None
        try:
            response = client.post("/models/roster/sync", json={"dry_run": False})
        except Exception:
            pass
        assert response is not None, "An unexpected worker must return a fixed HTTP error"
        body = fixed_error(response, 502, "private-marker")
        assert body["code"] == "fetch_failed"
        assert d.registry._model_roster_errors == errors + 1 and last_good(d.registry) == good
        rendered = response.text + status(d.registry)["last_error"] + capsys.readouterr().err
        rendered += "".join(r.getMessage() for r in caplog.records)
        assert "private-marker" not in rendered
        after = snapshot(d.registry)
        retry = client.post("/models/roster/sync", json={"dry_run": True})
        assert retry.status_code == 200 and getter.call_count == 2
        assert snapshot(d.registry) == after and d.registry._model_roster_errors == errors + 1


@pytest.mark.parametrize("saved", [None, "", "not-json", " \n\t", "{}"])
def test_release_refuses_bad_saved_provenance_without_any_write(apps, monkeypatch, saved):
    d = apps()
    row = model_row(document())
    row["pricing"]["input"] = 7.0
    add(d.registry, row)
    if saved is None:
        d.registry.delete_setting(DOCUMENT_KEY)
    else:
        d.registry.set_setting(DOCUMENT_KEY, saved)
    before = snapshot(d.registry)
    invalidate = Mock()
    monkeypatch.setattr(runtime_model_catalog, "invalidate", invalidate)
    with browser(d.app) as client:
        fixed_error(
            client.post("/models/roster/release", json={"id": SONNET, "fields": "all"}), 409
        )
    assert snapshot(d.registry) == before and owned(d.registry) == {"input_price"}
    invalidate.assert_not_called()


@pytest.mark.parametrize("ownership", ["not-json", "{}", '"input_price"', "[1]", '["unknown"]'])
def test_release_refuses_malformed_ownership_without_any_write(apps, ownership):
    d = apps()
    d.registry._db.execute("UPDATE models SET operator_fields=? WHERE id=?", (ownership, SONNET))
    d.registry._db.commit()
    before = snapshot(d.registry)
    with browser(d.app) as client:
        fixed_error(
            client.post("/models/roster/release", json={"id": SONNET, "fields": ["input_price"]}),
            409,
        )
    assert snapshot(d.registry) == before


@pytest.mark.parametrize("fields", [[], ["context_window", "context_window"], "all"])
def test_release_is_local_off_and_expands_context_ownership_once(apps, fields):
    d = apps()
    row = model_row(document())
    row.update(context_window=800_000, is_1m=False)
    add(d.registry, row)
    before = snapshot(d.registry)
    with browser(d.app) as client:
        response = client.post("/models/roster/release", json={"id": SONNET, "fields": fields})
    assert response.status_code == 200
    if fields == []:
        assert snapshot(d.registry) == before
    else:
        assert owned(d.registry) == set()
        assert d.registry.get_model(SONNET)["is_1m"] == 1
        assert set(response.json()["rows"][0]["fields_released"]) == {"context_window", "is_1m"}


def test_release_accepts_valid_custom_provider_id(apps):
    d = apps()
    custom = new_model("custom-model")
    custom["provider"] = "custom"
    add(d.registry, custom)
    with browser(d.app) as client:
        response = client.post(
            "/models/roster/release", json={"id": "custom/custom-model", "fields": "all"}
        )
    assert response.status_code == 200 and owned(d.registry, "custom/custom-model") == set()


@pytest.mark.parametrize("target_exists", [False, True])
def test_release_uses_exact_target_despite_lower_rowid_alias(apps, target_exists):
    d = apps()
    custom = new_model(SONNET)
    add(d.registry, custom)
    custom_id = "openai/" + SONNET
    cursor = d.registry._db.execute("SELECT * FROM models WHERE id=?", (SONNET,))
    columns = [item[0] for item in cursor.description]
    target = cursor.fetchone()
    d.registry._db.execute("DELETE FROM models WHERE id=?", (SONNET,))
    if target_exists:
        d.registry._db.execute(
            f"INSERT INTO models ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            target,
        )
    d.registry._db.commit()
    alias = d.registry._db.execute("SELECT * FROM models WHERE id=?", (custom_id,)).fetchone()
    if target_exists:
        rowids = dict(d.registry._db.execute("SELECT id,rowid FROM models"))
        assert rowids[custom_id] < rowids[SONNET]
    with browser(d.app) as client:
        response = client.post("/models/roster/release", json={"id": SONNET, "fields": "all"})
    assert response.status_code == (200 if target_exists else 404)
    assert (
        d.registry._db.execute("SELECT * FROM models WHERE id=?", (custom_id,)).fetchone() == alias
    )


def test_two_apps_keep_configuration_and_apply_ownership_separate(apps):
    first = apps(enabled=True, getter=lambda url, **kw: result(encode(document(2))), url=URL)
    second = apps(
        enabled=True, getter=lambda url, **kw: result(encode(document(3)), FINAL), url=FINAL
    )
    with browser(first.app) as one, browser(second.app) as two:
        assert one.get("/models/roster").json()["url"] == URL
        assert two.get("/models/roster").json()["url"] == FINAL
        assert one.post("/models/roster/sync", json={"dry_run": False}).status_code == 200
        assert status(first.registry)["last_applied_revision"] == 2
        assert status(second.registry)["last_applied_revision"] == 1
        assert two.post("/models/roster/sync", json={"dry_run": False}).status_code == 200
        assert status(first.registry)["last_applied_revision"] == 2
        assert status(second.registry)["last_applied_revision"] == 3


@pytest.mark.asyncio
async def test_busy_route_refuses_worker_but_local_release_still_works(apps):
    entered, release = threading.Event(), threading.Event()

    def getter(url, **kwargs):
        entered.set()
        assert release.wait(5)
        return result(encode(document()))

    d = apps(enabled=True, getter=getter)
    service = d.app.state.model_roster_sync
    operation = asyncio.create_task(required(service, "sync")(dry_run=False))
    try:
        await until(entered.is_set)
        before = snapshot(d.registry)
        transport = httpx.ASGITransport(app=d.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={SESSION_COOKIE_NAME: create_session_cookie(TEST_SESSION_SECRET)},
        ) as client:
            fixed_error(await client.post("/models/roster/sync", json={"dry_run": True}), 409)
            assert snapshot(d.registry) == before
            response = await client.post(
                "/models/roster/release", json={"id": SONNET, "fields": []}
            )
            assert response.status_code == 200 and snapshot(d.registry) == before
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        await required(service, "close")()


@pytest.mark.asyncio
async def test_outer_timeout_route_returns_fixed_504_and_keeps_late_bytes_out(apps):
    entered, release = threading.Event(), threading.Event()

    def getter(url, **kwargs):
        entered.set()
        assert release.wait(5)
        return result(encode(document()))

    d = apps(enabled=True, getter=getter)
    service = d.app.state.model_roster_sync
    service.timeout = 0.03
    good = last_good(d.registry)
    try:
        transport = httpx.ASGITransport(app=d.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={SESSION_COOKIE_NAME: create_session_cookie(TEST_SESSION_SECRET)},
        ) as client:
            fixed_error(await client.post("/models/roster/sync", json={"dry_run": False}), 504)
        assert entered.is_set() and last_good(d.registry) == good
        before = snapshot(d.registry)
        worker = getattr(service, "worker_task", None)
        assert isinstance(worker, asyncio.Task) and not worker.done()
        release.set()
        await asyncio.gather(worker, return_exceptions=True)
        assert snapshot(d.registry) == before
    finally:
        release.set()
        await required(service, "close")()
