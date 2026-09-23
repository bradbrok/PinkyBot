"""Verify internal request signatures over the routed path."""

from urllib.parse import unquote

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

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
    headers = _headers(f"{_PREFIX}/D?one/a")
    response = TestClient(app).get(path, headers=headers)
    assert response.status_code == 401
    assert seen == [unquote(path)]


@pytest.mark.parametrize("signed_suffix", ["D", "D#one/a"])
def test_internal_routed_path_hash_rejected(routed_app, signed_suffix):
    app, seen = routed_app
    path = f"{_PREFIX}/D%23one/a"
    response = TestClient(app).get(path, headers=_headers(f"{_PREFIX}/{signed_suffix}"))
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
    response = TestClient(app).get(path, headers=_headers(unquote(path)))
    assert response.status_code == 200
    assert response.json() == {"segment": segment, "item": "a", "gate": "internal_hmac"}
    assert seen == [unquote(path.split("?", 1)[0])]


def test_internal_routed_path_encoded_slash_keeps_route_shape(routed_app):
    app, seen = routed_app
    path = f"{_PREFIX}/one%2Ftwo/a"
    response = TestClient(app).get(path, headers=_headers(unquote(path)))
    assert response.status_code == 404
    assert seen == [unquote(path)]


@pytest.mark.parametrize("include_prefix", [True, False])
def test_internal_routed_path_root_path(routed_app, include_prefix):
    app, seen = routed_app
    path = f"{_PREFIX}/plain/a"
    root_path = "/mounted"
    signed_path = root_path + path if include_prefix else path
    response = TestClient(app, root_path=root_path).get(path, headers=_headers(signed_path))
    assert response.status_code == (200 if include_prefix else 401)
    assert seen == [root_path + path]
    if include_prefix:
        assert response.json()["gate"] == "internal_hmac"


@pytest.mark.parametrize("delimiter", ["?", "#"])
def test_internal_routed_path_verifier_rejects_delimiters(delimiter):
    path = f"{_PREFIX}/D{delimiter}one/a"
    headers = _headers(path)
    assert not verify_internal_request(
        _SECRET, agent_name="sample", method="GET", path=path,
        timestamp=headers[INTERNAL_TIMESTAMP_HEADER],
        signature=headers[INTERNAL_SIGNATURE_HEADER],
    )
