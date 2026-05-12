"""Agent service-auth identity registry routes.

Admin routes are explicitly guarded by a dependency supplied from
``create_api``. The read-only verifier lookup and enrollment redemption paths
are intentionally narrow and do not grant admin power.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from pinky_identity import (
    DEFAULT_FLEET,
    DEFAULT_TOKEN_TTL_SECONDS,
    PURPOSE_SERVICE_AUTH,
    IdentityRegistry,
    IdentityRegistryError,
    public_key_from_bytes,
)

router = APIRouter(tags=["identity"])

_registry: IdentityRegistry | None = None
_require_admin: Callable[[Request], None] | None = None


def set_dependencies(
    *, registry: IdentityRegistry, require_admin: Callable[[Request], None]
) -> None:
    """Wire the identity registry and explicit admin guard."""
    global _registry, _require_admin
    _registry = registry
    _require_admin = require_admin


def _store() -> IdentityRegistry:
    if _registry is None:
        raise HTTPException(503, "identity registry is not configured")
    return _registry


def _admin(request: Request) -> None:
    if _require_admin is None:
        raise HTTPException(503, "identity admin guard is not configured")
    _require_admin(request)


def _b64url_decode(value: str) -> bytes:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("empty base64 value")
    padded = cleaned + "=" * (-len(cleaned) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _public_key_from_request(req: dict[str, Any]):
    raw = req.get("public_key_b64") or req.get("public_key")
    if raw:
        try:
            return public_key_from_bytes(_b64url_decode(str(raw)))
        except Exception as exc:
            raise HTTPException(400, f"invalid public_key_b64: {exc}") from exc
    jwk = req.get("public_jwk") or req.get("jwk")
    if isinstance(jwk, dict) and jwk.get("x"):
        if jwk.get("kty") not in (None, "OKP"):
            raise HTTPException(400, "public_jwk.kty must be OKP")
        if jwk.get("crv") not in (None, "Ed25519"):
            raise HTTPException(400, "public_jwk.crv must be Ed25519")
        try:
            return public_key_from_bytes(_b64url_decode(str(jwk["x"])))
        except Exception as exc:
            raise HTTPException(400, f"invalid public_jwk.x: {exc}") from exc
    raise HTTPException(400, "public_key_b64 or public_jwk.x is required")


def _actor(req: dict[str, Any], default: str = "admin") -> str:
    return (
        req.get("actor")
        or req.get("issued_by")
        or req.get("approved_by")
        or default
    ).strip()


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc))
    if isinstance(exc, IdentityRegistryError):
        return HTTPException(400, str(exc))
    return HTTPException(500, str(exc))


# -- Read-only verifier path --------------------------------------------------


@router.get("/identity/keys/{kid}")
async def get_identity_key(kid: str):
    """Read-only, kid-narrow public key lookup for service verifiers."""
    key = _store().get_key(kid)
    if key is None:
        raise HTTPException(404, f"identity key not found: {kid}")
    return key.to_dict()


# -- Enrollment redemption ----------------------------------------------------


@router.post("/identity/enroll/redeem")
async def redeem_enrollment_token(req: dict[str, Any]):
    """Redeem a one-time enrollment token and activate the submitted key."""
    pub = _public_key_from_request(req)
    try:
        key = _store().redeem_enrollment_token(
            token=str(req.get("token") or ""),
            public_key=pub,
            fleet=(req.get("fleet") or DEFAULT_FLEET),
            agent_name=str(req.get("agent_name") or ""),
            purpose=(req.get("purpose") or PURPOSE_SERVICE_AUTH),
            actor=str(req.get("actor") or req.get("agent_name") or ""),
            metadata=req.get("metadata") if isinstance(req.get("metadata"), dict) else None,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return key.to_dict()


# -- Explicitly guarded admin paths ------------------------------------------


@router.post("/identity/admin/enrollment-tokens")
async def issue_enrollment_token(req: dict[str, Any], request: Request):
    _admin(request)
    ttl = int(req.get("ttl_seconds") or DEFAULT_TOKEN_TTL_SECONDS)
    try:
        token = _store().issue_enrollment_token(
            fleet=(req.get("fleet") or DEFAULT_FLEET),
            agent_name=str(req.get("agent_name") or ""),
            purpose=(req.get("purpose") or PURPOSE_SERVICE_AUTH),
            public_key_fingerprint=str(req.get("public_key_fingerprint") or ""),
            issued_by=_actor(req),
            ttl_seconds=ttl,
            reason=str(req.get("reason") or ""),
            metadata=req.get("metadata") if isinstance(req.get("metadata"), dict) else None,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return token.to_dict(include_token=True)


@router.get("/identity/admin/enrollment-tokens")
async def list_enrollment_tokens(
    request: Request,
    fleet: str = "",
    agent_name: str = "",
    purpose: str = "",
    status: str = "",
    limit: int = 100,
):
    _admin(request)
    tokens = _store().list_enrollment_tokens(
        fleet=fleet,
        agent_name=agent_name,
        purpose=purpose,
        status=status,
        limit=limit,
    )
    return {"tokens": [t.to_dict() for t in tokens], "count": len(tokens)}


@router.post("/identity/admin/enrollment-tokens/{token_id}/revoke")
async def revoke_enrollment_token(token_id: str, req: dict[str, Any], request: Request):
    _admin(request)
    try:
        token = _store().revoke_enrollment_token(
            token_id,
            revoked_by=_actor(req),
            reason=str(req.get("reason") or ""),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return token.to_dict()


@router.post("/identity/admin/keys/pending")
async def submit_pending_key(req: dict[str, Any], request: Request):
    _admin(request)
    pub = _public_key_from_request(req)
    try:
        key = _store().submit_pending_key(
            public_key=pub,
            fleet=(req.get("fleet") or DEFAULT_FLEET),
            agent_name=str(req.get("agent_name") or ""),
            purpose=(req.get("purpose") or PURPOSE_SERVICE_AUTH),
            actor=_actor(req),
            metadata=req.get("metadata") if isinstance(req.get("metadata"), dict) else None,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return key.to_dict()


@router.get("/identity/admin/keys")
async def list_identity_keys(
    request: Request,
    fleet: str = "",
    agent_name: str = "",
    purpose: str = "",
    status: str = "",
    limit: int = 100,
):
    _admin(request)
    keys = _store().list_keys(
        fleet=fleet,
        agent_name=agent_name,
        purpose=purpose,
        status=status,
        limit=limit,
    )
    return {"keys": [k.to_dict() for k in keys], "count": len(keys)}


@router.post("/identity/admin/keys/{kid}/approve")
async def approve_identity_key(kid: str, req: dict[str, Any], request: Request):
    _admin(request)
    try:
        key = _store().approve_key(
            kid,
            approved_by=_actor(req),
            reason=str(req.get("reason") or ""),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return key.to_dict()


@router.post("/identity/admin/keys/{kid}/revoke")
async def revoke_identity_key(kid: str, req: dict[str, Any], request: Request):
    _admin(request)
    try:
        key = _store().revoke_key(
            kid,
            revoked_by=_actor(req),
            reason=str(req.get("reason") or ""),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return key.to_dict()


@router.post("/identity/admin/keys/{kid}/retire")
async def retire_identity_key(kid: str, req: dict[str, Any], request: Request):
    _admin(request)
    try:
        key = _store().retire_key(
            kid,
            retired_by=_actor(req),
            reason=str(req.get("reason") or ""),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return key.to_dict()


@router.get("/identity/admin/audit")
async def list_identity_audit(
    request: Request,
    fleet: str = "",
    agent_name: str = "",
    event_type: str = "",
    limit: int = 100,
):
    _admin(request)
    entries = _store().list_audit(
        fleet=fleet,
        agent_name=agent_name,
        event_type=event_type,
        limit=limit,
    )
    return {"entries": [e.to_dict() for e in entries], "count": len(entries)}
