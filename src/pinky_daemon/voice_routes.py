"""Voice Routes — FastAPI router for Twilio voice calling.

Endpoints for outbound AI phone calls (propose → approve → dial → converse → report)
and inbound AI voicemail. Phase 1 implements propose_call; other endpoints are stubs.

Dependency injection: call set_dependencies(voice_store=..., agents=..., broker_send=...)
from api.py after stores are initialised.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Module-level shared state (populated by set_dependencies) ─────────────────

_voice_store: Any = None
_agents: Any = None
_broker_send: Callable | None = None  # async fn(agent_name, platform, chat_id, text)
_base_url: str = ""


def set_dependencies(
    *,
    voice_store: Any,
    agents: Any,
    broker_send: Callable | None = None,
    base_url: str = "",
) -> None:
    global _voice_store, _agents, _broker_send, _base_url
    _voice_store = voice_store
    _agents = agents
    _broker_send = broker_send
    _base_url = base_url


# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/voice", tags=["voice"])


# ── Pydantic models ──────────────────────────────────────────────────────────

class ProposeCallRequest(BaseModel):
    target_name: str
    target_phone: str
    goal: str
    context: dict = {}
    fallback_behavior: str = "notify_brad_if_no_answer"
    requested_by_agent: str = "barsik"


class CancelCallRequest(BaseModel):
    reason: str = ""


class ApproveVoiceRequest(BaseModel):
    request_id: str


class TerminateSessionRequest(BaseModel):
    reason: str


class TransferSessionRequest(BaseModel):
    reason: str


class DtmfRequest(BaseModel):
    digits: str


# ── Helper: notify owner of pending call request ─────────────────────────────

async def _notify_owner_call_request(req: Any) -> None:
    """Send TG notification to primary user about a pending voice call request."""
    if not _agents or not _broker_send:
        _log("voice: cannot notify owner — agents or broker_send not configured")
        return

    primary = _agents.get_primary_user()
    if not primary:
        _log("voice: cannot notify owner — no primary user configured")
        return

    chat_id = primary.get("chat_id") or primary.get("id", "")
    if not chat_id:
        _log("voice: cannot notify owner — primary user has no chat_id")
        return

    ctx = json.loads(req.context) if isinstance(req.context, str) else req.context
    ctx_lines = "\n".join(f"  {k}: {v}" for k, v in ctx.items()) if ctx else "  (none)"

    text = (
        f"📞 Voice call request from {req.requested_by_agent}\n\n"
        f"Target: {req.target_name}\n"
        f"Phone: {req.target_phone}\n"
        f"Goal: {req.goal}\n"
        f"Context:\n{ctx_lines}\n\n"
        f"/approve_voice_{req.id}\n"
        f"/deny_voice_{req.id}"
    )

    # Use primary user's default agent for delivery, not the requesting agent —
    # the requester may not have its own Telegram adapter configured.
    notify_agent = primary.get("default_agent") or "barsik"
    try:
        await _broker_send(notify_agent, "telegram", str(chat_id), text)
    except Exception as e:
        _log(f"voice: failed to notify owner — {e}")


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/request")
async def propose_call_endpoint(body: ProposeCallRequest) -> dict:
    """Create a call request and notify the owner for approval."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")

    try:
        req = _voice_store.create_call_request(
            requested_by_agent=body.requested_by_agent,
            target_name=body.target_name,
            target_phone=body.target_phone,
            goal=body.goal,
            context=body.context,
            fallback_behavior=body.fallback_behavior,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Notify owner for approval
    await _notify_owner_call_request(req)

    return {
        "request_id": req.id,
        "approval_state": req.approval_state,
        "target_name": req.target_name,
        "target_phone": req.target_phone,
        "goal": req.goal,
        "message": "Call request created. Waiting for owner approval.",
    }


@router.get("/requests")
async def list_call_requests(
    agent: str = "", state: str = "", limit: int = 50
) -> dict:
    """List call requests with optional filters."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")
    requests = _voice_store.list_call_requests(
        agent_name=agent, state=state, limit=limit
    )
    return {"requests": [r.to_dict() for r in requests]}


@router.get("/request/{request_id}")
async def get_call_request(request_id: str) -> dict:
    """Get a single call request by ID."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")
    req = _voice_store.get_call_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Call request not found")
    return req.to_dict()


