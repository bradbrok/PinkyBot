"""Tests for pinky_identity.bearer_tokens (BearerTokenStore + primitives)."""

from __future__ import annotations

import base64
import secrets
import time
from datetime import timedelta

import pytest

from pinky_identity import (
    DEFAULT_TOKEN_TTL,
    TOKEN_SECRET_BYTES,
    BearerTokenClaims,
    BearerTokenError,
    BearerTokenExpiredError,
    BearerTokenNotFoundError,
    BearerTokenRevokedError,
    BearerTokenStore,
    MintResult,
    SignatureError,
)

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = BearerTokenStore(db_path=tmp_path / "bearer.db")
    try:
        yield s
    finally:
        s.close()


def _mint_default(store: BearerTokenStore, **overrides) -> MintResult:
    """Mint a token with sensible defaults so each test only spells the
    fields it cares about."""
    kwargs = dict(
        session_id="sess-1",
        agent_name="barsik",
        fleet="pinky",
        purpose="service-auth",
    )
    kwargs.update(overrides)
    return store.mint(**kwargs)


# -- Construction ------------------------------------------------------------


def test_store_creates_db_file(tmp_path):
    path = tmp_path / "nested" / "bearer.db"
    assert not path.exists()
    BearerTokenStore(db_path=path).close()
    assert path.exists()


def test_token_secret_constants():
    # If TOKEN_SECRET_BYTES drops below 16 (128-bit security), tests
    # should fail loudly — bearer tokens are the gate to the signer.
    assert TOKEN_SECRET_BYTES >= 16
    assert DEFAULT_TOKEN_TTL.total_seconds() > 0


# -- Mint --------------------------------------------------------------------


def test_mint_returns_secret_token_and_claims(store):
    result = _mint_default(store)
    assert isinstance(result, MintResult)
    # base64url-no-padding of 32 bytes is 43 chars.
    assert len(result.token) == 43
    # token_id is the first 16 hex chars of sha256(secret).
    assert len(result.token_id) == 16
    # All claims fields populated, last_used_at/revoked_at clean.
    c = result.claims
    assert c.session_id == "sess-1"
    assert c.agent_name == "barsik"
    assert c.fleet == "pinky"
    assert c.purpose == "service-auth"
    assert c.last_used_at is None
    assert c.revoked_at is None
    # Expires roughly 1h out (DEFAULT_TOKEN_TTL).
    assert c.expires_at - c.created_at == pytest.approx(
        DEFAULT_TOKEN_TTL.total_seconds(), abs=1.0
    )


def test_mint_distinct_tokens_have_distinct_secrets(store):
    """Sanity check on secrets.token_bytes — every mint produces a
    fresh random secret."""
    t1 = _mint_default(store).token
    t2 = _mint_default(store).token
    assert t1 != t2


def test_mint_with_explicit_ttl(store):
    result = _mint_default(store, ttl=timedelta(minutes=5))
    delta = result.claims.expires_at - result.claims.created_at
    assert delta == pytest.approx(300.0, abs=1.0)


def test_mint_with_explicit_expires_at(store):
    now = time.time()
    explicit = now + 600.0
    result = _mint_default(store, expires_at=explicit, now=now)
    assert result.claims.expires_at == pytest.approx(explicit, abs=0.01)


def test_mint_rejects_both_ttl_and_expires_at(store):
    now = time.time()
    with pytest.raises(ValueError, match="at most one"):
        _mint_default(
            store,
            ttl=timedelta(minutes=5),
            expires_at=now + 600,
            now=now,
        )


def test_mint_rejects_past_expires_at(store):
    now = time.time()
    with pytest.raises(ValueError, match="must be in the future"):
        _mint_default(store, expires_at=now - 1, now=now)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", ""),
        ("agent_name", ""),
        ("fleet", ""),
        ("purpose", ""),
    ],
)
def test_mint_rejects_empty_required_fields(store, field, value):
    with pytest.raises(ValueError):
        _mint_default(store, **{field: value})


# -- Validate: happy path ---------------------------------------------------


def test_validate_round_trips(store):
    result = _mint_default(store)
    claims = store.validate(result.token)
    assert claims.token_id == result.token_id
    assert claims.session_id == "sess-1"
    assert claims.agent_name == "barsik"
    assert claims.fleet == "pinky"
    assert claims.purpose == "service-auth"
    assert claims.revoked_at is None


