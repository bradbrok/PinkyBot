"""Exact frontmatter compatibility, caller coverage, and bounded parsing time."""

import ast
import itertools
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pinky_daemon import kb_store
from pinky_daemon.kb_store import KBStore, _content_hash, _content_preview, _parse_frontmatter
from pinky_daemon.librarian_runner import LibrarianRunner
from pinky_daemon.skill_loader import parse_skill_md
from pinky_daemon.wiki_builder import save_wiki_pages

REFERENCE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)

try:
    from pinky_daemon.frontmatter import split_frontmatter
except ModuleNotFoundError as exc:
    # Exercise the existing implementation when running these tests before the change.
    if exc.name != "pinky_daemon.frontmatter":
        raise

    def split_frontmatter(text):
        match = kb_store._FRONTMATTER_RE.match(text)
        return match.groups() if match else None


def reference(text):
    match = REFERENCE.match(text)
    return match.groups() if match else None


@pytest.mark.parametrize("alphabet,max_length", [("-\n a", 8), ("-\n\t\rax", 6)])
def test_exhaustive_reference_match(alphabet, max_length):
    for length in range(max_length + 1):
        for chars in itertools.product(alphabet, repeat=length):
            text = "".join(chars)
            assert split_frontmatter(text) == reference(text), repr(text)


def test_whitespace_and_multiple_fences_match_reference():
    whitespace = ["", " ", "\n", "\n\n", "\r\n\t\n", "\v\f\x1c\x1d\x1e\x1f\x85\u2028\u2029"]
    for opening, header, closing, body in itertools.product(
        whitespace, ["", "title: Example", "---", "---\n---", "\n---\ntext"],
        whitespace, ["", "body", "\nbody", " ---\nbody", "\n---\nbody"],
    ):
        text = f"---{opening}\n{header}\n---{closing}{body}"
        assert split_frontmatter(text) == reference(text), repr(text)


def test_closing_fence_consumes_all_trailing_whitespace():
    text = "---\ntitle: Example\n--- \t\r\n\n\vbody\n"
    assert split_frontmatter(text) == ("title: Example", "body\n")


def test_body_preserves_newlines_after_its_first_nonspace():
    text = "---\nmeta\n---\nbody\n\n"
    assert split_frontmatter(text) == ("meta", "body\n\n")


def test_opening_whitespace_uses_latest_viable_newline():
    assert split_frontmatter("---\n\nmeta\n---\nbody") == ("meta", "body")
    assert split_frontmatter("---\n\n---\n---\nbody") == ("---", "body")
    assert split_frontmatter("---\n\n---") == ("", "")
    assert split_frontmatter("---\n---") is None


def test_writer_and_repository_fixture_corpus(tmp_path):
    kb = KBStore(tmp_path / "data")
    raw = kb.ingest(
        title="Example: café", content="Raw body\n", tags=["one", "two"],
        source_url="https://example.invalid/source", owner_notes="line one\nline two",
    )
    wiki = kb.save_wiki(
        "topics/example", "Example", "# Example\n\nWiki body\n",
        sources=[raw.id], related=["topics/related"],
    )
    corpus = [
        (kb.kb_dir / raw.file_path).read_text(),
        (kb.kb_dir / wiki.file_path).read_text(),
        "---\nname: example\ndescription: Example skill\nallowed-tools: Read Grep\n---\n\nRead files.\n",
        "---\nname: example\ndescription: |\n  Example skill\nmetadata:\n  version: '1'\n"
        "allowed-tools:\n  - Read\n  - Grep\n---\n\nRead files.\n",
    ]
    repo = Path(__file__).resolve().parents[1]
    # Include checked-in markdown and literal frontmatter in existing test fixtures.
    for folder in (repo / "tests", repo / "src", repo / "skills"):
        for path in folder.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            if text.startswith("---"):
                corpus.append(text)
    fixture_count = 0
    for path in (repo / "tests").rglob("*.py"):
        if path == Path(__file__):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("---"):
                    corpus.append(node.value)
                    fixture_count += 1
    assert fixture_count > 0
    for text in corpus:
        assert split_frontmatter(text) == reference(text), repr(text)


LONG_SHAPES = {
    "newlines": "---\n" + "\n" * 200_000,
    "spaces_then_newlines": "---" + " " * 100_000 + "\n" * 100_000,
    "alternating_whitespace": "---\n" + " \n" * 100_000,
}


