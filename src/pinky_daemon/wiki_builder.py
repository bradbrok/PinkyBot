"""Wiki Builder — generates wiki pages from raw KB sources.

Reads raw sources from data/kb/raw/, generates interconnected wiki pages
in data/kb/wiki/topics/ and data/kb/wiki/people/. Uses an LLM to determine
taxonomy, merge related content, and produce dense standalone pages.

Can run incrementally (only process new sources) or full rebuild.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pinky_daemon.kb_store import KBStore, _content_hash, _FRONTMATTER_RE


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


WIKI_BUILDER_SYSTEM_PROMPT = """\
You are a wiki builder. Your job is to read raw source documents and produce \
interconnected wiki pages organized by topic and person.

## Rules

1. **One page per topic or person.** Don't create overlapping pages. If two \
sources cover the same topic, merge them into one page.

2. **Dense over long.** Each page should be the most efficient way to get \
up to speed. Cut filler. Every sentence should earn its place.

3. **Standalone.** A reader should understand the page without reading other \
pages. Include enough context.

4. **Backlinked.** Every factual claim should reference which raw source it \
came from, using the format: `[source: raw-ID]`.

5. **Cross-linked.** Use `[[Page Title]]` to link between wiki pages. Only \
link to pages that exist or that you're creating in this batch.

6. **Opinionated.** Include the owner's perspective and the filing agent's \
analysis when present in the sources. Mark opinions clearly.

7. **Categorize correctly:**
   - `topics/` — concepts, technologies, events, products, trends
   - `people/` — individuals (not companies — companies go in topics)

8. **Frontmatter format:**
```yaml
---
title: Page Title
slug: topics/page-slug  OR  people/person-slug
sources: [raw-2026-04-08-001, raw-2026-04-08-003]
related: [topics/other-topic, people/some-person]
---
```

9. **Merge decisions:**
   - If a new source adds info to an existing topic, UPDATE the existing page
   - If a source covers a genuinely new topic, CREATE a new page
   - When in doubt, merge into the broader topic rather than fragmenting

10. **Section structure** for topic pages:
```
# Title
[1-2 sentence summary]

## Key Points
- ...

## Details
[expanded content organized logically]

## Our Take
[owner/agent perspective if available]

## Sources
- [source description](raw-ID)
```

11. **Section structure** for people pages:
```
# Person Name
[1-2 sentence summary of who they are and why they matter]

## Background
- Role, company, known for

## Recent Activity
[what they've been saying/doing, from sources]

## Our Take
[owner/agent perspective if available]

## Sources
- [source description](raw-ID)
```
"""

WIKI_BUILDER_USER_PROMPT = """\
Here are the raw sources to process:

{sources_content}

---

Existing wiki pages (for merge/update decisions):
{existing_pages}

---

Generate wiki pages from these sources. Return your response as a JSON array \
of page objects, each with:
- "slug": the page path (e.g. "topics/llm-knowledge-bases" or "people/andrej-karpathy")
- "title": human-readable title
- "content": full markdown content INCLUDING frontmatter
- "sources": list of raw source IDs used
- "related": list of related page slugs