def test_validate_bumps_last_used_at_by_default(store):
    result = _mint_default(store)
    assert result.claims.last_used_at is None
    before = time.time()
    claims = store.validate(result.token)
    assert claims.last_used_at is not None
    assert claims.last_used_at >= before
    # Persists across calls.
    persisted = store.get_claims(result.token_id)
    assert persisted.last_used_at == claims.last_used_at


def test_validate_does_not_touch_when_touch_false(store):
    result = _mint_default(store)
    claims = store.validate(result.token, touch=False)
    assert claims.last_used_at is None
    persisted = store.get_claims(result.token_id)
    assert persisted.last_used_at is None


# -- Validate: failure modes ------------------------------------------------


def test_validate_rejects_unknown_token(store):
    # Mint nothing; present a syntactically valid but unknown token.
    fake = base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_SECRET_BYTES))
    fake_str = fake.rstrip(b"=").decode("ascii")
    with pytest.raises(BearerTokenNotFoundError):
        store.validate(fake_str)


def test_validate_rejects_malformed_token(store):
    with pytest.raises(BearerTokenNotFoundError):
        store.validate("not!valid!base64!")


def test_validate_rejects_empty_token(store):
    with pytest.raises(BearerTokenNotFoundError):
        store.validate("")


def test_validate_rejects_oversized_token(store):
    # Don't burn CPU hashing arbitrary attacker input — the decoder
    # bails on tokens far longer than the legitimate size.
    big = "A" * (4 * TOKEN_SECRET_BYTES + 1)
    with pytest.raises(BearerTokenNotFoundError):
        store.validate(big)


def test_validate_rejects_wrong_length_token(store):
    # Valid base64url but decodes to wrong-length secret bytes — treat
    # as not-found, same as forgery.
    short = base64.urlsafe_b64encode(b"\x00" * 8).rstrip(b"=").decode("ascii")
    with pytest.raises(BearerTokenNotFoundError):
        store.validate(short)


def test_validate_rejects_expired_token(store):
    now = time.time()
    result = _mint_default(store, ttl=timedelta(seconds=10), now=now)
    # Validate "in the future" past expiry.
    with pytest.raises(BearerTokenExpiredError) as exc:
        store.validate(result.token, now=now + 11)
    assert result.token_id in str(exc.value)


def test_validate_rejects_revoked_token(store):
    result = _mint_default(store)
    store.invalidate(result.token_id)
    with pytest.raises(BearerTokenRevokedError) as exc:
        store.validate(result.token)
    assert result.token_id in str(exc.value)


def test_validate_does_not_touch_on_failure(store):
    """An expired/revoked token's last_used_at must NOT advance — the
    audit trail should reflect the last successful use, not failed
    attempts."""
    now = time.time()
    result = _mint_default(store, ttl=timedelta(seconds=10), now=now)
    with pytest.raises(BearerTokenExpiredError):
        store.validate(result.token, now=now + 11)
    persisted = store.get_claims(result.token_id)
    assert persisted.last_used_at is None


# -- Revocation -------------------------------------------------------------


def test_invalidate_returns_true_on_first_call_false_on_second(store):
    result = _mint_default(store)
    assert store.invalidate(result.token_id) is True
    # Idempotent: already revoked → no-op, returns False.
    assert store.invalidate(result.token_id) is False


def test_invalidate_unknown_token_id_returns_false(store):
    assert store.invalidate("deadbeefdeadbeef") is False


def test_invalidate_session_revokes_all_tokens_for_session(store):
    a = _mint_default(store, session_id="sess-A")
    b = _mint_default(store, session_id="sess-A", agent_name="other")
    c = _mint_default(store, session_id="sess-B")

    count = store.invalidate_session("sess-A")
    assert count == 2

    with pytest.raises(BearerTokenRevokedError):
        store.validate(a.token)
    with pytest.raises(BearerTokenRevokedError):
        store.validate(b.token)
    # Other session unaffected.
    store.validate(c.token)


def test_invalidate_session_unknown_id_returns_zero(store):
    assert store.invalidate_session("does-not-exist") == 0


def test_invalidate_identity_revokes_all_tokens_for_tuple(store):
    # Same identity, different sessions.
    a = _mint_default(store, session_id="sess-A")
    b = _mint_default(store, session_id="sess-B")
    # Different agent — should be untouched.
    c = _mint_default(store, session_id="sess-C", agent_name="murzik")

    count = store.invalidate_identity(
        fleet="pinky", agent_name="barsik", purpose="service-auth"
    )
    assert count == 2

    with pytest.raises(BearerTokenRevokedError):
        store.validate(a.token)
    with pytest.raises(BearerTokenRevokedError):
        store.validate(b.token)
    store.validate(c.token)


