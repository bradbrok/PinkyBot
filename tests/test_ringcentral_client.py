"""Unit tests for RingCentral JWT transport and the signed HTTP bridge."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from pinky_ringcentral.auth import (
    DerivedKeyAuthenticator,
    build_signed_headers,
    derive_agent_key,
)
from pinky_ringcentral.bridge import create_app
from pinky_ringcentral.client import RingCentralAPIError, RingCentralClient
from pinky_ringcentral.service import SmsService
from pinky_ringcentral.store import BridgeStore, DoNotTextStore
from pinky_ringcentral.wake import MemoryWakeSink


@pytest.mark.asyncio
async def test_jwt_grant_and_sms_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/restapi/oauth/token":
            return httpx.Response(
                200, json={"access_token": "access-a", "expires_in": 3600}
            )
        return httpx.Response(
            200, json={"id": "msg-1", "messageStatus": "Queued"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingCentralClient(
        "client-id",
        "client-secret",
        "https://platform.ringcentral.com",
        "geordi-jwt",
        http=http,
    )
    result = await client.send_sms(
        from_phone="+19254714225",
        to_phone="+19255550123",
        text="hello",
    )
    assert result["id"] == "msg-1"
    token_request, send_request = seen
    assert token_request.url.path == "/restapi/oauth/token"
    assert dict(httpx.QueryParams(token_request.content.decode())) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": "geordi-jwt",
    }
    assert token_request.headers["authorization"] == (
        "Basic " + base64.b64encode(b"client-id:client-secret").decode()
    )
    assert send_request.headers["authorization"] == "Bearer access-a"
    assert json.loads(send_request.content) == {
        "from": {"phoneNumber": "+19254714225"},
        "to": [{"phoneNumber": "+19255550123"}],
        "text": "hello",
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_one_401_refreshes_token_and_retries_once() -> None:
    token_count = 0
    message_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_count, message_count
        if request.url.path == "/restapi/oauth/token":
            token_count += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"access-{token_count}",
                    "expires_in": 3600,
                },
            )
        message_count += 1
        if message_count == 1:
            return httpx.Response(401, json={"message": "expired"})
        assert request.headers["authorization"] == "Bearer access-2"
        return httpx.Response(200, json={"id": "msg-1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingCentralClient(
        "client-id", "client-secret", "https://rc.example", "jwt", http=http
    )
    assert (await client.get_message("msg-1"))["id"] == "msg-1"
    assert (token_count, message_count) == (2, 2)
    await http.aclose()


@pytest.mark.asyncio
async def test_api_error_redacts_every_credential_and_token() -> None:
    secret_values = {
        "client-id",
        "client-secret",
        "geordi-jwt",
        "access-secret",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/restapi/oauth/token":
            return httpx.Response(
                200, json={"access_token": "access-secret", "expires_in": 3600}
            )
        return httpx.Response(
            400,
            json={
                "message": "client-id client-secret geordi-jwt access-secret"
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingCentralClient(
        "client-id",
        "client-secret",
        "https://rc.example",
        "geordi-jwt",
        http=http,
    )
    with pytest.raises(RingCentralAPIError) as caught:
        await client.get_message("msg-1")
    assert all(value not in str(caught.value) for value in secret_values)
    assert "[redacted]" in str(caught.value)
    await http.aclose()


@pytest.mark.asyncio
async def test_post_transport_error_is_marked_may_have_completed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/restapi/oauth/token":
            return httpx.Response(200, json={"access_token": "a"})
        raise httpx.ReadError("response was lost", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingCentralClient("id", "secret", "https://rc.example", "jwt", http=http)
    with pytest.raises(RingCentralAPIError) as caught:
        await client.send_sms(
            from_phone="+19254714225",
            to_phone="+19255550123",
            text="hello",
        )
    assert caught.value.may_have_completed is True
    await http.aclose()


@pytest.mark.asyncio
async def test_message_history_query_uses_general_store_filters() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/restapi/oauth/token":
            return httpx.Response(200, json={"access_token": "a"})
        seen.update(dict(request.url.params))
        return httpx.Response(
            200, json={"records": [], "paging": {"totalPages": 1}}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RingCentralClient("id", "secret", "https://rc.example", "jwt", http=http)
    await client.list_messages(
        phone_number="+19255550123",
        date_from="2026-07-01T00:00:00Z",
        direction="Inbound",
        page=2,
        per_page=50,
    )
    assert seen == {
        "messageType": "SMS",
        "availability": "Alive",
        "page": "2",
        "perPage": "50",
        "phoneNumber": "+19255550123",
        "dateFrom": "2026-07-01T00:00:00Z",
        "direction": "Inbound",
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_bridge_rejects_unsigned_and_accepts_derived_key(
    tmp_path: Path,
) -> None:
    dnt_path = tmp_path / "do_not_text.json"
    dnt_path.write_text('{"version":1,"entries":{}}', encoding="utf-8")
    dnt_path.chmod(0o600)

    class FakeClient:
        configured = True

        async def send_sms(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": "msg-1", "messageStatus": "Queued"}

        async def aclose(self) -> None:
            return None

    master = "master-secret"
    instance = "daemon-generation"
    service = SmsService(
        FakeClient(),
        DoNotTextStore(dnt_path),
        BridgeStore(tmp_path / "bridge.sqlite3"),
        MemoryWakeSink(),
        now=lambda: datetime(2026, 7, 25, 19, 0, tzinfo=UTC),
    )
    app = create_app(
        service,
        DerivedKeyAuthenticator(master, allowed_agents={"geordi"}),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://bridge"
    ) as http:
        unsigned = await http.post(
            "/v1/sms/send",
            json={"to": "+19255550123", "text": "hello"},
        )
        assert unsigned.status_code == 401
        path = "/v1/sms/send"
        key = derive_agent_key(master, "geordi", instance)
        headers = build_signed_headers(
            key,
            agent_name="geordi",
            instance_id=instance,
            method="POST",
            path=path,
        )
        signed = await http.post(
            path,
            headers=headers,
            json={"to": "+19255550123", "text": "hello"},
        )
        assert signed.status_code == 200
        assert signed.json()["message_id"] == "msg-1"
    service.store.close()
