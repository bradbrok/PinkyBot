"""RingCentral SMS primitives and bridge-owned compliance invariants."""

from __future__ import annotations

import asyncio
import contextlib
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
STATUS_QUARANTINE_AFTER_FAILURES = 3
STATUS_RETRY_BASE_SECONDS = 30
STATUS_RETRY_MAX_SECONDS = 3600

_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPT_OUT_CODE_RE = re.compile(r"^SMS-[A-Z]+-413$", re.IGNORECASE)
_LANDLINE_CODE_RE = re.compile(
    r"^SMS-(?:(?:RC|UP)-410|CAR-(?:400|411))$", re.IGNORECASE
)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(Exception, asyncio.CancelledError):
        task.result()


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


class SmsNotFoundError(SmsServiceError):
    code = "sms_not_found"
    status_code = 404


class SmsIntegrityError(SmsServiceError):
    code = "sms_integrity_error"
    status_code = 502


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
    except (ValueError, ZoneInfoNotFoundError) as exc:
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
        cancellation_reconcile_timeout: float = 1.0,
    ) -> None:
        self.client = client
        self.dnt = dnt
        self.store = store
        self.wake_sink = wake_sink
        self._now = now
        self._threading_days = threading_days
        self._cancellation_reconcile_timeout = max(
            cancellation_reconcile_timeout,
            0.0,
        )
        # This is the linearization boundary between inbound STOP and transmit.
        self._compliance_lock = asyncio.Lock()
        self._side_effect_lock = asyncio.Lock()
        self._inbound_delivery_lock = asyncio.Lock()
        self._status_refresh_lock = asyncio.Lock()

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

        cancellation = asyncio.Event()
        owned = asyncio.create_task(
            self._dispatch_and_finalize(
                attempt_id=attempt_id,
                phone=phone,
                body=body,
                cancellation=cancellation,
            ),
            name=f"ringcentral-send-{attempt_id}",
        )
        owned.add_done_callback(_consume_task_result)
        try:
            message = await asyncio.shield(owned)
        except asyncio.CancelledError:
            cancellation.set()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(owned)
            raise
        return {
            "attempt_id": attempt_id,
            "message_id": _message_id(message),
            "message_status": str(message.get("messageStatus") or ""),
            "delivery_error_code": _delivery_code(message),
            "from": SENDER_PHONE,
            "to": phone,
            "template_id": str(template_id or ""),
        }

    async def _dispatch_and_finalize(
        self,
        *,
        attempt_id: str,
        phone: str,
        body: str,
        cancellation: asyncio.Event,
    ) -> dict[str, Any]:
        try:
            async with self._compliance_lock:
                # This lock is the STOP/transmit linearization boundary. The
                # owned task retains it through the single RC dispatch even if
                # its caller is cancelled.
                try:
                    pending_event = self.store.pending_compliance_event(phone)
                    quarantined_status = (
                        self.store.quarantined_status_for_phone(phone)
                    )
                    unresolved_attempt = (
                        self.store.unresolved_attempt_for_phone(
                            phone,
                            exclude_attempt_id=attempt_id,
                        )
                    )
                except Exception as exc:
                    error = ComplianceStateUnavailableError(
                        "the compliance quarantine ledger is unavailable",
                        phone_number=phone,
                        attempt_id=attempt_id,
                    )
                    self._finish_attempt(
                        attempt_id,
                        outcome="rejected",
                        error=str(error),
                    )
                    raise error from exc
                if pending_event:
                    error = ComplianceStateUnavailableError(
                        "a mandatory compliance update is pending",
                        phone_number=phone,
                        pending_event=pending_event,
                        attempt_id=attempt_id,
                    )
                    self._finish_attempt(
                        attempt_id,
                        outcome="rejected",
                        error=str(error),
                    )
                    raise error
                if quarantined_status:
                    error = ComplianceStateUnavailableError(
                        "an outbound SMS has an unresolved status quarantine",
                        phone_number=phone,
                        message_id=quarantined_status,
                        attempt_id=attempt_id,
                    )
                    self._finish_attempt(
                        attempt_id,
                        outcome="rejected",
                        error=str(error),
                    )
                    raise error
                if unresolved_attempt:
                    error = ComplianceStateUnavailableError(
                        "a prior SMS transmission has an unresolved outcome",
                        phone_number=phone,
                        unresolved_attempt_id=unresolved_attempt,
                        uncertain_attempt_id=unresolved_attempt,
                        attempt_id=attempt_id,
                    )
                    self._finish_attempt(
                        attempt_id,
                        outcome="rejected",
                        error=str(error),
                    )
                    raise error
                blocked = self.dnt.get(phone)
                if blocked is not None:
                    error = DoNotTextBlockedError(
                        "destination is in the do_not_text registry",
                        phone_number=phone,
                        reason=str(blocked.get("reason") or "do_not_text"),
                        attempt_id=attempt_id,
                    )
                    self._finish_attempt(
                        attempt_id,
                        outcome="rejected",
                        error=str(error),
                    )
                    raise error
                if cancellation.is_set():
                    raise RingCentralAPIError(
                        "SMS transmission cancelled before dispatch"
                    )
                message = await self._await_dispatch_response(
                    from_phone=SENDER_PHONE,
                    to_phone=phone,
                    text=body,
                    cancellation=cancellation,
                )
                self._validate_send_response(message)
        except DoNotTextUnavailableError as exc:
            self._finish_attempt(
                attempt_id,
                outcome="rejected",
                error="do_not_text registry unavailable",
            )
            raise ComplianceStateUnavailableError(
                str(exc), attempt_id=attempt_id
            ) from exc
        except (
            AuditUnavailableError,
            ComplianceStateUnavailableError,
            DoNotTextBlockedError,
        ):
            raise
        except RingCentralAPIError as exc:
            try:
                self._finish_attempt(
                    attempt_id,
                    outcome="uncertain" if exc.may_have_completed else "failed",
                    error=str(exc),
                    may_have_completed=exc.may_have_completed,
                )
            except AuditUnavailableError as audit_exc:
                audit_exc.details["may_have_completed"] = (
                    exc.may_have_completed
                )
                raise audit_exc from exc
            raise
        except Exception as exc:
            transmission_error = RingCentralAPIError(
                f"SMS transmission failed unexpectedly: {type(exc).__name__}",
                may_have_completed=True,
            )
            try:
                self._finish_attempt(
                    attempt_id,
                    outcome="uncertain",
                    error=f"unexpected {type(exc).__name__}",
                    may_have_completed=True,
                )
            except AuditUnavailableError as audit_exc:
                audit_exc.details["may_have_completed"] = True
                raise audit_exc from transmission_error
            raise transmission_error from exc

        try:
            self._finish_attempt(
                attempt_id,
                outcome="transmitted",
                message=message,
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
        try:
            await self._process_delivery_state(message, phone_number=phone)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AuditUnavailableError(
                "mandatory delivery side-effect staging is unavailable",
                attempt_id=attempt_id,
                message_id=_message_id(message),
                may_have_completed=True,
            ) from exc
        return message

    async def _await_dispatch_response(
        self,
        *,
        from_phone: str,
        to_phone: str,
        text: str,
        cancellation: asyncio.Event,
    ) -> dict[str, Any]:
        request = asyncio.create_task(
            self.client.send_sms(
                from_phone=from_phone,
                to_phone=to_phone,
                text=text,
            ),
            name="ringcentral-api-send",
        )
        cancelled = asyncio.create_task(
            cancellation.wait(),
            name="ringcentral-send-cancellation",
        )
        try:
            done, _pending = await asyncio.wait(
                {request, cancelled},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if request in done:
                try:
                    return request.result()
                except asyncio.CancelledError as exc:
                    raise RingCentralAPIError(
                        "SMS transmission outcome became unknown",
                        may_have_completed=True,
                    ) from exc
            try:
                return await asyncio.wait_for(
                    asyncio.shield(request),
                    timeout=self._cancellation_reconcile_timeout,
                )
            except TimeoutError as exc:
                request.cancel()
                request.add_done_callback(_consume_task_result)
                raise RingCentralAPIError(
                    "SMS transmission cancelled after dispatch; outcome unknown",
                    may_have_completed=True,
                ) from exc
            except asyncio.CancelledError as exc:
                raise RingCentralAPIError(
                    "SMS transmission outcome became unknown",
                    may_have_completed=True,
                ) from exc
        finally:
            cancelled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancelled

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
        scan_start = self._now().astimezone(UTC)
        date_from = (scan_start - timedelta(days=days)).isoformat()
        date_to = scan_start.isoformat()
        records: list[dict[str, Any]] = []
        page = 1
        while len(records) < limit:
            payload = await self.client.list_messages(
                phone_number=phone,
                date_from=date_from,
                date_to=date_to,
                page=page,
                per_page=100,
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
        outbound = self.store.get_outbound(value)
        if outbound is None:
            self._record_status_check(value, outcome="not_found")
            raise SmsNotFoundError(
                "message_id is not an indexed outbound SMS",
                message_id=value,
            )
        message = await self.client.get_message(value)
        violations = self._status_integrity_violations(
            message,
            message_id=value,
            phone_number=str(outbound["phone_number"]),
        )
        if violations:
            error = f"status scope mismatch: {','.join(violations)}"
            self._record_status_check(
                value,
                outcome="integrity_error",
                error=error,
            )
            raise SmsIntegrityError(
                "RingCentral status record failed SMS scope validation",
                message_id=value,
                violations=violations,
            )
        self.store.update_message_status(message)
        self._record_status_check(value, outcome="ok")
        await self._process_delivery_state(
            message,
            phone_number=str(outbound["phone_number"]),
        )
        return {
            "message_id": value,
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
                return self.dnt.add(
                    phone,
                    reason=reason,
                    source=source,
                    triggered_at=self._now()
                    .astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    manual_override=True,
                )
            except DoNotTextUnavailableError as exc:
                raise ComplianceStateUnavailableError(str(exc)) from exc

    async def remove_do_not_text(self, *, phone_number: str) -> dict[str, Any]:
        phone = normalize_phone(phone_number)
        async with self._compliance_lock:
            try:
                removed = self.dnt.remove(
                    phone,
                    removed_at=self._now()
                    .astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                )
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
        violations = self._inbound_integrity_violations(message)
        if violations:
            audit_message_id = message_id if message_id else "<missing>"
            try:
                self.store.record_inbound_rejection(
                    audit_message_id,
                    error=f"inbound scope mismatch: {','.join(violations)}",
                )
            except Exception as exc:
                raise AuditUnavailableError(
                    "audit log could not quarantine an invalid inbound record"
                ) from exc
            return "ignored"
        phone = normalize_phone(_message_phone(message, "from"))
        keyword = str(message.get("subject") or "").strip().upper()
        received_at = str(message.get("creationTime") or utc_now())
        if keyword in STOP_KEYWORDS:
            event_key = f"inbound:{message_id}"
            async with self._side_effect_lock:
                if not self.store.side_effects_required(
                    event_key,
                    compliance_phone=phone,
                ):
                    return "stop"
                self.store.enqueue_inbound(
                    message_id=message_id,
                    payload=message,
                    classification="stop",
                    threading_hint={},
                    delivered=True,
                )
                # DNT is updated before the event can enter Geordi's serial
                # queue. A pending event is retried after a crash; the DNT
                # removal tombstone prevents stale evidence from winning.
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
                self.store.mark_compliance_applied(event_key)
                self.store.mark_side_effects_applied(event_key)
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
        async with self._inbound_delivery_lock:
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
                    await self.wake_sink.send_event(
                        "ringcentral.sms.inbound", event
                    )
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
        async with self._status_refresh_lock:
            return await self._refresh_pending_statuses(limit=limit)

    async def _refresh_pending_statuses(self, *, limit: int) -> int:
        refreshed = 0
        now = self._now().astimezone(UTC)
        audit_failure: Exception | None = None
        for message_id in self.store.pending_status_ids(
            limit=limit,
            now=now.isoformat(),
        ):
            try:
                await self.get_sms_status(message_id=message_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                outbound = self.store.get_outbound(message_id)
                if outbound is None:
                    continue
                failures = int(outbound["status_failure_count"]) + 1
                quarantine = (
                    failures >= STATUS_QUARANTINE_AFTER_FAILURES
                )
                delay = min(
                    STATUS_RETRY_BASE_SECONDS * (2 ** (failures - 1)),
                    STATUS_RETRY_MAX_SECONDS,
                )
                next_retry_at = (now + timedelta(seconds=delay)).isoformat()
                error = f"{type(exc).__name__}: {str(exc)[:400]}"
                try:
                    self.store.record_status_failure(
                        message_id,
                        error=error,
                        next_retry_at=next_retry_at,
                        quarantine=quarantine,
                    )
                except Exception as store_exc:
                    audit_failure = store_exc
                continue
            refreshed += 1
        if audit_failure is not None:
            raise AuditUnavailableError(
                "audit log could not record status retry state"
            ) from audit_failure
        return refreshed

    async def _process_delivery_state(
        self, message: dict[str, Any], *, phone_number: str
    ) -> None:
        status = str(message.get("messageStatus") or "")
        if status not in TERMINAL_FAILURE_STATUSES:
            return
        message_id = _message_id(message)
        if not _MESSAGE_ID_RE.fullmatch(message_id):
            return
        carrier_code = _delivery_code(message)
        reason = classify_delivery_dnt(carrier_code)
        event_key = f"delivery:{message_id}"
        async with self._side_effect_lock:
            if not self.store.side_effects_required(
                event_key,
                compliance_phone=phone_number if reason else "",
            ):
                return
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
                        # Leave the side-effect event pending. Polling retries
                        # the compliance mutation, while a removal tombstone
                        # prevents stale evidence from overriding a human.
                        return
                try:
                    self.store.mark_compliance_applied(event_key)
                except Exception:
                    # Keep the durable quarantine if reconciliation cannot be
                    # recorded, even when the JSON write itself succeeded.
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
                # The side-effect event remains pending so polling retries this
                # wake; never turn a successful send into a retryable error.
                return
            try:
                self.store.mark_side_effects_applied(
                    event_key, delivery_message_id=message_id
                )
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

    def _validate_send_response(self, message: dict[str, Any]) -> None:
        message_id = _message_id(message)
        message_status = message.get("messageStatus")
        if (
            not _MESSAGE_ID_RE.fullmatch(message_id)
            or not isinstance(message_status, str)
            or not message_status.strip()
        ):
            raise RingCentralAPIError(
                "SMS transmission response was incomplete",
                may_have_completed=True,
            )

    def _record_status_check(
        self, message_id: str, *, outcome: str, error: str = ""
    ) -> None:
        try:
            self.store.record_status_check(
                message_id,
                outcome=outcome,
                error=error,
            )
        except Exception as exc:
            raise AuditUnavailableError(
                "audit log could not record the status lookup",
                message_id=message_id,
            ) from exc

    def _status_integrity_violations(
        self,
        message: dict[str, Any],
        *,
        message_id: str,
        phone_number: str,
    ) -> list[str]:
        violations: list[str] = []
        if _message_id(message) != message_id:
            violations.append("message_id")
        if str(message.get("type") or "") != "SMS":
            violations.append("type")
        if str(message.get("direction") or "") != "Outbound":
            violations.append("direction")
        try:
            origin = normalize_phone(_message_phone(message, "from"))
        except SmsValidationError:
            origin = ""
        if origin != SENDER_PHONE:
            violations.append("from")
        try:
            destination = normalize_phone(_message_phone(message, "to"))
        except SmsValidationError:
            destination = ""
        if destination != phone_number:
            violations.append("to")
        return violations

    def _inbound_integrity_violations(
        self, message: dict[str, Any]
    ) -> list[str]:
        violations: list[str] = []
        if not _MESSAGE_ID_RE.fullmatch(_message_id(message)):
            violations.append("message_id")
        if str(message.get("type") or "") != "SMS":
            violations.append("type")
        if str(message.get("direction") or "") != "Inbound":
            violations.append("direction")
        receivers = message.get("to")
        if isinstance(receivers, dict):
            receivers = [receivers]
        receiver_matches = False
        if isinstance(receivers, list):
            for receiver in receivers:
                if not isinstance(receiver, dict):
                    continue
                try:
                    phone = normalize_phone(
                        str(receiver.get("phoneNumber") or "")
                    )
                except SmsValidationError:
                    continue
                if phone == SENDER_PHONE:
                    receiver_matches = True
                    break
        if not receiver_matches:
            violations.append("to")
        try:
            normalize_phone(_message_phone(message, "from"))
        except SmsValidationError:
            violations.append("from")
        return violations