@pytest.mark.parametrize("shape", LONG_SHAPES)
def test_long_whitespace_finishes_within_one_second(shape, record_property):
    # The child is killed and reaped even if the old regex is still installed.
    script = r'''
import json, sys, time
from pinky_daemon.kb_store import _content_preview
shapes = {
    "newlines": "---\n" + "\n" * 200_000,
    "spaces_then_newlines": "---" + " " * 100_000 + "\n" * 100_000,
    "alternating_whitespace": "---\n" + " \n" * 100_000,
}
start = time.perf_counter()
result = _content_preview(shapes[sys.argv[1]])
print(json.dumps({"seconds": time.perf_counter() - start, "result": result}), flush=True)
'''
    started = time.perf_counter()
    try:
        child = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, shape],
            capture_output=True, text=True, timeout=3, check=True,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - started
        record_property("timing_lower_bound_seconds", 3)
        record_property("hard_timeout_wall_seconds", elapsed)
        pytest.fail("frontmatter parsing exceeded the 3-second child hard timeout")
    result = json.loads(child.stdout)
    record_property("operation_seconds", result["seconds"])
    record_property("child_wall_seconds", time.perf_counter() - started)
    assert result["result"] == "---"
    assert result["seconds"] < 1


def test_parse_frontmatter_caller_preserves_yaml_and_heading():
    assert _parse_frontmatter("---\n\ntitle: Example\n---\n\n# Example\n\nBody\n") == (
        {"title": "Example"}, "Body",
    )
    assert _parse_frontmatter("plain\nbody") == ({}, "plain\nbody")


def test_content_preview_caller_preserves_normalization_and_limit():
    text = "---\nmeta: hidden\n---\t\r\n\nBody\n\twords " + "x" * 600
    assert _content_preview(text) == ("Body words " + "x" * 600)[:500]
    assert _content_preview("plain\n\t body") == "plain body"


@pytest.mark.parametrize("kind", ["raw", "wiki"])
def test_reindex_callers_strip_frontmatter(kind, tmp_path):
    kb = KBStore(tmp_path / "data")
    if kind == "raw":
        record = kb.ingest(title="Example", content="original")
        ref_id = record.id
    else:
        record = kb.save_wiki("example", "Example", "original")
        ref_id = record.slug
    text = "---\n\ntitle: Example\n--- \t\n\nindexed body\n"
    (kb.kb_dir / record.file_path).write_text(text)
    assert kb.reindex() == {"raw": int(kind == "raw"), "wiki": int(kind == "wiki")}
    conn = kb._conn()
    try:
        rows = conn.execute("SELECT body FROM fts_content WHERE ref_id=? AND kind=?", (ref_id, kind)).fetchall()
        assert [row["body"] for row in rows] == ["indexed body\n"]
    finally:
        conn.close()


def test_librarian_body_hash_caller_ignores_metadata(tmp_path):
    kb = KBStore(tmp_path / "data")
    runner = LibrarianRunner(kb, db_path=tmp_path / "state.db")
    text = "---\n\ntitle: Example\n--- \t\n\nbody\n"
    assert runner._body_hash(text) == _content_hash("body\n")
    assert runner._body_hash("plain\nbody") == _content_hash("plain\nbody")


def test_wiki_builder_caller_indexes_only_body(tmp_path):
    kb = KBStore(tmp_path / "data")
    text = "---\n\ntitle: Example\n--- \t\n\nwiki body\n"
    assert save_wiki_pages(kb, [{"slug": "example", "title": "Example", "content": text}]) == ["example"]
    conn = kb._conn()
    try:
        assert conn.execute("SELECT body FROM fts_content WHERE kind='wiki'").fetchone()["body"] == "wiki body\n"
    finally:
        conn.close()
    assert (kb.kb_dir / "wiki/example.md").read_text() == text


def test_skill_loader_caller_preserves_metadata_and_strips_body(tmp_path):
    directory = tmp_path / "example"
    directory.mkdir()
    path = directory / "SKILL.md"
    path.write_text("---\n\nname: example\ndescription: Example skill\nallowed-tools: Read Grep\n--- \t\n\nRead files.\n")
    skill = parse_skill_md(path)
    assert skill is not None
    assert (skill.name, skill.description, skill.body, skill.allowed_tools) == (
        "example", "Example skill", "Read files.", ["Read", "Grep"],
    )
