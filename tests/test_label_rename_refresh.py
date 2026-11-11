"""A rename checks both ids again after a suspended settlement."""

import asyncio
import inspect
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_label_rename_claims import (
    _assert_claim_refusal,
    _assert_unchanged,
    _claim_decision,
    _own_session,
    _patch,
    _rename_cells,
    _state,
)
from tests.test_session_owner_reads import _register
from tests.test_streaming_session_owner_reads import streaming_session

pytestmark = pytest.mark.real_auth

KINDS = ("broker", "label", "recorded", "actual-post")


def _capture(name, data):
    root = Path(os.environ["TMPDIR"]) / "rename-captures"
    root.mkdir(exist_ok=True)
    (root / (name + ".json")).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _recorded_claim(instance, names, ids):
    return instance.agents.has_streaming_session_id_claim(names, ids)


async def _add_claim(instance, monkeypatch, client, position, kind, target):
    other_label = "old" if position == "old" else "new"
    if kind == "actual-post":

        async def fixture_start(name, *, label="main", resume_id=""):
            assert name == "tenant-peer"
            return streaming_session(instance, name, label)

        replace_cell(
            monkeypatch,
            inspect.unwrap(closure(instance.app, "create_streaming_session")),
            "_start_streaming_session",
            fixture_start,
        )
        other_path = "/agents/tenant-peer/streaming-sessions"
        created = await client.post(
            other_path + "?label=" + other_label,
            headers=signed(instance, "POST", other_path, "normal"),
        )
        assert created.status_code == 200, created.text
        other = instance.app.state.broker.get_streaming_session("tenant-peer", other_label)
        assert await other.send("renameclaimneedle late foreign row")
    elif kind == "broker":
        other = streaming_session(instance, "tenant-peer", other_label)
        assert await other.send("renameclaimneedle late foreign row")
    else:
        instance.agents.set_streaming_session_id(
            "tenant-peer",
            "" if kind == "label" else target,
            label=other_label if kind == "label" else "recorded",
        )
        instance.app.state.conversation_store.append(
            target,
            "assistant",
            "renameclaimneedle late foreign row",
            timestamp=17,
        )


