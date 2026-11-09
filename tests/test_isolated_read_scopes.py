"""Enforced read scopes come from signed identity, with full-response controls."""

import copy
import os

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api, voice_routes
from pinky_daemon.api_models import OwnerProfileRequest
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_read_enforce import _read
from tests.test_isolated_read_enforce import read_logs as read_logs

pytestmark = pytest.mark.real_auth

SAFE_PROFILE_FIELDS = {"name", "pronouns", "timezone", "role", "comm_style", "languages", "locale"}
PRIVATE_PROFILE_FIELDS = {"code_word"}
COLLECTIONS = [
    ("/api/voice/requests", "requests", "requested_by_agent"),
    ("/api/voice/sessions", "sessions", "agent_name"),
    ("/schedules", "schedules", "agent_name"),
    ("/scheduler/wake-ledger", "records", "agent_name"),
]


@pytest.fixture
def layout(daemon):
    instance = daemon()
    for name in ("tenant", "peer"):
        instance.agents.update(name, groups=["shared"], soul=f"private-{name}", role="worker")
    for name in ("normal", "dreamer"):
        instance.agents.update(name, groups=["separate"], soul=f"private-{name}")
    instance.agents.set_main_agent("normal")
    instance.agents.set_owner_profile({
        "name": "Owner fixture", "pronouns": "they/them", "timezone": "UTC",
        "role": "operator", "comm_style": "brief", "languages": "en", "locale": "en",
        "code_word": "private-code-word",
    })
    for name in ("assigned-fixture", "delegated-fixture", "unassigned-fixture"):
        instance.skills.register(name, directive=f"text-{name}", origin_agent="tenant")
    instance.skills.assign_to_agent("tenant", "assigned-fixture")
    instance.skills.set_refresh_delegate("delegated-fixture", "tenant")
    for name in ("tenant", "peer"):
        schedule = instance.agents.add_schedule(
            name, "0 9 * * *", name="fixture", prompt=f"private-wake-{name}",
        )
        instance.agents.persist_schedule_wake(
            schedule.id, agent_name=name, schedule_name=schedule.name,
            prompt=schedule.prompt, fired_at=100.0,
        )
        request = voice_routes._voice_store.create_call_request(
            requested_by_agent=name, target_name="Fixture", target_phone="+15550100101",
            goal=f"private-voice-{name}", fallback_behavior="end_call",
        )
        voice_routes._voice_store.create_session(
            call_request_id=request.id, call_sid=f"call-{name}", agent_name=name,
            direction="outbound", from_number="+15550100102", to_number="+15550100101",
        )
    return instance


def _as_actor(instance, path, actor):
    if actor != "browser":
        return _read(instance, path, name=actor)
    client = TestClient(instance.app)
    client.cookies.set(
        SESSION_COOKIE_NAME, create_session_cookie(os.environ["PINKY_SESSION_SECRET"]),
    )
    try:
        return client.get(path)
    finally:
        client.close()


def test_roster_has_only_shared_groups_and_safe_fields(layout, monkeypatch):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(layout, "/agents")
    rows = response.json()["agents"]
    assert {row["name"] for row in rows} == {"tenant", "peer"}
    assert all(set(row) == {"name", "display_name", "role", "groups", "enabled", "online"}
               for row in rows)
    assert all(type(row["online"]) is bool for row in rows)
    assert "private-" not in response.text
    assert _read(layout, "/agents?group=separate").json()["agents"] == []


def test_roster_grant_returns_every_row_with_exact_four_fields(layout, monkeypatch):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    layout.agents.set_isolated_grants("tenant", ["fleet_roster"])
    rows = _read(layout, "/agents").json()["agents"]
    assert {row["name"] for row in rows} == {"tenant", "peer", "normal", "dreamer"}
    assert all(set(row) == {"name", "model", "groups", "enabled"} for row in rows)
    layout.agents.set_isolated_grants("tenant", [])
    assert {row["name"] for row in _read(layout, "/agents").json()["agents"]} == {"tenant", "peer"}


