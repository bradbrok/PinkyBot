# Memory System

Pinky uses a hybrid two-tier memory architecture. Claude Code's native memory handles working state; the SQLite MCP server handles long-term semantic recall.

## Tier 1: Working Memory (Claude Code Native)

Claude Code's built-in memory system. Auto-loaded every session, zero config.

### Components

- `MEMORY.md` — Index file (~200 lines), pointers to topic files
- `memory/*.md` — Topic files with YAML frontmatter

### Memory Types

| Type | Purpose | Example |
|------|---------|---------|
| `user` | Info about the user | Role, preferences, knowledge |
| `feedback` | How to approach work | Corrections and confirmed approaches |
| `project` | Ongoing work context | Goals, decisions, deadlines |
| `reference` | External resource pointers | Where to find things |

### Characteristics

- Auto-loaded every session
- Human-readable, git-trackable
- Managed via native Read/Write/Edit tools — no MCP needed
- Best for: active project state, preferences, corrections, working decisions

## Tier 2: Long-Term Memory (pinky-memory MCP)

SQLite + vector embeddings for semantic search across large memory stores.

### Features

- **Hybrid search** — Vector embeddings (OpenAI `text-embedding-3-small`) + BM25 keyword search
- **Salience decay** — Memories fade unless reinforced (0.97/day, ~23-day half-life)
- **Entity tagging** — Tag people, search by person
- **Memory graph** — Bidirectional links between related memories
- **Source bridging** — Link memories to specific Telegram/Discord/Slack messages
- **Spaced review** — Periodic re-evaluation schedule

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
| `reflect` | Store a new memory with auto-embedding |
| `recall` | Semantic + keyword hybrid search |
| `introspect` | Aggregate stats and patterns |
| `memory_query` | Structured filtering with presets |
| `memory_links` | Explore the memory graph |

### Running

```bash
# Default
python -m pinky_memory

# Custom DB path
python -m pinky_memory --db ./data/memory.db
```

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

## The Bridge: Tier 1 → Tier 2

How important memories flow from working memory into long-term memory:

1. **Agent-driven (now):** The agent decides when to promote. CLAUDE.md includes guidance to use `reflect()` for important cross-session learnings and `recall()` when context is missing.

2. **PreCompact hook (planned):** A Claude Code hook fires before context compression, automatically extracting key memories into the SQLite store.

## When to Use Which Tier

| Scenario | Tier |
|----------|------|
| Active project state | Tier 1 (MEMORY.md) |
| User preferences | Tier 1 (memory/*.md) |
| Correction from user | Tier 1 (feedback file) |
| "What did we discuss about X three weeks ago?" | Tier 2 (recall) |
| Cross-session insight worth preserving | Tier 2 (reflect) |
| Entity-specific search ("everything about Brad") | Tier 2 (recall with entity filter) |
| Quick reference pointer | Tier 1 (reference file) |