async def _late_claim_after_settle(daemon, monkeypatch, position, kind, *, call_one=False):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    session = await _own_session(instance, "peer-old")
    assert await session.send("renameclaimneedle original own row")
    new_label = "after-rename" if position == "old" else "peer-new"
    old_id, new_id = session.id, "tenant-" + new_label
    target = old_id if position == "old" else new_id
    if position == "new" and not call_one:
        instance.agents.set_streaming_session_id("tenant", "", label=new_label)
    initial = closure(instance.app, "_agent_session_ownership")("tenant")
    if call_one:
        assert not initial.keeps(new_id)
    else:
        assert initial.keeps(old_id) and initial.keeps(new_id)
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    entered, release = asyncio.Event(), asyncio.Event()

    class Debt:
        _replacement_cleanup_strict = False
        _recovery_retired = False
        disconnected = 0

        async def _quiesce_recovery_owner(self):
            assert self._replacement_cleanup_strict is True
            assert self._recovery_retired is True
            entered.set()
            await release.wait()

        async def disconnect(self):
            self.disconnected += 1

    settle = closure(instance.app, "_settle_startup_debt")
    debt_map = inspect.getclosurevars(settle).nonlocals["_candidate_cleanup_debts"]
    debt = Debt()
    debt_map[("tenant", new_label)] = debt
    before = _state(instance, session, old_id, new_id)
    path = "/agents/tenant/streaming-sessions/peer-old"
    transport = httpx.ASGITransport(app=instance.app)
    would_refuse = None
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        pending = asyncio.create_task(
            client.patch(
                path,
                json={"label": new_label},
                headers=signed(instance, "PATCH", path),
            )
        )
        observed = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait(
                {pending, observed},
                timeout=3,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert done, "rename neither entered settlement nor returned"
            if not call_one:
                assert entered.is_set(), "first check refused before settlement"
            if entered.is_set():
                assert not pending.done()
                await _add_claim(instance, monkeypatch, client, position, kind, target)
                snapshot = closure(instance.app, "_agent_session_ownership")("tenant")
                would_refuse = any(
                    snapshot.live_owners.get(sid, "") not in ("", "tenant")
                    or snapshot.explicit_owners.get(sid, frozenset()) - {"tenant"}
                    for sid in (old_id, new_id)
                ) or _recorded_claim(
                    instance,
                    snapshot.family_names - {"tenant"},
                    (old_id, new_id),
                )
                assert would_refuse
                before = _state(instance, session, old_id, new_id)
        finally:
            release.set()
            observed.cancel()
            await asyncio.gather(observed, return_exceptions=True)
        response = await asyncio.wait_for(pending, 3)
        read_path = "/agents/tenant/chat-history"
        history = await client.get(read_path, headers=signed(instance, "GET", read_path))
    after = _state(instance, session, old_id, new_id)
    _capture(
        ("early-" if call_one else "late-") + kind + "-" + position,
        {
            "status": response.status_code,
            "body": response.text,
            "entered": entered.is_set(),
            "would_refuse_before_release": bool(would_refuse),
            "before_old": before["old_rows"],
            "before_new": before["new_rows"],
            "after_old": after["old_rows"],
            "after_new": after["new_rows"],
            "final_label": session._config.label,
            "tenant_history": history.json(),
            "startup_debt_removed": ("tenant", new_label) not in debt_map,
        },
    )
    if call_one:
        assert not entered.is_set(), "settlement ran before the first refusal"
        assert debt_map[("tenant", new_label)] is debt
        assert debt._replacement_cleanup_strict is False
        assert debt._recovery_retired is False
        assert debt.disconnected == 0
    else:
        assert entered.is_set()
        assert debt._replacement_cleanup_strict is False
        assert debt._recovery_retired is True
        assert debt.disconnected == 1
        assert ("tenant", new_label) not in debt_map
    assert response.status_code == 409, response.text
    _assert_claim_refusal(response, "peer-old")
    _assert_unchanged(instance, session, old_id, new_id, before)


@pytest.mark.parametrize("kind", KINDS)
async def test_late_old_id_claim_refuses_after_settle(daemon, monkeypatch, kind):
    await _late_claim_after_settle(daemon, monkeypatch, "old", kind)


@pytest.mark.parametrize("kind", KINDS)
async def test_late_destination_claim_with_own_empty_label_refuses_after_settle(
    daemon,
    monkeypatch,
    kind,
):
    await _late_claim_after_settle(daemon, monkeypatch, "new", kind)


@pytest.mark.parametrize("kind", KINDS)
async def test_destination_without_own_label_refuses_before_settle(daemon, monkeypatch, kind):
    await _late_claim_after_settle(daemon, monkeypatch, "new", kind, call_one=True)


@pytest.mark.parametrize("query", ["", "?q=renameclaimneedle"], ids=["plain", "q"])
async def test_text_only_destination_preserves_retained_rows(daemon, monkeypatch, query):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    session = await _own_session(instance, "notes")
    assert await session.send("renameclaimneedle own current row")
    other = streaming_session(instance, "tenant-peer", "unused")
    assert await other.send("renameclaimneedle retained foreign row")
    instance.app.state.broker.unregister_streaming("tenant-peer", label="unused")
    old_id, target = session.id, "tenant-peer-unused"
    ownership = closure(instance.app, "_agent_session_ownership")("tenant")
    assert not ownership.keeps(target)
    assert ownership.explicit_owners.get(target, frozenset()) == frozenset()
    assert not _recorded_claim(instance, {"tenant-peer"}, {target})
    before = _state(instance, session, old_id, target)
    history_before = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance.app),
        base_url="http://fixture",
    ) as client:
        for owner in ("tenant", "tenant-peer"):
            path = "/agents/" + owner + "/chat-history"
            history_before[owner] = (
                await client.get(
                    path + query,
                    headers=signed(instance, "GET", path, owner),
                )
            ).json()
        path = "/agents/tenant/streaming-sessions/notes"
        response = await client.patch(
            path,
            json={"label": "peer-unused"},
            headers=signed(instance, "PATCH", path),
        )
        path = "/agents/tenant/chat-history"
        after = (await client.get(path + query, headers=signed(instance, "GET", path))).json()
    _capture(
        "text-only-" + ("plain" if not query else "q"),
        {
            "status": response.status_code,
            "before": history_before,
            "after": after,
            "final_rows": [
                row.to_dict() for row in instance.app.state.conversation_store.get_history(target)
            ],
        },
    )
    assert not any("retained foreign row" in row["content"] for row in after["messages"])
    _assert_claim_refusal(response, "notes")
    _assert_unchanged(instance, session, old_id, target, before)


