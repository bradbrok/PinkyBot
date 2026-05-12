"""Pinky Zoho MCP server tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ZohoClient
from .helpers import ZohoHelpers
from .schema_cache import SchemaCache


def _json(result: dict[str, Any]) -> str:
    return json.dumps(result)


class ZohoToolbox:
    """Testable implementation behind the MCP function wrappers."""

    def __init__(self, client: ZohoClient, schema_cache: SchemaCache | None = None) -> None:
        self.client = client
        self.schema_cache = schema_cache or SchemaCache()
        self.helpers = ZohoHelpers(client, self.schema_cache)

    def health(self) -> str:
        return _json(self.client.get("/health"))

    def whoami(self) -> str:
        return _json(self.client.get("/whoami"))

    def audit(self, module: str = "", operation: str = "", since: str = "", limit: int = 50) -> str:
        return _json(self.client.get("/audit", module=module, operation=operation, since=since, limit=limit))

    def crm_modules(self) -> str:
        return _json(self.client.get("/crm/modules"))

    def crm_fields(self, module: str, refresh: bool = False) -> str:
        def fetch(name: str) -> dict[str, Any]:
            return self.client.get(f"/crm/{name}/fields")

        result = self.schema_cache.refresh(module, fetch) if refresh else self.schema_cache.get(module, fetch)
        return _json(result)

    def crm_search(
        self,
        module: str,
        criteria: str = "",
        word: str = "",
        email: str = "",
        phone: str = "",
        page: int = 1,
        limit: int = 20,
        page_token: str = "",
        all: bool = False,
        max_records: int = 5000,
    ) -> str:
        return _json(self.client.get(
            f"/crm/{module}/search",
            criteria=criteria,
            word=word,
            email=email,
            phone=phone,
            page=page,
            limit=limit,
            page_token=page_token,
            all=all,
            max_records=max_records,
        ))

    def crm_get(self, module: str, record_id: str) -> str:
        return _json(self.client.get(f"/crm/{module}/{record_id}"))

    def crm_list(
        self,
        module: str,
        fields: str = "",
        sort: str = "",
        order: str = "desc",
        page: int = 1,
        limit: int = 20,
        criteria: str = "",
        page_token: str = "",
        all: bool = False,
        max_records: int = 5000,
    ) -> str:
        return _json(self.client.get(
            f"/crm/{module}",
            fields=fields,
            sort=sort,
            order=order,
            page=page,
            limit=limit,
            criteria=criteria,
            page_token=page_token,
            all=all,
            max_records=max_records,
        ))

    def crm_related(
        self,
        module: str,
        record_id: str,
        related_module: str,
        fields: str = "",
        page: int = 1,
        limit: int = 20,
    ) -> str:
        return _json(self.client.get(
            f"/crm/{module}/{record_id}/related/{related_module}",
            fields=fields,
            page=page,
            limit=limit,
        ))

    def crm_create(self, module: str, data: dict[str, Any] | list[dict[str, Any]]) -> str:
        return _json(self.client.post(f"/crm/{module}", {"data": data}))

    def crm_update(self, module: str, record_id: str, data: dict[str, Any] | list[dict[str, Any]]) -> str:
        return _json(self.client.put(f"/crm/{module}/{record_id}", {"data": data}))

    def crm_add_note(self, module: str, record_id: str, title: str, content: str) -> str:
        return _json(self.client.post(f"/crm/{module}/{record_id}/notes", {
            "title": title,
            "content": content,
        }))

    def crm_notes(self, module: str, record_id: str) -> str:
        return _json(self.client.get(f"/crm/{module}/{record_id}/notes"))

    def email_templates(self, module: str = "") -> str:
        return _json(self.client.get("/email/templates", module=module))

    def email_template(self, template_id: str) -> str:
        return _json(self.client.get(f"/email/templates/{template_id}"))

    def email_from_addresses(self) -> str:
        return _json(self.client.get("/email/from-addresses"))

    def crm_send_email(
        self,
        module: str,
        record_id: str,
        from_email: str,
        to_email: str,
        subject: str = "",
        content: str = "",
        template_id: str = "",
    ) -> str:
        mail: dict[str, Any] = {
            "from": {"email": from_email},
            "to": [{"email": to_email}],
            "subject": subject,
            "content": content,
        }
        if template_id:
            mail["template"] = {"id": template_id}
        return _json(self.client.post(f"/crm/{module}/{record_id}/email", {"data": [mail]}))

    def email_approvals(self, state: str = "", limit: int = 20) -> str:
        return _json(self.client.get("/email/approvals", status=state or "pending", limit=limit))

    def email_approval(self, approval_id: str) -> str:
        return _json(self.client.get(f"/email/approvals/{approval_id}"))

    def email_approve(self, approval_id: str) -> str:
        return _json(self.client.post(f"/email/approvals/{approval_id}/approve"))

    def email_reject(self, approval_id: str, reason: str = "") -> str:
        body = {"reason": reason} if reason else None
        return _json(self.client.post(f"/email/approvals/{approval_id}/reject", body))

    def desk_tickets(
        self,
        status: str = "",
        sort: str = "-modifiedTime",
        limit: int = 20,
        from_index: int = 1,
        assignee: str = "",
        department_id: str = "",
        channel: str = "",
        priority: str = "",
        created_time_range: str = "",
        modified_time_range: str = "",
        received_in_days: int | None = None,
        view_id: str = "",
        include: str = "",
    ) -> str:
        return _json(self.client.get(
            "/desk/tickets",
            status=status,
            sort=sort,
            limit=limit,
            from_index=from_index,
            assignee=assignee,
            department_id=department_id,
            channel=channel,
            priority=priority,
            created_time_range=created_time_range,
            modified_time_range=modified_time_range,
            received_in_days=received_in_days,
            view_id=view_id,
            include=include,
        ))

    def desk_agents(self, limit: int = 50, from_index: int = 1) -> str:
        return _json(self.client.get("/desk/agents", limit=limit, from_index=from_index))

    def desk_views(
        self,
        module: str = "tickets",
        department_id: str = "",
        limit: int = 50,
        from_index: int = 1,
    ) -> str:
        return _json(self.client.get(
            "/desk/views",
            module=module,
            department_id=department_id,
            limit=limit,
            from_index=from_index,
        ))

    def desk_ticket(self, ticket_id: str) -> str:
        return _json(self.client.get(f"/desk/tickets/{ticket_id}"))

    def desk_create_ticket(self, data: dict[str, Any]) -> str:
        return _json(self.client.post("/desk/tickets", data))

    def desk_update_ticket(self, ticket_id: str, data: dict[str, Any]) -> str:
        return _json(self.client.patch(f"/desk/tickets/{ticket_id}", data))

    def desk_ticket_comments(self, ticket_id: str) -> str:
        return _json(self.client.get(f"/desk/tickets/{ticket_id}/comments"))

    def desk_add_ticket_comment(self, ticket_id: str, content: str, is_public: bool = False) -> str:
        return _json(self.client.post(f"/desk/tickets/{ticket_id}/comments", {
            "content": content,
            "is_public": is_public,
        }))

    def desk_search(self, q: str, module: str = "tickets", limit: int = 20, from_index: int = 1) -> str:
        return _json(self.client.get(
            "/desk/search",
            module=module,
            q=q,
            limit=limit,
            from_index=from_index,
        ))

    def projects(self, status: str = "active", page: int = 1, limit: int = 100) -> str:
        return _json(self.client.get("/projects", status=status, page=page, limit=limit))

    def project(self, project_id: str) -> str:
        return _json(self.client.get(f"/projects/{project_id}"))

    def project_tasks(self, project_id: str, page: int = 1, limit: int = 100) -> str:
        return _json(self.client.get(f"/projects/{project_id}/tasks", page=page, limit=limit))

    def project_task(self, project_id: str, task_id: str) -> str:
        return _json(self.client.get(f"/projects/{project_id}/tasks/{task_id}"))

    def project_milestones(self, project_id: str) -> str:
        return _json(self.client.get(f"/projects/{project_id}/milestones"))

    def book_event(
        self,
        start: str,
        end: str,
        title: str,
        contact_id: str = "",
        account_id: str = "",
        lead_id: str = "",
        venue: str = "",
        meeting_venue: str = "",
        description: str = "",
    ) -> str:
        return _json(self.helpers.book_event(
            start=start,
            end=end,
            title=title,
            contact_id=contact_id,
            account_id=account_id,
            lead_id=lead_id,
            venue=venue,
            meeting_venue=meeting_venue,
            description=description,
        ))

    def log_call(
        self,
        contact_or_lead_id: str,
        call_type: str = "Outbound",
        subject: str = "",
        description: str = "",
        duration_seconds: int | None = None,
        when: str = "",
    ) -> str:
        return _json(self.helpers.log_call(
            contact_or_lead_id=contact_or_lead_id,
            call_type=call_type,
            subject=subject,
            description=description,
            duration_seconds=duration_seconds,
            when=when,
        ))

    def create_task(
        self,
        subject: str,
        due_date: str = "",
        contact_id: str = "",
        account_id: str = "",
        lead_id: str = "",
        priority: str = "Normal",
        status: str = "Not Started",
        description: str = "",
    ) -> str:
        return _json(self.helpers.create_task(
            subject=subject,
            due_date=due_date,
            contact_id=contact_id,
            account_id=account_id,
            lead_id=lead_id,
            priority=priority,
            status=status,
            description=description,
        ))

    def find_or_create_lead(
        self,
        last_name: str,
        first_name: str = "",
        company: str = "",
        phone: str = "",
        email: str = "",
    ) -> str:
        return _json(self.helpers.find_or_create_lead(
            last_name=last_name,
            first_name=first_name,
            company=company,
            phone=phone,
            email=email,
        ))


def create_server(
    *,
    agent_name: str,
    api_url: str = "",
    secret: str = "",
    host: str = "127.0.0.1",
    port: int = 8108,
    client: ZohoClient | None = None,
) -> FastMCP:
    """Create the pinky-zoho MCP server."""
    mcp = FastMCP("pinky-zoho", host=host, port=port)
    toolbox = ZohoToolbox(client or ZohoClient(agent_name=agent_name, api_url=api_url, secret=secret))

    @mcp.tool()
    def zoho_health() -> str:
        """Check Zoho API server health."""
        return toolbox.health()

    @mcp.tool()
    def zoho_whoami() -> str:
        """Show the bound agent's Zoho permissions and scoping."""
        return toolbox.whoami()

    @mcp.tool()
    def zoho_audit(module: str = "", operation: str = "", since: str = "", limit: int = 50) -> str:
        """Read this agent's Zoho audit log."""
        return toolbox.audit(module, operation, since, limit)

    @mcp.tool()
    def zoho_crm_modules() -> str:
        """List accessible CRM modules."""
        return toolbox.crm_modules()

    @mcp.tool()
    def zoho_crm_fields(module: str, refresh: bool = False) -> str:
        """Get CRM field metadata for a module, cached in-process for one hour."""
        return toolbox.crm_fields(module, refresh)

    @mcp.tool()
    def zoho_crm_search(
        module: str,
        criteria: str = "",
        word: str = "",
        email: str = "",
        phone: str = "",
        page: int = 1,
        limit: int = 20,
        page_token: str = "",
        all: bool = False,
        max_records: int = 5000,
    ) -> str:
        """Search CRM records by criteria, word, email, or phone."""
        return toolbox.crm_search(module, criteria, word, email, phone, page, limit, page_token, all, max_records)

    @mcp.tool()
    def zoho_crm_get(module: str, record_id: str) -> str:
        """Get a single CRM record."""
        return toolbox.crm_get(module, record_id)

    @mcp.tool()
    def zoho_crm_list(
        module: str,
        fields: str = "",
        sort: str = "",
        order: str = "desc",
        page: int = 1,
        limit: int = 20,
        criteria: str = "",
        page_token: str = "",
        all: bool = False,
        max_records: int = 5000,
    ) -> str:
        """List CRM records."""
        return toolbox.crm_list(module, fields, sort, order, page, limit, criteria, page_token, all, max_records)

    @mcp.tool()
    def zoho_crm_related(
        module: str,
        record_id: str,
        related_module: str,
        fields: str = "",
        page: int = 1,
        limit: int = 20,
    ) -> str:
        """List related CRM records."""
        return toolbox.crm_related(module, record_id, related_module, fields, page, limit)

    @mcp.tool()
    def zoho_crm_create(module: str, data: dict[str, Any] | list[dict[str, Any]]) -> str:
        """Create a CRM record."""
        return toolbox.crm_create(module, data)

    @mcp.tool()
    def zoho_crm_update(module: str, record_id: str, data: dict[str, Any] | list[dict[str, Any]]) -> str:
        """Update a CRM record."""
        return toolbox.crm_update(module, record_id, data)

    @mcp.tool()
    def zoho_crm_add_note(module: str, record_id: str, title: str, content: str) -> str:
        """Add a note to a CRM record."""
        return toolbox.crm_add_note(module, record_id, title, content)

    @mcp.tool()
    def zoho_crm_notes(module: str, record_id: str) -> str:
        """List notes on a CRM record."""
        return toolbox.crm_notes(module, record_id)

    @mcp.tool()
    def zoho_email_templates(module: str = "") -> str:
        """List available email templates."""
        return toolbox.email_templates(module)

    @mcp.tool()
    def zoho_email_template(template_id: str) -> str:
        """Get one email template."""
        return toolbox.email_template(template_id)

    @mcp.tool()
    def zoho_email_from_addresses() -> str:
        """List available email sender addresses."""
        return toolbox.email_from_addresses()

    @mcp.tool()
    def zoho_crm_send_email(
        module: str,
        record_id: str,
        from_email: str,
        to_email: str,
        subject: str = "",
        content: str = "",
        template_id: str = "",
    ) -> str:
        """Send or queue an email through the Zoho approval gate."""
        return toolbox.crm_send_email(module, record_id, from_email, to_email, subject, content, template_id)

    @mcp.tool()
    def zoho_email_approvals(state: str = "", limit: int = 20) -> str:
        """List email approvals for the bound agent identity."""
        return toolbox.email_approvals(state, limit)

    @mcp.tool()
    def zoho_email_approval(approval_id: str) -> str:
        """Get one email approval."""
        return toolbox.email_approval(approval_id)

    @mcp.tool()
    def zoho_email_approve(approval_id: str) -> str:
        """Approve a pending email."""
        return toolbox.email_approve(approval_id)

    @mcp.tool()
    def zoho_email_reject(approval_id: str, reason: str = "") -> str:
        """Reject a pending email."""
        return toolbox.email_reject(approval_id, reason)

    @mcp.tool()
    def zoho_desk_tickets(
        status: str = "",
        sort: str = "-modifiedTime",
        limit: int = 20,
        from_index: int = 1,
        assignee: str = "",
        department_id: str = "",
        channel: str = "",
        priority: str = "",
        created_time_range: str = "",
        modified_time_range: str = "",
        received_in_days: int | None = None,
        view_id: str = "",
        include: str = "",
    ) -> str:
        """List Desk tickets."""
        return toolbox.desk_tickets(
            status,
            sort,
            limit,
            from_index,
            assignee,
            department_id,
            channel,
            priority,
            created_time_range,
            modified_time_range,
            received_in_days,
            view_id,
            include,
        )

    @mcp.tool()
    def zoho_desk_agents(limit: int = 50, from_index: int = 1) -> str:
        """List Desk agents."""
        return toolbox.desk_agents(limit, from_index)

    @mcp.tool()
    def zoho_desk_views(
        module: str = "tickets",
        department_id: str = "",
        limit: int = 50,
        from_index: int = 1,
    ) -> str:
        """List Desk views."""
        return toolbox.desk_views(module, department_id, limit, from_index)

    @mcp.tool()
    def zoho_desk_ticket(ticket_id: str) -> str:
        """Get a Desk ticket."""
        return toolbox.desk_ticket(ticket_id)

    @mcp.tool()
    def zoho_desk_create_ticket(data: dict[str, Any]) -> str:
        """Create a Desk ticket."""
        return toolbox.desk_create_ticket(data)

    @mcp.tool()
    def zoho_desk_update_ticket(ticket_id: str, data: dict[str, Any]) -> str:
        """Update a Desk ticket."""
        return toolbox.desk_update_ticket(ticket_id, data)

    @mcp.tool()
    def zoho_desk_ticket_comments(ticket_id: str) -> str:
        """List Desk ticket comments."""
        return toolbox.desk_ticket_comments(ticket_id)

    @mcp.tool()
    def zoho_desk_add_ticket_comment(ticket_id: str, content: str, is_public: bool = False) -> str:
        """Add a Desk ticket comment."""
        return toolbox.desk_add_ticket_comment(ticket_id, content, is_public)

    @mcp.tool()
    def zoho_desk_search(q: str, module: str = "tickets", limit: int = 20, from_index: int = 1) -> str:
        """Search Desk records."""
        return toolbox.desk_search(q, module, limit, from_index)

    @mcp.tool()
    def zoho_projects(status: str = "active", page: int = 1, limit: int = 100) -> str:
        """List Zoho Projects projects."""
        return toolbox.projects(status, page, limit)

    @mcp.tool()
    def zoho_project(project_id: str) -> str:
        """Get a Zoho Projects project."""
        return toolbox.project(project_id)

    @mcp.tool()
    def zoho_project_tasks(project_id: str, page: int = 1, limit: int = 100) -> str:
        """List project tasks."""
        return toolbox.project_tasks(project_id, page, limit)

    @mcp.tool()
    def zoho_project_task(project_id: str, task_id: str) -> str:
        """Get one project task."""
        return toolbox.project_task(project_id, task_id)

    @mcp.tool()
    def zoho_project_milestones(project_id: str) -> str:
        """List project milestones."""
        return toolbox.project_milestones(project_id)

    @mcp.tool()
    def zoho_book_event(
        start: str,
        end: str,
        title: str,
        contact_id: str = "",
        account_id: str = "",
        lead_id: str = "",
        venue: str = "",
        meeting_venue: str = "",
        description: str = "",
    ) -> str:
        """Create an Events record with the common relationship fields filled in."""
        return toolbox.book_event(start, end, title, contact_id, account_id, lead_id, venue, meeting_venue, description)

    @mcp.tool()
    def zoho_log_call(
        contact_or_lead_id: str,
        call_type: str = "Outbound",
        subject: str = "",
        description: str = "",
        duration_seconds: int | None = None,
        when: str = "",
    ) -> str:
        """Create a Calls record for a contact or lead."""
        return toolbox.log_call(contact_or_lead_id, call_type, subject, description, duration_seconds, when)

    @mcp.tool()
    def zoho_create_task(
        subject: str,
        due_date: str = "",
        contact_id: str = "",
        account_id: str = "",
        lead_id: str = "",
        priority: str = "Normal",
        status: str = "Not Started",
        description: str = "",
    ) -> str:
        """Create a Tasks record with common relationship fields filled in."""
        return toolbox.create_task(subject, due_date, contact_id, account_id, lead_id, priority, status, description)

    @mcp.tool()
    def zoho_find_or_create_lead(
        last_name: str,
        first_name: str = "",
        company: str = "",
        phone: str = "",
        email: str = "",
    ) -> str:
        """Find a lead by phone, email, or name criteria, creating one if no match exists."""
        return toolbox.find_or_create_lead(last_name, first_name, company, phone, email)

    return mcp
