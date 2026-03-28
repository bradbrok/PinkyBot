# Memory System

Pinky ships with two memory backends. Choose based on your needs.

## File-Based Memory (Default)

Memories are individual markdown files with YAML frontmatter, indexed by `MEMORY.md`. Human-readable, git-trackable, zero dependencies.

### Directory Structure

```
memory/
├── MEMORY.md              # Index -- one-line entries with links
├── user_preferences.md    # Individual memory files
├── project_website.md
├── feedback_testing.md
└── reference_api_docs.md
```

### Memory File Format

```markdown
---
name: User Preferences
description: Brad's communication and coding preferences
type: user
---

Prefers casual tone. Likes concise code reviews.
Values high autonomy -- delegates and expects follow-through.
```

### Memory Types

| Type | Purpose | Example |
|------|---------|---------|
| `user` | Info about the user | Role, preferences, knowledge |
| `feedback` | How to approach work | Corrections and confirmed approaches |
| `project` | Ongoing work context | Goals, decisions, deadlines |
| `reference` | External resource pointers | Where to find things |

### MCP Tools

| Tool | Description |
|------|-------------|
| `memory_save` | Create a new memory file |
| `memory_read` | Read a specific memory by filename |
| `memory_update` | Update an existing memory |
| `memory_delete` | Delete a memory and remove from index |
| `memory_list` | List all memories (optionally filter by type) |
| `memory_search` | Keyword search across all memories |
| `memory_index` | Read the MEMORY.md index |

## SQLite Memory (Advanced)

For power users who want semantic search, vector embeddings, salience decay, and automatic consolidation.

### Features

- **Hybrid search** -- Vector embeddings (OpenAI) + BM25 keyword search
- **Salience decay** -- Memories fade unless reinforced (0.97/day, ~23-day half-life)
- **Consolidation** -- Similar memories (>0.85 cosine similarity) auto-merge
- **Promotion** -- Clusters of 3+ episodic memories promote to semantic insights
- **Spaced review** -- Periodic re-evaluation schedule

### Data Model

Each memory (reflection) has:
- `type`: insight, project_state, interaction_pattern, continuation, fact
- `salience`: 1-5 importance score
- `weight`: decays over time
- `entities`: tagged people
- `embedding`: 1536-dim vector for semantic search
- `links`: connections to related memories

### MCP Tools

| Tool | Description |
|------|-------------|
| `reflect` | Store a new memory with embedding |
| `recall` | Semantic + keyword hybrid search |
| `introspect` | Aggregate stats and patterns |
| `memory_query` | Structured filtering with presets |
| `memory_links` | Explore the memory graph |

### Lifecycle

```
Input → Extract → Embed → Store → Recall → Decay → Archive
                                      ↓
                              Consolidate (merge similar)
                                      ↓
                              Promote (episodic → semantic)
                                      ↓
                              Review (spaced repetition)
```

### Storage

Single SQLite file with WAL mode. Copy the `.db` file to migrate your memories anywhere.

## Choosing a Backend

| | File-Based | SQLite |
|---|---|---|
| **Setup** | Zero config | Needs OpenAI API key |
| **Search** | Keyword only | Semantic + keyword |
| **Readability** | Human-readable markdown | Binary DB file |
| **Git tracking** | Yes | No |
| **Memory decay** | Manual | Automatic |
| **Best for** | Getting started, simple use | Production, large memory stores |
