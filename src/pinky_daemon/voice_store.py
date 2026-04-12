"""Voice Store — SQLite-backed store for voice call state.

Supports outbound AI phone calls (Barsik → Haiku via ConversationRelay)
and inbound AI voicemail. Four tables:

  - call_request: proposal + approval lifecycle (outbound only)
  - voice_call_session: one row per Twilio call (outbound or inbound)
  - voice_call_event: normalized event log per call
  - voice_call_artifact: post-call outputs (transcript, outcome, etc.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import sys
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── E.164 normalization ──────────────────────────────────────────────────────


def _normalize_e164(phone: str) -> str:
    """Normalize a phone number to E.164 format.

    Strips non-digit chars (except leading +), prepends +1 for 10-digit US numbers.
    Raises ValueError if result doesn't look like a valid E.164 number.
    """
    stripped = re.sub(r"[^\d+]", "", phone)
    if stripped.startswith("+"):
        digits = stripped[1:]
        result = f"+{digits}"
    else:
        digits = stripped
        if len(digits) == 10:
            result = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            result = f"+{digits}"
        else:
            result = f"+{digits}"

    if not re.match(r"^\+\d{10,15}$", result):
        raise ValueError(f"Invalid phone number: {phone!r} → {result!r}")
    return result


# ── HMAC approval tokens ─────────────────────────────────────────────────────

_APPROVAL_TOKEN_TTL = 3600  # 1 hour


def make_approval_token(request_id: str, secret: str) -> str:
    """Generate HMAC-signed token encoding request_id + expiry."""
    expiry = str(int(time.time() + _APPROVAL_TOKEN_TTL))
    payload = f"{request_id}:{expiry}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    raw = f"{payload}:{sig}"
    return urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def verify_approval_token(token: str, secret: str) -> str | None:
    """Verify token and return request_id if valid and not expired, else None."""
    try:
        padded = token + "=" * (4 - len(token) % 4)
        raw = urlsafe_b64decode(padded.encode()).decode()
        request_id, expiry_str, sig = raw.rsplit(":", 2)
        payload = f"{request_id}:{expiry_str}"
        expected = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            return None
        if time.time() > float(expiry_str):
            return None
        return request_id
    except Exception:
        return None


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class CallRequest:
    """Outbound call proposal — tracks approval lifecycle before Twilio is contacted."""

    id: str = ""
    idempotency_key: str = ""
    requested_by_agent: str = ""
    authorized_by: str = ""
    authorized_at: float = 0.0
    target_name: str = ""
    target_phone: str = ""
    goal: str = ""
    context: str = ""  # JSON blob
    fallback_behavior: str = "notify_brad_if_no_answer"
    approval_state: str = "pending_approval"
    max_duration_sec: int = 300
    created_at: float = 0.0
    expires_at: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "requested_by_agent": self.requested_by_agent,
            "authorized_by": self.authorized_by,
            "authorized_at": self.authorized_at,
            "target_name": self.target_name,
            "target_phone": self.target_phone,
            "goal": self.goal,
            "context": json.loads(self.context) if self.context else {},
            "fallback_behavior": self.fallback_behavior,
            "approval_state": self.approval_state,
            "max_duration_sec": self.max_duration_sec,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        return d


@dataclass
class VoiceCallSession:
    """One row per actual Twilio call (outbound or inbound)."""

    id: str = ""
    call_request_id: str = ""
    call_sid: str = ""
    cr_session_id: str = ""
    voice_model: str = ""
    agent_name: str = ""
    direction: str = ""
    from_number: str = ""
    to_number: str = ""
    status: str = "queued"
    disclosure_completed_at: float = 0.0
    consent_or_policy_flags: str = ""
    failure_reason: str = ""
    started_at: float = 0.0
    answered_at: float = 0.0
    ended_at: float = 0.0
    max_duration_sec: int = 300

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "call_request_id": self.call_request_id or None,
            "call_sid": self.call_sid,
            "cr_session_id": self.cr_session_id,
            "voice_model": self.voice_model,
            "agent_name": self.agent_name,
            "direction": self.direction,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "status": self.status,
            "disclosure_completed_at": self.disclosure_completed_at or None,
            "consent_or_policy_flags": self.consent_or_policy_flags,
            "failure_reason": self.failure_reason,
            "started_at": self.started_at or None,
            "answered_at": self.answered_at or None,
            "ended_at": self.ended_at or None,
            "max_duration_sec": self.max_duration_sec,
        }


@dataclass
class VoiceCallEvent:
    """Normalized event log entry for a voice call."""

    id: str = ""
    call_session_id: str = ""
    call_sid: str = ""
    cr_session_id: str = ""
    event_type: str = ""
    role: str = ""
    content: str = ""
    metadata: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "call_session_id": self.call_session_id,
            "call_sid": self.call_sid,
            "event_type": self.event_type,
            "role": self.role,
            "content": self.content,
            "metadata": json.loads(self.metadata) if self.metadata else {},
            "ts": self.ts,
        }


@dataclass
class VoiceCallArtifact:
    """Post-call outputs: transcript, summary, extracted outcome."""

    id: str = ""
    call_sid: str = ""
    call_session_id: str = ""
    transcript_url: str = ""
    summary: str = ""
    extracted_outcome: str = ""
    caller_name: str = ""
    caller_purpose: str = ""
    notification_sent_at: float = 0.0
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "call_sid": self.call_sid,
            "call_session_id": self.call_session_id,
            "transcript_url": self.transcript_url,
            "summary": self.summary,
            "extracted_outcome": (
                json.loads(self.extracted_outcome) if self.extracted_outcome else {}
            ),
            "caller_name": self.caller_name,
            "caller_purpose": self.caller_purpose,
            "notification_sent_at": self.notification_sent_at or None,
            "created_at": self.created_at,
        }


# ── Store ─────────────────────────────────────────────────────────────────────

_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "call_request": [],
    "voice_call_session": [],
    "voice_call_event": [],
    "voice_call_artifact": [],
}


class VoiceStore:
    """SQLite-backed store for voice call state."""

    def __init__(self, db_path: str = "data/voice_calls.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.row_factory = sqlite3.Row
        self._init_tables()
        self._migrate()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS call_request (
                id                  TEXT PRIMARY KEY,
                idempotency_key     TEXT UNIQUE NOT NULL,
                requested_by_agent  TEXT NOT NULL,
                authorized_by       TEXT NOT NULL DEFAULT '',
                authorized_at       REAL NOT NULL DEFAULT 0,
                target_name         TEXT NOT NULL,
                target_phone        TEXT NOT NULL,
                goal                TEXT NOT NULL,
                context             TEXT NOT NULL DEFAULT '{}',
                fallback_behavior   TEXT NOT NULL DEFAULT 'notify_brad_if_no_answer',
                approval_state      TEXT NOT NULL DEFAULT 'pending_approval',
                max_duration_sec    INTEGER NOT NULL DEFAULT 300,
                created_at          REAL NOT NULL,
                expires_at          REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_cr_approval_state
                ON call_request(approval_state);

            CREATE INDEX IF NOT EXISTS idx_cr_agent
                ON call_request(requested_by_agent);

            CREATE TABLE IF NOT EXISTS voice_call_session (
                id                      TEXT PRIMARY KEY,
                call_request_id         TEXT REFERENCES call_request(id),
                call_sid                TEXT UNIQUE NOT NULL,
                cr_session_id           TEXT NOT NULL DEFAULT '',
                voice_model             TEXT NOT NULL DEFAULT '',
                agent_name              TEXT NOT NULL,
                direction               TEXT NOT NULL,
                from_number             TEXT NOT NULL,
                to_number               TEXT NOT NULL,
                status                  TEXT NOT NULL DEFAULT 'queued',
                disclosure_completed_at REAL NOT NULL DEFAULT 0,
                consent_or_policy_flags TEXT NOT NULL DEFAULT '',
                failure_reason          TEXT NOT NULL DEFAULT '',
                started_at              REAL NOT NULL DEFAULT 0,
                answered_at             REAL NOT NULL DEFAULT 0,
                ended_at                REAL NOT NULL DEFAULT 0,
                max_duration_sec        INTEGER NOT NULL DEFAULT 300,
                UNIQUE (call_request_id)
            );

            CREATE INDEX IF NOT EXISTS idx_vcs_call_sid
                ON voice_call_session(call_sid);

            CREATE INDEX IF NOT EXISTS idx_vcs_direction
                ON voice_call_session(direction);

            CREATE TABLE IF NOT EXISTS voice_call_event (
                id                TEXT PRIMARY KEY,
                call_session_id   TEXT NOT NULL REFERENCES voice_call_session(id),
                call_sid          TEXT NOT NULL,
                cr_session_id     TEXT NOT NULL DEFAULT '',
                event_type        TEXT NOT NULL,
                role              TEXT NOT NULL DEFAULT '',
                content           TEXT NOT NULL DEFAULT '',
                metadata          TEXT NOT NULL DEFAULT '',
                ts                REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_vce_session
                ON voice_call_event(call_session_id);

            CREATE INDEX IF NOT EXISTS idx_vce_call_sid
                ON voice_call_event(call_sid);

            CREATE TABLE IF NOT EXISTS voice_call_artifact (
                id                  TEXT PRIMARY KEY,
                call_sid            TEXT UNIQUE NOT NULL,
                call_session_id     TEXT NOT NULL REFERENCES voice_call_session(id),
                transcript_url      TEXT NOT NULL DEFAULT '',
                summary             TEXT NOT NULL DEFAULT '',
                extracted_outcome   TEXT NOT NULL DEFAULT '',
                caller_name         TEXT NOT NULL DEFAULT '',
                caller_purpose      TEXT NOT NULL DEFAULT '',
                notification_sent_at REAL NOT NULL DEFAULT 0,
                created_at          REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_vca_session
                ON voice_call_artifact(call_session_id);
        """)
        self._db.commit()

    def _migrate(self) -> None:
        """Add missing columns following the _ensure_columns pattern."""
        for table, migrations in _MIGRATIONS.items():
            existing = {
                row[1]
                for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for col, typedef in migrations:
                if col not in existing:
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {typedef}"
                    )
                    _log(f"voice_store: migrated — added {col} to {table}")
        self._db.commit()

    # ── Row helpers ───────────────────────────────────────────────

    def _row_to_call_request(self, row: sqlite3.Row) -> CallRequest:
        return CallRequest(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            requested_by_agent=row["requested_by_agent"],
            authorized_by=row["authorized_by"],
            authorized_at=row["authorized_at"],
            target_name=row["target_name"],
            target_phone=row["target_phone"],
            goal=row["goal"],
            context=row["context"],
            fallback_behavior=row["fallback_behavior"],
            approval_state=row["approval_state"],
            max_duration_sec=row["max_duration_sec"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def _row_to_session(self, row: sqlite3.Row) -> VoiceCallSession:
        return VoiceCallSession(
            id=row["id"],
            call_request_id=row["call_request_id"] or "",
            call_sid=row["call_sid"],
            cr_session_id=row["cr_session_id"],
            voice_model=row["voice_model"],
            agent_name=row["agent_name"],
            direction=row["direction"],
            from_number=row["from_number"],
            to_number=row["to_number"],
            status=row["status"],
            disclosure_completed_at=row["disclosure_completed_at"],
            consent_or_policy_flags=row["consent_or_policy_flags"],
            failure_reason=row["failure_reason"],
            started_at=row["started_at"],
            answered_at=row["answered_at"],
            ended_at=row["ended_at"],
            max_duration_sec=row["max_duration_sec"],
        )

    def _row_to_event(self, row: sqlite3.Row) -> VoiceCallEvent:
        return VoiceCallEvent(
            id=row["id"],
            call_session_id=row["call_session_id"],
            call_sid=row["call_sid"],
            cr_session_id=row["cr_session_id"],
            event_type=row["event_type"],
            role=row["role"],
            content=row["content"],
            metadata=row["metadata"],
            ts=row["ts"],
        )

    def _row_to_artifact(self, row: sqlite3.Row) -> VoiceCallArtifact:
        return VoiceCallArtifact(
            id=row["id"],
            call_sid=row["call_sid"],
            call_session_id=row["call_session_id"],
            transcript_url=row["transcript_url"],
            summary=row["summary"],
            extracted_outcome=row["extracted_outcome"],
            caller_name=row["caller_name"],
            caller_purpose=row["caller_purpose"],
            notification_sent_at=row["notification_sent_at"],
            created_at=row["created_at"],
        )

    # ── Call Request CRUD ─────────────────────────────────────────

    def create_call_request(
        self,
        *,
        requested_by_agent: str,
        target_name: str,
        target_phone: str,
        goal: str,
        context: dict | None = None,
        fallback_behavior: str = "notify_brad_if_no_answer",
        max_duration_sec: int = 300,
    ) -> CallRequest:
        """Create a new call request with pending_approval state."""
        request_id = str(uuid.uuid4())
        idempotency_key = str(uuid.uuid4())
        phone = _normalize_e164(target_phone)
        now = time.time()
        expires_at = now + _APPROVAL_TOKEN_TTL

        self._db.execute(
            """
            INSERT INTO call_request (
                id, idempotency_key, requested_by_agent, target_name, target_phone,
                goal, context, fallback_behavior, max_duration_sec, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id, idempotency_key, requested_by_agent, target_name, phone,
                goal, json.dumps(context or {}), fallback_behavior, max_duration_sec,
                now, expires_at,
            ),
        )
        self._db.commit()
        return self.get_call_request(request_id)  # type: ignore[return-value]

    def get_call_request(self, request_id: str) -> CallRequest | None:
        row = self._db.execute(
            "SELECT * FROM call_request WHERE id = ?", (request_id,)
        ).fetchone()
        return self._row_to_call_request(row) if row else None

    def update_call_request_state(
        self,
        request_id: str,
        *,
        approval_state: str,
        authorized_by: str = "",
        authorized_at: float = 0.0,
    ) -> CallRequest | None:
        """Update approval state. Idempotent — returns current state if already set."""
        existing = self.get_call_request(request_id)
        if not existing:
            return None

        # Idempotent: if already in this state, just return it
        if existing.approval_state == approval_state:
            return existing

        # Only write authorization audit fields on actual approval
        if approval_state == "approved":
            self._db.execute(
                """
                UPDATE call_request
                SET approval_state = ?, authorized_by = ?, authorized_at = ?
                WHERE id = ?
                """,
                (approval_state, authorized_by, authorized_at or time.time(), request_id),
            )
        else:
            self._db.execute(
                "UPDATE call_request SET approval_state = ? WHERE id = ?",
                (approval_state, request_id),
            )
        self._db.commit()
        return self.get_call_request(request_id)

    def cancel_call_request(self, request_id: str) -> CallRequest | None:
        return self.update_call_request_state(
            request_id, approval_state="cancelled"
        )

    def list_call_requests(
        self, *, agent_name: str = "", state: str = "", limit: int = 50
    ) -> list[CallRequest]:
        sql = "SELECT * FROM call_request WHERE 1=1"
        params: list = []
        if agent_name:
            sql += " AND requested_by_agent = ?"
            params.append(agent_name)
        if state:
            sql += " AND approval_state = ?"
            params.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(sql, params).fetchall()
        return [self._row_to_call_request(r) for r in rows]

    # ── Voice Call Session CRUD ───────────────────────────────────

    def create_session(
        self,
        *,
        call_request_id: str = "",
        call_sid: str,
        voice_model: str = "claude-haiku-4-5",
        agent_name: str,
        direction: str,
        from_number: str,
        to_number: str,
        max_duration_sec: int = 300,
    ) -> VoiceCallSession:
        session_id = str(uuid.uuid4())
        self._db.execute(
            """
            INSERT INTO voice_call_session (
                id, call_request_id, call_sid, voice_model, agent_name,
                direction, from_number, to_number, max_duration_sec, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                call_request_id or None,
                call_sid,
                voice_model,
                agent_name,
                direction,
                from_number,
                to_number,
                max_duration_sec,
                time.time(),
            ),
        )
        self._db.commit()
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> VoiceCallSession | None:
        row = self._db.execute(
            "SELECT * FROM voice_call_session WHERE id = ?", (session_id,)
        ).fetchone()
        return self._row_to_session(row) if row else None

    def get_session_by_call_sid(self, call_sid: str) -> VoiceCallSession | None:
        row = self._db.execute(
            "SELECT * FROM voice_call_session WHERE call_sid = ?", (call_sid,)
        ).fetchone()
        return self._row_to_session(row) if row else None

    def update_session(self, session_id: str, **kwargs: object) -> None:
        """Update arbitrary fields on a voice call session."""
        if not kwargs:
            return
        allowed = {
            "call_sid", "cr_session_id", "voice_model", "status",
            "disclosure_completed_at", "consent_or_policy_flags",
            "failure_reason", "answered_at", "ended_at",
        }
        filtered = {k: v for k, v in kwargs.items() if k in allowed}
        if not filtered:
            return
        sets = ", ".join(f"{k} = ?" for k in filtered)
        vals = list(filtered.values()) + [session_id]
        self._db.execute(
            f"UPDATE voice_call_session SET {sets} WHERE id = ?", vals
        )
        self._db.commit()

    def list_sessions(
        self,
        *,
        agent_name: str = "",
        direction: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[VoiceCallSession]:
        sql = "SELECT * FROM voice_call_session WHERE 1=1"
        params: list = []
        if agent_name:
            sql += " AND agent_name = ?"
            params.append(agent_name)
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(sql, params).fetchall()
        return [self._row_to_session(r) for r in rows]

    # ── Voice Call Event CRUD ─────────────────────────────────────

    def log_event(
        self,
        *,
        call_session_id: str,
        call_sid: str,
        event_type: str,
        role: str = "",
        content: str = "",
        metadata: dict | None = None,
        cr_session_id: str = "",
    ) -> VoiceCallEvent:
        event_id = str(uuid.uuid4())
        now = time.time()
        self._db.execute(
            """
            INSERT INTO voice_call_event (
                id, call_session_id, call_sid, cr_session_id,
                event_type, role, content, metadata, ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, call_session_id, call_sid, cr_session_id,
                event_type, role, content, json.dumps(metadata or {}), now,
            ),
        )
        self._db.commit()
        return VoiceCallEvent(
            id=event_id, call_session_id=call_session_id, call_sid=call_sid,
            cr_session_id=cr_session_id, event_type=event_type, role=role,
            content=content, metadata=json.dumps(metadata or {}), ts=now,
        )

    def get_events(
        self, call_session_id: str, *, limit: int = 500
    ) -> list[VoiceCallEvent]:
        rows = self._db.execute(
            "SELECT * FROM voice_call_event WHERE call_session_id = ? ORDER BY ts LIMIT ?",
            (call_session_id, limit),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ── Voice Call Artifact CRUD ──────────────────────────────────

    def save_artifact(
        self,
        *,
        call_sid: str,
        call_session_id: str,
        transcript_url: str = "",
        summary: str = "",
        extracted_outcome: dict | None = None,
        caller_name: str = "",
        caller_purpose: str = "",
    ) -> VoiceCallArtifact:
        artifact_id = str(uuid.uuid4())
        now = time.time()
        self._db.execute(
            """
            INSERT INTO voice_call_artifact (
                id, call_sid, call_session_id, transcript_url, summary,
                extracted_outcome, caller_name, caller_purpose, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id, call_sid, call_session_id, transcript_url,
                summary, json.dumps(extracted_outcome or {}),
                caller_name, caller_purpose, now,
            ),
        )
        self._db.commit()
        return VoiceCallArtifact(
            id=artifact_id, call_sid=call_sid, call_session_id=call_session_id,
            transcript_url=transcript_url, summary=summary,
            extracted_outcome=json.dumps(extracted_outcome or {}),
            caller_name=caller_name, caller_purpose=caller_purpose, created_at=now,
        )

    def get_artifact_by_call_sid(self, call_sid: str) -> VoiceCallArtifact | None:
        row = self._db.execute(
            "SELECT * FROM voice_call_artifact WHERE call_sid = ?", (call_sid,)
        ).fetchone()
        return self._row_to_artifact(row) if row else None

    def update_artifact(self, artifact_id: str, **kwargs: object) -> None:
        """Update arbitrary fields on an artifact."""
        allowed = {
            "transcript_url", "summary", "extracted_outcome",
            "caller_name", "caller_purpose", "notification_sent_at",
        }
        filtered = {k: v for k, v in kwargs.items() if k in allowed}
        if not filtered:
            return
        sets = ", ".join(f"{k} = ?" for k in filtered)
        vals = list(filtered.values()) + [artifact_id]
        self._db.execute(
            f"UPDATE voice_call_artifact SET {sets} WHERE id = ?", vals
        )
        self._db.commit()