@pytest.mark.parametrize("field", ["agent_name", "assigned_agent", "agent", "created_by"])
def test_roster_scope_cannot_come_from_a_query(layout, monkeypatch, field):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    rows = _read(layout, f"/agents?{field}=normal").json()["agents"]
    assert {row["name"] for row in rows} == {"tenant", "peer"}


def test_profile_model_sensitive_field_inventory():
    assert set(OwnerProfileRequest.model_fields) == SAFE_PROFILE_FIELDS | PRIVATE_PROFILE_FIELDS
    assert not any("notification" in key or key.endswith(("chat_id", "user_id"))
                   for key in OwnerProfileRequest.model_fields)


@pytest.mark.parametrize("field", sorted(PRIVATE_PROFILE_FIELDS))
def test_profile_removes_each_private_model_field(layout, monkeypatch, field):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    profile = _read(layout, "/settings/owner-profile").json()
    assert set(profile) == SAFE_PROFILE_FIELDS
    assert field not in profile
    assert profile == {key: value for key, value in layout.agents.get_owner_profile().items()
                       if key in SAFE_PROFILE_FIELDS}


@pytest.mark.parametrize("field", [
    "notification_destinations", "notification_platform", "notification_chat_id",
    "notification_user_id", "chat_id", "user_id",
])
def test_profile_does_not_expose_unlisted_destination_fields(layout, monkeypatch, field):
    profile = {**layout.agents.get_owner_profile(), field: "private-destination"}
    monkeypatch.setattr(layout.agents, "get_owner_profile", lambda: copy.deepcopy(profile))
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(layout, "/settings/owner-profile")
    assert field not in response.json()
    assert "private-destination" not in response.text


@pytest.mark.parametrize("skill", ["assigned-fixture", "delegated-fixture"])
def test_assigned_and_delegated_skill_reads_work(layout, monkeypatch, skill):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(layout, f"/skills/{skill}")
    assert response.status_code == 200
    assert response.json()["directive"] == f"text-{skill}"


def test_skill_access_uses_current_assignment_and_delegation(layout, monkeypatch):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    assert _read(layout, "/skills/unassigned-fixture").status_code == 404
    layout.skills.assign_to_agent("tenant", "unassigned-fixture")
    assert _read(layout, "/skills/unassigned-fixture").status_code == 200
    layout.skills.set_refresh_delegate("delegated-fixture", "")
    assert _read(layout, "/skills/delegated-fixture").status_code == 404


@pytest.mark.parametrize("path,key,owner", COLLECTIONS)
@pytest.mark.parametrize("suffix", ["", "?agent_name=tenant"])
def test_enforced_collections_have_only_the_signed_callers_rows(
    layout, monkeypatch, path, key, owner, suffix,
):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(layout, path + suffix)
    assert response.status_code == 200
    assert {row[owner] for row in response.json()[key]} == {"tenant"}
    assert "private-wake-peer" not in response.text
    assert "private-voice-peer" not in response.text


@pytest.mark.parametrize("path,key,owner", COLLECTIONS)
@pytest.mark.parametrize("query", ["agent_name", "assigned_agent", "agent", "created_by"])
def test_foreign_query_filters_cannot_widen_collections(layout, monkeypatch, path, key, owner, query):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(layout, f"{path}?{query}=peer")
    if path in {"/schedules", "/scheduler/wake-ledger"} and query == "agent_name":
        assert response.status_code == 403
    else:
        assert response.status_code == 200
        assert {row[owner] for row in response.json()[key]} <= {"tenant"}


@pytest.mark.parametrize("actor,mode", [
    ("tenant", "off"), ("tenant", "shadow"), ("normal", "enforce"), ("browser", "enforce"),
])
def test_full_response_controls_for_every_scoped_handler(layout, monkeypatch, actor, mode):
    paths = ["/agents", "/settings/owner-profile", "/settings/main-agent", "/skills/unassigned-fixture",
             *[path for path, _, _ in COLLECTIONS], "/system/health"]
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "off")
    before = {path: _as_actor(layout, path, actor).json() for path in paths}
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    after = {path: _as_actor(layout, path, actor).json() for path in paths}
    assert before == after
    assert len(after["/agents"]["agents"]) == 4
    assert all("soul" in row for row in after["/agents"]["agents"])
    assert after["/settings/owner-profile"]["code_word"] == "private-code-word"
    for path, key, owner in COLLECTIONS:
        assert {row[owner] for row in after[path][key]} == {"tenant", "peer"}


