"""Static endpoints must remain reachable through the registered HTTP router."""

import re
from itertools import product

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from pinky_daemon.api import create_api
from pinky_daemon.auth import build_internal_auth_headers


def _path_examples(route):
    """Render valid paths and track which characters came from static literals."""
    samples = ("sample", "1", "1.5", "00000000-0000-0000-0000-000000000001", "a/b")
    tokens = list(re.finditer(r"\{([^{}]+)\}", route.path_format))
    options = [
        [value for value in samples if re.fullmatch(route.param_convertors[token[1]].regex, value)]
        for token in tokens
    ]
    assert all(options), f"Add a path example for a converter in {route.path}"
    for values in product(*options):
        path, static_positions, end = "", set(), 0
        for token, value in zip(tokens, values):
            literal = route.path_format[end : token.start()]
            static_positions.update(len(path) + i for i, char in enumerate(literal) if char != "/")
            path += literal + value
            end = token.end()
        literal = route.path_format[end:]
        static_positions.update(len(path) + i for i, char in enumerate(literal) if char != "/")
        yield path + literal, static_positions


def _shadowed_routes(routes):
    """Find earlier parameter captures consuming a later route's static text."""
    shadows = set()
    for index, later in enumerate(routes):
        if not getattr(later, "methods", None):
            continue
        earlier_routes = [
            earlier
            for earlier in routes[:index]
            if getattr(earlier, "param_convertors", None)
            and later.methods.intersection(getattr(earlier, "methods", ()) or ())
        ]
        for path, static_positions in _path_examples(later):
            for earlier in earlier_routes:
                match = earlier.path_regex.fullmatch(path)
                if match and any(
                    static_positions.intersection(range(*match.span(name)))
                    for name in earlier.param_convertors
                ):
                    methods = ",".join(sorted(later.methods.intersection(earlier.methods)))
                    shadows.add(f"{methods} {later.path} is shadowed by earlier {earlier.path}")
    return sorted(shadows)


@pytest.fixture
def app(tmp_path):
    application = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    yield application
    application.state.agents.close()


def test_static_routes_are_not_shadowed_by_earlier_parameters(app):
    shadows = _shadowed_routes(app.routes)
    assert not shadows, "\n".join(shadows)


@pytest.mark.real_auth
def test_signed_skill_apply_reaches_real_handler(app, tmp_path):
    registry = app.state.agents
    working_dir = tmp_path / "sample"
    registry.register("sample", working_dir=str(working_dir))
    path = "/agents/sample/skills/apply"
    headers = build_internal_auth_headers(
        registry.get_signing_key("sample"),
        agent_name="sample",
        method="POST",
        path=path,
    )
    client = TestClient(app)
    try:
        assert not client.cookies
        response = client.post(path, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["applied"] is True
        assert result["agent"] == "sample"
        assert result["session_restarted"] is False
        assert (working_dir / ".mcp.json").is_file()
    finally:
        client.close()


@pytest.mark.parametrize(
    "earlier,later,methods,expected",
    [
        ("/items/{name}", "/items/apply", ("POST", "POST"), True),
        ("/owners/{owner}/items/{name}", "/owners/{owner}/items/apply", ("POST", "POST"), True),
        ("/items/{name}", "/items/apply", ("GET", "POST"), False),
        ("/items/apply", "/items/{name}", ("POST", "POST"), False),
        ("/items/{number:int}", "/items/apply", ("POST", "POST"), False),
        ("/items/{number:int}", "/items/1", ("POST", "POST"), True),
        ("/files/{path:path}", "/files/static/info", ("GET", "GET"), True),
        ("/owners/{id:int}/items/{name}", "/owners/{owner}/items/apply", ("POST", "POST"), True),
    ],
)
def test_shadow_audit_respects_literals_converters_and_methods(earlier, later, methods, expected):
    async def endpoint(request):
        pass

    routes = [
        Route(path, endpoint, methods=[method]) for path, method in zip((earlier, later), methods)
    ]
    assert bool(_shadowed_routes(routes)) is expected
