"""Pi-side MCP surface for the private RingCentral Mini bridge."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from pinky_ringcentral.mcp_client import RingCentralBridgeClient
from pinky_ringcentral.service import DEFAULT_RECIPIENT_TIMEZONE


def _json(result: dict[str, Any]) -> str:
    return json.dumps(result, separators=(",", ":"), sort_keys=True)


class RingCentralToolbox:
    def __init__(self, client: RingCentralBridgeClient) -> None:
        self.client = client

    def send_sms(
        self,
        to: str,
        text: str,
        template_id: str = "",
        recipient_timezone: str = DEFAULT_RECIPIENT_TIMEZONE,
    ) -> str:
        return _json(
            self.client.post(
                "/v1/sms/send",
                {
                    "to": to,
                    "text": text,
                    "template_id": template_id,
                    "recipient_timezone": recipient_timezone,
                },
            )
        )

    def get_sms_history(
        self, phone_number: str, limit: int = 100, days: int = 30
    ) -> str:
        return _json(
            self.client.get(
                "/v1/sms/history",
                phone_number=phone_number,
                limit=limit,
                days=days,
            )
        )

    def get_sms_status(self, message_id: str) -> str:
        return _json(
            self.client.get(f"/v1/sms/{message_id}/status")
        )

    def add_do_not_text(
        self, phone_number: str, reason: str = "manual"
    ) -> str:
        return _json(
            self.client.post(
                "/v1/do-not-text/add",
                {"phone_number": phone_number, "reason": reason},
            )
        )

    def remove_do_not_text(self, phone_number: str) -> str:
        return _json(
            self.client.post(
                "/v1/do-not-text/remove",
                {"phone_number": phone_number},
            )
        )

    def list_do_not_text(self) -> str:
        return _json(self.client.get("/v1/do-not-text"))


def create_server(
    *,
    agent_name: str,
    secret: str,
    instance_id: str,
    bridge_url: str = "",
) -> FastMCP:
    client = RingCentralBridgeClient(
        agent_name=agent_name,
        secret=secret,
        instance_id=instance_id,
        bridge_url=bridge_url,
    )
    toolbox = RingCentralToolbox(client)
    mcp = FastMCP("pinky-ringcentral")

    @mcp.tool()
    def send_sms(
        to: str,
        text: str,
        template_id: str = "",
        recipient_timezone: str = DEFAULT_RECIPIENT_TIMEZONE,
    ) -> str:
        """Send one SMS from Geordi's (925) 471-4225 RingCentral line.

        The bridge always re-checks the authoritative do_not_text registry at
        transmission time. It rejects sends outside 08:00–20:00 recipient
        local time and returns ``next_allowed_at``; it never queues a send.
        ``recipient_timezone`` is an IANA name and defaults to
        America/Los_Angeles when omitted. ``template_id`` is an optional audit
        correlation value; this primitive does not render templates.
        """
        return toolbox.send_sms(
            to, text, template_id, recipient_timezone
        )

    @mcp.tool()
    def get_sms_history(
        phone_number: str, limit: int = 100, days: int = 30
    ) -> str:
        """Rehydrate the general SMS thread for a phone number.

        Returns paginated RingCentral message-store records in chronological
        order. Use before composing any merchant text; the general record shape
        is also suitable for ticket correlation and voice briefing packs.
        """
        return toolbox.get_sms_history(phone_number, limit, days)

    @mcp.tool()
    def get_sms_status(message_id: str) -> str:
        """Query current delivery status and carrier error for a sent SMS."""
        return toolbox.get_sms_status(message_id)

    @mcp.tool()
    def add_do_not_text(
        phone_number: str, reason: str = "manual"
    ) -> str:
        """Add a destination to the authoritative bridge-side DNT registry."""
        return toolbox.add_do_not_text(phone_number, reason)

    @mcp.tool()
    def remove_do_not_text(phone_number: str) -> str:
        """Remove a DNT entry only on explicit Ryan or Brad instruction."""
        return toolbox.remove_do_not_text(phone_number)

    @mcp.tool()
    def list_do_not_text() -> str:
        """List authoritative DNT entries and their trigger evidence."""
        return toolbox.list_do_not_text()

    return mcp
