"""TOFU trust policy tests.

Exercises the state machine end-to-end:

    (none) → first_seen → pinned → changed → pinned   (accept_rotation)
                                  → rejected          (reject_rotation)
                       → verified                     (verify)
"""

from __future__ import annotations

import pytest

from pinky_federation.keys import EncryptionKeyPair, SigningKeyPair
from pinky_federation.state import (
    PEER_PIN_CHANGED,
    PEER_PIN_FIRST_SEEN,
    PEER_PIN_PINNED,
    PEER_PIN_REJECTED,
    PEER_PIN_VERIFIED,
    FederationStateStore,
)
from pinky_federation.tofu import (
    NoPendingRotationError,
    TofuError,
    TrustDecision,
    TrustPolicy,
    UnknownPeerError,
)

ADDR = "alice@example.com"


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "fed.db"
    s = FederationStateStore(str(db_path))
    yield s
    s.close()


@pytest.fixture
def policy(store: FederationStateStore) -> TrustPolicy:
    return TrustPolicy(store)


def _new_keys() -> tuple[SigningKeyPair, EncryptionKeyPair]:
    """Fresh random signing + encryption keypairs."""
    return SigningKeyPair.generate(), EncryptionKeyPair.generate()


# -- first observation (TOFU auto-pin) -----------------------------------


def test_first_observation_auto_pins(policy: TrustPolicy) -> None:
    sig, enc = _new_keys()
    result = policy.observe(ADDR, sig.public_key, enc.public_key)
    assert result.decision == TrustDecision.FIRST_SEEN
    assert result.record.status == PEER_PIN_FIRST_SEEN
    assert result.record.sig_pk == sig.public_key.to_bytes()
    assert result.record.enc_pk == enc.public_key.to_bytes()
    assert result.record.fingerprint  # non-empty, 16 bytes
    assert len(result.record.fingerprint) == 16
    assert result.record.first_seen > 0
    # first_seen and last_seen are both set to ~now (within the same call);
    # we tolerate tiny clock drift between the two timestamp captures.
    assert abs(result.record.first_seen - result.record.last_seen) < 0.1
    assert result.trusted


def test_first_seen_is_trusted_but_unverified(policy: TrustPolicy) -> None:
    sig, enc = _new_keys()
    result = policy.observe(ADDR, sig.public_key, enc.public_key)
    assert result.trusted is True
    assert result.record.status != PEER_PIN_VERIFIED


# -- repeat observation with same keys -----------------------------------


def test_second_observation_matching_promotes_to_pinned(
    policy: TrustPolicy,
) -> None:
    sig, enc = _new_keys()
    first = policy.observe(ADDR, sig.public_key, enc.public_key)
    assert first.record.status == PEER_PIN_FIRST_SEEN

    second = policy.observe(ADDR, sig.public_key, enc.public_key)
    assert second.decision == TrustDecision.TRUSTED
    assert second.record.status == PEER_PIN_PINNED
    assert second.record.last_seen >= first.record.last_seen


def test_repeat_observation_pinned_is_idempotent(policy: TrustPolicy) -> None:
    sig, enc = _new_keys()
    policy.observe(ADDR, sig.public_key, enc.public_key)
    policy.observe(ADDR, sig.public_key, enc.public_key)  # → pinned
    third = policy.observe(ADDR, sig.public_key, enc.public_key)
    assert third.decision == TrustDecision.TRUSTED
    assert third.record.status == PEER_PIN_PINNED


# -- mismatch detection --------------------------------------------------


def test_different_fingerprint_triggers_mismatch(policy: TrustPolicy) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # → pinned

    sig2, enc2 = _new_keys()
    result = policy.observe(ADDR, sig2.public_key, enc2.public_key)
    assert result.decision == TrustDecision.MISMATCH
    assert result.record.status == PEER_PIN_CHANGED
    # Primary slot still holds the ORIGINAL pin — we don't silently overwrite.
    assert result.record.sig_pk == sig1.public_key.to_bytes()
    assert result.record.enc_pk == enc1.public_key.to_bytes()
    # Pending slot captured the new proposal.
    assert result.record.pending_sig_pk == sig2.public_key.to_bytes()
    assert result.record.pending_enc_pk == enc2.public_key.to_bytes()
    assert result.record.pending_fingerprint != result.record.fingerprint
    assert result.record.pending_first_seen > 0
    assert not result.trusted


def test_mismatch_from_first_seen_state(policy: TrustPolicy) -> None:
    """Mismatch should also trigger if we only have a first_seen pin, not
    a fully promoted pinned one."""
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # first_seen

    sig2, enc2 = _new_keys()
    result = policy.observe(ADDR, sig2.public_key, enc2.public_key)
    assert result.decision == TrustDecision.MISMATCH
    assert result.record.status == PEER_PIN_CHANGED


