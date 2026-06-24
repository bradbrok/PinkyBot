"""Tests for the Slack purchase-approval button flow (#249 phase 1).

Covers the three security-relevant layers:
  - BrokerSlackPoller._handle_interactive — the daemon-side approver GATE (uses
    the VERIFIED Slack clicker id; fail-closed) and the route-to-agent dispatch.
  - AgentRegistry purchase-approver allowlist (fail-closed).
  - SlackAdapter blocks send + response_url update.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.pollers import (
    _PURCHASE_APPROVE_ACTION_ID,
    _PURCHASE_REJECT_ACTION_ID,
    BrokerSlackPoller,
    _strip_emoji,
)
from pinky_daemon.purchase_approval import make_approval_token

BRAD = "U7W8RJGP5"
CHAN = "CMYGW146S"
PID = "abc123def456"
SECRET = "test-secret"


def _make_poller(
    *, approver_ok=True, inject_ok=True, registry_raises=False, registry=None,
    approval_secret=SECRET,
):
    adapter = MagicMock()
    adapter.bot_token = "xoxb-test"
    adapter.respond_via_url = MagicMock()
    broker = MagicMock()
    broker.inject_agent_message = AsyncMock(return_value=inject_ok)

    if registry is None and registry is not False:
        registry = MagicMock()
        if registry_raises:
            registry.is_purchase_approver.side_effect = RuntimeError("db down")
        else:
            registry.is_purchase_approver.return_value = approver_ok
        registry.get_purchase_approval_secret.return_value = approval_secret

    poller = BrokerSlackPoller(
        adapter, "chekov", broker,
        registry=(None if registry is False else registry),
        app_token="xapp-test",
    )
    poller._bot_user_id = "UBOT"
    poller._bot_id = "B_SELF"
    return poller


def _interactive(action_id, *, value=PID, user_id=BRAD, username="brad", channel=CHAN):
    return {
        "type": "block_actions",
        "user": {"id": user_id, "username": username},
        "channel": {"id": channel},
        "actions": [{"action_id": action_id, "value": value}],
        "response_url": "https://hooks.slack.com/actions/xxx",
        "message": {"ts": "1.1"},
    }


class TestApproverGate:
    @pytest.mark.asyncio
    async def test_authorized_approve_routes_to_agent_and_updates_card(self):
        poller = _make_poller(approver_ok=True, inject_ok=True)
        await poller._handle_interactive(_interactive(_PURCHASE_APPROVE_ACTION_ID))

        poller._broker.inject_agent_message.assert_awaited_once()
        frm, to, msg = poller._broker.inject_agent_message.call_args[0]
        assert frm == "slack-approval" and to == "chekov"
        assert "approve_purchase" in msg
        assert PID in msg
        assert BRAD in msg  # verified approver id passed straight through

        # Card replaced (buttons gone), not an ephemeral note.
        poller._adapter.respond_via_url.assert_called_once()
        _url, payload = poller._adapter.respond_via_url.call_args[0]
        assert payload.get("replace_original") is True
        assert "Approved by" in payload.get("text", "")

    @pytest.mark.asyncio
    async def test_authorized_reject_calls_reject_purchase(self):
        poller = _make_poller(approver_ok=True, inject_ok=True)
        await poller._handle_interactive(_interactive(_PURCHASE_REJECT_ACTION_ID))
        _f, _t, msg = poller._broker.inject_agent_message.call_args[0]
        assert "reject_purchase" in msg
        _url, payload = poller._adapter.respond_via_url.call_args[0]
        assert "Rejected by" in payload.get("text", "")

    @pytest.mark.asyncio
    async def test_unauthorized_clicker_is_refused_not_routed(self):
        poller = _make_poller(approver_ok=False)
        await poller._handle_interactive(
            _interactive(_PURCHASE_APPROVE_ACTION_ID, user_id="UMALLORY", username="mallory")
        )
        # NEVER reaches the agent.
        poller._broker.inject_agent_message.assert_not_awaited()
        # Clicker gets an ephemeral refusal; the card is untouched.
        _url, payload = poller._adapter.respond_via_url.call_args[0]
        assert payload.get("response_type") == "ephemeral"
        assert payload.get("replace_original") is False
        assert "not authorized" in payload.get("text", "").lower()

    @pytest.mark.asyncio
    async def test_gate_uses_verified_clicker_id(self):
        """The id passed to the allowlist check is the Slack payload user.id."""
        poller = _make_poller(approver_ok=True)
        await poller._handle_interactive(
            _interactive(_PURCHASE_APPROVE_ACTION_ID, user_id="U_VERIFIED")
        )
        poller._registry.is_purchase_approver.assert_called_once_with("U_VERIFIED")

    @pytest.mark.asyncio
    async def test_registry_none_fails_closed(self):
        poller = _make_poller(registry=False)  # no registry wired
        await poller._handle_interactive(_interactive(_PURCHASE_APPROVE_ACTION_ID))
        poller._broker.inject_agent_message.assert_not_awaited()
        _url, payload = poller._adapter.respond_via_url.call_args[0]
        assert payload.get("response_type") == "ephemeral"

    @pytest.mark.asyncio
    async def test_registry_error_fails_closed(self):
        poller = _make_poller(registry_raises=True)
        await poller._handle_interactive(_interactive(_PURCHASE_APPROVE_ACTION_ID))
        poller._broker.inject_agent_message.assert_not_awaited()
        _url, payload = poller._adapter.respond_via_url.call_args[0]
        assert payload.get("response_type") == "ephemeral"

    @pytest.mark.asyncio
    async def test_agent_offline_keeps_buttons(self):
        poller = _make_poller(approver_ok=True, inject_ok=False)  # inject returns False
        await poller._handle_interactive(_interactive(_PURCHASE_APPROVE_ACTION_ID))
        # Authorized + attempted, but couldn't deliver -> ephemeral retry, card kept.
        _url, payload = poller._adapter.respond_via_url.call_args[0]
        assert payload.get("response_type") == "ephemeral"
        assert "again" in payload.get("text", "").lower()

    @pytest.mark.asyncio
    async def test_non_purchase_action_ignored(self):
        poller = _make_poller(approver_ok=True)
        await poller._handle_interactive(_interactive("some_other_button"))
        poller._broker.inject_agent_message.assert_not_awaited()
        poller._adapter.respond_via_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_pending_id_dropped(self):
        poller = _make_poller(approver_ok=True)
        await poller._handle_interactive(_interactive(_PURCHASE_APPROVE_ACTION_ID, value=""))
        poller._broker.inject_agent_message.assert_not_awaited()
        poller._adapter.respond_via_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_block_actions_ignored(self):
        poller = _make_poller(approver_ok=True)
        await poller._handle_interactive({"type": "view_submission"})
        poller._broker.inject_agent_message.assert_not_awaited()
        poller._adapter.respond_via_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_pending_id_dropped(self):
        """An injection-shaped value (quotes/spaces) is rejected at the boundary."""
        poller = _make_poller(approver_ok=True)
        await poller._handle_interactive(
            _interactive(_PURCHASE_APPROVE_ACTION_ID, value="abc'); drop()")
        )
        poller._broker.inject_agent_message.assert_not_awaited()
        poller._adapter.respond_via_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_clicker_id_dropped(self):
        poller = _make_poller(approver_ok=True)
        await poller._handle_interactive(
            _interactive(_PURCHASE_APPROVE_ACTION_ID, user_id="U7 OR 1=1")
        )
        poller._broker.inject_agent_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_emoji_in_approver_name_stripped_from_card(self):
        poller = _make_poller(approver_ok=True, inject_ok=True)
        await poller._handle_interactive(
            _interactive(_PURCHASE_APPROVE_ACTION_ID, username="brad \U0001f44d")
        )
        _url, payload = poller._adapter.respond_via_url.call_args[0]
        rendered = payload.get("text", "") + str(payload.get("blocks", ""))
        assert "\U0001f44d" not in rendered
        assert "brad" in rendered


class TestApprovalToken:
    @pytest.mark.asyncio
    async def test_instruction_carries_verifiable_token(self):
        poller = _make_poller(approver_ok=True, inject_ok=True, approval_secret=SECRET)
        await poller._handle_interactive(_interactive(_PURCHASE_APPROVE_ACTION_ID))
        _f, _t, msg = poller._broker.inject_agent_message.call_args[0]
        token = re.search(r"approval_token='([0-9a-f]+)'", msg).group(1)
        expires = int(re.search(r"token_expires=(\d+)", msg).group(1))
        # The minted token must verify for exactly (approve, PID, clicker, expires).
        expected = make_approval_token(
            SECRET, decision="approve", pending_id=PID, approver=BRAD, expires=expires
        )
        assert token == expected

    @pytest.mark.asyncio
    async def test_reject_token_decision_bound(self):
        poller = _make_poller(approver_ok=True, inject_ok=True, approval_secret=SECRET)
        await poller._handle_interactive(_interactive(_PURCHASE_REJECT_ACTION_ID))
        _f, _t, msg = poller._broker.inject_agent_message.call_args[0]
        token = re.search(r"approval_token='([0-9a-f]+)'", msg).group(1)
        expires = int(re.search(r"token_expires=(\d+)", msg).group(1))
        assert token == make_approval_token(
            SECRET, decision="reject", pending_id=PID, approver=BRAD, expires=expires
        )

    @pytest.mark.asyncio
    async def test_no_secret_fails_closed(self):
        poller = _make_poller(approver_ok=True, approval_secret="")
        await poller._handle_interactive(_interactive(_PURCHASE_APPROVE_ACTION_ID))
        # Authorized clicker, but no secret to mint a token -> refuse, don't route.
        poller._broker.inject_agent_message.assert_not_awaited()
        _url, payload = poller._adapter.respond_via_url.call_args[0]
        assert payload.get("response_type") == "ephemeral"
        assert "not configured" in payload.get("text", "").lower() or "secret" in payload.get("text", "").lower()

    def test_daemon_golden_vector(self):
        # Locked cross-repo contract — must equal pos-spec-purchasing's verifier.
        assert make_approval_token(
            "testsecret", decision="approve", pending_id="abc123",
            approver="U7W8RJGP5", expires=1700000000,
        ) == "8226a7515254d1640bc320eb2f9a57ca45cddcc8b920eaffa7044b82dbee0f3e"


class TestStripEmoji:
    def test_strips_emoji(self):
        assert _strip_emoji("brad \U0001f44d") == "brad"
        assert _strip_emoji("\U0001f7e2 ok \U0001f534") == "ok"

    def test_preserves_cyrillic_and_accents(self):
        assert _strip_emoji("Юлия") == "Юлия"
        assert _strip_emoji("José") == "José"

    def test_plain_ascii_unchanged(self):
        assert _strip_emoji("Brad Brockman") == "Brad Brockman"


class TestRegistryPurchaseApprovers:
    @pytest.fixture
    def registry(self, tmp_path):
        from pinky_daemon.agent_registry import AgentRegistry
        return AgentRegistry(db_path=str(tmp_path / "agents.db"))

    def test_empty_allowlist_denies_all(self, registry):
        # Fail-closed: no one approves until configured.
        assert registry.get_purchase_approvers() == []
        assert registry.is_purchase_approver(BRAD) is False

    def test_set_and_check(self, registry):
        registry.set_purchase_approvers([BRAD, "U_KEV"])
        assert registry.get_purchase_approvers() == [BRAD, "U_KEV"]
        assert registry.is_purchase_approver(BRAD) is True
        assert registry.is_purchase_approver("U_KEV") is True
        assert registry.is_purchase_approver("U_MALLORY") is False

    def test_blank_id_denied(self, registry):
        registry.set_purchase_approvers([BRAD])
        assert registry.is_purchase_approver("") is False

    def test_set_strips_blanks(self, registry):
        registry.set_purchase_approvers([BRAD, "", "  "])
        assert registry.get_purchase_approvers() == [BRAD]

    def test_comma_separated_legacy_value(self, registry):
        registry.set_setting("purchase_approver_slack_ids", "U1, U2 , U3")
        assert registry.get_purchase_approvers() == ["U1", "U2", "U3"]


class TestSlackAdapterBlocks:
    def _adapter(self):
        from pinky_outreach.slack import SlackAdapter
        return SlackAdapter("xoxb-fake")

    def test_send_message_forwards_blocks(self):
        adapter = self._adapter()
        resp = MagicMock()
        resp.json.return_value = {"ok": True, "message": {"ts": "1.1"}}
        adapter._client.post = MagicMock(return_value=resp)

        blocks = [{"type": "actions", "elements": []}]
        adapter.send_message("C1", "fallback", blocks=blocks)
        sent = adapter._client.post.call_args.kwargs["json"]
        assert sent["blocks"] == blocks
        assert sent["text"] == "fallback"
        adapter.close()

    def test_send_message_omits_blocks_when_none(self):
        adapter = self._adapter()
        resp = MagicMock()
        resp.json.return_value = {"ok": True, "message": {"ts": "1.1"}}
        adapter._client.post = MagicMock(return_value=resp)
        adapter.send_message("C1", "plain")
        sent = adapter._client.post.call_args.kwargs["json"]
        assert "blocks" not in sent  # _request drops None
        adapter.close()

    def test_respond_via_url_posts_payload(self, monkeypatch):
        from pinky_outreach import slack as slack_mod
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            r = MagicMock()
            r.status_code = 200
            return r

        monkeypatch.setattr(slack_mod.httpx, "post", fake_post)
        adapter = self._adapter()
        adapter.respond_via_url("https://hooks.slack/x", {"replace_original": True, "text": "ok"})
        assert captured["url"] == "https://hooks.slack/x"
        assert captured["json"]["text"] == "ok"
        adapter.close()

    def test_respond_via_url_noop_on_empty(self, monkeypatch):
        from pinky_outreach import slack as slack_mod
        called = {"n": 0}
        monkeypatch.setattr(slack_mod.httpx, "post", lambda *a, **k: called.__setitem__("n", 1))
        adapter = self._adapter()
        adapter.respond_via_url("", {"text": "x"})
        assert called["n"] == 0
        adapter.close()
