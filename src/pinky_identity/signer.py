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
    attach_content_digest,
    content_digest_header_matches_body,
    sign_request,
)
from pinky_identity.keys import (
    SignatureError,
    SigningKeypair,
    public_key_from_bytes,
)
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
    """Registry and signer store disagree on the fingerprint (or public
    key bytes) for a kid.

    Should be impossible if both layers are honest. Treat as corruption.
    Raised whenever ``registry_row.fingerprint != store_row.fingerprint``
    or ``registry_row.public_key != store_row.public_key_raw`` — the
    signer treats either disagreement as the same corruption class.
    """


class BodyNotBoundError(SignerError):
    """Refusing to sign a request whose body is not bound to the signature.

    Raised by :meth:`DaemonSigner.sign_request_by_kid` when the caller
    supplied a request with a non-empty body but did not include
    ``content-digest`` in ``covered_components``. The default verifier
    (:func:`pinky_identity.verify_request`) rejects such signatures with
    ``require_content_digest_if_body=True`` — minting one would just
    produce an unverifiable request. Fail closed at sign time so the bug
    surfaces immediately and never on the wire.

    Override via ``require_content_digest_if_body=False`` on the signer
    call if you really intend to sign body-free (e.g. a proxy that
    doesn't have the body); matches the verify-side knob.
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
        require_content_digest_if_body: bool = True,
    ) -> str:
        """Sign *request* using the keypair for *kid*.

        Returns the kid that was stamped into the signature (the caller
        usually already has it; returning makes logging consistent with
        :func:`pinky_identity.sign_request`).

        Secure-by-default body binding (mirrors
        :func:`pinky_identity.verify_request`):

        - If ``request.body`` is non-empty and ``content-digest`` is
          **not** in ``covered_components``, refuse with
          :class:`BodyNotBoundError`. The default verifier rejects such
          signatures (PR-2b's body-binding fix); minting one would just
          be a foot-gun. To opt out (proxy-style signing without the
          body), pass ``require_content_digest_if_body=False``.
        - If ``content-digest`` is in ``covered_components`` and
          ``request.body`` is non-empty, the signer guarantees the
          header that gets signed is bound to the body:

          * No header set → auto-attach via
            :func:`pinky_identity.attach_content_digest`.
          * Header set and matches the body's SHA-256 → preserved
            unchanged (supports streaming callers that pre-computed,
            and RFC 9530 multi-algorithm headers like
            ``sha-256=:...:, sha-512=:...:``).
          * Header set but stale (doesn't match the body) →
            :class:`BodyNotBoundError`. Signing it anyway would just
            produce a request the default verifier rejects, which is
            indistinguishable on the wire from a body-swap attack —
            we'd rather catch it at the source.

        Raises a :class:`SignerError` subclass on any failure.
        """
        body_bytes = _extract_body_bytes(request)
        has_body = len(body_bytes) > 0
        content_digest_covered = any(
            _normalize_component_id(c) == "content-digest"
            for c in covered_components
        )

        if has_body and not content_digest_covered and require_content_digest_if_body:
            raise BodyNotBoundError(
                "refusing to sign: request has a non-empty body but "
                "'content-digest' is not in covered_components. The "
                "signature would not bind the body and the default "
                "verifier rejects this. Add 'content-digest' to "
                "covered_components, or pass "
                "require_content_digest_if_body=False to override."
            )

        if has_body and content_digest_covered:
            headers = getattr(request, "headers", None)
            existing = None
            if headers is not None:
                existing = headers.get("Content-Digest") or headers.get(
                    "content-digest"
                )
            if existing is None:
                attach_content_digest(request)
            elif not content_digest_header_matches_body(existing, body_bytes):
                # Pre-set but stale digest. Signing it would mint a
                # signature the default verifier rejects (looks like a
                # body-swap). Fail closed at the source.
                raise BodyNotBoundError(
                    "refusing to sign: pre-set 'Content-Digest' header "
                    "does not match SHA-256 of request body. The signer "
                    "would otherwise sign a stale digest; the default "
                    "verifier rejects this and it is indistinguishable "
                    "from a body-swap attack on the wire. Recompute the "
                    "header (or clear it and let the signer attach it) "
                    "before signing."
                )

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

        Filtering rules:

        - Non-active rows (``pending``/``retired``/``revoked``) are
          skipped silently — they aren't signable, so they don't belong
          in a "what can I verify against" view.
        - Orphan rows (store-only, no registry row) are skipped silently
          — same reason.
        - **Inconsistent active rows** (registry/store disagree on
          fingerprint or public-key bytes) are **not** silently included
          — they raise :class:`KidFingerprintMismatchError`. Quietly
          serving up a verifier that uses one half's public key while
          the other half thinks the kid points somewhere else would let
          self-verification mask corruption. Fail loud instead.

        The registered public key is the **registry**'s public key —
        the registry is the authority for kid → identity binding. The
        consistency gate above guarantees this matches the store's
        public key for any kid we include.
        """
        resolver = PinkyKeyResolver()
        for kid in self._store.list_kids():
            registry_row = self._registry.get_key(kid)
            if registry_row is None or registry_row.status != KEY_ACTIVE:
                continue
            try:
                store_row = self._store.get_row(kid)
            except SignerStoreError:
                continue
            # Apply the same consistency gate the sign path uses. For
            # active+stored rows, registry/store disagreement is
            # corruption — raise instead of silently including one half.
            _check_registry_store_consistency(
                kid=kid, registry_row=registry_row, store_row=store_row
            )
            # Register the registry's public key as the verifier
            # authority. (Consistency-gated above, so it equals the
            # store's public_key_raw.) Falls back to public_key_from_bytes
            # because the registry hands us raw bytes via the
            # duck-typed protocol.
            resolver.register_public_key(
                public_key_from_bytes(registry_row.public_key)
            )
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

        _check_registry_store_consistency(
            kid=kid, registry_row=registry_row, store_row=store_row
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


# -- helpers ----------------------------------------------------------------


def _check_registry_store_consistency(
    *,
    kid: str,
    registry_row: IdentityKeyRecordLike,
    store_row: Any,
) -> None:
    """Raise :class:`KidFingerprintMismatchError` if registry and store
    disagree on the identity bound to *kid*.

    Two checks; either failure is the same corruption class:

    1. Fingerprint equality (cheap, catches almost everything).
    2. Public-key bytes equality (defensive — guards against fingerprint
       collisions or a bug that updates one column and not the other).

    Both checks are necessary: comparing only the fingerprint trusts the
    hash; comparing only the bytes makes the error message less useful.
    """
    if registry_row.fingerprint != store_row.fingerprint:
        raise KidFingerprintMismatchError(
            f"kid {kid!r}: registry fingerprint "
            f"{registry_row.fingerprint!r} != store fingerprint "
            f"{store_row.fingerprint!r}"
        )
    if bytes(registry_row.public_key) != bytes(store_row.public_key_raw):
        raise KidFingerprintMismatchError(
            f"kid {kid!r}: registry public_key bytes disagree with "
            f"store public_key_raw despite matching fingerprint "
            f"{registry_row.fingerprint!r}; treating as corruption"
        )


def _extract_body_bytes(request: Any) -> bytes:
    """Return ``request.body`` normalized to bytes.

    Mirrors :func:`pinky_identity.http_signatures.attach_content_digest`
    and :func:`pinky_identity.http_signatures._enforce_content_digest_body_binding`
    so the sign-side body check and the verify-side body check agree on
    what counts as "non-empty body".
    """
    body = getattr(request, "body", None)
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    # Defensive: stream/iterator bodies aren't supported. Treat as
    # non-empty so the secure-default check refuses; the caller can
    # buffer the body before signing.
    return b"<unsupported-body-type>"


def _normalize_component_id(component: str) -> str:
    """Normalize a covered-component id for the content-digest check.

    Callers may pass either ``"content-digest"`` or ``'"content-digest"'``;
    structured-field parameters are also stripped (e.g. ``";name=foo"``).
    Matches the parsing the verifier uses on the inbound side.
    """
    head, _, _ = component.partition(";")
    return head.strip().strip('"').lower()


__all__ = [
    "DEFAULT_FLEET",
    "KEY_ACTIVE",
    "PURPOSE_SERVICE_AUTH",
    "BodyNotBoundError",
    "DaemonSigner",
    "IdentityKeyRecordLike",
    "KeyNotActiveError",
    "KeyNotInStoreError",
    "KeyNotRegisteredError",
    "KidFingerprintMismatchError",
    "RegistryLike",
    "SignerError",
]
