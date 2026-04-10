"""Tests for KB store — raw source CRUD operations."""

import pytest

from pinky_daemon.kb_store import KBStore, _parse_frontmatter


@pytest.fixture
def kb(tmp_path):
    """Create a KBStore in a temp directory."""
    return KBStore(str(tmp_path / "kb"))


class TestParsesFrontmatter:
    def test_basic(self):
        text = "---\ntitle: Hello\ntags: [a, b]\n---\n\n# Hello\n\nBody text here."
        fm, body = _parse_frontmatter(text)
        assert fm["title"] == "Hello"
        assert fm["tags"] == ["a", "b"]
        assert body == "Body text here."

    def test_no_frontmatter(self):
        text = "Just plain text."
        fm, body = _parse_frontmatter(text)
        assert fm == {}
        assert body == "Just plain text."

    def test_empty_body(self):
        text = "---\ntitle: Empty\n---\n\n# Empty\n\n"
        fm, body = _parse_frontmatter(text)
        assert fm["title"] == "Empty"
        assert body == ""


class TestIngest:
    def test_ingest_creates_file_and_db(self, kb):
        source = kb.ingest(title="Test Source", content="Hello world", tags=["test"])
        assert source.id.startswith("raw-")
        assert source.title == "Test Source"
        assert source.tags == ["test"]

        # File exists
        full_path = kb.kb_dir / source.file_path
        assert full_path.exists()
        content = full_path.read_text()
        assert "Hello world" in content
        assert "Test Source" in content

    def test_ingest_indexes_in_fts(self, kb):
        kb.ingest(title="Searchable", content="unique keyword xyzzy", tags=["findme"])
        results = kb.search("xyzzy")
        assert len(results) == 1
        assert results[0]["title"] == "Searchable"


class TestDeleteRaw:
    def test_delete_removes_everything(self, kb):
        source = kb.ingest(title="To Delete", content="Bye bye", tags=["temp"])
        assert kb.get_raw(source.id) is not None

        deleted = kb.delete_raw(source.id)
        assert deleted is True

        # DB row gone
        assert kb.get_raw(source.id) is None

        # File gone
        full_path = kb.kb_dir / source.file_path
        assert not full_path.exists()

        # FTS gone
        results = kb.search("Bye bye")
        assert len(results) == 0

    def test_delete_nonexistent_returns_false(self, kb):
        assert kb.delete_raw("raw-9999-01-01-999") is False

    def test_delete_then_count(self, kb):
        kb.ingest(title="Keep", content="a")
        s2 = kb.ingest(title="Delete", content="b")
        assert kb.count_raw() == 2

        kb.delete_raw(s2.id)
        assert kb.count_raw() == 1


class TestUpdateRaw:
    def test_update_title(self, kb):
        source = kb.ingest(title="Old Title", content="Body text", tags=["a"])
        updated = kb.update_raw(source.id, title="New Title")
        assert updated is not None
        assert updated.title == "New Title"

        # File reflects change
        content = kb.get_raw_content(source.id)
        assert "# New Title" in content

    def test_update_tags(self, kb):
        source = kb.ingest(title="Tagged", content="Body", tags=["old"])
        updated = kb.update_raw(source.id, tags=["new", "fresh"])
        assert updated.tags == ["new", "fresh"]

        # FTS updated — search for new tag
        results = kb.search("fresh")
        assert len(results) == 1

    def test_update_content(self, kb):
        source = kb.ingest(title="Content", content="Original body")
        updated = kb.update_raw(source.id, content="Updated body text")
        assert updated is not None

        content = kb.get_raw_content(source.id)
        assert "Updated body text" in content
        assert "Original body" not in content

        # FTS reflects new content
        assert len(kb.search("Updated body")) == 1
        assert len(kb.search("Original body")) == 0

    def test_update_nonexistent_returns_none(self, kb):
        assert kb.update_raw("raw-9999-01-01-999", title="Nope") is None

    def test_update_preserves_unmodified_fields(self, kb):
        source = kb.ingest(
            title="Stable", content="Body",
            tags=["keep"], source_type="article", source_url="https://example.com"
        )
        updated = kb.update_raw(source.id, title="Changed Title")
        assert updated.source_type == "article"
        assert updated.source_url == "https://example.com"
        assert updated.tags == ["keep"]


class TestCountRaw:
    def test_count_all(self, kb):
        assert kb.count_raw() == 0
        kb.ingest(title="A", content="a")
        kb.ingest(title="B", content="b")
        assert kb.count_raw() == 2

    def test_count_with_type_filter(self, kb):
        kb.ingest(title="Article", content="a", source_type="article")
        kb.ingest(title="Note", content="b", source_type="note")
        assert kb.count_raw(source_type="article") == 1
        assert kb.count_raw(source_type="note") == 1

    def test_count_with_tag_filter(self, kb):
        kb.ingest(title="A", content="a", tags=["python"])
        kb.ingest(title="B", content="b", tags=["rust"])
        assert kb.count_raw(tag="python") == 1
