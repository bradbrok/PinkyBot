"""Signed read diagnostics preserve existing authorization and responses."""

import inspect
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from pinky_daemon import api
from pinky_daemon import isolated_policy as policy
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from tests.conftest import TEST_SESSION_SECRET
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth

PENDING_CASES = [
    ("/agents", "/agents"),
    ("/settings/owner-profile", "/settings/owner-profile"),
    ("/settings/main-agent", "/settings/main-agent"),
    ("/tasks", "/tasks"),
    ("/tasks/{task_id}", "/tasks/987654"),
    ("/skills/{name}", "/skills/missing-skill"),
    ("/kb/search", "/kb/search?q=fixture"),
    ("/kb/wiki/{slug:path}", "/kb/wiki/missing-page"),
    ("/kb/stats", "/kb/stats"),
    ("/api/voice/requests", "/api/voice/requests"),
    ("/api/voice/sessions", "/api/voice/sessions"),
]
UNLISTED_CASES = [
    ("/system/primary-user", "/system/primary-user"),
    ("/comms/messages", "/comms/messages"),
    ("/conversations", "/conversations"),
    ("/sessions/{session_id}", "/sessions/private-session-marker"),
    ("/sessions/{session_id}", "/sessions/missing-session-marker"),
]


class _ApiTime:
    def time(self):
        return 1700000000.0

    def __getattr__(self, name):
        return getattr(time, name)


class _ApiUuid:
    def uuid4(self):
        return uuid.UUID(int=1)

    def __getattr__(self, name):
        return getattr(uuid, name)


@pytest.fixture(autouse=True)
def private_runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(api, "time", _ApiTime())
    monkeypatch.setattr(api, "uuid", _ApiUuid())
    monkeypatch.setattr(policy, "_read_policy_warning_emitted", False, raising=False)
    monkeypatch.delenv("PINKY_ISOLATED_READ_POLICY_MODE", raising=False)


@pytest.fixture
def logs(monkeypatch):
    messages = []
    monkeypatch.setattr(api, "_log", lambda text, **kwargs: messages.append(str(text)))
    return messages


def _read_lines(logs):
    return [line for line in logs if line.startswith("isolation: WOULD DENY READ ")]


def _unchanged_read(d, monkeypatch, logs, path, *, name="tenant", method="GET", read_mode=None):
    headers = signed(d, method, path.split("?", 1)[0], name)
    client = TestClient(d.app)
    try:
        monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "off")
        baseline = client.request(method, path, headers=headers)
        if read_mode is None:
            monkeypatch.delenv("PINKY_ISOLATED_READ_POLICY_MODE", raising=False)
        else:
            monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", read_mode)
        logs.clear()
        response = client.request(method, path, headers=headers)
    finally:
        client.close()
    assert response.status_code == baseline.status_code
    assert response.content == baseline.content
    assert dict(response.headers) == dict(baseline.headers)
    return response


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("template,path", PENDING_CASES)
def test_pending_reads_log_templates_without_changing_responses(
    daemon, monkeypatch, logs, mode, template, path
):
    d = daemon(mode)
    _unchanged_read(d, monkeypatch, logs, path)
    assert _read_lines(logs) == [
        f"isolation: WOULD DENY READ GET {template} for tenant reason=pending-scope repeats=0"
    ]


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("template,path", UNLISTED_CASES)
def test_unlisted_reads_log_templates_without_changing_responses(
    daemon, monkeypatch, logs, mode, template, path
):
    d = daemon(mode)
    if path == "/sessions/private-session-marker":
        d.app.state.manager.create(
            session_id="private-session-marker",
            agent_name="normal",
            working_dir=str(d.root / "normal"),
        )
    _unchanged_read(d, monkeypatch, logs, path)
    assert _read_lines(logs) == [
        f"isolation: WOULD DENY READ GET {template} for tenant reason=unlisted repeats=0"
    ]
    if template == "/sessions/{session_id}":
        assert path.rsplit("/", 1)[1] not in "\n".join(logs)


