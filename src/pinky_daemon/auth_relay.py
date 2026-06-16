"""Relay a tmux ``claude`` OAuth login to the agent's owner (#205).

When a freshly-spawned tmux ``claude`` REPL has no credentials it sits at the
OAuth login wall — it prints a ``https://claude.ai/oauth/...`` URL and waits for
the user to approve in a browser and paste back a code. Today the only fix is a
human running ``claude login`` by hand inside the session.

This module automates the human-in-the-loop:

  * ``TmuxSession._watch_for_oauth_url`` detects the wall (read-only
    ``capture_pane``) and calls :meth:`AuthRelayCoordinator.open`, which relays
    the URL to the agent's owner and awaits the code.
  * ``Broker.handle_inbound`` intercepts the owner's reply, validates it, and
    calls :meth:`AuthRelayCoordinator.submit`, which wakes the watcher with the
    code. The watcher pastes it into the pane.

The whole feature is gated behind ``PINKY_TMUX_AUTH_RELAY`` (default OFF). When
off, :meth:`enabled` returns False and neither side ever calls in here.

Security: the code is accepted ONLY from the agent's owner (a third-party code
would log the agent into the *attacker's* Claude account — a session hijack).
The owner-gate and reply-correlation live in the broker intercept; this module
never logs the code itself.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

# ── Pure extractors (no daemon deps — trivially unit-testable) ───────────────

# claude prints an absolute https URL on claude.ai / console.anthropic.com.
# ``[^\s'"]+`` stops at whitespace/quotes; with a ``-J`` (join-wrapped) capture
# the long URL is contiguous, but we also de-wrap defensively below.
_OAUTH_URL_RE = re.compile(
    r"https://(?:claude\.ai|console\.anthropic\.com)/[^\s'\"<>]+"
)

# A pasted login code is a long URL-safe token, optionally ``<code>#<state>``.
# Length floor filters out one-word prose; the real correctness boundary is the
# owner-gate + reply-to-our-relay check in the broker, so this stays permissive.
_CODE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~/+#=-]{12,}$")

DEFAULT_TTL_SEC = 600.0  # OAuth device codes expire ~10-15 min.
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def extract_oauth_url(pane_text: str) -> Optional[str]:
    """Return the first claude/anthropic OAuth URL in ``pane_text``, else None.

    De-wraps any stray whitespace the terminal may have injected mid-URL.
    """
    if not pane_text:
        return None
    m = _OAUTH_URL_RE.search(pane_text)
    if not m:
        return None
    return "".join(m.group(0).split())


def extract_auth_code(reply_text: str) -> Optional[str]:
    """Pull a login code out of the owner's reply, or None if none looks valid.

    Handles both a bare-token reply ("just the code") and a code embedded in a
    short sentence ("the code is <token>") by picking the longest code-shaped
    token. Returns None for prose / empty so the caller can ask again.
    """
    if not reply_text:
        return None
    best: Optional[str] = None
    for raw in reply_text.split():
        tok = raw.strip().strip(".,;:!?\"'()[]{}")
        if _CODE_TOKEN_RE.match(tok) and (best is None or len(tok) > len(best)):
            best = tok
    return best


def looks_like_login_wall(pane_text: str) -> bool:
    """True when the pane appears to be sitting at the OAuth login wall."""
    return extract_oauth_url(pane_text) is not None


# ── Owner-facing message copy ────────────────────────────────────────────────


def build_relay_message(agent_name: str, url: str) -> str:
    return (
        f'Agent "{agent_name}" needs a Claude sign-in to start.\n\n'
        f"1) Open this link and approve it:\n{url}\n\n"
        f"2) Reply to THIS message with the code it gives you."
    )


def build_expiry_message(agent_name: str) -> str:
    return (
        f'The Claude sign-in link for agent "{agent_name}" expired before a code '
        f"arrived. Restart the session to try again."
    )


# ── Coordinator ──────────────────────────────────────────────────────────────

# send_fn(agent_name, platform, chat_id, content) -> dict | None
#   (the daemon's _broker_send; returns {"message_id": ...} on success)
SendFn = Callable[[str, str, str, str], Awaitable[object]]
# owner_resolver() -> {"chat_id": str, ...}
OwnerResolver = Callable[[], dict]
# get_setting(key, default) -> str  (registry settings reader)
SettingReader = Callable[..., str]


@dataclass
class _Pending:
    agent_name: str
    relay_mid: str
    future: "asyncio.Future[str]"
    issued_at: float


class AuthRelayCoordinator:
    """Process-wide registry of in-flight login relays, keyed by agent.

    One agent session can be at exactly one login wall at a time, so pending
    state is keyed by ``agent_name``. Decouples the TmuxSession watcher
    (producer of "need auth") from the Broker intercept (consumer of "here's
    the code").
    """

    def __init__(self) -> None:
        self._send_fn: Optional[SendFn] = None
        self._owner_resolver: Optional[OwnerResolver] = None
        self._pending: dict[str, _Pending] = {}

    # -- wiring (called once at daemon startup) --
    def configure(self, send_fn: SendFn, owner_resolver: OwnerResolver) -> None:
        self._send_fn = send_fn
        self._owner_resolver = owner_resolver

    @property
    def configured(self) -> bool:
        return self._send_fn is not None and self._owner_resolver is not None

    @staticmethod
    def enabled(get_setting: Optional[SettingReader] = None) -> bool:
        """Flag check: registry setting wins if explicitly set, else env var."""
        if get_setting is not None:
            try:
                v = (get_setting("PINKY_TMUX_AUTH_RELAY", "") or "").strip().lower()
                if v in _TRUE:
                    return True
                if v in _FALSE:
                    return False
            except Exception:
                pass
        return os.environ.get("PINKY_TMUX_AUTH_RELAY", "0").strip().lower() in _TRUE

    # -- producer side (TmuxSession watcher) --
    async def open(
        self,
        agent_name: str,
        url: str,
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        platform: str = "telegram",
    ) -> str:
        """Relay ``url`` to the owner and await the code.

        Returns the code string. Raises ``TimeoutError`` after ``ttl_sec`` (and
        notifies the owner of expiry) or ``RuntimeError`` if unconfigured / no
        owner chat is known.
        """
        if not self.configured:
            raise RuntimeError("auth relay not configured")
        owner = self._owner_resolver() or {}  # type: ignore[misc]
        chat_id = str(owner.get("chat_id") or "").strip()
        if not chat_id:
            raise RuntimeError("no owner chat_id to relay auth to")

        # Replace any stale pending for this agent (e.g. a prior boot attempt).
        old = self._pending.pop(agent_name, None)
        if old is not None and not old.future.done():
            old.future.cancel()

        result = await self._send_fn(  # type: ignore[misc]
            agent_name, platform, chat_id, build_relay_message(agent_name, url)
        )
        relay_mid = ""
        if isinstance(result, dict):
            relay_mid = str(result.get("message_id") or "")

        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[str]" = loop.create_future()
        self._pending[agent_name] = _Pending(agent_name, relay_mid, fut, time.time())
        try:
            return await asyncio.wait_for(fut, timeout=ttl_sec)
        except asyncio.TimeoutError:
            try:
                await self._send_fn(  # type: ignore[misc]
                    agent_name, platform, chat_id, build_expiry_message(agent_name)
                )
            except Exception:
                pass
            raise
        finally:
            cur = self._pending.get(agent_name)
            if cur is not None and cur.future is fut:
                self._pending.pop(agent_name, None)

    async def notify_owner(
        self, agent_name: str, text: str, *, platform: str = "telegram"
    ) -> None:
        """Best-effort out-of-band note to the owner (e.g. "signed in")."""
        if not self.configured:
            return
        owner = self._owner_resolver() or {}  # type: ignore[misc]
        chat_id = str(owner.get("chat_id") or "").strip()
        if not chat_id:
            return
        try:
            await self._send_fn(agent_name, platform, chat_id, text)  # type: ignore[misc]
        except Exception:
            pass

    # -- consumer side (Broker intercept) --
    def has_pending(self, agent_name: str) -> bool:
        p = self._pending.get(agent_name)
        return p is not None and not p.future.done()

    def pending_relay_mid(self, agent_name: str) -> Optional[str]:
        p = self._pending.get(agent_name)
        return p.relay_mid if p is not None else None

    def submit(self, agent_name: str, code: str) -> bool:
        """Deliver ``code`` to a waiting :meth:`open`. True if one was waiting."""
        p = self._pending.get(agent_name)
        if p is None or p.future.done():
            return False
        p.future.set_result(code)
        return True


# Single process-wide instance shared by tmux_session.py and broker.py.
coordinator = AuthRelayCoordinator()
