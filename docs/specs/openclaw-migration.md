# Spec: OpenClaw → PinkyBot Migration Tool

**Issue:** #101  
**Status:** Draft — reviewed by Pushok + Ryzhik  
**Last updated:** 2026-04-04

---

## Goal

Build a first-class migration path for OpenClaw users to bring their agents into PinkyBot. Removes the switching cost, signals PinkyBot as the destination platform for serious agent users.

OpenClaw is the main competitor: MIT license, ~100K GitHub stars, 24+ platform integrations, ClawHub skills marketplace.

---

## OpenClaw Agent Structure

An OpenClaw agent is a directory of Markdown files + a global JSON5 config. No per-agent daemon.

| File | Purpose |
|---|---|
| `SOUL.md` | Personality, tone, behavioral limits (blends identity + ethics in one file) |
| `IDENTITY.md` | Name, agent ID, role label |
| `AGENTS.md` | Operating procedures, workflows, handoff protocols |
| `USER.md` | Static owner profile: name, timezone, comm prefs |
| `TOOLS.md` | Tool documentation and constraints |
| `HEARTBEAT.md` | Scheduled tasks in plain English |
| `MEMORY.md` | Long-term persistent facts |
| `memory/YYYY-MM-DD.md` | Daily rolling context notes |

Global config (`~/.openclaw/openclaw.json`):
- Channel tokens (Telegram, Discord, Slack, WhatsApp, iMessage, etc.)
- Model selection (`provider/model` string)
- Per-skill env vars and API keys
- Identity-to-channel routing map

---

## Migration Manifest (What Moves Where)

### Automatic — high confidence
| OpenClaw | PinkyBot | Notes |
|---|---|---|
| `IDENTITY.md` name/role | `agents.name`, `agents.display_name`, `agents.role` | Direct mapping |
| `SOUL.md` (personality section) | `agents.soul` | Claude-assisted split from limits section |
| `SOUL.md` (limits/ethics section) | `agents.boundaries` | Claude-assisted split |
| `USER.md` | `agents.users` | Verbatim text preservation |
| Bot tokens (Telegram/Discord/Slack) | `agent_tokens` table | Same tokens work in PinkyBot |
| `channels.*.allowFrom` user IDs | `approved_users` table | Direct mapping |
| `.clawhub/lock.json` skill list | Skill registrations | Directive-only (see Skills section) |
| `provider/model` string | `agents.model` + provider lookup | Mapping table needed (see below) |

### Claude-assisted — processed by LLM
| OpenClaw | PinkyBot | Why LLM needed |
|---|---|---|
| `MEMORY.md` (unstructured Markdown) | `pinky_memory` Reflection records | Type classification, salience scoring, semantic chunking |
| `AGENTS.md` procedures | `agent_directives` records | Paragraph → individual directive splitting |
| `HEARTBEAT.md` plain-English tasks | `agent_schedules` cron entries | Natural language → cron expression parsing |

**Key insight (Ryzhik):** PinkyBot's `soul` field is raw text assembled with boundaries, directives, skills, and owner profile into the final system prompt via `build_system_prompt()`. OpenClaw personality must be decomposed into these separate layers — it cannot be dumped raw into `soul` as a monolith.

### Manual — user must provide post-migration
- Per-skill API keys / env vars (secrets cannot migrate)
- MCP server configs for skills (OpenClaw skills are instruction-only)
- Platforms not supported by PinkyBot (see Platform Gap below)

### Skipped in v1
- Conversation history — too large, too noisy; offer as optional archive download
- Memory SQLite direct import — text extraction + re-embed is the practical path
- `memory/YYYY-MM-DD.md` daily notes — punted to v2 (import as `continuation` type)

---

## Platform Gap

PinkyBot supports: **Telegram, Discord, Slack**

OpenClaw supports: Telegram, Discord, Slack, WhatsApp, iMessage, Signal, Matrix, WeChat, LINE, and 14 more.

**Policy:** Surface unsupported platforms explicitly in the preview UI — never silently drop them. Name them specifically: "OpenClaw's WhatsApp integration isn't supported in PinkyBot yet." Don't be vague.

---

