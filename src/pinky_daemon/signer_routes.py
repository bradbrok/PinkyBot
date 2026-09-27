"""Signer Routes — daemon-internal FastAPI router for the service-auth signer.

Exposes ``POST /internal/signer/sign``: the gate streaming sessions (and
other in-process callers) go through to mint RFC 9421 signatures on
outbound HTTP requests, using the daemon-local key material managed by
:class:`pinky_identity.DaemonSigner` (PR-4b-2).

Authorization model:

- Every request MUST carry ``Authorization: Bearer <token>``.
- The bearer token is minted by the daemon at session-start via
  :meth:`pinky_identity.BearerTokenStore.mint` (PR-4b-3) and bound to
  the streaming session and the identity tuple
  ``(fleet, agent_name, purpose)``. The session cannot ask the signer
  to sign as a different agent.
- The token validates, expires, and can be revoked by session, by
  token id, or by identity tuple. All three failure modes surface as
  HTTP 401.

Why a separate module:

- ``api.py`` is large; the signer surface is tight (one endpoint plus
  models) and isolating it keeps the unit tests fast and obvious.
- Same pattern as ``voice_routes.py`` / ``migration/routes.py``: a
  ``router`` plus ``set_dependencies()`` for the daemon to wire in
  singletons. The router has zero coupling to FastAPI app state —
  daemon wiring (api.py mount + DaemonSigner construction) is a
  separate PR (PR-4b-5).

Wire format:

Request body (JSON):

.. code-block:: json

    {
        "method": "POST",
        "url": "https://api.example.com/v1/things",
        "headers": {"Content-Type": "application/json", "Accept": "*/*"},
        "body_b64": "<base64-of-bytes-or-empty-string>",
        "covered_components": ["@method", "@target-uri", "@authority",
                               "content-digest"],
        "expires_in_seconds": null,
        "nonce": null,
        "require_content_digest_if_body": true,
        "tag": "pinky-service-auth-v1",
        "label": "pinky"
    }

Response body (JSON):

.. code-block:: json

    {
        "kid": "abc123def456...",
        "headers": {
            "Signature": "pinky=:<sig>:",
            "Signature-Input": "pinky=(\"@method\" \"@target-uri\" ...);created=...;keyid=\"...\";...",
            "Content-Digest": "sha-256=:...:"
        }
    }

Only the headers the signer actually attached are returned —
``Content-Digest`` is present only when the signer attached it (i.e.
``content-digest`` was covered and the body was non-empty and the
caller didn't pre-set the header).

Bodies are never returned. The caller still owns the body; this
endpoint signs and hands the auth headers back.
"""

from __future__ import annotations

import base64
import binascii
import sys
from datetime import timedelta
from typing import Any

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from pinky_identity import (
    DEFAULT_COVERED_COMPONENTS,
    DEFAULT_LABEL,
    DEFAULT_TAG,
    BearerTokenClaims,
    BearerTokenError,
    BearerTokenExpiredError,
    BearerTokenNotFoundError,
    BearerTokenRevokedError,
    BearerTokenStore,
    BodyNotBoundError,
    DaemonSigner,
    KeyNotActiveError,
    KeyNotInStoreError,
    KeyNotRegisteredError,
    KidFingerprintMismatchError,
    SignerError,
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Module-level shared state (populated by set_dependencies) ────────────────

_bearer_store: BearerTokenStore | None = None
_signer: DaemonSigner | None = None


def set_dependencies(
    *,
    bearer_store: BearerTokenStore,
    signer: DaemonSigner,
) -> None:
    """Wire the BearerTokenStore + DaemonSigner singletons.

    Called once at daemon startup, after both components have been
    constructed. The router exposes module-level state for simplicity;
    the daemon never swaps these out at runtime, and a fresh
    DaemonSigner can be installed by restarting the daemon — same
    contract as ``voice_routes``.
    """
    if not isinstance(bearer_store, BearerTokenStore):
        raise TypeError("bearer_store must be a BearerTokenStore")
    if not isinstance(signer, DaemonSigner):
        raise TypeError("signer must be a DaemonSigner")
    global _bearer_store, _signer
    _bearer_store = bearer_store
    _signer = signer


# ── Pydantic models ──────────────────────────────────────────────────────────


class SignRequestPayload(BaseModel):
    """Inbound payload for ``POST /internal/signer/sign``.

    Models the subset of an outbound HTTP request the signer needs to
    compute an RFC 9421 signature. The caller (a streaming session,
    typically) builds the request elsewhere — this endpoint signs and
    returns the auth headers.
    """

    method: str = Field(
        ...,
        min_length=1,
        max_length=16,
        description="HTTP method, uppercased canonical form (GET/POST/PUT/...).",
    )
    url: str = Field(
        ...,
        min_length=1,
        max_length=8192,
        description=(
            "Full request URL including scheme + authority. Signed as "
            "``@target-uri`` and parsed for ``@authority``."
        ),
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Outbound headers the caller intends to send. Headers the "
            "signer chooses to add (Signature, Signature-Input, and "
            "Content-Digest when applicable) are returned separately in "
            "the response; the caller merges."
        ),
    )
    body_b64: str = Field(
        default="",
        description=(
            "Base64-encoded body bytes. Empty string means no body. "
            "Bodies travel base64 because JSON can't represent "
            "arbitrary bytes natively."
        ),
    )
    covered_components: list[str] = Field(
        default_factory=lambda: list(DEFAULT_COVERED_COMPONENTS),
        description=(
            "RFC 9421 covered component IDs. Order is significant for "
            "the signature base; keep callers consistent. Defaults to "
            "``DEFAULT_COVERED_COMPONENTS`` from pinky_identity."
        ),
    )
    expires_in_seconds: float | None = Field(
        default=None,
        gt=0,
        le=86400,
        description=(
            "Optional ``expires`` parameter in seconds; encoded as "
            "``created + expires_in``. Bound to 24h max to keep "
            "outbound signatures from outliving sessions."
        ),
    )
    nonce: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Optional explicit nonce. Defaults to a random 16-byte hex "
            "value in the signer."
        ),
    )
    require_content_digest_if_body: bool = Field(
        default=True,
        description=(
            "Mirrors the DaemonSigner knob: when True (default), "
            "a non-empty body without 'content-digest' in covered "
            "components is rejected at sign time."
        ),
    )
    tag: str = Field(
        default=DEFAULT_TAG,
        min_length=1,
        max_length=128,
        description="RFC 9421 ``tag`` parameter. Pinned by verifiers.",
    )
    label: str = Field(
        default=DEFAULT_LABEL,
        min_length=1,
        max_length=64,
        description=(
            "Signature label written into the ``Signature`` header. "
            "Single-label only; multi-sig is not supported here."
        ),
    )


