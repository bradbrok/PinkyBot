"""OpenClaw Gateway Protocol v4 — minimal viable gateway for PinkyBot.

This module lets the OpenClaw Android app (and CLI/web clients speaking the
OpenClaw Gateway WebSocket protocol) connect to PinkyBot and chat with a single
target agent (default: ``satoshi``) *without* running a separate OpenClaw
Gateway server.

Scope (minimal viable subset of protocol v4):

  * ``connect.challenge`` → ``connect`` handshake with protocol negotiation
    (min/maxProtocol must include 4) and a permissive "trusted path" auth model
    (the protocol's own ``allowInsecureAuth`` localhost/self-host exception).
    We do NOT verify device signatures — this is a single-user self-hosted
    bridge, and the app talks to it directly over the LAN/tunnel the owner
    controls. A shared bearer token can be REQUIRED via
    ``OPENCLAW_GATEWAY_TOKEN`` if the owner wants one.
  * ``hello-ok`` response with the schema-required fields
    (server/features/snapshot/auth/policy).
  * ``sessions.list`` / ``sessions.create`` so the app can populate its
    session picker.
  * ``chat.send`` / ``sessions.send`` → injected into the target agent's
    streaming session. The agent's reply is streamed back as a
    ``session.message`` event (v4 ``deltaText`` + cumulative ``message``) and a
    terminal ``res`` for the request.
  * ``chat.history`` / ``chat.abort`` / ``ping`` / ``health`` best-effort stubs.
  * Periodic ``tick`` keepalive so the client's tick-timeout watchdog
    (close code 4000) does not fire.

What is NOT implemented (out of scope for a text-chat bridge):
  * Real device-signature verification / device-token pairing lifecycle.
  * Node capabilities (camera/canvas/screen/voice), talk/real-time audio.
  * Approvals, config RPCs, updates, node/device pairing methods.
  * Multi-agent routing — everything routes to ONE target agent.

Wiring: :func:`set_dependencies` is called once from ``api.py`` with the live
broker, an ``ensure_session`` coroutine, the agent registry and config. The
outbound bridge (agent reply → WebSocket) is driven by
:func:`deliver_agent_reply`, which ``_broker_send`` calls when it sees
``platform == "openclaw"``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("pinky.openclaw")

# File handler so openclaw frames are visible even when the daemon's stdout/stderr
# is routed to a supervisor socket rather than a file. Installed lazily in
# set_dependencies() once the daemon path is known; falls back to a fixed path.
_log_fh: logging.FileHandler | None = None

# ── API keys for voice pipeline ──────────────────────────────────────────────

# Keys are read from the environment, never from a hardcoded fallback, and the
# lookup is repeated at call time rather than trusted once at import. The daemon
# normally loads ~/.pinkybot/.env (gitignored) in __main__._load_dotenv() before
# importing api.py, but not every restart path goes through that, so each
# resolver also reads the .env files directly as a fallback. Empty means the
# corresponding voice step raises and is skipped; text chat is unaffected.

_ENV_FILES = [
    os.path.expanduser("~/projects/dnd-ai-master/.env"),
    os.path.expanduser("~/.pinkybot/.env"),
]


def _load_env_key(name: str) -> str:
    """Resolve an API key from os.environ, falling back to reading the .env files."""
    key = os.getenv(name, "").strip()
    if key:
        return key
    prefix = f"{name}="
    for env_path in _ENV_FILES:
        try:
            with open(env_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line.startswith(prefix):
                        return _line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            pass
    return ""


def _load_openai_key() -> str:
    """Load OpenAI key from env or fallback env files."""
    return _load_env_key("OPENAI_API_KEY")


def _load_deepgram_key() -> str:
    """Load Deepgram key from env or fallback env files."""
    return _load_env_key("DEEPGRAM_API_KEY")


DEEPGRAM_API_KEY: str = _load_deepgram_key()
OPENAI_API_KEY: str = _load_openai_key()

PROTOCOL_VERSION = 4
# The app advertises a [minProtocol, maxProtocol] range; we accept as long as
# the range straddles our version.
SERVER_PROTOCOL = 4

# Policy advertised in hello-ok. Values mirror the upstream defaults so the
# client's buffering/tick logic behaves normally.
POLICY_MAX_PAYLOAD = 26_214_400          # 25 MiB
POLICY_MAX_BUFFERED_BYTES = 52_428_800   # 50 MiB
POLICY_TICK_INTERVAL_MS = 15_000

# Pre-connection frames are capped at 64 KiB by the spec; we enforce a generous
# ceiling to reject obviously-bogus frames before handshake.
PREAUTH_MAX_FRAME_BYTES = 64 * 1024


# ── Dependency injection (set once from api.py) ──────────────────────────────

_broker = None                # MessageBroker
_ensure_session = None        # async fn(agent_name, *, label) -> streaming session
_agents = None                # AgentRegistry
_target_agent = "satoshi"     # which agent OpenClaw clients talk to
_TransportSessionState = None  # enum for CONNECTED checks


def set_dependencies(
    *,
    broker,
    ensure_session,
    agents,
    transport_session_state,
    target_agent: str = "satoshi",
) -> None:
    """Inject live daemon collaborators. Called once during app setup."""
    global _broker, _ensure_session, _agents, _target_agent, _TransportSessionState, _log_fh
    _broker = broker
    _ensure_session = ensure_session
    _agents = agents
    _TransportSessionState = transport_session_state
    _target_agent = os.environ.get("OPENCLAW_TARGET_AGENT", target_agent)

    # Install a dedicated file handler so every openclaw frame is logged to a
    # readable file regardless of how the daemon's stdio is plumbed.
    if _log_fh is None:
        try:
            _log_dir = os.environ.get(
                "PINKY_LOG_DIR",
                os.path.join(os.path.expanduser("~"), ".pinkybot", "logs"),
            )
            os.makedirs(_log_dir, exist_ok=True)
            _log_fh = logging.FileHandler(os.path.join(_log_dir, "openclaw.log"))
            _log_fh.setLevel(logging.DEBUG)
            _log_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(_log_fh)
            log.setLevel(logging.DEBUG)
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"[openclaw] could not install file handler: {exc}", file=sys.stderr)

    log.info("openclaw: gateway wired (target agent=%s)", _target_agent)


# ── Session registry ─────────────────────────────────────────────────────────
#
# Each OpenClaw sessionKey maps to a live WS connection + an outbound queue.
# When the target agent replies, _broker_send calls deliver_agent_reply() with
# the sessionKey (carried as chat_id); we push the text onto the queue and the
# per-request forwarder turns it into a session.message event + terminal res.


@dataclass
class _OpenClawSession:
    session_key: str
    ws: WebSocket
    role: str = "operator"   # "operator" or "node"
    device_id: str = ""      # device.id from the connect frame (stable per phone)
    # Outbound replies from the agent, keyed by nothing (single in-flight turn
    # per session is enough for a chat UI). Each item is the full reply text.
    replies: asyncio.Queue = field(default_factory=asyncio.Queue)
    created_at: float = field(default_factory=time.time)
    title: str = "Satoshi"
    # In-memory conversation history for chat.history responses.
    # Each entry: {role, content:[{type,text}], timestamp, idempotencyKey}
    # The app calls chat.history immediately after receiving a chat event to
    # sync its local state; returning empty [] causes it to clear the chat UI.
    messages: list = field(default_factory=list)


# session_key -> _OpenClawSession
_sessions: dict[str, "_OpenClawSession"] = {}


@dataclass
class _TalkSession:
    """An in-progress voice session (talk.session.create → send → end)."""
    talk_key: str            # unique key for this voice session (from params.sessionKey)
    session_key: str         # parent _OpenClawSession.session_key
    mode: str = "realtime"
    audio_chunks: list[bytes] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    done: bool = False       # True once _process_talk_session has been kicked off


# talk_key -> _TalkSession
_talk_sessions: dict[str, "_TalkSession"] = {}

# Live node registry: session_key -> node info dict.
# Populated when a client connects with role="node" and sends node.presence.alive.
# Used to answer node.list so the operator sees real connected nodes.
_node_registry: dict[str, dict] = {}

# Node WebSocket map: session_key -> WebSocket.
# Enables server-initiated device method calls (camera.snap, sms.send, etc.)
_node_ws_map: dict[str, WebSocket] = {}

# Pending server→node device requests: req_id -> asyncio.Future
_device_pending: dict[str, "asyncio.Future[dict]"] = {}

# Device capability methods that should be forwarded to the connected Android node.
_NODE_METHOD_PREFIXES: tuple[str, ...] = (
    "camera.", "canvas.", "sms.", "location.", "contacts.", "calendar.",
    "photos.", "motion.", "notifications.", "device.", "callLog.",
    "voicewake.", "system.", "talk.ptt.",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _forward_to_node(method: str, params: dict, timeout: float = 15.0) -> dict:
    """Forward a device method call to the connected Android node and await the response.

    Server sends {"req": method, "id": req_id, "params": params} to the Android's WS.
    Android executes (camera.snap, sms.send, location.get, …) and replies with a
    standard res frame.  The future is resolved in the main message loop when the
    matching res arrives.

    Raises:
        ValueError  — no node connected (open OpenClaw on the phone)
        asyncio.TimeoutError — node took too long
        Exception   — node returned an error payload
    """
    # Prefer a session that has formally registered via node.event;
    # fall back to any ws that called set-up (role=node implied).
    node_sk = next((sk for sk in _node_ws_map if sk in _node_registry), None)
    if node_sk is None:
        node_sk = next(iter(_node_ws_map), None)
    if node_sk is None:
        raise ValueError("No Android node connected — open OpenClaw app on your device")

    node_ws = _node_ws_map[node_sk]
    req_id = f"srv-{uuid.uuid4().hex}"
    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict] = loop.create_future()
    _device_pending[req_id] = future

    try:
        frame = json.dumps({"req": method, "id": req_id, "params": params or {}})
        log.info("openclaw: → node method=%r req=%s node=%s", method, req_id[:8], node_sk[:12])
        await node_ws.send_text(frame)
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("openclaw: device method %r timed out (%.1fs)", method, timeout)
        raise
    finally:
        _device_pending.pop(req_id, None)


def _ts_header() -> str:
    """Return a '[openclaw | dm | ...]' style timestamp for the agent prompt."""
    try:
        tz = _agents.get_default_timezone() if _agents else "UTC"
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz)).strftime(f"%Y-%m-%d %H:%M:%S {tz}")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Outbound bridge: agent reply → WebSocket ─────────────────────────────────


def has_session(session_key: str) -> bool:
    """True if an OpenClaw client is connected for this session key."""
    return session_key in _sessions


async def _push_node_connected(node: dict) -> None:
    """Push a node.connected event to every connected operator session.

    Called when (a) a new operator connects (so it sees existing nodes) and
    (b) when a node registers, so connected operators get a live update.
    This is what flips the Android diagnostic from 'Ready: no' to 'Ready: yes'.
    """
    event_payload = {
        "node": node,
        "ts": _now_ms(),
    }
    for sess in list(_sessions.values()):
        if sess.role == "operator":
            try:
                await _send_event(sess.ws, "node.connected", event_payload)
            except Exception:  # noqa: BLE001
                pass


async def deliver_agent_reply(session_key: str, content: str) -> bool:
    """Route an agent reply (from _broker_send) to the matching OpenClaw client.

    Returns True if a session was found and the reply enqueued. Empty replies
    (routed-turn no-ops) are ignored so the client isn't spammed with blanks.

    If the exact session_key isn't found (can happen when the broker uses the
    Telegram chat_id as routing key due to session-context bleed), we fall back
    to broadcasting to ALL operator sessions that have a chat turn in flight.
    This ensures replies always reach the connected app.
    """
    if not content or not content.strip():
        return True  # swallow empty routed-turn callbacks

    sess = _sessions.get(session_key)
    log.info(
        "openclaw: deliver_agent_reply key=%r found=%s content_len=%d",
        session_key[:16] if session_key else "",
        sess is not None,
        len(content) if content else 0,
    )

    if sess is not None:
        await sess.replies.put(content)
        log.info("openclaw: reply enqueued for session=%s", sess.session_key[:16])
        return True

    # Fallback: key not found (routing mismatch). Try all operator sessions.
    operator_sessions = [s for s in _sessions.values() if s.role == "operator"]
    if not operator_sessions:
        log.warning(
            "openclaw: deliver_agent_reply — no session for key=%r and no operator sessions (known: %s)",
            session_key[:16] if session_key else "",
            list(_sessions.keys()),
        )
        return False

    log.warning(
        "openclaw: deliver_agent_reply — key=%r not found, broadcasting to %d operator session(s) (routing mismatch fallback)",
        session_key[:16] if session_key else "",
        len(operator_sessions),
    )
    for op_sess in operator_sessions:
        await op_sess.replies.put(content)
        log.info("openclaw: reply enqueued (fallback) for session=%s", op_sess.session_key[:16])
    return True


# ── Voice pipeline helpers ───────────────────────────────────────────────────


def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    """Wrap raw PCM bytes in a WAV (RIFF) container.

    OpenClaw gateway-relay sends raw PCM audio (linear16, 24000 Hz, mono).
    Default sample_rate updated to 24000 to match OpenClaw Android's appendAudio format.
    Deepgram auto-detects format from the WAV header, so this avoids the
    Content-Type mismatch that causes a 400 Bad Request when sending raw PCM
    with Content-Type: audio/webm.
    """
    import struct
    data_size = len(pcm_data)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,   # total file size - 8 bytes
        b"WAVE",
        b"fmt ",
        16,               # fmt chunk size
        1,                # PCM format
        channels,
        sample_rate,
        sample_rate * channels * bits // 8,  # byte rate
        channels * bits // 8,                # block align
        bits,
        b"data",
        data_size,
    )
    return header + pcm_data


async def _stt_deepgram(audio_data: bytes, language: str = "it") -> str:
    """Transcribe audio via Deepgram REST API. Returns transcript text.

    Supports Italian (default) and any language Deepgram nova-3 covers.
    Audio is wrapped in WAV so Deepgram can auto-detect PCM format from header.
    The OpenClaw gateway-relay sends raw linear16 PCM at 16000 Hz, mono.
    """
    key = DEEPGRAM_API_KEY or _load_deepgram_key()
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY not configured")
    # WAV container: Deepgram reads sample_rate/encoding from the RIFF header.
    # Do NOT include encoding/sample_rate/channels in the URL when sending WAV —
    # those params tell Deepgram to treat the body as raw PCM (no container), which
    # would break because the WAV header bytes would look like garbage to the decoder.
    url = (
        f"https://api.deepgram.com/v1/listen"
        f"?model=nova-3&language={language}&smart_format=true&punctuate=true"
    )
    headers = {
        "Authorization": f"Token {key}",
        "Content-Type": "audio/wav",
    }
    wav_data = _pcm_to_wav(audio_data)
    log.debug("openclaw: STT sending %.1f KB WAV (%d raw PCM bytes)", len(wav_data) / 1024, len(audio_data))
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, content=wav_data, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    channels = data.get("results", {}).get("channels", [])
    if channels:
        alts = channels[0].get("alternatives", [])
        if alts:
            return alts[0].get("transcript", "").strip()
    return ""


async def _tts_openai(text: str, voice: str = "onyx") -> bytes:
    """Synthesize speech via OpenAI TTS API. Returns raw PCM bytes (16-bit signed LE, 24kHz mono).

    Uses tts-1 (fast, low-latency) with the 'onyx' voice (deep, clear Italian).
    Output format is 'pcm' (raw linear16 24kHz mono) so it can be sent directly
    to the OpenClaw app as a talk.event {type:"audio", audioBase64:...} payload
    without any container conversion.
    Falls back to raising RuntimeError so callers can gracefully skip audio.
    """
    key = OPENAI_API_KEY or _load_openai_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    body = {"model": "tts-1", "input": text, "voice": voice, "response_format": "pcm"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/speech",
            json=body,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.content


async def _tts_openai_stream(text: str, voice: str = "onyx", chunk_size: int = 8192):
    """Stream raw PCM chunks from OpenAI TTS API (async generator).

    Yields bytes chunks as they arrive from the API — the first chunk is available
    within ~300-500ms, dramatically reducing the latency before the app starts playing.
    Each chunk is raw 16-bit signed LE PCM at 24kHz mono (no WAV header).
    The OpenClaw app's playRealtimeAudio() expects raw PCM and queues chunks for
    sequential playback via AudioTrack in STREAM mode.
    """
    key = OPENAI_API_KEY or _load_openai_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    body = {"model": "tts-1", "input": text, "voice": voice, "response_format": "pcm"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream(
            "POST",
            "https://api.openai.com/v1/audio/speech",
            json=body,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(chunk_size):
                if chunk:
                    yield chunk


async def _query_agent(sess: "_OpenClawSession", text: str, timeout: float = 295.0) -> str:
    """Send text to the target agent and return the plain-text reply.

    Shared by both the text-chat path (_route_chat) and the voice pipeline
    (_process_talk_session). Returns empty string on failure.
    """
    if _ensure_session is None or _broker is None:
        return ""
    try:
        streaming = await _ensure_session(_target_agent, label="openclaw")
    except Exception as e:  # noqa: BLE001
        log.warning("openclaw: _query_agent ensure_session failed: %s", e)
        return ""
    if streaming is None or (
        _TransportSessionState is not None
        and streaming.state != _TransportSessionState.CONNECTED
    ):
        return ""
    # Drain stale replies from any prior turn.
    while not sess.replies.empty():
        try:
            sess.replies.get_nowait()
        except asyncio.QueueEmpty:
            break
    prompt = (
        f"[openclaw | voice | App | openclaw | {_ts_header()}]\n"
        f"SYSTEM: You are replying via the OpenClaw app voice mode (NOT Telegram). "
        f"Respond with plain text only — do NOT call mcp__pinky-messaging__ tools. "
        f"Keep your reply concise for voice (2-3 sentences). "
        f"Your reply is automatically converted to speech and delivered to the app.\n\n"
        f"{text}"
    )
    try:
        await streaming.send(prompt, platform="openclaw", chat_id=sess.session_key)
    except Exception as e:  # noqa: BLE001
        log.warning("openclaw: _query_agent streaming.send failed: %s", e)
        return ""
    try:
        reply = await asyncio.wait_for(sess.replies.get(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("openclaw: _query_agent timeout after %.0fs", timeout)
        return ""
    import re as _re
    return _re.sub(r"^\[skills:[^\]]*\]\n+", "", reply).strip()


async def _process_talk_session(
    ws: WebSocket, sess: "_OpenClawSession", talk_sess: "_TalkSession"
) -> None:
    """Full voice pipeline: STT → agent → TTS → talk.event wrapper events.

    Called as an asyncio.create_task() when the final audio chunk arrives.

    Protocol note (from OpenClaw Android ChatController + TalkModeManager source):
    - All voice events must be wrapped as: event="talk.event", payload={type:..., relaySessionId:...}
    - Individual talk.session.* event names are NOT handled by the app.
    - Audio output must be raw PCM (16-bit signed LE, 24kHz mono) sent as
      talk.event {type:"audio", audioBase64:"..."} — NOT MP3.
    - STT input is also PCM 24kHz mono from the app's appendAudio calls.
    """
    tkey = talk_sess.talk_key

    def _talk_event(ev_type: str, extra: dict | None = None) -> None:
        """Helper: build a talk.event payload. Returns a coroutine."""
        payload: dict = {"relaySessionId": tkey, "type": ev_type, "ts": _now_ms()}
        if extra:
            payload.update(extra)
        return _send_event(ws, "talk.event", payload)

    # ── Concatenate all received audio chunks ────────────────────────────────
    audio_data = b"".join(talk_sess.audio_chunks)
    if not audio_data:
        await _talk_event("error", {"message": "no audio received"})
        return

    # ── STT ──────────────────────────────────────────────────────────────────
    try:
        transcript = await _stt_deepgram(audio_data)
        log.info("openclaw: voice STT transcript=%r (%.1f KB)", transcript[:80], len(audio_data) / 1024)
    except Exception as e:  # noqa: BLE001
        log.warning("openclaw: STT failed: %s", e)
        await _talk_event("error", {"message": f"STT failed: {e}"})
        _talk_sessions.pop(tkey, None)
        return

    if not transcript:
        await _talk_event("error", {"message": "could not transcribe audio — try again"})
        _talk_sessions.pop(tkey, None)
        return

    # Emit user transcript so the app shows what was said
    await _talk_event("transcript", {"role": "user", "text": transcript, "final": True})

    # ── Agent ────────────────────────────────────────────────────────────────
    agent_text = await _query_agent(sess, transcript)
    if not agent_text:
        await _talk_event("error", {"message": "agent did not respond"})
        _talk_sessions.pop(tkey, None)
        return

    # Emit assistant transcript (text of the reply, shown in UI)
    await _talk_event("transcript", {"role": "assistant", "text": agent_text, "final": True})

    # ── TTS (streaming) ──────────────────────────────────────────────────────
    # Stream raw PCM chunks from OpenAI TTS and forward each chunk immediately
    # as a talk.event {type:"audio"} to the app.  Streaming cuts first-audio
    # latency from ~11s (full response) to ~0.3-0.5s (first chunk), preventing
    # the app's auto-session-recreate from killing the audio before it starts.
    #
    # The OpenClaw Android app's playRealtimeAudio() passes bytes directly to
    # AudioTrack in STREAM mode — it expects raw 16-bit signed LE PCM at 24kHz
    # mono, NOT a WAV/RIFF container. Each chunk goes into an unlimited queue
    # and is played sequentially, so many small events are safe.
    try:
        total_bytes = 0
        chunk_count = 0
        async for pcm_chunk in _tts_openai_stream(agent_text):
            audio_b64 = base64.b64encode(pcm_chunk).decode("ascii")
            await _talk_event("audio", {"audioBase64": audio_b64})
            total_bytes += len(pcm_chunk)
            chunk_count += 1
        log.info(
            "openclaw: TTS streamed %d chunks, %.1f KB PCM total (24kHz mono 16-bit)",
            chunk_count,
            total_bytes / 1024,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("openclaw: TTS failed (transcript still delivered): %s", e)
        # Don't abort — user already received the text transcript above.

    # ── Done ─────────────────────────────────────────────────────────────────
    # NOTE: Do NOT send talk.event {type:"close"} from the server here.
    # The app auto-recreates the talk session with the SAME key immediately
    # after sending its own talk.session.close (within ~100ms).  If the server
    # also sends a close event (with the same relaySessionId), the app mistakes
    # it for a close on the NEW session and kills it — cancelling audio playback
    # before it can start.  The app manages its own session lifecycle; the server
    # only needs to deliver audio + transcript and then stay silent.
    _talk_sessions.pop(tkey, None)


# ── Talk method handlers ──────────────────────────────────────────────────────


async def _handle_talk_session_create(
    ws: WebSocket, sess: "_OpenClawSession", req_id: str, params: dict
) -> None:
    """Handle talk.session.create — initialise a voice session."""
    talk_key = str(params.get("sessionKey") or uuid.uuid4().hex)
    mode = str(params.get("mode") or "realtime")
    talk_sess = _TalkSession(talk_key=talk_key, session_key=sess.session_key, mode=mode)
    _talk_sessions[talk_key] = talk_sess
    log.info("openclaw: talk session created key=%s session=%s", talk_key[:24], sess.session_key[:16])
    # relaySessionId is the primary field OpenClaw Android reads from the RPC response
    # (TalkModeManager.kt: root?.get("relaySessionId") ?: root?.get("sessionId")).
    # Return all aliases so older app versions also work.
    await _send_res(ws, req_id, {
        "ok": True,
        "relaySessionId": talk_key,   # primary (new protocol)
        "talkSessionKey": talk_key,   # legacy alias
        "sessionId": talk_key,        # fallback alias
        "id": talk_key,
        "mode": mode,
        "ts": _now_ms(),
    })
    # Emit talk.event {type:"ready"} so the app transitions to listening state.
    await _send_event(ws, "talk.event", {
        "relaySessionId": talk_key,
        "type": "ready",
        "mode": mode,
        "ts": _now_ms(),
    })


async def _handle_talk_session_send(
    ws: WebSocket, sess: "_OpenClawSession", req_id: str, params: dict
) -> None:
    """Handle talk.session.send — receive a base64-encoded audio chunk."""
    talk_key = str(params.get("talkSessionKey") or params.get("sessionId") or params.get("sessionKey") or "")
    chunk_b64 = str(params.get("chunk") or params.get("audio") or params.get("audioBase64") or "")
    is_final = bool(params.get("final") or params.get("isFinal") or False)
    talk_sess = _talk_sessions.get(talk_key)
    if talk_sess is None:
        await _send_err(ws, req_id, "NOT_FOUND", f"talk session {talk_key!r} not found")
        return
    if chunk_b64:
        try:
            talk_sess.audio_chunks.append(base64.b64decode(chunk_b64))
        except Exception as e:  # noqa: BLE001
            log.warning("openclaw: bad audio chunk (key=%s): %s", talk_key[:16], e)
    await _send_res(ws, req_id, {"ok": True, "talkSessionKey": talk_key, "ts": _now_ms()})
    if is_final and not talk_sess.done:
        talk_sess.done = True
        asyncio.create_task(_process_talk_session(ws, sess, talk_sess))


async def _handle_talk_session_end(
    ws: WebSocket, _sess: "_OpenClawSession", req_id: str, params: dict
) -> None:
    """Handle talk.session.end — clean up an ended voice session."""
    talk_key = str(params.get("talkSessionKey") or params.get("sessionId") or params.get("sessionKey") or "")
    _talk_sessions.pop(talk_key, None)
    await _send_res(ws, req_id, {"ok": True, "talkSessionKey": talk_key, "ts": _now_ms()})


# ── WebSocket frame helpers ──────────────────────────────────────────────────


async def _send_json(ws: WebSocket, obj: dict) -> None:
    raw = json.dumps(obj, ensure_ascii=False)
    log.debug("openclaw: → outbound: %s", raw[:300])
    await ws.send_text(raw)


async def _send_event(ws: WebSocket, event: str, payload: dict, seq: int | None = None) -> None:
    frame = {"type": "event", "event": event, "payload": payload}
    if seq is not None:
        frame["seq"] = seq
    await _send_json(ws, frame)


async def _send_res(ws: WebSocket, req_id: str, payload: dict) -> None:
    await _send_json(ws, {"type": "res", "id": req_id, "ok": True, "payload": payload})


async def _send_err(ws: WebSocket, req_id: str, code: str, message: str, details: dict | None = None) -> None:
    err = {"code": code, "message": message}
    if details:
        err["details"] = details
    await _send_json(ws, {"type": "res", "id": req_id, "ok": False, "error": err})


# ── Handshake ────────────────────────────────────────────────────────────────


def _auth_ok(params: dict) -> tuple[bool, str]:
    """Validate auth for the connect request.

    Permissive by default (self-hosted trusted path). If OPENCLAW_GATEWAY_TOKEN
    is set, the client MUST present it as auth.token / auth.bootstrapToken /
    auth.password. Returns (ok, reason).
    """
    required = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if not required:
        return True, ""
    auth = params.get("auth") or {}
    presented = {
        str(auth.get("token") or "").strip(),
        str(auth.get("bootstrapToken") or "").strip(),
        str(auth.get("password") or "").strip(),
    }
    if required in presented:
        return True, ""
    return False, "invalid or missing gateway token"


def _protocol_ok(params: dict) -> bool:
    lo = params.get("minProtocol")
    hi = params.get("maxProtocol")
    # If unspecified, be lenient — some clients only send maxProtocol.
    try:
        lo = int(lo) if lo is not None else SERVER_PROTOCOL
        hi = int(hi) if hi is not None else SERVER_PROTOCOL
    except (TypeError, ValueError):
        return False
    return lo <= SERVER_PROTOCOL <= hi


def _node_entry() -> dict:
    """Canonical node descriptor for the target agent (Satoshi bridge)."""
    return {
        "nodeId": _target_agent,
        "name": _target_agent.capitalize(),
        "platform": "linux",
        "status": "online",
        "ready": True,
        "capabilities": ["chat", "sessions"],
    }


def _device_node_entry(device_id: str) -> dict:
    """Canonical node descriptor for the phone itself (identified by device_id).

    When the Android app checks node.list, it looks for its own device_id in
    the list to determine 'Ready: yes'. We include the phone as a node so the
    operator sees itself as connected.
    """
    return {
        "nodeId": device_id,
        "name": "Your Phone",
        "platform": "android",
        "status": "online",
        "ready": True,
        "capabilities": ["chat", "audio", "sessions"],
    }


def _hello_ok_payload(conn_id: str, scopes: list[str], role: str = "operator", device_id: str = "") -> dict:
    """Build the schema-required hello-ok payload.

    The snapshot.nodes (and matching presence entry) are populated immediately
    so the Android diagnostic shows 'Ready: yes' as soon as the handshake
    completes — without waiting for a nodes.list RPC or gateway.ready event.

    device_id: the stable device identifier from the connect frame (device.id).
    When present, the phone itself is included as a node so the app's "Ready"
    check (which looks for its own device in the node list) succeeds immediately.
    """
    server_version = "pinkybot-openclaw-bridge/1"
    node = _node_entry()
    nodes = [node]
    presence: dict = {
        _target_agent: {
            "nodeId": _target_agent,
            "status": "online",
            "ready": True,
            "ts": _now_ms(),
        }
    }
    # Include the phone itself as a node so the operator sees its own device.
    if device_id:
        dev_node = _device_node_entry(device_id)
        nodes.append(dev_node)
        presence[device_id] = {
            "nodeId": device_id,
            "status": "online",
            "ready": True,
            "ts": _now_ms(),
        }
    return {
        "type": "hello-ok",
        "protocol": PROTOCOL_VERSION,
        "server": {"version": server_version, "connId": conn_id},
        "features": {
            "methods": [
                "connect", "health", "ping",
                "nodes.list", "node.list", "gateway.status", "gateway.info",
                "sessions.list", "sessions.create", "sessions.send",
                "sessions.subscribe", "sessions.unsubscribe",
                "chat.send", "chat.history", "chat.abort",
                "talk.config", "talk.session.create", "talk.session.send",
                "talk.session.end", "talk.session.list", "talk.session.status",
            ],
            "events": [
                "connect.challenge", "gateway.ready", "node.connected",
                "chat", "session.message", "agent", "tick", "health",
                "talk.event",  # primary voice event wrapper (type: ready|transcript|audio|error|close)
            ],
        },
        # Include nodes and channels in the initial snapshot so the client's
        # "Ready" state is populated immediately at handshake time (before any
        # events or RPCs). An empty channels list causes "Ready: no".
        "snapshot": {
            "sessions": [],
            "nodes": nodes,
            "channels": [{
                "id": "chat",
                "type": "text",
                "name": "Satoshi Chat",
                "status": "active",
                "ready": True,
                "nodeId": _target_agent,
                "capabilities": ["text", "chat"],
            }],
            "presence": presence,
            "stateVersion": str(_now_ms()),
        },
        "auth": {
            # Echo back the role the client requested so it doesn't mismatch.
            "role": role,
            "scopes": scopes,
            # Echo a stable device token so the app can persist it and reconnect.
            "deviceToken": f"pinky-openclaw-{conn_id}",
            "issuedAtMs": _now_ms(),
        },
        "policy": {
            "maxPayload": POLICY_MAX_PAYLOAD,
            "maxBufferedBytes": POLICY_MAX_BUFFERED_BYTES,
            "tickIntervalMs": POLICY_TICK_INTERVAL_MS,
        },
    }


# ── Chat routing ─────────────────────────────────────────────────────────────


async def _route_chat(
    ws: WebSocket,
    req_id: str,
    sess: "_OpenClawSession",
    text: str,
    timeout_ms: int = 300_000,
    reply_session_key: str = "",
    run_id: str = "",
) -> None:
    """Inject `text` into the target agent and stream the reply back.

    We wait (bounded) for a reply on the session's queue, then emit it as a
    ``chat`` event (state="final") so the OpenClaw Android app can display it,
    followed by a terminal ``res`` with ``status="ok"`` which clears the app's
    pendingRun timer (preventing "Timed out waiting for a reply" errors).

    Protocol notes (from OpenClaw Android source ChatController.kt):
    - App tracks runs via ``runId`` (= ``idempotencyKey`` from client params).
    - ``session.message`` only carries session metadata (tokens, displayName).
    - Actual content must be a ``chat`` event: ``{state:"final", message:{role:"assistant",
      content:[{type:"text",text:"..."}]}, runId:"..."}``.
    - The ``res`` ACK must be ``{runId:"...", status:"ok"}``; status "ok" clears
      the 120-second pendingRunTimeout in the app.  Without it the app shows
      "Timed out waiting for a reply; try again or refresh."

    Uses a dedicated ``label="openclaw"`` session so OpenClaw chat never
    shares context with the main Telegram session (and avoids busy-session
    timeouts when the agent is mid-turn on another platform).

    timeout_ms: client-supplied timeout (from params.timeoutMs); we leave a
    2-second safety margin so our gateway reply arrives before the client cuts.
    """
    if _ensure_session is None or _broker is None:
        await _send_err(ws, req_id, "UNAVAILABLE", "gateway not fully initialised")
        return

    # OpenClaw gets its own session label so it never shares a busy Telegram
    # turn.  "openclaw" is stable across reconnects (label is the key).
    try:
        streaming = await _ensure_session(_target_agent, label="openclaw")
    except Exception as e:  # noqa: BLE001
        log.warning("openclaw: ensure_session failed: %s", e)
        streaming = None
    if streaming is None or (
        _TransportSessionState is not None
        and streaming.state != _TransportSessionState.CONNECTED
    ):
        await _send_err(
            ws, req_id, "UNAVAILABLE",
            f"agent '{_target_agent}' session could not be started",
        )
        return

    # Drain any stale replies from a previous aborted turn.
    while not sess.replies.empty():
        try:
            sess.replies.get_nowait()
        except asyncio.QueueEmpty:
            break

    # Inject with an [openclaw | ...] metadata header, matching how other
    # platforms format inbound messages. platform="openclaw" + chat_id=sessionKey
    # makes the agent's reply route back through _broker_send → deliver_agent_reply.
    #
    # IMPORTANT: the system note below is critical. Without it the agent calls
    # mcp__pinky-messaging__send(platform="telegram") which routes the reply to
    # Telegram instead of back through this WebSocket.  Plain-text output (no
    # messaging tool call) is the correct response path for OpenClaw turns.
    prompt = (
        f"[openclaw | dm | App | openclaw | {_ts_header()}]\n"
        f"SYSTEM: You are replying via the OpenClaw app (NOT Telegram). "
        f"Respond with plain text only — do NOT call mcp__pinky-messaging__ tools. "
        f"Your plain-text reply is automatically delivered back to the app.\n\n"
        f"{text}"
    )
    try:
        await streaming.send(prompt, platform="openclaw", chat_id=sess.session_key)
    except Exception as e:  # noqa: BLE001
        log.warning("openclaw: streaming.send failed: %s", e)
        await _send_err(ws, req_id, "INTERNAL", f"send failed: {e}")
        return

    # Use a generous fixed timeout regardless of the client's timeoutMs.
    # OpenClaw sends timeoutMs=30000 but PinkyBot session startup (cold start
    # after daemon restart) + agent thinking can easily exceed 30 s.  If we
    # honour the 30 s ceiling the reply arrives after the gateway already sent
    # a TIMEOUT error and the stale reply is silently drained on the next turn.
    # The WebSocket stays alive (tick every 15 s) so the client can wait 295 s.
    agent_timeout = 295.0  # deliberate: ignore app's timeoutMs, see comment above

    # Wait (bounded) for the agent's reply to arrive on the outbound queue.
    message_id = uuid.uuid4().hex
    try:
        reply = await asyncio.wait_for(sess.replies.get(), timeout=agent_timeout)
    except asyncio.TimeoutError:
        await _send_err(ws, req_id, "TIMEOUT", "agent did not reply in time")
        return

    # Use the sessionKey the client sent in chat.send params (e.g.
    # "agent:main:node-157572eed71a") so the app can match the reply to the
    # correct chat session it already has open. Fall back to sess.session_key
    # if the client didn't include one.
    event_session_key = reply_session_key or sess.session_key

    # Strip internal skill-routing prefix ([skills: ...]\n\n) before forwarding.
    import re as _re
    clean_reply = _re.sub(r"^\[skills:[^\]]*\]\n+", "", reply).strip()

    # Use run_id (= client's idempotencyKey) for correlation; fall back to a
    # fresh UUID so the payload always has a valid runId.
    effective_run_id = run_id or message_id

    now_ms = _now_ms()

    # Store both turns in the session's message history so chat.history
    # returns accurate data. The app calls chat.history immediately after
    # receiving the chat event; returning [] clears the visible chat UI.
    sess.messages.append({
        "role": "user",
        "content": [{"type": "text", "text": text}],
        "timestamp": now_ms - 1000,     # slightly before the assistant reply
        "idempotencyKey": effective_run_id,
    })
    sess.messages.append({
        "role": "assistant",
        "content": [{"type": "text", "text": clean_reply}],
        "timestamp": now_ms,
        "idempotencyKey": f"{effective_run_id}:assistant",
    })
    # Keep history bounded (last 100 messages, ~50 turns)
    if len(sess.messages) > 100:
        sess.messages = sess.messages[-100:]

    # 1. "chat" event delivers the content in the format the OpenClaw Android
    #    app expects (ChatController.handleChatEvent).  The app reads text from
    #    message.content[].text when type=="text"; it does NOT read deltaText.
    await _send_event(ws, "chat", {
        "sessionKey": event_session_key,
        "runId": effective_run_id,
        "state": "final",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": clean_reply}],
        },
        "ts": now_ms,
    })

    # 2. Terminal RPC response.  status="ok" is required to clear the app's
    #    pendingRun timer (armPendingRunTimeout, 120 s) — without it the app
    #    shows "Timed out waiting for a reply; try again or refresh."
    await _send_res(ws, req_id, {
        "runId": effective_run_id,
        "status": "ok",
    })


# ── Method dispatch (post-handshake) ─────────────────────────────────────────


async def _dispatch(ws: WebSocket, sess: "_OpenClawSession", req_id: str, method: str, params: dict) -> None:  # noqa: C901 (complex but readable)
    if method in ("ping", "health"):
        await _send_res(ws, req_id, {"ok": True, "ts": _now_ms(), "status": "healthy"})
        return

    # ── Session methods ───────────────────────────────────────────────────────

    if method == "sessions.list":
        await _send_res(ws, req_id, {"sessions": [{
            "sessionKey": sess.session_key,
            "title": sess.title,
            "agent": _target_agent,
            "createdAtMs": int(sess.created_at * 1000),
        }]})
        return

    if method == "sessions.create":
        title = (params.get("title") or sess.title) if isinstance(params, dict) else sess.title
        sess.title = title
        await _send_res(ws, req_id, {
            "sessionKey": sess.session_key,
            "title": sess.title,
            "agent": _target_agent,
        })
        return

    if method in ("sessions.subscribe", "sessions.unsubscribe"):
        await _send_res(ws, req_id, {"sessionKey": sess.session_key, "subscribed": method.endswith("subscribe")})
        return

    if method == "chat.history":
        # The app reads "sessionId" (not "sessionKey") from the response root,
        # plus "thinkingLevel", "messages[]", and "sessionInfo".
        # Returning messages:[] clears the visible chat UI — always return
        # the session's in-memory history so the app can reconcile messages.
        req_sk = (params.get("sessionKey") if isinstance(params, dict) else None) or sess.session_key
        await _send_res(ws, req_id, {
            "sessionId": req_sk,          # primary field app reads
            "sessionKey": req_sk,         # compatibility alias
            "thinkingLevel": "off",
            "messages": list(sess.messages),
            "sessionInfo": {
                "key": req_sk,
                "displayName": sess.title,
                "updatedAt": _now_ms(),
                "totalTokens": 0,
                "totalTokensFresh": False,
                "contextTokens": 0,
            },
        })
        return

    if method == "chat.abort":
        req_sk = (params.get("sessionKey") if isinstance(params, dict) else None) or sess.session_key
        await _send_res(ws, req_id, {"aborted": True, "sessionKey": req_sk})
        return

    # ── Node/gateway methods ──────────────────────────────────────────────────

    if method in ("node.list", "nodes.list"):
        # The Android app queries node.list (singular) to determine Ready state.
        # It looks for its own device_id in the list. Return:
        # 1. All real registered nodes (keyed by device_id via node.event)
        # 2. The phone itself (if sess.device_id is known, inject directly)
        # 3. The Satoshi bridge node (always present)
        real = list(_node_registry.values())
        ids = {n.get("nodeId") for n in real}
        # Include the operator's own phone as a node so "Ready" check passes.
        if sess.device_id and sess.device_id not in ids:
            real.append(_device_node_entry(sess.device_id))
            ids.add(sess.device_id)
        bridge = _node_entry()
        if bridge["nodeId"] not in ids:
            real.append(bridge)
        await _send_res(ws, req_id, {"nodes": real})
        return

    if method in ("gateway.status", "gateway.info"):
        n_online = len(_node_registry) + 1  # +1 for satoshi bridge
        await _send_res(ws, req_id, {
            "status": "ready",
            "ready": True,
            "nodesOnline": n_online,
            "version": "pinkybot-openclaw-bridge/1",
            "ts": _now_ms(),
        })
        return

    if method == "node.event":
        # The Android app connects as role=node and sends node.presence.alive to
        # announce itself. Acknowledge and update the node registry.
        if isinstance(params, dict):
            event_name = params.get("event", "")
            payload_json = params.get("payloadJSON") or params.get("payload") or "{}"
            try:
                payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
            except (ValueError, TypeError):
                payload = {}
            if "presence" in event_name or "alive" in event_name:
                # Use device_id (from connect params) as nodeId so the operator
                # session can find its own device when it calls node.list.
                node_id = sess.device_id or sess.session_key
                node_info = {
                    "nodeId": node_id,
                    "name": payload.get("displayName", "Android Node"),
                    "platform": payload.get("deviceFamily", "android").lower(),
                    "status": "online",
                    "ready": True,
                    "capabilities": ["chat", "audio", "sessions"],
                    "model": payload.get("modelIdentifier", ""),
                    "version": payload.get("version", ""),
                    "ts": _now_ms(),
                }
                _node_registry[sess.session_key] = node_info
                # Store WS so server can send device method requests back to the node.
                _node_ws_map[sess.session_key] = ws
                log.info(
                    "openclaw: node registered (session=%s node_id=%r name=%r)",
                    sess.session_key,
                    node_id,
                    node_info["name"],
                )
                # Notify all connected operator sessions that a new node is online.
                asyncio.create_task(_push_node_connected(node_info))
        await _send_res(ws, req_id, {"ok": True, "ts": _now_ms()})
        return

    # ── Diagnostic stubs (operator queries after connect) ────────────────────
    # The OpenClaw Android app queries these on every connect to populate its
    # diagnostic panel. Return minimal valid responses so the panel shows green.

    if method == "agents.list":
        await _send_res(ws, req_id, {"agents": [{
            "id": _target_agent,
            "name": _target_agent.capitalize(),
            "status": "online",
            "description": "PinkyBot bridge agent",
        }]})
        return

    if method == "models.list":
        await _send_res(ws, req_id, {"models": [
            {"id": "claude-sonnet", "name": "Claude Sonnet", "provider": "anthropic", "active": True},
        ]})
        return

    if method == "config.get":
        await _send_res(ws, req_id, {"config": {
            "targetAgent": _target_agent,
            "bridge": "pinkybot-openclaw",
        }})
        return

    if method == "exec.approval.list":
        await _send_res(ws, req_id, {"approvals": []})
        return

    if method == "cron.status":
        await _send_res(ws, req_id, {"enabled": False, "jobs": [], "ok": True})
        return

    if method == "usage.status":
        await _send_res(ws, req_id, {"ok": True, "status": "ok", "ts": _now_ms()})
        return

    if method == "skills.status":
        await _send_res(ws, req_id, {"skills": [], "ok": True})
        return

    if method == "channels.status":
        # Return an active text channel so the app sees at least one ready channel.
        # An empty channels list causes the OpenClaw app to show "Ready: no" and
        # lock the UI on the "Gateway Recovery" screen, preventing any chat access.
        chat_channel = {
            "id": "chat",
            "type": "text",
            "name": "Satoshi Chat",
            "status": "active",
            "ready": True,
            "nodeId": _target_agent,
            "capabilities": ["text", "chat"],
        }
        await _send_res(ws, req_id, {
            "ok": True,
            "ready": True,
            "channels": [chat_channel],
            "ts": _now_ms(),
        })
        return

    if method == "doctor.memory.status":
        await _send_res(ws, req_id, {"ok": True, "status": "healthy", "ts": _now_ms()})
        return

    if method == "doctor.memory.dreamDiary":
        await _send_res(ws, req_id, {"ok": True, "entries": []})
        return

    if method == "models.authStatus":
        await _send_res(ws, req_id, {"ok": True, "authenticated": True, "provider": "anthropic"})
        return

    if method == "cron.list":
        await _send_res(ws, req_id, {"ok": True, "jobs": []})
        return

    if method == "device.pair.list":
        await _send_res(ws, req_id, {"ok": True, "devices": []})
        return

    if method == "logs.tail":
        await _send_res(ws, req_id, {"lines": [], "ok": True})
        return

    # ── Chat ─────────────────────────────────────────────────────────────────

    if method in ("chat.send", "sessions.send"):
        text = ""
        timeout_ms = 300_000
        reply_session_key = ""
        idempotency_key = ""
        if isinstance(params, dict):
            text = (
                params.get("text")
                or params.get("message")
                or params.get("content")
                or ""
            )
            if isinstance(text, dict):
                text = text.get("text", "")
            raw_timeout = params.get("timeoutMs")
            if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
                timeout_ms = int(raw_timeout)
            # Echo back the sessionKey the app sent so the chat event and res
            # carry the same sessionKey the app's ChatController expects.
            reply_session_key = str(params.get("sessionKey") or "")
            # idempotencyKey is used as runId — the app tracks pending runs by
            # this ID and clears the 120-second timeout when it sees status="ok".
            idempotency_key = str(params.get("idempotencyKey") or "")
        text = str(text).strip()
        if not text:
            await _send_err(ws, req_id, "INVALID_ARGUMENT", "empty message")
            return
        await _route_chat(
            ws, req_id, sess, text,
            timeout_ms=timeout_ms,
            reply_session_key=reply_session_key,
            run_id=idempotency_key,
        )
        return

    # ── Talk / voice ─────────────────────────────────────────────────────────
    # talk.config: advertise full voice capabilities (Deepgram STT + OpenAI TTS).
    if method == "talk.config":
        await _send_res(ws, req_id, {
            "ok": True,
            "supported": True,
            "capabilities": {
                "modes": ["realtime", "transcription"],
                "transports": ["gateway-relay"],
                "brains": ["agent-consult"],
                "stt": "deepgram-nova-3",
                "tts": "openai-tts-1",
                "ttsVoice": "onyx",
                "language": "it",
            },
            "ts": _now_ms(),
        })
        return

    # talk.session.create — start a voice session (user taps mic button).
    if method == "talk.session.create":
        await _handle_talk_session_create(ws, sess, req_id, params if isinstance(params, dict) else {})
        return

    # talk.session.appendAudio — v4 protocol: receive base64 PCM/audio chunk.
    # Primary method used by OpenClaw Android app for gateway-relay audio input.
    if method == "talk.session.appendAudio":
        await _handle_talk_session_send(ws, sess, req_id, params if isinstance(params, dict) else {})
        return

    # talk.session.send — legacy alias for appendAudio.
    if method == "talk.session.send":
        await _handle_talk_session_send(ws, sess, req_id, params if isinstance(params, dict) else {})
        return

    # talk.session.startTurn — v4 turn lifecycle: acknowledge start of a new turn.
    if method == "talk.session.startTurn":
        p = params if isinstance(params, dict) else {}
        talk_key = str(p.get("talkSessionKey") or p.get("sessionId") or p.get("sessionKey") or "")
        log.info("openclaw: talk.session.startTurn key=%s session=%s", talk_key[:24], sess.session_key[:16])
        # Reset audio buffer so the turn starts fresh.
        talk_sess = _talk_sessions.get(talk_key)
        if talk_sess:
            talk_sess.audio_chunks.clear()
            talk_sess.done = False
        await _send_res(ws, req_id, {"ok": True, "talkSessionKey": talk_key, "ts": _now_ms()})
        return

    # talk.session.endTurn — v4 turn lifecycle: trigger STT→agent→TTS pipeline.
    if method == "talk.session.endTurn":
        p = params if isinstance(params, dict) else {}
        talk_key = str(p.get("talkSessionKey") or p.get("sessionId") or p.get("sessionKey") or "")
        talk_sess = _talk_sessions.get(talk_key)
        log.info(
            "openclaw: talk.session.endTurn key=%s audio_chunks=%d session=%s",
            talk_key[:24],
            len(talk_sess.audio_chunks) if talk_sess else 0,
            sess.session_key[:16],
        )
        if talk_sess and not talk_sess.done:
            talk_sess.done = True
            asyncio.create_task(_process_talk_session(ws, sess, talk_sess))
        await _send_res(ws, req_id, {"ok": True, "talkSessionKey": talk_key, "ts": _now_ms()})
        return

    # talk.session.cancelTurn — v4: cancel current audio input without processing.
    if method == "talk.session.cancelTurn":
        p = params if isinstance(params, dict) else {}
        talk_key = str(p.get("talkSessionKey") or p.get("sessionId") or p.get("sessionKey") or "")
        talk_sess = _talk_sessions.get(talk_key)
        if talk_sess:
            talk_sess.audio_chunks.clear()
            talk_sess.done = False
        log.info("openclaw: talk.session.cancelTurn key=%s", talk_key[:24])
        await _send_res(ws, req_id, {"ok": True, "talkSessionKey": talk_key, "ts": _now_ms()})
        return

    # talk.session.close — v4: close/cleanup a talk session.
    # If audio has been accumulated (but no endTurn was called), trigger STT pipeline first.
    if method == "talk.session.close":
        p = params if isinstance(params, dict) else {}
        talk_key = str(p.get("talkSessionKey") or p.get("sessionId") or p.get("sessionKey") or "")
        talk_sess = _talk_sessions.get(talk_key)
        if talk_sess and talk_sess.audio_chunks and not talk_sess.done:
            talk_sess.done = True
            log.info(
                "openclaw: talk.session.close — triggering STT pipeline (chunks=%d key=%s)",
                len(talk_sess.audio_chunks), talk_key[:24],
            )
            asyncio.create_task(_process_talk_session(ws, sess, talk_sess))
        await _handle_talk_session_end(ws, sess, req_id, p)
        return

    # talk.session.end — legacy: client signals end of voice turn.
    if method == "talk.session.end":
        await _handle_talk_session_end(ws, sess, req_id, params if isinstance(params, dict) else {})
        return

    # talk.session.list / status — lightweight informational stubs.
    if method in ("talk.session.list", "talk.session.status"):
        active = [
            {"talkSessionKey": k, "mode": v.mode, "createdAtMs": int(v.created_at * 1000)}
            for k, v in _talk_sessions.items()
            if v.session_key == sess.session_key
        ]
        await _send_res(ws, req_id, {"ok": True, "sessions": active, "ts": _now_ms()})
        return

    # voice.session.* aliases (some app versions use this namespace).
    if method in (
        "voice.session.create", "voice.session.send", "voice.session.end",
        "voice.session.appendAudio", "voice.session.startTurn",
        "voice.session.endTurn", "voice.session.cancelTurn", "voice.session.close",
    ):
        mapped = method.replace("voice.", "talk.")
        log.debug("openclaw: remapping %r → %r", method, mapped)
        await _dispatch(ws, sess, req_id, mapped, params)
        return

    # ── Device capability relay ───────────────────────────────────────────────
    # Forward Android device methods (camera, SMS, location, contacts, etc.)
    # to the connected OpenClaw node (phone) and relay the response back.
    if any(method.startswith(prefix) for prefix in _NODE_METHOD_PREFIXES):
        try:
            result = await _forward_to_node(method, params if isinstance(params, dict) else {})
            await _send_res(ws, req_id, result)
        except ValueError as exc:
            await _send_err(ws, req_id, "NODE_UNAVAILABLE", str(exc))
        except asyncio.TimeoutError:
            await _send_err(ws, req_id, "TIMEOUT", f"{method} timed out waiting for Android")
        except Exception as exc:  # noqa: BLE001
            await _send_err(ws, req_id, "DEVICE_ERROR", str(exc))
        return

    # Unknown method — log it and respond so the client doesn't hang.
    log.debug("openclaw: METHOD_NOT_FOUND method=%r session=%s", method, sess.session_key)
    await _send_err(ws, req_id, "METHOD_NOT_FOUND", f"unsupported method: {method}")


# ── Tick keepalive ───────────────────────────────────────────────────────────


async def _tick_loop(ws: WebSocket) -> None:
    seq = 0
    try:
        while True:
            await asyncio.sleep(POLICY_TICK_INTERVAL_MS / 1000.0)
            seq += 1
            await _send_event(ws, "tick", {"ts": _now_ms()}, seq=seq)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        return
    except Exception:  # noqa: BLE001
        return


# ── Top-level connection handler ─────────────────────────────────────────────


async def handle_connection(ws: WebSocket) -> None:
    """Handle one OpenClaw Gateway WebSocket connection end-to-end."""
    await ws.accept()
    conn_id = uuid.uuid4().hex
    nonce = uuid.uuid4().hex

    # Phase 1: server sends the challenge immediately on open.
    try:
        await _send_event(ws, "connect.challenge", {"nonce": nonce, "ts": _now_ms()})
    except Exception:  # noqa: BLE001
        return

    # Phase 2: wait for the client's `connect` request (bounded, 15s per spec).
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=15.0)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        try:
            await ws.close(code=4001)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        frame = json.loads(raw)
    except (ValueError, TypeError):
        try:
            await ws.close(code=4002)
        except Exception:  # noqa: BLE001
            pass
        return

    # OpenClaw protocol uses "req" as the method field name (not "method").
    # Support both for forward compatibility.
    method = frame.get("req") or frame.get("method")
    req_id = frame.get("id") or uuid.uuid4().hex
    params = frame.get("params") or {}
    if method != "connect":
        await _send_err(ws, req_id, "PROTOCOL_ERROR", "first frame must be connect")
        await ws.close(code=4003)
        return

    if not _protocol_ok(params):
        await _send_err(
            ws, req_id, "PROTOCOL_VERSION_UNSUPPORTED",
            f"gateway speaks protocol v{SERVER_PROTOCOL}",
            details={"serverProtocol": SERVER_PROTOCOL},
        )
        await ws.close(code=4004)
        return

    ok, reason = _auth_ok(params)
    if not ok:
        await _send_err(ws, req_id, "UNAUTHENTICATED", reason, details={"code": "DEVICE_AUTH_REQUIRED"})
        await ws.close(code=4005)
        return

    # Echo back the role the client requested (Android uses "node", web/CLI uses
    # "operator"). This is a self-hosted bridge so we accept any role.
    client_role = str(params.get("role") or "operator")
    if client_role == "node":
        scopes = ["node.connect", "node.read", "node.write", "chat.send", "sessions.list"]
    else:
        scopes = ["operator.read", "operator.write"]

    # Register WS immediately for any connecting node so device relay works even
    # before the app sends a formal node.event registration.
    # Android connects with role="node" or clientInfo.mode="node".
    _client_mode = str((params.get("clientInfo") or {}).get("mode") or "")
    if client_role == "node" or _client_mode == "node":
        # Will be overwritten later when node.event is received, but guarantees
        # the mapping exists for early device method calls.
        pass  # session_key not yet defined; we register below after sess creation

    # Extract stable device_id from connect params — used as nodeId so the
    # operator can identify its own device in node.list (flips Ready to yes).
    device_id = str((params.get("device") or {}).get("id") or "")

    # DEBUG: log the connect frame so we can diagnose Android handshake issues.
    log.info(
        "openclaw: connect frame — role=%r proto=[%s,%s] client=%r device_id=%r",
        client_role,
        params.get("minProtocol"),
        params.get("maxProtocol"),
        (params.get("client") or {}).get("platform"),
        device_id[:16],
    )

    await _send_res(ws, req_id, _hello_ok_payload(conn_id, scopes, role=client_role, device_id=device_id))

    # Register the session. sessionKey defaults to the connection id but honours
    # a client-provided one so reconnects can resume.
    session_key = str(params.get("sessionKey") or conn_id)
    sess = _OpenClawSession(session_key=session_key, ws=ws, role=client_role, device_id=device_id)
    _sessions[session_key] = sess
    # Register WS immediately for node-role connections so device relay works
    # before the app sends a formal node.event registration frame.
    if client_role == "node" or _client_mode == "node":
        _node_ws_map[session_key] = ws
    log.info("openclaw: client connected role=%s device_id=%r (session=%s conn=%s)", client_role, device_id[:16], session_key, conn_id)

    # Build the full node list for events (includes device + bridge).
    extra_nodes: list[dict] = list(_node_registry.values())
    extra_ids = {n.get("nodeId") for n in extra_nodes}
    if device_id and device_id not in extra_ids:
        extra_nodes.append(_device_node_entry(device_id))
        extra_ids.add(device_id)
    bridge = _node_entry()
    if bridge["nodeId"] not in extra_ids:
        extra_nodes.append(bridge)

    # Push gateway.ready so the app knows the gateway is alive.
    try:
        await _send_event(ws, "gateway.ready", {
            "ready": True,
            "nodesOnline": len(extra_nodes),
            "nodes": extra_nodes,
            "ts": _now_ms(),
        })
    except Exception:  # noqa: BLE001
        pass

    # If this is an operator, push node.connected for every available node so
    # the Android diagnostic flips to "Ready: yes" immediately.
    if client_role == "operator":
        for node in extra_nodes:
            try:
                await _send_event(ws, "node.connected", {"node": node, "ts": _now_ms()})
            except Exception:  # noqa: BLE001
                pass

        # Pre-warm the dedicated "openclaw" agent session in the background so
        # it is CONNECTED and ready before the first chat.send arrives.
        # Cold-starting takes 10–30 s; doing it now (at operator connect time)
        # means chat messages won't time out waiting for session boot.
        if _ensure_session is not None:
            async def _prewarm_openclaw_session() -> None:
                try:
                    log.info("openclaw: pre-warming 'openclaw' session for %s", _target_agent)
                    await _ensure_session(_target_agent, label="openclaw")
                    log.info("openclaw: 'openclaw' session ready")
                except Exception as exc:  # noqa: BLE001
                    log.warning("openclaw: pre-warm failed: %s", exc)
            asyncio.create_task(_prewarm_openclaw_session())

    tick_task = asyncio.create_task(_tick_loop(ws))

    # Phase 3: message loop.
    try:
        while True:
            raw = await ws.receive_text()
            # Log every inbound frame (truncated) for diagnostic purposes.
            # This is the only reliable way to see what the Android app sends
            # since the daemon's stdout/stderr may go to a supervisor socket.
            log.debug("openclaw: ← inbound (session=%s): %s", session_key, raw[:400])
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            # Check if this is a response to a pending server→node device request.
            # These arrive when Android replies to a forwarded camera.snap, sms.send, etc.
            _msg_type = msg.get("type")
            _msg_id = msg.get("id")
            if _msg_type == "res" and _msg_id and _msg_id in _device_pending:
                _fut = _device_pending.pop(_msg_id)
                if not _fut.done():
                    if msg.get("ok"):
                        _fut.set_result(msg.get("payload") or {})
                    else:
                        _err = msg.get("error") or {}
                        _fut.set_exception(Exception(_err.get("message", "device returned error")))
                continue

            # OpenClaw client frames: {"req": "method.name", "id": "...", "params": {...}}
            # The "req" field IS the method name. Legacy/alternative format may use
            # {"type": "req", "method": "..."} — support both.
            _req_name = msg.get("req") or (msg.get("method", "") if _msg_type == "req" else "")
            if _req_name:
                m = _req_name
                rid = msg.get("id") or uuid.uuid4().hex
                p = msg.get("params") or {}
                log.info("openclaw: req method=%r session=%s", m, session_key)
                # Route chat sends concurrently so a long agent turn doesn't
                # block ticks / other RPCs. Other methods are cheap → inline.
                if m in ("chat.send", "sessions.send"):
                    asyncio.create_task(_dispatch(ws, sess, rid, m, p))
                else:
                    await _dispatch(ws, sess, rid, m, p)
            # Ignore client events / acks silently.
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("openclaw: connection error (session=%s): %s", session_key, e)
    finally:
        tick_task.cancel()
        _sessions.pop(session_key, None)
        _node_registry.pop(session_key, None)  # clean up if this was a node session
        _node_ws_map.pop(session_key, None)
        # Cancel any in-flight device requests for this node.
        for _rid, _fut in list(_device_pending.items()):
            if not _fut.done():
                _fut.set_exception(Exception("node disconnected"))
            _device_pending.pop(_rid, None)
        log.info("openclaw: client disconnected (session=%s)", session_key)
