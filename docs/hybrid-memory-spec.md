# PinkyBot Hybrid Memory Architecture

**Status**: Draft
**Author**: Misha
**Date**: 2026-03-29

## Problem

PinkyBot's file-based memory backend reimplements what Claude Code already provides natively. Running both creates duplicate systems fighting over the same job — markdown files with YAML frontmatter, an index, topic files. Meanwhile, the SQLite vector backend offers capabilities Claude Code genuinely lacks but isn't properly bridged to the native layer.

## Proposal

Eliminate the file-based backend. Let Claude Code's native memory be Tier 1. Reshape the SQLite MCP server into a Tier 2 "long-term memory" layer that *complements* rather than *competes with* the built-in system.

---

## Architecture: Two Tiers

### Tier 1: Claude Code Native Memory (Working Memory)

**What it is**: The memory system Claude Code already ships with.

**Components**:
- `CLAUDE.md` — Project instructions, identity, loaded every session
- `MEMORY.md` — Auto-loaded index (~200 lines), pointers to topic files
- `memory/*.md` — Topic files with YAML frontmatter (type, name, description)
- Types: `user`, `feedback`, `project`, `reference`

**Characteristics**:
- Auto-loaded every session (zero config)
- Human-readable, git-trackable
- Managed via native Read/Write/Edit tools
- Agent writes directly — no MCP needed
- Good for: preferences, active project state, working decisions, correction history

**PinkyBot's role**: None. Hands off. Claude Code owns this layer entirely. The daemon should NOT write a file-based memory MCP into `.mcp.json` — it's redundant.

---

### Tier 2: Pinky Memory MCP (Long-Term Semantic Memory)

**What it is**: The SQLite + vector backend, exposed as an MCP server.

**Components**:
- SQLite database (WAL mode, single portable file)
- OpenAI `text-embedding-3-small` embeddings (1536-dim)
- `sqlite-vec` for indexed cosine search (NumPy fallback)
- FTS5 for keyword search (BM25 ranking)
- Hybrid merge: vector + keyword results deduplicated and re-ranked

**What it stores** (things Claude Code's native memory can't do well):
- **Semantic search** across hundreds/thousands of memories
- **Cross-session recall** — "what did we discuss about X three weeks ago?"
- **Entity-linked memories** — tag people, search by person
- **Source-bridged memories** — linked to specific Telegram/Discord/Slack messages
- **Salience-weighted retrieval** — important stuff floats up, noise decays
- **Memory graph** — bidirectional links between related memories

**Data model** (unchanged from current `types.py`):
```
Reflection {
  id, type, content, context, project, salience (1-5),
  active, no_recall, supersedes, superseded_by,
  entities[], source_session_id, source_channel, source_message_ids[],
  embedding[], created_at, accessed_at, access_count, weight,
  next_review_date, review_interval_days, event_date
}
```

**MCP Tools** (unchanged):
- `reflect()` — store with auto-embedding
- `recall()` — hybrid semantic + keyword search
- `introspect()` — aggregate stats
- `memory_query()` — structured filtering with presets
- `memory_links()` — graph traversal

---

## The Bridge: Tier 1 → Tier 2

The key question: how do important memories flow from working memory (markdown) into long-term memory (vectors)?

### Option A: Hook-Driven Extraction (Recommended)

Use Claude Code's `PreCompact` hook to extract memories before context compression.

**How it works**:
1. Claude Code fires `PreCompact` when context approaches the limit
2. A hook script runs before compression
3. The script calls `reflect()` on the Pinky Memory MCP with a summary of the session's key learnings
4. Working memory stays clean; long-term memory gets the important bits

**Hook config** (in Claude Code `settings.json`):
```json
{
  "hooks": {
    "PreCompact": [{
      "type": "command",
      "command": "python -m pinky_memory.hooks.pre_compact --session $SESSION_ID --dir $MEMORY_DIR"
    }]
  }
}
```

**Hook script** (`pinky_memory/hooks/pre_compact.py`):
- Reads the current MEMORY.md and recent topic files
- Diffs against what's already in the SQLite store (avoid duplicates)
- Calls `reflect()` for new/changed memories
- Lightweight — runs in <2 seconds

### Option B: Agent-Driven Sync

The agent itself decides when to promote memories to Tier 2.

**How it works**:
- Agent instructions (in CLAUDE.md / soul) include guidance: "When you learn something important that should persist beyond this session, call `reflect()` to store it in long-term memory."
- No automation needed — the agent uses judgment
- Works today with zero infrastructure changes

**Trade-off**: Relies on the agent remembering to do it. Easy to forget under load. But also the simplest path.

### Option C: Daemon Heartbeat Sync

The daemon periodically scans native memory files and syncs to SQLite.

