"""Federation state store — SQLite-backed local persistence.

Owns the on-disk layout under ``data/federation/`` for the federation v0.2
implementation. This module is the root storage dependency for TOFU peer
pinning (P-03), transport (P-04), invite redemption (P-05), and attachment
metadata (P-06).

Tables:

- ``tenants`` — tenant membership context (which tenants this instance belongs
  to, role within each, public signing key for verification).
- ``instance_keys`` — per-device receive keys (X25519 encryption + Ed25519
  device signing) with lifecycle states ``active`` / ``decrypt_only`` /
  ``retired``. Multiple keys may coexist during rotation; ``decrypt_only`` keys
  cannot encrypt new outbound traffic but still decrypt inbound for in-flight
  messages.
- ``peer_pins`` — TOFU pin store keyed by canonical peer address. Stores both
  long-term keys plus the 128-bit fingerprint for display/comparison.
- ``outbox`` — pending outbound envelopes waiting for relay delivery.
- ``inbox`` — received plaintext messages awaiting consumption by higher
  layers (e.g. the messaging UI).
- ``issued_invites`` — invite cache (token hash only — raw token never
  persisted).
- ``attachments`` — attachment metadata pointing at on-disk encrypted blobs.

This module owns *only* the structure and CRUD. Encryption of tenant
signing seeds is the job of ``key_store.py`` (which writes into the
``tenant_signing_keys`` table that this module also creates).

Nothing in this module talks to the network; nothing logs key material;
nothing returns plaintext private keys. The signing-seed table is created
here for schema cohesion, but its contents are opaque BLOBs from this
layer's perspective — only ``key_store.EncryptedTenantKeyStore`` ever
encrypts/decrypts them.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = "data/federation/state.db"

# Instance-key lifecycle states.
INSTANCE_KEY_ACTIVE = "active"
INSTANCE_KEY_DECRYPT_ONLY = "decrypt_only"
INSTANCE_KEY_RETIRED = "retired"
_VALID_INSTANCE_KEY_STATES = frozenset(
    {INSTANCE_KEY_ACTIVE, INSTANCE_KEY_DECRYPT_ONLY, INSTANCE_KEY_RETIRED}
)

# Instance-key kinds.
INSTANCE_KEY_KIND_ENCRYPTION = "encryption"  # X25519
INSTANCE_KEY_KIND_SIGNING = "signing"  # Ed25519 (per-device, not tenant)
_VALID_INSTANCE_KEY_KINDS = frozenset(
    {INSTANCE_KEY_KIND_ENCRYPTION, INSTANCE_KEY_KIND_SIGNING}
)

# Peer-pin status values.
PEER_PIN_PINNED = "pinned"
PEER_PIN_ROTATED = "rotated"
PEER_PIN_REJECTED = "rejected"
_VALID_PEER_PIN_STATUSES = frozenset({PEER_PIN_PINNED, PEER_PIN_ROTATED, PEER_PIN_REJECTED})

# Outbox status values.
OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"
_VALID_OUTBOX_STATUSES = frozenset({OUTBOX_PENDING, OUTBOX_SENT, OUTBOX_FAILED})

# Inbox status values.
INBOX_NEW = "new"
INBOX_READ = "read"
INBOX_ARCHIVED = "archived"
_VALID_INBOX_STATUSES = frozenset({INBOX_NEW, INBOX_READ, INBOX_ARCHIVED})

# Invite status values.
INVITE_ACTIVE = "active"
INVITE_REDEEMED = "redeemed"
INVITE_EXPIRED = "expired"
INVITE_REVOKED = "revoked"
_VALID_INVITE_STATUSES = frozenset(
    {INVITE_ACTIVE, INVITE_REDEEMED, INVITE_EXPIRED, INVITE_REVOKED}
)

# Tenant role values.
ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
_VALID_ROLES = frozenset({ROLE_OWNER, ROLE_MEMBER})


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _now() -> float:
    return time.time()


# -- Records ---------------------------------------------------------------


@dataclass
class TenantRecord:
    """Membership in a tenant. ``signing_pk`` is the *tenant* identity public
    key (Ed25519 raw 32 bytes); ``role`` is this instance's role in the
    tenant (``owner`` for the issuer, ``member`` otherwise)."""

    tenant_id: str = ""
    address: str = ""
    signing_pk: bytes = b""
    role: str = ROLE_MEMBER
    created_at: float = 0.0


@dataclass
class InstanceKeyRecord:
    """A single per-device key with lifecycle state."""

    kid: str = ""  # short stable id, e.g. first 16 hex of fingerprint
    tenant_id: str = ""
    kind: str = INSTANCE_KEY_KIND_ENCRYPTION
    public_key: bytes = b""
    encrypted_secret: bytes = b""  # opaque to this layer; key_store decrypts
    state: str = INSTANCE_KEY_ACTIVE
    created_at: float = 0.0
    retired_at: float = 0.0


@dataclass
class PeerPinRecord:
    """TOFU pin for a remote peer."""

    peer_address: str = ""
    sig_pk: bytes = b""
    enc_pk: bytes = b""
    fingerprint: bytes = b""
    status: str = PEER_PIN_PINNED
    first_seen: float = 0.0
    last_seen: float = 0.0


@dataclass
class OutboxRecord:
    """A queued outbound envelope."""

    msg_id: str = ""
    tenant_id: str = ""
    recipient_address: str = ""
    envelope_blob: bytes = b""
    status: str = OUTBOX_PENDING
    attempts: int = 0
    last_error: str = ""
    created_at: float = 0.0
    next_retry_at: float = 0.0


@dataclass
class InboxRecord:
    """A received decrypted message."""

    msg_id: str = ""
    tenant_id: str = ""
    sender_address: str = ""
    plaintext_blob: bytes = b""
    status: str = INBOX_NEW
    received_at: float = 0.0


@dataclass
class InviteRecord:
    """An invite issued by this instance. We only store the *hash* of the
    redemption token; the raw token is given out once and never persisted."""

    invite_id: str = ""
    tenant_id: str = ""
    recipient_hint: str = ""
    token_hash: bytes = b""
    expires_at: float = 0.0
    status: str = INVITE_ACTIVE
    created_at: float = 0.0


@dataclass
class AttachmentRecord:
    """Metadata pointer for an attachment blob."""

    attachment_id: str = ""
    msg_id: str = ""
    sha256: bytes = b""
    size: int = 0
    mime: str = ""
    local_path: str = ""
    encrypted: bool = True
    created_at: float = 0.0


# -- Store -----------------------------------------------------------------


class FederationStateStore:
    """SQLite-backed federation state.

    All methods are safe to call from multiple threads — the connection
    is opened with ``check_same_thread=False`` and we serialize writes via
    SQLite's own locking. Reads use the same connection; if write contention
    becomes an issue we can split read/write later.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.row_factory = sqlite3.Row
        self._create_schema()
        self._ensure_columns()

    # -- schema ------------------------------------------------------------

    def _create_schema(self) -> None:
        c = self._db
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                signing_pk BLOB NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                created_at REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_signing_keys (
                tenant_id TEXT PRIMARY KEY,
                encrypted_seed BLOB NOT NULL,
                nonce BLOB NOT NULL,
                kdf_version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS instance_keys (
                kid TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                public_key BLOB NOT NULL,
                encrypted_secret BLOB NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                retired_at REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_instance_keys_tenant_state "
            "ON instance_keys(tenant_id, state)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS peer_pins (
                peer_address TEXT PRIMARY KEY,
                sig_pk BLOB NOT NULL,
                enc_pk BLOB NOT NULL,
                fingerprint BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pinned',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                msg_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                recipient_address TEXT NOT NULL,
                envelope_blob BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                next_retry_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, next_retry_at)")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS inbox (
                msg_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                sender_address TEXT NOT NULL,
                plaintext_blob BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                received_at REAL NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_inbox_tenant_status ON inbox(tenant_id, status)")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS issued_invites (
                invite_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                recipient_hint TEXT NOT NULL DEFAULT '',
                token_hash BLOB NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_invites_tenant_status "
            "ON issued_invites(tenant_id, status)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS attachments (
                attachment_id TEXT PRIMARY KEY,
                msg_id TEXT NOT NULL,
                sha256 BLOB NOT NULL,
                size INTEGER NOT NULL,
                mime TEXT NOT NULL DEFAULT '',
                local_path TEXT NOT NULL,
                encrypted INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(msg_id)")
        c.commit()

    def _ensure_columns(self) -> None:
        """Forward-compatible column additions following the project pattern."""
        # No migrations yet — this is the initial schema. Future additions:
        #   migrations: list[tuple[str, str, list[tuple[str, str]]]] = [
        #       ("table", "column", "TYPE DEFAULT ..."),
        #   ]
        pass

    # -- tenants -----------------------------------------------------------

    def upsert_tenant(self, rec: TenantRecord) -> TenantRecord:
        if rec.role not in _VALID_ROLES:
            raise ValueError(f"invalid role: {rec.role!r}")
        if not rec.tenant_id or not rec.address:
            raise ValueError("tenant_id and address are required")
        if not isinstance(rec.signing_pk, bytes) or len(rec.signing_pk) != 32:
            raise ValueError("signing_pk must be 32 bytes")
        if not rec.created_at:
            rec.created_at = _now()
        self._db.execute(
            """
            INSERT INTO tenants (tenant_id, address, signing_pk, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                address=excluded.address,
                signing_pk=excluded.signing_pk,
                role=excluded.role
            """,
            (rec.tenant_id, rec.address, rec.signing_pk, rec.role, rec.created_at),
        )
        self._db.commit()
        return rec

    def get_tenant(self, tenant_id: str) -> Optional[TenantRecord]:
        row = self._db.execute(
            "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if not row:
            return None
        return TenantRecord(
            tenant_id=row["tenant_id"],
            address=row["address"],
            signing_pk=bytes(row["signing_pk"]),
            role=row["role"],
            created_at=row["created_at"],
        )

    def list_tenants(self) -> list[TenantRecord]:
        rows = self._db.execute("SELECT * FROM tenants ORDER BY created_at").fetchall()
        return [
            TenantRecord(
                tenant_id=r["tenant_id"],
                address=r["address"],
                signing_pk=bytes(r["signing_pk"]),
                role=r["role"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- instance keys -----------------------------------------------------

    def add_instance_key(self, rec: InstanceKeyRecord) -> InstanceKeyRecord:
        if rec.kind not in _VALID_INSTANCE_KEY_KINDS:
            raise ValueError(f"invalid kind: {rec.kind!r}")
        if rec.state not in _VALID_INSTANCE_KEY_STATES:
            raise ValueError(f"invalid state: {rec.state!r}")
        if not rec.kid or not rec.tenant_id:
            raise ValueError("kid and tenant_id are required")
        if not rec.created_at:
            rec.created_at = _now()
        self._db.execute(
            """
            INSERT INTO instance_keys
                (kid, tenant_id, kind, public_key, encrypted_secret, state,
                 created_at, retired_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.kid,
                rec.tenant_id,
                rec.kind,
                rec.public_key,
                rec.encrypted_secret,
                rec.state,
                rec.created_at,
                rec.retired_at,
            ),
        )
        self._db.commit()
        return rec

    def get_instance_key(self, kid: str) -> Optional[InstanceKeyRecord]:
        row = self._db.execute(
            "SELECT * FROM instance_keys WHERE kid = ?", (kid,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_instance_key(row)

    def list_instance_keys(
        self,
        tenant_id: Optional[str] = None,
        state: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[InstanceKeyRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if state is not None:
            if state not in _VALID_INSTANCE_KEY_STATES:
                raise ValueError(f"invalid state filter: {state!r}")
            clauses.append("state = ?")
            params.append(state)
        if kind is not None:
            if kind not in _VALID_INSTANCE_KEY_KINDS:
                raise ValueError(f"invalid kind filter: {kind!r}")
            clauses.append("kind = ?")
            params.append(kind)
        sql = "SELECT * FROM instance_keys"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        rows = self._db.execute(sql, params).fetchall()
        return [self._row_to_instance_key(r) for r in rows]

    def transition_instance_key(self, kid: str, new_state: str) -> None:
        """Move an instance key between lifecycle states.

        Allowed transitions:
            active        -> decrypt_only, retired
            decrypt_only  -> retired
            retired       -> (terminal)
        """
        if new_state not in _VALID_INSTANCE_KEY_STATES:
            raise ValueError(f"invalid state: {new_state!r}")
        current = self.get_instance_key(kid)
        if current is None:
            raise KeyError(f"unknown instance key: {kid!r}")
        if current.state == new_state:
            return
        allowed = {
            INSTANCE_KEY_ACTIVE: {INSTANCE_KEY_DECRYPT_ONLY, INSTANCE_KEY_RETIRED},
            INSTANCE_KEY_DECRYPT_ONLY: {INSTANCE_KEY_RETIRED},
            INSTANCE_KEY_RETIRED: set(),
        }
        if new_state not in allowed[current.state]:
            raise ValueError(
                f"illegal transition {current.state!r} -> {new_state!r} for kid={kid!r}"
            )
        retired_at = _now() if new_state == INSTANCE_KEY_RETIRED else current.retired_at
        self._db.execute(
            "UPDATE instance_keys SET state = ?, retired_at = ? WHERE kid = ?",
            (new_state, retired_at, kid),
        )
        self._db.commit()

    @staticmethod
    def _row_to_instance_key(row: sqlite3.Row) -> InstanceKeyRecord:
        return InstanceKeyRecord(
            kid=row["kid"],
            tenant_id=row["tenant_id"],
            kind=row["kind"],
            public_key=bytes(row["public_key"]),
            encrypted_secret=bytes(row["encrypted_secret"]),
            state=row["state"],
            created_at=row["created_at"],
            retired_at=row["retired_at"],
        )

    # -- peer pins ---------------------------------------------------------

    def upsert_peer_pin(self, rec: PeerPinRecord) -> PeerPinRecord:
        if rec.status not in _VALID_PEER_PIN_STATUSES:
            raise ValueError(f"invalid pin status: {rec.status!r}")
        if not rec.peer_address:
            raise ValueError("peer_address is required")
        if len(rec.sig_pk) != 32 or len(rec.enc_pk) != 32:
            raise ValueError("sig_pk and enc_pk must be 32 bytes each")
        if len(rec.fingerprint) != 16:
            raise ValueError("fingerprint must be 16 bytes")
        now = _now()
        if not rec.first_seen:
            rec.first_seen = now
        rec.last_seen = now
        self._db.execute(
            """
            INSERT INTO peer_pins
                (peer_address, sig_pk, enc_pk, fingerprint, status, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(peer_address) DO UPDATE SET
                sig_pk=excluded.sig_pk,
                enc_pk=excluded.enc_pk,
                fingerprint=excluded.fingerprint,
                status=excluded.status,
                last_seen=excluded.last_seen
            """,
            (
                rec.peer_address,
                rec.sig_pk,
                rec.enc_pk,
                rec.fingerprint,
                rec.status,
                rec.first_seen,
                rec.last_seen,
            ),
        )
        self._db.commit()
        return rec

    def get_peer_pin(self, peer_address: str) -> Optional[PeerPinRecord]:
        row = self._db.execute(
            "SELECT * FROM peer_pins WHERE peer_address = ?", (peer_address,)
        ).fetchone()
        if not row:
            return None
        return PeerPinRecord(
            peer_address=row["peer_address"],
            sig_pk=bytes(row["sig_pk"]),
            enc_pk=bytes(row["enc_pk"]),
            fingerprint=bytes(row["fingerprint"]),
            status=row["status"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )

    # -- outbox / inbox ----------------------------------------------------

    def enqueue_outbound(self, rec: OutboxRecord) -> OutboxRecord:
        if rec.status not in _VALID_OUTBOX_STATUSES:
            raise ValueError(f"invalid outbox status: {rec.status!r}")
        if not rec.msg_id or not rec.tenant_id or not rec.recipient_address:
            raise ValueError("msg_id, tenant_id, recipient_address are required")
        if not rec.created_at:
            rec.created_at = _now()
        self._db.execute(
            """
            INSERT INTO outbox
                (msg_id, tenant_id, recipient_address, envelope_blob, status,
                 attempts, last_error, created_at, next_retry_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.msg_id,
                rec.tenant_id,
                rec.recipient_address,
                rec.envelope_blob,
                rec.status,
                rec.attempts,
                rec.last_error,
                rec.created_at,
                rec.next_retry_at,
            ),
        )
        self._db.commit()
        return rec

    def mark_outbound_status(
        self,
        msg_id: str,
        status: str,
        last_error: str = "",
        next_retry_at: float = 0.0,
    ) -> None:
        if status not in _VALID_OUTBOX_STATUSES:
            raise ValueError(f"invalid outbox status: {status!r}")
        cur = self._db.execute(
            """
            UPDATE outbox
               SET status = ?,
                   attempts = attempts + 1,
                   last_error = ?,
                   next_retry_at = ?
             WHERE msg_id = ?
            """,
            (status, last_error, next_retry_at, msg_id),
        )
        self._db.commit()
        if cur.rowcount == 0:
            raise KeyError(f"unknown outbound msg_id: {msg_id!r}")

    def list_outbound(
        self, status: Optional[str] = None, limit: int = 100
    ) -> list[OutboxRecord]:
        if status is not None and status not in _VALID_OUTBOX_STATUSES:
            raise ValueError(f"invalid status filter: {status!r}")
        if status is None:
            rows = self._db.execute(
                "SELECT * FROM outbox ORDER BY created_at LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM outbox WHERE status = ? ORDER BY created_at LIMIT ?",
                (status, limit),
            ).fetchall()
        return [
            OutboxRecord(
                msg_id=r["msg_id"],
                tenant_id=r["tenant_id"],
                recipient_address=r["recipient_address"],
                envelope_blob=bytes(r["envelope_blob"]),
                status=r["status"],
                attempts=r["attempts"],
                last_error=r["last_error"],
                created_at=r["created_at"],
                next_retry_at=r["next_retry_at"],
            )
            for r in rows
        ]

    def store_inbound(self, rec: InboxRecord) -> InboxRecord:
        if rec.status not in _VALID_INBOX_STATUSES:
            raise ValueError(f"invalid inbox status: {rec.status!r}")
        if not rec.msg_id or not rec.tenant_id or not rec.sender_address:
            raise ValueError("msg_id, tenant_id, sender_address are required")
        if not rec.received_at:
            rec.received_at = _now()
        self._db.execute(
            """
            INSERT INTO inbox
                (msg_id, tenant_id, sender_address, plaintext_blob, status, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rec.msg_id,
                rec.tenant_id,
                rec.sender_address,
                rec.plaintext_blob,
                rec.status,
                rec.received_at,
            ),
        )
        self._db.commit()
        return rec

    def mark_inbound_status(self, msg_id: str, status: str) -> None:
        if status not in _VALID_INBOX_STATUSES:
            raise ValueError(f"invalid inbox status: {status!r}")
        cur = self._db.execute(
            "UPDATE inbox SET status = ? WHERE msg_id = ?", (status, msg_id)
        )
        self._db.commit()
        if cur.rowcount == 0:
            raise KeyError(f"unknown inbound msg_id: {msg_id!r}")

    def list_inbound(
        self,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[InboxRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status is not None:
            if status not in _VALID_INBOX_STATUSES:
                raise ValueError(f"invalid status filter: {status!r}")
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM inbox"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY received_at DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(sql, params).fetchall()
        return [
            InboxRecord(
                msg_id=r["msg_id"],
                tenant_id=r["tenant_id"],
                sender_address=r["sender_address"],
                plaintext_blob=bytes(r["plaintext_blob"]),
                status=r["status"],
                received_at=r["received_at"],
            )
            for r in rows
        ]

    # -- invites -----------------------------------------------------------

    def add_invite(self, rec: InviteRecord) -> InviteRecord:
        if rec.status not in _VALID_INVITE_STATUSES:
            raise ValueError(f"invalid invite status: {rec.status!r}")
        if not rec.invite_id or not rec.tenant_id:
            raise ValueError("invite_id and tenant_id are required")
        if not isinstance(rec.token_hash, bytes) or len(rec.token_hash) != 32:
            raise ValueError("token_hash must be 32 bytes (SHA-256 of token)")
        if not rec.created_at:
            rec.created_at = _now()
        self._db.execute(
            """
            INSERT INTO issued_invites
                (invite_id, tenant_id, recipient_hint, token_hash,
                 expires_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.invite_id,
                rec.tenant_id,
                rec.recipient_hint,
                rec.token_hash,
                rec.expires_at,
                rec.status,
                rec.created_at,
            ),
        )
        self._db.commit()
        return rec

    def mark_invite_status(self, invite_id: str, status: str) -> None:
        if status not in _VALID_INVITE_STATUSES:
            raise ValueError(f"invalid invite status: {status!r}")
        cur = self._db.execute(
            "UPDATE issued_invites SET status = ? WHERE invite_id = ?",
            (status, invite_id),
        )
        self._db.commit()
        if cur.rowcount == 0:
            raise KeyError(f"unknown invite_id: {invite_id!r}")

    def list_invites(
        self, tenant_id: Optional[str] = None, status: Optional[str] = None
    ) -> list[InviteRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if status is not None:
            if status not in _VALID_INVITE_STATUSES:
                raise ValueError(f"invalid status filter: {status!r}")
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM issued_invites"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        rows = self._db.execute(sql, params).fetchall()
        return [
            InviteRecord(
                invite_id=r["invite_id"],
                tenant_id=r["tenant_id"],
                recipient_hint=r["recipient_hint"],
                token_hash=bytes(r["token_hash"]),
                expires_at=r["expires_at"],
                status=r["status"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- attachments -------------------------------------------------------

    def add_attachment(self, rec: AttachmentRecord) -> AttachmentRecord:
        if not rec.attachment_id or not rec.msg_id:
            raise ValueError("attachment_id and msg_id are required")
        if not isinstance(rec.sha256, bytes) or len(rec.sha256) != 32:
            raise ValueError("sha256 must be 32 bytes")
        if rec.size < 0:
            raise ValueError("size must be non-negative")
        if not rec.created_at:
            rec.created_at = _now()
        self._db.execute(
            """
            INSERT INTO attachments
                (attachment_id, msg_id, sha256, size, mime, local_path,
                 encrypted, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.attachment_id,
                rec.msg_id,
                rec.sha256,
                rec.size,
                rec.mime,
                rec.local_path,
                int(bool(rec.encrypted)),
                rec.created_at,
            ),
        )
        self._db.commit()
        return rec

    def list_attachments(self, msg_id: str) -> list[AttachmentRecord]:
        rows = self._db.execute(
            "SELECT * FROM attachments WHERE msg_id = ? ORDER BY created_at",
            (msg_id,),
        ).fetchall()
        return [
            AttachmentRecord(
                attachment_id=r["attachment_id"],
                msg_id=r["msg_id"],
                sha256=bytes(r["sha256"]),
                size=r["size"],
                mime=r["mime"],
                local_path=r["local_path"],
                encrypted=bool(r["encrypted"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- diagnostics -------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Counts only — no key material, no envelope contents."""
        out: dict[str, int] = {}
        for table in (
            "tenants",
            "tenant_signing_keys",
            "instance_keys",
            "peer_pins",
            "outbox",
            "inbox",
            "issued_invites",
            "attachments",
        ):
            out[table] = self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"FederationStateStore(db_path={self.db_path!r})"

    def close(self) -> None:
        self._db.close()
