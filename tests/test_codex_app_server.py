"""Unit tests for the codex app-server JSON-RPC transport client.

Drives CodexAppServerClient against an in-memory stream pair (a real
asyncio.StreamReader the test feeds server frames into, plus a fake writer
capturing client output) so no real ``codex app-server`` subprocess is needed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pinky_daemon.codex_app_server import CodexAppServerClient, CodexAppServerError


class FakeWriter:
    """Minimal asyncio.StreamWriter stand-in capturing written bytes."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def frames(self) -> list[dict]:
        return [json.loads(ln) for ln in self.buffer.split(b"\n") if ln.strip()]


def make_client(**kwargs) -> tuple[CodexAppServerClient, asyncio.StreamReader, FakeWriter]:
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    client = CodexAppServerClient(reader, writer, **kwargs)
    client.start()
    return client, reader, writer


async def _settle() -> None:
    """Yield enough times for the read loop / request coroutine to advance."""
    for _ in range(10):
        await asyncio.sleep(0)


def _feed(reader: asyncio.StreamReader, msg: dict) -> None:
    reader.feed_data((json.dumps(msg) + "\n").encode())


class TestRequestResponse:
    @pytest.mark.asyncio
    async def test_request_response_correlation(self):
        client, reader, writer = make_client()
        task = asyncio.create_task(client.request("ping", {"x": 1}))
        await _settle()

        frames = writer.frames()
        assert len(frames) == 1
        assert frames[0]["method"] == "ping"
        assert frames[0]["params"] == {"x": 1}
        rid = frames[0]["id"]

        _feed(reader, {"id": rid, "result": {"pong": True}})
        result = await asyncio.wait_for(task, timeout=1)
        assert result == {"pong": True}
        await client.close()

    @pytest.mark.asyncio
    async def test_concurrent_requests_resolve_out_of_order(self):
        client, reader, writer = make_client()
        t1 = asyncio.create_task(client.request("a"))
        t2 = asyncio.create_task(client.request("b"))
        await _settle()

        frames = writer.frames()
        assert len(frames) == 2
        ids = {f["method"]: f["id"] for f in frames}
        assert ids["a"] != ids["b"]

        # Respond to the second request first.
        _feed(reader, {"id": ids["b"], "result": "B"})
        _feed(reader, {"id": ids["a"], "result": "A"})
        assert await asyncio.wait_for(t1, timeout=1) == "A"
        assert await asyncio.wait_for(t2, timeout=1) == "B"
        await client.close()

    @pytest.mark.asyncio
    async def test_error_frame_raises(self):
        client, reader, writer = make_client()
        task = asyncio.create_task(client.request("boom"))
        await _settle()
        rid = writer.frames()[0]["id"]

        _feed(reader, {"id": rid, "error": {"code": -32000, "message": "kaboom"}})
        with pytest.raises(CodexAppServerError, match="kaboom") as exc:
            await asyncio.wait_for(task, timeout=1)
        assert exc.value.code == -32000
        await client.close()

    @pytest.mark.asyncio
    async def test_request_timeout(self):
        client, _reader, _writer = make_client()
        with pytest.raises(CodexAppServerError, match="timed out"):
            await client.request("slow", timeout=0.05)
        await client.close()

    @pytest.mark.asyncio
    async def test_request_on_closed_client_raises(self):
        client, _reader, _writer = make_client()
        await client.close()
        with pytest.raises(CodexAppServerError, match="closed"):
            await client.request("nope")

    @pytest.mark.asyncio
    async def test_initialize_sends_client_info(self):
        client, reader, writer = make_client()
        task = asyncio.create_task(client.initialize(name="pinkybot", version="9"))
        await _settle()
        frame = writer.frames()[0]
        assert frame["method"] == "initialize"
        assert frame["params"] == {
            "clientInfo": {"name": "pinkybot", "version": "9"},
            "capabilities": {},
        }

        _feed(reader, {"id": frame["id"], "result": {"codexHome": "/x"}})
        assert await asyncio.wait_for(task, timeout=1) == {"codexHome": "/x"}
        await client.close()


class TestNotifications:
    @pytest.mark.asyncio
    async def test_notification_dispatched_to_handler(self):
        seen: list[tuple[str, dict]] = []

        async def handler(method: str, params: dict) -> None:
            seen.append((method, params))

        client, reader, _writer = make_client(notification_handler=handler)
        _feed(reader, {"method": "turn/started", "params": {"thread_id": "t1"}})
        await _settle()
        assert seen == [("turn/started", {"thread_id": "t1"})]
        await client.close()

    @pytest.mark.asyncio
    async def test_notification_without_handler_is_ignored(self):
        client, reader, _writer = make_client()
        _feed(reader, {"method": "turn/started", "params": {}})
        await _settle()  # must not raise
        await client.close()

    @pytest.mark.asyncio
    async def test_outbound_notify_has_no_id(self):
        client, _reader, writer = make_client()
        await client.notify("client/event", {"k": "v"})
        frame = writer.frames()[0]
        assert "id" not in frame
        assert frame == {"method": "client/event", "params": {"k": "v"}}
        await client.close()


