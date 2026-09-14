"""Durable policy overrides, single-use approvals, and audit receipts."""

from __future__ import annotations

import importlib
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from pinky_daemon.store_catalog import StoreCatalog
from pinky_daemon.store_manifest import derive_fleet_store_manifest


def _store(tmp_path, *, catalog=None):
    try:
        api = importlib.import_module("pinky_daemon.tool_policy_store")
    except ModuleNotFoundError as exc:
        if exc.name != "pinky_daemon.tool_policy_store":
            raise
        pytest.fail("missing tool-policy SQLite store", pytrace=False)
    return api.ToolPolicyStore(str(tmp_path / "tool_policy.db"), catalog=catalog)


def _pending(store, **changes):
    values = dict(agent_name="sample", session_id="session-1", tool_use_id="tool-1",
                  tool_name="Bash", input_sha256="a" * 64, summary="destructive command",
                  created_at=time.time(), deadline_ts=time.time() + 570)
    values.update(changes)
    return store.create_pending(**values)


def _resolve(store, pending_id, **changes):
    values = dict(agent="sample", tool_use_id="tool-1", input_sha256="a" * 64,
                  result="allow", resolved_by="owner:test", reason="reviewed")
    values.update(changes)
    return store.resolve_pending(pending_id, **values)


def _record(store, **changes):
    values = dict(
        agent_name="sample", session_id="session-1", tool_use_id="tool-1", tool_name="Bash",
        evaluation={"evaluated_permission": "deny", "evaluation": {
            "type": "rule", "rule_id": "shell.destructive", "reason_code": "destructive_shell",
            "principal_class": "group", "input_sha256": "a" * 64,
        }}, hook_sha256="b" * 64, settings_sha256="c" * 64,
    )
    values.update(changes)
    return store.record_decision(**values)


def test_pending_id_idempotency_and_no_rebinding(tmp_path):
    store = _store(tmp_path)
    try:
        pending_id = _pending(store)
        assert re.fullmatch(r"tp_[0-9a-f]{16}", pending_id)
        assert _pending(store) == pending_id
        with pytest.raises(ValueError, match="binding"):
            _pending(store, input_sha256="d" * 64)
        row = store.get_pending(pending_id)
        assert row["input_sha256"] == "a" * 64
        assert row["state"] == "pending"
        assert store.count_pending() == 1
    finally:
        store.close()


@pytest.mark.parametrize("changes", [
    {"agent": "other"}, {"tool_use_id": "other"}, {"input_sha256": "b" * 64},
])
def test_resolve_binding_mismatch_does_not_consume_pending(tmp_path, changes):
    store = _store(tmp_path)
    try:
        pending_id = _pending(store)
        assert _resolve(store, pending_id, **changes) == "binding_mismatch"
        assert store.get_pending(pending_id)["state"] == "pending"
        assert _resolve(store, pending_id) == "resolved"
        assert _resolve(store, pending_id, result="deny") == "already_resolved"
        row = store.get_pending(pending_id)
        assert row["result"] == "allow"
        assert row["resolved_by"] == "owner:test"
        assert _resolve(store, "tp_" + "0" * 16) == "not_found"
    finally:
        store.close()


def test_resolve_race_has_one_winner_and_preserves_winner(tmp_path):
    store = _store(tmp_path)
    try:
        pending_id = _pending(store)
        barrier = threading.Barrier(8)

        def resolve(index):
            try:
                barrier.wait(timeout=5)
                return index, _resolve(store, pending_id, resolved_by=f"owner:{index}")
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(resolve, range(8)))
        winners = [index for index, status in outcomes if status == "resolved"]
        assert len(winners) == 1
        assert sum(status == "already_resolved" for _, status in outcomes) == 7
        assert store.get_pending(pending_id)["resolved_by"] == f"owner:{winners[0]}"
    finally:
        store.close()


def test_expire_due_keeps_timeout_receipt_and_rejects_late_allow(tmp_path):
    store = _store(tmp_path)
    try:
        due = _pending(store, created_at=10, deadline_ts=20)
        live = _pending(store, tool_use_id="tool-2", created_at=10, deadline_ts=30)
        assert store.expire_due(19) == []
        assert store.expire_due(20) == [due]
        row = store.get_pending(due)
        assert row["state"] == "resolved"
        assert row["result"] == "deny"
        assert row["resolved_by"] == "timeout"
        assert row["reason"] == "no owner decision within the approval window"
        assert _resolve(store, due) == "already_resolved"
        assert store.expire_due(20) == []
        assert store.get_pending(live)["state"] == "pending"
    finally:
        store.close()


