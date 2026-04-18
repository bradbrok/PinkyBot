"""Trust-on-first-use (TOFU) pinning policy for federation peers.

This module is the **local trust root** for federation v0.2. The relay
directory is treated as a lookup service, not an authority: the first time
we see a peer's public keys we pin them locally, and every subsequent
lookup is compared against the pinned fingerprint. A mismatch stops
silent key substitution attacks — the relay can't swap in attacker keys
without the operator noticing.

State machine
-------------

Statuses live on :class:`~pinky_federation.state.PeerPinRecord`::

    (none)
      │   observe(new keys)
      ▼
    first_seen  ── observe(same fp) ──────────────▶ pinned
        │                                            │
        │   observe(different fp)                    │ observe(different fp)
        ▼                                            ▼
    changed  ◀─ observe(yet another fp, pending updated) ─┐
      │                                                    │
      ├── accept_rotation() ───────▶ pinned ──── verify() ──▶ verified
      │                                   │
      └── reject_rotation() ───▶ rejected ┘
           │
           └── accept_rotation(new_keys) ──▶ pinned   (operator overrides earlier no)

Guarantees
----------

- **Never silently accept a new fingerprint for an existing peer.** A diff
  always produces a ``changed`` status and requires an explicit operator
  decision.
- **The old pin is preserved.** When we detect a mismatch, the new proposal
  is captured in the ``pending_*`` slot, not written over the trusted slot.
  The operator can compare old and new side-by-side.
- **Rejected peers stay rejected.** A peer in the ``rejected`` state always
  returns :class:`TrustDecision.REJECTED` regardless of what keys they
  present, until the operator explicitly accepts a rotation.
- **Idempotent.** Calling :meth:`TrustPolicy.observe` repeatedly with the
  same trusted keys is safe — it just bumps ``last_seen``.

This module does no network I/O and no logging of secret material.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pinky_federation.fingerprint import fingerprint as compute_fingerprint
from pinky_federation.keys import EncryptionPublicKey, SigningPublicKey
from pinky_federation.state import (
    PEER_PIN_CHANGED,
    PEER_PIN_FIRST_SEEN,
    PEER_PIN_PINNED,
    PEER_PIN_REJECTED,
    PEER_PIN_ROTATED,
    PEER_PIN_VERIFIED,
    FederationStateStore,
    PeerPinRecord,
    _now,
)

# Statuses that mean "primary slot keys are currently trusted for encrypting".
# ``rotated`` is a legacy P-02 alias kept here for backward-compat — anything
# written in that state is treated as ``pinned``.
_TRUSTED_STATUSES = frozenset(
    {PEER_PIN_PINNED, PEER_PIN_VERIFIED, PEER_PIN_ROTATED}
)


class TrustDecision(str, Enum):
    """Outcome of a trust lookup.

    String-valued so it serialises cleanly for UI / log lines.
    """

    FIRST_SEEN = "first_seen"
    """Brand-new peer. The keys were just auto-pinned via TOFU. Callers may
    proceed to encrypt, but should surface this as a soft warning in the UI
    ("new contact — verify fingerprint out-of-band")."""

    TRUSTED = "trusted"
    """Keys match the existing pin. Safe to encrypt."""

    MISMATCH = "mismatch"
    """The presented fingerprint does NOT match the pinned fingerprint. Do
    NOT encrypt to these keys. The operator must accept or reject. The new
    proposal has been captured in the peer's ``pending_*`` slot."""

    REJECTED = "rejected"
    """Peer is in the rejected state — a prior rotation was denied. Refuse
    to encrypt until the operator explicitly accepts."""


class TofuError(Exception):
    """Base class for TOFU policy errors."""


class NoPendingRotationError(TofuError):
    """Raised when :meth:`TrustPolicy.accept_rotation` is called with no
    pending proposal and no explicit new keys provided."""


class UnknownPeerError(TofuError):
    """Raised when operating on a peer that has never been observed."""


@dataclass(frozen=True)
class TrustResult:
    """Result of :meth:`TrustPolicy.observe`.

    ``decision`` is the headline outcome; ``record`` is the updated
    :class:`PeerPinRecord` after the observation.
    """

    decision: TrustDecision
    record: PeerPinRecord

    @property
    def trusted(self) -> bool:
        """True iff the caller can safely encrypt to the primary slot keys.

        ``FIRST_SEEN`` is considered trusted-but-unverified — we auto-pin
        on TOFU so first messages work. Callers that want a stricter
        posture (e.g. require operator verification before sending) should
        gate on ``record.status == PEER_PIN_VERIFIED`` instead.
        """
        return self.decision in (TrustDecision.FIRST_SEEN, TrustDecision.TRUSTED)