## UX Flow (Pushok)

### Entry point
"Import from OpenClaw" as a third path in the existing agent creation wizard, alongside "Start from scratch" and "Use template." Keeps it discoverable without cluttering nav.

### Step 1 — Upload
User provides:
- OpenClaw workspace directory (as zip)
- Optional: `~/.openclaw/openclaw.json` (channel tokens + model config)
- Optional: `.clawhub/lock.json` (skill enumeration)

Upload or paste. No live OpenClaw API sync in v1.

### Step 2 — Preview
Grouped by category: **Identity**, **Memory**, **Connections**, **Automation**

Each item gets a status badge:
- ✅ Will import cleanly
- ⚠️ Will import with caveats (tooltip explains what changed)
- ❌ Not supported (tooltip names the specific feature/platform)

Show all three — don't hide ❌ items. That's how you set expectations before the user commits.

**Memory section:** Show count ("142 memories will be imported") + sample entries. Optional "curate on import" — search/filter list so power users can uncheck noise before importing.

**Soul/identity section:** Show rendered diff — what was in OpenClaw vs what will land in Pinky (especially the SOUL.md → soul + boundaries split). If normalization is happening, users see it.

**Skills section:** Matched skills (in PinkyBot catalog) vs unmatched ("not available in PinkyBot — will import as directive-only"). Don't silently drop skills.

### Step 3 — Confirm + Create
- Creates agent immediately in `stopped` state
- Memory import runs async in the background (chunking + classification + embedding takes time)
- Progress indicator — don't fake "done" before memories land
- Poll `GET /api/migrate/openclaw/status/{task_id}` for memory import progress
- If import fails partway: show what succeeded and what failed — no silent partial imports

### Naming conflicts
If agent name already exists: create with suffix (`alice-imported`). Easy to rename post-creation.

---

## Backend Architecture (Ryzhik)

### Module structure

```
src/pinky_daemon/
  migration/
    __init__.py
    mif_parser.py       # MIF JSON-LD + MEMORY.md → Reflection objects
    skill_converter.py  # ClawHub SKILL.md → Pinky skill format
    agent_mapper.py     # openclaw.json + workspace files → Pinky DB fields
    importer.py         # Orchestrates the whole flow
    api.py              # Migration API endpoints
```

### API endpoints

**`POST /api/migrate/openclaw/parse`**
- Accepts: multipart form with workspace zip + optional openclaw.json + optional lock.json
- Unpacks to temp dir, validates structure
- Returns: raw parsed manifest

**`POST /api/migrate/openclaw/preview`**
- Accepts: parsed manifest
- Triggers Claude-assisted processing: soul/boundaries split, schedule parsing, memory chunking + classification
- Returns: enriched migration manifest with ✅/⚠️/❌ status per item, warnings, count summaries

**`POST /api/migrate/openclaw/apply`**
- Accepts: confirmed manifest (user has reviewed, optionally deselected items)
- Synchronous: creates agent, directives, tokens, schedules, skills (all via existing API)
- Spawns background task for memory embedding + reflection store writes
- Returns: `{ agent_name, task_id }`

**`GET /api/migrate/openclaw/status/{task_id}`**
- Returns: `{ total, imported, failed, done }`

### Claude-assisted processing (`importer.py`)

```python
async def split_soul_boundaries(soul_md: str) -> tuple[str, str]:
    # Prompt: separate personality/identity from limits/ethics

async def parse_heartbeat_schedules(heartbeat_md: str) -> list[ScheduleEntry]:
    # Prompt: extract tasks, convert to cron + timezone

async def classify_memories(memory_md: str) -> list[ReflectionDraft]:
    # Prompt: chunk → type (fact/insight/continuation) + salience (1-5)

async def split_agents_directives(agents_md: str) -> list[DirectiveDraft]:
    # Prompt: identify distinct operating rules → individual directive records
```

### Memory import — MIF + MEMORY.md → Reflections

PinkyBot `reflections` table schema (Ryzhik):
```
id, type, content, context, project, salience (1-5), active,
no_recall, supersedes, embedding, created_at, accessed_at,
access_count, weight
```
Plus: `reflection_links` (bidirectional), `reflections_fts` (FTS5), `reflections_vec` (vector search).