class TestServerRequests:
    @pytest.mark.asyncio
    async def test_server_request_routed_to_handler_and_answered(self):
        async def handler(method: str, params: dict) -> dict:
            assert method == "execCommandApproval"
            return {"decision": "approved"}

        client, reader, writer = make_client(server_request_handler=handler)
        _feed(reader, {"id": 42, "method": "execCommandApproval", "params": {"cmd": "ls"}})
        await _settle()

        responses = [f for f in writer.frames() if f.get("id") == 42]
        assert responses == [{"id": 42, "result": {"decision": "approved"}}]
        await client.close()

    @pytest.mark.asyncio
    async def test_unhandled_server_request_gets_error_response(self):
        client, reader, writer = make_client()  # no server_request_handler
        _feed(reader, {"id": 7, "method": "execCommandApproval", "params": {}})
        await _settle()

        resp = [f for f in writer.frames() if f.get("id") == 7][0]
        assert "error" in resp
        assert resp["error"]["code"] == -32601
        await client.close()

    @pytest.mark.asyncio
    async def test_handler_exception_becomes_error_frame(self):
        async def handler(method: str, params: dict) -> dict:
            raise RuntimeError("handler blew up")

        client, reader, writer = make_client(server_request_handler=handler)
        _feed(reader, {"id": 9, "method": "execCommandApproval", "params": {}})
        await _settle()

        resp = [f for f in writer.frames() if f.get("id") == 9][0]
        assert resp["error"]["code"] == -32000
        assert "handler blew up" in resp["error"]["message"]
        await client.close()


class TestFramingAndLifecycle:
    @pytest.mark.asyncio
    async def test_multiple_frames_in_one_chunk(self):
        seen: list[str] = []

        async def handler(method: str, params: dict) -> None:
            seen.append(method)

        client, reader, _writer = make_client(notification_handler=handler)
        chunk = (
            json.dumps({"method": "a", "params": {}})
            + "\n"
            + json.dumps({"method": "b", "params": {}})
            + "\n"
        ).encode()
        reader.feed_data(chunk)
        await _settle()
        assert seen == ["a", "b"]
        await client.close()

    @pytest.mark.asyncio
    async def test_non_json_line_is_skipped(self):
        seen: list[str] = []

        async def handler(method: str, params: dict) -> None:
            seen.append(method)

        client, reader, _writer = make_client(notification_handler=handler)
        reader.feed_data(b"not json at all\n")
        _feed(reader, {"method": "ok", "params": {}})
        await _settle()
        assert seen == ["ok"]  # bad line skipped, good one still dispatched
        await client.close()

    @pytest.mark.asyncio
    async def test_eof_fails_pending_requests(self):
        client, reader, writer = make_client()
        task = asyncio.create_task(client.request("hang"))
        await _settle()
        assert writer.frames()  # request was sent

        reader.feed_eof()
        with pytest.raises(CodexAppServerError, match="connection closed"):
            await asyncio.wait_for(task, timeout=1)
        await client.close()

    @pytest.mark.asyncio
    async def test_eof_signals_transport_closed_after_requests_are_empty(self):
        closed: list[CodexAppServerError] = []
        client, reader, _writer = make_client()
        client.set_transport_closed_handler(closed.append)

        reader.feed_eof()
        await _settle()

        assert len(closed) == 1
        assert str(closed[0]) == "connection closed"
        await client.close()
        assert len(closed) == 1

    @pytest.mark.asyncio
    async def test_deliberate_close_does_not_signal_transport_failure(self):
        closed: list[CodexAppServerError] = []
        client, _reader, _writer = make_client()
        client.set_transport_closed_handler(closed.append)

        await client.close()

        assert closed == []

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        client, _reader, writer = make_client()
        await client.close()
        await client.close()  # must not raise
        assert writer.closed


class TestStderrDrain:
    @pytest.mark.asyncio
    async def test_stderr_is_drained_and_logged(self):
        """stderr lines must be consumed so a long-lived child never blocks."""
        logged: list[str] = []
        reader = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        writer = FakeWriter()
        client = CodexAppServerClient(
            reader, writer, stderr=stderr, log=logged.append
        )
        client.start()

        stderr.feed_data(b"warning: something\n")
        stderr.feed_data(b"\n")  # blank line ignored
        stderr.feed_data(b"another line\n")
        await _settle()

        drained = [m for m in logged if "[stderr]" in m]
        assert any("warning: something" in m for m in drained)
        assert any("another line" in m for m in drained)
        await client.close()

    @pytest.mark.asyncio
    async def test_stderr_task_stops_on_eof(self):
        reader = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        client = CodexAppServerClient(reader, FakeWriter(), stderr=stderr)
        client.start()
        assert client._stderr_task is not None

        stderr.feed_eof()
        await _settle()
        assert client._stderr_task.done()
        await client.close()

    @pytest.mark.asyncio
    async def test_close_cancels_stderr_task(self):
        reader = asyncio.StreamReader()
        stderr = asyncio.StreamReader()  # never fed → drain stays blocked
        client = CodexAppServerClient(reader, FakeWriter(), stderr=stderr)
        client.start()
        task = client._stderr_task
        assert task is not None
        await client.close()
        assert task.done()
        assert client._stderr_task is None