@pytest.mark.parametrize("query", ["", "?q=renameclaimneedle"], ids=["plain", "q"])
async def test_old_id_read_denial_preserves_rows_with_a_structural_snapshot(
    daemon,
    monkeypatch,
    query,
):
    instance = daemon("off")
    _register(instance, "tenant-peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    session = await _own_session(instance, "peer-unused")
    assert await session.send("renameclaimneedle own current row")
    other = streaming_session(instance, "tenant-peer", "unused")
    assert await other.send("renameclaimneedle retained foreign row")
    instance.app.state.broker.unregister_streaming("tenant-peer", label="unused")
    old_id, new_id = session.id, "tenant-renamed"
    original = closure(instance.app, "_agent_session_ownership")

    def without_old_alias(name, *args, **kwargs):
        snapshot = original(name, *args, **kwargs)
        if name == "tenant":
            explicit = dict(snapshot.explicit_owners)
            explicit.pop(old_id, None)
            return replace(snapshot, explicit_owners=explicit)
        return snapshot

    replace_cell(
        monkeypatch, _claim_decision(instance), "_agent_session_ownership", without_old_alias
    )
    ownership = without_old_alias("tenant")
    assert not ownership.keeps(old_id)
    assert ownership.explicit_owners.get(old_id, frozenset()) == frozenset()
    assert not _recorded_claim(instance, {"tenant-peer"}, {old_id})
    before = _state(instance, session, old_id, new_id)
    path = "/agents/tenant/chat-history"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance.app),
        base_url="http://fixture",
    ) as client:
        first = await client.get(path + query, headers=signed(instance, "GET", path))
        assert not any("retained foreign row" in row["content"] for row in first.json()["messages"])
        rename_path = "/agents/tenant/streaming-sessions/peer-unused"
        response = await client.patch(
            rename_path,
            json={"label": "renamed"},
            headers=signed(instance, "PATCH", rename_path),
        )
        after = (await client.get(path + query, headers=signed(instance, "GET", path))).json()
    _capture(
        "old-text-structural-" + ("plain" if not query else "q"),
        {
            "status": response.status_code,
            "before_old": before["old_rows"],
            "before_new": before["new_rows"],
            "after": after,
        },
    )
    assert not any("retained foreign row" in row["content"] for row in after["messages"])
    _assert_claim_refusal(response, "peer-unused")
    _assert_unchanged(instance, session, old_id, new_id, before)


@pytest.mark.parametrize(
    "family_case",
    [
        ("tenant-peer", "tenant", "peer-"),
        ("tenant-peer-child", "tenant", "peer-child-"),
        ("tenant", "tenant-peer-child", ""),
    ],
)
@pytest.mark.parametrize("retired", [False, True])
async def test_reverse_and_three_deep_families_refuse(daemon, monkeypatch, family_case, retired):
    instance = daemon("off")
    caller, other, prefix = family_case
    for name in {caller, other} - {"tenant"}:
        _register(instance, name)
    label = "notes" if caller != "tenant" else "peer-child-notes"
    session = streaming_session(instance, caller, label)
    instance.agents.set_streaming_session_id(caller, session.resume_handle, label=label)
    other_label = (prefix + "notes") if caller != "tenant" else "notes"
    instance.agents.set_streaming_session_id(other, "", label=other_label)
    if retired:
        instance.agents.retire(other)
    p = "/agents/" + caller + "/streaming-sessions/" + label
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance.app), base_url="http://fixture"
    ) as client:
        response = await client.patch(
            p, json={"label": "renamed"}, headers=signed(instance, "PATCH", p, "normal")
        )
    assert response.status_code == 409, response.text
    assert session._config.label == label


