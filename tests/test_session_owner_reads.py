"""Agent reads and lifecycle attribution follow the available owner evidence."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.isolated_policy_support import closure, signed
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_read_enforce import _read

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("suffix", ["", "?q=ownershipneedle"])
def test_chat_history_does_not_include_a_longer_agents_rows(daemon, monkeypatch, suffix):
    instance = daemon()
    work = instance.root / "tenant-peer"
    work.mkdir()
    instance.agents.register("tenant-peer", isolated=True, working_dir=str(work), groups=["other"])
    store = instance.app.state.conversation_store
    store.append("tenant-main", "assistant", "Own history fixture")
    store.append("tenant-peer-main", "assistant", "ownershipneedle foreign fixture")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(instance, "/agents/tenant/chat-history" + suffix)
    assert response.status_code == 200
    assert not any("ownershipneedle" in row["content"] for row in response.json()["messages"])


def test_sessions_do_not_include_a_longer_agents_live_session(daemon, monkeypatch):
    instance = daemon()
    work = instance.root / "tenant-peer"
    work.mkdir()
    instance.agents.register("tenant-peer", isolated=True, working_dir=str(work), groups=["other"])
    instance.app.state.manager.create(
        session_id="tenant-peer-main",
        agent_name="tenant-peer",
        soul="foreign-session-body-fixture",
        working_dir=str(work),
    )
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(instance, "/agents/tenant/sessions")
    assert response.status_code == 200
    assert "foreign-session-body-fixture" not in response.text


@pytest.mark.asyncio
async def test_incremental_stream_separates_agent_and_label(daemon, monkeypatch):
    from tests.test_isolated_read_shadow import _request, _shadow_check

    instance = daemon()
    work = instance.root / "tenant-peer"
    work.mkdir()
    instance.agents.register("tenant-peer", isolated=True, working_dir=str(work), groups=["other"])
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert not _shadow_check(instance)(
        _request(instance, "/agents/tenant/streaming/events"), "tenant"
    )
    response = await closure(instance.app, "stream_agent_events")("tenant", label="peer-main")
    foreign = await closure(instance.app, "_make_streaming_event_callback")("tenant-peer", "main")
    own = await closure(instance.app, "_make_streaming_event_callback")("tenant", "peer-main")
    await foreign({"type": "delta", "text": "foreign-stream-body-fixture"})
    await own({"type": "delta", "text": "own-stream-body-fixture"})
    try:
        event = await asyncio.wait_for(anext(response.body_iterator), 0.5)
    finally:
        await response.body_iterator.aclose()
    assert "foreign-stream-body-fixture" not in event
    assert "own-stream-body-fixture" in event


@pytest.mark.parametrize(
    "session_id,registered_owner,expected",
    [
        ("worker-name-main", "worker-name", "worker-name"),
        ("unlisted-main", "", ""),
        ("tenant-main", "", "tenant"),
    ],
    ids=["hyphenated-owner", "unowned", "unchanged-owner"],
)
def test_event_attribution_uses_the_registered_owner(
    daemon,
    session_id,
    registered_owner,
    expected,
):
    instance = daemon()
    if registered_owner:
        work = instance.root / registered_owner
        work.mkdir()
        instance.agents.register(registered_owner, working_dir=str(work))
    path = "/sessions/" + session_id + "/events"
    client = TestClient(instance.app)
    try:
        response = client.post(
            path,
            json={"event_type": "session_start"},
            headers=signed(instance, "POST", path, "normal"),
        )
    finally:
        client.close()
    assert response.status_code == 200
    assert set(response.json()) == {"ok", "id", "session_id", "event_type"}
    assert response.json()["ok"] is True
    events = _read(instance, path, name="normal")
    assert events.status_code == 200
    assert events.json()["count"] == 1
    assert events.json()["events"][0]["agent_name"] == expected


def _register(instance, name):
    work = instance.root / name
    work.mkdir()
    instance.agents.register(name, isolated=True, working_dir=str(work))


def _post(instance, path, caller="tenant", body=None):
    client = TestClient(instance.app)
    try:
        return client.post(path, json=body or {}, headers=signed(instance, "POST", path, caller))
    finally:
        client.close()


def _owner_post(instance, path):
    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
    from tests.conftest import TEST_SESSION_SECRET

    client = TestClient(instance.app)
    client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
    try:
        return client.post(path, json={})
    finally:
        client.close()


def _collect(instance, name):
    import inspect

    resolver = instance.app.state.agent_history_resolver
    return inspect.getclosurevars(resolver).nonlocals["_collect_agent_session_ids"](name)


def _refresh_calls(instance, monkeypatch):
    from pinky_daemon import api

    calls = []
    manager = instance.app.state.manager
    monkeypatch.setattr(api, "_write_mcp_json", lambda *args, **kwargs: None)

    def refresh(sid):
        calls.append(sid)
        return manager.get(sid)

    monkeypatch.setattr(manager, "refresh", refresh)
    return calls


def _last_message(instance, name):
    from pinky_daemon.autonomy import AutonomyEngine

    engine = AutonomyEngine(
        instance.agents,
        None,
        instance.app.state.conversation_store,
        session_ownership_provider=closure(instance.app, "_agent_session_ownership"),
    )
    return engine._last_message_time(name)


@pytest.mark.parametrize("caller", ["tenant", "tenant-peer"])
@pytest.mark.parametrize("site", ["sessions", "collect", "history", "search", "refresh", "last"])
def test_prefix_family_retains_only_each_agents_candidates(daemon, monkeypatch, caller, site):
    instance = daemon()
    _register(instance, "tenant-peer")
    manager, store = instance.app.state.manager, instance.app.state.conversation_store
    for name, stamp in [("tenant", 10), ("tenant-peer", 20)]:
        manager.create(session_id=name + "-main", agent_name=name, working_dir="fixture-work")
        store.append(name + "-main", "user", "needle " + name, timestamp=stamp)
    path = "/agents/" + caller
    expected = {caller + "-main"}
    if site == "sessions":
        response = _read(instance, path + "/sessions", name=caller)
        assert response.status_code == 200
        actual = {row["id"] for row in response.json()["sessions"]}
    elif site == "collect":
        actual = _collect(instance, caller)
    elif site in {"history", "search"}:
        response = _read(
            instance,
            path + "/chat-history" + ("?q=needle" if site == "search" else ""),
            name=caller,
        )
        assert response.status_code == 200
        actual = {row["session_id"] for row in response.json()["messages"]}
    elif site == "refresh":
        calls = _refresh_calls(instance, monkeypatch)
        response = _owner_post(instance, path + "/sessions/refresh")
        assert response.status_code == 200
        assert set(response.json()["refreshed_sessions"]) == expected
        actual = set(calls)
    else:
        assert _last_message(instance, caller) == (10 if caller == "tenant" else 20)
        return
    assert actual == expected


@pytest.mark.parametrize("caller", ["tenant", "peer"])
@pytest.mark.parametrize("site", ["sessions", "collect", "history", "search", "refresh", "last"])
def test_live_owner_overrules_a_conflicting_id_prefix(daemon, monkeypatch, caller, site):
    instance = daemon()
    manager, store = instance.app.state.manager, instance.app.state.conversation_store
    manager.create(session_id="tenant-main", agent_name="peer", working_dir="fixture-work")
    manager.create(session_id="tenant-own", agent_name="tenant", working_dir="fixture-work")
    store.append("tenant-main", "user", "needle peer-owned fixture", timestamp=99)
    store.append("tenant-own", "user", "needle own fixture", timestamp=10)
    expected = {"tenant-own"} if caller == "tenant" else {"tenant-main"}
    path = "/agents/" + caller
    if site == "sessions":
        response = _read(instance, path + "/sessions", name=caller)
        assert response.status_code == 200
        actual = {row["id"] for row in response.json()["sessions"]}
    elif site == "collect":
        actual = _collect(instance, caller)
    elif site in {"history", "search"}:
        response = _read(
            instance,
            path + "/chat-history" + ("?q=needle" if site == "search" else ""),
            name=caller,
        )
        assert response.status_code == 200
        actual = {row["session_id"] for row in response.json()["messages"]}
    elif site == "refresh":
        calls = _refresh_calls(instance, monkeypatch)
        response = _owner_post(instance, path + "/sessions/refresh")
        assert response.status_code == 200
        assert set(response.json()["refreshed_sessions"]) == expected
        actual = set(calls)
    else:
        # The timestamp primitive never widens beyond its original prefix.
        assert _last_message(instance, caller) == (10 if caller == "tenant" else None)
        return
    assert actual == expected


@pytest.mark.parametrize("caller", ["a", "a-b", "a-b-c"])
@pytest.mark.parametrize("query", ["", "?q=needle"])
def test_three_deep_names_keep_the_longest_registered_owner(daemon, caller, query):
    instance = daemon()
    for name in ["a", "a-b", "a-b-c"]:
        _register(instance, name)
        instance.app.state.conversation_store.append(name + "-main", "user", "needle " + name)
    response = _read(instance, "/agents/" + caller + "/chat-history" + query, name=caller)
    assert response.status_code == 200
    assert {row["session_id"] for row in response.json()["messages"]} == {caller + "-main"}


@pytest.mark.parametrize("retired", [False, True], ids=["unregistered", "retired"])
@pytest.mark.parametrize("query", ["", "?q=needle"])
def test_retired_name_remains_an_owner_but_an_unregistered_name_does_not(daemon, retired, query):
    instance = daemon()
    if retired:
        _register(instance, "tenant-former")
        assert instance.agents.retire("tenant-former")
    instance.app.state.conversation_store.append("tenant-former-main", "user", "needle fixture")
    response = _read(instance, "/agents/tenant/chat-history" + query)
    assert response.status_code == 200
    assert [row["session_id"] for row in response.json()["messages"]] == (
        [] if retired else ["tenant-former-main"]
    )
    if retired:
        longer = _read(instance, "/agents/tenant-former/chat-history" + query, name="normal")
        assert longer.status_code == 200
        assert [row["session_id"] for row in longer.json()["messages"]] == ["tenant-former-main"]


@pytest.mark.parametrize("query", ["", "?q=needle"])
def test_empty_live_owner_uses_the_registered_name_family(daemon, query):
    instance = daemon()
    _register(instance, "tenant-peer")
    instance.app.state.manager.create(
        session_id="tenant-peer-main",
        agent_name="",
        working_dir="fixture-work",
    )
    instance.app.state.conversation_store.append("tenant-peer-main", "user", "needle fixture")
    short = _read(instance, "/agents/tenant/chat-history" + query)
    long = _read(instance, "/agents/tenant-peer/chat-history" + query, name="tenant-peer")
    assert short.status_code == long.status_code == 200
    assert short.json()["messages"] == []
    assert [row["session_id"] for row in long.json()["messages"]] == ["tenant-peer-main"]
    assert _read(instance, "/agents/tenant/sessions").json()["sessions"] == []
    assert _read(instance, "/agents/tenant-peer/sessions", name="tenant-peer").json()["count"] == 1


@pytest.mark.parametrize("failed_read", [False, True])
def test_family_exclusions_escape_like_wildcards(daemon, monkeypatch, failed_read):
    instance = daemon()
    name = "unit_1"
    store = instance.app.state.conversation_store
    for owner in [name, "unit_1-kind_1", "unitx1"]:
        _register(instance, owner)
        store.append(owner + "-main", "user", "needle fixture", timestamp=10)
    store.append("unit_1-kindx1-main", "user", "needle owned sibling", timestamp=20)
    if failed_read:

        def unavailable(prefix):
            raise OSError("fixture prefix read failure")

        monkeypatch.setattr(store, "list_session_ids_with_prefix", unavailable)
    response = _read(instance, "/agents/" + name + "/chat-history?q=needle", name=name)
    assert response.status_code == 200
    assert {row["session_id"] for row in response.json()["messages"]} == {
        "unit_1-main",
        "unit_1-kindx1-main",
    }


@pytest.mark.parametrize(
    "query,failed_read", [("", False), ("?q=needle", False), ("?q=needle", True)]
)
def test_family_exclusions_run_before_the_history_limit(daemon, monkeypatch, query, failed_read):
    instance = daemon()
    _register(instance, "tenant-peer")
    store = instance.app.state.conversation_store
    own = [store.append("tenant-main", "user", "needle own", timestamp=n + 1) for n in range(3)]
    for n in range(12):
        store.append("tenant-peer-main", "user", "needle foreign", timestamp=100 + n)
    if failed_read:

        def unavailable(prefix):
            raise OSError("fixture prefix read failure")

        monkeypatch.setattr(store, "list_session_ids_with_prefix", unavailable)
    joiner = "&" if query else "?"
    response = _read(instance, "/agents/tenant/chat-history" + query + joiner + "limit=2")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["messages"]] == [own[2].id, own[1].id]
    assert response.json()["count"] == 2


@pytest.mark.parametrize("failed_read", [False, True])
def test_opaque_registry_alias_remains_owned_when_the_store_read_fails(
    daemon, monkeypatch, failed_read
):
    instance = daemon()
    instance.agents.set_streaming_session_id("tenant", "opaque-resume", label="notes-part")
    store = instance.app.state.conversation_store
    store.append("opaque-resume", "user", "needle opaque fixture", timestamp=10)
    if failed_read:

        def unavailable(prefix):
            raise OSError("fixture prefix read failure")

        monkeypatch.setattr(store, "list_session_ids_with_prefix", unavailable)
    scope = _collect(instance, "tenant")
    assert scope == {"opaque-resume", "tenant-notes-part"}
    for query in ["", "?q=needle"]:
        response = _read(instance, "/agents/tenant/chat-history" + query)
        assert response.status_code == 200
        assert [row["session_id"] for row in response.json()["messages"]] == ["opaque-resume"]


def test_recorded_alias_overrules_text_but_a_live_foreign_owner_overrules_both(daemon):
    instance = daemon()
    _register(instance, "tenant-peer")
    store = instance.app.state.conversation_store
    instance.agents.set_streaming_session_id("tenant", "tenant-peer-recorded", label="notes")
    instance.agents.set_streaming_session_id("tenant", "tenant-peer-live", label="live")
    store.append("tenant-peer-recorded", "user", "needle recorded fixture", timestamp=10)
    store.append("tenant-peer-live", "user", "needle live fixture", timestamp=20)
    instance.app.state.manager.create(
        session_id="tenant-peer-live",
        agent_name="tenant-peer",
        working_dir="fixture-work",
    )
    scope = _collect(instance, "tenant")
    assert "tenant-peer-recorded" in scope and "tenant-peer-live" not in scope
    for query in ["", "?q=needle"]:
        response = _read(instance, "/agents/tenant/chat-history" + query)
        assert response.status_code == 200
        assert [row["session_id"] for row in response.json()["messages"]] == [
            "tenant-peer-recorded"
        ]
    assert _last_message(instance, "tenant") == 10


def test_bare_id_does_not_widen_any_existing_candidate_set(daemon, monkeypatch):
    instance = daemon()
    instance.app.state.manager.create(
        session_id="tenant", agent_name="", working_dir="fixture-work"
    )
    instance.app.state.conversation_store.append(
        "tenant", "user", "needle bare fixture", timestamp=10
    )
    assert _collect(instance, "tenant") == set()
    assert _read(instance, "/agents/tenant/sessions").json()["sessions"] == []
    assert _read(instance, "/agents/tenant/chat-history").json()["messages"] == []
    assert _read(instance, "/agents/tenant/chat-history?q=needle").json()["messages"] == []
    calls = _refresh_calls(instance, monkeypatch)
    assert (
        _owner_post(instance, "/agents/tenant/sessions/refresh").json()["refreshed_sessions"] == []
    )
    assert calls == [] and _last_message(instance, "tenant") is None


def test_case_insensitive_like_fallback_keeps_its_previous_behavior(daemon):
    instance = daemon()
    instance.app.state.conversation_store.append(
        "TENANT-main", "user", "needle case fixture", timestamp=10
    )
    assert _collect(instance, "tenant") == set()
    plain = _read(instance, "/agents/tenant/chat-history").json()
    query = _read(instance, "/agents/tenant/chat-history?q=needle").json()
    assert plain["messages"] == []
    assert [row["session_id"] for row in query["messages"]] == ["TENANT-main"]
    assert query["sessions_searched"] == 0
    assert _last_message(instance, "tenant") == 10


def test_stored_rows_return_to_id_text_when_the_live_owner_ends(daemon):
    from pinky_daemon.sessions import SessionState

    instance = daemon()
    session = instance.app.state.manager.create(
        session_id="tenant-main",
        agent_name="peer",
        working_dir="fixture-work",
    )
    instance.app.state.conversation_store.append(
        "tenant-main", "user", "needle fixture", timestamp=10
    )
    assert _read(instance, "/agents/tenant/chat-history?q=needle").json()["messages"] == []
    session.state = SessionState.closed
    for query in ["", "?q=needle"]:
        response = _read(instance, "/agents/tenant/chat-history" + query)
        assert [row["session_id"] for row in response.json()["messages"]] == ["tenant-main"]


def test_stored_joined_id_tie_goes_to_the_longer_registered_name(daemon):
    instance = daemon()
    for name in ["a", "a-b"]:
        _register(instance, name)
    instance.app.state.conversation_store.append("a-b-main", "user", "needle ambiguous fixture")
    for query in ["", "?q=needle"]:
        short = _read(instance, "/agents/a/chat-history" + query, name="a")
        long = _read(instance, "/agents/a-b/chat-history" + query, name="a-b")
        assert short.json()["messages"] == []
        assert [row["session_id"] for row in long.json()["messages"]] == ["a-b-main"]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("query", ["", "?q=needle"])
def test_compatibility_history_response(daemon, monkeypatch, mode, query):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    instance.app.state.conversation_store.append(
        "tenant-main", "user", "needle owned fixture", timestamp=10
    )
    response = _read(instance, "/agents/tenant/chat-history" + query)
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["sessions_searched"] == 1


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_compatibility_sessions_response(daemon, monkeypatch, mode):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    session = instance.app.state.manager.create(
        session_id="tenant-main",
        agent_name="tenant",
        soul="owned fixture",
        working_dir="fixture-work",
    )
    session.created_at = session.last_active = 10
    response = _read(instance, "/agents/tenant/sessions")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["sessions"][0]["id"] == "tenant-main"


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_compatibility_refresh_response(daemon, monkeypatch, mode):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    for label in ["main", "notes"]:
        instance.app.state.manager.create(
            session_id="tenant-" + label,
            agent_name="tenant",
            working_dir="fixture-work",
        )
    calls = _refresh_calls(instance, monkeypatch)
    response = _owner_post(instance, "/agents/tenant/sessions/refresh")
    assert response.status_code == 200
    assert response.json() == {
        "agent": "tenant",
        "refreshed_sessions": ["tenant-main", "tenant-notes"],
        "count": 2,
    }
    assert calls == ["tenant-main", "tenant-notes"]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_compatibility_event_response(daemon, monkeypatch, mode):
    import time

    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    monkeypatch.setattr(time, "time", lambda: 1000)
    path = "/sessions/tenant-main/events"
    response = _post(instance, path, "normal", {"event_type": "session_start"})
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "id": 1,
        "session_id": "tenant-main",
        "event_type": "session_start",
    }
    assert _read(instance, path, name="normal").json()["events"][0]["agent_name"] == "tenant"


@pytest.mark.asyncio
async def test_compatibility_incremental_stream_wire(daemon):
    instance = daemon()
    response = await closure(instance.app, "stream_agent_events")("tenant", label="notes-part")
    callback = await closure(instance.app, "_make_streaming_event_callback")("tenant", "notes-part")
    await callback({"type": "delta", "text": "owned fixture"})
    try:
        event = await asyncio.wait_for(anext(response.body_iterator), 0.5)
    finally:
        await response.body_iterator.aclose()
    assert (
        event
        == 'data: {"session_id": "tenant-notes-part", "type": "delta", "text": "owned fixture"}\n\n'
    )


def test_legacy_percent_session_row_has_no_registered_text_owner(daemon):
    from pinky_daemon.session_identity import resolve_session_owner

    instance = daemon()
    store = instance.app.state.conversation_store
    sid = "tenant%legacy-main"
    store._conn.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, 'user', ?, 10)",
        (sid, "needle legacy fixture"),
    )
    store._conn.commit()
    names = [agent.name for agent in instance.agents.list(include_retired=True)]
    assert resolve_session_owner(sid, names) == ""
    assert store.count(sid) == 1
    for name in ["tenant", "peer", "normal"]:
        for query in ["", "?q=needle"]:
            response = _read(instance, "/agents/" + name + "/chat-history" + query, name=name)
            assert response.status_code == 200
            assert response.json()["messages"] == []


@pytest.mark.parametrize(
    "request_owner,live_owner,expected",
    [
        ("manual-owner", None, "manual-owner"),
        ("", "peer", "peer"),
        ("", "", ""),
        ("manual-owner", "peer", "manual-owner"),
    ],
)
def test_explicit_request_owner_and_existing_live_event_branch_stay_unchanged(
    daemon,
    request_owner,
    live_owner,
    expected,
):
    instance = daemon()
    if live_owner is not None:
        instance.app.state.manager.create(
            session_id="tenant-main",
            agent_name=live_owner,
            working_dir="fixture-work",
        )
    path = "/sessions/tenant-main/events"
    response = _post(
        instance,
        path,
        "normal",
        {
            "event_type": "session_start",
            "agent_name": request_owner,
        },
    )
    assert response.status_code == 200
    event_id = response.json()["id"]
    events = _read(instance, path, name="normal").json()["events"]
    assert next(row for row in events if row["id"] == event_id)["agent_name"] == expected


def test_autonomy_timestamp_excludes_id_only_prefix_family(daemon):
    instance = daemon()
    _register(instance, "tenant-peer")
    store = instance.app.state.conversation_store
    store.append("tenant-main", "user", "own timestamp fixture", timestamp=10)
    store.append("tenant-peer-main", "user", "other timestamp fixture", timestamp=99)
    assert _last_message(instance, "tenant") == 10
    assert _last_message(instance, "tenant-peer") == 99