def test_mismatch_is_not_silently_accepted_on_repeat(policy: TrustPolicy) -> None:
    """A second observation with the same mismatched keys does NOT promote
    the proposal — it stays in changed state."""
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # pinned

    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)  # mismatch
    result = policy.observe(ADDR, sig2.public_key, enc2.public_key)  # again
    assert result.decision == TrustDecision.MISMATCH
    assert result.record.status == PEER_PIN_CHANGED
    # Primary pin was NOT overwritten by the repeat.
    assert result.record.sig_pk == sig1.public_key.to_bytes()


def test_mismatch_pending_updates_with_yet_another_fp(policy: TrustPolicy) -> None:
    """If, while in changed state, a third distinct fingerprint arrives,
    the pending slot is updated to the latest — the operator sees the
    freshest attacker/rotating-peer keys."""
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # pinned

    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)  # mismatch → changed

    sig3, enc3 = _new_keys()
    result = policy.observe(ADDR, sig3.public_key, enc3.public_key)
    assert result.decision == TrustDecision.MISMATCH
    assert result.record.status == PEER_PIN_CHANGED
    assert result.record.pending_sig_pk == sig3.public_key.to_bytes()


def test_pinned_peer_returning_to_original_fp_from_changed_clears_pending(
    policy: TrustPolicy,
) -> None:
    """If we're in changed state and the peer shows up with the ORIGINAL
    pinned fingerprint again, the proposal was a blip — clear pending,
    back to pinned."""
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # pinned

    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)  # changed

    result = policy.observe(ADDR, sig1.public_key, enc1.public_key)
    assert result.decision == TrustDecision.TRUSTED
    assert result.record.status == PEER_PIN_PINNED
    assert result.record.pending_fingerprint == b""
    assert result.record.pending_sig_pk == b""


# -- accept_rotation -----------------------------------------------------


def test_accept_rotation_promotes_pending(policy: TrustPolicy) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # pinned

    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)  # mismatch

    record = policy.accept_rotation(ADDR)
    assert record.status == PEER_PIN_PINNED
    assert record.sig_pk == sig2.public_key.to_bytes()
    assert record.enc_pk == enc2.public_key.to_bytes()
    assert record.pending_fingerprint == b""
    assert record.pending_sig_pk == b""
    # Subsequent lookups with new keys are now TRUSTED.
    result = policy.observe(ADDR, sig2.public_key, enc2.public_key)
    assert result.decision == TrustDecision.TRUSTED


def test_accept_rotation_with_explicit_keys_overrides_pending(
    policy: TrustPolicy,
) -> None:
    """Operator provides out-of-band verified keys — those win over whatever
    was in the pending slot."""
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # pinned

    # attacker-style pending proposal
    sig_bad, enc_bad = _new_keys()
    policy.observe(ADDR, sig_bad.public_key, enc_bad.public_key)

    # operator verifies a different set of keys out-of-band
    sig_good, enc_good = _new_keys()
    record = policy.accept_rotation(ADDR, sig_good.public_key, enc_good.public_key)
    assert record.sig_pk == sig_good.public_key.to_bytes()
    assert record.enc_pk == enc_good.public_key.to_bytes()
    assert record.status == PEER_PIN_PINNED
    assert record.pending_fingerprint == b""


def test_accept_rotation_without_pending_or_keys_raises(
    policy: TrustPolicy,
) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # first_seen, no pending

    with pytest.raises(NoPendingRotationError):
        policy.accept_rotation(ADDR)


def test_accept_rotation_partial_keys_raises(policy: TrustPolicy) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)

    sig2, _enc2 = _new_keys()
    with pytest.raises(ValueError):
        policy.accept_rotation(ADDR, sig_pk=sig2.public_key)


def test_accept_rotation_unknown_peer_raises(policy: TrustPolicy) -> None:
    with pytest.raises(UnknownPeerError):
        policy.accept_rotation("nobody@example.com")


# -- reject_rotation -----------------------------------------------------


def test_reject_rotation_sets_status_and_clears_pending(
    policy: TrustPolicy,
) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)  # pinned

    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)  # mismatch

    record = policy.reject_rotation(ADDR)
    assert record.status == PEER_PIN_REJECTED
    assert record.pending_fingerprint == b""
    assert record.pending_sig_pk == b""


def test_rejected_peer_observe_returns_rejected_regardless(
    policy: TrustPolicy,
) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)

    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)
    policy.reject_rotation(ADDR)

    # Even the "original" keys should now return REJECTED.
    r1 = policy.observe(ADDR, sig1.public_key, enc1.public_key)
    assert r1.decision == TrustDecision.REJECTED
    # And any new keys.
    sig3, enc3 = _new_keys()
    r2 = policy.observe(ADDR, sig3.public_key, enc3.public_key)
    assert r2.decision == TrustDecision.REJECTED


