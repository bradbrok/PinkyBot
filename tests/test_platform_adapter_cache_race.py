"""Credential mutations must invalidate in-flight adapter construction."""

from __future__ import annotations

import inspect
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon import api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from pinky_outreach import buzz, buzz_dependency, discord, imessage, slack, telegram
from tests.conftest import TEST_SESSION_SECRET
from tests.isolated_policy_support import closure, replace_cell

pytestmark = pytest.mark.real_auth
CASES = ("slack", "account", "imessage", "global")
OLD = "fixture-old"
NEW = "fixture-new"


@pytest.fixture
def service(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Adapter cache tests must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
    app = api.create_api(db_path=str(tmp_path / "state.db"),
                         default_working_dir=str(tmp_path))
    registry = app.state.agents
    for name in ("sample", "other"):
        work = tmp_path / name
        work.mkdir()
        registry.register(name, working_dir=str(work))
    registry.set_main_agent("sample")
    state = SimpleNamespace(
        app=app, registry=registry, built=[], local=threading.local(), on_close=None,
        get=closure(app, "_get_platform_adapter"),
        imessage=closure(app, "_get_imessage_adapter"),
        evict=closure(app, "_evict_platform_adapters"),
    )
    original = registry.get_raw_token

    def read(*args):
        value = original(*args)
        state.local.token = value
        return value

    monkeypatch.setattr(registry, "get_raw_token", read)

    def build(token=None, **kwargs):
        adapter = SimpleNamespace(token=token if token is not None else state.local.token,
                                  options=kwargs, close=Mock())
        adapter.close.side_effect = lambda: state.on_close() if state.on_close else None
        state.built.append(adapter)
        return adapter

    for module, name in ((slack, "SlackAdapter"), (telegram, "TelegramAdapter"),
                         (discord, "DiscordAdapter"), (imessage, "iMessageAdapter"),
                         (buzz, "BuzzAdapter")):
        monkeypatch.setattr(module, name, build)
    state.client = TestClient(app, follow_redirects=False)
    try:
        yield state
    finally:
        state.client.close()
        app.state.store_catalog.close()
        registry.close()


def platform_for(case):
    return "imessage" if case in ("imessage", "global") else "slack"


def seed(service, case, token=OLD):
    service.registry.set_token("sample", platform_for(case), token,
                               settings={"account_id": "account-one"})


def lookup(service, case):
    if case in ("imessage", "global"):
        return service.imessage("" if case == "global" else "sample")
    return service.get("sample", "slack",
                       account_id="account-one" if case == "account" else "")


def mutate(service, case, method):
    headers = {"Cookie": f"{SESSION_COOKIE_NAME}={create_session_cookie(TEST_SESSION_SECRET)}"}
    response = service.client.request(
        method, f"/agents/sample/tokens/{platform_for(case)}", headers=headers,
        json={"token": NEW, "settings": {"account_id": "account-one"}}
        if method == "PUT" else None,
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("method", ("DELETE", "PUT"))
@pytest.mark.parametrize("resume_at_unlock", (False, True), ids=("after-route", "at-unlock"))
def test_owner_token_mutation_rejects_inflight_adapter(
    service, monkeypatch, case, method, resume_at_unlock,
):
    seed(service, case)
    read, release, finished = threading.Event(), threading.Event(), threading.Event()
    worker = {}
    original = service.registry.get_raw_token

    def gated(*args):
        token = original(*args)
        if not read.is_set():
            worker["ident"] = threading.get_ident()
            read.set()
            assert release.wait(10), "token mutation could not progress while the reader waited"
        return token

    monkeypatch.setattr(service.registry, "get_raw_token", gated)
    # Schedule a worker at the eviction's lock release, exposing a bump moved
    # outside the critical section. The unlocked baseline uses the route barrier.
    lock = inspect.getclosurevars(service.evict).nonlocals.get("_adapter_lock")
    exits = []
    if resume_at_unlock and lock is not None:
        class ReleaseGate:
            def __enter__(self):
                lock.acquire()

            def __exit__(self, *exc):
                lock.release()
                if read.is_set() and threading.get_ident() != worker["ident"]:
                    exits.append(True)
                    if len(exits) == (2 if case == "global" else 1):
                        release.set()
                        assert finished.wait(10), "reader did not finish after cache unlock"

        replace_cell(monkeypatch, service.evict, "_adapter_lock", ReleaseGate())

    def run():
        try:
            return lookup(service, case)
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert read.wait(10)
            mutate(service, case, method)
        finally:
            release.set()
        result = future.result(timeout=10)
    subsequent = lookup(service, case)
    if method == "DELETE":
        assert result is None, "removed credentials escaped to the waiting caller"
        assert subsequent is None, "removed credentials were cached after eviction"
        assert len(service.built) == 1
    else:
        assert result is not None and result.token == NEW
        assert subsequent is result and subsequent.token == NEW
        assert len(service.built) == 2
        result.close.assert_not_called()
    service.built[0].close.assert_called_once_with()
    if resume_at_unlock and lock is not None:
        assert len(exits) >= (2 if case == "global" else 1)


@pytest.mark.parametrize("case", (*CASES, "buzz"))
def test_concurrent_miss_returns_one_adapter_and_closes_extra(service, monkeypatch, case):
    barrier = threading.Barrier(2)
    if case == "buzz":
        ready = Mock()
        monkeypatch.setattr(buzz_dependency, "missing_buzz_dependencies", lambda: [])
        monkeypatch.setattr(service.registry, "mark_buzz_dependency_ready", ready)

        def material(name):
            barrier.wait(timeout=10)
            return SimpleNamespace(private_key=b"fixture-key", relay_url="wss://fixture.invalid",
                                   community_id="fixture-community")

        monkeypatch.setattr(service.registry, "get_buzz_signing_material", material)
        def get():
            return service.get("sample", "buzz")
    else:
        seed(service, case)

        def gated(*args):
            # The route barriers cover real registry reads. Here isolate cache
            # contention from concurrent use of the registry's SQLite connection.
            service.local.token = OLD
            barrier.wait(timeout=10)
            return OLD

        monkeypatch.setattr(service.registry, "get_raw_token", gated)
        monkeypatch.setattr(service.registry, "get_raw_token_for_account", gated)
        monkeypatch.setattr(service.registry, "get_main_agent", lambda: "sample")
        def get():
            return lookup(service, case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(get) for _ in range(2)]
        first, second = [future.result(timeout=15) for future in futures]
    assert first is second
    assert get() is first
    assert len(service.built) == 2
    extra = next(adapter for adapter in service.built if adapter is not first)
    extra.close.assert_called_once_with()
    first.close.assert_not_called()
    if case == "buzz":
        assert ready.call_count == 2
        assert first.options == {"relay_url": "wss://fixture.invalid",
                                 "community_id": "fixture-community"}


@pytest.mark.parametrize("case", CASES)
def test_two_generation_changes_stop_after_one_retry(service, monkeypatch, case):
    seed(service, case)
    original = service.registry.get_raw_token
    reads = []

    def changing(*args):
        token = original(*args)
        reads.append(token)
        assert len(reads) <= 2, "adapter creation retried without a bound"
        service.evict("__global__" if case == "global" else "sample", platform_for(case))
        return token

    monkeypatch.setattr(service.registry, "get_raw_token", changing)
    assert lookup(service, case) is None
    assert len(reads) == len(service.built) == 2
    for adapter in service.built:
        adapter.close.assert_called_once_with()
    monkeypatch.setattr(service.registry, "get_raw_token", original)
    fresh = lookup(service, case)
    assert fresh is service.built[2]
    assert lookup(service, case) is fresh


def test_account_keys_and_unrelated_cache_entries_stay_independent(service):
    seed(service, "slack")
    service.registry.set_token("other", "slack", "fixture-other")
    service.registry.set_token("sample", "discord", "fixture-discord")
    generic = lookup(service, "slack")
    account = lookup(service, "account")
    other = service.get("other", "slack")
    different_platform = service.get("sample", "discord")
    assert generic is not account
    assert service.get("sample", "slack", account_id="wrong-account") is None
    mutate(service, "slack", "PUT")
    assert lookup(service, "slack").token == NEW
    assert lookup(service, "account").token == NEW
    assert service.get("other", "slack") is other
    assert service.get("sample", "discord") is different_platform
    # Existing eviction behavior closes Buzz only, not other cached adapters.
    generic.close.assert_not_called()
    account.close.assert_not_called()


@pytest.mark.parametrize("missing", (False, True))
def test_buzz_dependency_marks_and_empty_material(service, monkeypatch, missing):
    refused, ready, material = Mock(), Mock(), Mock(return_value=None)
    monkeypatch.setattr(buzz_dependency, "missing_buzz_dependencies",
                        lambda: ["fixture-dependency"] if missing else [])
    monkeypatch.setattr(service.registry, "mark_buzz_dependency_refused", refused)
    monkeypatch.setattr(service.registry, "mark_buzz_dependency_ready", ready)
    monkeypatch.setattr(service.registry, "get_buzz_signing_material", material)
    assert service.get("sample", "buzz") is None
    assert not service.built
    if missing:
        refused.assert_called_once_with("sample", "missing:fixture-dependency")
        ready.assert_not_called()
        material.assert_not_called()
    else:
        ready.assert_called_once_with("sample")
        material.assert_called_once_with("sample")
        refused.assert_not_called()
