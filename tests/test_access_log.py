"""HTTP access receipts: authentication, credential redaction, and file failures."""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import stat
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import SESSION_COOKIE_NAME, create_api
from pinky_daemon.auth import build_internal_auth_headers, create_session_cookie

pytestmark = pytest.mark.real_auth
SECRET = "access-log-test-session-secret-32-bytes"
KEYS = [
    "ts",
    "rid",
    "crid",
    "peer",
    "port",
    "xff",
    "ts_user",
    "method",
    "path",
    "qk",
    "status",
    "dur_ms",
    "gate",
    "caller",
    "ua",
    "upgrade",
]


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setenv("PINKY_SESSION_SECRET", SECRET)
    clients = []

    def make(*, mode="enforce", path=None, explicit=False):
        index = len(clients)
        target = path or tmp_path / f"access-{index}.log"
        monkeypatch.setenv(
            "PINKY_ACCESS_LOG", str(target.with_suffix(".env")) if explicit else str(target)
        )
        monkeypatch.setenv("PINKY_AUTH_DENY_DEFAULT", mode)
        kwargs = {"access_log_path": target} if explicit else {}
        app = create_api(
            db_path=str(tmp_path / f"state-{index}" / "data.db"),
            default_working_dir=str(tmp_path),
            **kwargs,
        )
        client = TestClient(app, follow_redirects=False)
        clients.append(client)
        return client, target

    yield make
    for client in clients:
        writer = getattr(client.app.state, "access_log", None)
        if writer:
            writer.close()
        client.close()


def rows(path):
    assert path.exists(), "every enabled HTTP request must produce an access receipt"
    data = path.read_bytes()
    assert data.endswith(b"\n")
    return [json.loads(line) for line in data.splitlines()]


def login(client):
    client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(SECRET, user="operator"))