class SignResponse(BaseModel):
    """Outbound payload from ``POST /internal/signer/sign``.

    Carries the kid that was stamped into the signature plus the auth
    headers the caller should merge into the outbound request. Body
    bytes are never echoed back — the caller owns them.
    """

    kid: str = Field(
        ...,
        description=(
            "The kid stamped into the signature. Useful for log "
            "correlation; the receiver also reads this off "
            "``Signature-Input`` for key lookup."
        ),
    )
    headers: dict[str, str] = Field(
        ...,
        description=(
            "Headers the signer attached: ``Signature``, "
            "``Signature-Input``, and ``Content-Digest`` when "
            "applicable. Merge into the outbound request before "
            "sending."
        ),
    )


# ── Bearer auth dependency ──────────────────────────────────────────────────


def _require_bearer(authorization: str | None) -> BearerTokenClaims:
    """Extract and validate the Authorization: Bearer header.

    Maps every :class:`BearerTokenError` subclass to HTTP 401 with a
    short reason. The reason does not distinguish "unknown" from
    "revoked" from "expired" beyond a single word — the client doesn't
    benefit from finer granularity and we don't want to leak which
    rows exist.

    Raised as :class:`HTTPException` so FastAPI surfaces them with the
    right status + JSON shape.
    """
    if _bearer_store is None:
        # 503 not 500: the dependency wiring hasn't completed yet (or
        # the daemon was started without identity enabled). Either
        # way, the caller can retry.
        raise HTTPException(
            status_code=503,
            detail="signer service not ready: bearer_store not configured",
        )
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="Authorization scheme must be 'Bearer'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = _bearer_store.validate(token)
    except BearerTokenExpiredError:
        raise HTTPException(
            status_code=401,
            detail="bearer token expired",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    except BearerTokenRevokedError:
        raise HTTPException(
            status_code=401,
            detail="bearer token revoked",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    except BearerTokenNotFoundError:
        raise HTTPException(
            status_code=401,
            detail="bearer token invalid",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    except BearerTokenError as e:
        # Defensive: any new BearerTokenError subclass not handled above
        # should still 401, not 500. The umbrella catch keeps the
        # endpoint from surfacing internal class names.
        _log(f"signer: unexpected bearer error: {e!r}")  # pragma: no cover
        raise HTTPException(  # pragma: no cover
            status_code=401,
            detail="bearer token invalid",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    return claims


# ── Router ──────────────────────────────────────────────────────────────────


router = APIRouter(prefix="/internal/signer", tags=["signer"])


def _decode_body(body_b64: str) -> bytes:
    """Decode the base64 body field. Empty string → empty bytes.

    Strict base64 (not base64url) for the wire — internal API,
    callers control both ends. Invalid base64 surfaces as HTTP 400.
    """
    if not body_b64:
        return b""
    try:
        return base64.b64decode(body_b64, validate=True)
    except (ValueError, binascii.Error) as e:
        raise HTTPException(
            status_code=400, detail=f"body_b64 is not valid base64: {e}"
        ) from None


def _build_prepared(payload: SignRequestPayload, body: bytes) -> Any:
    """Build the request object the signer accepts.

    :class:`pinky_identity.DaemonSigner.sign_request_by_kid` calls into
    :func:`pinky_identity.sign_request`, which forwards to
    ``http_message_signatures`` — that library accepts any object with
    ``method``, ``url``, ``headers`` (mutable dict), and ``body``
    attributes. The simplest fit is ``requests.Request().prepare()``.
    """
    req = requests.Request(
        method=payload.method.upper(),
        url=payload.url,
        data=body if body else None,
        headers=dict(payload.headers),
    ).prepare()
    # PreparedRequest can normalize Content-Length etc.; preserve the
    # caller's headers verbatim by reapplying after prepare(). This
    # keeps signed component values aligned with what the caller will
    # actually send.
    for k, v in payload.headers.items():
        req.headers[k] = v
    return req


@router.post("/sign", response_model=SignResponse)
def sign(
    payload: SignRequestPayload,
    authorization: str | None = Header(default=None),
) -> SignResponse:
    """Sign an outbound HTTP request on behalf of a bearer-authenticated session.

    Flow:

    1. Validate ``Authorization: Bearer …`` → :class:`BearerTokenClaims`.
       Failures → 401 with ``WWW-Authenticate``.
    2. Reject the request if the signer wiring isn't ready → 503.
    3. Build a :class:`requests.PreparedRequest` from the payload.
    4. Resolve the active kid for the bearer's identity tuple via
       :meth:`DaemonSigner.resolve_active_kid` and sign with
       ``sign_request_by_kid``.
    5. Map :class:`SignerError` failure modes to HTTP statuses:

       - :class:`KeyNotRegisteredError` → 404 ("agent has no key
         registered yet")
       - :class:`KeyNotActiveError` → 409 ("key is pending/retired/
         revoked"). The bearer outlived the key.
       - :class:`KeyNotInStoreError` → 500 (provisioning bug; the
         registry has it but the signer store doesn't)
       - :class:`KidFingerprintMismatchError` → 500 (corruption)
       - :class:`BodyNotBoundError` → 400 (caller foot-gun: body
         without content-digest in covered components)

    6. Return the kid + just the headers the signer attached. The
       caller merges into the outbound request before sending.
    """
    claims = _require_bearer(authorization)
    if _signer is None:
        raise HTTPException(
            status_code=503,
            detail="signer service not ready: DaemonSigner not configured",
        )

    body = _decode_body(payload.body_b64)
    req = _build_prepared(payload, body)

    expires_in = (
        timedelta(seconds=payload.expires_in_seconds)
        if payload.expires_in_seconds is not None
        else None
    )

    # Snapshot headers before signing so we can return only the delta
    # — the caller already has the headers they sent us, no point in
    # echoing them back.
    pre_sign_keys = set(req.headers.keys())

    try:
        kid = _signer.sign_request_for_agent(
            req,
            agent_name=claims.agent_name,
            fleet=claims.fleet,
            purpose=claims.purpose,
            covered_components=tuple(payload.covered_components),
            expires_in=expires_in,
            nonce=payload.nonce,
            tag=payload.tag,
            label=payload.label,
            require_content_digest_if_body=payload.require_content_digest_if_body,
        )
    except KeyNotRegisteredError as e:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no active key registered for "
                f"(fleet={claims.fleet!r}, agent_name={claims.agent_name!r}, "
                f"purpose={claims.purpose!r})"
            ),
        ) from e
    except KeyNotActiveError as e:
        raise HTTPException(
            status_code=409,
            detail=(
                f"key for {claims.agent_name!r} is not active "
                f"(status={e.status!r}); the bearer token outlived the key"
            ),
        ) from e
    except BodyNotBoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except KidFingerprintMismatchError as e:
        # Corruption: registry and signer-store disagree. Don't leak
        # the fingerprints; log them and surface a generic 500.
        _log(f"signer: kid fingerprint mismatch — {e}")
        raise HTTPException(
            status_code=500, detail="signer state inconsistent"
        ) from e
    except KeyNotInStoreError as e:
        # Provisioning bug: registry has it, store doesn't. Generic 500
        # — the operator looks at logs.
        _log(f"signer: key in registry but not in signer store — {e}")
        raise HTTPException(
            status_code=500, detail="signer state inconsistent"
        ) from e
    except SignerError as e:
        # Defensive: any unmodelled SignerError subclass still surfaces
        # as 500 rather than escaping as an unhandled exception.
        _log(f"signer: unexpected signer error: {e!r}")  # pragma: no cover
        raise HTTPException(  # pragma: no cover
            status_code=500, detail="signer error"
        ) from e

    new_headers = {
        name: value
        for name, value in req.headers.items()
        if name not in pre_sign_keys
        or name in ("Signature", "Signature-Input", "Content-Digest")
    }
    # Trim to just the signer-attached headers so callers don't have to
    # diff client-side. The pre_sign_keys snapshot above catches new
    # headers; the explicit name set above keeps Signature/-Input/Content-
    # Digest even on the off chance the caller pre-set one of them
    # (rare for Content-Digest, never for Signature).
    relevant = {"Signature", "Signature-Input", "Content-Digest"}
    new_headers = {k: v for k, v in new_headers.items() if k in relevant}

    return SignResponse(kid=kid, headers=new_headers)


__all__ = [
    "SignRequestPayload",
    "SignResponse",
    "router",
    "set_dependencies",
]
