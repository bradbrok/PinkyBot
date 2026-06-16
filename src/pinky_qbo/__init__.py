"""pinky-qbo — read-only QuickBooks Online MCP server (per-agent stdio).

Aggregates-only v1: financial reports + company_info + chart of accounts for a
single agent on the TOD fleet (onesie). No writes, no transaction-level PII.

Read-only is an app-layer invariant — see specs/qbo-mcp-onesie.md §6.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