def test_expired_unpolled_pending_cannot_be_approved(tmp_path):
    store = _store(tmp_path)
    try:
        due = _pending(store, created_at=1, deadline_ts=2)
        assert _resolve(store, due) == "already_resolved"
        assert store.get_pending(due)["result"] == "deny"
    finally:
        store.close()


def test_override_expiry_and_agent_scoped_delete(tmp_path):
    store = _store(tmp_path)
    try:
        first = store.put_override(agent="sample", pattern="Bash", rule_id=None,
                                   decision="pause", note="review", created_by="owner:test",
                                   valid_until=20)
        store.put_override(agent="other", pattern="Bash", rule_id=None, decision="allow",
                           note="other scope", created_by="owner:test", valid_until=None)
        assert len(store.list_overrides("sample", 19)) == 1
        assert store.list_overrides("sample", 20) == []
        assert store.delete_override(agent="other", override_id=first) is False
        assert store.delete_override(agent="sample", override_id=first) is True
        assert len(store.list_overrides("other", 30)) == 1
    finally:
        store.close()


def test_allow_counts_and_full_decisions_remain_separate(tmp_path):
    store = _store(tmp_path)
    try:
        for _ in range(3):
            store.bump_allow_count(agent="sample", rule_id="default", day="2026-01-01")
        store.bump_allow_count(agent="other", rule_id="default", day="2026-01-01")
        assert store.list_decisions("sample", 0, 10) == []
        row = store._db.execute(
            "SELECT n FROM tool_policy_allow_counts WHERE agent_name=? AND rule_id=? AND day=?",
            ("sample", "default", "2026-01-01"),
        ).fetchone()
        assert row[0] == 3
        _record(store)
        _record(store, agent_name="other", tool_use_id="tool-2")
        [row] = store.list_decisions("sample", 0, 10)
        assert row["evaluated_permission"] == "deny"
        assert row["eval_type"] == "rule"
        assert row["rule_id"] == "shell.destructive"
        assert row["reason_code"] == "destructive_shell"
        assert row["principal_class"] == "group"
        assert row["input_sha256"] == "a" * 64
        assert row["result"] == "deny"
        assert row["latency_ms"] >= 0
        assert row["hook_sha256"] == "b" * 64
        assert row["settings_sha256"] == "c" * 64
        assert store.list_decisions("sample", time.time() + 10, 10) == []
        assert store.tamper_count_since(0) == 0
        _record(store, tool_use_id="tampered", evaluation={
            "evaluated_permission": "deny", "evaluation": {
                "type": "tamper", "reason_code": "hook_tamper", "principal_class": "group",
                "input_sha256": "d" * 64,
            },
        })
        assert store.tamper_count_since(0) == 1
    finally:
        store.close()


def test_catalog_registration_and_manifest_ownership(tmp_path):
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    store = _store(tmp_path, catalog=catalog)
    try:
        [entry] = catalog.snapshot()
        assert entry.logical_name == "tool_policy"
        assert entry.journal_mode == "wal"
        assert entry.owner == "ToolPolicyStore"
        assert catalog.validate() == []
        manifest = derive_fleet_store_manifest(tmp_path / "conversations.db")
        assert manifest["tool_policy"].criticality == "authority"
        assert manifest["tool_policy"].path == str(tmp_path / "tool_policy.db")
        assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        store.close()
        catalog.close()


