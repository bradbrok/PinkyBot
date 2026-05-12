"""Session-bound bearer tokens for the daemon-local signer service.

A bearer token is the capability "this streaming session may ask the
daemon-local signer to sign HTTP requests on behalf of agent
``(fleet, agent_name, purpose)``". The daemon mints one when a streaming
session starts; the session presents it on every call to
``/internal/signer/sign`` (PR-4b-4); the daemon validates the token
before composing :class:`pinky_identity.DaemonSigner` to sign.

Design rules (see ``docs/design/agent-asymmetric-identity.md`` and #461):

- **Tokens are opaque secrets.** 32 cryptographically random bytes,
  transported as base64url. The store persists only ``sha256(secret)`` —
  the raw secret never touches the database after the mint call returns
  it. Lookups happen by hash; a database leak doesn't yield usable
  tokens.
- **Bound to the identity tuple, not the kid.** Tokens authorize
  signing as ``(fleet, agent_name, purpose)``. The kid is resolved at
  sign time via the registry, so a key rotation doesn't invalidate
  outstanding session tokens. Revocation of the *agent identity* is
  separate from revocation of the *session capability*.
- **Session-scoped.** Each token carries the ``session_id`` of the
  streaming session it was minted for. The daemon can invalidate every
  outstanding token for a session in one call (e.g. when the session
  process exits or its context restart drops authority).
- **Expiry is mandatory.** Every token has an ``expires_at`` —
  :meth:`BearerTokenStore.mint` requires either an explicit value or a
  ``ttl``. The verifier's ``validate()`` refuses expired tokens.
- **Revocation is explicit.** :meth:`invalidate`, :meth:`invalidate_session`,
  and :meth:`invalidate_identity` write a ``revoked_at`` timestamp;
  ``validate()`` refuses any token with a non-null ``revoked_at``.
- **No partial trust.** :meth:`validate` returns the full
  :class:`BearerTokenClaims` or raises a :class:`BearerTokenError`
  subclass. No "valid but not active" middle state.
- **Stand-alone primitive.** Depends only on SQLite + the package's
  :class:`SignatureError`. Does not import the registry, the signer,
  the keystore, or any HTTP layer — so it can be tested without
  standing up the rest of the stack, and PR-4b-4 composes this with
  :class:`DaemonSigner` cleanly.

Error model:

- :class:`BearerTokenError` — umbrella, subclass of
  :class:`pinky_identity.SignatureError`. One ``except`` covers every
  failure mode.
- :class:`BearerTokenNotFoundError` — token doesn't exist in the store
  (unknown secret, or a partially-typed token). Indistinguishable in
  the message from an outright fake — we don't leak whether the prefix
  was real.
- :class:`BearerTokenExpiredError` — past ``expires_at``.
- :class:`BearerTokenRevokedError` — explicit ``revoked_at`` is set.

Token format on the wire: base64url-encoded 32 raw bytes (43 ASCII
chars, no padding). Callers usually wrap in ``Authorization: Bearer …``
on the HTTP layer (PR-4b-4); this module deals in the raw token string.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from pinky_identity.keys import SignatureError

# -- Constants ---------------------------------------------------------------

#: Default on-disk path for the bearer-token store. Lives alongside the
#: signer store under ``data/identity/`` — same trust boundary (daemon
#: only), different concern (capabilities vs encrypted seeds), so kept
#: in separate files.
DEFAULT_BEARER_TOKEN_STORE_PATH = "data/identity/bearer_tokens.db"

#: Bytes of cryptographic randomness in each token secret. 32 bytes →
#: 256 bits. RFC 4648 base64url with no padding encodes that as 43
#: characters. Don't lower this below 16 (128 bits); 32 is the modern
#: default and matches the device-key length.
TOKEN_SECRET_BYTES: int = 32

#: Default TTL when callers don't specify one. 1 hour matches typical
#: streaming-session lifetimes; daemons can mint longer for batch jobs
#: by passing an explicit ``ttl=`` or ``expires_at=``.
DEFAULT_TOKEN_TTL: timedelta = timedelta(hours=1)


# -- Errors ------------------------------------------------------------------


class BearerTokenError(SignatureError):
    """Umbrella for any bearer-token failure.

    Subclass of :class:`pinky_identity.SignatureError` so a generic
    ``except SignatureError`` block covers signer, keystore, and bearer
    failures uniformly.
    """


class BearerTokenNotFoundError(BearerTokenError):
    """The presented token doesn't match any stored row.

    Indistinguishable in the message from outright forgery — we don't
    confirm whether a prefix was real, only that the full secret
    doesn't authenticate. Callers should log generically.
    """


class BearerTokenExpiredError(BearerTokenError):
    """The token's ``expires_at`` is in the past."""