def _shadow_check(d):
    middleware = next(
        item
        for item in d.app.user_middleware
        if getattr(item.kwargs.get("dispatch"), "__name__", "") == "auth_middleware"
    )
    return inspect.getclosurevars(middleware.kwargs["dispatch"]).nonlocals["_isolated_read_shadow"]


def _request(d, path="/comms/messages", method="GET"):
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "server": ("testserver", 80),
            "root_path": "",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "app": d.app,
        }
    )


@pytest.mark.parametrize("template,path", PENDING_CASES + UNLISTED_CASES)
def test_normal_signed_reads_have_no_read_line(daemon, monkeypatch, logs, template, path):
    d = daemon()
    _unchanged_read(d, monkeypatch, logs, path, name="normal")
    assert _read_lines(logs) == []


@pytest.mark.parametrize("path", ["/agents/tenant", "/agents/tenant/health", "/agents/normal"])
def test_agent_named_reads_keep_existing_boundaries_and_have_no_read_line(
    daemon, monkeypatch, logs, path
):
    d = daemon()
    response = _unchanged_read(d, monkeypatch, logs, path)
    if path == "/agents/normal":
        assert response.status_code == 403
    assert _read_lines(logs) == []


def test_browser_session_reads_have_no_read_line(daemon, monkeypatch, logs):
    d = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "shadow")
    client = TestClient(d.app)
    client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
    logs.clear()
    try:
        response = client.get("/comms/messages")
    finally:
        client.close()
    assert response.status_code == 200
    assert _read_lines(logs) == []


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("path", ["/models/roster", "/admin/missing-read"])
def test_existing_targetless_refusals_do_not_run_read_diagnostics(
    daemon, monkeypatch, logs, mode, path
):
    d = daemon(mode)
    response = _unchanged_read(d, monkeypatch, logs, path, read_mode="shadow")
    assert response.status_code == 403
    assert _read_lines(logs) == []


@pytest.mark.parametrize("method,path", [("OPTIONS", "/comms/messages"), ("GET", "/auth/status")])
def test_options_and_public_requests_have_no_read_line(daemon, monkeypatch, logs, method, path):
    d = daemon()
    _unchanged_read(d, monkeypatch, logs, path, method=method)
    assert _read_lines(logs) == []


@pytest.mark.parametrize(
    "mode,status,action", [("shadow", 404, "WOULD DENY"), ("enforce", 403, "DENY")]
)
def test_write_checks_and_their_lines_stay_unchanged(
    daemon, monkeypatch, logs, mode, status, action
):
    d = daemon(mode)
    response = _unchanged_read(d, monkeypatch, logs, "/missing-write-route", method="POST")
    assert response.status_code == status
    assert _read_lines(logs) == []
    assert logs == [f"isolation: {action} POST <unknown> for tenant mode={mode} reason=unlisted"]


@pytest.mark.parametrize(
    "main,read,expected",
    [
        ("off", None, False),
        ("off", "", False),
        ("shadow", None, True),
        ("enforce", None, True),
        ("enforce", "", True),
        ("enforce", "off", False),
        ("off", "shadow", True),
        ("enforce", " SHADOW ", True),
        ("enforce", " OFF ", False),
    ],
)
def test_read_mode_defaults_and_overrides_never_change_responses(
    daemon, monkeypatch, logs, main, read, expected
):
    d = daemon(main)
    response = _unchanged_read(d, monkeypatch, logs, "/comms/messages", read_mode=read)
    assert response.status_code == 200
    assert bool(_read_lines(logs)) is expected


