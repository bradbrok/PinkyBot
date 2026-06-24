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
  approver="U7W8RJGP5", expires=1700000000
  -> 8226a7515254d1640bc320eb2f9a57ca45cddcc8b920eaffa7044b82dbee0f3e
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
    secret: str, *, decision: str, pending_id: str, approver: str, expires: int
) -> str:
    """Return the HMAC-SHA256 hex token over the canonical decision tuple."""
    msg = "\n".join([decision, pending_id, approver, str(int(expires))])
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
