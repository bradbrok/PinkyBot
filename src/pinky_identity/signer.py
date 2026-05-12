"""Daemon-local service-auth signer.

The :class:`DaemonSigner` composes the public-side registry
(:mod:`pinky_identity.registry`, PR-3) with the private-side signer store
(:mod:`pinky_identity.signer_store`, PR-4b-1) and the RFC 9421 wrappers
(:mod:`pinky_identity.http_signatures`, PR-2b) into the single in-process
entry point the daemon uses to sign outbound HTTP requests on behalf of
agents.

Design rules (see ``docs/design/agent-asymmetric-identity.md`` and #461):

- **Registry is the authority for signability.** A kid present in the
  signer store but marked ``pending``/``retired``/``revoked`` in the
  registry MUST NOT sign. The signer always checks the registry first.
- **The signer never returns secret material.** It signs requests in
  place and returns the same :class:`requests.PreparedRequest` (now with
  ``Signature`` and ``Signature-Input`` headers). Callers never see the
  decrypted seed.
- **No HTTP layer here.** PR-4b-2 is the in-process service. The
  ``/internal/signer/sign`` endpoint (PR-4b-4) and session-bound bearer
  tokens (PR-4b-3) compose with this — but live in separate modules so
  the DaemonSigner can be tested without standing up a FastAPI app.
- **Duck-typed registry.** This module does not import
  :class:`pinky_identity.registry.IdentityRegistry` at runtime — it
  consumes a :class:`RegistryLike` protocol. Lets the DaemonSigner stack
  cleanly on this branch before PR-3 lands and keeps the testing surface
  small.

Error model:

- :class:`SignerError` — umbrella, subclass of
  :class:`pinky_identity.SignatureError`. One ``except`` covers
  the whole signer.
- :class:`KeyNotRegisteredError` — no row in registry for kid /
  (fleet, agent, purpose).
- :class:`KeyNotInStoreError` — row in registry but no encrypted seed in
  signer store. Indicates a provisioning bug; never expected at steady
  state.
- :class:`KeyNotActiveError` — registry status is not ``active``. Carries
  the actual status string so callers can log "tried to sign with a
  revoked key" or "key still pending approval".
- :class:`KidFingerprintMismatchError` — the registry row and the signer
  store row exist for the same kid but disagree on fingerprint. Should
  be impossible if both layers are honest; treat as corruption.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pinky_identity.http_signatures import (
    DEFAULT_COVERED_COMPONENTS,
    DEFAULT_LABEL,
    DEFAULT_TAG,
    PinkyKeyResolver,
    sign_request,
)
from pinky_identity.keys import SignatureError, SigningKeypair
from pinky_identity.signer_store import EncryptedSignerStore, SignerStoreError

# -- Constants --------------------------------------------------------------

#: Status string that means the registry considers the key signable.
#: MUST track :data:`pinky_identity.registry.KEY_ACTIVE`. Kept local so
#: this module doesn't import the registry at runtime — but the value is
#: the same.
KEY_ACTIVE: str = "active"

#: Default purpose for the service-auth signer. Lines up with
#: :data:`pinky_identity.registry.PURPOSE_SERVICE_AUTH`.
PURPOSE_SERVICE_AUTH: str = "service-auth"

#: Default fleet identifier (matches Murzik's registry default after the
#: ``"pos"`` → ``"pinky"`` cleanup in PR-3 commit 4cd61df).
DEFAULT_FLEET: str = "pinky"


# -- Errors ------------------------------------------------------------------


class SignerError(SignatureError):
    """Umbrella for any failure inside :class:`DaemonSigner`.

    Subclass of :class:`pinky_identity.SignatureError` so a single
    ``except SignatureError`` covers signer, keystore, and HTTP-signature
    failures uniformly.
    """


class KeyNotRegisteredError(SignerError):
    """No row in the registry for the requested kid / identity tuple."""


class KeyNotInStoreError(SignerError):
    """Registry has the kid but the signer store doesn't have the seed.

    Should be unreachable at steady state — indicates a provisioning bug
    or out-of-band deletion. Treat as a hard error; never silently re-
    enroll.
    """


class KeyNotActiveError(SignerError):
    """The registry status for the kid is not ``active``.

    Carries the actual status so callers can distinguish "pending
    approval" from "revoked" in logs and metrics.
    """

    def __init__(self, kid: str, status: str) -> None:
        super().__init__(
            f"key {kid!r} is not active (status={status!r}); refusing to sign"
        )
        self.kid = kid
        self.status = status


class KidFingerprintMismatchError(SignerError):
    """Registry and signer store disagree on the fingerprint for a kid.

    Should be impossible if both layers are honest. Treat as corruption.
    """


# -- Registry protocol -------------------------------------------------------


@runtime_checkable
class IdentityKeyRecordLike(Protocol):
    """The subset of registry-row fields the signer reads.

    Mirrors :class:`pinky_identity.registry.IdentityKeyRecord` but is
    duck-typed so this module doesn't import the registry at runtime.
    """

    kid: str
    fingerprint: str
    status: str
    public_key: bytes


@runtime_checkable
class RegistryLike(Protocol):
    """The subset of registry methods the signer calls.

    Mirrors :class:`pinky_identity.registry.IdentityRegistry`. Tests pass
    a stub; the real registry is used in production.
    """

    def get_key(self, kid: str) -> IdentityKeyRecordLike | None: ...

    def get_active_key(
        self, fleet: str, agent_name: str, purpose: str
    ) -> IdentityKeyRecordLike | None: ...


# -- DaemonSigner -----------------------------------------------------------


@dataclass
class _ResolvedIdentity:
    """Internal: a registry+store pair for a single kid."""

    kid: str
    fingerprint: str
    status: str
    public_key_raw: bytes
    keypair: SigningKeypair


class DaemonSigner:
    """In-process signer that holds the device key and signs on demand.

    The signer is constructed once per daemon process. It holds:

    - A :class:`RegistryLike` for lifecycle/status lookups (public half).
    - An :class:`EncryptedSignerStore` for the encrypted seeds (private
      half). The store holds the :class:`DeviceKey` internally.
    - Sensible defaults: ``fleet=DEFAULT_FLEET``,
      ``purpose=PURPOSE_SERVICE_AUTH``.

    All public methods either succeed (with a signed
    :class:`requests.PreparedRequest`) or raise a subclass of
    :class:`SignerError`. No partial-success returns; no surfacing of the
    raw seed.
    """

    __slots__ = ("_registry", "_store", "_fleet", "_purpose")

    def __init__(
        self,
        *,
        registry: RegistryLike,
        signer_store: EncryptedSignerStore,
        fleet: str = DEFAULT_FLEET,
        purpose: str = PURPOSE_SERVICE_AUTH,
    ) -> None:
        if not isinstance(signer_store, EncryptedSignerStore):
            raise TypeError("signer_store must be an EncryptedSignerStore")
        # The registry is duck-typed — accept anything with the right
        # methods. The Protocol check at type-check time is enough; we
        # don't enforce isinstance at runtime so stubs are easy.
        self._registry = registry
        self._store = signer_store
        self._fleet = fleet
        self._purpose = purpose

    # -- public API --

    def sign_request_by_kid(
        self,
        request: Any,
        *,
        kid: str,
        created: datetime | None = None,
        expires_in: timedelta | None = None,
        nonce: str | None = None,
        covered_components: Sequence[str] = DEFAULT_COVERED_COMPONENTS,
        tag: str = DEFAULT_TAG,
        label: str = DEFAULT_LABEL,
    ) -> str:
        """Sign *request* using the keypair for *kid*.

        Returns the kid that was stamped into the signature (the caller
        usually already has it; returning makes logging consistent with
        :func:`pinky_identity.sign_request`).

        Raises a :class:`SignerError` subclass on any failure.
        """
        ident = self._resolve(kid=kid)
        return sign_request(
            request,
            keypair=ident.keypair,
            kid=ident.kid,
            created=created,
            expires_in=expires_in,
            nonce=nonce,
            covered_components=covered_components,
            tag=tag,
            label=label,
        )

    def sign_request_for_agent(
        self,
        request: Any,
        *,
        agent_name: str,
        fleet: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Sign *request* using the active key for an agent identity tuple.

        Convenience around :meth:`sign_request_by_kid` that first looks up
        the active kid via the registry. ``fleet`` and ``purpose`` default
        to the signer's configured values.
        """
        kid = self.resolve_active_kid(
            agent_name=agent_name, fleet=fleet, purpose=purpose
        )
        return self.sign_request_by_kid(request, kid=kid, **kwargs)

    def resolve_active_kid(
        self,
        *,
        agent_name: str,
        fleet: str | None = None,
        purpose: str | None = None,
    ) -> str:
        """Return the active kid for an identity tuple.

        Raises :class:`KeyNotRegisteredError` if no row exists. (The row
        may be present in non-active status; this method only returns
        actively signable kids — :meth:`RegistryLike.get_active_key` is
        the registry's filter.)
        """
        effective_fleet = fleet if fleet is not None else self._fleet
        effective_purpose = purpose if purpose is not None else self._purpose
        record = self._registry.get_active_key(
            effective_fleet, agent_name, effective_purpose
        )
        if record is None:
            raise KeyNotRegisteredError(
                f"no active key registered for "
                f"(fleet={effective_fleet!r}, agent_name={agent_name!r}, "
                f"purpose={effective_purpose!r})"
            )
        return record.kid

    def get_public_key_resolver(self) -> PinkyKeyResolver:
        """Return a :class:`PinkyKeyResolver` populated with every public
        key the signer can sign for (registry-active ⋂ store-present).

        Useful for callers that want to verify their own outbound
        signatures (e.g. integration tests, paranoid signers that want to
        double-check before sending).

        Excludes pending/retired/revoked keys — only active rows.
        """
        resolver = PinkyKeyResolver()
        for kid in self._store.list_kids():
            registry_row = self._registry.get_key(kid)
            if registry_row is None or registry_row.status != KEY_ACTIVE:
                continue
            try:
                row = self._store.get_row(kid)
            except SignerStoreError:
                continue
            # PinkyKeyResolver.register_public_key wants a PublicKey, not
            # raw bytes. Reuse the cleartext public from the store row.
            resolver.register_public_key(row.to_wrapped().public())
        return resolver

    # -- internals --

    def _resolve(self, *, kid: str) -> _ResolvedIdentity:
        """Compose registry + store into a :class:`_ResolvedIdentity`.

        Single point that enforces the signability invariants. All
        sign-paths funnel through here.
        """
        registry_row = self._registry.get_key(kid)
        if registry_row is None:
            raise KeyNotRegisteredError(
                f"no registry row for kid {kid!r}"
            )
        if registry_row.status != KEY_ACTIVE:
            raise KeyNotActiveError(kid=kid, status=registry_row.status)

        try:
            store_row = self._store.get_row(kid)
        except SignerStoreError as e:
            raise KeyNotInStoreError(
                f"registry has kid {kid!r} but signer store does not"
            ) from e

        if registry_row.fingerprint != store_row.fingerprint:
            raise KidFingerprintMismatchError(
                f"kid {kid!r}: registry fingerprint "
                f"{registry_row.fingerprint!r} != store fingerprint "
                f"{store_row.fingerprint!r}"
            )

        # Unwrap once. SignerStoreError / KeystoreError bubble up — the
        # caller catches via SignatureError if they want everything.
        keypair = self._store.get_signing_keypair(kid)
        return _ResolvedIdentity(
            kid=kid,
            fingerprint=store_row.fingerprint,
            status=registry_row.status,
            public_key_raw=store_row.public_key_raw,
            keypair=keypair,
        )


__all__ = [
    "DEFAULT_FLEET",
    "KEY_ACTIVE",
    "PURPOSE_SERVICE_AUTH",
    "DaemonSigner",
    "IdentityKeyRecordLike",
    "KeyNotActiveError",
    "KeyNotInStoreError",
    "KeyNotRegisteredError",
    "KidFingerprintMismatchError",
    "RegistryLike",
    "SignerError",
]
