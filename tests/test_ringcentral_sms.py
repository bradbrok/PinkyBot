"""Unit coverage for the #447 RingCentral SMS bridge and MCP primitive."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from pinky_daemon.auth import verify_internal_request
from pinky_ringcentral.auth import (
    DerivedKeyAuthenticator,
    build_signed_headers,
    derive_agent_key,
)
from pinky_ringcentral.inbound import (
    INBOUND_FILTER,
    OUTBOUND_FILTER,
    RingCentralInboundWorker,
    _safe_error,
    _websocket_uri,
)
from pinky_ringcentral.mcp_client import RingCentralBridgeClient
from pinky_ringcentral.server import create_server
from pinky_ringcentral.service import (
    ComplianceStateUnavailableError,
    DoNotTextBlockedError,
    QuietHoursBlockedError,
    SmsService,
    SmsValidationError,
    classify_delivery_dnt,
)
from pinky_ringcentral.store import BridgeStore, DoNotTextStore
from pinky_ringcentral.wake import DaemonFerryWakeSink, MemoryWakeSink

NOW = datetime(2026, 7, 25, 19, 0, tzinfo=UTC)  # noon Pacific


class FakeRingCentralClient:
    configured = True

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.send_result: dict[str, Any] = {
            "id": "msg-queued-1",
            "messageStatus": "Queued",
            "deliveryErrorCode": "",
        }
        self.status_result: dict[str, Any] = {}
        self.pages: dict[int, dict[str, Any]] = {
            1: {"records": [], "paging": {"totalPages": 1}}
        }
        self.on_send: Any | None = None
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def send_sms(
        self, *, from_phone: str, to_phone: str, text: str
    ) -> dict[str, Any]:
        self.sent.append(
            {"from_phone": from_phone, "to_phone": to_phone, "text": text}
        )
        if self.on_send is not None:
            self.on_send()
        return dict(self.send_result)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        return {"id": message_id, **self.status_result}

    async def list_messages(self, **kwargs: Any) -> dict[str, Any]:
        return self.pages.get(
            int(kwargs.get("page", 1)),
            {"records": [], "paging": {"totalPages": 1}},
        )

    async def get_websocket_token(self) -> dict[str, Any]:
        return {
            "uri": "wss://example.invalid/ws",
            "ws_access_token": "single-use-token",
        }


class FailingWakeSink:
    async def send_event(self, kind: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("wake unavailable")


def initialize_dnt(path: Path) -> None:
    path.write_text('{"version":1,"entries":{}}\n', encoding="utf-8")
    path.chmod(0o600)


def make_service(
    tmp_path: Path,
    *,
    wake: Any | None = None,
    now: datetime = NOW,
    initialize: bool = True,
) -> tuple[SmsService, FakeRingCentralClient, DoNotTextStore, BridgeStore, Any]:
    dnt_path = tmp_path / "state" / "do_not_text.json"
    dnt_path.parent.mkdir(parents=True)
    if initialize:
        initialize_dnt(dnt_path)
    dnt = DoNotTextStore(dnt_path)
    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")
    client = FakeRingCentralClient()
    sink = wake or MemoryWakeSink()
    service = SmsService(client, dnt, store, sink, now=lambda: now)
    return service, client, dnt, store, sink


@pytest.mark.asyncio
async def test_send_fails_closed_when_dnt_file_is_absent(tmp_path: Path) -> None:
    service, client, _dnt, store, _wake = make_service(
        tmp_path, initialize=False
    )
    with pytest.raises(ComplianceStateUnavailableError) as caught:
        await service.send_sms(to="+19255550123", text="hello")
    assert client.sent == []
    attempt = store.get_attempt(caught.value.details["attempt_id"])
    assert attempt and attempt["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_send_fails_closed_when_dnt_file_is_malformed(tmp_path: Path) -> None:
    service, client, dnt, _store, _wake = make_service(tmp_path)
    dnt.path.write_text("{bad json", encoding="utf-8")
    with pytest.raises(ComplianceStateUnavailableError):
        await service.send_sms(to="+19255550123", text="hello")
    assert client.sent == []


@pytest.mark.asyncio
async def test_dnt_is_checked_at_transmission_time(tmp_path: Path) -> None:
    service, client, dnt, store, _wake = make_service(tmp_path)
    dnt.add("+19255550123", reason="opt_out", source="test")
    with pytest.raises(DoNotTextBlockedError) as caught:
        await service.send_sms(to="(925) 555-0123", text="composed earlier")
    assert client.sent == []
    assert store.get_attempt(caught.value.details["attempt_id"])["outcome"] == (
        "rejected"
    )


@pytest.mark.asyncio
async def test_legacy_formatted_dnt_key_still_blocks(tmp_path: Path) -> None:
    service, client, dnt, _store, _wake = make_service(tmp_path)
    dnt.path.write_text(
        json.dumps({"(925) 555-0123": {"reason": "legacy opt out"}}),
        encoding="utf-8",
    )
    with pytest.raises(DoNotTextBlockedError):
        await service.send_sms(to="+19255550123", text="must not send")
    assert client.sent == []


def test_opt_out_reason_is_never_downgraded(tmp_path: Path) -> None:
    path = tmp_path / "do_not_text.json"
    initialize_dnt(path)
    dnt = DoNotTextStore(path)
    dnt.add("+19255550123", reason="opt_out", source="inbound_stop")
    entry = dnt.add(
        "+19255550123",
        reason="landline",
        source="delivery_failure",
        carrier_code="SMS-CAR-411",
        source_message_id="msg-1",
    )
    assert entry["reason"] == "opt_out"
    assert entry["carrier_code"] == "SMS-CAR-411"


@pytest.mark.asyncio
async def test_stop_wins_race_before_send_linearization(tmp_path: Path) -> None:
    service, client, _dnt, _store, wake = make_service(tmp_path)
    await service._compliance_lock.acquire()
    stop = asyncio.create_task(
        service.handle_inbound(
            {
                "id": "stop-1",
                "type": "SMS",
                "direction": "Inbound",
                "from": {"phoneNumber": "+19255550123"},
                "subject": "STOP",
                "creationTime": "2026-07-25T19:00:00Z",
            }
        )
    )
    await asyncio.sleep(0)
    send = asyncio.create_task(
        service.send_sms(to="+19255550123", text="already composed")
    )
    await asyncio.sleep(0)
    service._compliance_lock.release()
    assert await stop == "stop"
    with pytest.raises(DoNotTextBlockedError):
        await send
    assert client.sent == []
    assert wake.events == []


@pytest.mark.asyncio
async def test_quiet_hours_reject_with_next_allowed_at(tmp_path: Path) -> None:
    before_open = datetime(2026, 7, 25, 14, 59, tzinfo=UTC)
    service, client, _dnt, _store, _wake = make_service(
        tmp_path, now=before_open
    )
    with pytest.raises(QuietHoursBlockedError) as caught:
        await service.send_sms(to="+19255550123", text="too early")
    assert caught.value.details == {
        "recipient_timezone": "America/Los_Angeles",
        "next_allowed_at": "2026-07-25T15:00:00Z",
        "attempt_id": caught.value.details["attempt_id"],
    }
    assert client.sent == []


@pytest.mark.asyncio
async def test_invalid_timezone_is_rejected_and_audited(tmp_path: Path) -> None:
    service, client, _dnt, store, _wake = make_service(tmp_path)
    with pytest.raises(SmsValidationError) as caught:
        await service.send_sms(
            to="+19255550123",
            text="hello",
            recipient_timezone="Not/A_Zone",
        )
    attempt_id = caught.value.details["attempt_id"]
    assert store.get_attempt(attempt_id)["outcome"] == "rejected"
    assert client.sent == []


@pytest.mark.asyncio
async def test_send_uses_fixed_geordi_sender_and_audits_result(
    tmp_path: Path,
) -> None:
    service, client, _dnt, store, _wake = make_service(tmp_path)
    result = await service.send_sms(
        to="925-555-0123",
        text="This is Geordi.",
        template_id="BM-SMS-T24",
    )
    assert client.sent == [
        {
            "from_phone": "+19254714225",
            "to_phone": "+19255550123",
            "text": "This is Geordi.",
        }
    ]
    assert result["message_status"] == "Queued"
    attempt = store.get_attempt(result["attempt_id"])
    assert attempt and attempt["outcome"] == "transmitted"
    assert attempt["template_id"] == "BM-SMS-T24"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("stop", True),
        ("UNSUBSCRIBE", True),
        ("not stop", False),
    ],
)
async def test_inbound_stop_taxonomy(
    tmp_path: Path, keyword: str, expected: bool
) -> None:
    service, _client, dnt, _store, wake = make_service(tmp_path)
    classification = await service.handle_inbound(
        {
            "id": f"in-{keyword}",
            "from": {"phoneNumber": "+19255550123"},
            "subject": keyword,
            "creationTime": "2026-07-25T19:00:00Z",
        }
    )
    assert (classification == "stop") is expected
    entry = dnt.get("+19255550123")
    assert (entry is not None) is expected
    if expected:
        assert entry == {
            "reason": "opt_out",
            "source": "inbound_stop",
            "added_at": entry["added_at"],
            "source_message_id": f"in-{keyword}",
            "carrier_code": "",
            "triggered_at": "2026-07-25T19:00:00Z",
        }
        assert wake.events == []
    else:
        assert wake.events[0][0] == "ringcentral.sms.inbound"


@pytest.mark.asyncio
async def test_inbound_threading_hint_and_duplicate_dedupe(tmp_path: Path) -> None:
    service, _client, _dnt, _store, wake = make_service(tmp_path)
    await service.send_sms(to="+19255550123", text="outbound")
    inbound = {
        "id": "reply-1",
        "from": {"phoneNumber": "+19255550123"},
        "subject": "Can we reschedule?",
        "creationTime": "2026-07-25T19:01:00Z",
    }
    assert await service.handle_inbound(inbound) == "threaded_reply"
    assert await service.handle_inbound(inbound) == "threaded_reply"
    inbound_events = [
        event for event in wake.events if event[0] == "ringcentral.sms.inbound"
    ]
    assert len(inbound_events) == 1
    assert inbound_events[0][1]["threading_hint"]["has_recent_outbound"] is True
    assert inbound_events[0][1]["message"] == inbound


@pytest.mark.asyncio
async def test_failed_wake_remains_durable_for_retry(tmp_path: Path) -> None:
    service, _client, _dnt, store, _wake = make_service(
        tmp_path, wake=FailingWakeSink()
    )
    await service.handle_inbound(
        {
            "id": "reply-1",
            "from": {"phoneNumber": "+19255550123"},
            "subject": "hello",
        }
    )
    assert [item["message_id"] for item in store.pending_inbound()] == ["reply-1"]
    memory = MemoryWakeSink()
    service.wake_sink = memory
    assert await service.deliver_pending_inbound() == 1
    assert store.pending_inbound() == []


@pytest.mark.asyncio
async def test_history_auto_paginates_and_preserves_general_records(
    tmp_path: Path,
) -> None:
    service, client, _dnt, _store, _wake = make_service(tmp_path)
    later = {"id": "2", "creationTime": "2026-07-25T19:02:00Z", "faxPageCount": 0}
    earlier = {
        "id": "1",
        "creationTime": "2026-07-25T19:01:00Z",
        "subject": "full payload",
    }
    client.pages = {
        1: {"records": [later], "paging": {"totalPages": 2}},
        2: {"records": [earlier], "paging": {"totalPages": 2}},
    }
    result = await service.get_sms_history(
        phone_number="+19255550123", limit=10, days=90
    )
    assert result["records"] == [earlier, later]


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("SMS-RC-413", "opt_out"),
        ("SMS-UP-410", "landline"),
        ("SMS-CAR-400", "landline"),
        ("SMS-CAR-411", "landline"),
        ("SMS-RC-500", ""),
    ],
)
def test_delivery_error_dnt_mapping(code: str, reason: str) -> None:
    assert classify_delivery_dnt(code) == reason


@pytest.mark.asyncio
async def test_delivery_failure_adds_evidence_and_wakes(tmp_path: Path) -> None:
    service, client, dnt, _store, wake = make_service(tmp_path)
    client.status_result = {
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-CAR-411",
        "to": [{"phoneNumber": "+19255550123"}],
        "lastModifiedTime": "2026-07-25T19:02:00Z",
    }
    result = await service.get_sms_status(message_id="msg-1")
    assert result["delivery_error_code"] == "SMS-CAR-411"
    assert dnt.get("+19255550123")["source_message_id"] == "msg-1"
    assert dnt.get("+19255550123")["carrier_code"] == "SMS-CAR-411"
    assert wake.events[0][0] == "ringcentral.sms.delivery_failed"


@pytest.mark.asyncio
async def test_post_transmit_dnt_outage_does_not_invite_duplicate_retry(
    tmp_path: Path,
) -> None:
    service, client, dnt, store, _wake = make_service(tmp_path)
    client.send_result = {
        "id": "failed-at-send",
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-RC-413",
    }
    client.on_send = lambda: dnt.path.write_text("{broken", encoding="utf-8")
    result = await service.send_sms(to="+19255550123", text="one attempt")
    assert result["message_id"] == "failed-at-send"
    assert len(client.sent) == 1
    assert store.pending_status_ids() == ["failed-at-send"]


def test_derived_key_auth_is_instance_bound() -> None:
    master = "master-secret"
    key = derive_agent_key(master, "geordi", "generation-a")
    headers = build_signed_headers(
        key,
        agent_name="geordi",
        instance_id="generation-a",
        method="POST",
        path="/v1/sms/send?ignored=true",
        timestamp=1000,
    )
    auth = DerivedKeyAuthenticator(
        master, allowed_agents={"geordi"}, clock=lambda: 1000
    )
    assert auth.authenticate(
        method="POST", path="/v1/sms/send", headers=headers
    )
    headers["x-pinky-instance"] = "generation-b"
    assert (
        auth.authenticate(method="POST", path="/v1/sms/send", headers=headers)
        is None
    )


def test_mcp_write_transport_error_is_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail(request: urllib.request.Request, timeout: float):
        calls.append(request.full_url)
        raise urllib.error.URLError("lost response")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    client = RingCentralBridgeClient(
        agent_name="geordi",
        secret="derived",
        instance_id="generation-a",
    )
    result = client.post("/v1/sms/send", {"to": "+19255550123", "text": "hi"})
    assert len(calls) == 1
    assert result["may_have_completed"] is True


def test_mcp_surface_contains_only_p1_sms_primitives() -> None:
    server = create_server(
        agent_name="geordi",
        secret="derived",
        instance_id="generation-a",
        bridge_url="http://bridge.invalid",
    )
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert names == {
        "send_sms",
        "get_sms_history",
        "get_sms_status",
        "add_do_not_text",
        "remove_do_not_text",
        "list_do_not_text",
    }
    assert not any("call" in name or "ringsense" in name for name in names)


@pytest.mark.asyncio
async def test_daemon_ferry_wake_is_signed_and_contains_full_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"sent": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = DaemonFerryWakeSink(
        api_url="http://daemon:8888",
        session_secret="session-secret",
        http=http,
    )
    payload = {"message": {"id": "in-1", "subject": "hello"}}
    await sink.send_event("ringcentral.sms.inbound", payload)
    request = seen["request"]
    assert verify_internal_request(
        "session-secret",
        agent_name="barsik",
        method="POST",
        path="/agents/barsik/mesh/send",
        timestamp=request.headers["x-pinky-timestamp"],
        signature=request.headers["x-pinky-signature"],
    )
    assert seen["json"]["target"] == "geordi@pi"
    assert seen["json"]["kind"] == "msg"
    assert seen["json"]["body"]["event"] == payload
    await http.aclose()


@pytest.mark.asyncio
async def test_polling_fallback_processes_inbound_and_advances_cursor(
    tmp_path: Path,
) -> None:
    service, client, dnt, store, _wake = make_service(tmp_path)
    client.pages = {
        1: {
            "records": [
                {
                    "id": "stop-polled",
                    "from": {"phoneNumber": "+19255550123"},
                    "subject": "STOP",
                    "creationTime": "2026-07-25T19:01:00Z",
                }
            ],
            "paging": {"totalPages": 1},
        }
    }
    worker = RingCentralInboundWorker(service, now=lambda: NOW)
    assert await worker.poll_once() == 1
    assert dnt.get("+19255550123")["reason"] == "opt_out"
    assert store.get_metadata("ringcentral_inbound_poll_cursor") == (
        "2026-07-25T19:01:00Z"
    )


def test_ws_subscription_is_outbound_only_and_has_no_webhook() -> None:
    request = RingCentralInboundWorker._subscription_request()
    filters = request[1]["eventFilters"]
    assert filters == [INBOUND_FILTER, OUTBOUND_FILTER]
    assert request[1]["deliveryMode"] == {"transportType": "WebSocket"}
    assert "webhook" not in json.dumps(request).lower()
    assert RingCentralInboundWorker._subscription_accepted(
        json.dumps([{"type": "ClientResponse", "status": 200}, {}])
    )
    assert not RingCentralInboundWorker._subscription_accepted(
        json.dumps([{"type": "ClientResponse", "status": 403}, {}])
    )


def test_ws_single_use_token_is_appended_and_errors_redact_it() -> None:
    uri = _websocket_uri(
        "wss://example.ringcentral.com/ws", "single-use-secret"
    )
    assert uri == (
        "wss://example.ringcentral.com/ws?access_token=single-use-secret"
    )
    safe = _safe_error(RuntimeError(f"failed at {uri}&wsc=recovery-secret"))
    assert "single-use-secret" not in safe
    assert "recovery-secret" not in safe


@pytest.mark.asyncio
async def test_ws_server_notification_uses_two_part_protocol(
    tmp_path: Path,
) -> None:
    service, _client, _dnt, _store, wake = make_service(tmp_path)
    worker = RingCentralInboundWorker(service)

    class FakeWebSocket:
        sent: list[str] = []

        async def send(self, value: str) -> None:
            self.sent.append(value)

    message = {
        "id": "ws-in-1",
        "from": {"phoneNumber": "+19255550123"},
        "subject": "hello from websocket",
    }
    frame = json.dumps(
        [
            {"type": "ServerNotification", "messageId": "notification-1"},
            {"event": INBOUND_FILTER, "body": message},
        ]
    )
    await worker._handle_frame(FakeWebSocket(), frame)
    assert wake.events[0][1]["message"] == message
