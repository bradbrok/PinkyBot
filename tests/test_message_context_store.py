"""Durability and retention contracts for broker message context."""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from pinky_daemon import message_context_store as store_module
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
        "metadata": {"chat_title": "Brad", "direction": "inbound"},
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
        hard_cap_per_agent=2,
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
    assert len(store.find("barsik", "1")) == 2
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



def _text_context(message_id="record", *, direction="inbound"):
    return {"agent_name": "sample", "message_id": message_id, "platform": "test",
            "chat_id": "chat", "timestamp": 1.0, "metadata": {"direction": direction}}


def _put_text(store, context, **kwargs):
    import inspect

    assert {"content", "sender_id"} <= set(inspect.signature(store.put).parameters), (
        "the store must accept text only through explicit keyword arguments"
    )
    store.put(context, **kwargs)


def _read_text(store, message_id="record", **kwargs):
    getter = getattr(store, "get_text", None)
    assert callable(getter), "a separate bounded text getter is required"
    return getter("sample", message_id, platform="test", chat_id="chat", **kwargs)


@pytest.mark.parametrize("text", ["", " exact\r\ntext e\u0301 é 😀 ", "é" * 8192, "x" * 16384],
                         ids=["empty", "unicode", "multibyte-limit", "ascii-limit"])
def test_explicit_inbound_text_survives_reopen_without_normalization(tmp_path, text):
    path = str(tmp_path / "context.db")
    store = MessageContextStore(path)
    _put_text(store, _text_context(), content=text, sender_id="sender-1")
    store.close()
    store = MessageContextStore(path)
    try:
        assert _read_text(store) == (text, "ok", "sender-1")
        ordinary = store.get("sample", "record", platform="test", chat_id="chat")
        assert not {"content", "text", "content_status", "sender_id"} & ordinary.keys()
    finally:
        store.close()


@pytest.mark.parametrize("text", ["é" * 8192 + "x", "x" * 16385], ids=["multibyte-over", "ascii-over"])
def test_over_byte_cap_replaces_previous_text_with_null_not_prefix(tmp_path, text):
    store = MessageContextStore(str(tmp_path / "context.db"))
    try:
        _put_text(store, _text_context(), content="old", sender_id="sender-0")
        _put_text(store, _text_context(), content=text, sender_id="sender-1")
        assert _read_text(store) == (None, "too_long", "sender-1")
        assert store._db.execute("SELECT content FROM message_contexts").fetchone()[0] is None
    finally:
        store.close()


@pytest.mark.parametrize("direction", ["outbound", "", "Inbound"])
def test_non_inbound_upsert_clears_every_text_column(tmp_path, direction):
    store = MessageContextStore(str(tmp_path / "context.db"))
    try:
        _put_text(store, _text_context(), content="previous", sender_id="sender-1")
        _put_text(store, _text_context(direction=direction), content="forged", sender_id="forged")
        assert _read_text(store) == (None, "absent", "")
        row = store._db.execute("SELECT content, content_status, sender_id FROM message_contexts").fetchone()
        assert tuple(row) == (None, "absent", "")
    finally:
        store.close()


def test_metadata_and_context_keys_cannot_supply_text_or_sender(tmp_path):
    store = MessageContextStore(str(tmp_path / "context.db"))
    try:
        context = _text_context()
        context.update(content="forged", text="forged", sender_id="forged")
        context["metadata"].update(content="forged", text="forged", sender_id="forged")
        store.put(context)
        assert _read_text(store) == (None, "absent", "")
        _put_text(store, context, content="exact", sender_id="sender-1")
        assert _read_text(store) == ("exact", "ok", "sender-1")
    finally:
        store.close()


