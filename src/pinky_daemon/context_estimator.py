"""Shared context-size estimation helpers for non-SDK runtimes."""

from __future__ import annotations

from pinky_daemon.sessions import CHARS_PER_TOKEN


class ContextTextEstimator:
    """Estimate context from internal prompts plus persisted conversation text.

    Runtimes that do not expose authoritative context usage can share this
    best-effort estimator instead of duplicating token-counting fallback code.
    """

    def __init__(self, *, chars_per_token: int = CHARS_PER_TOKEN) -> None:
        self._chars_per_token = max(1, chars_per_token)
        self._internal_context_texts: list[str] = []

    def record_internal_text(self, text: str) -> None:
        """Track prompt/response text that is not in ConversationStore."""
        if text:
            self._internal_context_texts.append(text)

    def estimated_tokens(
        self,
        *,
        session_id: str,
        conversation_store=None,
        history_limit: int = 1000,
    ) -> int:
        """Estimate tokens from internal text and optional conversation history."""
        total = sum(self._estimate_text_tokens(text) for text in self._internal_context_texts)
        if not conversation_store:
            return total
        try:
            history = conversation_store.get_history(session_id, limit=history_limit)
        except Exception:
            return total
        return total + sum(
            self._estimate_text_tokens(getattr(msg, "content", ""))
            for msg in history
        )

    def context_info(
        self,
        *,
        session_id: str,
        max_tokens: int,
        conversation_store=None,
        history_limit: int = 1000,
    ) -> dict:
        """Return the context-info shape expected by streaming status APIs."""
        total = self.estimated_tokens(
            session_id=session_id,
            conversation_store=conversation_store,
            history_limit=history_limit,
        )
        pct = round(total / max_tokens * 100, 1) if max_tokens > 0 else 0.0
        return {
            "total_tokens": total,
            "max_tokens": max_tokens,
            "percentage": pct,
            "categories": [],
            "mcp_tools": [],
        }

    def _estimate_text_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // self._chars_per_token)
