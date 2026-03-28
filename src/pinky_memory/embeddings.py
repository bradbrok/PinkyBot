from __future__ import annotations

import os


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


def build_embedding_client() -> EmbeddingClient | NoOpEmbeddingClient:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return NoOpEmbeddingClient()
    return EmbeddingClient(api_key=api_key)