@pytest.mark.parametrize("legacy_key", [False, True])
@pytest.mark.parametrize("seed_text", [False, True])
def test_text_columns_migrate_twice_and_survive_identity_rebuild(tmp_path, legacy_key, seed_text):
    path = str(tmp_path / "context.db")
    db = sqlite3.connect(path)
    key = "agent_name, message_id" if legacy_key else "agent_name, platform, chat_id, message_id"
    db.execute(f"""CREATE TABLE message_contexts (
        agent_name TEXT NOT NULL, message_id TEXT NOT NULL, platform TEXT NOT NULL,
        chat_id TEXT NOT NULL, message_ts REAL NOT NULL, reply_to TEXT NOT NULL DEFAULT '',
        is_group INTEGER NOT NULL DEFAULT 0, source_was_voice INTEGER NOT NULL DEFAULT 0,
        attachments_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{{}}',
        stored_at REAL NOT NULL, PRIMARY KEY ({key}))""")
    db.execute("""INSERT INTO message_contexts
        (agent_name, message_id, platform, chat_id, message_ts, metadata_json, stored_at)
        VALUES ('sample', 'record', 'test', 'chat', 1, '{"direction":"inbound"}', ?)""",
               (time.time(),))
    if seed_text:
        for column in ["content TEXT", "content_status TEXT NOT NULL DEFAULT 'absent'",
                       "sender_id TEXT NOT NULL DEFAULT ''"]:
            db.execute("ALTER TABLE message_contexts ADD COLUMN " + column)
        db.execute("UPDATE message_contexts SET content='prior text', content_status='ok', sender_id='prior-sender'")
    db.commit()
    db.close()
    for _ in range(2):
        store = MessageContextStore(path)
        try:
            columns = {r["name"] for r in store._db.execute("PRAGMA table_info(message_contexts)")}
            assert {"content", "content_status", "sender_id"} <= columns
            assert _read_text(store) == (("prior text", "ok", "prior-sender") if seed_text
                                         else (None, "absent", ""))
        finally:
            store.close()


def test_text_visibility_matches_retention_cap_and_snapshot(tmp_path, monkeypatch):
    store = MessageContextStore(str(tmp_path / "context.db"), max_per_agent=2, hard_cap_per_agent=2)
    now = time.time()
    try:
        _put_text(store, _text_context(), content="first", sender_id="sender", stored_at=now)
        assert _read_text(store, stored_at=now) == ("first", "ok", "sender")
        _put_text(store, _text_context(), content="second", sender_id="sender", stored_at=now + 1)
        assert _read_text(store, stored_at=now) is None
        for index in range(2):
            _put_text(store, _text_context(str(index)), content="recent", stored_at=now + 2 + index)
        assert _read_text(store) is None
        assert store._db.execute("SELECT 1 FROM message_contexts WHERE message_id='record'").fetchone() is None
        future = now + 32 * 86400
        monkeypatch.setattr("pinky_daemon.message_context_store.time.time", lambda: future)
        assert _read_text(store, "1") is None
        store.sweep_retention(now=future)
        assert store._db.execute("SELECT count(*) FROM message_contexts").fetchone()[0] == 0
    finally:
        store.close()


def test_private_text_lookup_uses_every_identity_component(tmp_path):
    store = MessageContextStore(str(tmp_path / "context.db"))
    try:
        _put_text(store, _text_context(), content="first", sender_id="sender-1")
        _put_text(store, {**_text_context(), "chat_id": "other-chat"}, content="second", sender_id="sender-2")
        getter = getattr(store, "get_text", None)
        assert callable(getter)
        assert getter("other", "record", platform="test", chat_id="chat") is None
        assert getter("sample", "record", platform="other", chat_id="chat") is None
        assert getter("sample", "other", platform="test", chat_id="chat") is None
        assert getter("sample", "record", platform="test", chat_id="other-chat") == ("second", "ok", "sender-2")
        assert _read_text(store) == ("first", "ok", "sender-1")
    finally:
        store.close()


DAY = 86400
CONTEXT_NOW = 1_800_000_000.0


@pytest.fixture
def context_clock(monkeypatch):
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: CONTEXT_NOW))
    return CONTEXT_NOW


def _age_context(message_id, *, agent_name="sample", direction="inbound"):
    return {"agent_name": agent_name, "message_id": message_id, "platform": "test",
            "chat_id": "chat", "timestamp": CONTEXT_NOW, "metadata": {"direction": direction}}


def _age_row_ids(store, agent_name="sample"):
    return [row[0] for row in store._db.execute(
        "SELECT message_id FROM message_contexts WHERE agent_name=? "
        "ORDER BY stored_at ASC, rowid ASC", (agent_name,),
    )]


def test_minimum_age_preserves_young_rows_beyond_count_limit(tmp_path, context_clock):
    path = str(tmp_path / "context.db")
    store = MessageContextStore(path, max_per_agent=1000)
    try:
        for index in range(1500):
            store.put(_age_context(str(index), direction="outbound" if index % 2 else "inbound"),
                      stored_at=context_clock - 6 * DAY + index)
        assert store.sweep_retention(now=context_clock) == 0
        assert _age_row_ids(store) == [str(index) for index in range(1500)]
    finally:
        store.close()
    store = MessageContextStore(path, max_per_agent=1000)
    try:
        assert _age_row_ids(store) == [str(index) for index in range(1500)]
    finally:
        store.close()


