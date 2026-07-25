"""Bridge-side compliance state, audit records, and inbound delivery queue."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class DoNotTextUnavailableError(Exception):
    """The authoritative DNT file cannot be read safely."""


_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _normalize_phone_key(value: str) -> str:
    compact = re.sub(r"[\s().-]", "", value.strip())
    if compact.isdigit() and len(compact) == 10:
        compact = f"+1{compact}"
    elif compact.startswith("1") and compact.isdigit() and len(compact) == 11:
        compact = f"+{compact}"
    if not _PHONE_RE.fullmatch(compact):
        raise DoNotTextUnavailableError(
            "do_not_text registry contains an invalid phone key"
        )
    return compact


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class DoNotTextStore:
    """Atomic JSON do-not-text registry.

    Reads are deliberately fail-closed: missing, unreadable, or malformed state
    is an error rather than an empty registry. Adds may create a missing file so
    an inbound STOP can establish the registry without losing the opt-out. An
    acknowledged update fsyncs both file contents and parent-directory metadata
    so replacing the registry is durable across a power-loss restart.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def get(self, phone_number: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load_state()[0].get(phone_number)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            entries, _removals = self._load_state()
            return [
                {"phone_number": phone, **entry}
                for phone, entry in sorted(entries.items())
            ]

    def add(
        self,
        phone_number: str,
        *,
        reason: str,
        source: str,
        source_message_id: str = "",
        carrier_code: str = "",
        triggered_at: str = "",
        manual_override: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                entries, removals = self._load_state()
            else:
                entries = {}
                removals = {}
            effective_triggered_at = triggered_at or utc_now()
            removed_at = removals.get(phone_number, "")
            evidence_time = _parse_timestamp(effective_triggered_at)
            removal_time = _parse_timestamp(removed_at)
            if not manual_override and removed_at and (
                evidence_time is None
                or removal_time is None
                or evidence_time <= removal_time
            ):
                return {
                    "phone_number": phone_number,
                    "suppressed": True,
                    "removed_at": removed_at,
                    "triggered_at": effective_triggered_at,
                }
            existing = entries.get(phone_number)
            effective_reason = reason or "do_not_text"
            if (
                isinstance(existing, dict)
                and existing.get("reason") == "opt_out"
                and effective_reason != "opt_out"
            ):
                effective_reason = "opt_out"
            entry = {
                "reason": effective_reason,
                "source": source or "unknown",
                "added_at": (
                    existing.get("added_at") if isinstance(existing, dict) else None
                )
                or utc_now(),
                "source_message_id": source_message_id,
                "carrier_code": carrier_code,
                "triggered_at": effective_triggered_at,
            }
            entries[phone_number] = entry
            self._write_state(entries, removals)
            return {"phone_number": phone_number, **entry}

    def remove(self, phone_number: str, *, removed_at: str = "") -> bool:
        with self._lock:
            entries, removals = self._load_state()
            removed = entries.pop(phone_number, None) is not None
            removals[phone_number] = removed_at or utc_now()
            self._write_state(entries, removals)
            return removed

    def _load_state(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DoNotTextUnavailableError(
                "do_not_text registry is missing or unreadable; refusing transmission"
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DoNotTextUnavailableError(
                "do_not_text registry is malformed; refusing transmission"
            ) from exc

        version = 1
        removals: Any = {}
        if isinstance(payload, dict) and "entries" in payload:
            version = payload.get("version", 1)
            entries = payload.get("entries")
            removals = payload.get("removals", {})
        elif isinstance(payload, dict):
            # Accept the original Geordi-side {"phone": metadata} draft shape.
            entries = payload
        elif isinstance(payload, list):
            # Accept a simple legacy list without weakening malformed-file checks.
            entries = {str(phone): {} for phone in payload}
        else:
            entries = None
        if (
            version != 1
            or not isinstance(entries, dict)
            or not isinstance(removals, dict)
        ):
            raise DoNotTextUnavailableError(
                "do_not_text registry has an unsupported shape; refusing transmission"
            )

        normalized: dict[str, dict[str, Any]] = {}
        for phone, entry in entries.items():
            if not isinstance(phone, str):
                raise DoNotTextUnavailableError(
                    "do_not_text registry contains an invalid phone key"
                )
            if isinstance(entry, str):
                entry = {"reason": entry}
            if not isinstance(entry, dict):
                raise DoNotTextUnavailableError(
                    "do_not_text registry contains invalid entry metadata"
                )
            normalized_phone = _normalize_phone_key(phone)
            if normalized_phone in normalized:
                raise DoNotTextUnavailableError(
                    "do_not_text registry contains duplicate normalized phone keys"
                )
            normalized[normalized_phone] = dict(entry)
        normalized_removals: dict[str, str] = {}
        for phone, removed_at in removals.items():
            if not isinstance(phone, str) or not isinstance(removed_at, str):
                raise DoNotTextUnavailableError(
                    "do_not_text registry contains invalid removal metadata"
                )
            normalized_phone = _normalize_phone_key(phone)
            if (
                normalized_phone in normalized_removals
                or _parse_timestamp(removed_at) is None
            ):
                raise DoNotTextUnavailableError(
                    "do_not_text registry contains invalid removal metadata"
                )
            normalized_removals[normalized_phone] = removed_at
        return normalized, normalized_removals

    def _write_state(
        self,
        entries: dict[str, dict[str, Any]],
        removals: dict[str, str],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {"version": 1, "entries": entries, "removals": removals},
            indent=2,
            sort_keys=True,
        ) + "\n"
        temp_path = ""
        directory_fd = -1
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
            temp_path = ""
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            os.fsync(directory_fd)
        except OSError as exc:
            raise DoNotTextUnavailableError(
                "do_not_text registry could not be updated"
            ) from exc
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass


class BridgeStore:
    """SQLite audit log, outbound index, and durable inbound wake queue."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _create_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sms_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    phone_number TEXT NOT NULL,
                    text TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    recipient_timezone TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    message_status TEXT NOT NULL DEFAULT '',
                    delivery_error_code TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    may_have_completed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS outbound_messages (
                    message_id TEXT PRIMARY KEY,
                    phone_number TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    message_status TEXT NOT NULL,
                    delivery_error_code TEXT NOT NULL DEFAULT '',
                    delivery_notified_at TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT NOT NULL,
                    status_failure_count INTEGER NOT NULL DEFAULT 0,
                    status_next_retry_at TEXT NOT NULL DEFAULT '',
                    status_last_error TEXT NOT NULL DEFAULT '',
                    status_quarantined_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS outbound_phone_time
                    ON outbound_messages(phone_number, sent_at DESC);
                CREATE TABLE IF NOT EXISTS inbound_messages (
                    message_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    threading_hint_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    delivered_at TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS inbound_rejections (
                    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    rejected_at TEXT NOT NULL,
                    error TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS inbound_rejection_message
                    ON inbound_rejections(message_id);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS side_effect_events (
                    event_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT '',
                    compliance_phone TEXT NOT NULL DEFAULT '',
                    compliance_applied_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS message_status_checks (
                    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS status_check_message_time
                    ON message_status_checks(message_id, checked_at);
                CREATE TABLE IF NOT EXISTS consumed_request_ids (
                    request_id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    method TEXT NOT NULL,
                    target TEXT NOT NULL,
                    consumed_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in self._db.execute(
                    "PRAGMA table_info(outbound_messages)"
                ).fetchall()
            }
            if "delivery_notified_at" not in columns:
                self._db.execute(
                    "ALTER TABLE outbound_messages "
                    "ADD COLUMN delivery_notified_at TEXT NOT NULL DEFAULT ''"
                )
            outbound_migrations = {
                "status_failure_count": (
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "status_next_retry_at": "TEXT NOT NULL DEFAULT ''",
                "status_last_error": "TEXT NOT NULL DEFAULT ''",
                "status_quarantined_at": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in outbound_migrations.items():
                if column not in columns:
                    self._db.execute(
                        f"ALTER TABLE outbound_messages "
                        f"ADD COLUMN {column} {definition}"
                    )
            attempt_columns = {
                row["name"]
                for row in self._db.execute(
                    "PRAGMA table_info(sms_attempts)"
                ).fetchall()
            }
            if "may_have_completed" not in attempt_columns:
                self._db.execute(
                    "ALTER TABLE sms_attempts "
                    "ADD COLUMN may_have_completed "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            side_effect_columns = {
                row["name"]
                for row in self._db.execute(
                    "PRAGMA table_info(side_effect_events)"
                ).fetchall()
            }
            if "compliance_phone" not in side_effect_columns:
                self._db.execute(
                    "ALTER TABLE side_effect_events "
                    "ADD COLUMN compliance_phone TEXT NOT NULL DEFAULT ''"
                )
            if "compliance_applied_at" not in side_effect_columns:
                self._db.execute(
                    "ALTER TABLE side_effect_events "
                    "ADD COLUMN compliance_applied_at TEXT NOT NULL DEFAULT ''"
                )

    def begin_attempt(
        self,
        *,
        phone_number: str,
        text: str,
        template_id: str,
        recipient_timezone: str,
    ) -> str:
        attempt_id = str(uuid.uuid4())
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO sms_attempts
                    (attempt_id, phone_number, text, template_id,
                     recipient_timezone, requested_at, outcome)
                VALUES (?, ?, ?, ?, ?, ?, 'checking')
                """,
                (
                    attempt_id,
                    phone_number,
                    text,
                    template_id,
                    recipient_timezone,
                    utc_now(),
                ),
            )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        outcome: str,
        error: str = "",
        message: dict[str, Any] | None = None,
        may_have_completed: bool = False,
    ) -> None:
        message = message or {}
        message_id = str(message.get("id") or "")
        status = str(message.get("messageStatus") or "")
        error_code = str(
            message.get("deliveryErrorCode")
            or message.get("deliveryErrorCodeId")
            or ""
        )
        completed_at = utc_now()
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE sms_attempts
                SET outcome=?, error=?, message_id=?, message_status=?,
                    delivery_error_code=?, completed_at=?,
                    may_have_completed=?
                WHERE attempt_id=?
                """,
                (
                    outcome,
                    error,
                    message_id,
                    status,
                    error_code,
                    completed_at,
                    int(may_have_completed),
                    attempt_id,
                ),
            )
            if message_id:
                row = self._db.execute(
                    "SELECT phone_number, template_id FROM sms_attempts "
                    "WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                self._db.execute(
                    """
                    INSERT INTO outbound_messages
                        (message_id, phone_number, template_id, sent_at,
                         message_status, delivery_error_code, last_checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        message_status=excluded.message_status,
                        delivery_error_code=excluded.delivery_error_code,
                        last_checked_at=excluded.last_checked_at
                    """,
                    (
                        message_id,
                        row["phone_number"],
                        row["template_id"],
                        completed_at,
                        status,
                        error_code,
                        completed_at,
                    ),
                )

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sms_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_outbound(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM outbound_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def uncertain_attempt_for_phone(self, phone_number: str) -> str:
        with self._lock:
            row = self._db.execute(
                """
                SELECT attempt_id FROM sms_attempts
                WHERE phone_number=? AND may_have_completed=1
                ORDER BY requested_at ASC LIMIT 1
                """,
                (phone_number,),
            ).fetchone()
        return str(row["attempt_id"]) if row else ""

    def unresolved_attempt_for_phone(
        self,
        phone_number: str,
        *,
        exclude_attempt_id: str = "",
    ) -> str:
        with self._lock:
            row = self._db.execute(
                """
                SELECT attempt_id FROM sms_attempts
                WHERE phone_number=? AND attempt_id!=?
                  AND (outcome='checking' OR may_have_completed=1)
                ORDER BY requested_at ASC LIMIT 1
                """,
                (phone_number, exclude_attempt_id),
            ).fetchone()
        return str(row["attempt_id"]) if row else ""

    def record_status_check(
        self, message_id: str, *, outcome: str, error: str = ""
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO message_status_checks
                    (message_id, checked_at, outcome, error)
                VALUES (?, ?, ?, ?)
                """,
                (message_id, utc_now(), outcome, error[:500]),
            )

    def status_checks(self, message_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM message_status_checks
                WHERE message_id=?
                ORDER BY check_id ASC
                """,
                (message_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def consume_request_id(
        self,
        request_id: str,
        *,
        agent_name: str,
        method: str,
        target: str,
    ) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO consumed_request_ids
                    (request_id, agent_name, method, target, consumed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, agent_name, method, target, utc_now()),
            )
        return cursor.rowcount == 1

    def update_message_status(self, message: dict[str, Any]) -> None:
        message_id = str(message.get("id") or "")
        if not message_id:
            return
        status = str(message.get("messageStatus") or "")
        error_code = str(
            message.get("deliveryErrorCode")
            or message.get("deliveryErrorCodeId")
            or ""
        )
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE outbound_messages
                SET message_status=?, delivery_error_code=?, last_checked_at=?,
                    status_failure_count=0, status_next_retry_at='',
                    status_last_error='', status_quarantined_at=''
                WHERE message_id=?
                """,
                (status, error_code, utc_now(), message_id),
            )
            self._db.execute(
                """
                UPDATE sms_attempts
                SET message_status=?, delivery_error_code=?
                WHERE message_id=?
                """,
                (status, error_code, message_id),
            )

    def recent_outbound(
        self, phone_number: str, *, since: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """
                SELECT message_id, sent_at, template_id, message_status
                FROM outbound_messages
                WHERE phone_number=? AND sent_at>=?
                ORDER BY sent_at DESC LIMIT 1
                """,
                (phone_number, since),
            ).fetchone()
        return dict(row) if row else None

    def pending_status_ids(
        self, limit: int = 100, *, now: str = ""
    ) -> list[str]:
        current = now or utc_now()
        with self._lock:
            rows = self._db.execute(
                """
                SELECT message_id FROM outbound_messages
                WHERE (
                    message_status NOT IN ('Delivered', 'DeliveryFailed',
                                           'SendingFailed')
                    OR (
                        message_status IN ('DeliveryFailed', 'SendingFailed')
                        AND delivery_notified_at=''
                    )
                )
                  AND status_quarantined_at=''
                  AND (status_next_retry_at='' OR status_next_retry_at<=?)
                ORDER BY sent_at ASC LIMIT ?
                """,
                (current, limit),
            ).fetchall()
        return [row["message_id"] for row in rows]

    def record_status_failure(
        self,
        message_id: str,
        *,
        error: str,
        next_retry_at: str,
        quarantine: bool,
    ) -> None:
        quarantined_at = utc_now() if quarantine else ""
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE outbound_messages
                SET status_failure_count=status_failure_count+1,
                    status_next_retry_at=?,
                    status_last_error=?,
                    status_quarantined_at=CASE
                        WHEN ?!='' THEN ?
                        ELSE status_quarantined_at
                    END,
                    last_checked_at=?
                WHERE message_id=?
                """,
                (
                    "" if quarantine else next_retry_at,
                    error[:500],
                    quarantined_at,
                    quarantined_at,
                    utc_now(),
                    message_id,
                ),
            )
            self._db.execute(
                """
                INSERT INTO message_status_checks
                    (message_id, checked_at, outcome, error)
                VALUES (?, ?, ?, ?)
                """,
                (
                    message_id,
                    utc_now(),
                    "quarantined" if quarantine else "retry_pending",
                    error[:500],
                ),
            )

    def quarantined_status_for_phone(self, phone_number: str) -> str:
        with self._lock:
            row = self._db.execute(
                """
                SELECT message_id FROM outbound_messages
                WHERE phone_number=? AND status_quarantined_at!=''
                ORDER BY status_quarantined_at ASC LIMIT 1
                """,
                (phone_number,),
            ).fetchone()
        return str(row["message_id"]) if row else ""

    def mark_delivery_notified(self, message_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE outbound_messages
                SET delivery_notified_at=?
                WHERE message_id=?
                """,
                (utc_now(), message_id),
            )

    def side_effects_required(
        self, event_key: str, *, compliance_phone: str = ""
    ) -> bool:
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO side_effect_events
                    (event_key, state, first_seen_at, compliance_phone)
                VALUES (?, 'pending', ?, ?)
                """,
                (event_key, utc_now(), compliance_phone),
            )
            if compliance_phone:
                self._db.execute(
                    """
                    UPDATE side_effect_events
                    SET compliance_phone=?
                    WHERE event_key=? AND compliance_phone=''
                    """,
                    (compliance_phone, event_key),
                )
            row = self._db.execute(
                "SELECT state FROM side_effect_events WHERE event_key=?",
                (event_key,),
            ).fetchone()
        return bool(row and row["state"] != "applied")

    def mark_compliance_applied(self, event_key: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE side_effect_events
                SET compliance_applied_at=?
                WHERE event_key=?
                """,
                (utc_now(), event_key),
            )

    def pending_compliance_event(self, phone_number: str) -> str:
        with self._lock:
            row = self._db.execute(
                """
                SELECT event_key FROM side_effect_events
                WHERE compliance_phone=?
                  AND compliance_applied_at=''
                  AND state!='applied'
                ORDER BY first_seen_at ASC LIMIT 1
                """,
                (phone_number,),
            ).fetchone()
        return str(row["event_key"]) if row else ""

    def mark_side_effects_applied(
        self, event_key: str, *, delivery_message_id: str = ""
    ) -> None:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE side_effect_events
                SET state='applied', applied_at=?,
                    compliance_applied_at=CASE
                        WHEN compliance_phone!='' AND compliance_applied_at=''
                        THEN ?
                        ELSE compliance_applied_at
                    END
                WHERE event_key=?
                """,
                (now, now, event_key),
            )
            if delivery_message_id:
                self._db.execute(
                    """
                    UPDATE outbound_messages
                    SET delivery_notified_at=?
                    WHERE message_id=?
                    """,
                    (now, delivery_message_id),
                )

    def enqueue_inbound(
        self,
        *,
        message_id: str,
        payload: dict[str, Any],
        classification: str,
        threading_hint: dict[str, Any],
        delivered: bool = False,
    ) -> bool:
        now = utc_now()
        with self._lock, self._db:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO inbound_messages
                    (message_id, payload_json, classification,
                     threading_hint_json, state, received_at, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    json.dumps(payload, sort_keys=True),
                    classification,
                    json.dumps(threading_hint, sort_keys=True),
                    "delivered" if delivered else "pending",
                    now,
                    now if delivered else "",
                ),
            )
        return cursor.rowcount == 1

    def pending_inbound(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM inbound_messages
                WHERE state='pending'
                ORDER BY received_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["threading_hint"] = json.loads(
                item.pop("threading_hint_json")
            )
            result.append(item)
        return result

    def record_inbound_delivery(
        self, message_id: str, *, delivered: bool, error: str = ""
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE inbound_messages
                SET state=?, delivered_at=?, attempts=attempts+1, last_error=?
                WHERE message_id=?
                """,
                (
                    "delivered" if delivered else "pending",
                    utc_now() if delivered else "",
                    error[:500],
                    message_id,
                ),
            )

    def record_inbound_rejection(self, message_id: str, *, error: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO inbound_rejections
                    (message_id, rejected_at, error)
                VALUES (?, ?, ?)
                """,
                (message_id[:128], utc_now(), error[:500]),
            )

    def inbound_rejections(self, message_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM inbound_rejections
                WHERE message_id=?
                ORDER BY rejection_id ASC
                """,
                (message_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_metadata(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