@pytest.mark.parametrize("value", ["enforce", "bogus"])
def test_invalid_read_modes_use_shadow_and_warn_once(daemon, monkeypatch, logs, value):
    d = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", value)
    headers = signed(d, "GET", "/comms/messages")
    client = TestClient(d.app)
    logs.clear()
    try:
        responses = [client.get("/comms/messages", headers=headers) for _ in range(2)]
    finally:
        client.close()
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].content == responses[1].content
    assert dict(responses[0].headers) == dict(responses[1].headers)
    assert [line for line in logs if "read policy mode" in line] == [
        f"isolation: read policy mode {value} is not available; using shadow"
    ]
    assert len(_read_lines(logs)) == 1


def test_invalid_mode_warning_is_process_wide_and_safe(monkeypatch, logs):
    raw = "bad\n" + "x" * 80 + "\x1b"
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", raw)
    assert policy.read_policy_mode(logs.append) == "shadow"
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert policy.read_policy_mode(logs.append) == "shadow"
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip().lower())[:32]
    assert logs == [f"isolation: read policy mode {safe} is not available; using shadow"]
    assert len(safe) == 32 and "\n" not in logs[0] and "\x1b" not in logs[0]


def test_invalid_mode_without_a_callback_uses_one_error_record(monkeypatch, caplog):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert policy.read_policy_mode() == "shadow"
    assert policy.read_policy_mode() == "shadow"
    records = [record for record in caplog.records if "read policy mode" in record.message]
    assert len(records) == 1 and records[0].levelname == "ERROR"


def test_identical_reads_only_log_once_until_the_window_expires(daemon, monkeypatch, logs):
    d = daemon()
    now = [100.0]
    monkeypatch.setattr(api, "_isolated_read_now", lambda: now[0])
    client = TestClient(d.app)
    headers = signed(d, "GET", "/comms/messages")
    logs.clear()
    try:
        for _ in range(50):
            assert client.get("/comms/messages", headers=headers).status_code == 200
        assert _read_lines(logs) == [
            "isolation: WOULD DENY READ GET /comms/messages for tenant reason=unlisted repeats=0"
        ]
        now[0] = 701.0
        assert client.get("/comms/messages", headers=headers).status_code == 200
        assert _read_lines(logs)[-1] == (
            "isolation: WOULD DENY READ GET /comms/messages for tenant reason=unlisted repeats=49"
        )
        assert len(_read_lines(logs)) == 2
    finally:
        client.close()


def test_caller_template_and_reason_have_separate_repeat_keys(daemon, monkeypatch, logs):
    d = daemon()
    check = _shadow_check(d)
    monkeypatch.setattr(api, "_isolated_read_now", lambda: 100.0)
    logs.clear()
    check(_request(d), "tenant")
    check(_request(d), "peer")
    check(_request(d, "/system/primary-user"), "tenant")
    monkeypatch.setattr(api, "isolation_flag", lambda registry, name: None)
    check(_request(d), "tenant")
    assert _read_lines(logs) == [
        "isolation: WOULD DENY READ GET /comms/messages for tenant reason=unlisted repeats=0",
        "isolation: WOULD DENY READ GET /comms/messages for peer reason=unlisted repeats=0",
        "isolation: WOULD DENY READ GET /system/primary-user for tenant reason=unlisted repeats=0",
        "isolation: WOULD DENY READ GET /comms/messages for tenant reason=registry unavailable repeats=0",
    ]


def test_repeat_map_is_private_to_each_app(daemon, logs):
    first, second = daemon(), daemon()
    logs.clear()
    _shadow_check(first)(_request(first), "tenant")
    _shadow_check(second)(_request(second), "tenant")
    assert len(_read_lines(logs)) == 2
    assert all(line.endswith("repeats=0") for line in _read_lines(logs))


