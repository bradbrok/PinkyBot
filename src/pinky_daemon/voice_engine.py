"""Voice Engine — conversation logic for Twilio ConversationRelay calls.

Handles Haiku subagent prompting, streaming token delivery, post-call
finalization with Opus review, and Twilio signature validation.

Dependencies:
  - anthropic SDK (for Haiku streaming + Opus review)
  - twilio SDK (for REST client + request validation)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, AsyncIterator, Callable

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_OPUS_MODEL = "claude-opus-4-6-20260205"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Twilio helpers ───────────────────────────────────────────────────────────


def get_twilio_client(agents: Any) -> Any:
    """Lazy-import and build Twilio REST client from system settings."""
    from twilio.rest import Client

    sid = agents.get_setting("TWILIO_ACCOUNT_SID") or os.environ.get(
        "TWILIO_ACCOUNT_SID", ""
    )
    token = agents.get_setting("TWILIO_AUTH_TOKEN") or os.environ.get(
        "TWILIO_AUTH_TOKEN", ""
    )
    if not sid or not token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN required")
    return Client(sid, token)


def get_twilio_phone(agents: Any) -> str:
    """Get configured Twilio phone number."""
    phone = agents.get_setting("TWILIO_PHONE_NUMBER") or os.environ.get(
        "TWILIO_PHONE_NUMBER", ""
    )
    if not phone:
        raise RuntimeError("TWILIO_PHONE_NUMBER not configured")
    return phone


def validate_twilio_signature(
    agents: Any, url: str, params: dict, signature: str
) -> bool:
    """Validate X-Twilio-Signature on incoming webhooks."""
    from twilio.request_validator import RequestValidator

    token = agents.get_setting("TWILIO_AUTH_TOKEN") or os.environ.get(
        "TWILIO_AUTH_TOKEN", ""
    )
    if not token:
        _log("voice: cannot validate signature — no auth token")
        return False
    validator = RequestValidator(token)
    return validator.validate(url, params, signature)


# ── Voice agent prompt ───────────────────────────────────────────────────────


def build_voice_agent_prompt(
    target_name: str,
    goal: str,
    context: dict,
    caller_name: str = "Brad",
    max_duration_sec: int = 300,
) -> str:
    """Build system prompt for the Haiku voice subagent (outbound calls)."""
    context_lines = "\n".join(
        f"- {k}: {v}" for k, v in context.items()
    ) if context else "- (no additional context)"

    return f"""You are an AI phone assistant calling {target_name} \
on behalf of {caller_name}.

GOAL: {goal}

INFO YOU HAVE (use as needed during the call):
{context_lines}

RULES:
- Be polite and concise. This is a phone call — no bullet points, no markdown.
- Speak naturally. Short sentences. Pause between thoughts.
- If you achieve the goal, confirm the details back and end the call politely.
- If the preferred option isn't available, negotiate within any flexible range \
before giving up.
- If they can't help or the goal isn't achievable, thank them and end the call.
- If the conversation goes off-script or you're unsure, say you'll have \
{caller_name} follow up directly.
- Only share info listed above. Never volunteer address, email, payment info, \
or anything beyond the task scope.
- Never claim to be human if sincerely asked.

