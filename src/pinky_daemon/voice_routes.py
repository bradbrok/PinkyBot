"""Voice Routes — FastAPI router for Twilio voice calling.

Endpoints for outbound AI phone calls (propose → approve → dial → converse → report)
and inbound AI voicemail. Phase 2: full outbound calling via ConversationRelay.

Dependency injection: call set_dependencies(voice_store=..., agents=..., broker_send=...)
from api.py after stores are initialised.
"""

from __future__ import annotations

import asyncio
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

# Active WebSocket connections keyed by session ID (for mid-call control)
_active_ws: dict[str, WebSocket] = {}

# Agents trusted for auto-approve (skip manual approval, dial immediately)
AUTO_APPROVE_AGENTS = {"barsik"}


def _get_base_url() -> str:
    """Get PINKY_BASE_URL dynamically from settings or env."""
    import os
    if _base_url:
        return _base_url
    if _agents:
        url = _agents.get_setting("PINKY_BASE_URL")
        if url:
            return url
    return os.environ.get("PINKY_BASE_URL", "")


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


# ── Twilio signature validation ──────────────────────────────────────────────


async def _validate_twilio_request(request: Request) -> bool:
    """Validate X-Twilio-Signature on incoming Twilio webhooks.

    Returns True if valid, raises 403 if invalid.
    """
    if not _agents:
        return True  # can't validate without agent registry
    from pinky_daemon.voice_engine import validate_twilio_signature

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        _log("voice: missing X-Twilio-Signature — rejecting")
        raise HTTPException(status_code=403, detail="Missing Twilio signature")

    # Build the full URL Twilio used to sign.
    # Behind a reverse proxy (Tailscale Funnel), request.url is the local
    # address (http://127.0.0.1:8888/...) but Twilio signed with the public
    # URL (https://olegs-mac-mini.../...).  Reconstruct it from PINKY_BASE_URL.
    base = _get_base_url()
    if base:
        url = f"https://{base}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"
    else:
        url = str(request.url)
    # For POST requests, params are form data; for GET, query params
    params: dict = {}
    if request.method == "POST":
        form = await request.form()
        params = dict(form)
    else:
        params = dict(request.query_params)

    if not validate_twilio_signature(_agents, url, params, signature):
        _log(f"voice: invalid Twilio signature for {request.url.path}")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    return True


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


async def _notify_owner_auto_approved(req: Any) -> None:
    """Notify owner that a call was auto-approved and is dialing."""
    if not _agents or not _broker_send:
        return
    primary = _agents.get_primary_user()
    if not primary or not primary.get("chat_id"):
        return

    ctx = json.loads(req.context) if isinstance(req.context, str) else req.context
    ctx_lines = "\n".join(f"  {k}: {v}" for k, v in ctx.items()) if ctx else ""

    text = (
        f"📲 Auto-approved call from {req.requested_by_agent}\n\n"
        f"Target: {req.target_name}\n"
        f"Phone: {req.target_phone}\n"
        f"Goal: {req.goal}"
    )
    if ctx_lines:
        text += f"\nContext:\n{ctx_lines}"

    notify_agent = primary.get("default_agent") or "barsik"
    try:
        await _broker_send(
            notify_agent, "telegram", str(primary["chat_id"]), text,
        )
    except Exception as e:
        _log(f"voice: failed to notify owner (auto-approve) — {e}")