def test_hash_columns_migrate_existing_decision_table_without_losing_rows(tmp_path):
    with closing(sqlite3.connect(tmp_path / "tool_policy.db")) as connection:
        connection.execute("""CREATE TABLE tool_policy_decisions (
            id INTEGER PRIMARY KEY, agent_name TEXT, session_id TEXT, tool_use_id TEXT,
            tool_name TEXT, evaluated_permission TEXT, eval_type TEXT, rule_id TEXT,
            reason_code TEXT, principal_class TEXT, input_sha256 TEXT, result TEXT,
            resolved_by TEXT, latency_ms INTEGER, created_at REAL)""")
        connection.execute("""INSERT INTO tool_policy_decisions
            (id,agent_name,evaluated_permission,created_at) VALUES(1,'sample','deny',1)""")
        connection.commit()
    store = _store(tmp_path)
    try:
        columns = {row[1]: row for row in store._db.execute("PRAGMA table_info(tool_policy_decisions)")}
        for column in ("hook_sha256", "settings_sha256"):
            assert columns[column][2].upper() == "TEXT"
            assert columns[column][3] == 0
        [row] = store.list_decisions("sample", 0, 10)
        assert row["id"] == 1
        assert row["hook_sha256"] is None
        assert row["settings_sha256"] is None
        pending_columns = {row[1] for row in store._db.execute("PRAGMA table_info(tool_policy_pending)")}
        assert not {"hook_sha256", "settings_sha256"} & pending_columns
    finally:
        store.close()


def test_thread_local_connections_and_atomic_allow_counter(tmp_path):
    store = _store(tmp_path)
    barrier = threading.Barrier(6)
    connections_ready = threading.Barrier(6)

    def hammer(index):
        try:
            barrier.wait(timeout=5)
            connection_id = id(store._db)
            connections_ready.wait(timeout=5)
            for turn in range(10):
                store.bump_allow_count(agent="sample", rule_id="default", day="2026-01-01")
                pending_id = _pending(store, tool_use_id=f"{index}-{turn}")
                assert store.get_pending(pending_id)["tool_use_id"] == f"{index}-{turn}"
            return connection_id
        finally:
            store.close()

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(hammer, range(6)))
        assert len(set(ids)) == 6
        assert store.count_pending() == 60
        assert store._db.execute("SELECT n FROM tool_policy_allow_counts").fetchone()[0] == 60
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["resolve", "expire"])
def test_pending_transition_is_one_conditional_update_without_state_preread(
    tmp_path, monkeypatch, operation
):
    store = _store(tmp_path)
    try:
        pending_id = _pending(store, deadline_ts=time.time() + (570 if operation == "resolve" else -1))
        connection = store._db
        statements = []

        class ConnectionProbe:
            def execute(self, sql, parameters=()):
                statements.append((sql, parameters))
                return connection.execute(sql, parameters)

            def cursor(self, *args, **kwargs):
                raise AssertionError("pending CAS must use the observed connection.execute")

            def executemany(self, *args, **kwargs):
                raise AssertionError("pending CAS must be a single UPDATE")

            def executescript(self, *args, **kwargs):
                raise AssertionError("pending CAS must be a single UPDATE")

            def __getattr__(self, name):
                return getattr(connection, name)

            def __enter__(self):
                connection.__enter__()
                return self

            def __exit__(self, *args):
                return connection.__exit__(*args)

        # Probe the public connection seam, independent of its thread-local storage name.
        with monkeypatch.context() as patch:
            patch.setattr(type(store), "_db", property(lambda _: ConnectionProbe()))
            if operation == "resolve":
                assert _resolve(store, pending_id) == "resolved"
            else:
                assert store.expire_due(time.time()) == [pending_id]
        normalized = [(" ".join(sql.lower().split()), args) for sql, args in statements]
        updates = [(i, sql, args) for i, (sql, args) in enumerate(normalized)
                   if sql.startswith("update ")]
        assert len(updates) == 1, normalized
        index, sql, args = updates[0]
        assert "update tool_policy_pending" in sql
        assert " where " in sql
        where = sql.split(" where ", 1)[1]
        # Accept literal or bound values, but pin every predicate to the WHERE clause.
        assert re.search(r"\bstate\s*=\s*(?:'pending'|[?:])", where), sql
        if "'pending'" not in where:
            assert "pending" in (args.values() if isinstance(args, dict) else args)
        assert re.search(r"\bdeadline_ts\s*" + (r">" if operation == "resolve" else r"<="), where)
        if operation == "resolve":
            for column in ("pending_id", "agent_name", "tool_use_id", "input_sha256"):
                assert re.search(r"\b" + column + r"\s*=", where), sql
            values = args.values() if isinstance(args, dict) else args
            for value in (pending_id, "sample", "tool-1", "a" * 64):
                assert value in values
        assert not any(sql.startswith("select ") and "tool_policy_pending" in sql
                       for sql, _ in normalized[:index]), normalized
    finally:
        store.close()