async def test_callback_factory_has_no_actual_suspension(daemon):
    instance = daemon("off")
    factory = _rename_cells(instance)["_make_streaming_resume_handle_callback"]
    import ast

    body = ast.parse(__import__("textwrap").dedent(inspect.getsource(factory)))
    assert not any(
        isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for node in ast.walk(body)
    )
    coroutine = factory("tenant", "notes")
    with pytest.raises(StopIteration) as result:
        coroutine.send(None)
    assert callable(result.value.value)


async def test_fixed_clock_success_capture(daemon, monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: 1794370000.0)
    instance = daemon("off")
    _register(instance, "tenant-peer")
    session = await _own_session(instance, "notes")
    assert await session.send("renameclaimneedle fixed clock success")
    response = _patch(instance, "notes", "renamed")
    assert response.status_code == 200, response.text
    store = instance.app.state.conversation_store
    _capture(
        "success-fixed",
        {
            "body_hex": response.content.hex(),
            "old_rows": [row.to_dict() for row in store.get_history("tenant-notes")],
            "new_rows": [row.to_dict() for row in store.get_history("tenant-renamed")],
            "labels": [
                tuple(row)
                for row in instance.agents._db.execute(
                    "SELECT agent_name,label,session_id,updated_at FROM streaming_session_labels ORDER BY agent_name,label"
                )
            ],
            "fts": [row.to_dict() for row in store.search("renameclaimneedle")],
            "label": session._config.label,
        },
    )


@asynccontextmanager
async def _blocked_rename(daemon, monkeypatch):
    instance = daemon("off")
    session = await _own_session(instance, "notes")
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    entered, release = asyncio.Event(), asyncio.Event()

    class Debt:
        async def _quiesce_recovery_owner(self):
            entered.set()
            await release.wait()

        async def disconnect(self):
            return None

    cells = inspect.getclosurevars(closure(instance.app, "_settle_startup_debt")).nonlocals
    cells["_candidate_cleanup_debts"][("tenant", "renamed")] = Debt()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance.app, raise_app_exceptions=False),
        base_url="http://fixture",
    ) as client:
        path = "/agents/tenant/streaming-sessions/notes"
        task = asyncio.create_task(
            client.patch(path, json={"label": "renamed"}, headers=signed(instance, "PATCH", path)),
            name="rename-probe",
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            yield instance, session, client, task, release
        finally:
            release.set()
            if not task.done():
                await asyncio.wait_for(task, 3)


@pytest.mark.parametrize("operation", ["delete-old", "create-new"])
async def test_real_same_agent_mutations_wait_for_lifecycle_lock(daemon, monkeypatch, operation):
    async with _blocked_rename(daemon, monkeypatch) as (instance, session, client, rename, release):
        wrapped = closure(instance.app, "rename_streaming_session")
        real = inspect.getclosurevars(wrapped).nonlocals["_lifecycle"]
        attempted = asyncio.Event()

        @asynccontextmanager
        async def observe(name):
            if name == "tenant":
                attempted.set()
            async with real(name):
                yield

        replace_cell(monkeypatch, wrapped, "_lifecycle", observe)
        if operation == "delete-old":
            p = "/agents/tenant/streaming-sessions/notes"
            method = "DELETE"
            expected = 404
            request = client.delete(p, headers=signed(instance, method, p, "normal"))
        else:
            p = "/agents/tenant/streaming-sessions"
            method = "POST"
            expected = 409
            request = client.post(
                p + "?label=renamed", headers=signed(instance, method, p, "normal")
            )
        other = asyncio.create_task(request, name="competing-probe")
        await asyncio.wait_for(attempted.wait(), 3)
        assert not other.done()
        assert session._config.label == "notes"
        release.set()
        renamed = await asyncio.wait_for(rename, 3)
        result = await asyncio.wait_for(other, 3)
        _capture(
            "same-agent-" + operation,
            {
                "rename_status": renamed.status_code,
                "other_status": result.status_code,
                "other_body": result.text,
                "blocked_before_release": True,
            },
        )
        assert renamed.status_code == 200, renamed.text
        assert result.status_code == expected, result.text
