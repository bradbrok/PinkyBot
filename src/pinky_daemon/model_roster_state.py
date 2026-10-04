"""Pure row planning and field ownership for a local model roster."""

from __future__ import annotations

import json

from pinky_daemon.model_roster import ModelRow

MANAGED_FIELDS = (
    "display_name",
    "description",
    "tier",
    "context_window",
    "is_1m",
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_write_5m_price",
    "cache_write_1h_price",
    "supports_thinking",
    "active",
    "sort_order",
)
CONTEXT_FIELDS = frozenset({"context_window", "is_1m"})
CLASSIFICATION_MARKER = "migration:model_roster_baseline_v1"


def model_values(row: ModelRow) -> dict:
    """Flatten validated roster values into the existing SQLite column names."""
    return {
        "display_name": row.display_name,
        "description": row.description,
        "tier": row.tier,
        "context_window": row.context_window,
        "is_1m": int(row.is_1m),
        "input_price": row.pricing.input,
        "output_price": row.pricing.output,
        "cached_input_price": row.pricing.cached_input,
        "cache_write_5m_price": row.pricing.cache_write_5m,
        "cache_write_1h_price": row.pricing.cache_write_1h,
        "supports_thinking": int(row.supports_thinking),
        "active": int(row.active),
        "sort_order": row.sort_order,
    }


def context_unit(fields: set[str]) -> set[str]:
    result = set(fields)
    if result & CONTEXT_FIELDS:
        result.update(CONTEXT_FIELDS)
    return result


def operator_fields(encoded: str) -> set[str]:
    """Refuse malformed provenance rather than treating an owned field as unowned."""
    try:
        values = json.loads(encoded)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid model operator_fields") from exc
    if not isinstance(values, list) or any(
        not isinstance(value, str) or value not in MANAGED_FIELDS for value in values
    ):
        raise ValueError("invalid model operator_fields")
    return context_unit(set(values))


def plan_fields(existing: dict, incoming: dict, owned: set[str], *, fields=None) -> dict:
    """Plan only differing eligible values; a roster never retires an active row."""
    fields = set(MANAGED_FIELDS) if fields is None else context_unit(set(fields))
    owned = context_unit(owned)
    updates = {}
    skipped = []
    for field in MANAGED_FIELDS:
        if field not in fields or existing[field] == incoming[field]:
            continue
        if field == "active" and existing[field] and not incoming[field]:
            skipped.append({"field": field, "reason": "deactivation_not_automatic"})
        elif field in owned:
            skipped.append({"field": field, "reason": "operator_owned"})
        else:
            updates[field] = incoming[field]
    if updates.keys() & CONTEXT_FIELDS:
        updates.update({field: incoming[field] for field in CONTEXT_FIELDS})
    return {"updates": updates, "skipped": skipped}


def empty_counts() -> dict:
    """Each category counts rows once; categories may overlap on a partially owned row."""
    return dict(
        inserted=0,
        updated=0,
        unchanged=0,
        skipped_operator=0,
        skipped_deactivation=0,
        skipped_inactive=0,
    )