class BearerTokenRevokedError(BearerTokenError):
    """The token was explicitly invalidated by a revoke call."""


# -- Token primitives --------------------------------------------------------


def _encode_token(secret_bytes: bytes) -> str:
    """Encode raw secret bytes as base64url (RFC 4648 §5, no padding).

    43 chars for 32 bytes. URL/header safe.
    """
    return base64.urlsafe_b64encode(secret_bytes).rstrip(b"=").decode("ascii")


def _decode_token(token: str) -> bytes:
    """Inverse of :func:`_encode_token`.

    The wire format is **strict no-padding base64url**: any ``=``
    character in the input is rejected. Two reasons:

    1. The encoder never emits padding, so a legitimate caller has
       no reason to send any. Accepting padded variants creates a
       normalization gap — two distinct strings both decode to the
       same secret, which complicates audit/log invariants.
    2. Strict parsing shrinks the attacker surface (one canonical
       form to reason about).

    Raises :class:`BearerTokenNotFoundError` for any malformed input
    — we don't leak which kind of malformation occurred (length,
    charset, padding, etc.) since unknown-token and bad-token are the
    same threat model.
    """
    if not isinstance(token, str) or not token:
        raise BearerTokenNotFoundError("empty or non-string token")
    # Reject obviously-oversized input before hashing so callers
    # can't induce unbounded work with a 10 MB "token".
    if len(token) > 4 * TOKEN_SECRET_BYTES:
        raise BearerTokenNotFoundError("token too long")
    # Strict no-padding enforcement. ``=`` only appears as base64
    # padding; the encoder strips it.
    if "=" in token:
        raise BearerTokenNotFoundError("token must not contain padding")
    padding = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + padding)
    except (ValueError, binascii.Error) as e:
        raise BearerTokenNotFoundError("malformed token encoding") from e


def _hash_secret(secret_bytes: bytes) -> str:
    """SHA-256 the raw secret. Returns lowercase hex.

    Used both at mint time (to store the verifier-side hash) and at
    validate time (to look up by hash). The store never sees raw
    secrets after the mint call returns.
    """
    return hashlib.sha256(secret_bytes).hexdigest()


# -- Claims dataclass --------------------------------------------------------


