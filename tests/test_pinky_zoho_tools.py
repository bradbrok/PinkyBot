from __future__ import annotations

import inspect
import json
from typing import Any

from pinky_zoho.schema_cache import SchemaCache
from pinky_zoho.server import ZohoToolbox


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        return {"path": path, "params": params}

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        return {"path": path, "body": body}

    def put(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("PUT", path, body))
        return {"path": path, "body": body}

    def patch(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("PATCH", path, body))
        return {"path": path, "body": body}


def test_toolbox_methods_do_not_accept_agent_parameter() -> None:
    for name, method in inspect.getmembers(ZohoToolbox, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        assert "agent" not in inspect.signature(method).parameters


def test_email_approvals_maps_state_to_live_server_status_param() -> None:
    client = FakeClient()
    toolbox = ZohoToolbox(client, SchemaCache())

    result = json.loads(toolbox.email_approvals(state="approved", limit=3))

    assert result == {"path": "/email/approvals", "params": {"status": "approved", "limit": 3}}
    assert client.calls == [("GET", "/email/approvals", {"status": "approved", "limit": 3})]


def test_email_approvals_defaults_to_pending_status() -> None:
    client = FakeClient()
    toolbox = ZohoToolbox(client, SchemaCache())

    result = json.loads(toolbox.email_approvals(limit=2))

    assert result == {"path": "/email/approvals", "params": {"status": "pending", "limit": 2}}


def test_desk_tickets_uses_live_server_pagination_params() -> None:
    client = FakeClient()
    toolbox = ZohoToolbox(client, SchemaCache())

    result = json.loads(toolbox.desk_tickets(from_index=11, view_id="v1"))

    assert result["path"] == "/desk/tickets"
    assert result["params"]["from_index"] == 11
    assert result["params"]["view_id"] == "v1"
    assert "page" not in result["params"]
    assert "all" not in result["params"]


def test_project_task_endpoint_is_exposed() -> None:
    client = FakeClient()
    toolbox = ZohoToolbox(client, SchemaCache())

    result = json.loads(toolbox.project_task("p1", "t1"))

    assert result == {"path": "/projects/p1/tasks/t1", "params": {}}


def test_crm_send_email_uses_server_wire_shape() -> None:
    client = FakeClient()
    toolbox = ZohoToolbox(client, SchemaCache())

    result = json.loads(toolbox.crm_send_email(
        "Contacts",
        "c1",
        "owner@example.com",
        "client@example.com",
        subject="Hello",
        content="Body",
    ))

    assert result["path"] == "/crm/Contacts/c1/email"
    assert result["body"]["data"] == [{
        "from": {"email": "owner@example.com"},
        "to": [{"email": "client@example.com"}],
        "subject": "Hello",
        "content": "Body",
    }]
