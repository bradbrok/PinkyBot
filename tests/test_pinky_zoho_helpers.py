from __future__ import annotations

from typing import Any

from pinky_zoho.helpers import ZohoHelpers
from pinky_zoho.schema_cache import SchemaCache


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.search_results: list[dict[str, Any]] = []

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        if path == "/crm/Events/fields":
            return {
                "fields": [{
                    "api_name": "Meeting_Venue__s",
                    "pick_list_values": [{"actual_value": "Phone"}, {"actual_value": "In-office"}],
                }]
            }
        if path == "/crm/Leads/search":
            return self.search_results.pop(0) if self.search_results else {"data": []}
        return {"ok": True}

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path, body or {}))
        return {"posted": path, "body": body}


def test_book_event_builds_related_event_payload_with_schema_default() -> None:
    client = FakeClient()
    helpers = ZohoHelpers(client, SchemaCache())

    result = helpers.book_event(
        start="2026-05-12T10:00:00-07:00",
        end="2026-05-12T10:30:00-07:00",
        title="Intro call",
        contact_id="c1",
        account_id="a1",
    )

    assert result["posted"] == "/crm/Events"
    payload = result["body"]["data"]
    assert payload["Event_Title"] == "Intro call"
    assert payload["Who_Id"] == {"id": "c1"}
    assert payload["What_Id"] == {"id": "a1"}
    assert payload["se_module"] == "Accounts"
    assert payload["Meeting_Venue__s"] == "Phone"


def test_create_task_uses_lead_relationship_when_no_account() -> None:
    client = FakeClient()
    helpers = ZohoHelpers(client, SchemaCache())

    result = helpers.create_task(subject="Follow up", lead_id="l1")

    payload = result["body"]["data"]
    assert payload["Who_Id"] == {"id": "l1"}
    assert "What_Id" not in payload
    assert payload["se_module"] == "Leads"
    assert payload["Priority"] == "Normal"
    assert payload["Status"] == "Not Started"


def test_book_event_uses_lead_without_what_id_when_no_account() -> None:
    client = FakeClient()
    helpers = ZohoHelpers(client, SchemaCache())

    result = helpers.book_event(start="2026-05-12T10:00:00-07:00", end="2026-05-12T10:30:00-07:00", title="Lead call", lead_id="l1")

    payload = result["body"]["data"]
    assert payload["Who_Id"] == {"id": "l1"}
    assert "What_Id" not in payload
    assert payload["se_module"] == "Leads"


def test_log_call_uses_lookup_object_for_who_id() -> None:
    client = FakeClient()
    helpers = ZohoHelpers(client, SchemaCache())

    result = helpers.log_call(contact_or_lead_id="c1", subject="Call summary")

    payload = result["body"]["data"]
    assert payload["Who_Id"] == {"id": "c1"}
    assert payload["Subject"] == "Call summary"


def test_find_or_create_lead_returns_existing_match_before_create() -> None:
    client = FakeClient()
    client.search_results = [{"data": []}, {"data": [{"id": "lead1"}]}]
    helpers = ZohoHelpers(client, SchemaCache())

    result = helpers.find_or_create_lead(last_name="Smith", phone="555", email="a@b.test")

    assert result["created"] is False
    assert result["data"] == {"id": "lead1"}
    assert client.calls == [
        ("GET", "/crm/Leads/search", {"phone": "555"}),
        ("GET", "/crm/Leads/search", {"email": "a@b.test"}),
    ]


def test_find_or_create_lead_creates_when_searches_miss() -> None:
    client = FakeClient()
    helpers = ZohoHelpers(client, SchemaCache())

    result = helpers.find_or_create_lead(last_name="Smith", first_name="Ava")

    assert result["created"] is True
    assert client.calls[-1] == (
        "POST",
        "/crm/Leads",
        {"data": {"Last_Name": "Smith", "First_Name": "Ava", "Company": "Unknown"}},
    )
