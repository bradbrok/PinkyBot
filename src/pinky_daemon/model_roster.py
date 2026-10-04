"""Strict parsing and packaged resources for the model catalog."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from importlib import resources

SCHEMA = "pinky-model-roster/1"
MAX_BYTES = 256 * 1024
MAX_MODELS = 500
MAX_REVISION = 2**63 - 1
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,99}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DOCUMENT_FIELDS = {"schema", "revision", "updated", "models"}
_MODEL_FIELDS = {
    "provider", "model_id", "display_name", "description", "tier", "context_window",
    "is_1m", "pricing", "supports_thinking", "active", "sort_order",
}
_PRICE_FIELDS = {"input", "output", "cached_input", "cache_write_5m", "cache_write_1h"}


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input: float
    output: float
    cached_input: float
    cache_write_5m: float
    cache_write_1h: float


@dataclass(frozen=True, slots=True)
class ModelRow:
    provider: str
    model_id: str
    display_name: str
    description: str
    tier: str
    context_window: int
    is_1m: bool
    pricing: ModelPricing
    supports_thinking: bool
    active: bool
    sort_order: int


@dataclass(frozen=True, slots=True)
class Roster:
    schema: str
    revision: int
    updated: str
    models: tuple[ModelRow, ...]


def _object(value: object, fields: set[str], path: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    if value.keys() != fields:
        missing = sorted(fields - value.keys())
        unknown = sorted(value.keys() - fields)
        raise ValueError(f"{path} keys: missing {missing}, unknown {unknown}")
    return value


def _string(value: object, minimum: int, maximum: int, path: str) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{path} must be a string of {minimum}..{maximum} characters")
    return value


def _integer(value: object, minimum: int, maximum: int | None, path: str) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{path} must be an integer in {minimum}..{maximum or 'unbounded'}")
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path} must be a boolean")
    return value


def _price(value: object, path: str) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 1000 or not math.isfinite(value):
        raise ValueError(f"{path} must be a finite number in 0..1000")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def parse(blob: bytes) -> Roster:
    """Validate the whole document or raise ValueError without returning partial rows."""
    if not isinstance(blob, bytes) or len(blob) > MAX_BYTES:
        raise ValueError(f"roster must be UTF-8 bytes, at most {MAX_BYTES} bytes")
    try:
        document = json.loads(blob.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("roster must be a valid UTF-8 JSON document") from exc
    except RecursionError as exc:
        raise ValueError("roster JSON nesting exceeds the parser limit") from exc
    document = _object(document, _DOCUMENT_FIELDS, "roster")
    if document["schema"] != SCHEMA:
        raise ValueError(f"roster.schema must be {SCHEMA!r}")
    revision = _integer(document["revision"], 1, MAX_REVISION, "roster.revision")
    updated = document["updated"]
    if not isinstance(updated, str) or not _DATE.fullmatch(updated):
        raise ValueError("roster.updated must be a canonical YYYY-MM-DD date")
    try:
        date.fromisoformat(updated)
    except ValueError as exc:
        raise ValueError("roster.updated must be a valid date") from exc
    models = document["models"]
    if not isinstance(models, list) or not 1 <= len(models) <= MAX_MODELS:
        raise ValueError(f"roster.models must be an array with 1..{MAX_MODELS} models")
    rows = []
    identifiers = set()
    for index, value in enumerate(models):
        path = f"roster.models[{index}]"
        row = _object(value, _MODEL_FIELDS, path)
        provider = row["provider"]
        if not isinstance(provider, str) or provider not in {"anthropic", "openai"}:
            raise ValueError(f"{path}.provider must be anthropic or openai")
        model_id = row["model_id"]
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
            raise ValueError(f"{path}.model_id must be a valid bare model identifier")
        if model_id in identifiers:
            raise ValueError(f"{path}.model_id duplicates {model_id!r}")
        identifiers.add(model_id)
        display_name = _string(row["display_name"], 1, 100, f"{path}.display_name")
        description = _string(row["description"], 0, 500, f"{path}.description")
        tier = _string(row["tier"], 0, 40, f"{path}.tier")
        context = _integer(row["context_window"], 8192, 10_000_000, f"{path}.context_window")
        is_1m = _boolean(row["is_1m"], f"{path}.is_1m")
        if is_1m != (context >= 1_000_000):
            raise ValueError(f"{path}.is_1m must agree with context_window")
        pricing = _object(row["pricing"], _PRICE_FIELDS, f"{path}.pricing")
        prices = {name: _price(value, f"{path}.pricing.{name}")
                  for name, value in pricing.items()}
        thinking = _boolean(row["supports_thinking"], f"{path}.supports_thinking")
        active = _boolean(row["active"], f"{path}.active")
        sort_order = _integer(row["sort_order"], 0, 10000, f"{path}.sort_order")
        rows.append(ModelRow(provider, model_id, display_name, description, tier, context,
                             is_1m, ModelPricing(**prices), thinking, active, sort_order))
    return Roster(SCHEMA, revision, updated, tuple(rows))


def load_bundled() -> Roster:
    """Load the shipped roster; resource and validation errors propagate to the caller."""
    return parse(resources.files("pinky_daemon.catalog").joinpath("models.json").read_bytes())