# ── Endpoints: Call Request lifecycle ────────────────────────────────────────


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

    # Auto-approve for trusted agents — dial immediately, notify owner
    if body.requested_by_agent in AUTO_APPROVE_AGENTS:
        _voice_store.update_call_request_state(
            req.id,
            approval_state="approved",
            authorized_by="auto_approve",
            authorized_at=time.time(),
        )
        req = _voice_store.get_call_request(req.id)

        # Notify owner (informational, not requesting approval)
        await _notify_owner_auto_approved(req)

        # Trigger dial
        dial_result = {}
        base = _get_base_url()
        if base:
            try:
                from pinky_daemon.voice_engine import dial_approved_call
                dial_result = await dial_approved_call(
                    req, _voice_store, _agents, base, _broker_send,
                )
            except Exception as e:
                _log(f"voice: auto-approve dial failed — {e}")
                dial_result = {"error": str(e)}
        else:
            dial_result = {"error": "PINKY_BASE_URL not configured"}

        return {
            "request_id": req.id,
            "approval_state": "approved",
            "target_name": req.target_name,
            "target_phone": req.target_phone,
            "goal": req.goal,
            "message": "Auto-approved and dialing.",
            "dial": dial_result,
        }

    # Non-trusted agents: notify owner for manual approval
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
    """Approve a pending call request and initiate the dial."""
    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")

    req = _voice_store.get_call_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Call request not found")

    if req.approval_state == "approved":
        return {
            "request_id": request_id, "approval_state": "approved",
            "message": "Already approved.",
        }

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

    # Trigger the dial
    dial_result = {}
    base = _get_base_url()
    if base:
        try:
            from pinky_daemon.voice_engine import dial_approved_call

            # Re-fetch after state update
            updated_req = _voice_store.get_call_request(request_id)
            dial_result = await dial_approved_call(
                updated_req, _voice_store, _agents, base, _broker_send,
            )
        except Exception as e:
            _log(f"voice: dial trigger failed — {e}")
            dial_result = {"error": str(e)}
    else:
        _log("voice: PINKY_BASE_URL not set — cannot dial")
        dial_result = {"error": "PINKY_BASE_URL not configured"}

    return {
        "request_id": request_id,
        "approval_state": "approved",
        "message": "Call approved and dial initiated.",
        "dial": dial_result,
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
        return {
            "request_id": request_id, "approval_state": "rejected",
            "message": "Already denied.",
        }

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


# ── Twilio callbacks ────────────────────────────────────────────────────────


@router.api_route(
    "/twiml/outbound/{call_request_id}", methods=["GET", "POST"]
)
async def twiml_outbound(
    call_request_id: str, request: Request
) -> Response:
    """Return ConversationRelay TwiML when Twilio connects outbound call."""
    await _validate_twilio_request(request)

    if not _voice_store:
        raise HTTPException(status_code=503, detail="Voice module not initialized")

    req = _voice_store.get_call_request(call_request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Call request not found")

    # Find the session for this request
    sessions = _voice_store.list_sessions(agent_name=req.requested_by_agent)
    session = None
    for s in sessions:
        if s.call_request_id == call_request_id:
            session = s
            break

    if not session:
        raise HTTPException(
            status_code=404, detail="No session found for this call request"
        )

    # Update session status — TwiML fetch means call is connecting (not yet answered)
    _voice_store.update_session(session.id, status="connecting")

    # Build ConversationRelay TwiML with disclosure greeting
    from pinky_daemon.voice_engine import (
        build_disclosure_greeting,
        build_outbound_twiml,
    )

    greeting = build_disclosure_greeting(req.target_name, req.goal)
    ws_url = f"wss://{_get_base_url()}/ws/voice/{session.id}"
    twiml = build_outbound_twiml(ws_url, welcome_greeting=greeting)

    _log(f"voice: TwiML served for session {session.id}, ws={ws_url}")
    return Response(content=twiml, media_type="application/xml")


@router.api_route("/twiml/inbound", methods=["GET", "POST"])
async def twiml_inbound(request: Request) -> Response:
    """TwiML for inbound calls — AI voicemail."""
    raise HTTPException(status_code=501, detail="Not implemented — Phase 3")


@router.post("/status/{call_sid}")
async def status_callback(call_sid: str, request: Request) -> dict:
    """Twilio call status callback — updates session state."""
    await _validate_twilio_request(request)
    form = await request.form()
    twilio_status = form.get("CallStatus", "")
    duration = form.get("CallDuration", "0")
    # Twilio puts the real CallSid in form data — use that over path param
    real_sid = form.get("CallSid", "") or call_sid

    _log(f"voice: status callback — SID={real_sid}, status={twilio_status}")

    if not _voice_store:
        return {"ok": True}

    # Try real SID first, fall back to path param (covers pending-xxx placeholders)
    session = _voice_store.get_session_by_call_sid(real_sid)
    if not session:
        session = _voice_store.get_session_by_call_sid(call_sid)
    if not session:
        _log(f"voice: status callback for unknown SID {call_sid}")
        return {"ok": True}

    # Map Twilio status to internal status
    status_map = {
        "queued": "queued",
        "ringing": "ringing",
        "in-progress": "in_progress",
        "completed": "completed",
        "failed": "failed",
        "busy": "failed",
        "no-answer": "no_answer",
        "canceled": "cancelled",
    }
    internal_status = status_map.get(twilio_status, twilio_status)

    updates: dict[str, Any] = {"status": internal_status}
    if internal_status in ("completed", "failed", "no_answer", "cancelled"):
        if not session.ended_at:
            updates["ended_at"] = time.time()

    _voice_store.update_session(session.id, **updates)

    # Log the status event
    _voice_store.log_event(
        call_session_id=session.id,
        call_sid=real_sid,
        event_type="status",
        content=twilio_status,
        metadata={"duration": duration},
    )

    return {"ok": True}


@router.post("/amd/{call_request_id}")
async def amd_callback(call_request_id: str, request: Request) -> dict:
    """Twilio AMD (answering machine detection) callback."""
    await _validate_twilio_request(request)
    form = await request.form()
    answered_by = form.get("AnsweredBy", "unknown")
    call_sid = form.get("CallSid", "")

    _log(f"voice: AMD callback — request={call_request_id}, "
         f"answered_by={answered_by}")

    if not _voice_store:
        return {"ok": True}

    req = _voice_store.get_call_request(call_request_id)
    session = _voice_store.get_session_by_call_sid(call_sid) if call_sid else None

    if session:
        # Log AMD event
        _voice_store.log_event(
            call_session_id=session.id,
            call_sid=call_sid,
            event_type="amd",
            content=answered_by,
        )

    # Handle machine detection based on fallback_behavior
    if answered_by in ("machine_start", "machine_end_beep",
                       "machine_end_silence", "machine_end_other", "fax"):
        fallback = req.fallback_behavior if req else "notify_brad_if_no_answer"

        if fallback == "hang_up":
            _log(f"voice: AMD detected machine — hanging up (SID={call_sid})")
            try:
                from pinky_daemon.voice_engine import get_twilio_client
                client = get_twilio_client(_agents)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: client.calls(call_sid).update(status="completed"),
                )
            except Exception as e:
                _log(f"voice: failed to hang up on machine — {e}")

        elif fallback == "notify_brad_if_no_answer":
            if _broker_send and _agents:
                primary = _agents.get_primary_user()
                if primary and primary.get("chat_id"):
                    target = req.target_name if req else call_sid
                    notify_agent = (
                        primary.get("default_agent") or "barsik"
                    )
                    try:
                        await _broker_send(
                            notify_agent, "telegram",
                            str(primary["chat_id"]),
                            f"📞 Call to {target} reached voicemail "
                            f"({answered_by}). No message left.",
                        )
                    except Exception as e:
                        _log(f"voice: AMD notify failed — {e}")

    return {"ok": True}


# ── ConversationRelay WebSocket ──────────────────────────────────────────────


# NOTE: Not decorated — registered on app directly at /ws/voice/{id}.
# IMPORTANT: Must call ws.accept() BEFORE any validation, otherwise
# Starlette returns HTTP 403 instead of a proper WS close frame.
async def conversationrelay_ws(ws: WebSocket, call_session_id: str):
    """ConversationRelay WebSocket — Haiku subagent phone conversation."""
    await ws.accept()

    if not _voice_store:
        _log("voice: WS rejected — voice module not initialized")
        await ws.close(code=4001, reason="Voice module not initialized")
        return

    session = _voice_store.get_session(call_session_id)
    if not session:
        _log(f"voice: WS rejected — session {call_session_id} not found")
        await ws.close(code=4004, reason="Session not found")
        return

    # Only accept sessions that are in an expected pre-conversation state
    if session.status not in ("queued", "ringing", "connecting", "in_progress"):
        _log(f"voice: WS rejected — session {call_session_id} status={session.status}")
        await ws.close(code=4003, reason="Session not active")
        return

    _active_ws[call_session_id] = ws
    _log(f"voice: WS connected for session {call_session_id}")

    # Get call request for goal/context
    req = _voice_store.get_call_request(
        session.call_request_id
    ) if session.call_request_id else None

    # Import engine functions
    from pinky_daemon.voice_engine import (
        build_disclosure_greeting,
        build_voice_agent_prompt,
        finalize_call,
        haiku_respond,
    )

    # Build system prompt
    ctx = {}
    goal = "assist with the call"
    target_name = session.to_number
    if req:
        ctx = json.loads(req.context) if isinstance(req.context, str) else req.context
        goal = req.goal
        target_name = req.target_name

    system_prompt = build_voice_agent_prompt(
        target_name=target_name,
        goal=goal,
        context=ctx,
        caller_name=ctx.get("name", "Brad"),
        max_duration_sec=session.max_duration_sec,
    )

    # Get API key: agent's provider_key → system setting → env var
    _api_key = ""
    if _agents:
        agent_info = _agents.get(session.agent_name)
        if agent_info and getattr(agent_info, "provider_key", ""):
            _api_key = agent_info.provider_key
        if not _api_key:
            _api_key = _agents.get_setting("ANTHROPIC_API_KEY") or ""

    # Conversation state
    messages: list[dict] = []
    call_active = True
    setup_received = False
    # Track mutable call state locally (session object is stale after DB updates)
    live_call_sid = session.call_sid
    live_answered_at: float | None = session.answered_at

    try:
        while call_active:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "")

            # Reject non-setup messages before setup is complete
            if not setup_received and msg_type != "setup":
                _log(f"voice: received {msg_type} before setup — ignoring")
                continue

            if msg_type == "setup":
                # CR connected — validate and update session
                cr_sid = msg.get("sessionId", "")
                call_sid = msg.get("callSid", "")
                account_sid = msg.get("accountSid", "")
                _log(f"voice: setup — CR={cr_sid}, callSid={call_sid}")

                # Validate: callSid must match session (or pending placeholder)
                if call_sid and not session.call_sid.startswith("pending"):
                    if call_sid != session.call_sid:
                        _log(f"voice: callSid mismatch — "
                             f"expected {session.call_sid}, got {call_sid}")
                        await ws.close(code=4003, reason="callSid mismatch")
                        call_active = False
                        continue

                # Validate: accountSid should match our Twilio account
                if account_sid and _agents:
                    expected_sid = _agents.get_setting("TWILIO_ACCOUNT_SID")
                    if expected_sid and account_sid != expected_sid:
                        _log("voice: accountSid mismatch — rejecting")
                        await ws.close(code=4003, reason="Invalid account")
                        call_active = False
                        continue

                if call_sid and call_sid != session.call_sid:
                    _voice_store.update_session(
                        session.id, call_sid=call_sid
                    )
                    live_call_sid = call_sid

                now = time.time()
                live_answered_at = now
                _voice_store.update_session(
                    session.id,
                    cr_session_id=cr_sid,
                    status="in_progress",
                    answered_at=now,
                )

                # Disclosure greeting is handled by welcomeGreeting in TwiML
                # — no need to send it over WS. Just log it.
                greeting = build_disclosure_greeting(target_name, goal)
                _voice_store.update_session(
                    session.id, disclosure_completed_at=time.time()
                )
                _voice_store.log_event(
                    call_session_id=session.id,
                    call_sid=live_call_sid,
                    event_type="disclosure",
                    role="agent",
                    content=greeting,
                )
                _log("voice: disclosure via welcomeGreeting (TwiML)")
                setup_received = True

            elif msg_type == "prompt":
                # Caller spoke — transcribed text from CR
                voice_prompt = msg.get("voicePrompt", "")
                is_last = msg.get("last", True)

                if not voice_prompt or not is_last:
                    continue

                _log(f"voice: caller said: {voice_prompt[:80]}")

                # Log caller turn
                _voice_store.log_event(
                    call_session_id=session.id,
                    call_sid=live_call_sid,
                    event_type="prompt",
                    role="caller",
                    content=voice_prompt,
                )

                # Add to conversation history
                messages.append({
                    "role": "user", "content": voice_prompt
                })

                # Stream Haiku response
                full_response = []
                try:
                    async for token in haiku_respond(
                        messages, system_prompt, api_key=_api_key
                    ):
                        full_response.append(token)
                        await ws.send_text(json.dumps({
                            "type": "text",
                            "token": token,
                            "last": False,
                        }))

                    # Send last=true to signal end of agent turn
                    await ws.send_text(json.dumps({
                        "type": "text",
                        "token": "",
                        "last": True,
                    }))
                except Exception as e:
                    _log(f"voice: Haiku streaming error — {e}")
                    await ws.send_text(json.dumps({
                        "type": "text",
                        "token": "I'm sorry, I'm having a "
                                 "technical issue. Let me have "
                                 "Brad follow up with you directly.",
                        "last": True,
                    }))
                    full_response = ["[error: Haiku failed]"]

                response_text = "".join(full_response)

                # Add assistant response to history
                messages.append({
                    "role": "assistant", "content": response_text
                })

                # Log agent turn
                _voice_store.log_event(
                    call_session_id=session.id,
                    call_sid=live_call_sid,
                    event_type="response",
                    role="agent",
                    content=response_text,
                )

                # Check duration limit (from answer time, not dial time)
                call_start = live_answered_at or session.started_at
                elapsed = time.time() - call_start
                if elapsed >= session.max_duration_sec:
                    _log("voice: max duration reached — ending call")
                    await ws.send_text(json.dumps({
                        "type": "end",
                        "handoffData": json.dumps({"reason": "timeout"}),
                    }))
                    call_active = False

            elif msg_type == "interrupt":
                _log("voice: caller interrupted (barge-in)")
                _voice_store.log_event(
                    call_session_id=session.id,
                    call_sid=live_call_sid,
                    event_type="interrupt",
                    role="caller",
                )

            elif msg_type == "dtmf":
                digit = msg.get("digit", "")
                _log(f"voice: DTMF received: {digit}")
                _voice_store.log_event(
                    call_session_id=session.id,
                    call_sid=live_call_sid,
                    event_type="dtmf",
                    content=digit,
                )
                # Feed DTMF into conversation context
                messages.append({
                    "role": "user",
                    "content": f"[The caller pressed {digit} on the keypad]",
                })

            elif msg_type == "end":
                call_status = msg.get("handoffData", "")
                _log(f"voice: call ended — {call_status}")
                _voice_store.log_event(
                    call_session_id=session.id,
                    call_sid=live_call_sid,
                    event_type="end",
                    content=call_status,
                )
                call_active = False

            else:
                _log(f"voice: unknown WS message type: {msg_type}")

    except Exception as e:
        _log(f"voice: WS error — {e}")
    finally:
        _active_ws.pop(call_session_id, None)

        # Update session as ended
        _voice_store.update_session(
            session.id, status="completed", ended_at=time.time()
        )

        # Finalize call in background (Opus review + artifact + notify)
        if messages:
            asyncio.create_task(
                finalize_call(
                    _voice_store.get_session(call_session_id),
                    _voice_store, _agents, _broker_send,
                    api_key=_api_key,
                )
            )

        _log(f"voice: WS closed for session {call_session_id}")


