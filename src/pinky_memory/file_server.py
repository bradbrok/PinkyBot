"""File-based Memory MCP Server — Pulse node style.

This is the default memory server for Pinky. Memories are markdown files
with YAML frontmatter, indexed by MEMORY.md. Human-readable, git-trackable,
zero infrastructure.

For advanced use cases (semantic search, vector embeddings), see the
SQLite-based server in server.py.
"""

from __future__ import annotations

import json
import sys

from mcp.server.fastmcp import FastMCP

from pinky_memory.file_store import FileMemoryStore


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def create_file_server(
    memory_dir: str = "memory",
    *,
    host: str = "127.0.0.1",
    port: int = 8100,
) -> FastMCP:
    mcp = FastMCP("pinky-memory", host=host, port=port)
    store = FileMemoryStore(memory_dir)

    @mcp.tool()
    def memory_save(
        name: str,
        description: str,
        content: str,
        type: str = "fact",
        filename: str = "",
    ) -> str:
        """Save a new memory to a markdown file.

        Memories are stored as individual .md files with frontmatter,
        and indexed in MEMORY.md. Use this to persist important information
        across conversations.

        Types:
        - user: Info about the user (role, preferences, knowledge)
        - feedback: Guidance on how to approach work (corrections + confirmations)
        - project: Ongoing work, goals, decisions, deadlines
        - reference: Pointers to external resources and systems

        Args:
            name: Short title for the memory.
            description: One-line description (used for relevance matching).
            content: The memory content (markdown).
            type: Memory type: user, feedback, project, reference.
            filename: Optional filename override (auto-generated if empty).
        """
        if type not in ("user", "feedback", "project", "reference"):
            return json.dumps({"error": f"Invalid type '{type}'. Must be: user, feedback, project, reference"})

        memory = store.write_memory(name, description, type, content, filename)
        _log(f"memory_save: {memory.filename} type={type}")

        return json.dumps({
            "filename": memory.filename,
            "name": memory.name,
            "type": memory.type,
            "saved": True,
        })

    @mcp.tool()
    def memory_read(filename: str) -> str:
        """Read a specific memory file by filename.

        Args:
            filename: The memory filename (e.g. "user_preferences.md").
        """
        memory = store.read_memory(filename)
        if not memory:
            return json.dumps({"error": f"Memory '{filename}' not found"})

        _log(f"memory_read: {filename}")

        return json.dumps({
            "filename": memory.filename,
            "name": memory.name,
            "description": memory.description,
            "type": memory.type,
            "content": memory.content,
        })

    @mcp.tool()
    def memory_update(
        filename: str,
        name: str = "",
        description: str = "",
        type: str = "",
        content: str = "",
    ) -> str:
        """Update an existing memory file.

        Only provided fields are updated; others keep their current values.

        Args:
            filename: The memory filename to update.
            name: New title (empty = keep current).
            description: New description (empty = keep current).
            type: New type (empty = keep current).
            content: New content (empty = keep current).
        """
        memory = store.update_memory(
            filename,
            name=name or None,
            description=description or None,
            type=type or None,
            content=content if content else None,
        )
        if not memory:
            return json.dumps({"error": f"Memory '{filename}' not found"})

        _log(f"memory_update: {filename}")

        return json.dumps({
            "filename": memory.filename,
            "name": memory.name,
            "type": memory.type,
            "updated": True,
        })

    @mcp.tool()
    def memory_delete(filename: str) -> str:
        """Delete a memory file and remove it from the index.

        Args:
            filename: The memory filename to delete.
        """
        deleted = store.delete_memory(filename)
        if not deleted:
            return json.dumps({"error": f"Memory '{filename}' not found"})

        _log(f"memory_delete: {filename}")
        return json.dumps({"filename": filename, "deleted": True})

    @mcp.tool()
    def memory_list(type: str = "") -> str:
        """List all stored memories.

        Returns filename, name, description, and type for each memory.

        Args:
            type: Filter by type (user, feedback, project, reference). Empty = all.
        """
        memories = store.list_memories()
        if type:
            memories = [m for m in memories if m.type == type]

        _log(f"memory_list: {len(memories)} memories")

        return json.dumps({
            "count": len(memories),
            "memories": [
                {
                    "filename": m.filename,
                    "name": m.name,
                    "description": m.description,
                    "type": m.type,
                }
                for m in memories
            ],
        })

    @mcp.tool()
    def memory_search(query: str, type: str = "") -> str:
        """Search across all memory files by keyword.

        Searches name, description, and content of each memory file.

        Args:
            query: Search query (case-insensitive substring match).
            type: Filter by type (empty = search all).
        """
        results = store.search(query, type_filter=type)

        _log(f"memory_search: {len(results)} results for '{query}'")

        return json.dumps({
            "query": query,
            "count": len(results),
            "memories": [
                {
                    "filename": m.filename,
                    "name": m.name,
                    "description": m.description,
                    "type": m.type,
                    "content": m.content[:500],  # Preview
                }
                for m in results
            ],
        })

    @mcp.tool()
    def memory_index() -> str:
        """Read the MEMORY.md index file.

        Returns the full contents of the memory index, which provides
        a quick overview of all stored memories.
        """
        index = store.read_index()
        _log("memory_index: read")
        return index

    return mcp
