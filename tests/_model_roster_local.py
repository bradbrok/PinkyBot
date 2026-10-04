"""Synthetic registry inputs and public-contract assertions for local roster tests."""

from __future__ import annotations

import copy
import inspect
import json
import sqlite3
from contextlib import closing
from importlib import resources

SOURCE = "https://raw.githubusercontent.com/example/project/main/models.json"
MARKER = "migration:model_roster_baseline_v1"
DOCUMENT_KEY = "model_roster.last_applied_document"
SONNET = "anthropic/claude-sonnet-5"
FIELDS = {
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
}
CONTEXT_FIELDS = {"context_window", "is_1m"}
PRICE_FIELDS = {
    "input_price": "input",
    "output_price": "output",
    "cached_input_price": "cached_input",
    "cache_write_5m_price": "cache_write_5m",
    "cache_write_1h_price": "cache_write_1h",
}


def bundled_bytes(name="models.json"):
    return resources.files("pinky_daemon.catalog").joinpath(name).read_bytes()


def document(revision=2):
    result = json.loads(bundled_bytes())
    result.update(revision=revision, updated="2026-10-04")
    return result


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def model_row(value, full_id=SONNET):
    return next(row for row in value["models"] if f"{row['provider']}/{row['model_id']}" == full_id)


def new_model(model_id="roster-added-model", *, active=True):
    result = copy.deepcopy(model_row(document()))
    result.update(provider="openai", model_id=model_id, display_name="Added model", active=active)
    return result


def flat(row):
    result = {name: row[name] for name in FIELDS - PRICE_FIELDS.keys()}
    result.update({name: row["pricing"][key] for name, key in PRICE_FIELDS.items()})
    return result


def require_method(registry, name):
    method = getattr(registry, name, None)
    assert callable(method), f"AgentRegistry must implement {name}"
    return method


def apply(registry, value, *, source=SOURCE, dry_run=False):
    method = require_method(registry, "apply_model_roster")
    return method(
        encode(value) if isinstance(value, dict) else value, source=source, dry_run=dry_run
    )


def release(registry, full_id, fields):
    return require_method(registry, "release_model_roster_fields")(full_id, fields)


def status(registry):
    value = require_method(registry, "get_model_roster_status")()
    assert isinstance(value, dict)
    return value


def owned(registry, full_id=SONNET):
    row = registry.get_model(full_id)
    assert row is not None
    value = row.get("operator_fields")
    assert isinstance(value, str), "models.operator_fields must be persisted JSON text"
    result = json.loads(value)
    assert isinstance(result, list) and len(result) == len(set(result))
    assert set(result) <= FIELDS
    return set(result)


def add(registry, row, *, operator=None):
    kwargs = flat(row)
    kwargs.pop("active")
    kwargs.update(provider=row["provider"], model_id=row["model_id"])
    if operator is not None:
        assert "operator" in inspect.signature(registry.add_model).parameters, (
            "add_model must distinguish operator edits from automation"
        )
        kwargs["operator"] = operator
    return registry.add_model(**kwargs)


def snapshot(registry):
    models = registry._db.execute("SELECT * FROM models ORDER BY id").fetchall()
    settings = registry._db.execute(
        "SELECT key, value FROM system_settings ORDER BY key"
    ).fetchall()
    return {"models": models, "settings": settings}


def last_good(registry):
    return {
        key: value
        for key, value in registry._db.execute(
            "SELECT key, value FROM system_settings WHERE key LIKE 'model_roster.%' ORDER BY key"
        ).fetchall()
        if key not in {"model_roster.last_attempt_at", "model_roster.last_error"}
    }


def assert_skip(report, full_id, reason):
    rendered = json.dumps(report, sort_keys=True)
    assert reason in rendered
    assert full_id in rendered or full_id.split("/", 1)[1] in rendered


def legacy_db(path, *, overrides=None, extra_rows=(), write_columns=True):
    """Build only the old model/settings tables, with no agent or transport data."""
    columns = [
        "id TEXT PRIMARY KEY",
        "provider TEXT NOT NULL DEFAULT 'anthropic'",
        "model_id TEXT NOT NULL",
        "display_name TEXT NOT NULL DEFAULT ''",
        "description TEXT NOT NULL DEFAULT ''",
        "tier TEXT NOT NULL DEFAULT ''",
        "context_window INTEGER NOT NULL DEFAULT 200000",
        "is_1m INTEGER NOT NULL DEFAULT 0",
        "input_price REAL NOT NULL DEFAULT 0",
        "output_price REAL NOT NULL DEFAULT 0",
        "cached_input_price REAL NOT NULL DEFAULT 0",
        "supports_thinking INTEGER NOT NULL DEFAULT 1",
        "active INTEGER NOT NULL DEFAULT 1",
        "sort_order INTEGER NOT NULL DEFAULT 100",
        "created_at REAL NOT NULL DEFAULT 0",
        "updated_at REAL NOT NULL DEFAULT 0",
    ]
    if write_columns:
        columns += ["cache_write_5m_price REAL", "cache_write_1h_price REAL"]
    baseline = json.loads(bundled_bytes("models.baseline.json"))
    overrides = overrides or {}
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE models (" + ",".join(columns) + ",UNIQUE(provider,model_id))"
        )
        connection.execute(
            "CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
        )
        for row in baseline["models"] + list(extra_rows):
            full_id = f"{row['provider']}/{row['model_id']}"
            values = flat(row)
            values.update(
                id=full_id,
                provider=row["provider"],
                model_id=row["model_id"],
                created_at=11.0,
                updated_at=12.0,
            )
            values.update(overrides.get(full_id, {}))
            if not write_columns:
                values.pop("cache_write_5m_price")
                values.pop("cache_write_1h_price")
            names = list(values)
            connection.execute(
                f"INSERT INTO models ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                tuple(values[name] for name in names),
            )
    return path