@router.post("/request/{request_id}/approve")
async def approve_call_request(request_id: str) -> dict:
    """Approve a pending call request. Idempotent."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")

    req = _voice_store.get_call_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Call request not found")

    if req.approval_state == "approved":
        return {"request_id": request_id, "approval_state": "approved",
                "message": "Already approved."}

    if req.approval_state not in ("pending_approval",):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve — current state is '{req.approval_state}'",
        )

    if req.expires_at and time.time() > req.expires_at:
        _voice_store.update_call_request_state(
            request_id, approval_state="expired"
        )
        raise HTTPException(status_code=410, detail="Call request has expired")

    _voice_store.update_call_request_state(
        request_id,
        approval_state="approved",
        authorized_by="owner",
        authorized_at=time.time(),
    )

    return {
        "request_id": request_id,
        "approval_state": "approved",
        "message": "Call request approved. Ready for Phase 2 dial.",
    }


@router.post("/request/{request_id}/deny")
async def deny_call_request(request_id: str) -> dict:
    """Deny a pending call request. Idempotent."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")

    req = _voice_store.get_call_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Call request not found")

    if req.approval_state == "rejected":
        return {"request_id": request_id, "approval_state": "rejected",
                "message": "Already denied."}

    if req.approval_state == "approved":
        raise HTTPException(
            status_code=409,
            detail="Cannot deny — request is already approved.",
        )

    _voice_store.update_call_request_state(
        request_id, approval_state="rejected"
    )

    return {
        "request_id": request_id,
        "approval_state": "rejected",
        "message": "Call request denied.",
    }


@router.post("/request/{request_id}/cancel")
async def cancel_call_request_endpoint(
    request_id: str, body: CancelCallRequest
) -> dict:
    """Cancel a call request."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")

    req = _voice_store.cancel_call_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Call request not found")

    return {"request_id": request_id, "approval_state": "cancelled"}


# ── Phase 2 stubs ─────────────────────────────────────────────────────────────

def _stub_501(detail: str = "Not implemented — Phase 2") -> None:
    raise HTTPException(status_code=501, detail=detail)


@router.api_route(
    "/twiml/outbound/{call_request_id}", methods=["GET", "POST"]
)
async def twiml_outbound(call_request_id: str, request: Request) -> Response:
    """TwiML for outbound call answer — returns CR WebSocket connect."""
    _stub_501()


@router.api_route("/twiml/inbound", methods=["GET", "POST"])
async def twiml_inbound(request: Request) -> Response:
    """TwiML for inbound calls — AI voicemail."""
    _stub_501("Not implemented — Phase 3")


@router.post("/status/{call_sid}")
async def status_callback(call_sid: str, request: Request) -> dict:
    """Twilio call status callback."""
    _stub_501()


@router.post("/amd/{call_request_id}")
async def amd_callback(call_request_id: str, request: Request) -> dict:
    """Twilio AMD (answering machine detection) callback."""
    _stub_501()


@router.websocket("/ws/{call_session_id}")
async def conversationrelay_ws(ws: WebSocket, call_session_id: str):
    """ConversationRelay WebSocket handler — Haiku subagent conversation."""
    await ws.close(code=4001, reason="Not implemented — Phase 2")


@router.post("/session/{call_sid}/terminate")
async def terminate_session(call_sid: str, body: TerminateSessionRequest) -> dict:
    """Terminate an active call session."""
    _stub_501()


@router.post("/session/{call_sid}/transfer")
async def transfer_session(call_sid: str, body: TransferSessionRequest) -> dict:
    """Transfer an active call to a human."""
    _stub_501()


@router.post("/session/{call_sid}/dtmf")
async def send_dtmf_endpoint(call_sid: str, body: DtmfRequest) -> dict:
    """Send DTMF tones on an active call."""
    _stub_501()


# ── Session / artifact read endpoints ─────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(
    agent: str = "", direction: str = "", status: str = "", limit: int = 50
) -> dict:
    """List voice call sessions with optional filters."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")
    sessions = _voice_store.list_sessions(
        agent_name=agent, direction=direction, status=status, limit=limit
    )
    return {"sessions": [s.to_dict() for s in sessions]}


@router.get("/session/{call_sid}")
async def get_session_endpoint(call_sid: str) -> dict:
    """Get a voice call session by Twilio call SID."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")
    session = _voice_store.get_session_by_call_sid(call_sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@router.get("/session/{call_sid}/events")
async def get_session_events(call_sid: str) -> dict:
    """Get events for a voice call session."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")
    session = _voice_store.get_session_by_call_sid(call_sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    events = _voice_store.get_events(session.id)
    return {"events": [e.to_dict() for e in events]}


@router.get("/session/{call_sid}/artifact")
async def get_session_artifact(call_sid: str) -> dict:
    """Get post-call artifact for a voice call session."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")
    artifact = _voice_store.get_artifact_by_call_sid(call_sid)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact.to_dict()
