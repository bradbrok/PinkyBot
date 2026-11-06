"""Committed credential writers invalidate every affected adapter namespace."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_daemon.migration import routes as migration
from pinky_outreach import buzz_dependency
from tests.conftest import TEST_SESSION_SECRET
from tests.test_platform_adapter_cache_race import NEW, OLD
from tests.test_platform_adapter_cache_race import service as cache_service

service = cache_service
pytestmark = pytest.mark.real_auth


def owner():
    return {"Cookie": f"{SESSION_COOKIE_NAME}={create_session_cookie(TEST_SESSION_SECRET)}"}


def state(service):
    return inspect.getclosurevars(service.evict).nonlocals


def generation(service, name="sample", platform="slack"):
    return state(service)["_adapter_generation"].get((name, platform), 0)


def shared(service):
    token_id = service.registry.create_bot_token("fixture", "slack", OLD)["id"]
    for name, platform in (("sample", "slack"), ("other", "slack"),
                           ("sample", "discord"), ("sample", "imessage")):
        service.registry.set_token(name, platform, "", token_ref=token_id,
                                   settings={"account_id": "account-one"})
    return token_id


def shared_route(service, token_id, change):
    response = service.client.request(
        "DELETE" if change == "delete" else "PUT", f"/bot-tokens/{token_id}",
        headers=owner(), json=None if change == "delete" else
        {"platform": "telegram"} if change == "platform" else {"token": NEW},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("change", ("token", "platform", "delete"))
def test_shared_http_invalidates_all_binding_platforms(service, change):
    token_id = shared(service)
    service.registry.set_token("other", "discord", "fixture-unrelated")
    unrelated = service.get("other", "discord")
    getters = [lambda n=n, p=p, a=a: service.get(n, p, account_id=a)
               for n, p in (("sample", "slack"), ("other", "slack"), ("sample", "discord"))
               for a in ("", "account-one")]
    getters += [lambda: service.imessage("sample"), lambda: service.imessage()]
    old = [get() for get in getters]
    assert all(item.token == OLD for item in old)
    shared_route(service, token_id, change)
    for get, previous in zip(getters, old, strict=True):
        current = get()
        if change == "delete":
            assert current is None, "deleted shared credentials remained cached"
        else:
            assert current is not previous, "a referencing cache key survived the commit"
            assert current.token == (OLD if change == "platform" else NEW)
    assert service.get("other", "discord") is unrelated
    if change == "delete":
        assert service.registry.get_token("sample", "slack").token_ref == ""


def test_shared_inline_precedence_control(service):
    token_id = shared(service)
    service.registry.set_token("sample", "slack", "fixture-inline", token_ref=token_id)
    assert service.get("sample", "slack").token == "fixture-inline"
    shared_route(service, token_id, "token")
    assert service.get("sample", "slack").token == "fixture-inline"


@pytest.mark.parametrize("change", ("token", "delete"))
@pytest.mark.parametrize("warm", (False, True), ids=("pending-only", "cached-variant"))
def test_shared_commit_rejects_old_inflight_build(service, monkeypatch, change, warm):
    token_id = shared(service)
    if warm:
        assert service.get("sample", "slack").token == OLD
    read, release = threading.Event(), threading.Event()
    original = service.registry.get_raw_token

    def gated(*args):
        token = original(*args)
        if not read.is_set():
            read.set()
            assert release.wait(10), "shared writer could not finish"
        return token

    monkeypatch.setattr(service.registry, "get_raw_token", gated)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.get, "sample", "slack", account_id="account-one")
        try:
            assert read.wait(10)
            shared_route(service, token_id, change)
        finally:
            release.set()
        result = future.result(timeout=10)
    if change == "delete":
        assert result is None
        assert service.get("sample", "slack") is None
    else:
        assert result.token == NEW
        assert service.get("sample", "slack", account_id="account-one") is result
        assert service.get("sample", "slack").token == NEW
    service.built[1 if warm else 0].close.assert_called_once_with()


@pytest.mark.parametrize("change", ("token", "delete"))
def test_shared_invalidation_occurs_after_commit(service, monkeypatch, change):
    token_id = shared(service)
    db = service.registry._db
    before_commit = []

    def committed_read(name, platform):
        # A separate connection observes committed state while the writer has
        # an uncommitted update. No concurrent use of the writer connection.
        with sqlite3.connect(service.registry._db_path) as reader:
            row = reader.execute(
                "SELECT COALESCE(NULLIF(a.token,''), b.token, '') FROM agent_tokens a "
                "LEFT JOIN bot_tokens b ON a.token_ref=b.id "
                "WHERE a.agent_name=? AND a.platform=?", (name, platform),
            ).fetchone()
        return row[0] if row else ""

    monkeypatch.setattr(service.registry, "get_raw_token", committed_read)

    class CommitGate:
        def __getattr__(self, name):
            return getattr(db, name)

        def commit(self):
            # This reader starts after any premature notification, but before
            # the actual commit, so a pre-commit-only bump cannot protect it.
            before_commit.append(service.get("sample", "slack"))
            db.commit()

    monkeypatch.setattr(service.registry, "_db", CommitGate())
    shared_route(service, token_id, change)
    assert len(before_commit) == 1 and before_commit[0].token == OLD
    current = service.get("sample", "slack")
    assert current is not before_commit[0], "only a pre-commit invalidation occurred"
    assert current is None if change == "delete" else current.token == NEW


@pytest.mark.parametrize("change", ("ref", "disabled", "account_id", "team_id", "workspace_id"))
def test_agent_route_binding_changes_control(service, change):
    token_id = shared(service)
    old_generic = service.get("sample", "slack")
    old_account = service.get("sample", "slack", account_id="account-one")
    body = {"token": "", "token_ref": token_id, "settings": {"account_id": "account-one"}}
    if change == "ref":
        body["token_ref"] = service.registry.create_bot_token("replacement", "slack", NEW)["id"]
    elif change == "disabled":
        body["enabled"] = False
    else:
        body["settings"] = {change: "account-two"}
    response = service.client.put("/agents/sample/tokens/slack", headers=owner(), json=body)
    assert response.status_code == 200
    assert service.get("sample", "slack") is not old_generic
    account = service.get("sample", "slack", account_id="account-one")
    assert account is not old_account
    if change == "ref":
        assert account.token == NEW
    else:
        assert account is None
    if change == "disabled":
        body["enabled"] = True
        assert service.client.put("/agents/sample/tokens/slack", headers=owner(), json=body).status_code == 200
        assert service.get("sample", "slack", account_id="account-one").token == OLD
    elif change not in ("ref",):
        assert service.get("sample", "slack", account_id="account-two").token == OLD


def test_omitted_settings_control(service):
    shared(service)
    response = service.client.put("/agents/sample/tokens/slack", headers=owner(), json={"token": NEW})
    assert response.status_code == 200
    assert service.registry.get_token("sample", "slack").settings == {"account_id": "account-one"}
    assert service.get("sample", "slack", account_id="account-one").token == NEW


@pytest.mark.parametrize("recreate", (False, True))
def test_hard_delete_invalidates_warm_and_pending_builds(service, monkeypatch, recreate):
    shared(service)
    work_dir = service.registry.get("sample").working_dir
    warm = service.get("sample", "discord")
    before = generation(service)
    read, release = threading.Event(), threading.Event()
    original = service.registry.get_raw_token

    def gated(*args):
        token = original(*args)
        if not read.is_set():
            read.set()
            assert release.wait(10)
        return token

    monkeypatch.setattr(service.registry, "get_raw_token", gated)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.get, "sample", "slack")
        try:
            assert read.wait(10)
            assert service.registry.delete("sample")
            after_delete = generation(service)
            if recreate:
                service.registry.register("sample", working_dir=work_dir)
                service.registry.set_token("sample", "slack", NEW)
        finally:
            release.set()
        result = future.result(timeout=10)
    assert after_delete > before, "hard deletion reset or did not advance the generation"
    assert service.get("sample", "discord") is None and warm.token == OLD
    assert result.token == NEW if recreate else result is None
    service.built[1].close.assert_called_once_with()
    assert service.get("sample", "slack") is result


def test_hard_delete_expands_uncached_and_dynamic_platforms(service):
    internals = state(service)
    internals["_platform_adapters"][("sample", "extension", "account")] = object()
    internals["_adapter_generation"][("sample", "prior-only")] = 8
    before = dict(internals["_adapter_generation"])
    assert service.registry.delete("sample")
    for platform in ("telegram", "discord", "slack", "imessage", "buzz", "extension", "prior-only"):
        assert generation(service, platform=platform) > before.get(("sample", platform), 0)
    assert ("sample", "extension", "account") not in internals["_platform_adapters"]


def test_registration_compensation_invalidates_created_identity(service, monkeypatch, tmp_path):
    cached = []

    def fail_after_credentials(name):
        service.registry.set_token(name, "slack", OLD)
        cached.append(service.get(name, "slack"))
        raise RuntimeError("fixture registration failure")

    monkeypatch.setattr(service.registry, "get_or_create_signing_key", fail_after_credentials)
    response = service.client.post("/agents", headers=owner(), json={
        "name": "temporary", "working_dir": str(tmp_path / "temporary"),
    })
    assert response.status_code == 500 and len(cached) == 1
    assert service.registry.get("temporary") is None
    assert service.get("temporary", "slack") is None, "compensation left the cached credential"


@pytest.mark.parametrize("method", ("set", "remove"))
def test_direct_token_writer_invalidates_cache(service, method):
    service.registry.set_token("sample", "slack", OLD)
    previous = service.get("sample", "slack")
    if method == "set":
        service.registry.set_token("sample", "slack", NEW)
    else:
        assert service.registry.remove_token("sample", "slack")
    current = service.get("sample", "slack")
    assert current is not previous
    assert current.token == NEW if method == "set" else current is None


def migration_write(service, monkeypatch, name):
    # Isolate the unrelated workspace importer, while executing the real
    # migration endpoint and its direct registry credential writer.
    monkeypatch.setattr(migration, "_shared_agent_registry", service.registry)
    monkeypatch.setattr(migration, "_parse_store", {"fixture": {"preview": object()}})
    monkeypatch.setattr(migration, "apply_migration", lambda *a, **k:
                        SimpleNamespace(agent_name=name, task_id="fixture", items_created={}, warnings=[]))
    return asyncio.run(migration.apply_endpoint(
        migration.ApplyRequest(parse_id="fixture", tokens={"slack": NEW}),
    ))


def test_migration_direct_writer_invalidates_reused_namespace(service, monkeypatch):
    service.registry.set_token("sample", "slack", OLD)
    previous = service.get("sample", "slack")
    result = migration_write(service, monkeypatch, "sample")
    assert result["items_created"]["tokens"] == 1
    current = service.get("sample", "slack")
    assert current is not previous and current.token == NEW


def test_migration_fresh_name_control(service, monkeypatch):
    result = migration_write(service, monkeypatch, "other")
    assert result["items_created"]["tokens"] == 1
    assert service.get("other", "slack").token == NEW


@pytest.mark.parametrize("writer", ("main", "setting", "clear"))
def test_main_agent_writers_invalidate_global_imessage(service, writer):
    service.registry.set_token("sample", "imessage", OLD)
    assert service.imessage().token == OLD
    if writer == "main":
        service.registry.set_main_agent("other")
    elif writer == "setting":
        service.registry.set_setting("main_agent", "other")
    else:
        assert service.registry.delete_setting("main_agent")
    assert service.imessage() is None


def test_same_main_value_and_retire_control(service):
    service.registry.set_token("sample", "imessage", OLD)
    original = service.imessage()
    before = generation(service, "__global__", "imessage")
    service.registry.set_main_agent("sample")
    service.registry.set_setting("unrelated", "value")
    service.registry.delete_setting("unrelated")
    service.registry.retire("sample")
    assert generation(service, "__global__", "imessage") == before
    assert service.imessage() is original


def bind_buzz(service, monkeypatch):
    monkeypatch.setattr(buzz_dependency, "missing_buzz_dependencies", lambda: [])
    return service.registry.bind_buzz_identity_owner_control(
        "sample", private_key="11" * 32, relay_url="wss://fixture.invalid",
        community_id="fixture", relay_signing_pubkey="22" * 32,
        enabled=True, owner_actor="ui:fixture",
    )


@pytest.mark.parametrize("transition", ("unhealthy", "refused", "ready"))
def test_buzz_real_transition_notifies_after_commit(service, monkeypatch, transition):
    bind_buzz(service, monkeypatch)
    old = service.get("sample", "buzz")
    assert old is not None
    if transition == "ready":
        # Seed the prior committed state without invoking the factory's own
        # ready repair. The transition under test is still the real writer.
        service.registry._db.execute("UPDATE buzz_identities SET status='dependency_refused'")
        service.registry._db.commit()
    observations = []
    db = service.registry._db
    service.on_close = lambda: observations.append((db.in_transaction,
                                                   service.registry.get_buzz_identity("sample")["status"]))
    before = generation(service, platform="buzz")
    if transition == "unhealthy":
        service.registry.mark_buzz_identity_unhealthy("sample", "fixture")
    elif transition == "refused":
        service.registry.mark_buzz_dependency_refused("sample", "fixture")
    else:
        service.registry.mark_buzz_dependency_ready("sample")
    assert generation(service, platform="buzz") > before
    old.close.assert_called_once_with()
    expected = {"ready": "active", "refused": "dependency_refused", "unhealthy": "unhealthy"}[transition]
    assert observations == [(False, expected)]
    if transition == "unhealthy":
        assert service.get("sample", "buzz") is None


def test_buzz_ready_noop_build_can_publish_control(service, monkeypatch):
    bind_buzz(service, monkeypatch)
    before = generation(service, platform="buzz")
    adapter = service.get("sample", "buzz")
    assert adapter is not None
    assert generation(service, platform="buzz") == before
    assert service.get("sample", "buzz") is adapter
    adapter.close.assert_not_called()
    assert len(service.built) == 1


@pytest.mark.parametrize("operation", ("bind", "disable"))
def test_buzz_owner_route_eviction_control(service, monkeypatch, operation):
    bind_buzz(service, monkeypatch)
    old = service.get("sample", "buzz")
    if operation == "disable":
        response = service.client.post("/system/buzz-identities/sample/disable", headers=owner())
    else:
        response = service.client.put("/system/buzz-identities/sample", headers=owner(), json={
            "private_key": "11" * 32, "relay_url": "wss://fixture.invalid",
            "community_id": "fixture", "relay_signing_pubkey": "22" * 32, "enabled": True,
        })
    assert response.status_code == 200, response.text
    old.close.assert_called_once_with()
    current = service.get("sample", "buzz")
    assert current is None if operation == "disable" else current is not old


@pytest.mark.parametrize("kind", ("extra", "buzz"))
def test_close_owns_no_cache_lock_control(service, monkeypatch, kind):
    observations = []
    lock = state(service)["_adapter_lock"]

    def closed():
        acquired = lock.acquire(blocking=False)
        observations.append(acquired)
        if acquired:
            lock.release()

    service.on_close = closed
    if kind == "buzz":
        bind_buzz(service, monkeypatch)
        assert service.get("sample", "buzz") is not None
        service.evict("sample", "buzz")
    else:
        barrier = threading.Barrier(2)

        def read(*args):
            barrier.wait(timeout=10)
            return OLD

        monkeypatch.setattr(service.registry, "get_raw_token", read)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.get, "sample", "slack") for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        assert results[0] is results[1]
    assert observations == [True], "adapter disposal ran under the cache lock"


def test_listener_contract_is_postcommit_synchronous_and_failure_isolated(service, monkeypatch):
    register = getattr(service.registry, "add_token_change_listener", None)
    assert callable(register), "registry has no post-commit credential subscription"
    observations = []
    writer = threading.get_ident()

    def broken(pairs):
        raise RuntimeError("fixture-secret-must-not-appear")

    def observe(pairs):
        with sqlite3.connect(service.registry._db_path) as reader:
            row = reader.execute("SELECT token FROM agent_tokens WHERE agent_name='sample' AND platform='slack'").fetchone()
        observations.append((pairs, service.registry._db.in_transaction, threading.get_ident(), row[0]))

    from pinky_daemon import agent_registry
    log = Mock()
    monkeypatch.setattr(agent_registry, "_log", log)
    register(broken)
    register(observe)
    service.registry.set_token("sample", "slack", NEW)
    assert observations == [([("sample", "slack")], False, writer, NEW)]
    assert log.called and "fixture-secret-must-not-appear" not in str(log.call_args_list)
    assert service.get("sample", "slack").token == NEW


def test_churn_text_send_has_no_delivery_record_control(service, monkeypatch):
    service.registry.set_token("sample", "slack", OLD)
    original = service.registry.get_raw_token
    reads = []

    def changing(*args):
        value = original(*args)
        reads.append(value)
        assert len(reads) <= 2
        service.evict("sample", "slack")
        return value

    monkeypatch.setattr(service.registry, "get_raw_token", changing)
    record = Mock()
    from tests.isolated_policy_support import replace_cell
    route = next(route.endpoint for route in service.app.routes if route.path == "/broker/send")
    replace_cell(monkeypatch, route, "_record_outbound_message", record)
    response = service.client.post("/broker/send", headers=owner(), json={
        "agent_name": "sample", "platform": "slack", "chat_id": "C00000001", "content": "fixture",
    })
    assert response.status_code in (409, 503), response.text
    assert len(reads) == len(service.built) == 2
    record.assert_not_called()
    for adapter in service.built:
        adapter.close.assert_called_once_with()