def test_invalidate_skips_already_revoked_rows(store):
    """Bulk revoke shouldn't double-stamp revoked_at — the timestamp
    should reflect the original revocation, not a later sweep."""
    result = _mint_default(store)
    first = time.time()
    store.invalidate(result.token_id, now=first)
    first_persisted = store.get_claims(result.token_id)
    assert first_persisted.revoked_at == pytest.approx(first, abs=0.01)

    # Bulk revoke later — must not overwrite.
    later = first + 100
    count = store.invalidate_session("sess-1", now=later)
    assert count == 0  # already revoked
    second_persisted = store.get_claims(result.token_id)
    assert second_persisted.revoked_at == pytest.approx(first, abs=0.01)


# -- Introspection ----------------------------------------------------------


def test_get_claims_returns_revoked_claims_for_audit(store):
    """Operator tooling should see what happened to a revoked token."""
    result = _mint_default(store)
    store.invalidate(result.token_id)
    claims = store.get_claims(result.token_id)
    assert claims.revoked_at is not None
    assert claims.token_id == result.token_id


def test_get_claims_raises_on_unknown_id(store):
    with pytest.raises(BearerTokenNotFoundError):
        store.get_claims("deadbeefdeadbeef")


def test_list_for_session_returns_in_age_order(store):
    now = time.time()
    a = store.mint(
        session_id="sess-A", agent_name="x", fleet="pinky",
        purpose="service-auth", now=now,
    )
    b = store.mint(
        session_id="sess-A", agent_name="x", fleet="pinky",
        purpose="service-auth", now=now + 1,
    )
    rows = store.list_for_session("sess-A")
    assert [r.token_id for r in rows] == [a.token_id, b.token_id]


def test_list_for_identity_filters_correctly(store):
    a = _mint_default(store, agent_name="alpha")
    _b = _mint_default(store, agent_name="beta")
    rows = store.list_for_identity(
        fleet="pinky", agent_name="alpha", purpose="service-auth"
    )
    assert [r.token_id for r in rows] == [a.token_id]


def test_iter_active_excludes_revoked_and_expired(store):
    now = time.time()
    active = _mint_default(store, session_id="a", now=now)
    revoked = _mint_default(store, session_id="b", now=now)
    expired = _mint_default(
        store, session_id="c", ttl=timedelta(seconds=1), now=now
    )
    store.invalidate(revoked.token_id, now=now)

    # iter_active "now" is later than expired but earlier than active expiry.
    snapshot = list(store.iter_active(now=now + 5))
    ids = {c.token_id for c in snapshot}
    assert active.token_id in ids
    assert revoked.token_id not in ids
    assert expired.token_id not in ids


# -- Purging ----------------------------------------------------------------


def test_purge_expired_removes_expired_and_revoked(store):
    now = time.time()
    active = _mint_default(store, session_id="a", now=now)
    revoked = _mint_default(store, session_id="b", now=now)
    expired = _mint_default(
        store, session_id="c", ttl=timedelta(seconds=1), now=now
    )
    store.invalidate(revoked.token_id, now=now)

    count = store.purge_expired(now=now + 5)
    assert count == 2  # revoked + expired

    # Active still there.
    store.validate(active.token)
    # Revoked + expired are gone — get_claims now raises.
    with pytest.raises(BearerTokenNotFoundError):
        store.get_claims(revoked.token_id)
    with pytest.raises(BearerTokenNotFoundError):
        store.get_claims(expired.token_id)


def test_purge_expired_leaves_active_alone(store):
    now = time.time()
    a = _mint_default(store, now=now, ttl=timedelta(hours=1))
    assert store.purge_expired(now=now) == 0
    store.validate(a.token, now=now)


# -- Error class structure --------------------------------------------------


def test_all_bearer_errors_inherit_from_bearer_token_error():
    for cls in [
        BearerTokenNotFoundError,
        BearerTokenExpiredError,
        BearerTokenRevokedError,
    ]:
        assert issubclass(cls, BearerTokenError)


def test_bearer_token_error_is_signature_error_subclass(store):
    """Lets callers ``except SignatureError`` and catch every identity-
    side failure in one place — including bearer-token failures."""
    with pytest.raises(SignatureError):
        store.validate("clearly-not-a-token")


# -- Persistence across reopens ---------------------------------------------


