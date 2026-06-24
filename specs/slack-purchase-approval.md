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
      `channel`, `response_url`. `pending_id` and `clicker_id` are validated as
      clean opaque tokens (fail-closed drop on malformed) so nothing
      injection-shaped can reach the agent instruction; the approver display
      name is emoji-stripped before it lands in the shipped card.
   b. **GATE** — `registry.is_purchase_approver(clicker_id)`. Fail-closed: a
      blank id, an unconfigured allowlist, or any error → **deny**. Unauthorized
      clicks get an ephemeral refusal and never reach the agent.
   c. Authorized → **mint a daemon-signed approval token**
      (`HMAC-SHA256(secret, decision \n pending_id \n clicker_id \n expires)`,
      ~15-min TTL) and inject a synthetic instruction to the owning agent telling
      it to call `approve_purchase`/`reject_purchase` for that exact `pending_id`
      with the verified `clicker_id` **and the token + expiry**. Update the card
      via `response_url` ("Approved/Rejected by X", buttons removed). If no secret
      is configured, or the agent is unreachable, fail-closed (ephemeral note;
      card keeps its buttons).
5. The agent calls the MCP tool, relaying the token. The MCP verifies it before
   any state change, then the agent posts the cart link / confirmation.

## Enforcement layers (defense in depth)

1. **Daemon-signed token (authoritative).** The state transition (pending →
   approved | rejected) requires an HMAC token only the daemon can mint, and only
   after the verified-clicker gate (below) passes. The MCP verifies the token
   (shared secret, exact `decision`/`pending_id`/`approver`/`expiry` tuple, not
   expired) before touching the store. **An LLM cannot forge it**, so no prompt
   path — including the legacy "type `approve`" text path — can drive the agent
   into approving an order that was never clicked by a verified, allowlisted
   approver. Fail-closed: missing secret / missing / invalid / expired token →
   refused.
2. **Daemon verified-clicker gate.** `registry.is_purchase_approver(clicker_id)`
   on the Slack-signed `payload['user']['id']`. Fail-closed (blank id /
   unconfigured allowlist / error → deny). This decides *whether* the daemon
   mints a token at all.
3. **MCP approver allowlist (secondary).** `POS_PURCHASING_APPROVERS`; even with a
   valid token, the approver must be on the list when one is configured.

The shared secret lives in two places that must match: the daemon's
`purchase_approval_secret` system setting (signer) and the MCP's
`POS_PURCHASING_APPROVAL_SECRET` env (verifier). Either side unset → no approvals
(fail-closed).

## Phase 2 (pay-on-approval)

`approve_purchase` returns only an Amazon cart link today — **no money moves**.
Phase 2 adds real payment via the Amazon Business API. The token already proves a
verified click; phase 2 extends it so the *payment* step is bound to the same
proof (and may add a daemon-recorded click ledger for non-repudiation). **Do not
enable real payment before that exists.**

## Deploy prerequisites (Pi / chekov)

1. **Slack app**: enable *Interactivity & Shortcuts* (Socket Mode is already on,
   so no Request URL and no reinstall needed). Without it, clicks deliver no
   envelope and nothing happens.
2. **Shared approval secret** (fail-closed, else every approval is refused):
   generate one secret and set it on BOTH sides —
   `registry.set_purchase_approval_secret("<secret>")` (daemon) **and**
   `POS_PURCHASING_APPROVAL_SECRET=<same secret>` in chekov's pos-spec-purchasing
   MCP env. They must be identical.
3. **Seed the approver allowlist** (fail-closed):
   `registry.set_purchase_approvers(["U7W8RJGP5"])` (Brad). Stored in
   `system_settings.purchase_approver_slack_ids`. Optionally also set
   `POS_PURCHASING_APPROVERS` on the MCP env (secondary check).
4. Restart is on the **Pi** (chekov's Slack poller). Heads-up to Brad first.
