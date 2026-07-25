"""Unit coverage for the #447 RingCentral SMS bridge and MCP primitive."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
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
from pinky_ringcentral.client import RingCentralAPIError
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
    AuditUnavailableError,
    ComplianceStateUnavailableError,
    DoNotTextBlockedError,
    QuietHoursBlockedError,
    SmsIntegrityError,
    SmsNotFoundError,
    SmsService,
    SmsValidationError,
    classify_delivery_dnt,
)
from pinky_ringcentral.store import (
    BridgeStore,
    DoNotTextStore,
    DoNotTextUnavailableError,
)
from pinky_ringcentral.wake import DaemonFerryWakeSink, MemoryWakeSink

NOW = datetime(2026, 7, 25, 19, 0, tzinfo=UTC)  # noon Pacific


class FakeRingCentralClient:
    configured = True

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.status_requests: list[str] = []
        self.send_result: dict[str, Any] = {
            "id": "msg-queued-1",
            "messageStatus": "Queued",
            "deliveryErrorCode": "",
        }
        self.status_result: dict[str, Any] = {}
        self.status_results: dict[str, dict[str, Any]] = {}
        self.status_errors: dict[str, Exception] = {}
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
        self.status_requests.append(message_id)
        error = self.status_errors.get(message_id)
        if error is not None:
            raise error
        return {
            "id": message_id,
            "type": "SMS",
            "direction": "Outbound",
            "from": {"phoneNumber": "+19254714225"},
            "to": [{"phoneNumber": "+19255550123"}],
            **self.status_results.get(message_id, self.status_result),
        }

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


def test_dnt_commit_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state" / "do_not_text.json"
    path.parent.mkdir(parents=True)
    initialize_dnt(path)
    dnt = DoNotTextStore(path)
    real_fsync = os.fsync
    fsynced_types: list[str] = []

    def tracking_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        fsynced_types.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    dnt.add("+19255550123", reason="opt_out", source="test")
    assert fsynced_types == ["file", "directory"]


def test_consumed_write_request_id_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    store = BridgeStore(path)
    assert store.consume_request_id(
        "request-durable-0001",
        agent_name="geordi",
        method="POST",
        target="/v1/sms/send",
    )
    store.close()
    restarted = BridgeStore(path)
    assert not restarted.consume_request_id(
        "request-durable-0001",
        agent_name="geordi",
        method="POST",
        target="/v1/sms/send",
    )


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


def test_stale_evidence_cannot_override_newer_manual_removal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "do_not_text.json"
    initialize_dnt(path)
    dnt = DoNotTextStore(path)
    dnt.add(
        "+19255550123",
        reason="opt_out",
        source="inbound_stop",
        source_message_id="stop-stale",
        triggered_at="2026-07-25T19:00:00Z",
    )
    assert dnt.remove(
        "+19255550123", removed_at="2026-07-25T19:01:00Z"
    )
    replay = dnt.add(
        "+19255550123",
        reason="opt_out",
        source="inbound_stop",
        source_message_id="stop-stale",
        triggered_at="2026-07-25T19:00:00Z",
    )
    assert replay["suppressed"] is True
    assert dnt.get("+19255550123") is None


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
                "to": [{"phoneNumber": "+19254714225"}],
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
@pytest.mark.parametrize("recipient_timezone", ["Not/A_Zone", "../UTC"])
async def test_invalid_timezone_is_rejected_and_audited(
    tmp_path: Path,
    recipient_timezone: str,
) -> None:
    service, client, _dnt, store, _wake = make_service(tmp_path)
    with pytest.raises(SmsValidationError) as caught:
        await service.send_sms(
            to="+19255550123",
            text="hello",
            recipient_timezone=recipient_timezone,
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
async def test_missing_id_send_response_is_uncertain_and_has_no_side_effects(
    tmp_path: Path,
) -> None:
    service, client, dnt, store, wake = make_service(tmp_path)
    client.send_result = {
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-RC-413",
    }
    with pytest.raises(RingCentralAPIError) as caught:
        await service.send_sms(to="+19255550123", text="one attempt")
    assert caught.value.may_have_completed is True
    assert dnt.list() == []
    assert wake.events == []
    assert store.pending_status_ids() == []


@pytest.mark.asyncio
async def test_post_dispatch_side_effect_staging_failure_is_sticky(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client, _dnt, store, _wake = make_service(tmp_path)
    client.send_result = {
        "id": "msg-staging-failure",
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-RC-413",
    }

    def fail_staging(*args: Any, **kwargs: Any) -> bool:
        raise OSError("side-effect ledger unavailable")

    monkeypatch.setattr(store, "side_effects_required", fail_staging)
    with pytest.raises(AuditUnavailableError) as caught:
        await service.send_sms(to="+19255550123", text="one attempt")
    assert caught.value.details["may_have_completed"] is True
    assert caught.value.details["message_id"] == "msg-staging-failure"
    attempt = store.get_attempt(caught.value.details["attempt_id"])
    assert attempt["outcome"] == "transmitted"
    assert attempt["message_id"] == "msg-staging-failure"


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
    message_id = f"in-{keyword.replace(' ', '-')}"
    classification = await service.handle_inbound(
        {
            "id": message_id,
            "type": "SMS",
            "direction": "Inbound",
            "from": {"phoneNumber": "+19255550123"},
            "to": [{"phoneNumber": "+19254714225"}],
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
            "source_message_id": message_id,
            "carrier_code": "",
            "triggered_at": "2026-07-25T19:00:00Z",
        }
        assert wake.events == []
    else:
        assert wake.events[0][0] == "ringcentral.sms.inbound"


@pytest.mark.asyncio
async def test_other_line_stop_is_quarantined_without_side_effects(
    tmp_path: Path,
) -> None:
    service, _client, dnt, store, wake = make_service(tmp_path)
    classification = await service.handle_inbound(
        {
            "id": "other-line-stop",
            "type": "SMS",
            "direction": "Inbound",
            "from": {"phoneNumber": "+19255550123"},
            "to": [{"phoneNumber": "+14155550100"}],
            "subject": "STOP",
        }
    )
    assert classification == "ignored"
    assert dnt.list() == []
    assert wake.events == []
    assert "to" in store.inbound_rejections("other-line-stop")[-1]["error"]


@pytest.mark.asyncio
async def test_wrong_type_inbound_is_quarantined_without_side_effects(
    tmp_path: Path,
) -> None:
    service, _client, dnt, store, wake = make_service(tmp_path)
    classification = await service.handle_inbound(
        {
            "id": "voicemail-stop",
            "type": "VoiceMail",
            "direction": "Inbound",
            "from": {"phoneNumber": "+19255550123"},
            "to": [{"phoneNumber": "+19254714225"}],
            "subject": "STOP",
        }
    )
    assert classification == "ignored"
    assert dnt.list() == []
    assert wake.events == []
    assert "type" in store.inbound_rejections("voicemail-stop")[-1]["error"]


@pytest.mark.asyncio
async def test_inbound_threading_hint_and_duplicate_dedupe(tmp_path: Path) -> None:
    service, _client, _dnt, _store, wake = make_service(tmp_path)
    await service.send_sms(to="+19255550123", text="outbound")
    inbound = {
        "id": "reply-1",
        "type": "SMS",
        "direction": "Inbound",
        "from": {"phoneNumber": "+19255550123"},
        "to": [{"phoneNumber": "+19254714225"}],
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
async def test_duplicate_stop_cannot_reverse_manual_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _client, dnt, _store, _wake = make_service(tmp_path)
    stop = {
        "id": "stop-replayed",
        "type": "SMS",
        "direction": "Inbound",
        "from": {"phoneNumber": "+19255550123"},
        "to": [{"phoneNumber": "+19254714225"}],
        "subject": "STOP",
        "creationTime": "2026-07-25T19:00:00Z",
    }
    add_calls = 0
    original_add = dnt.add

    def counted_add(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal add_calls
        add_calls += 1
        return original_add(*args, **kwargs)

    monkeypatch.setattr(dnt, "add", counted_add)
    assert await service.handle_inbound(stop) == "stop"
    assert dnt.get("+19255550123") is not None
    assert (
        await service.remove_do_not_text(phone_number="+19255550123")
    )["removed"] is True

    assert await service.handle_inbound(stop) == "stop"
    assert dnt.get("+19255550123") is None
    assert add_calls == 1


@pytest.mark.asyncio
async def test_failed_wake_remains_durable_for_retry(tmp_path: Path) -> None:
    service, _client, _dnt, store, _wake = make_service(
        tmp_path, wake=FailingWakeSink()
    )
    await service.handle_inbound(
        {
            "id": "reply-1",
            "type": "SMS",
            "direction": "Inbound",
            "from": {"phoneNumber": "+19255550123"},
            "to": [{"phoneNumber": "+19254714225"}],
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


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [150, 250])
async def test_history_uses_invariant_pages_without_duplicates(
    tmp_path: Path,
    limit: int,
) -> None:
    service, client, _dnt, _store, _wake = make_service(tmp_path)
    source = [
        {
            "id": f"m{index:03d}",
            "creationTime": f"scan-{index:03d}",
        }
        for index in range(1, 301)
    ]
    requested_page_sizes: list[int] = []

    async def list_messages(**kwargs: Any) -> dict[str, Any]:
        page = int(kwargs["page"])
        per_page = int(kwargs["per_page"])
        requested_page_sizes.append(per_page)
        start = (page - 1) * per_page
        return {
            "records": source[start : start + per_page],
            "paging": {
                "totalPages": (len(source) + per_page - 1) // per_page
            },
        }

    client.list_messages = list_messages  # type: ignore[method-assign]
    result = await service.get_sms_history(
        phone_number="+19255550123",
        limit=limit,
        days=90,
    )
    assert [record["id"] for record in result["records"]] == [
        f"m{index:03d}" for index in range(1, limit + 1)
    ]
    assert requested_page_sizes == [100] * ((limit + 99) // 100)


@pytest.mark.asyncio
async def test_history_freezes_snapshot_across_page_insertion(
    tmp_path: Path,
) -> None:
    service, client, _dnt, _store, _wake = make_service(tmp_path)
    live = [
        {
            "id": f"m{index:03d}",
            "creationTime": f"scan-{index:03d}",
        }
        for index in range(200, 0, -1)
    ]
    date_tos: list[str] = []

    async def list_messages(**kwargs: Any) -> dict[str, Any]:
        page = int(kwargs["page"])
        per_page = int(kwargs["per_page"])
        date_to = str(kwargs.get("date_to") or "")
        date_tos.append(date_to)
        bounded = (
            [record for record in live if record["id"] != "m201"]
            if date_to
            else live
        )
        start = (page - 1) * per_page
        records = bounded[start : start + per_page]
        if page == 1:
            live.insert(
                0,
                {"id": "m201", "creationTime": "scan-201"},
            )
        return {
            "records": records,
            "paging": {
                "totalPages": (len(bounded) + per_page - 1) // per_page
            },
        }

    client.list_messages = list_messages  # type: ignore[method-assign]
    result = await service.get_sms_history(
        phone_number="+19255550123",
        limit=200,
        days=90,
    )
    assert [record["id"] for record in result["records"]] == [
        f"m{index:03d}" for index in range(1, 201)
    ]
    assert len(date_tos) == 2
    assert date_tos[0] and date_tos[0] == date_tos[1]


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
    service, client, dnt, store, wake = make_service(tmp_path)
    client.send_result = {"id": "msg-1", "messageStatus": "Queued"}
    await service.send_sms(to="+19255550123", text="outbound")
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
    assert store.status_checks("msg-1")[-1]["outcome"] == "ok"


@pytest.mark.asyncio
async def test_stale_delivery_requery_cannot_reverse_manual_removal(
    tmp_path: Path,
) -> None:
    service, client, dnt, _store, wake = make_service(tmp_path)
    client.send_result = {"id": "msg-stale", "messageStatus": "Queued"}
    await service.send_sms(to="+19255550123", text="outbound")
    client.status_result = {
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-CAR-411",
        "to": [{"phoneNumber": "+19255550123"}],
        "lastModifiedTime": "2026-07-25T19:00:00Z",
    }
    await service.get_sms_status(message_id="msg-stale")
    assert dnt.get("+19255550123") is not None
    await service.remove_do_not_text(phone_number="+19255550123")

    await service.get_sms_status(message_id="msg-stale")
    assert dnt.get("+19255550123") is None
    assert len(wake.events) == 1


@pytest.mark.asyncio
async def test_status_rejects_unindexed_message_without_upstream_call(
    tmp_path: Path,
) -> None:
    service, client, dnt, store, wake = make_service(tmp_path)
    with pytest.raises(SmsNotFoundError):
        await service.get_sms_status(message_id="arbitrary-store-id")
    assert client.status_requests == []
    assert dnt.list() == []
    assert wake.events == []
    assert store.status_checks("arbitrary-store-id")[-1]["outcome"] == "not_found"


@pytest.mark.asyncio
async def test_status_rejects_wrong_type_without_side_effects(
    tmp_path: Path,
) -> None:
    service, client, dnt, store, wake = make_service(tmp_path)
    client.send_result = {"id": "msg-wrong-type", "messageStatus": "Queued"}
    await service.send_sms(to="+19255550123", text="outbound")
    client.status_result = {
        "type": "VoiceMail",
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-CAR-411",
        "lastModifiedTime": "2026-07-25T19:02:00Z",
    }
    with pytest.raises(SmsIntegrityError) as caught:
        await service.get_sms_status(message_id="msg-wrong-type")
    assert caught.value.details["violations"] == ["type"]
    assert dnt.list() == []
    assert wake.events == []
    assert (
        store.status_checks("msg-wrong-type")[-1]["outcome"]
        == "integrity_error"
    )


@pytest.mark.asyncio
async def test_status_rejects_wrong_origin_without_side_effects(
    tmp_path: Path,
) -> None:
    service, client, dnt, store, wake = make_service(tmp_path)
    client.send_result = {"id": "msg-wrong-origin", "messageStatus": "Queued"}
    await service.send_sms(to="+19255550123", text="outbound")
    client.status_result = {
        "from": {"phoneNumber": "+14155550100"},
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-CAR-411",
        "lastModifiedTime": "2026-07-25T19:02:00Z",
    }
    with pytest.raises(SmsIntegrityError) as caught:
        await service.get_sms_status(message_id="msg-wrong-origin")
    assert caught.value.details["violations"] == ["from"]
    assert dnt.list() == []
    assert wake.events == []
    assert (
        store.status_checks("msg-wrong-origin")[-1]["outcome"]
        == "integrity_error"
    )


@pytest.mark.asyncio
async def test_status_rejects_mismatched_response_id_without_side_effects(
    tmp_path: Path,
) -> None:
    service, client, dnt, store, wake = make_service(tmp_path)
    client.send_result = {"id": "msg-requested", "messageStatus": "Queued"}
    await service.send_sms(to="+19255550123", text="outbound")
    client.status_result = {
        "id": "msg-different",
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-CAR-411",
        "lastModifiedTime": "2026-07-25T19:02:00Z",
    }
    with pytest.raises(SmsIntegrityError) as caught:
        await service.get_sms_status(message_id="msg-requested")
    assert caught.value.details["violations"] == ["message_id"]
    assert dnt.list() == []
    assert wake.events == []
    assert (
        store.status_checks("msg-requested")[-1]["outcome"]
        == "integrity_error"
    )


@pytest.mark.asyncio
async def test_status_refresh_continues_after_poisoned_oldest_id(
    tmp_path: Path,
) -> None:
    service, client, dnt, store, _wake = make_service(tmp_path)
    client.send_result = {"id": "m1-poison", "messageStatus": "Queued"}
    await service.send_sms(to="+19255550123", text="first")
    client.send_result = {"id": "m2-terminal", "messageStatus": "Queued"}
    await service.send_sms(to="+14155550100", text="second")
    client.status_errors["m1-poison"] = RingCentralAPIError(
        "not found",
        status_code=404,
    )
    client.status_results["m2-terminal"] = {
        "to": [{"phoneNumber": "+14155550100"}],
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-RC-413",
        "lastModifiedTime": "2026-07-25T19:02:00Z",
    }

    assert await service.refresh_pending_statuses() == 1
    assert client.status_requests == ["m1-poison", "m2-terminal"]
    assert dnt.get("+14155550100")["reason"] == "opt_out"
    assert store.get_outbound("m1-poison")["status_failure_count"] == 1


@pytest.mark.asyncio
async def test_persistently_failing_status_is_quarantined_and_visible(
    tmp_path: Path,
) -> None:
    service, client, _dnt, store, _wake = make_service(tmp_path)
    client.send_result = {"id": "m-quarantine", "messageStatus": "Queued"}
    await service.send_sms(to="+19255550123", text="first")
    client.status_errors["m-quarantine"] = RingCentralAPIError(
        "not found",
        status_code=404,
    )
    current = [NOW]
    service._now = lambda: current[0]

    await service.refresh_pending_statuses()
    current[0] += timedelta(seconds=31)
    await service.refresh_pending_statuses()
    current[0] += timedelta(seconds=61)
    await service.refresh_pending_statuses()

    outbound = store.get_outbound("m-quarantine")
    assert outbound["message_status"] == "Queued"
    assert outbound["status_failure_count"] == 3
    assert outbound["status_quarantined_at"]
    assert outbound["status_last_error"]
    assert store.status_checks("m-quarantine")[-1]["outcome"] == "quarantined"
    assert store.pending_status_ids(now=current[0].isoformat()) == []

    with pytest.raises(ComplianceStateUnavailableError):
        await service.send_sms(
            to="+19255550123",
            text="must not transmit",
        )
    assert len(client.sent) == 1


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


@pytest.mark.asyncio
async def test_delivery_dnt_write_failure_quarantines_next_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client, dnt, store, _wake = make_service(tmp_path)
    client.send_result = {
        "id": "failed-dnt-write",
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-RC-413",
    }

    def fail_add(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise DoNotTextUnavailableError("disk full")

    monkeypatch.setattr(dnt, "add", fail_add)
    await service.send_sms(to="+19255550123", text="first attempt")
    assert (
        store.pending_compliance_event("+19255550123")
        == "delivery:failed-dnt-write"
    )

    with pytest.raises(ComplianceStateUnavailableError):
        await service.send_sms(to="+19255550123", text="must not transmit")
    assert len(client.sent) == 1


@pytest.mark.asyncio
async def test_stop_dnt_write_failure_quarantines_next_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client, dnt, store, _wake = make_service(tmp_path)

    def fail_add(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise DoNotTextUnavailableError("replace denied")

    monkeypatch.setattr(dnt, "add", fail_add)
    with pytest.raises(ComplianceStateUnavailableError):
        await service.handle_inbound(
            {
                "id": "stop-dnt-write-failed",
                "type": "SMS",
                "direction": "Inbound",
                "from": {"phoneNumber": "+19255550123"},
                "to": [{"phoneNumber": "+19254714225"}],
                "subject": "STOP",
                "creationTime": "2026-07-25T19:00:00Z",
            }
        )
    assert (
        store.pending_compliance_event("+19255550123")
        == "inbound:stop-dnt-write-failed"
    )

    with pytest.raises(ComplianceStateUnavailableError):
        await service.send_sms(to="+19255550123", text="must not transmit")
    assert client.sent == []


@pytest.mark.asyncio
async def test_pending_compliance_quarantine_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client, dnt, store, _wake = make_service(tmp_path)
    client.send_result = {
        "id": "restart-quarantine",
        "messageStatus": "DeliveryFailed",
        "deliveryErrorCode": "SMS-RC-413",
    }

    def fail_add(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise DoNotTextUnavailableError("disk full")

    monkeypatch.setattr(dnt, "add", fail_add)
    await service.send_sms(to="+19255550123", text="first attempt")
    store_path = store.path
    dnt_path = dnt.path
    store.close()

    restarted_client = FakeRingCentralClient()
    restarted_store = BridgeStore(store_path)
    restarted = SmsService(
        restarted_client,
        DoNotTextStore(dnt_path),
        restarted_store,
        MemoryWakeSink(),
        now=lambda: NOW,
    )
    with pytest.raises(ComplianceStateUnavailableError):
        await restarted.send_sms(
            to="+19255550123",
            text="must not transmit",
        )
    assert restarted_client.sent == []


@pytest.mark.asyncio
async def test_uncertain_rc_error_survives_audit_finalization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client, _dnt, store, _wake = make_service(tmp_path)

    async def uncertain_send(**kwargs: Any) -> dict[str, Any]:
        raise RingCentralAPIError(
            "response lost", may_have_completed=True
        )

    def fail_finish(*args: Any, **kwargs: Any) -> None:
        raise OSError("audit unavailable")

    monkeypatch.setattr(client, "send_sms", uncertain_send)
    monkeypatch.setattr(store, "finish_attempt", fail_finish)
    with pytest.raises(AuditUnavailableError) as caught:
        await service.send_sms(to="+19255550123", text="one attempt")
    assert caught.value.details["may_have_completed"] is True
    assert isinstance(caught.value.__cause__, RingCentralAPIError)
    assert caught.value.__cause__.may_have_completed is True


@pytest.mark.asyncio
async def test_cancel_after_dispatch_finalizes_uncertain_without_second_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client, _dnt, store, _wake = make_service(tmp_path)
    service._cancellation_reconcile_timeout = 0.01
    dispatched = asyncio.Event()
    writes = 0

    async def blocked_send(**kwargs: Any) -> dict[str, Any]:
        nonlocal writes
        writes += 1
        dispatched.set()
        await asyncio.Event().wait()
        return {"id": "unreachable", "messageStatus": "Queued"}

    monkeypatch.setattr(client, "send_sms", blocked_send)
    caller = asyncio.create_task(
        service.send_sms(to="+19255550123", text="one write")
    )
    await asyncio.wait_for(dispatched.wait(), timeout=1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    uncertain_id = store.uncertain_attempt_for_phone("+19255550123")
    attempt = store.get_attempt(uncertain_id)
    assert attempt["outcome"] == "uncertain"
    assert attempt["may_have_completed"] == 1
    assert attempt["completed_at"]
    assert writes == 1

    await asyncio.wait_for(service._compliance_lock.acquire(), timeout=1)
    service._compliance_lock.release()
    with pytest.raises(ComplianceStateUnavailableError):
        await service.send_sms(
            to="+19255550123",
            text="must not write again",
        )
    assert writes == 1


def test_derived_key_auth_uses_configured_instance_pin() -> None:
    master = "master-secret"
    key_a = derive_agent_key(master, "geordi", "generation-a")
    headers_a = build_signed_headers(
        key_a,
        agent_name="geordi",
        instance_id="generation-a",
        method="POST",
        path="/v1/sms/send",
        timestamp=1000,
    )
    auth_a = DerivedKeyAuthenticator(
        master,
        current_instance_id="generation-a",
        allowed_agents={"geordi"},
        clock=lambda: 1000,
    )
    assert auth_a.authenticate(
        method="POST", path="/v1/sms/send", headers=headers_a
    )

    # A still-fresh request from the old pin is rejected after coordinated
    # configuration rotation to B.
    headers_a = build_signed_headers(
        key_a,
        agent_name="geordi",
        instance_id="generation-a",
        method="POST",
        path="/v1/sms/send",
        timestamp=2000,
    )
    auth_b = DerivedKeyAuthenticator(
        master,
        current_instance_id="generation-b",
        allowed_agents={"geordi"},
        clock=lambda: 2000,
    )
    assert (
        auth_b.authenticate(
            method="POST",
            path="/v1/sms/send",
            headers=headers_a,
        )
        is None
    )


def test_derived_key_auth_rejects_body_tamper() -> None:
    master = "master-secret"
    instance = "generation-a"
    key = derive_agent_key(master, "geordi", instance)
    headers = build_signed_headers(
        key,
        agent_name="geordi",
        instance_id=instance,
        method="POST",
        path="/v1/sms/send",
        body=b'{"to":"+19255550123"}',
        request_id="request-body-000001",
        timestamp=1000,
    )
    auth = DerivedKeyAuthenticator(
        master,
        current_instance_id=instance,
        allowed_agents={"geordi"},
        clock=lambda: 1000,
    )
    assert (
        auth.authenticate(
            method="POST",
            path="/v1/sms/send",
            headers=headers,
            body=b'{"to":"+14155550100"}',
        )
        is None
    )


def test_derived_key_auth_rejects_query_tamper() -> None:
    master = "master-secret"
    instance = "generation-a"
    key = derive_agent_key(master, "geordi", instance)
    headers = build_signed_headers(
        key,
        agent_name="geordi",
        instance_id=instance,
        method="GET",
        path="/v1/sms/history?phone_number=%2B19255550123",
        request_id="request-query-00001",
        timestamp=1000,
    )
    auth = DerivedKeyAuthenticator(
        master,
        current_instance_id=instance,
        allowed_agents={"geordi"},
        clock=lambda: 1000,
    )
    assert (
        auth.authenticate(
            method="GET",
            path="/v1/sms/history?phone_number=%2B14155550100",
            headers=headers,
        )
        is None
    )


def test_derived_key_auth_binds_duplicate_query_value_order() -> None:
    master = "master-secret"
    instance = "generation-a"
    key = derive_agent_key(master, "geordi", instance)
    headers = build_signed_headers(
        key,
        agent_name="geordi",
        instance_id=instance,
        method="GET",
        path="/v1/sms/history?limit=1&limit=2",
        request_id="request-query-00002",
        timestamp=1000,
    )
    auth = DerivedKeyAuthenticator(
        master,
        current_instance_id=instance,
        allowed_agents={"geordi"},
        clock=lambda: 1000,
    )
    assert (
        auth.authenticate(
            method="GET",
            path="/v1/sms/history?limit=2&limit=1",
            headers=headers,
        )
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


@pytest.mark.parametrize("body", [b"", b"not-json", b"[]"])
def test_mcp_malformed_200_write_is_uncertain(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return body

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *args, **kwargs: Response()
    )
    client = RingCentralBridgeClient(
        agent_name="geordi",
        secret="derived",
        instance_id="generation-a",
        bridge_url="http://bridge.invalid",
    )
    result = client.post(
        "/v1/sms/send", {"to": "+19255550123", "text": "hi"}
    )
    assert result["error"] == "bridge_malformed_response"
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
                    "type": "SMS",
                    "direction": "Inbound",
                    "from": {"phoneNumber": "+19255550123"},
                    "to": [{"phoneNumber": "+19254714225"}],
                    "subject": "STOP",
                    "creationTime": "2026-07-25T19:01:00Z",
                }
            ],
            "paging": {"totalPages": 1},
        }
    }
    scan_start = NOW + timedelta(minutes=2)
    worker = RingCentralInboundWorker(service, now=lambda: scan_start)
    assert await worker.poll_once() == 1
    assert dnt.get("+19255550123")["reason"] == "opt_out"
    assert store.get_metadata("ringcentral_inbound_poll_cursor") == (
        "2026-07-25T19:02:00Z"
    )


@pytest.mark.asyncio
async def test_empty_poll_advances_successful_scan_watermark(
    tmp_path: Path,
) -> None:
    service, _client, _dnt, store, _wake = make_service(tmp_path)
    worker = RingCentralInboundWorker(service, now=lambda: NOW)
    assert await worker.poll_once() == 0
    assert store.get_metadata("ringcentral_inbound_poll_cursor") == (
        "2026-07-25T19:00:00Z"
    )


@pytest.mark.asyncio
async def test_poll_freezes_snapshot_across_page_insertion(
    tmp_path: Path,
) -> None:
    service, client, _dnt, _store, wake = make_service(tmp_path)
    live = [
        {
            "id": f"m{index:03d}",
            "type": "SMS",
            "direction": "Inbound",
            "from": {"phoneNumber": f"+1415555{index:04d}"},
            "to": [{"phoneNumber": "+19254714225"}],
            "subject": "hello",
            "creationTime": f"scan-{index:03d}",
        }
        for index in range(200, 0, -1)
    ]
    calls: list[dict[str, Any]] = []

    async def list_messages(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        page = int(kwargs["page"])
        per_page = int(kwargs["per_page"])
        bounded = (
            [record for record in live if record["id"] != "m201"]
            if kwargs.get("date_to")
            else live
        )
        start = (page - 1) * per_page
        records = bounded[start : start + per_page]
        if page == 1:
            live.insert(
                0,
                {
                    "id": "m201",
                    "type": "SMS",
                    "direction": "Inbound",
                    "from": {"phoneNumber": "+14155559999"},
                    "to": [{"phoneNumber": "+19254714225"}],
                    "subject": "new",
                    "creationTime": "scan-201",
                },
            )
        return {
            "records": records,
            "paging": {"totalPages": 2},
        }

    client.list_messages = list_messages  # type: ignore[method-assign]
    worker = RingCentralInboundWorker(service, now=lambda: NOW)
    assert await worker.poll_once() == 200
    inbound_ids = [
        payload["message"]["id"]
        for kind, payload in wake.events
        if kind == "ringcentral.sms.inbound"
    ]
    assert inbound_ids == [f"m{index:03d}" for index in range(1, 201)]
    assert len(calls) == 2
    assert calls[0]["date_to"] == calls[1]["date_to"]
    assert calls[0]["phone_number"] == "+19254714225"


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
        "type": "SMS",
        "direction": "Inbound",
        "from": {"phoneNumber": "+19255550123"},
        "to": [{"phoneNumber": "+19254714225"}],
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


@pytest.mark.asyncio
async def test_ws_subscribes_before_overlapping_catchup_poll(
    tmp_path: Path,
) -> None:
    service, client, _dnt, _store, wake = make_service(tmp_path)
    subscription_sent = [False]
    poll_after_subscription: list[bool] = []
    message = {
        "id": "handoff-gap-sms",
        "type": "SMS",
        "direction": "Inbound",
        "from": {"phoneNumber": "+14155550100"},
        "to": [{"phoneNumber": "+19254714225"}],
        "subject": "arrived during handoff",
        "creationTime": "2026-07-25T19:00:00Z",
    }

    async def list_messages(**kwargs: Any) -> dict[str, Any]:
        poll_after_subscription.append(subscription_sent[0])
        return {
            "records": [message],
            "paging": {"totalPages": 1},
        }

    client.list_messages = list_messages  # type: ignore[method-assign]
    notification = json.dumps(
        [
            {"type": "ServerNotification", "messageId": "gap-notification"},
            {"event": INBOUND_FILTER, "body": message},
        ]
    )

    class FakeWebSocket:
        def __init__(self) -> None:
            self.frames = [
                json.dumps([{"type": "ConnectionDetails"}]),
                json.dumps([{"type": "ClientResponse", "status": 200}]),
                notification,
            ]

        async def send(self, value: str) -> None:
            subscription_sent[0] = True

        async def recv(self) -> str:
            frame = self.frames.pop(0)
            if frame == notification:
                worker.stop()
            return frame

    websocket = FakeWebSocket()

    class FakeConnection:
        async def __aenter__(self) -> FakeWebSocket:
            return websocket

        async def __aexit__(self, *args: Any) -> None:
            return None

    worker = RingCentralInboundWorker(
        service,
        websocket_connect=lambda *args, **kwargs: FakeConnection(),
        now=lambda: NOW,
    )
    await worker._run_websocket()
    inbound_events = [
        payload
        for kind, payload in wake.events
        if kind == "ringcentral.sms.inbound"
    ]
    assert poll_after_subscription == [True]
    assert len(inbound_events) == 1
    assert inbound_events[0]["message"]["id"] == "handoff-gap-sms"


@pytest.mark.asyncio
async def test_housekeeping_drains_pending_wake_while_ws_is_healthy(
    tmp_path: Path,
) -> None:
    service, client, _dnt, store, _wake = make_service(
        tmp_path,
        wake=FailingWakeSink(),
    )
    await service.handle_inbound(
        {
            "id": "housekeeping-retry",
            "type": "SMS",
            "direction": "Inbound",
            "from": {"phoneNumber": "+14155550100"},
            "to": [{"phoneNumber": "+19254714225"}],
            "subject": "retry me",
        }
    )
    idle = asyncio.Event()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.frames = [
                json.dumps([{"type": "ConnectionDetails"}]),
                json.dumps([{"type": "ClientResponse", "status": 200}]),
            ]

        async def send(self, value: str) -> None:
            return None

        async def recv(self) -> str:
            if self.frames:
                return self.frames.pop(0)
            idle.set()
            await worker._stop.wait()
            return json.dumps([{"type": "Heartbeat"}])

    websocket = FakeWebSocket()

    class FakeConnection:
        async def __aenter__(self) -> FakeWebSocket:
            return websocket

        async def __aexit__(self, *args: Any) -> None:
            return None

    worker = RingCentralInboundWorker(
        service,
        housekeeping_interval=0.01,
        websocket_connect=lambda *args, **kwargs: FakeConnection(),
        now=lambda: NOW,
    )
    ws_task = asyncio.create_task(worker._run_websocket())
    housekeeping_task = asyncio.create_task(
        worker.run_housekeeping_forever()
    )
    await asyncio.wait_for(idle.wait(), timeout=1)
    recovered = MemoryWakeSink()
    service.wake_sink = recovered

    async def wait_until_drained() -> None:
        while store.pending_inbound():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_until_drained(), timeout=1)
    worker.stop()
    await asyncio.gather(ws_task, housekeeping_task)
    assert store.pending_inbound() == []
    assert len(recovered.events) == 1
    assert recovered.events[0][1]["message"]["id"] == "housekeeping-retry"


@pytest.mark.asyncio
async def test_ws_heartbeat_response_is_consumed_without_echo(
    tmp_path: Path,
) -> None:
    service, _client, _dnt, _store, _wake = make_service(tmp_path)
    worker = RingCentralInboundWorker(service)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, value: str) -> None:
            self.sent.append(value)

    websocket = FakeWebSocket()
    frame = json.dumps(
        [{"type": "Heartbeat", "messageId": "client-heartbeat-1"}]
    )
    await worker._handle_frame(websocket, frame)
    assert websocket.sent == []