def test_unavailable_registry_cannot_widen_roster_but_safe_scopes_still_work(layout, monkeypatch):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    original = layout.agents.get

    def unavailable(name):
        if name == "tenant":
            raise RuntimeError("private-registry-marker")
        return original(name)

    monkeypatch.setattr(layout.agents, "get", unavailable)
    assert _read(layout, "/agents").json()["agents"] == []
    for path in ["/settings/main-agent", "/settings/owner-profile", "/skills/assigned-fixture", "/tasks"]:
        assert _read(layout, path).status_code == 200
    for path, key, owner in COLLECTIONS:
        response = _read(layout, path)
        assert response.status_code == 200
        assert {row[owner] for row in response.json()[key]} == {"tenant"}


def test_health_omits_per_agent_records_and_store_identifiers(layout, monkeypatch):
    marker = "private-health-marker"
    layout.app.state.storage_holds["peer"] = [{"path": marker, "prompt": marker}]
    layout.app.state.sqlite_lock_health = {
        "healthy": False, "checked": 2, "missing": [marker], "inconclusive": [marker],
    }
    original = layout.app.state.access_log.status()
    monkeypatch.setattr(layout.app.state.access_log, "status", lambda: {**original, "path": marker})
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(layout, "/system/health")
    assert response.status_code == 200
    assert marker not in response.text
    assert "storage_holds" not in response.json()
    assert response.json()["sqlite_locks"] == {"healthy": False, "checked": 2}
    assert "last_tick_age_s" in response.json()["scheduler"]
    for actor in ("normal", "browser"):
        assert marker in _as_actor(layout, "/system/health", actor).text


def test_nonisolated_enforce_reads_add_no_registry_lookup(layout, monkeypatch):
    original = layout.agents.get
    reads = []

    def get(name):
        reads.append(name)
        return original(name)

    monkeypatch.setattr(layout.agents, "get", get)
    counts = []
    for mode in ("shadow", "enforce"):
        monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
        reads.clear()
        assert _read(layout, "/agents", name="normal").status_code == 200
        counts.append(len(reads))
    assert counts[0] == counts[1]


@pytest.mark.parametrize("skill,refused", [
    ("assigned-fixture", False), ("delegated-fixture", False), ("unassigned-fixture", True),
])
def test_shadow_skill_diagnostic_uses_current_entitlement(
    layout, monkeypatch, read_logs, skill, refused,
):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "shadow")
    read_logs.clear()
    response = _read(layout, f"/skills/{skill}")
    assert response.status_code == 200
    assert response.json()["directive"] == f"text-{skill}"
    assert [line for line in read_logs if " READ " in line] == ([
        "isolation: WOULD DENY READ GET /skills/{name} for tenant "
        "reason=unassigned-skill repeats=0"
    ] if refused else [])


@pytest.mark.parametrize("path", ["/schedules", "/scheduler/wake-ledger"])
def test_shadow_foreign_schedule_query_is_only_a_diagnostic(layout, monkeypatch, read_logs, path):
    query = path + "?agent_name=peer"
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "off")
    before = _read(layout, query)
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "shadow")
    read_logs.clear()
    after = _read(layout, query)
    assert after.status_code == before.status_code == 200
    assert after.json() == before.json()
    assert [line for line in read_logs if " READ " in line] == [
        f"isolation: WOULD DENY READ GET {path} for tenant reason=other-agent repeats=0"
    ]


@pytest.mark.parametrize("path", ["/schedules", "/scheduler/wake-ledger"])
@pytest.mark.parametrize("query", ["", "?agent_name=tenant"])
def test_shadow_own_schedule_queries_log_nothing(layout, monkeypatch, read_logs, path, query):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "shadow")
    read_logs.clear()
    assert _read(layout, path + query).status_code == 200
    assert not any(" READ " in line for line in read_logs)