# ── In-call control endpoints ───────────────────────────────────────────────


@router.post("/session/{call_sid}/terminate")
async def terminate_session(
    call_sid: str, body: TerminateSessionRequest
) -> dict:
    """Terminate an active call session."""
    if not _voice_store or not _agents:
        raise HTTPException(
            status_code=503, detail="Voice module not initialized"
        )

    session = _voice_store.get_session_by_call_sid(call_sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Send end message over WS if connected
    ws = _active_ws.get(session.id)
    if ws:
        try:
            await ws.send_text(json.dumps({
                "type": "end",
                "handoffData": json.dumps({"reason": body.reason}),
            }))
        except Exception:
            pass

    # Also hang up via Twilio REST
    try:
        from pinky_daemon.voice_engine import get_twilio_client
        client = get_twilio_client(_agents)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: client.calls(call_sid).update(status="completed"),
        )
    except Exception as e:
        _log(f"voice: terminate failed — {e}")

    _voice_store.update_session(
        session.id, status="completed", ended_at=time.time()
    )
    return {"terminated": True, "call_sid": call_sid}


@router.post("/session/{call_sid}/transfer")
async def transfer_session(
    call_sid: str, body: TransferSessionRequest
) -> dict:
    """Transfer an active call to Brad."""
    if not _voice_store or not _agents:
        raise HTTPException(
            status_code=503, detail="Voice module not initialized"
        )

    session = _voice_store.get_session_by_call_sid(call_sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get Brad's phone from context or settings
    primary = _agents.get_primary_user()
    brad_phone = primary.get("phone", "") if primary else ""
    if not brad_phone:
        raise HTTPException(
            status_code=400, detail="Owner phone not configured"
        )

    # End ConversationRelay session, then redirect call via REST API
    ws = _active_ws.get(session.id)
    if ws:
        try:
            await ws.send_text(json.dumps({
                "type": "end",
                "handoffData": json.dumps({
                    "reason": "transfer",
                    "transferTo": brad_phone,
                }),
            }))
        except Exception as e:
            _log(f"voice: transfer WS end failed — {e}")

    # Redirect call to Brad's number via Twilio REST
    try:
        from pinky_daemon.voice_engine import get_twilio_client
        client = get_twilio_client(_agents)
        loop = asyncio.get_running_loop()

        # Redirect the call to dial Brad via inline TwiML
        transfer_twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Dial>{brad_phone}</Dial></Response>"
        )
        await loop.run_in_executor(
            None,
            lambda: client.calls(call_sid).update(
                twiml=transfer_twiml,
            ),
        )
    except Exception as e:
        _log(f"voice: transfer REST redirect failed — {e}")

    _voice_store.log_event(
        call_session_id=session.id,
        call_sid=call_sid,
        event_type="transfer",
        content=f"Transferred to {brad_phone}: {body.reason}",
    )

    return {"transferred": True, "call_sid": call_sid, "to": brad_phone}


