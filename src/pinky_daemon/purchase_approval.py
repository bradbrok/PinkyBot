"""Daemon-side minting of purchase-approval tokens (#249, money gate).

The PinkyBot daemon is the authoritative approver gate: after the Slack
interactivity handler verifies a clicker's signed identity against the approver
allowlist, it mints a short-lived HMAC token here and instructs the agent to
relay it to the pos-spec-purchasing MCP, which verifies it before flipping an
order's state. An LLM cannot forge the token (no shared secret), so no prompt
path can drive the agent into approving an order that was never clicked by a
verified, allowlisted approver.

CROSS-REPO CONTRACT — the canonical signing string and HMAC MUST stay
byte-for-byte identical to the verifier in
pos-spec-purchasing/src/pos_spec_purchasing/approval.py. The golden vector
  secret="testsecret", decision="approve", pending_id="abc123",
  requester="chekov", approver="U7W8RJGP5", expires=1700000000
  -> 17a128f65d20f93ef3080876906cb3f03968e55fefd1710bf734bae0dbf3f2dc
is locked by tests in BOTH repos; changing the scheme breaks the gate.
"""

from __future__ import annotations

import hashlib
import hmac

# Decisions a token may authorize (must match the MCP verifier).
APPROVE = "approve"
REJECT = "reject"

# How long a minted token stays valid — generous enough for the agent to act on
# the click, short enough to bound replay.
TOKEN_TTL_SECONDS = 900


def make_approval_token(
    secret: str,
    *,
    decision: str,
    pending_id: str,
    requester: str,
    approver: str,
    expires: int,
) -> str:
    """Return HMAC-SHA256 over the requester-bound decision tuple."""
    msg = "\n".join([decision, pending_id, requester, approver, str(int(expires))])
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