def test_t1_shape_and_minted_request_id(factory):
    client, path = factory()
    response = client.get("/api", headers={"X-Request-ID": "client.request-1"})
    assert response.status_code == 200
    [row] = rows(path)
    assert list(row) == KEYS
    assert re.fullmatch(r"[0-9a-f]{32}", row["rid"])
    assert row["rid"] == response.headers["x-request-id"]
    assert row["crid"] == "client.request-1" and row["rid"] != row["crid"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", row["ts"])
    assert row["peer"] == "testclient" and row["port"] == 80
    assert row["method"] == "GET" and row["path"] == "/api" and row["qk"] == []
    assert row["status"] == 200 and isinstance(row["dur_ms"], float)
    assert row["dur_ms"] >= 0 and round(row["dur_ms"], 1) == row["dur_ms"]
    assert row["gate"] == "public" and row["caller"] == "-"
    assert row["xff"] is row["ts_user"] is row["upgrade"] is None
    assert isinstance(row["ua"], str)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("crid", ["bad/id", "x" * 65, "bad value"])
def test_t1_client_request_id_rejected(factory, crid):
    client, path = factory()
    client.get("/api", headers={"X-Request-ID": crid})
    [row] = rows(path)
    assert row["crid"] is None and row["rid"] != crid


def test_t1_explicit_path_precedes_environment(factory):
    client, path = factory(explicit=True)
    assert client.get("/api").status_code == 200
    assert len(rows(path)) == 1


@pytest.mark.parametrize(
    "gate,target,status",
    [
        ("public", "/api", 200),
        ("public", "/assets/missing", 404),
        ("internal_hmac", "/agents", 200),
        ("deny_isolation", "/agents/other", 403),
        ("session", "/agents", 200),
        ("redirect_login", "/settings", 307),
        ("deny_browser_api", "/agents", 401),
        ("deny_protected_prefix", "/agents", 401),
        ("deny_default", "/unmapped", 401),
        ("shadow_passthrough", "/unmapped", 404),
    ],
)
def test_t2_auth_gates(factory, gate, target, status):
    client, path = factory(mode="shadow" if gate == "shadow_passthrough" else "enforce")
    headers = {}
    if gate in {"internal_hmac", "deny_isolation"}:
        registry = client.app.state.agents
        registry.register("worker", model="sonnet", isolated=gate == "deny_isolation")
        headers = build_internal_auth_headers(
            registry.get_signing_key("worker"), agent_name="worker", method="GET", path=target
        )
    if gate == "session":
        login(client)
    if gate == "deny_browser_api":
        headers = {"Accept": "application/json", "Sec-Fetch-Site": "same-origin"}
    response = client.get(target, headers=headers)
    assert response.status_code == status
    [row] = rows(path)
    assert row["gate"] == gate and row["status"] == status
    assert row["caller"] == {"session": "operator", "internal_hmac": "worker"}.get(gate, "-")


@pytest.mark.parametrize(
    "prefix,suffix",
    [("/hooks/", ""), ("/a/", "/asset"), ("/p/", ""), ("/p/", "/unlock"), ("/ws/voice/", "")],
)
def test_t3_secret_absence(factory, prefix, suffix):
    client, path = factory()
    secrets = [
        "path-credential-123456789",
        "query-secret-123",
        "auth-secret-123",
        "cookie-secret-123",
        "signature-secret-123",
        "body-secret-123",
    ]
    client.post(
        prefix + secrets[0] + suffix + "?token=" + secrets[1] + "&x=1",
        headers={
            "Authorization": "Bearer " + secrets[2],
            "Cookie": "secret=" + secrets[3],
            "X-Internal-Signature": secrets[4],
        },
        content=secrets[5],
    )
    [row] = rows(path)
    assert row["path"] == prefix + "<redacted>" + suffix
    assert row["qk"] == ["token", "x"]
    for secret in secrets:
        assert secret not in path.read_text()


def test_t3_shared_hook_filter_redacts_embedded_path():
    from pinky_daemon.routes.triggers import HookTokenRedactionFilter

    record = logging.LogRecord(
        "access", 20, "", 0, "GET /hooks/credential-secret HTTP/1.1", (), None
    )
    HookTokenRedactionFilter().filter(record)
    assert record.getMessage() == "GET /hooks/<redacted> HTTP/1.1"


def test_t4_upgrade_http_denied_and_logged(factory):
    client, path = factory()
    response = client.get("/agents", headers={"Upgrade": "websocket"})
    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "server-timing" in response.headers
    [row] = rows(path)
    assert row["gate"] == "deny_protected_prefix" and row["upgrade"] == "websocket"


@pytest.mark.parametrize(
    "header,expected", [("X-Content-Type-Options", "nosniff"), ("Server-Timing", "total;dur=")]
)
def test_upgrade_header_does_not_skip_http_middleware(factory, header, expected):
    client, _ = factory()
    response = client.get("/api", headers={"Upgrade": "websocket"})
    assert response.status_code == 200
    assert response.headers.get(header, "").startswith(expected)


def test_voice_websocket_dispatch_is_unchanged(factory, monkeypatch):
    import pinky_daemon.voice_routes as voice_routes

    client, path = factory()
    store = MagicMock()
    store.get_session.return_value = None
    monkeypatch.setattr(voice_routes, "_voice_store", store)
    with client.websocket_connect("/ws/voice/unknown-session") as ws:
        assert ws.receive() == {
            "type": "websocket.close",
            "code": 4004,
            "reason": "Session not found",
        }
    store.get_session.assert_called_once_with("unknown-session")
    assert not path.exists() or path.read_bytes() == b""
    response = client.get("/ws/voice/unknown-session")
    assert response.status_code == 401
    assert rows(path)[0]["gate"] == "deny_default"


@pytest.mark.parametrize("size", [0, 20, 10000])
def test_t5_forwarding_and_bounds(factory, size):
    client, path = factory()
    headers = (
        {}
        if not size
        else {
            "X-Forwarded-For": "1.2.3.4, 5.6.7.8" if size == 20 else "x" * size,
            "Tailscale-User-Login": "u" * size,
            "User-Agent": "a" * size,
            "Upgrade": "z" * size,
        }
    )
    client.get("/api", headers=headers)
    [row] = rows(path)
    assert row["peer"] == "testclient"
    for key, source, bound in [
        ("xff", "X-Forwarded-For", 256),
        ("ts_user", "Tailscale-User-Login", 256),
        ("upgrade", "Upgrade", 32),
    ]:
        assert row[key] == (headers[source][:bound] if size else None)
    if size:
        assert row["ua"] == headers["User-Agent"][:200]


def test_t5_path_bounded_after_redaction(factory):
    client, path = factory()
    client.get("/hooks/" + "s" * 1000 + "/" + "x" * 1000)
    [row] = rows(path)
    assert len(row["path"]) == 512 and row["path"].startswith("/hooks/<redacted>/")


def test_t6_exception_logged_and_reraised(factory):
    client, path = factory()
    error = RuntimeError("private-exception-message")

    @client.app.get("/api/explode")
    async def explode():
        raise error

    login(client)
    with pytest.raises(RuntimeError) as caught:
        client.get("/api/explode")
    assert caught.value is error
    [row] = rows(path)
    assert row["status"] == 500 and row["error"] == "RuntimeError"
    assert "private-exception-message" not in path.read_text()


@pytest.mark.parametrize("fault", ["directory", "closed_fd"])
def test_t7_write_failure_is_counted_and_throttled(factory, monkeypatch, tmp_path, fault):
    import pinky_daemon.access_log as access_log
    import pinky_daemon.api as api

    clock = [100.0]
    monkeypatch.setattr(access_log, "monotonic", lambda: clock[0])
    messages = []
    monkeypatch.setattr(api, "_log", messages.append)
    client, path = factory(path=tmp_path if fault == "directory" else None)
    writer = client.app.state.access_log
    if fault == "closed_fd":
        os.close(writer.fd)
        writer.fd = -1  # A closed descriptor that another test resource cannot reuse.
    for _ in range(3):
        assert client.get("/api").status_code == 200
    assert writer.write_failures == 3

    def logged():
        return [message for message in messages if "access log" in message.lower()]

    assert len(logged()) == 1
    clock[0] += 60
    assert client.get("/api").status_code == 200
    assert writer.write_failures == 4 and len(logged()) == 2
    login(client)
    health = client.get("/system/health")
    assert health.status_code == 200
    assert health.json()["access_log"]["write_failures"] == 4
    client.cookies.clear()
    assert client.get("/system/health").status_code == 401


def test_t8_rotation_retention_and_reopened_fd(factory):
    from pinky_daemon.log_rotation import LogRotator

    client, path = factory()
    client.get("/api")
    [before] = rows(path)
    now = time.time()
    old, recent = [path.with_name(path.name + suffix) for suffix in (".old.gz", ".recent.gz")]
    for archive, age in [(old, 91), (recent, 89)]:
        archive.write_bytes(b"archive")
        os.utime(archive, (now - age * 86400,) * 2)
    archive = LogRotator(
        path,
        backup_days=90,
        max_bytes=1,
        mode="rename",
        on_rotate=client.app.state.access_log.reopen,
    ).check_and_rotate()
    assert archive and not old.exists() and recent.exists()
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert json.loads(gzip.decompress(archive.read_bytes()))["rid"] == before["rid"]
    assert path.read_bytes() == b""
    client.get("/api")
    [after] = rows(path)
    assert after["rid"] != before["rid"]


def test_t9_kill_switch(tmp_path, monkeypatch):
    import pinky_daemon.api as api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_ACCESS_LOG", "off")
    messages = []
    monkeypatch.setattr(api, "_log", messages.append)
    app = create_api(db_path=str(tmp_path / "data.db"))
    client = TestClient(app)
    assert client.get("/api").status_code == 200
    assert not (tmp_path / "logs/access.log").exists()
    assert len([m for m in messages if "access log" in m.lower() and "off" in m.lower()]) == 1
    assert app.state.access_log.enabled is False
    assert app.state.access_log.write_failures == 0


def test_t10_auth_short_circuits_have_receipts(factory):
    client, path = factory()
    assert client.get("/agents").status_code == 401
    assert client.get("/settings").status_code == 307
    assert [(r["status"], r["gate"]) for r in rows(path)] == [
        (401, "deny_protected_prefix"),
        (307, "redirect_login"),
    ]


def test_t11_suite_default_never_creates_cwd_log(tmp_path, monkeypatch):
    assert os.environ["PINKY_ACCESS_LOG"] == "off"
    monkeypatch.chdir(tmp_path)
    app = create_api(db_path=str(tmp_path / "data.db"))
    assert TestClient(app).get("/api").status_code == 200
    assert not (tmp_path / "logs/access.log").exists()


@pytest.mark.parametrize("target,status", [("/agents", 401), ("/settings", 307)])
def test_denial_responses_have_security_headers(factory, target, status):
    client, _ = factory()
    response = client.get(target)
    assert response.status_code == status
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


# Bound event waits and descriptor handoff if either thread stalls.
@pytest.mark.timeout(5)
def test_t8_concurrent_rotation_never_loses_lines(tmp_path):
    from pinky_daemon.access_log import AccessLogWriter
    from pinky_daemon.log_rotation import LogRotator

    path = tmp_path / "access.log"
    writer = AccessLogWriter(path, log=lambda message: None)
    rotations, batch_size = 6, 32
    stopped, go, ready = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def produce():
        try:
            for batch in range(rotations):
                go.wait()
                go.clear()
                if stopped.is_set():
                    return
                for number in range(batch * batch_size, (batch + 1) * batch_size):
                    writer.write({"sequence": number})
                ready.set()
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    def handoff():
        # Write to the renamed inode before reopening it, then let the
        # producer wait so it cannot starve the descriptor handoff.
        ready.clear()
        go.set()
        ready.wait()
        assert not errors
        writer.reopen()

    rotator = LogRotator(path, mode="rename", on_rotate=handoff, max_bytes=1, backup_days=90)

    producer = threading.Thread(target=produce)
    producer.start()
    archives = []
    try:
        for _ in range(rotations):
            writer.write({"rotation_marker": len(archives)})
            archive = rotator.check_and_rotate()
            assert archive is not None
            archives.append(archive)
        writer.write({"sequence": rotations * batch_size})
    finally:
        stopped.set()
        go.set()
        producer.join()
        writer.close()
    assert not producer.is_alive() and not errors and writer.write_failures == 0
    documents = []
    for archive in archives:
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        documents.extend(
            json.loads(line) for line in gzip.decompress(archive.read_bytes()).splitlines()
        )
    documents.extend(json.loads(line) for line in path.read_bytes().splitlines())
    expected = []
    for batch in range(rotations):
        expected.append({"rotation_marker": batch})
        expected.extend(
            {"sequence": number}
            for number in range(batch * batch_size, (batch + 1) * batch_size)
        )
    expected.append({"sequence": rotations * batch_size})
    assert documents == expected


def test_t1_writer_uses_one_append_write_per_line(tmp_path, monkeypatch):
    import fcntl

    import pinky_daemon.access_log as access_log

    writer = access_log.AccessLogWriter(tmp_path / "access.log", log=lambda message: None)
    assert fcntl.fcntl(writer.fd, fcntl.F_GETFL) & os.O_APPEND
    real_write = os.write
    calls = []

    def observed_write(fd, data):
        if fd == writer.fd:
            calls.append(data)
        return real_write(fd, data)

    monkeypatch.setattr(access_log.os, "write", observed_write)
    try:
        writer.write({"sample": "value"})
        assert len(calls) == 1
        assert calls[0].endswith(b"\n")
        assert json.loads(calls[0]) == {"sample": "value"}
    finally:
        writer.close()


@pytest.mark.parametrize("segment,decoded", [
    ("prefix%20planted-secret", "prefix planted-secret"),
    ("prefix%09planted-secret", "prefix\tplanted-secret"),
    ("prefix%3Fplanted-secret", "prefix?planted-secret"),
    ("", ""),
    ("planted-secret" * 100, "planted-secret" * 100),
])
def test_t3_credential_position_redacts_any_segment_bytes(factory, segment, decoded):
    from pinky_daemon.access_log import redact_path

    client, path = factory()
    client.get("/hooks/" + segment)
    [row] = rows(path)
    assert row["path"] == "/hooks/<redacted>"
    assert redact_path("/hooks/" + decoded) == "/hooks/<redacted>"
    assert "planted-secret" not in path.read_text()


def test_t3_question_mark_in_asgi_path_is_a_credential_byte(factory):
    client, path = factory()
    app = client.app

    async def raw_path_app(scope, receive, send):
        scope = dict(scope, path="/p/prefix?planted-secret",
                     raw_path=b"/p/prefix%3Fplanted-secret", query_string=b"")
        await app(scope, receive, send)

    raw_client = TestClient(raw_path_app)
    try:
        raw_client.get("/probe")
    finally:
        raw_client.close()
    [row] = rows(path)
    assert row["path"] == "/p/<redacted>" and row["qk"] == []
    assert "planted-secret" not in path.read_text()
