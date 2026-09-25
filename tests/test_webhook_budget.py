"""Independent budgets for valid webhook tokens and unknown-token misses."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from pinky_daemon.routes import triggers as hooks

TOKEN = "whk_test_valid_token"
SECOND_TOKEN = "whk_test_second_token"
UNKNOWN_TOKEN = "whk_test_unknown_token"
NOW = 1_000.0


@dataclass
class _Trigger:
    id: int
    name: str = "test-trigger"
    agent_name: str = "test-agent"
    prompt_template: str = "{{body_raw}}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(hooks, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(hooks, "_hook_ip_buckets", {})
    monkeypatch.setattr(hooks, "_hook_rate_buckets", {})
    triggers = {TOKEN: _Trigger(1), SECOND_TOKEN: _Trigger(2)}

    async def wake(*args):
        pass

    monkeypatch.setattr(
        hooks,
        "_trigger_store",
        SimpleNamespace(
            get_by_token=triggers.get,
            record_fire=lambda trigger_id: None,
        ),
    )
    monkeypatch.setattr(hooks, "_wake_callback", wake)
    monkeypatch.setattr(hooks, "_log", lambda message: None)
    app = FastAPI()
    app.include_router(hooks.router)
    with TestClient(app) as test_client:
        yield test_client


def test_valid_deliveries_from_one_address_not_ip_limited(client):
    statuses = [client.post(f"/hooks/{TOKEN}", json={}).status_code for _ in range(25)]
    assert statuses == [200] * 25


def test_two_tokens_behind_one_address_do_not_share_budget(client):
    statuses = []
    for index in range(44):
        statuses.append(client.post(f"/hooks/{TOKEN}", json={}).status_code)
        if index % 6 == 0 and index < 42:
            statuses.append(client.post(f"/hooks/{SECOND_TOKEN}", json={}).status_code)
    assert statuses == [200] * 51


def test_misses_still_bounded_per_address(client):
    statuses = [client.post(f"/hooks/{UNKNOWN_TOKEN}", json={}).status_code for _ in range(20)]
    assert statuses == [404] * 20
    response = client.post(f"/hooks/{UNKNOWN_TOKEN}", json={})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert client.post(f"/hooks/{TOKEN}", json={}).status_code == 200
    assert len(hooks._hook_ip_buckets["testclient"]) == 20


@pytest.mark.parametrize(
    "bucket,token,count",
    [
        ("_hook_ip_buckets", UNKNOWN_TOKEN, 20),
        ("_hook_rate_buckets", TOKEN, 60),
    ],
)
@pytest.mark.parametrize("age,expected", [(45.0, 15), (59.9, 1), (0.0, 60)])
def test_retry_after_on_ip_and_token_429(client, bucket, token, count, age, expected):
    key = "testclient" if bucket == "_hook_ip_buckets" else token
    stamps = [NOW - age] * count
    getattr(hooks, bucket)[key] = stamps.copy()
    response = client.post(f"/hooks/{token}", json={})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(expected)
    assert getattr(hooks, bucket)[key] == stamps


def test_valid_traffic_creates_no_ip_bucket(client):
    for _ in range(5):
        assert client.post(f"/hooks/{TOKEN}", json={}).status_code == 200
    assert hooks._hook_ip_buckets == {}


def test_miss_does_not_read_body(client, monkeypatch):
    reads = []
    original_body = Request.body

    async def body(request):
        reads.append(request.url.path)
        return await original_body(request)

    monkeypatch.setattr(Request, "body", body)
    response = client.post(f"/hooks/{UNKNOWN_TOKEN}", content=b"x" * 2_097_152)
    assert response.status_code == 404
    assert reads == []
