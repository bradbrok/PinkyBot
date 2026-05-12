"""SQLite registry for service-auth public keys and enrollment tokens.

This is the registry/admin half of the per-agent asymmetric identity design.
It stores public service-auth keys, one-time enrollment tokens, lifecycle state,
and an audit trail. Private key storage belongs to the daemon-local signer
(PR-4), not this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pinky_identity.keys import (
    PublicKey,
    jwk_export,
    kid_from_public_key,
)
from pinky_identity.keys import (
    fingerprint as public_key_fingerprint,
)

DEFAULT_DB_PATH = "data/pinky_identity_registry.db"
DEFAULT_FLEET = "pos"
PURPOSE_SERVICE_AUTH = "service-auth"

KEY_PENDING = "pending"
KEY_ACTIVE = "active"
KEY_RETIRED = "retired"
KEY_REVOKED = "revoked"
KEY_STATUSES = frozenset({KEY_PENDING, KEY_ACTIVE, KEY_RETIRED, KEY_REVOKED})

TOKEN_ACTIVE = "active"
TOKEN_USED = "used"
TOKEN_REVOKED = "revoked"
TOKEN_STATUSES = frozenset({TOKEN_ACTIVE, TOKEN_USED, TOKEN_REVOKED})

DEFAULT_TOKEN_TTL_SECONDS = 15 * 60


class IdentityRegistryError(ValueError):
    """Raised when a registry operation is invalid."""


def _now() -> float:
    return time.time()


def _json_dumps(data: dict[str, Any] | None) -> str:
    return json.dumps(data or {}, sort_keys=True, separators=(",", ":"))


def _json_loads(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _clean_nonempty(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise IdentityRegistryError(f"{field} is required")
    return cleaned


def _clean_fleet_agent_purpose(
    *, fleet: str, agent_name: str, purpose: str
) -> tuple[str, str, str]:
    return (
        _clean_nonempty(fleet, "fleet"),
        _clean_nonempty(agent_name, "agent_name"),
        _clean_nonempty(purpose, "purpose"),
    )


def _clean_fingerprint(value: str) -> str:
    fp = _clean_nonempty(value, "public_key_fingerprint").lower()
    if len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        raise IdentityRegistryError("public_key_fingerprint must be 64 hex chars")
    return fp


@dataclass(frozen=True)
class IdentityKeyRecord:
    """A service-auth public key registered for one agent."""

    kid: str
    fleet: str
    agent_name: str
    purpose: str
    public_key: bytes
    public_jwk: dict[str, Any]
    fingerprint: str
    status: str
    created_at: float
    approved_at: float = 0.0
    approved_by: str = ""
    retired_at: float = 0.0
    revoked_at: float = 0.0
    revoked_by: str = ""
    revoke_reason: str = ""
    replaced_by_kid: str = ""
    not_before: float = 0.0
    not_after: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kid": self.kid,
            "fleet": self.fleet,
            "agent_name": self.agent_name,
            "purpose": self.purpose,
            "public_key_b64": _b64url_nopad(self.public_key),
            "public_jwk": dict(self.public_jwk),
            "fingerprint": self.fingerprint,
            "status": self.status,
            "created_at": self.created_at,
            "approved_at": self.approved_at or None,
            "approved_by": self.approved_by,
            "retired_at": self.retired_at or None,
            "revoked_at": self.revoked_at or None,
            "revoked_by": self.revoked_by,
            "revoke_reason": self.revoke_reason,
            "replaced_by_kid": self.replaced_by_kid,
            "not_before": self.not_before or None,
            "not_after": self.not_after or None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class EnrollmentTokenRecord:
    """One-time token binding a future public key to an agent identity."""

    token_id: str
    token_hash: str
    fleet: str
    agent_name: str
    purpose: str
    public_key_fingerprint: str
    status: str
    issued_by: str
    created_at: float
    expires_at: float
    used_at: float = 0.0
    revoked_at: float = 0.0
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    token: str = ""

    def to_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        out = {
            "token_id": self.token_id,
            "fleet": self.fleet,
            "agent_name": self.agent_name,
            "purpose": self.purpose,
            "public_key_fingerprint": self.public_key_fingerprint,
            "status": self.status,
            "issued_by": self.issued_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "used_at": self.used_at or None,
            "revoked_at": self.revoked_at or None,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }
        if include_token:
            out["token"] = self.token
        return out


@dataclass(frozen=True)
class IdentityAuditRecord:
    """Registry audit event."""

    id: int
    event_type: str
    fleet: str
    agent_name: str
    purpose: str
    kid: str
    fingerprint: str
    actor: str
    outcome: str
    reason: str
    metadata: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "fleet": self.fleet,
            "agent_name": self.agent_name,
            "purpose": self.purpose,
            "kid": self.kid,
            "fingerprint": self.fingerprint,
            "actor": self.actor,
            "outcome": self.outcome,
            "reason": self.reason,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


class IdentityRegistry:
    """SQLite-backed service-auth identity registry."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS identity_keys (
                kid          TEXT PRIMARY KEY,
                fleet        TEXT NOT NULL,
                agent_name   TEXT NOT NULL,
                purpose      TEXT NOT NULL,
                public_key   BLOB NOT NULL,
                public_jwk   TEXT NOT NULL DEFAULT '{}',
                fingerprint  TEXT NOT NULL UNIQUE,
                status       TEXT NOT NULL,
                created_at   REAL NOT NULL,
                approved_at  REAL NOT NULL DEFAULT 0,
                approved_by  TEXT NOT NULL DEFAULT '',
                retired_at   REAL NOT NULL DEFAULT 0,
                revoked_at   REAL NOT NULL DEFAULT 0,
                revoked_by   TEXT NOT NULL DEFAULT '',
                revoke_reason TEXT NOT NULL DEFAULT '',
                replaced_by_kid TEXT NOT NULL DEFAULT '',
                not_before   REAL NOT NULL DEFAULT 0,
                not_after    REAL NOT NULL DEFAULT 0,
                metadata     TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_identity_keys_agent
                ON identity_keys(fleet, agent_name, purpose, status);
            CREATE INDEX IF NOT EXISTS idx_identity_keys_fingerprint
                ON identity_keys(fingerprint);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_keys_one_active
                ON identity_keys(fleet, agent_name, purpose)
                WHERE status = 'active';

            CREATE TABLE IF NOT EXISTS enrollment_tokens (
                token_id     TEXT PRIMARY KEY,
                token_hash   TEXT NOT NULL UNIQUE,
                fleet        TEXT NOT NULL,
                agent_name   TEXT NOT NULL,
                purpose      TEXT NOT NULL,
                public_key_fingerprint TEXT NOT NULL,
                status       TEXT NOT NULL,
                issued_by    TEXT NOT NULL,
                created_at   REAL NOT NULL,
                expires_at   REAL NOT NULL,
                used_at      REAL NOT NULL DEFAULT 0,
                revoked_at   REAL NOT NULL DEFAULT 0,
                reason       TEXT NOT NULL DEFAULT '',
                metadata     TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_enrollment_tokens_agent
                ON enrollment_tokens(fleet, agent_name, purpose, status);
            CREATE INDEX IF NOT EXISTS idx_enrollment_tokens_fingerprint
                ON enrollment_tokens(public_key_fingerprint);

            CREATE TABLE IF NOT EXISTS identity_audit (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                fleet       TEXT NOT NULL DEFAULT '',
                agent_name  TEXT NOT NULL DEFAULT '',
                purpose     TEXT NOT NULL DEFAULT '',
                kid         TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                actor       TEXT NOT NULL DEFAULT '',
                outcome     TEXT NOT NULL DEFAULT 'success',
                reason      TEXT NOT NULL DEFAULT '',
                metadata    TEXT NOT NULL DEFAULT '{}',
                created_at  REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_identity_audit_created
                ON identity_audit(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_identity_audit_agent
                ON identity_audit(fleet, agent_name, purpose, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_identity_audit_event
                ON identity_audit(event_type);
        """)
        self._db.commit()

    # -- enrollment tokens -------------------------------------------------

    def issue_enrollment_token(
        self,
        *,
        fleet: str,
        agent_name: str,
        public_key_fingerprint: str,
        issued_by: str,
        purpose: str = PURPOSE_SERVICE_AUTH,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> EnrollmentTokenRecord:
        """Mint a one-time enrollment token bound to a public-key fingerprint."""
        fleet, agent_name, purpose = _clean_fleet_agent_purpose(
            fleet=fleet, agent_name=agent_name, purpose=purpose
        )
        public_key_fingerprint = _clean_fingerprint(public_key_fingerprint)
        issued_by = _clean_nonempty(issued_by, "issued_by")
        if ttl_seconds <= 0:
            raise IdentityRegistryError("ttl_seconds must be positive")
        now = _now()
        token = secrets.token_urlsafe(32)
        rec = EnrollmentTokenRecord(
            token_id=_new_id("enr"),
            token_hash=_token_hash(token),
            fleet=fleet,
            agent_name=agent_name,
            purpose=purpose,
            public_key_fingerprint=public_key_fingerprint,
            status=TOKEN_ACTIVE,
            issued_by=issued_by,
            created_at=now,
            expires_at=now + ttl_seconds,
            reason=reason.strip(),
            metadata=metadata or {},
            token=token,
        )
        with self._lock:
            self._db.execute(
                """
                INSERT INTO enrollment_tokens
                    (token_id, token_hash, fleet, agent_name, purpose,
                     public_key_fingerprint, status, issued_by, created_at,
                     expires_at, reason, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.token_id,
                    rec.token_hash,
                    rec.fleet,
                    rec.agent_name,
                    rec.purpose,
                    rec.public_key_fingerprint,
                    rec.status,
                    rec.issued_by,
                    rec.created_at,
                    rec.expires_at,
                    rec.reason,
                    _json_dumps(rec.metadata),
                ),
            )
            self._audit(
                "enrollment_token.issued",
                fleet=fleet,
                agent_name=agent_name,
                purpose=purpose,
                fingerprint=public_key_fingerprint,
                actor=issued_by,
                reason=reason,
                metadata={"token_id": rec.token_id, "ttl_seconds": ttl_seconds},
            )
            self._db.commit()
        return rec

    def get_enrollment_token(self, token_id: str) -> EnrollmentTokenRecord | None:
        row = self._db.execute(
            "SELECT * FROM enrollment_tokens WHERE token_id = ?", (token_id,)
        ).fetchone()
        return self._row_to_token(row) if row else None

    def list_enrollment_tokens(
        self,
        *,
        fleet: str = "",
        agent_name: str = "",
        purpose: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[EnrollmentTokenRecord]:
        conditions = []
        params: list[Any] = []
        for col, val in (
            ("fleet", fleet),
            ("agent_name", agent_name),
            ("purpose", purpose),
            ("status", status),
        ):
            if val:
                conditions.append(f"{col} = ?")
                params.append(val)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, min(int(limit), 500)))
        rows = self._db.execute(
            f"SELECT * FROM enrollment_tokens {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_token(row) for row in rows]

    def revoke_enrollment_token(
        self, token_id: str, *, revoked_by: str, reason: str = ""
    ) -> EnrollmentTokenRecord:
        revoked_by = _clean_nonempty(revoked_by, "revoked_by")
        now = _now()
        with self._lock:
            rec = self.get_enrollment_token(token_id)
            if rec is None:
                raise KeyError(f"unknown enrollment token: {token_id}")
            if rec.status != TOKEN_ACTIVE:
                raise IdentityRegistryError(
                    f"only active tokens can be revoked (got {rec.status})"
                )
            self._db.execute(
                """
                UPDATE enrollment_tokens
                SET status = ?, revoked_at = ?, reason = ?
                WHERE token_id = ?
                """,
                (TOKEN_REVOKED, now, reason.strip(), token_id),
            )
            self._audit(
                "enrollment_token.revoked",
                fleet=rec.fleet,
                agent_name=rec.agent_name,
                purpose=rec.purpose,
                fingerprint=rec.public_key_fingerprint,
                actor=revoked_by,
                reason=reason,
                metadata={"token_id": rec.token_id},
            )
            self._db.commit()
        updated = self.get_enrollment_token(token_id)
        assert updated is not None
        return updated

    def redeem_enrollment_token(
        self,
        *,
        token: str,
        public_key: PublicKey,
        fleet: str,
        agent_name: str,
        purpose: str = PURPOSE_SERVICE_AUTH,
        actor: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> IdentityKeyRecord:
        """Redeem a one-time token and activate the matching public key."""
        token = _clean_nonempty(token, "token")
        fleet, agent_name, purpose = _clean_fleet_agent_purpose(
            fleet=fleet, agent_name=agent_name, purpose=purpose
        )
        fp = public_key_fingerprint(public_key)
        kid = kid_from_public_key(public_key)
        now = _now()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM enrollment_tokens WHERE token_hash = ?",
                (_token_hash(token),),
            ).fetchone()
            if row is None:
                self._audit(
                    "enrollment_token.redeem_failed",
                    fleet=fleet,
                    agent_name=agent_name,
                    purpose=purpose,
                    fingerprint=fp,
                    actor=actor,
                    outcome="failure",
                    reason="unknown token",
                )
                self._db.commit()
                raise IdentityRegistryError("invalid enrollment token")
            token_rec = self._row_to_token(row)
            if token_rec.status != TOKEN_ACTIVE:
                self._audit(
                    "enrollment_token.redeem_failed",
                    fleet=fleet,
                    agent_name=agent_name,
                    purpose=purpose,
                    fingerprint=fp,
                    actor=actor,
                    outcome="failure",
                    reason=f"token status {token_rec.status}",
                    metadata={"token_id": token_rec.token_id},
                )
                self._db.commit()
                raise IdentityRegistryError(
                    f"enrollment token is not active (got {token_rec.status})"
                )
            if token_rec.expires_at < now:
                self._audit(
                    "enrollment_token.redeem_failed",
                    fleet=fleet,
                    agent_name=agent_name,
                    purpose=purpose,
                    fingerprint=fp,
                    actor=actor,
                    outcome="failure",
                    reason="token expired",
                    metadata={"token_id": token_rec.token_id},
                )
                self._db.commit()
                raise IdentityRegistryError("enrollment token expired")
            if (
                token_rec.fleet != fleet
                or token_rec.agent_name != agent_name
                or token_rec.purpose != purpose
                or token_rec.public_key_fingerprint != fp
            ):
                self._audit(
                    "enrollment_token.redeem_failed",
                    fleet=fleet,
                    agent_name=agent_name,
                    purpose=purpose,
                    fingerprint=fp,
                    actor=actor,
                    outcome="failure",
                    reason="binding mismatch",
                    metadata={"token_id": token_rec.token_id},
                )
                self._db.commit()
                raise IdentityRegistryError(
                    "enrollment token binding does not match submitted key"
                )
            if self.get_key(kid) is not None:
                raise IdentityRegistryError(f"key already registered: {kid}")
            active = self.get_active_key(
                fleet=fleet, agent_name=agent_name, purpose=purpose
            )
            if active is not None:
                raise IdentityRegistryError(
                    "active key already exists; use rotation or admin approval"
                )
            key = self._insert_key(
                public_key=public_key,
                fleet=fleet,
                agent_name=agent_name,
                purpose=purpose,
                status=KEY_ACTIVE,
                created_at=now,
                approved_at=now,
                approved_by=f"enrollment:{token_rec.token_id}",
                metadata=metadata or {},
            )
            self._db.execute(
                """
                UPDATE enrollment_tokens
                SET status = ?, used_at = ?
                WHERE token_id = ?
                """,
                (TOKEN_USED, now, token_rec.token_id),
            )
            self._audit(
                "identity_key.enrolled",
                fleet=fleet,
                agent_name=agent_name,
                purpose=purpose,
                kid=key.kid,
                fingerprint=key.fingerprint,
                actor=actor or agent_name,
                metadata={"token_id": token_rec.token_id},
            )
            self._db.commit()
            return key

    # -- key lifecycle -----------------------------------------------------

    def submit_pending_key(
        self,
        *,
        public_key: PublicKey,
        fleet: str,
        agent_name: str,
        purpose: str = PURPOSE_SERVICE_AUTH,
        actor: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> IdentityKeyRecord:
        """Store a key as pending for manual break-glass approval."""
        fleet, agent_name, purpose = _clean_fleet_agent_purpose(
            fleet=fleet, agent_name=agent_name, purpose=purpose
        )
        actor = actor.strip()
        now = _now()
        with self._lock:
            kid = kid_from_public_key(public_key)
            if self.get_key(kid) is not None:
                raise IdentityRegistryError(f"key already registered: {kid}")
            key = self._insert_key(
                public_key=public_key,
                fleet=fleet,
                agent_name=agent_name,
                purpose=purpose,
                status=KEY_PENDING,
                created_at=now,
                metadata=metadata or {},
            )
            self._audit(
                "identity_key.submitted",
                fleet=fleet,
                agent_name=agent_name,
                purpose=purpose,
                kid=key.kid,
                fingerprint=key.fingerprint,
                actor=actor,
            )
            self._db.commit()
            return key

    def approve_key(
        self, kid: str, *, approved_by: str, reason: str = ""
    ) -> IdentityKeyRecord:
        """Activate a pending key, retiring any currently active key."""
        approved_by = _clean_nonempty(approved_by, "approved_by")
        now = _now()
        with self._lock:
            key = self.get_key(kid)
            if key is None:
                raise KeyError(f"unknown key: {kid}")
            if key.status == KEY_REVOKED:
                raise IdentityRegistryError("revoked keys cannot be approved")
            active = self.get_active_key(
                fleet=key.fleet, agent_name=key.agent_name, purpose=key.purpose
            )
            if active and active.kid != kid:
                self._db.execute(
                    """
                    UPDATE identity_keys
                    SET status = ?, retired_at = ?, replaced_by_kid = ?
                    WHERE kid = ?
                    """,
                    (KEY_RETIRED, now, kid, active.kid),
                )
                self._audit(
                    "identity_key.retired",
                    fleet=active.fleet,
                    agent_name=active.agent_name,
                    purpose=active.purpose,
                    kid=active.kid,
                    fingerprint=active.fingerprint,
                    actor=approved_by,
                    reason=f"replaced by {kid}",
                )
            self._db.execute(
                """
                UPDATE identity_keys
                SET status = ?, approved_at = ?, approved_by = ?
                WHERE kid = ?
                """,
                (KEY_ACTIVE, now, approved_by, kid),
            )
            self._audit(
                "identity_key.approved",
                fleet=key.fleet,
                agent_name=key.agent_name,
                purpose=key.purpose,
                kid=kid,
                fingerprint=key.fingerprint,
                actor=approved_by,
                reason=reason,
            )
            self._db.commit()
        updated = self.get_key(kid)
        assert updated is not None
        return updated

    def revoke_key(self, kid: str, *, revoked_by: str, reason: str) -> IdentityKeyRecord:
        """Revoke a key so services reject it even if signatures verify."""
        revoked_by = _clean_nonempty(revoked_by, "revoked_by")
        reason = _clean_nonempty(reason, "reason")
        now = _now()
        with self._lock:
            key = self.get_key(kid)
            if key is None:
                raise KeyError(f"unknown key: {kid}")
            if key.status == KEY_REVOKED:
                return key
            self._db.execute(
                """
                UPDATE identity_keys
                SET status = ?, revoked_at = ?, revoked_by = ?, revoke_reason = ?
                WHERE kid = ?
                """,
                (KEY_REVOKED, now, revoked_by, reason, kid),
            )
            self._audit(
                "identity_key.revoked",
                fleet=key.fleet,
                agent_name=key.agent_name,
                purpose=key.purpose,
                kid=kid,
                fingerprint=key.fingerprint,
                actor=revoked_by,
                reason=reason,
            )
            self._db.commit()
        updated = self.get_key(kid)
        assert updated is not None
        return updated

    def retire_key(
        self, kid: str, *, retired_by: str, reason: str = ""
    ) -> IdentityKeyRecord:
        retired_by = _clean_nonempty(retired_by, "retired_by")
        now = _now()
        with self._lock:
            key = self.get_key(kid)
            if key is None:
                raise KeyError(f"unknown key: {kid}")
            if key.status == KEY_REVOKED:
                raise IdentityRegistryError("revoked keys cannot be retired")
            self._db.execute(
                "UPDATE identity_keys SET status = ?, retired_at = ? WHERE kid = ?",
                (KEY_RETIRED, now, kid),
            )
            self._audit(
                "identity_key.retired",
                fleet=key.fleet,
                agent_name=key.agent_name,
                purpose=key.purpose,
                kid=kid,
                fingerprint=key.fingerprint,
                actor=retired_by,
                reason=reason,
            )
            self._db.commit()
        updated = self.get_key(kid)
        assert updated is not None
        return updated

    def get_key(self, kid: str) -> IdentityKeyRecord | None:
        row = self._db.execute(
            "SELECT * FROM identity_keys WHERE kid = ?", (kid,)
        ).fetchone()
        return self._row_to_key(row) if row else None

    def get_active_key(
        self, *, fleet: str, agent_name: str, purpose: str = PURPOSE_SERVICE_AUTH
    ) -> IdentityKeyRecord | None:
        row = self._db.execute(
            """
            SELECT * FROM identity_keys
            WHERE fleet = ? AND agent_name = ? AND purpose = ? AND status = ?
            """,
            (fleet, agent_name, purpose, KEY_ACTIVE),
        ).fetchone()
        return self._row_to_key(row) if row else None

    def list_keys(
        self,
        *,
        fleet: str = "",
        agent_name: str = "",
        purpose: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[IdentityKeyRecord]:
        conditions = []
        params: list[Any] = []
        for col, val in (
            ("fleet", fleet),
            ("agent_name", agent_name),
            ("purpose", purpose),
            ("status", status),
        ):
            if val:
                conditions.append(f"{col} = ?")
                params.append(val)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, min(int(limit), 500)))
        rows = self._db.execute(
            f"SELECT * FROM identity_keys {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_key(row) for row in rows]

    # -- audit --------------------------------------------------------------

    def list_audit(
        self,
        *,
        fleet: str = "",
        agent_name: str = "",
        event_type: str = "",
        limit: int = 100,
    ) -> list[IdentityAuditRecord]:
        conditions = []
        params: list[Any] = []
        for col, val in (
            ("fleet", fleet),
            ("agent_name", agent_name),
            ("event_type", event_type),
        ):
            if val:
                conditions.append(f"{col} = ?")
                params.append(val)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, min(int(limit), 500)))
        rows = self._db.execute(
            f"SELECT * FROM identity_audit {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_audit(row) for row in rows]

    def close(self) -> None:
        self._db.close()

    # -- internals ---------------------------------------------------------

    def _insert_key(
        self,
        *,
        public_key: PublicKey,
        fleet: str,
        agent_name: str,
        purpose: str,
        status: str,
        created_at: float,
        approved_at: float = 0.0,
        approved_by: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> IdentityKeyRecord:
        if status not in KEY_STATUSES:
            raise IdentityRegistryError(f"invalid key status: {status}")
        kid = kid_from_public_key(public_key)
        fp = public_key_fingerprint(public_key)
        jwk = jwk_export(public_key, kid=kid)
        self._db.execute(
            """
            INSERT INTO identity_keys
                (kid, fleet, agent_name, purpose, public_key, public_jwk,
                 fingerprint, status, created_at, approved_at, approved_by,
                 metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kid,
                fleet,
                agent_name,
                purpose,
                public_key.raw,
                _json_dumps(jwk),
                fp,
                status,
                created_at,
                approved_at,
                approved_by,
                _json_dumps(metadata),
            ),
        )
        return IdentityKeyRecord(
            kid=kid,
            fleet=fleet,
            agent_name=agent_name,
            purpose=purpose,
            public_key=public_key.raw,
            public_jwk=jwk,
            fingerprint=fp,
            status=status,
            created_at=created_at,
            approved_at=approved_at,
            approved_by=approved_by,
            metadata=metadata or {},
        )

    def _audit(
        self,
        event_type: str,
        *,
        fleet: str = "",
        agent_name: str = "",
        purpose: str = "",
        kid: str = "",
        fingerprint: str = "",
        actor: str = "",
        outcome: str = "success",
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO identity_audit
                (event_type, fleet, agent_name, purpose, kid, fingerprint,
                 actor, outcome, reason, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                fleet,
                agent_name,
                purpose,
                kid,
                fingerprint,
                actor,
                outcome,
                reason.strip(),
                _json_dumps(metadata),
                _now(),
            ),
        )

    @staticmethod
    def _row_to_key(row: sqlite3.Row) -> IdentityKeyRecord:
        return IdentityKeyRecord(
            kid=row["kid"],
            fleet=row["fleet"],
            agent_name=row["agent_name"],
            purpose=row["purpose"],
            public_key=bytes(row["public_key"]),
            public_jwk=_json_loads(row["public_jwk"]),
            fingerprint=row["fingerprint"],
            status=row["status"],
            created_at=row["created_at"],
            approved_at=row["approved_at"],
            approved_by=row["approved_by"],
            retired_at=row["retired_at"],
            revoked_at=row["revoked_at"],
            revoked_by=row["revoked_by"],
            revoke_reason=row["revoke_reason"],
            replaced_by_kid=row["replaced_by_kid"],
            not_before=row["not_before"],
            not_after=row["not_after"],
            metadata=_json_loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_token(row: sqlite3.Row) -> EnrollmentTokenRecord:
        return EnrollmentTokenRecord(
            token_id=row["token_id"],
            token_hash=row["token_hash"],
            fleet=row["fleet"],
            agent_name=row["agent_name"],
            purpose=row["purpose"],
            public_key_fingerprint=row["public_key_fingerprint"],
            status=row["status"],
            issued_by=row["issued_by"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            used_at=row["used_at"],
            revoked_at=row["revoked_at"],
            reason=row["reason"],
            metadata=_json_loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> IdentityAuditRecord:
        return IdentityAuditRecord(
            id=row["id"],
            event_type=row["event_type"],
            fleet=row["fleet"],
            agent_name=row["agent_name"],
            purpose=row["purpose"],
            kid=row["kid"],
            fingerprint=row["fingerprint"],
            actor=row["actor"],
            outcome=row["outcome"],
            reason=row["reason"],
            metadata=_json_loads(row["metadata"]),
            created_at=row["created_at"],
        )
