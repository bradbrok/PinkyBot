"""Ownership precedence and exclusion query contracts."""

import sqlite3

import pytest

from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.session_identity import SessionIdScope, SessionOwnership, resolve_session_owner


@pytest.mark.parametrize(
    "sid,live,recorded,exact,expected",
    [
        ("a-b-c-main", "", "", False, "a-b-c"),
        ("a-b-c-main", "a", "a-b", False, "a"),
        ("a-b-c-main", "", "a", False, "a"),
        ("opaque", "a-b", "a", False, "a-b"),
        ("opaque", "", "a", False, "a"),
        ("opaque", "", "", False, ""),
        ("a", "", "", False, ""),
        ("a", "", "", True, "a"),
        ("a-b", "", "", True, "a-b"),
        ("A-main", "", "", False, ""),
    ],
)
def test_owner_uses_available_authorities_in_order(sid, live, recorded, exact, expected):
    assert (
        resolve_session_owner(
            sid,
            ["a", "a-b", "a-b-c"],
            live_owner=live,
            recorded_owner=recorded,
            allow_exact=exact,
        )
        == expected
    )


def test_filter_never_adds_ids_and_retains_unknown_existing_candidates():
    policy = SessionOwnership("a", ("a", "a-b"), {"a-other": "other"}, frozenset({"opaque"}))
    candidates = {"a-main", "a-b-main", "a-other", "opaque", "unknown"}
    assert SessionIdScope(candidates, policy) == {"a-main", "opaque", "unknown"}
    assert SessionIdScope(set(), policy) == set()


@pytest.fixture
def messages(tmp_path):
    store = ConversationStore(str(tmp_path / "messages.db"))
    try:
        yield store
    finally:
        store.close()


def test_primitive_defaults_retain_prefix_union_and_timestamp_semantics(messages):
    own = messages.append("a-main", "user", "needle own", timestamp=1)
    foreign = messages.append("a-b-main", "user", "needle other", timestamp=99)
    assert [
        m.id for m in messages.search("needle", session_ids={own.session_id}, session_prefix="a-")
    ] == [foreign.id, own.id]
    assert messages.last_message_time("a-") == 99


def test_search_exclusions_keep_stronger_scope_evidence_and_apply_live_refusal(messages):
    recorded = messages.append("a-b-recorded", "user", "needle recorded", timestamp=1)
    messages.append("a-b-main", "user", "needle other", timestamp=99)
    assert [
        m.id
        for m in messages.search(
            "needle",
            session_prefix="a-",
            session_ids={recorded.session_id},
            exclude_prefixes=("a-b-",),
            limit=1,
        )
    ] == [recorded.id]
    assert (
        messages.search(
            "needle",
            session_prefix="a-",
            session_ids={recorded.session_id},
            exclude_prefixes=("a-b-",),
            exclude_session_ids=(recorded.session_id,),
        )
        == []
    )


def test_timestamp_exclusions_preserve_authoritative_ids_inside_the_original_prefix(messages):
    messages.append("a-main", "user", "own", timestamp=1)
    messages.append("a-b-main", "user", "other", timestamp=99)
    messages.append("a-b-recorded", "user", "recorded", timestamp=10)
    messages.append("a-live", "user", "foreign live", timestamp=100)
    messages.append("opaque", "user", "outside original prefix", timestamp=1000)
    assert (
        messages.last_message_time(
            "a-",
            exclude_prefixes=("a-b-",),
            exclude_session_ids=("a-live",),
            retain_session_ids=("a-b-recorded", "opaque"),
        )
        == 10
    )


def test_exclusions_use_a_bounded_sql_parameter_budget(messages):
    own = messages.append("a-main", "user", "needle own", timestamp=1)
    previous = messages._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
    try:
        prefixes = tuple(f"a-child{n}-" for n in range(1500))
        ids = tuple(f"a-foreign{n}" for n in range(1500))
        assert [
            m.id
            for m in messages.search(
                "needle",
                session_prefix="a-",
                exclude_prefixes=prefixes,
                exclude_session_ids=ids,
            )
        ] == [own.id]
        assert (
            messages.last_message_time("a-", exclude_prefixes=prefixes, exclude_session_ids=ids)
            == 1
        )
    finally:
        messages._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)


def test_percent_exclusion_is_literal_in_search_and_timestamp_queries(messages):
    own = messages.append("scope-main", "user", "needle own", timestamp=1)
    lookalike = messages.append("scope-other-main", "user", "needle owned sibling", timestamp=10)
    messages.append("scope-%-main", "user", "needle excluded literal", timestamp=99)
    assert [
        m.id
        for m in messages.search(
            "needle",
            session_prefix="scope-",
            exclude_prefixes=("scope-%-",),
        )
    ] == [lookalike.id, own.id]
    assert messages.last_message_time("scope-", exclude_prefixes=("scope-%-",)) == 10