@pytest.fixture
def targeted_reads(daemon, monkeypatch):
    instance = daemon()
    instance.agents.update("tenant", groups=["shared"])
    instance.agents.update("peer", groups=["shared"])
    for name in ("tenant", "peer", "normal"):
        instance.agents.set_slack_mention_only(name, ["C12345678"], "fixture-owner")
    instance.readback_calls = []

    async def message(select, channel, timestamp, thread_timestamp):
        instance.readback_calls.append(("message", select.args, select.keywords))
        return {"agent": select.args[0], "channel": channel, "timestamp": timestamp}

    async def history(select, channel, oldest, latest, pages):
        instance.readback_calls.append(("history", select.args, select.keywords))
        return {"agent": select.args[0], "channel": channel, "messages": []}

    monkeypatch.setattr(api.slack_readback, "read_message", message)
    monkeypatch.setattr(api.slack_readback, "read_history", history)
    return instance


def _targeted_read(instance, monkeypatch, read_logs, mode, caller, target, kind):
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    prefix = f"/agents/{target}/slack"
    if kind == "message":
        path = prefix + "/message/C12345678/1730312345.123456?account_id=fixture-account"
    elif kind == "history":
        path = prefix + "/history/C12345678?oldest=1730312300.000001&latest=1730312400.000000"
        path += "&account_id=fixture-account"
    else:
        path = prefix + "/mention-only"
    read_logs.clear()
    response = _read(instance, path, name=caller)
    assert not any(" READ " in line for line in read_logs)
    if caller == target:
        assert response.status_code == 200
        if kind == "mention-only":
            assert response.json()["channels"][0]["channel_id"] == "C12345678"
        else:
            assert response.json()["agent"] == caller
            assert instance.readback_calls == [
                (kind, (caller, "slack"), {"account_id": "fixture-account"}),
            ]
    else:
        assert response.status_code == 403
        assert instance.readback_calls == []
        if caller == "normal":
            expected = ("owner session or matching agent required" if kind == "mention-only"
                        else "verified caller must match the target agent")
            assert response.json() == {"detail": expected}


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller,target", [
    ("tenant", "tenant"), ("tenant", "peer"), ("normal", "normal"), ("normal", "peer"),
])
def test_slack_message_reads_keep_the_existing_self_boundary(
    targeted_reads, monkeypatch, read_logs, mode, caller, target,
):
    _targeted_read(targeted_reads, monkeypatch, read_logs, mode, caller, target, "message")


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller,target", [
    ("tenant", "tenant"), ("tenant", "peer"), ("normal", "normal"), ("normal", "peer"),
])
def test_slack_history_reads_keep_the_existing_self_boundary(
    targeted_reads, monkeypatch, read_logs, mode, caller, target,
):
    _targeted_read(targeted_reads, monkeypatch, read_logs, mode, caller, target, "history")


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller,target", [
    ("tenant", "tenant"), ("tenant", "peer"), ("normal", "normal"), ("normal", "peer"),
])
def test_slack_mention_policy_reads_keep_the_existing_self_boundary(
    targeted_reads, monkeypatch, read_logs, mode, caller, target,
):
    _targeted_read(targeted_reads, monkeypatch, read_logs, mode, caller, target, "mention-only")


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("caller", ["tenant", "normal"])
def test_profile_read_permission_does_not_grant_owner_writes(layout, monkeypatch, mode, caller):
    from tests.isolated_policy_support import signed

    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", mode)
    path = "/settings/owner-profile"
    before = layout.agents.get_owner_profile()
    response = _read(layout, path, name=caller)
    assert response.status_code == 200
    assert ("code_word" in response.json()) is (caller == "normal" or mode != "enforce")
    client = TestClient(layout.app)
    try:
        response = client.put(
            path, headers=signed(layout, "PUT", path, caller), json={"role": "changed fixture"},
        )
        assert response.status_code == 403
        assert layout.agents.get_owner_profile() == before
        client.cookies.set(
            SESSION_COOKIE_NAME, create_session_cookie(os.environ["PINKY_SESSION_SECRET"]),
        )
        assert client.put(path, json={"role": "owner fixture update"}).status_code == 200
        assert layout.agents.get_owner_profile()["role"] == "owner fixture update"
    finally:
        client.close()