def test_repeat_map_resets_only_when_a_new_key_exceeds_the_limit(daemon, monkeypatch, logs):
    d = daemon()
    check = _shadow_check(d)
    monkeypatch.setattr(api, "resolve_mutation_route", lambda app, scope: (scope["path"], {}))
    monkeypatch.setattr(api, "_isolated_read_now", lambda: 100.0)
    logs.clear()
    reset = "isolation: read shadow log map reset (1024 keys)"
    for index in range(1024):
        check(_request(d, f"/read-counter/{index}"), "tenant")
    assert len(_read_lines(logs)) == 1024 and reset not in logs
    check(_request(d, "/read-counter/0"), "tenant")
    assert len(_read_lines(logs)) == 1024 and reset not in logs
    check(_request(d, "/read-counter/1024"), "tenant")
    assert logs.count(reset) == 1
    assert _read_lines(logs)[-1].endswith("repeats=0")
    check(_request(d, "/read-counter/0"), "tenant")
    assert len(_read_lines(logs)) == 1026 and logs.count(reset) == 1
    assert _read_lines(logs)[-1] == (
        "isolation: WOULD DENY READ GET /read-counter/0 for tenant reason=unlisted repeats=0"
    )


def test_a_repeat_599_seconds_later_stays_quiet(daemon, monkeypatch, logs):
    d = daemon()
    check = _shadow_check(d)
    now = [100.0]
    monkeypatch.setattr(api, "_isolated_read_now", lambda: now[0])
    logs.clear()
    check(_request(d), "tenant")
    expected = [
        "isolation: WOULD DENY READ GET /comms/messages for tenant reason=unlisted repeats=0"
    ]
    assert _read_lines(logs) == expected
    now[0] = 699.0
    check(_request(d), "tenant")
    assert _read_lines(logs) == expected


def test_the_window_ends_at_exactly_600_seconds(daemon, monkeypatch, logs):
    d = daemon()
    check = _shadow_check(d)
    now = [100.0]
    monkeypatch.setattr(api, "_isolated_read_now", lambda: now[0])
    logs.clear()
    check(_request(d), "tenant")
    assert len(_read_lines(logs)) == 1
    now[0] = 700.0
    check(_request(d), "tenant")
    lines = _read_lines(logs)
    assert len(lines) == 2
    assert lines[1] == (
        "isolation: WOULD DENY READ GET /comms/messages for tenant reason=unlisted repeats=0"
    )


def test_an_expired_key_already_in_a_full_map_does_not_reset_it(daemon, monkeypatch, logs):
    d = daemon()
    check = _shadow_check(d)
    now = [100.0]
    monkeypatch.setattr(api, "resolve_mutation_route", lambda app, scope: (scope["path"], {}))
    monkeypatch.setattr(api, "_isolated_read_now", lambda: now[0])
    logs.clear()
    for index in range(1024):
        check(_request(d, f"/read-counter/{index}"), "tenant")
    assert len(_read_lines(logs)) == 1024
    now[0] = 700.0
    check(_request(d, "/read-counter/0"), "tenant")
    assert logs.count("isolation: read shadow log map reset (1024 keys)") == 0
    lines = _read_lines(logs)
    assert len(lines) == 1025
    assert lines[-1] == (
        "isolation: WOULD DENY READ GET /read-counter/0 for tenant reason=unlisted repeats=0"
    )


def test_registry_lookup_failure_is_diagnostic_only(daemon, monkeypatch, logs):
    d = daemon()
    original = d.agents.get

    def unavailable(name):
        if name == "tenant":
            raise RuntimeError("private-registry-marker")
        return original(name)

    monkeypatch.setattr(d.agents, "get", unavailable)
    response = _unchanged_read(d, monkeypatch, logs, "/comms/messages")
    assert response.status_code == 200
    assert _read_lines(logs) == [
        "isolation: WOULD DENY READ GET /comms/messages for tenant reason=registry unavailable repeats=0"
    ]
    assert "private-registry-marker" not in "\n".join(_read_lines(logs))


def test_unknown_paths_use_a_shared_unknown_template(daemon, monkeypatch, logs):
    d = daemon()
    response = _unchanged_read(d, monkeypatch, logs, "/missing-route/private-path-marker")
    assert response.status_code == 404
    assert _read_lines(logs) == [
        "isolation: WOULD DENY READ GET <unknown> for tenant reason=unlisted repeats=0"
    ]
    assert "private-path-marker" not in "\n".join(logs)