@dataclass(frozen=True)
class BearerTokenClaims:
    """The validated identity behind a bearer token.

    Returned by :meth:`BearerTokenStore.validate`. The caller composes
    these claims with :class:`pinky_identity.DaemonSigner` —
    ``(fleet, agent_name, purpose)`` resolves to the active kid via
    :meth:`DaemonSigner.resolve_active_kid`, and the signer signs.

    The raw secret is not present here on purpose. By the time
    ``validate`` returns, the secret has done its job and the rest of
    the request flow should reason about identity, not credentials.
    """

    token_id: str
    session_id: str
    fleet: str
    agent_name: str
    purpose: str
    created_at: float
    expires_at: float
    last_used_at: float | None
    revoked_at: float | None

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True)
class MintResult:
    """The output of :meth:`BearerTokenStore.mint`.

    ``token`` is the secret string the caller hands to the streaming
    session — this is the **only** time the secret exists outside the
    minting process's memory. The other fields are the bookkeeping
    half (safe to log).

    The default :func:`repr` redacts ``token``. The credential must
    never leak through diagnostic logging: a stray
    ``log.debug("minted %r", result)`` would otherwise stamp the
    full signer-authority secret into a log file. Callers that need
    the token must read ``result.token`` explicitly.
    """

    token: str = field(repr=False)
    token_id: str
    claims: BearerTokenClaims

    def __repr__(self) -> str:
        # Custom repr — the dataclass-generated default would have
        # shown ``token=...`` even with ``repr=False`` on the field,
        # since field(repr=False) merely omits a field from the
        # auto-generated repr. Explicit is better than relying on
        # that; this makes the redaction obvious and adds a marker
        # so reviewers grepping for leaked tokens spot the safe form.
        return (
            "MintResult(token=<redacted>, "
            f"token_id={self.token_id!r}, "
            f"claims={self.claims!r})"
        )


# -- Store -------------------------------------------------------------------


