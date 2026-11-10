"""Catalog discovery and skill reads respect the signed caller's read boundary."""

import pytest

from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_read_enforce import _read

pytestmark = pytest.mark.real_auth


def test_available_catalog_hides_unassigned_skill_bodies(daemon, monkeypatch):
    instance = daemon()
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    marker = "private-catalog-directive-fixture"
    instance.skills.register("catalog-private", directive=marker, origin_agent="peer")
    instance.skills.assign_to_agent("peer", "catalog-private")
    named = _read(instance, "/skills/catalog-private")
    available = _read(instance, "/agents/tenant/skills/available")
    assert named.status_code == 404
    assert available.status_code == 200
    assert marker not in available.text


def test_shared_skill_loads_without_an_explicit_assignment(daemon, monkeypatch):
    instance = daemon()
    instance.skills.register("catalog-shared", directive="Shared fixture text", shared=True)
    assert instance.skills.is_assigned("tenant", "catalog-shared")
    assert "tenant" not in instance.skills.assigned_agents("catalog-shared")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    listing = _read(instance, "/agents/tenant/skills")
    response = _read(instance, "/skills/catalog-shared")
    assert any(row["name"] == "catalog-shared" for row in listing.json()["skills"])
    assert response.status_code == 200
    assert response.json()["directive"] == "Shared fixture text"