Respond ONLY with the JSON array, no other text. Example:
```json
[
  {{
    "slug": "topics/example",
    "title": "Example Topic",
    "content": "---\\ntitle: Example Topic\\nslug: topics/example\\nsources: [raw-2026-04-08-001]\\nrelated: [people/some-person]\\n---\\n\\n# Example Topic\\n...",
    "sources": ["raw-2026-04-08-001"],
    "related": ["people/some-person"]
  }}
]
```
"""


def _read_raw_sources(kb: KBStore, source_ids: list[str] | None = None) -> str:
    """Read raw source files and format them for the prompt."""
    if source_ids:
        sources = [kb.get_raw(sid) for sid in source_ids]
        sources = [s for s in sources if s is not None]
    else:
        sources = kb.list_raw(limit=500)

    parts = []
    for src in sources:
        content = kb.get_raw_content(src.id)
        if content:
            parts.append(f"--- SOURCE: {src.id} ---\n{content}\n--- END SOURCE ---\n")

    return "\n".join(parts)


def _read_existing_wiki(kb: KBStore) -> str:
    """Read existing wiki page metadata for merge decisions."""
    pages = kb.list_wiki(limit=500)
    if not pages:
        return "(no existing wiki pages)"

    lines = []
    for p in pages:
        lines.append(
            f"- {p.slug}: \"{p.title}\" (sources: {p.sources}, related: {p.related})"
        )
    return "\n".join(lines)


def build_wiki_prompt(
    kb: KBStore,
    source_ids: list[str] | None = None,
) -> tuple[str, str]:
    """Build the system + user prompts for wiki generation.

    Returns (system_prompt, user_prompt).
    """
    sources_content = _read_raw_sources(kb, source_ids)
    existing_pages = _read_existing_wiki(kb)

    user_prompt = WIKI_BUILDER_USER_PROMPT.format(
        sources_content=sources_content,
        existing_pages=existing_pages,
    )

    return WIKI_BUILDER_SYSTEM_PROMPT, user_prompt


def parse_wiki_response(response_text: str) -> list[dict]:
    """Parse the LLM response into wiki page objects."""
    # Try to extract JSON from the response
    # Handle markdown code fences
    text = response_text.strip()
    if text.startswith("```"):
        # Remove code fences
        lines = text.split("\n")
        # Remove first and last lines if they're fences
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        pages = json.loads(text)
        if isinstance(pages, list):
            return pages
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in the text
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            pages = json.loads(match.group())
            if isinstance(pages, list):
                return pages
        except json.JSONDecodeError:
            pass

    _log(f"[WikiBuilder] Failed to parse response: {text[:200]}")
    return []


def save_wiki_pages(kb: KBStore, pages: list[dict]) -> list[str]:
    """Save generated wiki pages to disk and index them.

    Returns list of saved slugs.
    """
    saved = []
    conn = kb._conn()

    try:
        for page in pages:
            slug = page.get("slug", "")
            title = page.get("title", "")
            content = page.get("content", "")
            sources = page.get("sources", [])
            related = page.get("related", [])

            if not slug or not content:
                _log(f"[WikiBuilder] Skipping page with missing slug or content")
                continue

            # Ensure directory exists
            file_path = f"wiki/{slug}.md"
            full_path = kb.kb_dir / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)

            # Write file
            full_path.write_text(content, encoding="utf-8")

            c_hash = _content_hash(content)
            now = datetime.now(timezone.utc).isoformat()

            # Upsert in SQLite
            conn.execute(
                """INSERT INTO wiki_pages
                   (slug, title, last_updated, sources, related, file_path, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                       title = excluded.title,
                       last_updated = excluded.last_updated,
                       sources = excluded.sources,
                       related = excluded.related,
                       content_hash = excluded.content_hash""",
                (
                    slug, title, now,
                    json.dumps(sources), json.dumps(related),
                    file_path, c_hash,
                ),
            )

            # Update FTS — delete old entry, insert new
            conn.execute(
                "DELETE FROM fts_content WHERE ref_id = ? AND kind = 'wiki'",
                (slug,),
            )

            # Extract body from content (strip frontmatter)
            match = _FRONTMATTER_RE.match(content)
            body = match.group(2) if match else content

            conn.execute(
                "INSERT INTO fts_content (ref_id, kind, title, body, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (slug, "wiki", title, body, ""),
            )

            saved.append(slug)
            _log(f"[WikiBuilder] Saved wiki page: {slug} — {title}")

        conn.commit()
    finally:
        conn.close()

    return saved


async def run_wiki_builder(
    kb: KBStore,
    source_ids: list[str] | None = None,
    api_url: str = "http://localhost:8888",
) -> list[str]:
    """Run the wiki builder end-to-end.

    Generates prompts, calls LLM via agent session, saves results.
    Returns list of saved wiki page slugs.
    """
    system_prompt, user_prompt = build_wiki_prompt(kb, source_ids)

    # Use Anthropic API directly for the wiki builder
    try:
        import anthropic
    except ImportError:
        _log("[WikiBuilder] anthropic package not installed")
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        _log("[WikiBuilder] ANTHROPIC_API_KEY not set")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    _log("[WikiBuilder] Generating wiki pages...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = response.content[0].text
    pages = parse_wiki_response(response_text)

    if not pages:
        _log("[WikiBuilder] No pages generated")
        return []

    _log(f"[WikiBuilder] Generated {len(pages)} pages, saving...")
    saved = save_wiki_pages(kb, pages)
    _log(f"[WikiBuilder] Done — saved {len(saved)} pages: {saved}")

    return saved