class TrustPolicy:
    """Pinning + rotation policy on top of :class:`FederationStateStore`.

    A thin stateless wrapper around the state store that enforces the TOFU
    state machine. Construct once per store and share freely — it holds no
    caches of its own.
    """

    def __init__(self, store: FederationStateStore) -> None:
        self._store = store

    # -- public API --------------------------------------------------------

    def observe(
        self,
        address: str,
        sig_pk: SigningPublicKey,
        enc_pk: EncryptionPublicKey,
    ) -> TrustResult:
        """Observe a peer's presented keys and update the pin accordingly.

        This is the hot path for every outbound send and inbound verify.

        Semantics:

        - No existing pin → auto-pin as ``first_seen``, return FIRST_SEEN.
        - Existing pin, same fingerprint, status in trusted set → bump
          ``last_seen``, return TRUSTED.
        - Existing pin, same fingerprint, status=first_seen → promote to
          ``pinned`` (second successful sighting confirms the pin), return
          TRUSTED.
        - Existing pin, different fingerprint → capture proposal in
          ``pending_*`` slot, set status=changed, return MISMATCH.
        - Existing pin, status=rejected → always return REJECTED regardless
          of keys (do NOT update last_seen; a rejected peer contacting us
          with new keys is suspicious, we don't want the mere lookup to
          update their record).
        - Existing pin, status=changed, mismatch against pending → update
          pending slot to the latest proposal, remain in changed, return
          MISMATCH. (Either the peer is rotating again, or an attacker is
          trying keys; either way, the operator still needs to decide.)
        - Existing pin, status=changed, match against *primary* (original
          pinned) fingerprint → the proposal was a blip / replay; return to
          pinned, clear pending, return TRUSTED.
        """
        sig_bytes = sig_pk.to_bytes()
        enc_bytes = enc_pk.to_bytes()
        fp = compute_fingerprint(address, sig_pk, enc_pk)
        now = _now()

        existing = self._store.get_peer_pin(address)

        if existing is None:
            rec = PeerPinRecord(
                peer_address=address,
                sig_pk=sig_bytes,
                enc_pk=enc_bytes,
                fingerprint=fp,
                status=PEER_PIN_FIRST_SEEN,
                first_seen=now,
                last_seen=now,
            )
            rec = self._store.upsert_peer_pin(rec)
            return TrustResult(TrustDecision.FIRST_SEEN, rec)

        if existing.status == PEER_PIN_REJECTED:
            # Do not update last_seen or capture the keys — a rejected peer
            # stays rejected and we don't want the rejection record mutated
            # by further probing.
            return TrustResult(TrustDecision.REJECTED, existing)

        if fp == existing.fingerprint:
            # Match against the trusted slot.
            existing.last_seen = now
            if existing.status == PEER_PIN_FIRST_SEEN:
                existing.status = PEER_PIN_PINNED
            elif existing.status == PEER_PIN_CHANGED:
                # Proposal was a blip — the real peer is back on their
                # original keys. Clear the pending slot and return to
                # pinned.
                existing.status = PEER_PIN_PINNED
                existing.pending_sig_pk = b""
                existing.pending_enc_pk = b""
                existing.pending_fingerprint = b""
                existing.pending_first_seen = 0.0
            # else: already pinned/verified/rotated — just a heartbeat.
            rec = self._store.upsert_peer_pin(existing)
            return TrustResult(TrustDecision.TRUSTED, rec)

        # Fingerprint mismatch — capture in pending slot, mark changed.
        existing.pending_sig_pk = sig_bytes
        existing.pending_enc_pk = enc_bytes
        existing.pending_fingerprint = fp
        if not existing.pending_first_seen or existing.status != PEER_PIN_CHANGED:
            existing.pending_first_seen = now
        existing.status = PEER_PIN_CHANGED
        existing.last_seen = now
        rec = self._store.upsert_peer_pin(existing)
        return TrustResult(TrustDecision.MISMATCH, rec)

    def accept_rotation(
        self,
        address: str,
        sig_pk: Optional[SigningPublicKey] = None,
        enc_pk: Optional[EncryptionPublicKey] = None,
    ) -> PeerPinRecord:
        """Operator explicitly accepts a key rotation for *address*.

        Two forms:

        - ``accept_rotation(address)`` — promote the pending slot (captured
          by the last MISMATCH observation) to the primary slot. Requires
          a pending proposal.
        - ``accept_rotation(address, sig_pk, enc_pk)`` — operator provides
          the new keys directly (e.g. out-of-band verification). Overrides
          whatever was in the pending slot.

        The peer's status moves to ``pinned`` (not ``verified`` — use
        :meth:`verify` for that) and ``first_seen`` is reset to "now" since
        this is effectively a new trust anchor.
        """
        existing = self._store.get_peer_pin(address)
        if existing is None:
            raise UnknownPeerError(f"no pin for peer: {address!r}")

        now = _now()

        if sig_pk is not None and enc_pk is not None:
            new_sig = sig_pk.to_bytes()
            new_enc = enc_pk.to_bytes()
            new_fp = compute_fingerprint(address, sig_pk, enc_pk)
        elif sig_pk is None and enc_pk is None:
            if not existing.pending_fingerprint:
                raise NoPendingRotationError(
                    f"no pending rotation for peer: {address!r}"
                )
            new_sig = existing.pending_sig_pk
            new_enc = existing.pending_enc_pk
            new_fp = existing.pending_fingerprint
        else:
            raise ValueError("provide both sig_pk and enc_pk, or neither")

        existing.sig_pk = new_sig
        existing.enc_pk = new_enc
        existing.fingerprint = new_fp
        existing.status = PEER_PIN_PINNED
        # The rotation is effectively a new trust anchor — reset first_seen.
        existing.first_seen = now
        existing.last_seen = now
        existing.pending_sig_pk = b""
        existing.pending_enc_pk = b""
        existing.pending_fingerprint = b""
        existing.pending_first_seen = 0.0
        # Clear any prior verification — the operator must re-verify.
        existing.verified_at = 0.0
        return self._store.upsert_peer_pin(existing)

    def reject_rotation(self, address: str) -> PeerPinRecord:
        """Operator rejects the pending rotation for *address*.

        Clears the pending slot and sets status to ``rejected``. The
        primary slot is left intact but is no longer considered trusted
        (rejected peers return REJECTED from :meth:`observe`).
        """
        existing = self._store.get_peer_pin(address)
        if existing is None:
            raise UnknownPeerError(f"no pin for peer: {address!r}")
        existing.pending_sig_pk = b""
        existing.pending_enc_pk = b""
        existing.pending_fingerprint = b""
        existing.pending_first_seen = 0.0
        existing.status = PEER_PIN_REJECTED
        existing.last_seen = _now()
        return self._store.upsert_peer_pin(existing)

    def verify(self, address: str) -> PeerPinRecord:
        """Mark the currently-pinned keys for *address* as operator-verified.

        This represents an out-of-band confirmation (fingerprint read aloud,
        QR code scanned, etc.) and is a stronger trust signal than TOFU.
        Only valid for peers currently in ``first_seen`` or ``pinned`` state;
        ``changed`` / ``rejected`` peers must be resolved via
        :meth:`accept_rotation` or :meth:`reject_rotation` first.
        """
        existing = self._store.get_peer_pin(address)
        if existing is None:
            raise UnknownPeerError(f"no pin for peer: {address!r}")
        if existing.status not in (PEER_PIN_FIRST_SEEN, PEER_PIN_PINNED, PEER_PIN_ROTATED):
            raise TofuError(
                f"cannot verify peer in status {existing.status!r}; "
                "resolve pending rotation first"
            )
        existing.status = PEER_PIN_VERIFIED
        existing.verified_at = _now()
        existing.last_seen = existing.verified_at
        return self._store.upsert_peer_pin(existing)

    def get(self, address: str) -> Optional[PeerPinRecord]:
        """Return the current pin record for *address*, or None."""
        return self._store.get_peer_pin(address)

    def list_changed(self) -> list[PeerPinRecord]:
        """Return all peers in the ``changed`` state awaiting operator action.

        UI surfaces (pending-rotations panel) use this to drive an inbox.
        """
        return self._store.list_peer_pins(status=PEER_PIN_CHANGED)


__all__ = [
    "TrustPolicy",
    "TrustDecision",
    "TrustResult",
    "TofuError",
    "NoPendingRotationError",
    "UnknownPeerError",
]
