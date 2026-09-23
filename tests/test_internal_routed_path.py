"""Verify internal request signatures over the routed path."""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote, unquote

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

import pinky_daemon.api as api_module
from pinky_daemon.auth import (
    INTERNAL_SIGNATURE_HEADER,
    INTERNAL_TIMESTAMP_HEADER,
    build_internal_auth_headers,
    verify_internal_request,
)

pytestmark = pytest.mark.real_auth

_SECRET = "routed-path-test-secret"
_PREFIX = "/agents/sample/signed-path"


def _headers(path):
    return build_internal_auth_headers(
        _SECRET, agent_name="sample", method="GET", path=path,
    )


@pytest.fixture
def routed_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PINKY_SESSION_SECRET", _SECRET)
    monkeypatch.setenv("PINKY_AUTH_DENY_DEFAULT", "enforce")
    monkeypatch.delenv("PINKY_UI_PASSWORD", raising=False)
    app = api_module.create_api(
        max_sessions=1, default_working_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
    )
    app.state.agents.register("sample", working_dir=str(tmp_path / "sample"))
    seen = []
    verify = api_module.verify_internal_request

    def record_path(*args, **kwargs):
        seen.append(kwargs["path"])
        return verify(*args, **kwargs)

    monkeypatch.setattr(api_module, "verify_internal_request", record_path)

    @app.get(_PREFIX + "/{segment}/{item}")
    async def signed_path(segment: str, item: str, request: Request):
        return {"segment": segment, "item": item, "gate": request.state.auth_gate}

    return app, seen


@pytest.mark.parametrize("suffix", ["D%3Fone/a", "D%3Ftwo/b"])
def test_internal_routed_path_question_mark_rejected(routed_app, suffix):
    app, seen = routed_app
    path = f"{_PREFIX}/{suffix}"
    headers = _headers(path)
    response = TestClient(app).get(path, headers=headers)
    assert response.status_code == 401
    assert seen == [unquote(path)]


def test_internal_routed_path_hash_rejected(routed_app):
    app, seen = routed_app
    path = f"{_PREFIX}/D%23one/a"
    response = TestClient(app).get(path, headers=_headers(path))
    assert response.status_code == 401
    assert seen == [unquote(path)]


@pytest.mark.parametrize(
    ("suffix", "segment"),
    [
        ("plain/a", "plain"),
        ("plain/a?limit=2&cursor=next", "plain"),
        ("two%20words/a", "two words"),
        ("one%3Atwo/a", "one:two"),
        ("%E9%9B%AA/a", "雪"),
    ],
)
def test_internal_routed_path_normal_request(routed_app, suffix, segment):
    app, seen = routed_app
    path = f"{_PREFIX}/{suffix}"
    response = TestClient(app).get(path, headers=_headers(path))
    assert response.status_code == 200
    assert response.json() == {"segment": segment, "item": "a", "gate": "internal_hmac"}
    assert seen == [unquote(path.split("?", 1)[0])]


def test_internal_routed_path_encoded_slash_keeps_route_shape(routed_app):
    app, seen = routed_app
    path = f"{_PREFIX}/one%2Ftwo/a"
    response = TestClient(app).get(path, headers=_headers(path))
    assert response.status_code == 404
    assert seen == [unquote(path)]


@pytest.mark.parametrize("mode", ["mount", "root_path"])
@pytest.mark.parametrize("prefix_count", [1, 0, 2], ids=["full", "unprefixed", "doubled"])
def test_internal_routed_path_root_path(routed_app, record_property, mode, prefix_count):
    app, seen = routed_app
    path = f"{_PREFIX}/plain/a"
    root_path = "/mounted"
    external_path = root_path + path
    scopes = []

    async def capture_scope(scope, receive, send):
        scopes.append({
            "root_path": scope["root_path"], "path": scope["path"],
            "raw_path": scope["raw_path"].decode(),
            "query_string": scope["query_string"].decode(),
        })
        await app(scope, receive, send)

    if mode == "mount":
        client = TestClient(Starlette(routes=[Mount(root_path, app=capture_scope)]))
    else:
        client = TestClient(capture_scope, root_path=root_path)
    response = client.get(
        external_path + "?limit=2", headers=_headers(root_path * prefix_count + path),
    )
    record_property("asgi_scope", json.dumps(scopes))
    assert scopes == [{
        "root_path": root_path, "path": external_path,
        "raw_path": external_path, "query_string": "limit=2",
    }]
    assert seen == [external_path]
    assert response.status_code == (200 if prefix_count == 1 else 401)
    if prefix_count == 1:
        assert response.json() == {"segment": "plain", "item": "a", "gate": "internal_hmac"}
    else:
        assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize("delimiter", ["?", "#"])
def test_internal_routed_path_verifier_rejects_delimiters(delimiter):
    path = f"{_PREFIX}/D{delimiter}one/a"
    headers = _headers(quote(path, safe="/"))
    assert not verify_internal_request(
        _SECRET, agent_name="sample", method="GET", path=path,
        timestamp=headers[INTERNAL_TIMESTAMP_HEADER],
        signature=headers[INTERNAL_SIGNATURE_HEADER],
    )


@pytest.mark.parametrize("delimiter", ["?", "#"])
def test_internal_routed_path_verifier_rejects_valid_full_path_mac(delimiter):
    path = f"{_PREFIX}/D{delimiter}one/a"
    timestamp = str(int(time.time()))
    payload = f"sample\nGET\n{path}\n{timestamp}".encode("utf-8")
    signature = base64.urlsafe_b64encode(
        hmac.new(_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")

    assert not verify_internal_request(
        _SECRET, agent_name="sample", method="GET", path=path,
        timestamp=timestamp, signature=signature,
    )


@pytest.mark.parametrize(
    ("path", "expected_signature"),
    [
        ("/agents/sample/signed-path/plain/a", "l1Gz4yDdA7yOAZxIHD7Iw6Ol7D-KAONqcPkORUYrMU0"),
        ("/research/42/export?format=pdf", "Pl4-F7aFVrpvs_QO0nujM6AThO4FYUYZciNG9jf6j2I"),
    ],
)
def test_plain_internal_path_signature_is_byte_identical_to_legacy(path, expected_signature):
    headers = build_internal_auth_headers(
        _SECRET, agent_name="sample", method="GET", path=path, timestamp=123,
    )
    assert headers[INTERNAL_SIGNATURE_HEADER] == expected_signature
