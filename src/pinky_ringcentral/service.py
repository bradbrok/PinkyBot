"""RingCentral SMS primitives and bridge-owned compliance invariants."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pinky_ringcentral.client import RingCentralAPIError, RingCentralClient
from pinky_ringcentral.store import (
    BridgeStore,
    DoNotTextStore,
    DoNotTextUnavailableError,
    utc_now,
)

SENDER_PHONE = "+19254714225"
DEFAULT_RECIPIENT_TIMEZONE = "America/Los_Angeles"
QUIET_HOURS_START = time(8, 0)
QUIET_HOURS_END = time(20, 0)
STOP_KEYWORDS = frozenset(
    {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
)
TERMINAL_FAILURE_STATUSES = frozenset({"DeliveryFailed", "SendingFailed"})

_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPT_OUT_CODE_RE = re.compile(r"^SMS-[A-Z]+-413$", re.IGNORECASE)
_LANDLINE_CODE_RE = re.compile(
    r"^SMS-(?:(?:RC|UP)-410|CAR-(?:400|411))$", re.IGNORECASE
)


class WakeSink(Protocol):
    async def send_event(self, kind: str, payload: dict[str, Any]) -> None: ...


class SmsServiceError(Exception):
    """Safe, structured error for bridge and MCP callers."""

    code = "sms_service_error"
    status_code = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), **self.details}


class SmsValidationError(SmsServiceError):
    code = "invalid_request"
    status_code = 422


class ComplianceStateUnavailableError(SmsServiceError):
    code = "compliance_state_unavailable"
    status_code = 503


class DoNotTextBlockedError(SmsServiceError):
    code = "do_not_text"
    status_code = 409


class QuietHoursBlockedError(SmsServiceError):
    code = "quiet_hours"
    status_code = 409


class AuditUnavailableError(SmsServiceError):
    code = "audit_unavailable"
    status_code = 503


def normalize_phone(phone_number: str) -> str:
    """Normalize a US 10-digit or already-E.164 destination."""

    raw = str(phone_number or "").strip()
    compact = re.sub(r"[\s().-]", "", raw)
    if compact.isdigit() and len(compact) == 10:
        compact = f"+1{compact}"
    elif compact.startswith("1") and compact.isdigit() and len(compact) == 11:
        compact = f"+{compact}"
    if not _PHONE_RE.fullmatch(compact):
        raise SmsValidationError(
            "phone_number must be a valid E.164 number",
            field="phone_number",
        )
    return compact


def normalize_text(text: str) -> str:
    value = str(text or "")
    if not value.strip():
        raise SmsValidationError("text must not be empty", field="text")
    if len(value) > 1000:
        raise SmsValidationError(
            "text exceeds RingCentral's 1000-character limit",
            field="text",
            max_length=1000,
        )
    return value


def quiet_hours_result(
    *,
    recipient_timezone: str,
    now: datetime,
) -> tuple[bool, str]:
    """Return ``(allowed, next_allowed_at)`` for the locked 08:00–20:00 window."""

    try:
        zone = ZoneInfo(recipient_timezone)
    except ZoneInfoNotFoundError as exc:
        raise SmsValidationError(
            "recipient_timezone must be a valid IANA timezone",
            field="recipient_timezone",
        ) from exc
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_now = now.astimezone(zone)
    local_clock = local_now.timetz().replace(tzinfo=None)
    if QUIET_HOURS_START <= local_clock < QUIET_HOURS_END:
        return True, ""
    next_day = local_now.date()
    if local_clock >= QUIET_HOURS_END:
        next_day += timedelta(days=1)
    next_local = datetime.combine(next_day, QUIET_HOURS_START, tzinfo=zone)
    next_utc = next_local.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return False, next_utc


def classify_delivery_dnt(delivery_error_code: str) -> str:
    code = str(delivery_error_code or "").strip()
    if _OPT_OUT_CODE_RE.fullmatch(code):
        return "opt_out"
    if _LANDLINE_CODE_RE.fullmatch(code):
        return "landline"
    return ""


def _message_id(message: dict[str, Any]) -> str:
    return str(message.get("id") or "")


def _delivery_code(message: dict[str, Any]) -> str:
    return str(
        message.get("deliveryErrorCode")
        or message.get("deliveryErrorCodeId")
        or ""
    )


def _message_phone(message: dict[str, Any], field: str) -> str:
    value = message.get(field)
    if isinstance(value, list):
        value = value[0] if value else {}
    if not isinstance(value, dict):
        return ""
    return str(value.get("phoneNumber") or "")


class SmsService:
    """Compliance-gated send, history, status, and inbound wake service."""

    def __init__(
        self,
        client: RingCentralClient,
        dnt: DoNotTextStore,
        store: BridgeStore,
        wake_sink: WakeSink,
        *,
        now: Any = lambda: datetime.now(UTC),
        threading_days: int = 7,
    ) -> None:
        self.client = client
        self.dnt = dnt
        self.store = store
        self.wake_sink = wake_sink
        self._now = now
        self._threading_days = threading_days
        # This is the linearization boundary between inbound STOP and transmit.
        self._compliance_lock = asyncio.Lock()

    async def send_sms(
        self,
        *,
        to: str,
        text: str,
        template_id: str = "",
        recipient_timezone: str = DEFAULT_RECIPIENT_TIMEZONE,
    ) -> dict[str, Any]:
        phone = normalize_phone(to)
        body = normalize_text(text)
        timezone = str(recipient_timezone or DEFAULT_RECIPIENT_TIMEZONE)
        try:
            attempt_id = self.store.begin_attempt(
                phone_number=phone,
                text=body,
                template_id=str(template_id or ""),
                recipient_timezone=timezone,
            )
        except Exception as exc:
            raise AuditUnavailableError(
                "audit log is unavailable; refusing transmission"
            ) from exc

        try:
            allowed, next_allowed_at = quiet_hours_result(
                recipient_timezone=timezone,
                now=self._now(),
            )
        except SmsValidationError as exc:
            self._finish_attempt(
                attempt_id, outcome="rejected", error=str(exc)
            )
            exc.details.setdefault("attempt_id", attempt_id)
            raise
        if not allowed:
            error = QuietHoursBlockedError(
                "recipient-local quiet hours block transmission",
                recipient_timezone=timezone,
                next_allowed_at=next_allowed_at,
                attempt_id=attempt_id,
            )
            self._finish_attempt(attempt_id, outcome="rejected", error=str(error))
            raise error

        try:
            async with self._compliance_lock:
                # Non-negotiable STOP-race gate: read authoritative state at the
                # same boundary that initiates the RingCentral transmission.
                blocked = self.dnt.get(phone)
                if blocked is not None:
                    error = DoNotTextBlockedError(
                        "destination is in the do_not_text registry",
                        phone_number=phone,
                        reason=str(blocked.get("reason") or "do_not_text"),
                        attempt_id=attempt_id,
                    )
                    self._finish_attempt(
                        attempt_id, outcome="rejected", error=str(error)
                    )
                    raise error
                message = await self.client.send_sms(
                    from_phone=SENDER_PHONE,
                    to_phone=phone,
                    text=body,
                )
        except DoNotTextUnavailableError as exc:
            self._finish_attempt(
                attempt_id,
                outcome="rejected",
                error="do_not_text registry unavailable",
            )
            raise ComplianceStateUnavailableError(
                str(exc), attempt_id=attempt_id
            ) from exc
        except DoNotTextBlockedError:
            raise
        except RingCentralAPIError as exc:
            self._finish_attempt(attempt_id, outcome="failed", error=str(exc))
            raise
        except Exception as exc:
            self._finish_attempt(
                attempt_id,
                outcome="failed",
                error=f"unexpected {type(exc).__name__}",
            )
            raise RingCentralAPIError(
                f"SMS transmission failed unexpectedly: {type(exc).__name__}",
                may_have_completed=True,
            ) from exc

        try:
            self._finish_attempt(
                attempt_id, outcome="transmitted", message=message
            )
        except AuditUnavailableError as exc:
            exc.details.update(
                {
                    "may_have_completed": True,
                    "message_id": _message_id(message),
                    "message_status": str(message.get("messageStatus") or ""),
                }
            )
            raise
        await self._process_delivery_state(message, phone_number=phone)
        return {
            "attempt_id": attempt_id,
            "message_id": _message_id(message),
            "message_status": str(message.get("messageStatus") or ""),
            "delivery_error_code": _delivery_code(message),
            "from": SENDER_PHONE,
            "to": phone,
            "template_id": str(template_id or ""),
        }

    async def get_sms_history(
        self,
        *,
        phone_number: str,
        limit: int = 100,
        days: int = 30,
    ) -> dict[str, Any]:
        phone = normalize_phone(phone_number)
        if not 1 <= limit <= 1000:
            raise SmsValidationError("limit must be between 1 and 1000", field="limit")
        if not 1 <= days <= 3650:
            raise SmsValidationError("days must be between 1 and 3650", field="days")
        date_from = (self._now().astimezone(UTC) - timedelta(days=days)).isoformat()
        records: list[dict[str, Any]] = []
        page = 1
        while len(records) < limit:
            payload = await self.client.list_messages(
                phone_number=phone,
                date_from=date_from,
                page=page,
                per_page=min(100, limit - len(records)),
            )
            page_records = payload.get("records")
            if not isinstance(page_records, list):
                raise RingCentralAPIError(
                    "Message-store query returned malformed records"
                )
            records.extend(
                record for record in page_records if isinstance(record, dict)
            )
            paging = payload.get("paging")
            total_pages = (
                int(paging.get("totalPages") or page)
                if isinstance(paging, dict)
                else page
            )
            if not page_records or page >= total_pages:
                break
            page += 1
        records = records[:limit]
        records.sort(key=lambda item: str(item.get("creationTime") or ""))
        return {
            "phone_number": phone,
            "days": days,
            "count": len(records),
            # Keep the general RingCentral message records intact: #448 needs
            # full thread context, not an SMS-flow-specific projection.
            "records": records,
        }

    async def get_sms_status(self, *, message_id: str) -> dict[str, Any]:
        value = str(message_id or "").strip()
        if not _MESSAGE_ID_RE.fullmatch(value):
            raise SmsValidationError(
                "message_id has an invalid shape", field="message_id"
            )
        message = await self.client.get_message(value)
        self.store.update_message_status(message)
        phone = ""
        try:
            phone = normalize_phone(_message_phone(message, "to"))
        except SmsValidationError:
            pass
        await self._process_delivery_state(message, phone_number=phone)
        return {
            "message_id": _message_id(message) or value,
            "message_status": str(message.get("messageStatus") or ""),
            "delivery_error_code": _delivery_code(message),
            "creation_time": str(message.get("creationTime") or ""),
            "last_modified_time": str(message.get("lastModifiedTime") or ""),
            "raw": message,
        }

    async def add_do_not_text(
        self,
        *,
        phone_number: str,
        reason: str = "manual",
        source: str = "manual",
    ) -> dict[str, Any]:
        phone = normalize_phone(phone_number)
        async with self._compliance_lock:
            try:
                return self.dnt.add(phone, reason=reason, source=source)
            except DoNotTextUnavailableError as exc:
                raise ComplianceStateUnavailableError(str(exc)) from exc

    async def remove_do_not_text(self, *, phone_number: str) -> dict[str, Any]:
        phone = normalize_phone(phone_number)
        async with self._compliance_lock:
            try:
                removed = self.dnt.remove(phone)
            except DoNotTextUnavailableError as exc:
                raise ComplianceStateUnavailableError(str(exc)) from exc
        return {"phone_number": phone, "removed": removed}

    async def list_do_not_text(self) -> dict[str, Any]:
        try:
            entries = self.dnt.list()
        except DoNotTextUnavailableError as exc:
            raise ComplianceStateUnavailableError(str(exc)) from exc
        return {"count": len(entries), "entries": entries}

    async def handle_inbound(self, message: dict[str, Any]) -> str:
        """Persist and classify one inbound message, returning its class."""

        message_id = _message_id(message)
        if not message_id:
            raise SmsValidationError("inbound message omitted id")
        phone = normalize_phone(_message_phone(message, "from"))
        keyword = str(message.get("subject") or "").strip().upper()
        received_at = str(message.get("creationTime") or utc_now())
        if keyword in STOP_KEYWORDS:
            # DNT is updated before the event can enter Geordi's serial queue.
            async with self._compliance_lock:
                try:
                    self.dnt.add(
                        phone,
                        reason="opt_out",
                        source="inbound_stop",
                        source_message_id=message_id,
                        triggered_at=received_at,
                    )
                except DoNotTextUnavailableError as exc:
                    raise ComplianceStateUnavailableError(str(exc)) from exc
            self.store.enqueue_inbound(
                message_id=message_id,
                payload=message,
                classification="stop",
                threading_hint={},
                delivered=True,
            )
            return "stop"

        since = (
            self._now().astimezone(UTC) - timedelta(days=self._threading_days)
        ).isoformat()
        recent = self.store.recent_outbound(phone, since=since)
        classification = "threaded_reply" if recent else "new_inbound"
        hint = {
            "has_recent_outbound": bool(recent),
            "window_days": self._threading_days,
            "recent_outbound": recent,
        }
        self.store.enqueue_inbound(
            message_id=message_id,
            payload=message,
            classification=classification,
            threading_hint=hint,
        )
        await self.deliver_pending_inbound()
        return classification

    async def deliver_pending_inbound(self, *, limit: int = 100) -> int:
        delivered = 0
        for item in self.store.pending_inbound(limit=limit):
            event = {
                "schema_version": 1,
                "source": "ringcentral_sms",
                "classification": item["classification"],
                "threading_hint": item["threading_hint"],
                "message": item["payload"],
            }
            try:
                await self.wake_sink.send_event("ringcentral.sms.inbound", event)
            except Exception as exc:
                self.store.record_inbound_delivery(
                    item["message_id"],
                    delivered=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                break
            self.store.record_inbound_delivery(
                item["message_id"], delivered=True
            )
            delivered += 1
        return delivered

    async def refresh_pending_statuses(self, *, limit: int = 100) -> int:
        refreshed = 0
        for message_id in self.store.pending_status_ids(limit=limit):
            await self.get_sms_status(message_id=message_id)
            refreshed += 1
        return refreshed

    async def _process_delivery_state(
        self, message: dict[str, Any], *, phone_number: str
    ) -> None:
        status = str(message.get("messageStatus") or "")
        if status not in TERMINAL_FAILURE_STATUSES:
            return
        message_id = _message_id(message)
        carrier_code = _delivery_code(message)
        reason = classify_delivery_dnt(carrier_code)
        if reason and phone_number:
            async with self._compliance_lock:
                try:
                    self.dnt.add(
                        phone_number,
                        reason=reason,
                        source="delivery_failure",
                        source_message_id=message_id,
                        carrier_code=carrier_code,
                        triggered_at=str(
                            message.get("lastModifiedTime")
                            or message.get("creationTime")
                            or utc_now()
                        ),
                    )
                except DoNotTextUnavailableError:
                    # Leave this terminal message pending in the outbound index.
                    # Polling retries the mandatory compliance mutation; never
                    # make a completed send look safe to retry.
                    return

        event = {
            "schema_version": 1,
            "source": "ringcentral_sms",
            "message_id": message_id,
            "message_status": status,
            "delivery_error_code": carrier_code,
            "do_not_text_reason": reason,
            "message": message,
        }
        try:
            await self.wake_sink.send_event(
                "ringcentral.sms.delivery_failed", event
            )
        except Exception:
            # The outbound index remains pending so polling retries this wake;
            # never turn a successful transmission into a retryable send error.
            return
        try:
            self.store.mark_delivery_notified(message_id)
        except Exception:
            # The wake may duplicate on retry, but an SMS send never does.
            return

    def _finish_attempt(self, attempt_id: str, **kwargs: Any) -> None:
        try:
            self.store.finish_attempt(attempt_id, **kwargs)
        except Exception as exc:
            # A send cannot be undone. Before transmission this caller always
            # fails closed; after an RC response, surface the audit outage.
            raise AuditUnavailableError(
                "audit log could not record the transmission attempt",
                attempt_id=attempt_id,
            ) from exc