@pytest.mark.parametrize(
    "explicit_head,template,status",
    [
        (True, "/read-head/{key}", 200),
        (False, "<unknown>", 405),
    ],
)
def test_head_logs_the_resolved_template_or_unknown(
    daemon, monkeypatch, logs, explicit_head, template, status
):
    d = daemon()

    async def read_head(key: str):
        return {"key": key}

    d.app.add_api_route(
        "/read-head/{key}", read_head, methods=["GET", "HEAD"] if explicit_head else ["GET"]
    )
    response = _unchanged_read(
        d, monkeypatch, logs, "/read-head/private-head-marker", method="HEAD"
    )
    assert response.status_code == status
    assert _read_lines(logs) == [
        f"isolation: WOULD DENY READ HEAD {template} for tenant reason=unlisted repeats=0"
    ]
    assert "private-head-marker" not in "\n".join(logs)


def test_method_is_part_of_the_repeat_key(daemon, logs):
    d = daemon()

    async def read_both():
        return {"ok": True}

    d.app.add_api_route("/read-both", read_both, methods=["GET", "HEAD"])
    logs.clear()
    check = _shadow_check(d)
    check(_request(d, "/read-both", "GET"), "tenant")
    check(_request(d, "/read-both", "HEAD"), "tenant")
    assert _read_lines(logs) == [
        "isolation: WOULD DENY READ GET /read-both for tenant reason=unlisted repeats=0",
        "isolation: WOULD DENY READ HEAD /read-both for tenant reason=unlisted repeats=0",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/skills/private-read-marker?filter=private-query-marker",
        "/kb/wiki/private-read-marker?filter=private-query-marker",
    ],
)
def test_read_lines_never_contain_path_or_query_values(daemon, monkeypatch, logs, path):
    d = daemon()
    _unchanged_read(d, monkeypatch, logs, path)
    assert len(_read_lines(logs)) == 1
    assert "private-read-marker" not in "\n".join(logs)
    assert "private-query-marker" not in "\n".join(logs)


def test_caller_names_use_the_existing_safe_format(daemon, logs):
    d = daemon()
    caller = "unusual\ncaller /" + "x" * 150
    logs.clear()
    _shadow_check(d)(_request(d), caller)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", caller)[:120]
    assert _read_lines(logs) == [
        f"isolation: WOULD DENY READ GET /comms/messages for {safe} reason=registry unavailable repeats=0"
    ]
    assert len(safe) == 120 and "\n" not in logs[0]


def test_resolution_exception_does_not_change_the_response(daemon, monkeypatch, logs):
    d = daemon()

    def unavailable(app, scope):
        raise RuntimeError("private-resolution-marker")

    monkeypatch.setattr(api, "resolve_mutation_route", unavailable)
    response = _unchanged_read(d, monkeypatch, logs, "/comms/messages")
    assert response.status_code == 200
    assert logs == ["isolation: read shadow check failed: RuntimeError"]


def test_log_sink_failure_cannot_refuse_the_read(daemon, monkeypatch, logs):
    d = daemon()
    attempts = []

    def broken_log(text):
        attempts.append(text)
        raise RuntimeError("private-log-marker")

    monkeypatch.setattr(api, "_log", broken_log)
    response = _unchanged_read(d, monkeypatch, logs, "/comms/messages")
    assert response.status_code == 200
    assert len(attempts) == 2
    assert attempts[1] == "isolation: read shadow check failed: RuntimeError"


def test_pending_entries_are_the_exact_existing_get_templates(daemon):
    d = daemon()
    expected = frozenset(("GET", template) for template, _ in PENDING_CASES)
    assert isinstance(policy.ISOLATED_READ_PENDING, frozenset)
    assert policy.ISOLATED_READ_PENDING == expected
    registered = {
        (method, route.path)
        for route in d.app.routes
        for method in (getattr(route, "methods", None) or ())
    }
    assert expected <= registered