def test_rejected_peer_can_be_re_accepted_by_operator(policy: TrustPolicy) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)
    policy.reject_rotation(ADDR)

    # Operator changes mind.
    sig3, enc3 = _new_keys()
    record = policy.accept_rotation(ADDR, sig3.public_key, enc3.public_key)
    assert record.status == PEER_PIN_PINNED

    result = policy.observe(ADDR, sig3.public_key, enc3.public_key)
    assert result.decision == TrustDecision.TRUSTED


def test_reject_rotation_unknown_peer_raises(policy: TrustPolicy) -> None:
    with pytest.raises(UnknownPeerError):
        policy.reject_rotation("nobody@example.com")


# -- verify --------------------------------------------------------------


def test_verify_promotes_pinned_to_verified(policy: TrustPolicy) -> None:
    sig, enc = _new_keys()
    policy.observe(ADDR, sig.public_key, enc.public_key)
    policy.observe(ADDR, sig.public_key, enc.public_key)  # pinned

    record = policy.verify(ADDR)
    assert record.status == PEER_PIN_VERIFIED
    assert record.verified_at > 0


def test_verify_on_first_seen_is_allowed(policy: TrustPolicy) -> None:
    sig, enc = _new_keys()
    policy.observe(ADDR, sig.public_key, enc.public_key)

    record = policy.verify(ADDR)
    assert record.status == PEER_PIN_VERIFIED


def test_verify_refuses_changed_state(policy: TrustPolicy) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)

    with pytest.raises(TofuError):
        policy.verify(ADDR)


def test_verify_refuses_rejected_state(policy: TrustPolicy) -> None:
    sig1, enc1 = _new_keys()
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    policy.observe(ADDR, sig1.public_key, enc1.public_key)
    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)
    policy.reject_rotation(ADDR)

    with pytest.raises(TofuError):
        policy.verify(ADDR)


def test_verify_unknown_peer_raises(policy: TrustPolicy) -> None:
    with pytest.raises(UnknownPeerError):
        policy.verify("nobody@example.com")


def test_verified_peer_observation_still_trusted(policy: TrustPolicy) -> None:
    sig, enc = _new_keys()
    policy.observe(ADDR, sig.public_key, enc.public_key)
    policy.verify(ADDR)

    result = policy.observe(ADDR, sig.public_key, enc.public_key)
    assert result.decision == TrustDecision.TRUSTED
    # verify() state persists across subsequent identical observations.
    assert result.record.status == PEER_PIN_VERIFIED


def test_accept_rotation_clears_verified_flag(policy: TrustPolicy) -> None:
    """A rotation invalidates prior verification — operator must re-verify."""
    sig, enc = _new_keys()
    policy.observe(ADDR, sig.public_key, enc.public_key)
    policy.verify(ADDR)

    sig2, enc2 = _new_keys()
    policy.observe(ADDR, sig2.public_key, enc2.public_key)
    record = policy.accept_rotation(ADDR)
    assert record.status == PEER_PIN_PINNED
    assert record.verified_at == 0


# -- list_changed --------------------------------------------------------


def test_list_changed_returns_only_changed_peers(policy: TrustPolicy) -> None:
    # Peer with no mismatch.
    sig_a, enc_a = _new_keys()
    policy.observe("a@example.com", sig_a.public_key, enc_a.public_key)
    policy.observe("a@example.com", sig_a.public_key, enc_a.public_key)

    # Peer in changed state.
    sig_b1, enc_b1 = _new_keys()
    policy.observe("b@example.com", sig_b1.public_key, enc_b1.public_key)
    policy.observe("b@example.com", sig_b1.public_key, enc_b1.public_key)
    sig_b2, enc_b2 = _new_keys()
    policy.observe("b@example.com", sig_b2.public_key, enc_b2.public_key)

    # Peer that was rejected.
    sig_c1, enc_c1 = _new_keys()
    policy.observe("c@example.com", sig_c1.public_key, enc_c1.public_key)
    policy.observe("c@example.com", sig_c1.public_key, enc_c1.public_key)
    sig_c2, enc_c2 = _new_keys()
    policy.observe("c@example.com", sig_c2.public_key, enc_c2.public_key)
    policy.reject_rotation("c@example.com")

    changed = policy.list_changed()
    addrs = {r.peer_address for r in changed}
    assert addrs == {"b@example.com"}


# -- get -----------------------------------------------------------------


def test_get_returns_none_for_unknown(policy: TrustPolicy) -> None:
    assert policy.get("nobody@example.com") is None


def test_get_returns_record_for_known(policy: TrustPolicy) -> None:
    sig, enc = _new_keys()
    policy.observe(ADDR, sig.public_key, enc.public_key)
    record = policy.get(ADDR)
    assert record is not None
    assert record.peer_address == ADDR
