"""Signed read enforcement denies broad answers and unassigned skill text."""

import pytest
from fastapi.testclient import TestClient

from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth


@pytest.fixture
def enforced(daemon, monkeypatch):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    return instance


@pytest.fixture
def client(enforced):
    instance = TestClient(enforced.app)
    try:
        yield instance
    finally:
        instance.close()


def test_enforce_restricts_roster_to_caller(enforced, client):
    response = client.get("/agents", headers=signed(enforced, "GET", "/agents"))
    assert response.status_code == 200
    assert {row["name"] for row in response.json()["agents"]} == {"tenant"}


def test_enforce_refuses_unlisted_system_read(enforced, client):
    path = "/system/primary-user"
    response = client.get(path, headers=signed(enforced, "GET", path))
    assert response.status_code == 403
    assert response.json() == {
        "error": "isolated agent 'tenant' cannot read this route", "agent": "tenant",
    }


def test_enforce_hides_unassigned_skill(enforced, client):
    name = "unassigned-fixture"
    path = f"/skills/{name}"
    missing = client.get(path, headers=signed(enforced, "GET", path))
    enforced.skills.register(name, directive="Private skill fixture text")
    response = client.get(path, headers=signed(enforced, "GET", path))
    assert missing.status_code == 404
    assert response.status_code == 404
    assert response.json() == missing.json()