@router.post("/session/{call_sid}/dtmf")
async def send_dtmf_endpoint(
    call_sid: str, body: DtmfRequest
) -> dict:
    """Send DTMF tones on an active call."""
    if not _voice_store:
        raise HTTPException(
            status_code=503, detail="Voice module not initialized"
        )

    session = _voice_store.get_session_by_call_sid(call_sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    ws = _active_ws.get(session.id)
    if not ws:
        raise HTTPException(
            status_code=409, detail="No active WS for this session"
        )

    try:
        await ws.send_text(json.dumps({
            "type": "sendDigits", "digits": body.digits,
        }))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to send DTMF: {e}"
        )

    _voice_store.log_event(
        call_session_id=session.id,
        call_sid=call_sid,
        event_type="dtmf_sent",
        content=body.digits,
    )

    return {"sent": True, "digits": body.digits}


# ── Session / artifact read endpoints ─────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(
    agent: str = "", direction: str = "", status: str = "", limit: int = 50
) -> dict:
    """List voice call sessions with optional filters."""
    if not _voice_store:
        raise HTTPException(
            status_code=503, detail="Voice module not initialized"
        )
    sessions = _voice_store.list_sessions(
        agent_name=agent, direction=direction, status=status, limit=limit
    )
    return {"sessions": [s.to_dict() for s in sessions]}


@router.get("/session/{call_sid}")
async def get_session_endpoint(call_sid: str) -> dict:
    """Get a voice call session by Twilio call SID."""
    if not _voice_store:
        raise HTTPException(
            status_code=503, detail="Voice module not initialized"
        )
    session = _voice_store.get_session_by_call_sid(call_sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@router.get("/session/{call_sid}/events")
async def get_session_events(call_sid: str) -> dict:
    """Get events for a voice call session."""
    if not _voice_store:
        raise HTTPException(
            status_code=503, detail="Voice module not initialized"
        )
    session = _voice_store.get_session_by_call_sid(call_sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    events = _voice_store.get_events(session.id)
    return {"events": [e.to_dict() for e in events]}


@router.get("/session/{call_sid}/artifact")
async def get_session_artifact(call_sid: str) -> dict:
    """Get post-call artifact for a voice call session."""
    if not _voice_store:
        raise HTTPException(
            status_code=503, detail="Voice module not initialized"
        )
    artifact = _voice_store.get_artifact_by_call_sid(call_sid)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact.to_dict()
