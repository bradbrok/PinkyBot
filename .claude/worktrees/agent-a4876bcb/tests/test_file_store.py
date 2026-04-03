"""Tests for the file-based memory store — REMOVED.

The file-based memory backend has been removed in favor of the hybrid memory
architecture. Claude Code's native memory (MEMORY.md, memory/*.md) serves as
Tier 1 working memory, while the SQLite backend provides Tier 2 long-term
semantic memory via the MCP server.

See docs/hybrid-memory-spec.md for details.
"""
