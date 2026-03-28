"""File-based memory store — Pulse node style.

Memories are individual markdown files with YAML frontmatter, indexed by MEMORY.md.
This is the default memory system for Pinky. Simple, human-readable, git-trackable.

Directory structure:
    memory/
    ├── MEMORY.md              # Index file — one-line entries with links
    ├── user_preferences.md    # Individual memory files
    ├── project_website.md
    ├── feedback_testing.md
    └── reference_api_docs.md

Each memory file has frontmatter:
    ---
    name: Short title
    description: One-line description for relevance matching
    type: user | feedback | project | reference
    ---
    Memory content here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Memory:
    """A single memory entry."""
    filename: str
    name: str
    description: str
    type: str  # user, feedback, project, reference
    content: str

    @property
    def path(self) -> str:
        return self.filename


_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)

_FIELD_RE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-like frontmatter from a markdown file."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    frontmatter_text = match.group(1)
    body = match.group(2).strip()

    fields = {}
    for field_match in _FIELD_RE.finditer(frontmatter_text):
        key = field_match.group(1).strip()
        value = field_match.group(2).strip()
        fields[key] = value

    return fields, body


def _build_file_content(name: str, description: str, type: str, content: str) -> str:
    """Build a memory file with frontmatter."""
    return f"""---
name: {name}
description: {description}
type: {type}
---

{content}
"""


def _slugify(name: str) -> str:
    """Convert a name to a filename-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug


class FileMemoryStore:
    """File-based memory store matching Pulse node conventions."""

    def __init__(self, memory_dir: str = "memory") -> None:
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "MEMORY.md"
        if not self._index_path.exists():
            self._index_path.write_text("# Memory Index\n")

    def list_memories(self) -> list[Memory]:
        """List all memory files (by reading the directory, not just the index)."""
        memories = []
        for path in sorted(self._dir.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            text = path.read_text(encoding="utf-8")
            fields, body = _parse_frontmatter(text)
            memories.append(Memory(
                filename=path.name,
                name=fields.get("name", path.stem),
                description=fields.get("description", ""),
                type=fields.get("type", "fact"),
                content=body,
            ))
        return memories

    def read_memory(self, filename: str) -> Memory | None:
        """Read a specific memory file."""
        path = self._dir / filename
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        fields, body = _parse_frontmatter(text)
        return Memory(
            filename=filename,
            name=fields.get("name", path.stem),
            description=fields.get("description", ""),
            type=fields.get("type", "fact"),
            content=body,
        )

    def write_memory(
        self,
        name: str,
        description: str,
        type: str,
        content: str,
        filename: str = "",
    ) -> Memory:
        """Write a memory file and update the index."""
        if not filename:
            filename = f"{type}_{_slugify(name)}.md"

        path = self._dir / filename
        file_content = _build_file_content(name, description, type, content)
        path.write_text(file_content, encoding="utf-8")

        # Update index
        self._update_index(filename, name, description)

        return Memory(
            filename=filename,
            name=name,
            description=description,
            type=type,
            content=content,
        )

    def update_memory(
        self,
        filename: str,
        name: str | None = None,
        description: str | None = None,
        type: str | None = None,
        content: str | None = None,
    ) -> Memory | None:
        """Update an existing memory file."""
        existing = self.read_memory(filename)
        if not existing:
            return None

        name = name or existing.name
        description = description or existing.description
        type = type or existing.type
        content = content if content is not None else existing.content

        path = self._dir / filename
        file_content = _build_file_content(name, description, type, content)
        path.write_text(file_content, encoding="utf-8")

        self._update_index(filename, name, description)

        return Memory(
            filename=filename,
            name=name,
            description=description,
            type=type,
            content=content,
        )

    def delete_memory(self, filename: str) -> bool:
        """Delete a memory file and remove from index."""
        path = self._dir / filename
        if not path.exists():
            return False
        path.unlink()
        self._remove_from_index(filename)
        return True

    def search(self, query: str, type_filter: str = "") -> list[Memory]:
        """Simple text search across memory files."""
        query_lower = query.lower()
        results = []
        for memory in self.list_memories():
            if type_filter and memory.type != type_filter:
                continue
            searchable = f"{memory.name} {memory.description} {memory.content}".lower()
            if query_lower in searchable:
                results.append(memory)
        return results

    def read_index(self) -> str:
        """Read the MEMORY.md index file."""
        return self._index_path.read_text(encoding="utf-8")

    def _update_index(self, filename: str, name: str, description: str) -> None:
        """Add or update an entry in MEMORY.md."""
        index_text = self._index_path.read_text(encoding="utf-8")
        entry_line = f"- [{name}]({filename}) — {description}"

        # Check if this file is already in the index
        pattern = re.compile(rf"^- \[.*?\]\({re.escape(filename)}\).*$", re.MULTILINE)
        if pattern.search(index_text):
            # Update existing entry
            index_text = pattern.sub(entry_line, index_text)
        else:
            # Append new entry
            index_text = index_text.rstrip() + "\n" + entry_line + "\n"

        self._index_path.write_text(index_text, encoding="utf-8")

    def _remove_from_index(self, filename: str) -> None:
        """Remove an entry from MEMORY.md."""
        index_text = self._index_path.read_text(encoding="utf-8")
        pattern = re.compile(rf"^- \[.*?\]\({re.escape(filename)}\).*\n?", re.MULTILINE)
        index_text = pattern.sub("", index_text)
        self._index_path.write_text(index_text, encoding="utf-8")
