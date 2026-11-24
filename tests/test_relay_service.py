"""ASGI contracts exercise auth-before-read, quotas and bounded concurrency."""

import asyncio
import base64
import hashlib
import json
import sys
from contextlib import contextmanager

import pytest

from pinky_relay.app import WindowLimit, create_relay_app
from pinky_relay.config import RelayConfig
from pinky_relay.store import MAX_BLOB, MIB, QueueStore

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")
TOKENS = {name: "synthetic-token-" + name + "-01234567890123456789"
          for name in ("peer-one", "peer-two", "receiver")}


@contextmanager
def service(tmp_path, *, tokens=None, **kwargs):
    tokens = tokens or TOKENS
    config = RelayConfig.from_mapping({k: hashlib.sha256(v.encode()).hexdigest() for k, v in tokens.items()})
    store = QueueStore(tmp_path / "queue.db", config.digests, disk_cap=96 * MIB)
    app = create_relay_app(config, store, **kwargs)
    try:
        yield app, store
    finally:
        store.close()


async def request(app, path, *, method="GET", body=b"", token=TOKENS["peer-one"],
                  headers=None, client="127.0.0.1", length=True):
    query = b""
    if "?" in path:
        path, query = path.split("?", 1)
        query = query.encode()
    fields = list(headers or [])
    if token is not None:
        fields.append((b"authorization", ("Bearer " + token).encode("latin-1")))
    if method == "POST" and length:
        fields.append((b"content-length", str(len(body)).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query, "headers": fields, "client": (client, 1234),
             "server": ("127.0.0.1", 8898), "root_path": ""}
    sent, reads = [], []

    async def receive():
        reads.append(True)
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(event):
        sent.append(event)

    await app(scope, receive, send)
    status = next(e["status"] for e in sent if e["type"] == "http.response.start")
    raw = b"".join(e.get("body", b"") for e in sent if e["type"] == "http.response.body")
    return status, json.loads(raw) if raw else None, reads, sent


def encoded(target="peer-two", blob=b"sealed fixture", **extra):
    return json.dumps({"to_fleet": target, "blob_b64": base64.b64encode(blob).decode(), **extra}).encode()


@pytest.mark.parametrize("path,method", [("/v1/send", "POST"), ("/v1/poll", "GET"), ("/v1/ack", "POST"), ("/unlisted", "GET")])
@pytest.mark.parametrize("token", [None, "wrong-token", ""])
async def test_every_nonhealth_route_authenticates_before_any_body_read(tmp_path, path, method, token):
    with service(tmp_path) as (app, _):
        result = await request(app, path, method=method, token=token, body=b"x" * 500000)
        assert result[0] == 401 and result[2] == []


async def test_health_is_content_free_and_prefix_stripping_contract_works(tmp_path):
    with service(tmp_path) as (app, _):
        for path in ("/health", "/ferry-relay/health"):
            result = await request(app, path, token=None)
            assert result[0:2] == (200, {"ok": True})
        assert app.root_path == "/ferry-relay"


async def test_token_stamps_sender_and_scopes_poll_ack(tmp_path):
    with service(tmp_path) as (app, q):
        result = await request(app, "/v1/send", method="POST", body=encoded(sender_fleet="receiver"))
        assert result[0] == 200 and len(result[1]["id"]) <= 64
        ident = result[1]["id"]
        stored = q._db.execute("SELECT sender_fleet,blob FROM messages WHERE id=?", (ident,)).fetchone()
        assert stored == ("peer-one", b"sealed fixture")
        assert (await request(app, "/v1/poll"))[1] == {"rows": []}
        page = await request(app, "/v1/poll", token=TOKENS["peer-two"])
        assert page[0] == 200 and [r["id"] for r in page[1]["rows"]] == [ident]
        ack = json.dumps({"ids": [ident]}).encode()
        assert (await request(app, "/v1/ack", method="POST", body=ack))[1] == {"deleted": 0}
        assert (await request(app, "/v1/ack", method="POST", body=ack, token=TOKENS["peer-two"]))[1] == {"deleted": 1}


@pytest.mark.parametrize("path,cap", [("/v1/send", 400 * 1024), ("/v1/ack", 16 * 1024)])
async def test_body_caps_and_missing_or_chunked_lengths_refuse_before_read(tmp_path, path, cap):
    with service(tmp_path) as (app, _):
        missing = await request(app, path, method="POST", body=b"{}", length=False)
        assert missing[0] == 411 and missing[2] == []
        chunked = await request(app, path, method="POST", body=b"{}", headers=[(b"transfer-encoding", b"chunked")])
        assert chunked[0] == 400 and chunked[2] == []
        over = await request(app, path, method="POST", body=b"x" * (cap + 1))
        assert over[0] == 413 and over[2] == []
        huge = await request(app, path, method="POST", length=False,
                             headers=[(b"content-length", b"9" * 100)])
        assert huge[0] == 413 and huge[2] == []


async def test_actual_stream_length_and_json_shapes_are_bounded(tmp_path):
    with service(tmp_path) as (app, _):
        wrong = await request(app, "/v1/send", method="POST", body=b"{}", length=False,
                              headers=[(b"content-length", b"1")])
        assert wrong[0] == 400
        duplicate = await request(app, "/v1/send", method="POST", body=b'{"to_fleet":"a","to_fleet":"b"}')
        assert duplicate[0] == 400
        deep = await request(app, "/v1/send", method="POST", body=(b'{"x":' * 21 + b'{}' + b'}' * 21))
        assert deep[0] == 400


async def test_decoded_blob_limit_and_unknown_destination(tmp_path):
    with service(tmp_path) as (app, _):
        assert (await request(app, "/v1/send", method="POST", body=encoded(blob=b"x" * MAX_BLOB)))[0] == 200
        assert (await request(app, "/v1/send", method="POST", body=encoded(blob=b"x" * (MAX_BLOB + 1))))[0] == 413
        assert (await request(app, "/v1/send", method="POST", body=encoded(target="absent")))[0] == 404


@pytest.mark.parametrize("ids", [["a"] * 101, ["a" * 65], ["not-ascii-\u00e9"], [False], "not-a-list"])
async def test_ack_list_and_ascii_id_limits(tmp_path, ids):
    with service(tmp_path) as (app, _):
        result = await request(app, "/v1/ack", method="POST", body=json.dumps({"ids": ids}).encode())
        assert result[0] == 400


async def test_header_limit_and_growth_stop_statuses(tmp_path):
    with service(tmp_path) as (app, q):
        assert (await request(app, "/v1/poll", headers=[(b"x-large", b"x" * 16384)]))[0] == 431
        q.stopped = True
        assert (await request(app, "/v1/send", method="POST", body=encoded()))[0] == 507
        for path, method, body in (("/v1/poll", "GET", b""), ("/v1/ack", "POST", b'{"ids":[]}')):
            result = await request(app, path, method=method, body=body)
            assert result[0] == 503
            assert any((b"retry-after", b"1") in e.get("headers", []) for e in result[3])


async def test_authenticated_rate_sixty_per_minute(tmp_path):
    now = [100.0]
    with service(tmp_path, clock=lambda: now[0]) as (app, _):
        for _ in range(60):
            assert (await request(app, "/v1/poll"))[0] == 200
        assert (await request(app, "/v1/poll"))[0] == 429
        now[0] += 60
        assert (await request(app, "/v1/poll"))[0] == 200


async def test_bad_auth_rate_thirty_uses_tcp_peer_unless_trusted_proxy(tmp_path):
    with service(tmp_path) as (app, _):
        for i in range(30):
            result = await request(app, "/v1/poll", token="wrong", client="10.0.0.1",
                                   headers=[(b"x-real-ip", f"10.2.0.{i}".encode())])
            assert result[0] == 401
        assert (await request(app, "/v1/poll", token="wrong", client="10.0.0.1"))[0] == 429
        assert (await request(app, "/v1/poll", token="wrong", client="10.0.0.2"))[0] == 401
        for _ in range(30):
            assert (await request(app, "/v1/poll", token="wrong", headers=[(b"x-real-ip", b"10.3.0.1")]))[0] == 401
        assert (await request(app, "/v1/poll", token="wrong", headers=[(b"x-real-ip", b"10.3.0.1")]))[0] == 429
        assert (await request(app, "/v1/poll", token="wrong", headers=[(b"x-real-ip", b"10.3.0.2")]))[0] == 401


def test_failure_limiter_memory_does_not_evict_live_sources_to_bypass_limits():
    limiter = WindowLimit(30, max_keys=2)
    assert limiter.take("first") and limiter.take("second")
    assert not limiter.take("third")
    for _ in range(29):
        assert limiter.take("first")
    assert not limiter.take("first")


async def test_two_polls_per_token_wait_outside_database_transactions(tmp_path):
    with service(tmp_path) as (app, q):
        tasks = [asyncio.create_task(request(app, "/v1/poll?wait=25")) for _ in range(2)]
        try:
            for _ in range(100):
                if app.state.relay.polls == 2:
                    break
                await asyncio.sleep(0)
            assert app.state.relay.polls == 2
            assert not q._db.in_transaction and not app.state.relay.writer.locked()
            assert (await request(app, "/v1/poll?wait=25"))[0] == 429
            incoming = await request(app, "/v1/send", method="POST", token=TOKENS["peer-two"],
                                     body=encoded(target="peer-one"))
            assert incoming[0] == 200
            tasks[0].cancel()
            tasks[1].cancel()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert app.state.relay.polls == 0


async def test_global_long_poll_limit_is_thirty_two(tmp_path):
    tokens = {f"fleet-{i}": f"synthetic-token-{i}" for i in range(17)}
    with service(tmp_path, tokens=tokens) as (app, _):
        tasks = [asyncio.create_task(request(app, "/v1/poll?wait=25", token=tokens[f"fleet-{i}"])) for i in range(16) for _ in range(2)]
        try:
            for _ in range(100):
                if app.state.relay.polls == 32:
                    break
                await asyncio.sleep(0)
            assert app.state.relay.polls == 32
            assert (await request(app, "/v1/poll?wait=25", token=tokens["fleet-16"]))[0] == 429
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_total_inflight_sixty_four_and_eight_per_token(tmp_path):
    tokens = {f"fleet-{i}": f"synthetic-token-{i}" for i in range(9)}
    release = asyncio.Event()
    with service(tmp_path, tokens=tokens) as (app, _):
        @app.get("/fixture/hold")
        async def hold():
            await release.wait()
            return {"ok": True}

        tasks = [asyncio.create_task(request(app, "/fixture/hold", token=tokens[f"fleet-{i}"])) for i in range(8) for _ in range(8)]
        try:
            for _ in range(100):
                if app.state.relay.requests == 64:
                    break
                await asyncio.sleep(0)
            assert app.state.relay.requests == 64
            assert (await request(app, "/v1/poll", token=tokens["fleet-8"]))[0] == 429
            assert all(app.state.relay.per_token[f"fleet-{i}"] == 8 for i in range(8))
            tasks[-1].cancel()
            await asyncio.gather(tasks[-1], return_exceptions=True)
            assert (await request(app, "/v1/poll", token=tokens["fleet-0"]))[0] == 429
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert app.state.relay.requests == 0


async def test_logs_never_format_token_or_blob_and_reasons_are_summarized(tmp_path, caplog):
    sentinel = "synthetic-private-body-sentinel-0123456789"
    with service(tmp_path) as (app, _):
        for _ in range(4):
            await request(app, "/v1/send", method="POST", body=sentinel.encode(), token=sentinel)
        logs = "\n".join(r.getMessage() + repr(r.args) for r in caplog.records)
        assert sentinel not in logs
        assert len([r for r in caplog.records if r.name == "pinky_relay.app"]) == 1