**How it works**:
1. On each heartbeat (or a dedicated cron), daemon reads `MEMORY.md` + topic files
2. Hashes content to detect changes since last sync
3. New/changed memories get embedded and stored via `reflect()`
4. Runs as a background job — agent doesn't need to think about it

**Trade-off**: More infrastructure, but fully automatic. Good for production.

### Recommendation: Start with B, add A

Option B works immediately with zero code changes — just update the soul/CLAUDE.md to instruct agents to use `reflect()` for important cross-session memories. Then add the PreCompact hook (Option A) as a safety net to catch what agents forget. Option C is nice-to-have for later.

---

## What to Cut

### Remove: File-Based Memory Backend

**Files to delete**:
- `src/pinky_memory/file_store.py`
- `src/pinky_memory/file_server.py`

**Why**: Claude Code's native memory does the same thing better — it's auto-loaded, doesn't need an MCP server, and the agent already knows how to use it.

### Remove: File Backend from Daemon MCP Config

**Change in `api.py`**:
- Stop writing `pinky-memory` with `--backend file` into `.mcp.json`
- Always wire the SQLite backend (or don't wire memory MCP at all if the agent has no long-term memory needs)

### Keep: Everything in the SQLite Backend

The SQLite store, vector search, types, server — all stay. This is the value-add over native Claude Code.

---

## Background Jobs (Phase 2)

These are designed in the spec but not implemented. They should run as daemon tasks or cron jobs, not inside the MCP server:

### 1. Decay Application
- **Frequency**: Daily
- **Logic**: `weight *= 0.97` for all memories where `salience < 4`
- **Implementation**: Daemon cron or `pinky_memory.jobs.decay`

### 2. Consolidation
- **Frequency**: Weekly
- **Logic**: Find memory pairs with >0.85 cosine similarity, merge into single reflection (supersession chain)
- **Implementation**: `pinky_memory.jobs.consolidate`

### 3. Promotion
- **Frequency**: Weekly
- **Logic**: Clusters of 3+ episodic memories on same topic → create a semantic insight, mark originals as superseded
- **Implementation**: `pinky_memory.jobs.promote`

### 4. Review Scheduling
- **Frequency**: Daily
- **Logic**: Surface memories past their `next_review_date`, bump interval on confirmation
- **Implementation**: `pinky_memory.jobs.review` — could output to agent's wake context

### 5. Hygiene
- **Frequency**: Monthly
- **Logic**: Archive memories with weight < 0.1, remove orphan links, dedup exact matches
- **Implementation**: `pinky_memory.jobs.hygiene`

All jobs should be runnable as:
```bash
python -m pinky_memory.jobs.decay --db ./data/memory.db
python -m pinky_memory.jobs.consolidate --db ./data/memory.db
```

And schedulable via daemon config:
```yaml
memory:
  jobs:
    decay: "0 4 * * *"        # daily at 4am
    consolidate: "0 5 * * 0"  # weekly Sunday 5am
    promote: "0 5 * * 0"      # weekly Sunday 5am
    review: "0 6 * * *"       # daily at 6am
    hygiene: "0 3 1 * *"      # monthly 1st at 3am
```

---

## Migration Path

### Step 1: Update Soul Templates (Now)
Add to default CLAUDE.md / soul template:
```
## Memory
- Working memory: Use native MEMORY.md and memory/*.md files for active project state
- Long-term memory: Use reflect() to store important cross-session learnings
- Recall: Use recall("query") to search long-term memory when context is missing
- Don't duplicate — if it's in MEMORY.md, don't also reflect() it unless it needs semantic search
```

### Step 2: Remove File Backend from MCP Config (Now)
Update `api.py` to wire SQLite backend by default (or no memory MCP if OPENAI_API_KEY is absent).

### Step 3: Delete File Backend Code (Now)
Remove `file_store.py`, `file_server.py`, update `__main__.py` to drop `--backend file` option.

### Step 4: Add PreCompact Hook (Soon)
Write `pinky_memory/hooks/pre_compact.py`, document hook setup in README.

### Step 5: Implement Background Jobs (Phase 2)
Build the five jobs as standalone scripts, wire into daemon cron config.

---

## Summary

| Layer | Owner | Storage | Search | Auto-loaded | Best For |
|-------|-------|---------|--------|-------------|----------|
| Tier 1 (Native) | Claude Code | Markdown files | Read tool | Yes | Active state, preferences, corrections |
| Tier 2 (Pinky) | MCP Server | SQLite + vectors | Semantic + keyword | On demand | Cross-session recall, entity search, decay |
| Bridge | Hook / Agent | — | — | — | Promoting important Tier 1 → Tier 2 |

**Net result**: Less code, no duplicate systems, each layer does what it's best at. Agents get human-readable working memory for free and semantic long-term memory when they need it.
