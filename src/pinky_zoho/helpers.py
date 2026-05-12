"""High-level Zoho helper flows exposed as MCP composite tools."""

from __future__ import annotations

from typing import Any

from .client import ZohoClient
from .schema_cache import SchemaCache


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in ("", None)}


def _records(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data", [])
    return data if isinstance(data, list) else []


def _lookup(record_id: str) -> dict[str, str]:
    return {"id": record_id}


class ZohoHelpers:
    """Composite Zoho workflows built on the low-level client."""

    def __init__(self, client: ZohoClient, schema_cache: SchemaCache) -> None:
        self.client = client
        self.schema_cache = schema_cache

    def book_event(
        self,
        *,
        start: str,
        end: str,
        title: str,
        contact_id: str = "",
        account_id: str = "",
        lead_id: str = "",
        venue: str = "",
        meeting_venue: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        payload = {
            "Event_Title": title,
            "Start_DateTime": start,
            "End_DateTime": end,
            "Venue": venue,
            "Meeting_Venue__s": meeting_venue or self._default_meeting_venue(),
            "Description": description,
        }
        if contact_id or lead_id:
            payload["Who_Id"] = _lookup(contact_id or lead_id)
        if account_id:
            payload["What_Id"] = _lookup(account_id)
            payload["se_module"] = "Accounts"
        elif lead_id:
            payload["se_module"] = "Leads"
        return self.client.post("/crm/Events", {"data": _drop_empty(payload)})

    def log_call(
        self,
        *,
        contact_or_lead_id: str,
        call_type: str = "Outbound",
        subject: str = "",
        description: str = "",
        duration_seconds: int | None = None,
        when: str = "",
    ) -> dict[str, Any]:
        payload = {
            "Who_Id": _lookup(contact_or_lead_id),
            "Call_Type": call_type,
            "Subject": subject or f"{call_type} call",
            "Description": description,
            "Call_Start_Time": when,
            "Call_Duration": duration_seconds,
        }
        return self.client.post("/crm/Calls", {"data": _drop_empty(payload)})

    def create_task(
        self,
        *,
        subject: str,
        due_date: str = "",
        contact_id: str = "",
        account_id: str = "",
        lead_id: str = "",
        priority: str = "Normal",
        status: str = "Not Started",
        description: str = "",
    ) -> dict[str, Any]:
        payload = {
            "Subject": subject,
            "Due_Date": due_date,
            "Priority": priority,
            "Status": status,
            "Description": description,
        }
        if contact_id or lead_id:
            payload["Who_Id"] = _lookup(contact_id or lead_id)
        if account_id:
            payload["What_Id"] = _lookup(account_id)
            payload["se_module"] = "Accounts"
        elif lead_id:
            payload["se_module"] = "Leads"
        return self.client.post("/crm/Tasks", {"data": _drop_empty(payload)})

    def find_or_create_lead(
        self,
        *,
        last_name: str,
        first_name: str = "",
        company: str = "",
        phone: str = "",
        email: str = "",
    ) -> dict[str, Any]:
        for params in self._lead_searches(last_name, first_name, phone, email):
            found = self.client.get("/crm/Leads/search", **params)
            matches = _records(found)
            if matches:
                return {"created": False, "data": matches[0], "search": found}

        payload = {
            "Last_Name": last_name,
            "First_Name": first_name,
            "Company": company or "Unknown",
            "Phone": phone,
            "Email": email,
        }
        created = self.client.post("/crm/Leads", {"data": _drop_empty(payload)})
        return {"created": True, "response": created}

    def _lead_searches(
        self,
        last_name: str,
        first_name: str,
        phone: str,
        email: str,
    ) -> list[dict[str, str]]:
        searches: list[dict[str, str]] = []
        if phone:
            searches.append({"phone": phone})
        if email:
            searches.append({"email": email})
        criteria = f"(Last_Name:equals:{last_name})"
        if first_name:
            criteria = f"({criteria}and(First_Name:equals:{first_name}))"
        searches.append({"criteria": criteria})
        return searches

    def _default_meeting_venue(self) -> str:
        fields = self.schema_cache.get("Events", lambda module: self.client.get(f"/crm/{module}/fields"))
        for field in fields.get("fields", []) or fields.get("data", []):
            if field.get("api_name") != "Meeting_Venue__s":
                continue
            values = [
                item.get("actual_value") or item.get("display_value")
                for item in field.get("pick_list_values", [])
                if item.get("actual_value") or item.get("display_value")
            ]
            for preferred in ("Phone", "In-office"):
                if preferred in values:
                    return preferred
            return values[0] if values else ""
        return ""
