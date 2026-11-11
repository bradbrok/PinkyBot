"""The rename decision retains the exact recorded-value read."""

import ast
import inspect
import itertools
import json
import sqlite3
import textwrap
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.session_identity import session_ownership
from tests.isolated_policy_support import daemon as daemon
from tests.test_label_rename_claims import _claim_decision
from tests.test_label_rename_refresh import _capture

pytestmark = pytest.mark.real_auth


def test_actual_guard_refuses_each_id_rejected_by_the_read_rule(daemon):
    instance = daemon("off")
    decision = _claim_decision(instance)
    body = ast.parse(textwrap.dedent(inspect.getsource(decision)))
    if decision.__name__ == "_rename_claim_refused":
        condition = next(node.value for node in ast.walk(body) if isinstance(node, ast.Return))
    else:
        condition = next(
            node.test
            for node in ast.walk(body)
            if isinstance(node, ast.If)
            and any(
                isinstance(inner, ast.Constant)
                and isinstance(inner.value, str)
                and "cannot be renamed because another session claim exists." in inner.value
                for inner in ast.walk(node)
            )
        )
    predicate = compile(ast.Expression(condition), "<rename guard>", "eval")
    owners = ("a", "a-b", "a-b-c")
    target = "a-b-c-notes"
    count = 0
    fallback = 0
    stricter = 0
    for caller, bits, live_owner in itertools.product(owners, range(512), ("", *owners, "outside")):
        brokers = {}
        labels = []
        records = []
        for i, owner in enumerate(owners):
            label = target[len(owner) + 1 :]
            if bits & (1 << (i * 3)):
                brokers[owner] = {label: object()}
            if bits & (1 << (i * 3 + 1)):
                labels.append({"agent_name": owner, "label": label})
            if bits & (1 << (i * 3 + 2)):
                records.append((owner, target))

        class Registry:
            def list(self, *, include_retired=False):
                assert include_retired is True
                return [SimpleNamespace(name=name) for name in (*owners, "outside")]

            def list_streaming_session_ids(self, name):
                return [
                    {"label": "recorded", "session_id": sid}
                    for owner, sid in records
                    if owner == name
                ]

            def list_streaming_session_labels(self, names):
                return [row for row in labels if row["agent_name"] in names]

            def has_streaming_session_id_claim(self, names, ids):
                assert caller not in names
                return any(owner in names and sid in ids for owner, sid in records)

        registry = Registry()
        legacy = [SimpleNamespace(id=target, agent_name=live_owner)] if live_owner else []
        ownership = session_ownership(
            registry, caller, live_sessions=legacy, broker_sessions=brokers
        )
        session_ids = (target, caller + "-safe-unused")
        denied = bool(
            eval(
                predicate,
                {
                    "ownership": ownership,
                    "session_ids": session_ids,
                    "name": caller,
                    "agents": registry,
                },
            )
        )
        explicit_foreign = bool(ownership.explicit_owners.get(target, frozenset()) - {caller})
        foreign_record = any(owner != caller for owner, sid in records)
        expected = (
            not all(ownership.keeps(sid) for sid in session_ids)
            or bool(live_owner and live_owner != caller)
            or explicit_foreign
            or foreign_record
        )
        assert denied == expected, (caller, bits, live_owner)
        if not ownership.keeps(target) and not denied:
            assert not ownership.explicit_owners.get(target)
            assert not live_owner
            assert caller != "a-b-c"
            assert target not in ownership.recorded_ids
            fallback += 1
        if denied and ownership.keeps(target):
            stricter += 1
        count += 1
    assert count == 7680
    assert fallback == 0 and stricter > 0
    _capture(
        "guard-oracle",
        {"states": count, "read_denials_allowed": fallback, "guard_stricter_than_keeps": stricter},
    )


def test_actual_sql_plan_four_bindings_and_parameterized_exact_values(tmp_path):
    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    try:
        for name in ("unit", "unit-peer", "other"):
            registry.register(name, working_dir=str(tmp_path / name))
        marker = 'id%_\\"漢\x00tail'
        registry.set_streaming_session_id("unit-peer", marker, label="stored")
        method = ast.parse(
            textwrap.dedent(inspect.getsource(AgentRegistry.has_streaming_session_id_claim))
        )
        sql = next(
            node.args[0].value
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        )
        names = ["unit-peer", *("unit-" + str(i) for i in range(2000))]
        ids = [marker, *("id-" + str(i) for i in range(2000))]
        bindings = (json.dumps(names), json.dumps(ids), json.dumps(names), json.dumps(ids))
        plan = [tuple(row) for row in registry._db.execute("EXPLAIN QUERY PLAN " + sql, bindings)]
        previous = registry._db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
        changes = registry._db.total_changes
        try:
            assert registry.has_streaming_session_id_claim(names, ids)
            assert not registry.has_streaming_session_id_claim(names, ids[1:])
            assert not registry.has_streaming_session_id_claim(["unit"], ids)
            assert not registry.has_streaming_session_id_claim(names, [marker.upper()])
        finally:
            registry._db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
        assert registry._db.total_changes == changes
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(registry.has_streaming_session_id_claim, names, ids).result(
                timeout=2
            )
        _capture(
            "query-plan",
            {
                "sqlite": sqlite3.sqlite_version,
                "plan": plan,
                "parameter_limit": 4,
                "names": len(names),
                "ids": len(ids),
                "total_changes_unchanged": True,
                "thread_control": True,
            },
        )
    finally:
        registry.close()
