"""Process-wide read-through access to the runtime model registry."""

from __future__ import annotations

import math
import re
import sys
import threading
from typing import Protocol


class ModelCatalogError(RuntimeError):
    """Raised when a bound catalog cannot provide coherent model pricing."""


class ModelCatalogReadError(ModelCatalogError):
    """Raised when a bound registry cannot be read."""


class _Registry(Protocol):
    def get_model(self, model_id: str) -> dict | None: ...

    def get_1m_models(self) -> set[str]: ...

    def list_models(self, *, provider: str = "", active_only: bool = True) -> list[dict]: ...


_RATE_FIELDS = (
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_write_5m_price",
    "cache_write_1h_price",
)
_TIER_RE = re.compile(r"\[[^\]]+\]\s*$")
_LOCK = threading.RLock()
_registry: _Registry | None = None
_rate_cache: dict[str, dict | None] = {}
_one_million_cache: set[str] | None = None
_one_million_loaded = False


def strip_tier(model_id: str) -> str:
    """Return a model id without a trailing context-tier suffix."""
    return _TIER_RE.sub("", (model_id or "").strip())


def incoherent_fields(model: dict) -> list[str]:
    """Return required pricing fields that are missing or invalid."""
    invalid: list[str] = []
    for field in _RATE_FIELDS:
        value = model.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            invalid.append(field)
            continue
        if not math.isfinite(float(value)) or float(value) < 0:
            invalid.append(field)
    return invalid


def bind_registry(registry: _Registry) -> None:
    """Atomically replace the bound registry and discard prior snapshots."""
    global _registry, _one_million_cache, _one_million_loaded
    with _LOCK:
        _registry = registry
        _rate_cache.clear()
        _one_million_cache = None
        _one_million_loaded = False


def reset_for_tests() -> None:
    """Return the process-wide catalog to its unbound state."""
    global _registry, _one_million_cache, _one_million_loaded
    with _LOCK:
        _registry = None
        _rate_cache.clear()
        _one_million_cache = None
        _one_million_loaded = False


def invalidate() -> None:
    """Discard all cached model and context-window reads."""
    global _one_million_cache, _one_million_loaded
    with _LOCK:
        _rate_cache.clear()
        _one_million_cache = None
        _one_million_loaded = False


def _log_read_error(subject: str, exc: Exception) -> None:
    print(
        f"ERROR runtime_model_catalog: {subject}: {exc}",
        file=sys.stderr,
        flush=True,
    )


def lookup_model(model_id: str) -> dict | None:
    """Read a model from the bound registry, caching only successful reads.

    ``None`` means either no registry is bound or the bound registry has no
    such model. Registry read failures and incomplete known rows are loud.
    """
    base = strip_tier(model_id)
    with _LOCK:
        registry = _registry
        if registry is None:
            return None
        if base in _rate_cache:
            row = _rate_cache[base]
        else:
            try:
                row = registry.get_model(base)
            except Exception as exc:
                _log_read_error(base, exc)
                raise ModelCatalogReadError(f"{base}: {exc}") from exc
            _rate_cache[base] = dict(row) if row is not None else None
            row = _rate_cache[base]

        if row is None or not row.get("active", 1):
            return None
        invalid = incoherent_fields(row)
        if invalid:
            full_id = row.get("id") or base
            fields = ", ".join(invalid)
            raise ModelCatalogError(f"{full_id} has incomplete pricing: {fields}")
        return dict(row)


def get_1m_models() -> set[str] | None:
    """Return the bound DB's authoritative 1M set, or ``None`` for fallback."""
    global _one_million_cache, _one_million_loaded
    with _LOCK:
        registry = _registry
        if registry is None:
            return None
        if _one_million_loaded:
            return set(_one_million_cache or ())
        try:
            models = set(registry.get_1m_models())
        except Exception as exc:
            _log_read_error("1M model catalog", exc)
            return None
        _one_million_cache = models
        _one_million_loaded = True
        return set(models)


def warm() -> None:
    """Populate registry snapshots without making boot depend on a clean read."""
    global _one_million_cache, _one_million_loaded
    with _LOCK:
        registry = _registry
        if registry is None:
            return
        try:
            rows = registry.list_models(active_only=True)
            models = set(registry.get_1m_models())
        except Exception as exc:
            _log_read_error("catalog warm", exc)
            return
        warmed = {strip_tier(row.get("model_id", "")): dict(row) for row in rows}
        _rate_cache.clear()
        _rate_cache.update(warmed)
        _one_million_cache = models
        _one_million_loaded = True