@pytest.mark.parametrize("tied", [False, True])
def test_age_limited_cap_keeps_newest_overall_and_every_young_row(tmp_path, context_clock, tied):
    store = MessageContextStore(str(tmp_path / "context.db"), max_per_agent=3)
    try:
        for index in range(5):
            store.put(_age_context(f"old-{index}"),
                      stored_at=context_clock - 8 * DAY + (0 if tied else index))
        store.sweep_retention(now=context_clock)
        assert _age_row_ids(store) == [f"old-{index}" for index in range(2, 5)]
        for index in range(5):
            store.put(_age_context(f"young-{index}"), stored_at=context_clock - 60 + index)
        store.put(_age_context("isolated", agent_name="other"), stored_at=context_clock)
        store.sweep_retention(now=context_clock)
        assert _age_row_ids(store) == [f"young-{index}" for index in range(5)]
        assert _age_row_ids(store, "other") == ["isolated"]
    finally:
        store.close()


def test_expiry_still_limits_reads_and_removes_rows_below_minimum_age(tmp_path, context_clock):
    store = MessageContextStore(str(tmp_path / "context.db"), min_age_days=60)
    try:
        store.put(_age_context("expired"), stored_at=context_clock - 31 * DAY)
        assert store.find("sample", "expired") == []
        assert store.get_text("sample", "expired", platform="test", chat_id="chat") is None
        assert store.sweep_retention(now=context_clock) == 1
        assert _age_row_ids(store) == []
        store.put(_age_context("retained"), stored_at=context_clock - 29 * DAY)
        assert store.sweep_retention(now=context_clock) == 0
        assert store.find("sample", "retained")
        assert store.retention_days == 30
    finally:
        store.close()


def test_zero_minimum_age_keeps_a_one_day_floor(tmp_path, context_clock):
    store = MessageContextStore(str(tmp_path / "context.db"), max_per_agent=1, min_age_days=0)
    try:
        store.put(_age_context("boundary"), stored_at=context_clock - DAY)
        store.put(_age_context("young"), stored_at=context_clock - DAY / 2)
        store.put(_age_context("latest"), stored_at=context_clock)
        assert store.sweep_retention(now=context_clock) == 0
        assert _age_row_ids(store) == ["boundary", "young", "latest"]
        assert store.min_age_days == 1
        assert store.sweep_retention(now=context_clock + 0.5) == 1
        assert _age_row_ids(store) == ["young", "latest"]
    finally:
        store.close()


@pytest.mark.parametrize("name,expected", [
    ("DEFAULT_MIN_AGE_DAYS", 7), ("DEFAULT_MAX_PER_AGENT", 5000),
    ("DEFAULT_HARD_CAP_PER_AGENT", 20000),
])
def test_message_context_retention_defaults(name, expected):
    assert getattr(store_module, name) == expected


@pytest.mark.parametrize("stage", ["put", "constructor"])
@pytest.mark.parametrize("tied", [False, True])
def test_hard_ceiling_keeps_newest_young_rows(tmp_path, context_clock, stage, tied):
    path = str(tmp_path / "context.db")
    store = MessageContextStore(path, max_per_agent=10,
                                hard_cap_per_agent=40 if stage == "put" else 100)
    try:
        for index in range(50):
            store.put(_age_context(str(index)),
                      stored_at=context_clock - 60 + (0 if tied else index))
        if stage == "constructor":
            assert len(_age_row_ids(store)) == 50
            store.close()
            store = MessageContextStore(path, max_per_agent=10, hard_cap_per_agent=40)
        assert _age_row_ids(store) == [str(index) for index in range(10, 50)]
        assert store.hard_cap_per_agent == 40
    finally:
        store.close()


def test_hard_ceiling_is_raised_to_count_limit(tmp_path, context_clock):
    store = MessageContextStore(str(tmp_path / "context.db"), max_per_agent=10, hard_cap_per_agent=3)
    try:
        for index in range(50):
            store.put(_age_context(str(index)), stored_at=context_clock - 60 + index)
        assert _age_row_ids(store) == [str(index) for index in range(40, 50)]
        assert store.hard_cap_per_agent == 10
    finally:
        store.close()