**MIF mapping:**
- `mif:memoryNode.content` → `content`
- `mif:memoryNode.type` → Pinky types: fact / insight / project_state / interaction_pattern / continuation
- `mif:memoryNode.importance` → `salience` (normalize to 1-5)
- `mif:memoryNode.tags` → `entities` (JSON array)
- `mif:memoryNode.relationships` → `reflection_links`
- `mif:timestamp` → `created_at` / `accessed_at`
- `weight` defaults to 1.0, `active` to 1

**MEMORY.md (unstructured):** Claude chunks + classifies → yields `ReflectionDraft` objects → batch insert via `store.add_reflection()` → trigger embedding backfill.

**Embedding note:** MIF exports won't have Pinky-compatible embeddings. Store raw text, re-embed lazily using the existing `reflections_vec` backfill mechanism.

### Model mapping table

```python
MODEL_MAP = {
    "anthropic/claude-opus-4-5": ("opus", None),
    "anthropic/claude-sonnet-4-5": ("sonnet", None),
    "anthropic/claude-haiku-4-5": ("haiku", None),
    "openai/gpt-4o": ("gpt-4o", "openrouter"),
    "google/gemini-2-flash": ("gemini-2-flash", "openrouter"),
    # fallback: keep raw string, set provider=custom
}
```

### Skills — ClawHub → Pinky

OpenClaw SKILL.md files are **instruction-only** — no MCP server bindings. PinkyBot skills can additionally bind MCP servers.

Migrated OpenClaw skills land as directive-only skills. Skills with tool integrations (e.g. API key in env vars) get a ⚠️ badge in preview: "This skill had an API key (`OPENAI_API_KEY`) — you'll need to re-enter it after migration."

`skill_converter.py`:
- Extract name, description from ClawHub metadata → SKILL.md frontmatter
- Extract instructions → SKILL.md body
- Strip ClawHub-specific extensions (payment, marketplace metadata)
- Flag skills with OpenClaw-specific runtime API dependencies

---

## Risks & Edge Cases (Ryzhik)

| Risk | Mitigation |
|---|---|
| Embedding dimension mismatch | Store raw text, embed lazily — never depend on imported vectors |
| MIF version drift | Version detection in `mif_parser.py`; warn on unrecognized version |
| Scale (10K+ memories) | Batch insert with progress reporting, not one-by-one |
| Idempotency (migration fails halfway) | Idempotent keys on all writes; re-run is safe |
| Privacy (export bundle contains PII) | Clean up temp files post-import; note for HTTPS-only in docs |
| Entity resolution | MIF entities may not match Pinky's owner/people format; mapping step in `agent_mapper.py` |
| Partial upload (user only has workspace, no openclaw.json) | Graceful degradation — import what's available, surface what's missing in preview |

---

## Open Questions

- Should migration create agent in `stopped` state (user manually starts) or auto-start? **Lean: stopped.**
- Where do we store raw imported workspace files post-import? Useful for debugging, adds storage. **Lean: temp dir, clean up after apply.**
- Should we expose `POST /api/agents/{name}/export` (OpenClaw format) for symmetry? **Punted to v2.**
- Ingestion via CLI tool (`pinky-migrate` script) for power users — v1 or v2? **Ryzhik says v1 alongside file upload is good.**

---

## Out of Scope (v1)

- Batch migration (multiple agents at once)
- Live OpenClaw sync / incremental re-sync
- Platforms not supported by PinkyBot (WhatsApp, iMessage, Signal, Matrix, etc.)
- Conversation history import (offer archive download instead)
- MIF as required intermediate format
- `memory/YYYY-MM-DD.md` daily notes import
- Export to OpenClaw format

---

## Estimate

| Work | Time |
|---|---|
| Migration module structure + parse/preview/apply/status endpoints | 3 days |
| Claude-assisted processing (soul split, schedules, memory, directives) | 1 day |
| Memory embedding + async background pipeline | 1 day |
| Frontend wizard (upload → preview → confirm) | 2 days |
| **Total** | **~1 week** |
