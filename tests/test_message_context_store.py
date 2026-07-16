"""Durability and retention contracts for broker message context."""

from __future__ import annotations

import sqlite3
import threading
import time

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.message_context_store import MessageContextStore
from pinky_daemon.sessions import SessionManager


def _stored_context(message_id: str, *, agent_name: str = "barsik") -> dict:
    return {
        "agent_name": agent_name,
        "message_id": message_id,
        "platform": "telegram",
        "chat_id": "6770805286",
        "timestamp": 1_700_000_000.0,
        "reply_to": "42",
        "is_group": False,
        "source_was_voice": True,
        "attachments": [{"type": "voice", "file_id": "voice-1"}],
        "metadata": {"chat_title": "Brad"},
    }


def test_context_survives_broker_restart_via_load_through(tmp_path):
    db_path = tmp_path / "message-context.db"
    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    first_store = MessageContextStore(str(db_path))
    first_broker = MessageBroker(
        registry,
        SessionManager(),
        message_context_store=first_store,
    )
    first_broker.remember_message_context(
        BrokerMessage(
            platform="telegram",
            chat_id="6770805286",
            sender_name="Brad",
            sender_id="owner",
            content="voice note",
            agent_name="barsik",
            message_id="99",
            reply_to="42",
            attachments=[{"type": "voice", "file_id": "voice-1"}],
            metadata={"chat_title": "Brad"},
            timestamp=1_700_000_000.0,
        ),
        source_was_voice=True,
    )
    first_store.close()

    second_store = MessageContextStore(str(db_path))
    second_broker = MessageBroker(
        registry,
        SessionManager(),
        message_context_store=second_store,
    )

    assert second_broker._message_contexts == {}
    context = second_broker.get_message_context("barsik", "99")
    assert context is not None
    assert context.to_dict() == _stored_context("99")
    assert second_broker._message_contexts[
        ("barsik", "telegram", "6770805286", "99")
    ] is context
    second_store.close()


def test_store_uses_wal_and_self_heals_optional_columns(tmp_path):
    db_path = tmp_path / "message-context.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE message_contexts (
            agent_name TEXT NOT NULL,
            message_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            message_ts REAL NOT NULL,
            PRIMARY KEY (agent_name, message_id)
        )
        """
    )
    legacy.execute(
        """
        INSERT INTO message_contexts (
            agent_name, message_id, platform, chat_id, message_ts
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("barsik", "legacy-1", "telegram", "CHAT_A", 1_700_000_000.0),
    )
    legacy.commit()
    legacy.close()

    store = MessageContextStore(str(db_path))
    mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
    columns = {
        row["name"]
        for row in store._db.execute("PRAGMA table_info(message_contexts)").fetchall()
    }

    assert str(mode).lower() == "wal"
    assert {"reply_to", "attachments_json", "metadata_json", "stored_at"} <= columns
    primary_key = tuple(
        row["name"]
        for row in sorted(
            (
                row
                for row in store._db.execute(
                    "PRAGMA table_info(message_contexts)"
                ).fetchall()
                if row["pk"]
            ),
            key=lambda row: row["pk"],
        )
    )
    assert primary_key == ("agent_name", "platform", "chat_id", "message_id")
    migrated = store.get("barsik", "legacy-1")
    assert migrated is not None
    assert migrated["chat_id"] == "CHAT_A"
    store.close()


def test_retention_prunes_old_rows_and_caps_each_agent(tmp_path):
    now = 1_800_000_000.0
    store = MessageContextStore(
        str(tmp_path / "message-context.db"),
        retention_days=30,
        max_per_agent=2,
    )

    store.put(_stored_context("expired"), stored_at=now - (31 * 86400))
    store.put(_stored_context("recent-1"), stored_at=now - 3)
    store.put(_stored_context("recent-2"), stored_at=now - 2)
    store.put(_stored_context("recent-3"), stored_at=now - 1)
    store.put(_stored_context("other", agent_name="murzik"), stored_at=now)

    assert store.get("barsik", "expired") is None
    assert store.get("barsik", "recent-1") is None
    assert store.get("barsik", "recent-2") is not None
    assert store.get("barsik", "recent-3") is not None
    assert store.get("murzik", "other") is not None
    store.close()


def test_chat_scoped_message_id_collision_is_ambiguous_but_exact_rows_survive(tmp_path):
    store = MessageContextStore(str(tmp_path / "message-context.db"))
    first = _stored_context("1")
    second = {**first, "chat_id": "CHAT_B"}

    store.put(first)
    store.put(second)

    assert store.get("barsik", "1") is None
    assert store.get(
        "barsik", "1", platform="telegram", chat_id="6770805286"
    )["chat_id"] == "6770805286"
    assert store.get(
        "barsik", "1", platform="telegram", chat_id="CHAT_B"
    )["chat_id"] == "CHAT_B"
    store.close()


def test_broker_cache_honors_store_retention_after_durable_sweep(tmp_path, monkeypatch):
    db_path = tmp_path / "message-context.db"
    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    store = MessageContextStore(str(db_path))
    broker = MessageBroker(registry, SessionManager(), message_context_store=store)
    now = time.time()
    store.put(_stored_context("old"), stored_at=now)

    assert broker.get_message_context("barsik", "old") is not None
    assert broker._message_contexts

    future = now + (31 * 86400)
    store.sweep_retention(now=future)
    monkeypatch.setattr("pinky_daemon.broker.time.time", lambda: future)

    assert broker.get_message_context("barsik", "old") is None
    assert broker._message_contexts == {}
    store.close()


def test_put_and_retention_sweep_hold_one_lock_against_close(tmp_path, monkeypatch):
    store = MessageContextStore(str(tmp_path / "message-context.db"))
    sweep_started = threading.Event()
    allow_sweep = threading.Event()
    close_finished = threading.Event()
    errors: list[BaseException] = []
    original_sweep = store._sweep_retention_locked

    def blocking_sweep(now: float) -> int:
        sweep_started.set()
        assert allow_sweep.wait(timeout=2)
        return original_sweep(now)

    def put_context() -> None:
        try:
            store.put(_stored_context("locked"))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def close_store() -> None:
        store.close()
        close_finished.set()

    monkeypatch.setattr(store, "_sweep_retention_locked", blocking_sweep)
    writer = threading.Thread(target=put_context)
    writer.start()
    assert sweep_started.wait(timeout=2)

    closer = threading.Thread(target=close_store)
    closer.start()
    assert not close_finished.wait(timeout=0.05)

    allow_sweep.set()
    writer.join(timeout=2)
    closer.join(timeout=2)
    assert not writer.is_alive()
    assert not closer.is_alive()
    assert errors == []