CONSTRAINTS:
- Max call duration is {max_duration_sec} seconds — be efficient.
- Do not ask more than one question at a time.
- Do not negotiate prices, make commitments beyond the stated goal, or \
improvise scope."""


def build_disclosure_greeting(target_name: str, goal: str) -> str:
    """Build the mandatory AI disclosure greeting sent before Haiku takes over."""
    return (
        f"Hi, this is an AI assistant calling on behalf of Brad. "
        f"I'm calling to {goal.lower().rstrip('.')}. Is that okay to proceed?"
    )


# ── Haiku streaming ─────────────────────────────────────────────────────────


async def haiku_respond(
    messages: list[dict],
    system_prompt: str,
    api_key: str = "",
) -> AsyncIterator[str]:
    """Stream a Haiku response token by token.

    Yields text chunks as they arrive. Caller is responsible for
    sending them as CR text messages.
    """
    import anthropic

    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.AsyncAnthropic(api_key=key)

    async with client.messages.stream(
        model=_HAIKU_MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


# ── Outbound dial ────────────────────────────────────────────────────────────


async def dial_approved_call(
    req: Any,
    voice_store: Any,
    agents: Any,
    base_url: str,
    broker_send: Callable | None = None,
) -> dict:
    """Initiate a Twilio outbound call for an approved request.

    Creates a voice_call_session, fires Twilio calls.create(),
    and returns the session info. Runs Twilio REST call in a thread
    to avoid blocking the event loop.
    """
    loop = asyncio.get_running_loop()

    twilio_phone = get_twilio_phone(agents)

    # Create session first (before calling Twilio)
    # Use unique placeholder — literal "pending" causes UNIQUE constraint
    # violations when previous sessions failed before getting a real SID.
    import uuid as _uuid

    pending_sid = f"pending-{_uuid.uuid4().hex[:12]}"
    session = voice_store.create_session(
        call_request_id=req.id,
        call_sid=pending_sid,  # updated after Twilio responds
        voice_model=_HAIKU_MODEL,
        agent_name=req.requested_by_agent,
        direction="outbound",
        from_number=twilio_phone,
        to_number=req.target_phone,
        max_duration_sec=req.max_duration_sec,
    )

    # Build callback URLs
    twiml_url = f"https://{base_url}/api/voice/twiml/outbound/{req.id}"
    status_url = f"https://{base_url}/api/voice/status/{pending_sid}"
    amd_url = f"https://{base_url}/api/voice/amd/{req.id}"

    try:
        client = get_twilio_client(agents)

        # Run Twilio REST call in thread (it's synchronous)
        call = await loop.run_in_executor(
            None,
            lambda: client.calls.create(
                to=req.target_phone,
                from_=twilio_phone,
                url=twiml_url,
                status_callback=status_url,
                status_callback_method="POST",
                machine_detection="DetectMessageEnd",
                machine_detection_timeout=30,
                async_amd=True,
                async_amd_status_callback=amd_url,
                async_amd_status_callback_method="POST",
            ),
        )

        # Update session with real call SID
        voice_store.update_session(
            session.id, call_sid=call.sid, status="queued"
        )

        _log(f"voice: dial initiated — SID={call.sid}, to={req.target_phone}")

        return {
            "session_id": session.id,
            "call_sid": call.sid,
            "status": "queued",
        }

    except Exception as e:
        _log(f"voice: dial failed — {e}")
        voice_store.update_session(
            session.id, status="failed", failure_reason=str(e)
        )

        # Notify owner of failure
        if broker_send and agents:
            primary = agents.get_primary_user()
            if primary and primary.get("chat_id"):
                notify_agent = primary.get("default_agent") or "barsik"
                try:
                    await broker_send(
                        notify_agent, "telegram", str(primary["chat_id"]),
                        f"❌ Call to {req.target_name} failed to dial: {e}",
                    )
                except Exception:
                    pass

        return {
            "session_id": session.id,
            "call_sid": "",
            "status": "failed",
            "error": str(e),
        }


# ── TwiML generation ────────────────────────────────────────────────────────


def build_outbound_twiml(ws_url: str, welcome_greeting: str = "") -> str:
    """Generate TwiML XML for outbound call with ConversationRelay."""
    import xml.sax.saxutils as saxutils

    greeting_attr = ""
    if welcome_greeting:
        greeting_attr = (
            f' welcomeGreeting="{saxutils.escape(welcome_greeting)}"'
            ' welcomeGreetingInterruptible="speech"'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<ConversationRelay url="{ws_url}"'
        f'{greeting_attr} '
        'ttsProvider="Google" '
        'voice="en-US-Journey-O" '
        'dtmfDetection="true" '
        'interruptible="true" />'
        "</Connect>"
        "</Response>"
    )


# ── Post-call finalization ───────────────────────────────────────────────────


def build_transcript(events: list) -> list[dict]:
    """Build a structured transcript from voice call events."""
    turns = []
    for ev in events:
        if ev.event_type in ("prompt", "response", "disclosure"):
            turns.append({
                "role": ev.role or ev.event_type,
                "text": ev.content,
                "ts": ev.ts,
            })
        elif ev.event_type in ("dtmf", "interrupt", "amd"):
            turns.append({
                "role": "system",
                "event": ev.event_type,
                "text": ev.content,
                "ts": ev.ts,
            })
    return turns


async def extract_outcome_with_opus(
    session: Any, transcript: list[dict], goal: str,
    api_key: str = "",
) -> dict:
    """Use Opus to review a call transcript and extract structured outcome."""
    import anthropic

    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.AsyncAnthropic(api_key=key)

    transcript_text = "\n".join(
        f"[{t.get('role', 'unknown')}] {t.get('text', '')}"
        for t in transcript
    )

    system = (
        "You review phone call transcripts and extract structured outcomes. "
        "Be concise and factual."
    )
    user = f"""Review this phone call transcript and extract the outcome.

