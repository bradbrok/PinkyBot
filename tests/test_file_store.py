"""Tests for the file-based memory store."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pinky_memory.file_store import FileMemoryStore, Memory, _parse_frontmatter, _slugify


class TestSlugify:
    def test_basic(self):
        assert _slugify("Hello World") == "hello_world"

    def test_special_chars(self):
        assert _slugify("Brad's Preferences!") == "brad_s_preferences"

    def test_multiple_spaces(self):
        assert _slugify("too   many   spaces") == "too_many_spaces"

    def test_leading_trailing(self):
        assert _slugify("  leading and trailing  ") == "leading_and_trailing"


class TestParseFrontmatter:
    def test_basic(self):
        text = "---\nname: Test\ntype: fact\n---\n\nContent here."
        fields, body = _parse_frontmatter(text)
        assert fields["name"] == "Test"
        assert fields["type"] == "fact"
        assert body == "Content here."

    def test_no_frontmatter(self):
        text = "Just plain content."
        fields, body = _parse_frontmatter(text)
        assert fields == {}
        assert body == "Just plain content."

    def test_description_with_special_chars(self):
        text = "---\nname: Test\ndescription: Brad's API keys -- don't share\ntype: reference\n---\n\nBody."
        fields, body = _parse_frontmatter(text)
        assert fields["description"] == "Brad's API keys -- don't share"
        assert fields["type"] == "reference"


class TestFileMemoryStore:
    @pytest.fixture
    def store(self, tmp_path):
        return FileMemoryStore(str(tmp_path / "memory"))

    @pytest.fixture
    def memory_dir(self, store):
        return Path(store._dir)

    def test_init_creates_directory(self, store, memory_dir):
        assert memory_dir.exists()
        assert (memory_dir / "MEMORY.md").exists()

    def test_write_and_read(self, store):
        mem = store.write_memory(
            name="Test Memory",
            description="A test memory",
            type="fact",
            content="This is test content.",
        )
        assert mem.filename == "fact_test_memory.md"
        assert mem.name == "Test Memory"
        assert mem.type == "fact"

        # Read it back
        result = store.read_memory(mem.filename)
        assert result is not None
        assert result.name == "Test Memory"
        assert result.description == "A test memory"
        assert result.content == "This is test content."

    def test_write_updates_index(self, store, memory_dir):
        store.write_memory("My Memory", "desc", "user", "content")
        index = (memory_dir / "MEMORY.md").read_text()
        assert "My Memory" in index
        assert "user_my_memory.md" in index

    def test_write_custom_filename(self, store):
        mem = store.write_memory(
            name="Custom",
            description="Custom file",
            type="project",
            content="Content",
            filename="my_custom_file.md",
        )
        assert mem.filename == "my_custom_file.md"

        result = store.read_memory("my_custom_file.md")
        assert result is not None
        assert result.name == "Custom"

    def test_read_nonexistent(self, store):
        result = store.read_memory("does_not_exist.md")
        assert result is None

    def test_list_memories(self, store):
        store.write_memory("First", "desc1", "user", "content1")
        store.write_memory("Second", "desc2", "project", "content2")
        store.write_memory("Third", "desc3", "feedback", "content3")

        memories = store.list_memories()
        assert len(memories) == 3
        names = {m.name for m in memories}
        assert names == {"First", "Second", "Third"}

    def test_list_empty(self, store):
        memories = store.list_memories()
        assert memories == []

    def test_update_memory(self, store):
        store.write_memory("Original", "orig desc", "user", "orig content")

        updated = store.update_memory(
            "user_original.md",
            content="updated content",
        )
        assert updated is not None
        assert updated.name == "Original"  # unchanged
        assert updated.content == "updated content"

        # Verify on disk
        result = store.read_memory("user_original.md")
        assert result.content == "updated content"

    def test_update_nonexistent(self, store):
        result = store.update_memory("nope.md", content="new")
        assert result is None

    def test_delete_memory(self, store, memory_dir):
        store.write_memory("ToDelete", "desc", "fact", "content")
        assert (memory_dir / "fact_todelete.md").exists()

        deleted = store.delete_memory("fact_todelete.md")
        assert deleted is True
        assert not (memory_dir / "fact_todelete.md").exists()

        # Index should be updated
        index = (memory_dir / "MEMORY.md").read_text()
        assert "todelete" not in index

    def test_delete_nonexistent(self, store):
        assert store.delete_memory("nope.md") is False

    def test_search_by_content(self, store):
        store.write_memory("Brad Info", "about brad", "user", "Brad likes eurorack synths")
        store.write_memory("Yulia Info", "about yulia", "user", "Yulia does branding")
        store.write_memory("Project X", "active project", "project", "Building a website")

        results = store.search("brad")
        assert len(results) == 1
        assert results[0].name == "Brad Info"

    def test_search_by_description(self, store):
        store.write_memory("Info", "synth modules and eurorack", "reference", "content")

        results = store.search("eurorack")
        assert len(results) == 1

    def test_search_case_insensitive(self, store):
        store.write_memory("Test", "desc", "fact", "UPPERCASE content")

        results = store.search("uppercase")
        assert len(results) == 1

    def test_search_with_type_filter(self, store):
        store.write_memory("A", "desc", "user", "matching content")
        store.write_memory("B", "desc", "project", "matching content")

        results = store.search("matching", type_filter="user")
        assert len(results) == 1
        assert results[0].name == "A"

    def test_search_no_results(self, store):
        store.write_memory("Test", "desc", "fact", "content")
        results = store.search("nonexistent query")
        assert results == []

    def test_read_index(self, store):
        store.write_memory("First", "desc1", "user", "content1")
        store.write_memory("Second", "desc2", "project", "content2")

        index = store.read_index()
        assert "# Memory Index" in index
        assert "First" in index
        assert "Second" in index

    def test_index_updates_on_rewrite(self, store, memory_dir):
        store.write_memory("Test", "original desc", "user", "content", filename="test.md")
        store.write_memory("Test Updated", "new desc", "user", "new content", filename="test.md")

        index = (memory_dir / "MEMORY.md").read_text()
        # Should have only one entry for test.md
        assert index.count("test.md") == 1
        assert "new desc" in index

    def test_multiple_types(self, store):
        store.write_memory("U", "user info", "user", "content")
        store.write_memory("F", "feedback", "feedback", "content")
        store.write_memory("P", "project", "project", "content")
        store.write_memory("R", "reference", "reference", "content")

        memories = store.list_memories()
        types = {m.type for m in memories}
        assert types == {"user", "feedback", "project", "reference"}
