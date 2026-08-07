"""Verified-contact registry, migration seed, and API coverage."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import create_api

BRAD_PRINCIPAL = (
    "buzz:posspecialists:"
    "90425c785cf23b60e57300658a7f4855938b3c2f661b3ef33acdb54831fcb44b"
)


def test_verified_contacts_seed_ships_for_barsik_and_is_one_shot(tmp_path):
    db_path = str(tmp_path / "agents.db")
    registry = AgentRegistry(db_path)
    registry.register("barsik", model="sonnet", working_dir=str(tmp_path / "barsik"))

    contact = registry.get_verified_contact("barsik", "buzz", BRAD_PRINCIPAL)
    assert contact == {
        "id": 1,
        "agent_name": "barsik",
        "platform": "buzz",
        "principal": BRAD_PRINCIPAL,
        "name": "Brad",
        "role": "owner",
        "added_at": contact["added_at"],
    }
    assert registry.delete_verified_contact("barsik", "buzz", BRAD_PRINCIPAL)
    registry.close()

    reopened = AgentRegistry(db_path)
    assert reopened.get_verified_contact("barsik", "buzz", BRAD_PRINCIPAL) is None
    reopened.close()


def test_verified_contact_registry_is_agent_and_platform_scoped(tmp_path):
    registry = AgentRegistry(str(tmp_path / "agents.db"))
    registry.register("kuzya", model="sonnet", working_dir=str(tmp_path / "kuzya"))
    registry.register("murzik", model="sonnet", working_dir=str(tmp_path / "murzik"))

    first = registry.upsert_verified_contact(
        "kuzya", "buzz", "buzz:community:abc", "Alice", "agent"
    )
    registry.upsert_verified_contact(
        "kuzya", "mesh", "buzz:community:abc", "Mesh Alice"
    )
    registry.upsert_verified_contact(
        "murzik", "buzz", "buzz:community:abc", "Other Alice"
    )
    updated = registry.upsert_verified_contact(
        "kuzya", "buzz", "buzz:community:abc", "Alice Updated", "owner"
    )

    assert updated["id"] == first["id"]
    assert (updated["name"], updated["role"]) == ("Alice Updated", "owner")
    assert [item["name"] for item in registry.list_verified_contacts("kuzya")] == [
        "Alice Updated",
        "Mesh Alice",
    ]
    assert registry.get_verified_contact(
        "murzik", "buzz", "buzz:community:abc"
    )["name"] == "Other Alice"
    registry.close()


def test_verified_contacts_api_get_put_delete(tmp_path):
    app = create_api(
        max_sessions=1,
        default_working_dir=str(tmp_path),
        db_path=str(tmp_path / "api.db"),
    )
    app.state.agents.register(
        "kuzya", model="sonnet", working_dir=str(tmp_path / "kuzya")
    )
    principal = "buzz:community:" + "aa" * 32

    client = TestClient(app)
    try:
        response = client.put(
            "/agents/kuzya/verified-contacts",
            params={
                "platform": "buzz",
                "principal": principal,
                "name": "Alice",
                "role": "agent",
            },
        )
        assert response.status_code == 200
        contact = response.json()["verified_contact"]
        assert {**contact, "added_at": 0} == {
            "id": 1,
            "agent_name": "kuzya",
            "platform": "buzz",
            "principal": principal,
            "name": "Alice",
            "role": "agent",
            "added_at": 0,
        }

        response = client.get("/agents/kuzya/verified-contacts")
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["verified_contacts"][0]["principal"] == principal

        response = client.delete(
            "/agents/kuzya/verified-contacts",
            params={"platform": "buzz", "principal": principal},
        )
        assert response.status_code == 200
        assert response.json() == {
            "deleted": True,
            "platform": "buzz",
            "principal": principal,
        }
        assert client.get("/agents/kuzya/verified-contacts").json()["count"] == 0
    finally:
        client.close()