CALL GOAL: {goal}

TRANSCRIPT:
{transcript_text}

Respond with JSON only:
{{
  "success": true/false,
  "summary": "One sentence summary of what happened",
  "key_details": {{"any": "relevant details like confirmation numbers, times, etc"}},
  "notes": "Any important observations"
}}"""

    try:
        response = await client.messages.create(
            model=_OPUS_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = response.content[0].text if response.content else "{}"
        # Extract JSON from response (may be wrapped in markdown)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as e:
        _log(f"voice: opus outcome extraction failed — {e}")
        return {
            "success": False,
            "summary": "Could not extract outcome from transcript",
            "notes": str(e),
        }


async def finalize_call(
    session: Any,
    voice_store: Any,
    agents: Any,
    broker_send: Callable | None = None,
    api_key: str = "",
) -> None:
    """Post-call finalization: build transcript, Opus review, save artifact, notify."""
    _log(f"voice: finalizing call {session.call_sid}")

    # Get call request for goal context
    req = voice_store.get_call_request(
        session.call_request_id
    ) if session.call_request_id else None
    goal = req.goal if req else "Unknown goal"

    # Build transcript from events
    events = voice_store.get_events(session.id)
    transcript = build_transcript(events)

    if not transcript:
        _log("voice: no transcript events — skipping finalization")
        return

    # Opus reviews transcript
    outcome = await extract_outcome_with_opus(
        session, transcript, goal, api_key=api_key
    )

    # Save artifact
    voice_store.save_artifact(
        call_sid=session.call_sid,
        call_session_id=session.id,
        transcript_url="",  # local only for now
        summary=outcome.get("summary", ""),
        extracted_outcome=outcome,
        caller_name="",
        caller_purpose="",
    )

    # Notify owner
    if broker_send and agents:
        primary = agents.get_primary_user()
        if primary and primary.get("chat_id"):
            target = req.target_name if req else session.to_number
            success = "✅" if outcome.get("success") else "❌"
            details = outcome.get("key_details", {})
            detail_lines = "\n".join(
                f"  • {k}: {v}" for k, v in details.items()
            ) if details else ""

            text = (
                f"{success} Call completed: {target}\n\n"
                f"Goal: {goal}\n"
                f"Result: {outcome.get('summary', 'No summary')}\n"
            )
            if detail_lines:
                text += f"\nDetails:\n{detail_lines}\n"
            if outcome.get("notes"):
                text += f"\nNotes: {outcome['notes']}"

            notify_agent = primary.get("default_agent") or "barsik"
            try:
                await broker_send(
                    notify_agent, "telegram",
                    str(primary["chat_id"]), text,
                )
            except Exception as e:
                _log(f"voice: failed to notify owner — {e}")

    _log(f"voice: finalization complete for {session.call_sid}")
