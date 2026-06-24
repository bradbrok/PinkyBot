# Slack purchase-approval buttons (#249 phase 1)

Interactive Approve / Reject buttons for the pos-spec-purchasing approval card,
with deterministic approver-identity enforcement. Replaces (augments) the
"type `approve` in the channel" flow.

## Why this shape

The verified, unspoofable approver identity (`payload['user']['id']`, set and
signed by Slack) exists **only** at the daemon's Socket Mode handler. The moment
a click is handed to an LLM, that identity becomes ordinary text the model could
be injected into mis-stating. So the **authoritative money-gate lives in the
daemon**, before anything reaches the agent. The LLM is an executor, never the
security boundary.

The daemon has **no MCP client** — it cannot call `approve_purchase` directly
(arch map, #249). So after the gate passes, the daemon injects a synthetic
instruction to the owning agent (chekov), which calls the purchasing MCP. (B1.)

## Flow

1. `propose_purchase` (pos-spec-purchasing) returns `slack_blocks` — an
   interactive card with Approve / Reject buttons. Contract:
   - approve `action_id` = `pos_purchase_approve`
   - reject  `action_id` = `pos_purchase_reject`
   - button `value` = the `pending_id`
2. The agent posts it: `send(chat_id, platform="slack", text=human_summary,
   blocks=<slack_blocks JSON string>)`. `text` is the notification fallback; if
   `blocks` is dropped/malformed the message degrades to text-only.
3. A human clicks a button → Slack delivers a `block_actions` interactive
   envelope over the existing Socket Mode connection.
4. `BrokerSlackPoller._handle_interactive`:
   a. Parse verified `clicker_id`, `action_id`→decision, `value`→`pending_id`,
      `channel`, `response_url`.
   b. **GATE** — `registry.is_purchase_approver(clicker_id)`. Fail-closed: a
      blank id, an unconfigured allowlist, or any error → **deny**. Unauthorized
      clicks get an ephemeral refusal and never reach the agent.
   c. Authorized → inject a synthetic instruction to the owning agent carrying
      the verified `clicker_id` as the approver, telling it to call
      `approve_purchase`/`reject_purchase` for that exact `pending_id` and post
      the result. Update the card via `response_url` ("Approved/Rejected by X",
      buttons removed). If the agent is unreachable, the card keeps its buttons
      and the clicker gets an ephemeral retry note.
5. The agent calls the MCP tool and posts the cart link / confirmation.

## Enforcement layers (defense in depth)

1. **Daemon gate (authoritative)** — verified Slack id vs allowlist
   (`purchase_approver_slack_ids` system setting). Fail-closed.
2. **MCP allowlist (secondary)** — `POS_PURCHASING_APPROVERS`; `approve_purchase`
   rejects an approver not on the list. Only as trustworthy as the `approver`
   string the agent passes, so it is a guard, not the gate.

## Known phase-1 gaps (closed in phase 2)

- The legacy "type `approve`" text path is **not** gated by the verified-clicker
  check (the agent acts on channel text). In phase 1 `approve_purchase` only
  returns a cart link — **no money moves** — so the residual risk is a spurious
  cart link, not a spend.
- **Phase 2 (pay-on-approval) closes this:** the daemon will record the verified
  click (`pending_id` → verified `approver_id`) and the payment tool will refuse
  to place/pay an order without a matching daemon-recorded verified approval.
  That removes the LLM and the text path from the money path entirely. Do NOT
  enable real payment before that record + check exist.

## Deploy prerequisites (Pi / chekov)

1. **Slack app**: enable *Interactivity & Shortcuts* (Socket Mode is already on,
   so no Request URL and no reinstall needed). Without it, clicks deliver no
   envelope and nothing happens.
2. **Seed the allowlist** (fail-closed, else every click is refused):
   `registry.set_purchase_approvers(["U7W8RJGP5"])` (Brad). Stored in
   `system_settings.purchase_approver_slack_ids`.
3. **MCP allowlist (optional, recommended):** set `POS_PURCHASING_APPROVERS`
   on chekov's pos-spec-purchasing MCP env.
4. Restart is on the **Pi** (chekov's Slack poller). Heads-up to Brad first.
