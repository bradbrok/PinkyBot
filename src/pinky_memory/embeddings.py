from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """Synchronous OpenAI embeddings wrapper for MCP server."""

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        dimensions: int = 0,
    ) -> None:
        import openai

        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
        self._dimensions = dimensions or int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))
        self._client = openai.OpenAI(api_key=self._api_key, timeout=5.0)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> list[float]:
        result = self.embed_batch([text])
        return result[0] if result else []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(
            model=self._model,
            input=texts,
            dimensions=self._dimensions,
        )
        return [item.embedding for item in response.data]


class NoOpEmbeddingClient:
    """Fallback client that returns empty embeddings (no API key configured)."""

    @property
    def dimensions(self) -> int:
        return 0

    def embed(self, text: str) -> list[float]:
        return []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


def build_embedding_client(api_key: str = "") -> EmbeddingClient | NoOpEmbeddingClient:
    """Build the embedding client, resolving the OpenAI key in priority order.

    1. ``api_key`` argument — for callers that resolve it from a settings
       store the process env doesn't carry (e.g. the daemon reading the
       ``OPENAI_API_KEY`` row in ``system_settings``).
    2. ``OPENAI_API_KEY`` environment variable.

    Falls back to the NoOp client (empty embeddings → keyword-only recall)
    only when no key is found anywhere. That downgrade is logged at WARNING:
    a missing key silently disabled semantic recall fleet-wide before
    2026-06-05 because the embedder only ever consulted the env, while the
    key lived in ``system_settings``. Loud failure prevents a repeat.
    """
    resolved = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not resolved:
        logger.warning(
            "pinky-memory: no OPENAI_API_KEY (passed arg or environment) — "
            "using NoOp embedder; semantic recall() is DISABLED (keyword-only) "
            "until a key is configured"
        )
        return NoOpEmbeddingClient()
    return EmbeddingClient(api_key=resolved)