def test_token_survives_store_close_and_reopen(tmp_path):
    """The point of a SQLite-backed store: state lives past process
    restart. A token minted in one BearerTokenStore instance must
    validate in a fresh instance pointing at the same file."""
    path = tmp_path / "bearer.db"
    s1 = BearerTokenStore(db_path=path)
    result = s1.mint(
        session_id="sess", agent_name="alpha", fleet="pinky",
        purpose="service-auth",
    )
    s1.close()

    s2 = BearerTokenStore(db_path=path)
    try:
        claims = s2.validate(result.token)
        assert claims.token_id == result.token_id
    finally:
        s2.close()


def test_context_manager_closes_store(tmp_path):
    path = tmp_path / "bearer.db"
    with BearerTokenStore(db_path=path) as s:
        s.mint(
            session_id="x", agent_name="x", fleet="x", purpose="x"
        )
    # Reopening must succeed (close released the connection).
    BearerTokenStore(db_path=path).close()


# -- Claims dataclass -------------------------------------------------------


def test_claims_is_expired_property():
    now = time.time()
    fresh = BearerTokenClaims(
        token_id="t", session_id="s", fleet="pinky",
        agent_name="a", purpose="p",
        created_at=now, expires_at=now + 100,
        last_used_at=None, revoked_at=None,
    )
    expired = BearerTokenClaims(
        token_id="t", session_id="s", fleet="pinky",
        agent_name="a", purpose="p",
        created_at=now - 200, expires_at=now - 1,
        last_used_at=None, revoked_at=None,
    )
    assert not fresh.is_expired
    assert expired.is_expired


# -- Secret redaction in repr -----------------------------------------------
#
# Regression for Murzik's PR-4b-3 review: MintResult must NOT leak the
# raw token through default dataclass repr — a stray debug log would
# otherwise stamp the full signer-authority secret into a logfile.


def test_mint_result_repr_does_not_leak_token(store):
    result = _mint_default(store)
    text = repr(result)
    assert result.token not in text
    # Make the redaction marker visible to reviewers grepping for
    # leaked tokens — they'll see the safe form explicitly.
    assert "<redacted>" in text
    # token_id and claims are still in the repr (they're not the
    # credential and are useful for diagnostics).
    assert result.token_id in text


def test_mint_result_str_does_not_leak_token(store):
    """``str(result)`` falls back to ``__repr__`` for dataclasses;
    verify the redaction holds through both surfaces."""
    result = _mint_default(store)
    assert result.token not in str(result)
    assert "<redacted>" in str(result)


def test_mint_result_format_does_not_leak_token(store):
    """The most common accidental leak is ``f"{result}"`` in a log
    statement. Lock down via the same custom repr."""
    result = _mint_default(store)
    formatted = f"{result}"
    assert result.token not in formatted
    assert "<redacted>" in formatted


def test_mint_result_token_still_accessible_as_attribute(store):
    """Redaction is for diagnostics, not access — callers explicitly
    reading ``result.token`` must still get the secret."""
    result = _mint_default(store)
    # Validates: the attribute holds the actual base64url token.
    claims = store.validate(result.token)
    assert claims.token_id == result.token_id


# -- Strict no-padding parsing ----------------------------------------------
#
# Murzik's non-blocking note: the encoder emits no padding, so the
# decoder shouldn't accept padded variants either. Reduces the
# canonical-form surface to exactly one string per secret.


def test_validate_rejects_padded_token(store):
    """A padded variant of a real token must be rejected even though
    it base64-decodes to the same secret."""
    result = _mint_default(store)
    # Real tokens are 43 chars (32 bytes); pad up to multiple of 4.
    padded = result.token + "="
    with pytest.raises(BearerTokenNotFoundError, match="padding"):
        store.validate(padded)
    # The canonical form still validates fine.
    store.validate(result.token)


def test_validate_rejects_token_with_internal_equals(store):
    """``=`` only appears as base64 padding; rejecting anywhere in the
    string is fine and slightly cheaper than position-aware checks."""
    with pytest.raises(BearerTokenNotFoundError, match="padding"):
        store.validate("AAAA=AAAA")


def test_claims_is_revoked_property():
    now = time.time()
    alive = BearerTokenClaims(
        token_id="t", session_id="s", fleet="pinky",
        agent_name="a", purpose="p",
        created_at=now, expires_at=now + 100,
        last_used_at=None, revoked_at=None,
    )
    dead = BearerTokenClaims(
        token_id="t", session_id="s", fleet="pinky",
        agent_name="a", purpose="p",
        created_at=now, expires_at=now + 100,
        last_used_at=None, revoked_at=now,
    )
    assert not alive.is_revoked
    assert dead.is_revoked
