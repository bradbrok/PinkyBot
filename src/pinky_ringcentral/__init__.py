"""RingCentral SMS bridge and agent MCP wrapper.

The Mini-side HTTP bridge is the only process that receives RingCentral
credentials. Agent-side MCP processes receive a per-agent derived HMAC key and
call the bridge over the private network.
"""

from pinky_ringcentral.client import RingCentralAPIError, RingCentralClient

__all__ = ["RingCentralAPIError", "RingCentralClient"]