class BearerTokenStore:
    """SQLite-backed store for session-bound bearer tokens.

    One row per outstanding token. The schema holds the bookkeeping
    half — id, session, identity tuple, timestamps, revocation flag —
    plus ``secret_hash`` (sha-256 hex). The raw secret is returned
    once at mint time and never persisted.

    Concurrency: single-process. WAL mode + autocommit with explicit
    transactions for multi-statement writes, matching
    :mod:`pinky_identity.signer_store`. Cross-process use isn't
    supported; only the daemon owns this file.

    Errors normalize to :class:`BearerTokenError` subclasses. Lookup
    misses and bad encodings both surface as
    :class:`BearerTokenNotFoundError` — callers shouldn't be able to
    distinguish "wrong format" from "no such token" since both are
    attacker-controlled.
    """

    __slots__ = ("_db_path", "_db")

    def __init__(
        self,
        *,
        db_path: str | Path = DEFAULT_BEARER_TOKEN_STORE_PATH,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            str(self._db_path), isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    # -- schema --

    def _ensure_schema(self) -> None:
        # ``secret_hash`` is UNIQUE: a single secret can only correspond
        # to one row. Lookups go through it. The composite indexes on
        # ``session_id`` and ``(fleet, agent_name, purpose)`` make the
        # bulk-invalidate paths O(rows-to-revoke), not full-scan.
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS bearer_tokens (
                token_id      TEXT PRIMARY KEY,
                secret_hash   TEXT UNIQUE NOT NULL,
                session_id    TEXT NOT NULL,
                fleet         TEXT NOT NULL,
                agent_name    TEXT NOT NULL,
                purpose       TEXT NOT NULL,
                created_at    REAL NOT NULL,
                expires_at    REAL NOT NULL,
                last_used_at  REAL,
                revoked_at    REAL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS bearer_tokens_session_idx "
            "ON bearer_tokens (session_id)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS bearer_tokens_identity_idx "
            "ON bearer_tokens (fleet, agent_name, purpose)"
        )

    # -- mint --

    def mint(
        self,
        *,
        session_id: str,
        agent_name: str,
        fleet: str,
        purpose: str,
        ttl: timedelta | None = None,
        expires_at: float | None = None,
        now: float | None = None,
    ) -> MintResult:
        """Mint a new bearer token bound to a session and identity tuple.

        Exactly one of ``ttl`` or ``expires_at`` may be supplied; if
        both are ``None``, :data:`DEFAULT_TOKEN_TTL` is used. The
        resulting ``expires_at`` is in absolute seconds since the
        epoch.

        Returns a :class:`MintResult` containing the secret token (the
        **only** time the raw secret is exposed outside the minting
        process) plus the verifier-side claims.

        Idempotency: this method is **not** idempotent — every call
        generates fresh randomness. Callers that want one token per
        session per identity should track that on their side.
        """
        if not session_id or not agent_name or not fleet or not purpose:
            raise ValueError(
                "session_id, agent_name, fleet, purpose must all be non-empty"
            )
        if ttl is not None and expires_at is not None:
            raise ValueError("pass at most one of ttl or expires_at")

        now_val = now if now is not None else time.time()
        if expires_at is None:
            effective_ttl = ttl if ttl is not None else DEFAULT_TOKEN_TTL
            expires_at = now_val + effective_ttl.total_seconds()
        if expires_at <= now_val:
            raise ValueError("expires_at must be in the future")

        secret_bytes = secrets.token_bytes(TOKEN_SECRET_BYTES)
        token = _encode_token(secret_bytes)
        secret_hash = _hash_secret(secret_bytes)
        # The token_id is a short-but-unique identifier for the row.
        # Derive it from the hash so it's deterministic given the
        # secret (handy for "I revoked this; here's the id"); keeping
        # only the first 16 hex chars matches the kid format used
        # elsewhere in this package.
        token_id = secret_hash[:16]

        with self._txn():
            self._db.execute(
                """
                INSERT INTO bearer_tokens
                    (token_id, secret_hash, session_id, fleet, agent_name,
                     purpose, created_at, expires_at, last_used_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    token_id,
                    secret_hash,
                    session_id,
                    fleet,
                    agent_name,
                    purpose,
                    now_val,
                    float(expires_at),
                ),
            )

        claims = BearerTokenClaims(
            token_id=token_id,
            session_id=session_id,
            fleet=fleet,
            agent_name=agent_name,
            purpose=purpose,
            created_at=now_val,
            expires_at=float(expires_at),
            last_used_at=None,
            revoked_at=None,
        )
        return MintResult(token=token, token_id=token_id, claims=claims)

    # -- validate --

    def validate(
        self,
        token: str,
        *,
        now: float | None = None,
        touch: bool = True,
    ) -> BearerTokenClaims:
        """Validate *token* and return its claims.

        Steps, in order:

        1. Decode the token (rejects malformed input as
           :class:`BearerTokenNotFoundError`).
        2. Look up by ``sha256(secret)`` in constant time relative to
           the database key — SQLite indexes are O(log n).
        3. Reject revoked tokens with :class:`BearerTokenRevokedError`.
        4. Reject expired tokens with :class:`BearerTokenExpiredError`.
        5. If ``touch`` is True (default), bump ``last_used_at`` to
           ``now``. This is a separate UPDATE; the validation itself
           doesn't depend on the bump succeeding.

        Note on side-channels: the hash comparison happens server-side
        inside SQLite, not in Python. A timing-oracle on bearer-token
        validation would have to bypass SQLite's index — unlikely. For
        completeness, the lookup uses :func:`hmac.compare_digest` on
        the resulting ``secret_hash`` to make any application-level
        comparison constant-time too.
        """
        secret_bytes = _decode_token(token)
        if len(secret_bytes) != TOKEN_SECRET_BYTES:
            # Wrong length is the same threat as forgery — no row
            # could match. Use the same error.
            raise BearerTokenNotFoundError("token length mismatch")
        secret_hash = _hash_secret(secret_bytes)

        row = self._db.execute(
            """
            SELECT token_id, secret_hash, session_id, fleet, agent_name,
                   purpose, created_at, expires_at, last_used_at, revoked_at
            FROM bearer_tokens
            WHERE secret_hash = ?
            """,
            (secret_hash,),
        ).fetchone()
        if row is None:
            raise BearerTokenNotFoundError("no matching token")

        # Defense-in-depth: constant-time compare in Python too. A
        # SQLite index hit already implies bytewise equality, but a
        # future migration to a different store shouldn't quietly
        # introduce a timing oracle.
        if not hmac.compare_digest(row["secret_hash"], secret_hash):
            raise BearerTokenNotFoundError("hash mismatch")  # pragma: no cover

        now_val = now if now is not None else time.time()
        revoked_at = row["revoked_at"]
        expires_at = float(row["expires_at"])

        if revoked_at is not None:
            raise BearerTokenRevokedError(
                f"token {row['token_id']!r} was revoked at {revoked_at}"
            )
        if now_val >= expires_at:
            raise BearerTokenExpiredError(
                f"token {row['token_id']!r} expired at {expires_at}"
            )

        if touch:
            # Separate UPDATE. Failure here doesn't fail the validation;
            # the claims are already authoritative.
            with self._txn():
                self._db.execute(
                    "UPDATE bearer_tokens SET last_used_at = ? "
                    "WHERE token_id = ?",
                    (now_val, row["token_id"]),
                )
            last_used_at: float | None = now_val
        else:
            last_used_at = (
                float(row["last_used_at"]) if row["last_used_at"] is not None else None
            )

        return BearerTokenClaims(
            token_id=row["token_id"],
            session_id=row["session_id"],
            fleet=row["fleet"],
            agent_name=row["agent_name"],
            purpose=row["purpose"],
            created_at=float(row["created_at"]),
            expires_at=expires_at,
            last_used_at=last_used_at,
            revoked_at=None,
        )

    # -- introspection (no secret material) --

    def get_claims(self, token_id: str) -> BearerTokenClaims:
        """Return claims for *token_id* without requiring the secret.

        For admin/diagnostic tooling — e.g. "what session does this
        token belong to". Returns even revoked/expired claims so the
        operator can see what happened. Raises
        :class:`BearerTokenNotFoundError` if the id is unknown.
        """
        row = self._db.execute(
            """
            SELECT token_id, session_id, fleet, agent_name, purpose,
                   created_at, expires_at, last_used_at, revoked_at
            FROM bearer_tokens WHERE token_id = ?
            """,
            (token_id,),
        ).fetchone()
        if row is None:
            raise BearerTokenNotFoundError(f"no token with id {token_id!r}")
        return _row_to_claims(row)

    def list_for_session(self, session_id: str) -> list[BearerTokenClaims]:
        """All claims (active and revoked) for a session, oldest first."""
        rows = self._db.execute(
            """
            SELECT token_id, session_id, fleet, agent_name, purpose,
                   created_at, expires_at, last_used_at, revoked_at
            FROM bearer_tokens
            WHERE session_id = ?
            ORDER BY created_at
            """,
            (session_id,),
        ).fetchall()
        return [_row_to_claims(r) for r in rows]

    def list_for_identity(
        self, *, fleet: str, agent_name: str, purpose: str
    ) -> list[BearerTokenClaims]:
        """All claims for an identity tuple, oldest first."""
        rows = self._db.execute(
            """
            SELECT token_id, session_id, fleet, agent_name, purpose,
                   created_at, expires_at, last_used_at, revoked_at
            FROM bearer_tokens
            WHERE fleet = ? AND agent_name = ? AND purpose = ?
            ORDER BY created_at
            """,
            (fleet, agent_name, purpose),
        ).fetchall()
        return [_row_to_claims(r) for r in rows]

    def iter_active(self, *, now: float | None = None) -> Iterable[BearerTokenClaims]:
        """Yield every currently-valid claim (non-revoked, non-expired)."""
        now_val = now if now is not None else time.time()
        rows = self._db.execute(
            """
            SELECT token_id, session_id, fleet, agent_name, purpose,
                   created_at, expires_at, last_used_at, revoked_at
            FROM bearer_tokens
            WHERE revoked_at IS NULL AND expires_at > ?
            ORDER BY created_at
            """,
            (now_val,),
        ).fetchall()
        for r in rows:
            yield _row_to_claims(r)

    # -- invalidate --

    def invalidate(self, token_id: str, *, now: float | None = None) -> bool:
        """Mark a single token revoked. Returns True if a row was touched.

        Already-revoked tokens are left alone (idempotent — return
        False so callers can distinguish "this was the revoke that
        landed" from "already done").
        """
        now_val = now if now is not None else time.time()
        with self._txn():
            cursor = self._db.execute(
                "UPDATE bearer_tokens SET revoked_at = ? "
                "WHERE token_id = ? AND revoked_at IS NULL",
                (now_val, token_id),
            )
            return cursor.rowcount > 0

    def invalidate_session(self, session_id: str, *, now: float | None = None) -> int:
        """Revoke every outstanding token for a session. Returns the count.

        Use when a streaming session ends or restarts — the new
        session should re-mint, not inherit the old session's
        capability set.
        """
        now_val = now if now is not None else time.time()
        with self._txn():
            cursor = self._db.execute(
                "UPDATE bearer_tokens SET revoked_at = ? "
                "WHERE session_id = ? AND revoked_at IS NULL",
                (now_val, session_id),
            )
            return cursor.rowcount

    def invalidate_identity(
        self,
        *,
        fleet: str,
        agent_name: str,
        purpose: str,
        now: float | None = None,
    ) -> int:
        """Revoke every outstanding token for an identity tuple.

        Use when the registry retires/revokes the underlying key —
        outstanding session capabilities for that identity should die
        with it, even though the tokens technically refer to the tuple
        rather than the kid. Counterpart on the public side to
        :meth:`pinky_identity.DaemonSigner._resolve`'s KEY_ACTIVE check.
        """
        now_val = now if now is not None else time.time()
        with self._txn():
            cursor = self._db.execute(
                "UPDATE bearer_tokens SET revoked_at = ? "
                "WHERE fleet = ? AND agent_name = ? AND purpose = ? "
                "AND revoked_at IS NULL",
                (now_val, fleet, agent_name, purpose),
            )
            return cursor.rowcount

    def purge_expired(self, *, now: float | None = None) -> int:
        """Delete expired and revoked rows. Returns the count deleted.

        Optional housekeeping; the validator already rejects expired
        and revoked rows, so this is purely about reclaiming disk. Safe
        to call on a timer.
        """
        now_val = now if now is not None else time.time()
        with self._txn():
            cursor = self._db.execute(
                "DELETE FROM bearer_tokens "
                "WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (now_val,),
            )
            return cursor.rowcount

    # -- bookkeeping --

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "BearerTokenStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _txn(self):
        # Matches signer_store: autocommit connection wrapped in
        # explicit BEGIN/COMMIT so multi-statement writes are atomic.
        self._db.execute("BEGIN")
        try:
            yield
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def __repr__(self) -> str:
        return f"BearerTokenStore(db_path={str(self._db_path)!r})"


# -- helpers ----------------------------------------------------------------


def _row_to_claims(row: sqlite3.Row) -> BearerTokenClaims:
    """Build a :class:`BearerTokenClaims` from a SQLite row."""
    return BearerTokenClaims(
        token_id=row["token_id"],
        session_id=row["session_id"],
        fleet=row["fleet"],
        agent_name=row["agent_name"],
        purpose=row["purpose"],
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        last_used_at=(
            float(row["last_used_at"]) if row["last_used_at"] is not None else None
        ),
        revoked_at=(
            float(row["revoked_at"]) if row["revoked_at"] is not None else None
        ),
    )


__all__ = [
    "DEFAULT_BEARER_TOKEN_STORE_PATH",
    "DEFAULT_TOKEN_TTL",
    "TOKEN_SECRET_BYTES",
    "BearerTokenClaims",
    "BearerTokenError",
    "BearerTokenExpiredError",
    "BearerTokenNotFoundError",
    "BearerTokenRevokedError",
    "BearerTokenStore",
    "MintResult",
]
