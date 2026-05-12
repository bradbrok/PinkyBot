"""RFC 9421 HTTP Message Signature wrappers for pinky_identity.

A thin opinionated layer over :mod:`http_message_signatures` (the canonical
Python implementation of RFC 9421) that speaks in terms of pinky_identity's
:class:`SigningKeypair` / :class:`PublicKey` types instead of PEM blobs.

Design rules (see ``docs/design/agent-asymmetric-identity.md`` and issue #461):

- Ed25519 only. Other algorithms aren't on the roadmap.
- Every signature carries the tag ``pinky-service-auth-v1`` — verifiers MUST
  set ``expect_tag`` so a signature minted for one purpose can't be replayed
  against another (an attacker who steals a Zoho-bound signature shouldn't be
  able to present it to a federation peer).
- A minimum covered-component set is enforced at verify time. ``@method`` and
  ``@target-uri`` are non-negotiable; without them the signature doesn't bind
  the operation. The defaults also include ``@authority`` so a signature
  minted for ``api.zoho.com`` can't be replayed against ``api.evil.com``.
- ``max_age`` is enforced via the ``created`` parameter — the library checks
  this automatically. Default 5 minutes; tune at the call site.
- Replay protection (nonce uniqueness) is *not* enforced here. That belongs
  in the service-verifier layer (PR-5) which has the per-service nonce store.
  We do emit a nonce by default so the verifier has something to dedupe on.

The :class:`PinkyKeyResolver` translates kid lookups into in-memory
:mod:`cryptography` Ed25519 key objects. PyNaCl seeds are 32 raw bytes, which
is exactly what ``Ed25519PrivateKey.from_private_bytes`` expects, so the
conversion is zero-copy and PEM-free.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from http_message_signatures import (
    HTTPMessageSigner,
    HTTPMessageVerifier,
    HTTPSignatureKeyResolver,
    InvalidSignature,
    VerifyResult,
    algorithms,
)

from pinky_identity.keys import (
    PublicKey,
    SignatureError,
    SigningKeypair,
    kid_from_public_key,
)

# -- Constants ---------------------------------------------------------------

#: Service-auth tag stamped into every signature. Verifiers pin this to
#: prevent cross-purpose replay (RFC 9421 §2.3 covers the ``tag`` parameter).
DEFAULT_TAG = "pinky-service-auth-v1"

#: Default label written into ``Signature-Input``. RFC 9421 §4.1 allows any
#: token; ``pinky`` is short and recognizable.
DEFAULT_LABEL = "pinky"

#: Default covered components. ``@method`` + ``@target-uri`` bind the
#: operation; ``@authority`` pins the host so a signature can't be replayed
#: against a different origin.
DEFAULT_COVERED_COMPONENTS: tuple[str, ...] = (
    "@method",
    "@target-uri",
    "@authority",
)

#: Minimum set the verifier insists on. Anything not in this set is optional
#: from the verifier's perspective — but if the sender omits ``@target-uri``
#: the verifier rejects the signature even if the math checks out.
DEFAULT_REQUIRED_COMPONENTS: frozenset[str] = frozenset(
    {"@method", "@target-uri", "@authority"}
)

#: Default freshness window. The library checks ``created`` against
#: ``datetime.now() - max_age`` automatically.
DEFAULT_MAX_AGE = timedelta(minutes=5)

#: ``Content-Digest`` algorithm. RFC 9530 defines ``sha-256`` and ``sha-512``;
#: we standardize on sha-256 for size and ubiquity.
CONTENT_DIGEST_ALGORITHM = "sha-256"


# -- Errors ------------------------------------------------------------------


class HttpSignatureVerificationError(SignatureError):
    """Raised when an inbound signature fails verification.

    Subclass of :class:`pinky_identity.SignatureError` so callers can catch
    the general signature-failure case without importing this module.
    """


# -- Key resolver ------------------------------------------------------------


def _to_crypto_private(keypair: SigningKeypair) -> Ed25519PrivateKey:
    """Lift a pinky_identity SigningKeypair into a cryptography private key.

    PyNaCl stores the seed as 32 raw bytes; cryptography's
    ``Ed25519PrivateKey.from_private_bytes`` takes the same shape. No PEM
    round-trip needed.
    """
    return Ed25519PrivateKey.from_private_bytes(
        keypair.secret.secret_bytes_insecure()
    )


def _to_crypto_public(public_key: PublicKey) -> Ed25519PublicKey:
    """Lift a pinky_identity PublicKey into a cryptography public key."""
    return Ed25519PublicKey.from_public_bytes(public_key.raw)


@dataclass
class PinkyKeyResolver(HTTPSignatureKeyResolver):
    """In-memory key resolver backed by pinky_identity types.

    Two maps:

    - ``signing_keys``: kid → :class:`SigningKeypair`, queried by the signer.
    - ``public_keys``: kid → :class:`PublicKey`, queried by the verifier.

    Either map may be empty if the resolver is used for only one direction
    (a service that only verifies inbound signatures doesn't need any
    signing keys, and vice versa).

    The resolver is intentionally simple — the registry layer (PR-3) is
    where lifecycle, persistence, and approval state live. This is the
    in-process adapter that sits between the registry and the signing
    library.
    """

    signing_keys: dict[str, SigningKeypair]
    public_keys: dict[str, PublicKey]

    def __init__(
        self,
        *,
        signing_keys: dict[str, SigningKeypair] | None = None,
        public_keys: dict[str, PublicKey] | None = None,
    ) -> None:
        self.signing_keys = dict(signing_keys or {})
        self.public_keys = dict(public_keys or {})

    @classmethod
    def from_signing_keypairs(
        cls, keypairs: Iterable[SigningKeypair]
    ) -> "PinkyKeyResolver":
        """Build a resolver from keypairs, indexing both halves by kid."""
        signing: dict[str, SigningKeypair] = {}
        public: dict[str, PublicKey] = {}
        for kp in keypairs:
            k = kid_from_public_key(kp.public)
            signing[k] = kp
            public[k] = kp.public
        return cls(signing_keys=signing, public_keys=public)

    def register_keypair(self, keypair: SigningKeypair) -> str:
        """Add a keypair (both halves) to the resolver. Returns the kid."""
        k = kid_from_public_key(keypair.public)
        self.signing_keys[k] = keypair
        self.public_keys[k] = keypair.public
        return k

    def register_public_key(self, public_key: PublicKey) -> str:
        """Add a verify-only public key. Returns the kid."""
        k = kid_from_public_key(public_key)
        self.public_keys[k] = public_key
        return k

    # -- HTTPSignatureKeyResolver interface --

    def resolve_private_key(self, key_id: str) -> Ed25519PrivateKey:
        try:
            kp = self.signing_keys[key_id]
        except KeyError as e:
            raise HttpSignatureVerificationError(
                f"no signing key registered for kid {key_id!r}"
            ) from e
        return _to_crypto_private(kp)

    def resolve_public_key(self, key_id: str) -> Ed25519PublicKey:
        try:
            pub = self.public_keys[key_id]
        except KeyError as e:
            raise HttpSignatureVerificationError(
                f"no public key registered for kid {key_id!r}"
            ) from e
        return _to_crypto_public(pub)


# -- Content-digest helper ---------------------------------------------------


def compute_content_digest(body: bytes) -> str:
    """Return an RFC 9530 ``Content-Digest`` value for ``body``.

    Format: ``sha-256=:<base64-sha256>:`` per the structured-fields
    dictionary encoding. The library's signer will read this header
    when ``content-digest`` is in the covered components.
    """
    raw = hashlib.sha256(body).digest()
    return f"{CONTENT_DIGEST_ALGORITHM}=:{base64.b64encode(raw).decode('ascii')}:"


def attach_content_digest(request: Any) -> str:
    """Compute and set the ``Content-Digest`` header on a prepared request.

    Reads ``request.body`` (treating ``None`` as empty bytes), computes the
    sha-256 digest, sets ``request.headers["Content-Digest"]``, and returns
    the header value. Idempotent — calling twice with the same body
    produces the same header.
    """
    body = request.body
    if body is None:
        body_bytes = b""
    elif isinstance(body, str):
        body_bytes = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        body_bytes = bytes(body)
    else:  # pragma: no cover - defensive
        raise TypeError(
            f"unsupported request body type for content-digest: {type(body).__name__}"
        )
    digest = compute_content_digest(body_bytes)
    request.headers["Content-Digest"] = digest
    return digest


# -- Sign / verify -----------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def sign_request(
    request: Any,
    *,
    keypair: SigningKeypair,
    kid: str | None = None,
    created: datetime | None = None,
    expires_in: timedelta | None = None,
    nonce: str | None = None,
    covered_components: Sequence[str] = DEFAULT_COVERED_COMPONENTS,
    tag: str = DEFAULT_TAG,
    label: str = DEFAULT_LABEL,
) -> str:
    """Sign a prepared HTTP request in place.

    Adds ``Signature`` and ``Signature-Input`` headers per RFC 9421 §4.

    Parameters
    ----------
    request:
        A :class:`requests.PreparedRequest` (or any object with mutable
        ``headers`` dict, ``method``, ``url``, and ``body`` attributes
        that ``http_message_signatures`` accepts).
    keypair:
        The Ed25519 keypair to sign with. Its kid is computed via
        :func:`kid_from_public_key` unless ``kid`` is given explicitly.
    kid:
        Optional override for the kid stamped into ``Signature-Input``.
        Defaults to the canonical kid of ``keypair.public``.
    created:
        Timestamp written into the signature parameters. Defaults to
        ``datetime.now(UTC)``. Verifiers enforce ``max_age`` against this.
    expires_in:
        Optional ``expires`` parameter, encoded as ``created + expires_in``.
        Independent of ``max_age`` — this is an absolute deadline the signer
        commits to; ``max_age`` is the verifier's freshness window.
    nonce:
        Optional explicit nonce string. Defaults to a 16-byte random hex
        nonce. Replay protection (uniqueness) is the verifier's job.
    covered_components:
        Tuple of component IDs (RFC 9421 §2). Order is significant for the
        signature base — keep callers consistent.
    tag:
        ``tag`` parameter. Verifiers MUST pin this. Defaults to
        :data:`DEFAULT_TAG`.
    label:
        Signature label written into ``Signature: <label>=...``. Defaults
        to :data:`DEFAULT_LABEL`. Multiple labels are not supported by
        this wrapper.

    Returns
    -------
    str
        The kid that was stamped into the signature, for caller logging.
    """
    resolver = PinkyKeyResolver.from_signing_keypairs([keypair])
    effective_kid = kid if kid is not None else kid_from_public_key(keypair.public)
    if kid is not None and kid != kid_from_public_key(keypair.public):
        # The library will happily sign with whatever kid you give it; but
        # verifiers look up by kid and would fail. Resolve this by also
        # registering the kid override.
        resolver.signing_keys[effective_kid] = keypair
        resolver.public_keys[effective_kid] = keypair.public

    effective_created = created if created is not None else _utc_now()
    effective_expires = (
        effective_created + expires_in if expires_in is not None else None
    )
    effective_nonce = nonce if nonce is not None else secrets.token_hex(16)

    signer = HTTPMessageSigner(
        signature_algorithm=algorithms.ED25519,
        key_resolver=resolver,
    )
    signer.sign(
        request,
        key_id=effective_kid,
        created=effective_created,
        expires=effective_expires,
        nonce=effective_nonce,
        label=label,
        tag=tag,
        covered_component_ids=tuple(covered_components),
        include_alg=True,
    )
    return effective_kid


def verify_request(
    request: Any,
    *,
    key_resolver: PinkyKeyResolver,
    max_age: timedelta | None = DEFAULT_MAX_AGE,
    expect_tag: str = DEFAULT_TAG,
    required_components: Iterable[str] = DEFAULT_REQUIRED_COMPONENTS,
    expect_label: str | None = None,
    require_content_digest_if_body: bool = True,
) -> VerifyResult:
    """Verify a signed HTTP request.

    Wraps :class:`HTTPMessageVerifier` with the policy hooks the library
    doesn't enforce itself:

    - ``required_components`` — the library verifies whatever the sender
      claims; this wrapper additionally insists the sender claimed at
      least the listed components. Without this, a sender could omit
      ``@target-uri`` and the math would still check out for a different
      URL.
    - ``expect_tag`` — passed through to the library; verifiers MUST set
      this to prevent cross-purpose replay.
    - **Content-Digest body binding** (RFC 9421 §7.5.6, RFC 9530). The
      library authenticates the value of the ``Content-Digest`` *header*
      but does not check that the header value matches a hash of
      ``request.body``. If we trusted the library alone, an attacker
      could swap the body and keep the signed digest header unchanged —
      the math would still verify. So when ``content-digest`` is in the
      verified covered components, this wrapper recomputes
      :func:`compute_content_digest` over the actual body and rejects
      any mismatch. When ``require_content_digest_if_body=True`` (the
      default), a request with a non-empty body that did NOT include
      ``content-digest`` in covered components is also rejected — the
      sender is opting out of body integrity, which is virtually never
      what production callers want.

    Raises
    ------
    HttpSignatureVerificationError
        On any failure: missing/extra signature, expired, tag mismatch,
        unknown kid, missing required component, body/digest mismatch,
        body present without ``content-digest`` covered, bad math.
        Does not leak which failure occurred beyond the message string.

    Returns
    -------
    VerifyResult
        The library's :class:`VerifyResult` for the verified signature.
        Callers can inspect ``covered_components`` and ``parameters`` for
        downstream policy (e.g. nonce-store dedupe in PR-5).
    """
    verifier = HTTPMessageVerifier(
        signature_algorithm=algorithms.ED25519,
        key_resolver=key_resolver,
    )
    try:
        # Pass a very large max_age and enforce our own — the library's
        # InvalidSignature for staleness collapses into the generic
        # exception type, and we want a consistent error surface.
        results = verifier.verify(
            request,
            max_age=max_age if max_age is not None else timedelta(days=36500),
            expect_tag=expect_tag,
            expect_label=expect_label,
        )
    except InvalidSignature as e:
        raise HttpSignatureVerificationError(str(e)) from e
    except HttpSignatureVerificationError:
        raise
    except Exception as e:
        # The library may surface raw cryptography or http_sfv errors when
        # the signature is malformed. Normalize them.
        raise HttpSignatureVerificationError(
            f"signature verification failed: {e}"
        ) from e

    if not results:
        raise HttpSignatureVerificationError("no signature found on request")
    if len(results) > 1:
        # We don't support multi-sig; reject rather than silently picking one.
        raise HttpSignatureVerificationError(
            f"expected exactly one signature, got {len(results)}"
        )
    result = results[0]

    # Required-components check — the library doesn't enforce a minimum
    # set. Compare against the structured-field list parsed by http_sfv.
    required = frozenset(required_components)
    covered = frozenset(result.covered_components.keys())
    # ``covered_components`` keys come back with quoted-string syntax and
    # may include parameters (e.g. ``"@query-param";name="foo"``). We
    # compare on the raw component id portion before the first ``;``.
    covered_ids = frozenset(_strip_component_params(k) for k in covered)
    missing = required - covered_ids
    if missing:
        raise HttpSignatureVerificationError(
            f"signature missing required covered components: {sorted(missing)}"
        )

    # Body / content-digest binding. The signature authenticates the
    # value of the Content-Digest *header*, not the body. Without this
    # check, a body swap that leaves the header unchanged passes
    # verification.
    _enforce_content_digest_body_binding(
        request,
        covered_ids=covered_ids,
        require_if_body=require_content_digest_if_body,
    )

    return result


def content_digest_header_matches_body(header_value: str, body: bytes) -> bool:
    """Return True if ``header_value`` contains a sha-256 digest that
    matches ``body``.

    Public helper so both sign-side and verify-side agree on what counts
    as a body-bound ``Content-Digest`` header. RFC 9530 permits multiple
    algorithms in a comma-separated structured field (e.g.
    ``sha-256=:...:, sha-512=:...:``); we accept the header as long as
    at least one entry is sha-256 with a value that matches the body's
    digest. Other algorithms are tolerated but neither validated nor
    required — bumping :data:`CONTENT_DIGEST_ALGORITHM` later is a
    coordinated change across signer and verifier.
    """
    expected = compute_content_digest(body)
    candidate_entries = [seg.strip() for seg in header_value.split(",")]
    return any(
        _content_digest_segment_matches(seg, expected) for seg in candidate_entries
    )


def _enforce_content_digest_body_binding(
    request: Any,
    *,
    covered_ids: frozenset[str],
    require_if_body: bool,
) -> None:
    """Cross-check ``Content-Digest`` header against the actual body.

    Two cases:

    1. ``content-digest`` is in the verified covered components —
       recompute the digest from ``request.body`` and require
       byte-equality with the header value the signature authenticated.
       Tamper anywhere on that path (body or header swap) is caught.
    2. ``content-digest`` is NOT covered, but the request has a non-empty
       body. If ``require_if_body=True`` (default), refuse — the sender
       opted out of body integrity, which is virtually never what
       production callers want. To override (e.g. proxy that doesn't
       know the body), pass ``require_content_digest_if_body=False``
       through :func:`verify_request`.

    Multiple digest algorithms in a single ``Content-Digest`` header
    (e.g. ``sha-256=...,sha-512=...``) are permitted by RFC 9530; we
    accept the request as long as at least one comparable algorithm
    (sha-256 today) matches the body. Other algorithms are ignored —
    they were authenticated by the signature too, but the policy here is
    "at least one match against the body". Bumping
    :data:`CONTENT_DIGEST_ALGORITHM` later is a coordinated change.
    """
    body = getattr(request, "body", None)
    headers = getattr(request, "headers", {}) or {}

    if body is None or body == b"" or body == "":
        body_bytes: bytes = b""
    elif isinstance(body, str):
        body_bytes = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        body_bytes = bytes(body)
    else:
        body_bytes = bytes(body)

    has_body = len(body_bytes) > 0
    content_digest_covered = "content-digest" in covered_ids

    if not content_digest_covered:
        if has_body and require_if_body:
            raise HttpSignatureVerificationError(
                "request has a non-empty body but the signature does not cover "
                "content-digest; the body is not bound to the signature. "
                "Refusing for safety; set require_content_digest_if_body=False "
                "if you really want to verify body-free."
            )
        return

    header_value = headers.get("Content-Digest") or headers.get("content-digest")
    if header_value is None:
        raise HttpSignatureVerificationError(
            "signature covers content-digest but request has no "
            "Content-Digest header"
        )

    if not content_digest_header_matches_body(header_value, body_bytes):
        raise HttpSignatureVerificationError(
            "Content-Digest header does not match SHA-256 of request body; "
            "body has been tampered with after signing"
        )


def _content_digest_segment_matches(segment: str, expected_full: str) -> bool:
    """Return True if ``segment`` is an RFC 9530 ``sha-256=:base64:`` entry
    whose value matches the digest from :func:`compute_content_digest`.

    ``expected_full`` is the full ``sha-256=:base64:`` string from
    :func:`compute_content_digest`. We match algorithm-then-value so a
    mismatched algorithm doesn't accidentally pass when the bytes line
    up. Other algorithms in the header are ignored.
    """
    seg = segment.strip()
    if not seg.startswith(f"{CONTENT_DIGEST_ALGORITHM}="):
        return False
    return seg == expected_full


def _strip_component_params(component_id: str) -> str:
    """Return the component id with structured-field parameters stripped.

    RFC 9421 component IDs are serialized as structured-field strings,
    optionally with parameters (e.g. ``"@query-param";name="foo"``). For
    the required-set check we only care about the bare component id.
    """
    # The library hands us keys in the form '"@method"', '"content-type"',
    # or '"@query-param";name="foo"'. http_sfv knows how to parse but for
    # the simple prefix-strip we want here, splitting is enough.
    head, _, _ = component_id.partition(";")
    return head.strip().strip('"')


__all__ = [
    "CONTENT_DIGEST_ALGORITHM",
    "DEFAULT_COVERED_COMPONENTS",
    "DEFAULT_LABEL",
    "DEFAULT_MAX_AGE",
    "DEFAULT_REQUIRED_COMPONENTS",
    "DEFAULT_TAG",
    "HttpSignatureVerificationError",
    "PinkyKeyResolver",
    "VerifyResult",
    "attach_content_digest",
    "compute_content_digest",
    "content_digest_header_matches_body",
    "sign_request",
    "verify_request",
]
