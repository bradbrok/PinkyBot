"""Small in-process schema cache for Zoho field metadata."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class SchemaCache:
    """Cache `/crm/{module}/fields` responses for a bounded TTL."""

    def __init__(self, *, ttl_seconds: int = 3600, time_fn: Callable[[], float] = time.time) -> None:
        self.ttl_seconds = ttl_seconds
        self._time_fn = time_fn
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, module: str, fetch: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        now = self._time_fn()
        cached = self._cache.get(module)
        if cached and now - cached[0] < self.ttl_seconds:
            return cached[1]
        value = fetch(module)
        self._cache[module] = (now, value)
        return value

    def refresh(self, module: str, fetch: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        value = fetch(module)
        self._cache[module] = (self._time_fn(), value)
        return value

    def clear(self, module: str | None = None) -> None:
        if module is None:
            self._cache.clear()
        else:
            self._cache.pop(module, None)
