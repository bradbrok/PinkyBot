# Pinky — Open Source Personal AI Framework

> Spec v0.1 — Draft by Oleg, 2026-03-27

## Vision

**Pinky is a personal AI companion framework powered by Claude Code.**

It gives Claude Code a soul, long-term memory, and the ability to talk to people across messaging platforms — turning it from a coding tool into a full personal AI sidekick.

Today Pinky is a 56K-line monolithic Python app that handles everything: LLM orchestration, context management, tool execution, memory, channels, scheduling. Most of that is now redundant — Claude Code does it better. The open-source version strips Pinky down to its unique value: **memory, personality, and connectivity**.

## Architecture

```
┌─────────────────────────────────────────────┐
│                 Claude Code                  │
│         (LLM brain — agent loop,            │
│    context management, tool execution)       │
├─────────────────────────────────────────────┤
│              CLAUDE.md (Soul)                │
│     Identity, personality, boundaries,       │
│     user profiles, behavioral rules          │
├──────────┬──────────┬───────────┬────────────┤
│  Memory  │ Outreach │  Google   │  Custom    │
│   MCP    │   MCP    │   MCP     │   MCPs     │
│  Server  │  Server  │  Server   │  Servers   │
└──────────┴──────────┴───────────┴────────────┘
```

**Claude Code** is the brain. It handles all LLM calls, tool execution, context window management, and agent orchestration. No custom code needed.

**CLAUDE.md** is the soul. Identity, personality, user profiles, behavioral rules, and boundaries — all in a markdown file that Claude Code reads automatically.

**MCP Servers** are the capabilities. Each integration is a standalone MCP server that Claude Code connects to via its config.

## Components

### 1. Soul System (CLAUDE.md)

Replaces Pinky's "Heart" system (DB-backed system prompt sections).

The soul is a `CLAUDE.md` file at the project root containing:

```markdown
# Agent Name

## IDENTITY
Name, creature type, vibe, emoticon.
Who you are.

## SOUL
Core behavioral principles.
How you act.

## USER
User profiles — name, timezone, preferences,
communication style, relationships.

## BOUNDARIES
What you can do autonomously vs what needs approval.
Ethics, privacy rules, data handling.

## COMMUNICATION
Which channels exist, how to reach people.

## MEMORY
Salient facts that should persist across sessions.
Updated by the agent as it learns.
```

This replaces ~2,000 lines of Heart store code, DB schemas, and section management. It's version-controlled, human-readable, and Claude Code loads it automatically.

### 2. Memory MCP Server (`pinky-memory`)

**This is the crown jewel.** Pinky's reflection-based memory system with vector search, BM25 keyword search, salience decay, deduplication, and semantic linking.

#### Data Model

```
Reflection {
  id: string (UUID)
  type: insight | project_state | interaction_pattern | continuation | fact
  content: string
  context: string
  project: string
  salience: 1-5
  active: boolean
  entities: string[]          # People tagged
  embedding: float[]          # Vector for semantic search
  weight: float               # Decays over time
  access_count: int
  created_at: datetime
  accessed_at: datetime
  supersedes: string          # ID of reflection this replaces
  superseded_by: string
  event_date: string          # When the described event occurred
  source_session_id: string   # Where this was learned
  source_channel: string
  next_review_date: string    # Spaced review schedule
  review_interval_days: int
}

ReflectionLink {
  source_id: string
  target_id: string
  similarity: float
}
```

#### MCP Tools

| Tool | Description |
|------|-------------|
| `reflect` | Store a new memory (with type, salience, entities, project) |
| `recall` | Semantic + keyword search across memories |
| `introspect` | Aggregate stats (memory counts by type, project, timeframe) |
| `memory_query` | Structured filtering with presets (recent_insights, stale_projects, high_value, orphans, due_review) |
| `memory_update` | Update existing reflection (content, salience, active status) |
| `memory_link` | Create semantic links between related memories |
| `memory_hygiene` | Run maintenance (decay, dedup, consolidation, promotion) |

#### Search Architecture

Two-path retrieval for high recall:

1. **Vector search** — OpenAI `text-embedding-3-small` (1536-dim) stored in SQLite, cosine similarity ranking with recency boost
2. **BM25 keyword search** — SQLite FTS5 index for exact-match and keyword queries
3. **Hybrid merge** — Results from both paths are merged, deduplicated, and re-ranked

#### Memory Lifecycle

```
Input → Extract → Embed → Store → Recall → Decay → Archive
                                      ↓
                              Consolidate (merge similar)
                                      ↓
                              Promote (episodic → semantic)
                                      ↓
                              Review (spaced repetition)
```

- **Extraction**: LLM extracts memories from conversations automatically
- **Decay**: Daily weight decay (0.97/day, ~23-day half-life). High-salience facts are immune.
- **Consolidation**: Memories with >0.85 cosine similarity are auto-merged
- **Promotion**: Clusters of 3+ related episodic memories get promoted to semantic insights
- **Review**: Spaced repetition surfaces memories for periodic re-evaluation

#### Storage

SQLite with WAL mode. Single file, zero infrastructure. Portable — copy the `.db` file and you have all your memories.

### 3. Outreach MCP Server (`pinky-outreach`)

Multi-platform messaging. Send and receive messages across:

- **Telegram** — Bot API (personal DMs, group chats, forum topics)
- **Discord** — Bot (channels, threads, DMs)
- **Slack** — Bot (channels, threads, DMs)
- **iMessage** — AppleScript bridge (macOS only)
- **SMS** — Via configured gateway
- **Email** — IMAP/SMTP

#### MCP Tools

| Tool | Description |
|------|-------------|
| `send_message` | Send to any platform (telegram/discord/slack/imessage/sms) |
| `check_messages` | Poll for new inbound messages |
| `message_history` | Retrieve conversation history |
| `search_chat` | Search across message history |
| `add_reaction` | React to messages (platform-specific) |
| `send_photo` | Send images/files |
| `mute_channel` / `unmute_channel` | Manage notification state |

#### Inbound Flow

Messages arrive via platform webhooks/polling → MCP server formats them as channel events → Claude Code receives them as tool results or system reminders → Claude Code decides whether/how to respond → calls `send_message` to reply.

### 4. Google Services MCP Server (`pinky-google`)

Google Calendar + Gmail integration.

#### MCP Tools

| Tool | Description |
|------|-------------|
| `calendar_list_events` | List upcoming events |
| `calendar_create_event` | Create calendar events |
| `calendar_update_event` | Modify existing events |
| `calendar_delete_event` | Remove events |
| `gmail_read` | Read emails |
| `gmail_send` | Send emails |
| `gmail_search` | Search inbox |

OAuth2 with refresh token. Config provides client_id, client_secret, refresh_token.

### 5. Wake System (Scheduling)

Replaces Pinky's heartbeat daemon. Uses Claude Code's cron/scheduling capabilities or a simple external scheduler that wakes Claude Code on a schedule.

```yaml
# Example: wake config
schedules:
  - name: morning_check
    cron: "0 8 * * *"
    timezone: America/Los_Angeles
    prompt: "Good morning. Check calendar, messages, and any pending tasks."

  - name: evening_summary
    cron: "0 21 * * *"
    timezone: America/Los_Angeles
    prompt: "End of day. Summarize what happened, any open items."
```

Each scheduled wake launches a Claude Code session with the specified prompt. The soul (CLAUDE.md) and all MCP servers are available automatically.

## Configuration

Single `pinky.yaml` at the project root:

```yaml
# Soul
soul_path: ./CLAUDE.md    # or inline

# Memory
memory:
  db_path: ./data/memory.db
  embedding_model: text-embedding-3-small
  embedding_provider: openai   # or local
  vector_dimensions: 1536
  search_top_k: 10
  decay_factor: 0.97
  consolidation_threshold: 0.85

# Outreach
outreach:
  telegram:
    bot_token: ${TELEGRAM_BOT_TOKEN}
    mode: polling   # or webhook
  discord:
    bot_token: ${DISCORD_BOT_TOKEN}
  slack:
    bot_token: ${SLACK_BOT_TOKEN}

# Google
google:
  client_id: ${GOOGLE_CLIENT_ID}
  client_secret: ${GOOGLE_CLIENT_SECRET}
  refresh_token: ${GOOGLE_REFRESH_TOKEN}

# Wake schedules
schedules:
  - name: morning
    cron: "0 8 * * *"
    prompt: "Morning check-in"
```

## Getting Started (Target UX)

```bash
# Install
pip install pinky-ai

# Initialize
pinky init
# Creates: CLAUDE.md (soul template), pinky.yaml (config), data/ (storage)

# Configure
# Edit CLAUDE.md — give your AI a personality
# Edit pinky.yaml — add API keys, enable channels

# Run MCP servers
pinky serve
# Starts memory + outreach + google MCP servers

# Connect Claude Code
pinky connect
# Writes Claude Code MCP config to ~/.claude/settings.json

# Talk to your AI
claude
# Claude Code now has memory, messaging, and your soul file
```

## Migration from Monolithic Pinky

### What Gets Kept
- **Memory DB** — `reflections.db` carries over as-is. All memories preserved.
- **Soul/personality** — Heart sections become CLAUDE.md sections. Same content, simpler format.
- **Tool integrations** — Google, voice, etc. become standalone MCP servers.

### What Gets Dropped (~40K lines)
- **LLM orchestration** (`pinky/llm/`, `pinky/agents/`) — Claude Code handles this
- **Context engine** (`pinky/context/`) — Claude Code manages its own context
- **Channel adapters** (`pinky/channels/`) — Outreach MCP replaces this
- **Session management** (`pinky/sessions/`) — Claude Code handles sessions
- **FastAPI app** (`pinky/main.py`, `pinky/routes/`) — No web server needed
- **Heartbeat daemon** (`pinky/heartbeat/`) — Wake system replaces this
- **Frontend** (`frontend/`) — Claude Code is the interface

### Migration Steps

1. **Extract memory MCP** — Pull `pinky/mcp/` into standalone package. Already uses FastMCP. Point it at existing `reflections.db`.

2. **Convert Heart → CLAUDE.md** — Export heart sections from DB, write to CLAUDE.md. One-time operation.

3. **Configure outreach MCP** — Move Telegram/Discord bot tokens to outreach config. Same bots, different runtime.

4. **Extract Google MCP** — Pull `pinky/tools/google/` into standalone MCP. Carry over OAuth tokens.

5. **Set up wake schedules** — Convert heartbeat tasks to cron definitions.

6. **Connect Claude Code** — Add MCP server configs. Done.

## Repository Structure

```
pinky/
├── CLAUDE.md.template        # Soul template for new users
├── pinky.yaml.example        # Config example
├── pyproject.toml
├── src/
│   ├── pinky_memory/         # Memory MCP server
│   │   ├── server.py         # FastMCP server
│   │   ├── store.py          # SQLite reflection store
│   │   ├── embeddings.py     # Embedding client (OpenAI / local)
│   │   ├── types.py          # Pydantic models
│   │   ├── search.py         # Hybrid vector + BM25 search
│   │   ├── lifecycle.py      # Decay, consolidation, promotion
│   │   └── hygiene.py        # Dedup, orphan cleanup, review
│   ├── pinky_outreach/       # Messaging MCP server
│   │   ├── server.py
│   │   ├── telegram.py
│   │   ├── discord.py
│   │   ├── slack.py
│   │   └── imessage.py
│   ├── pinky_google/         # Google services MCP server
│   │   ├── server.py
│   │   ├── calendar.py
│   │   └── gmail.py
│   └── pinky_cli/            # CLI (init, serve, connect)
│       ├── __main__.py
│       ├── init.py
│       ├── serve.py
│       └── connect.py
├── data/                     # Local storage (gitignored)
│   ├── memory.db
│   └── downloads/
└── docs/
    ├── getting-started.md
    ├── soul-guide.md         # How to write a good CLAUDE.md
    ├── memory-system.md      # Deep dive on memory architecture
    └── mcp-servers.md        # MCP server reference
```

## Key Dependencies

- `mcp` (FastMCP) — MCP server framework
- `pydantic` — Data validation
- `sqlite3` — Storage (stdlib, zero deps)
- `openai` — Embeddings (optional, can use local)
- `numpy` — Vector math for similarity search
- `python-telegram-bot` — Telegram integration
- `discord.py` — Discord integration
- `google-auth` / `google-api-python-client` — Google services

## What Makes This Different

| | Pinky | Other AI assistants |
|---|---|---|
| **Brain** | Claude Code (best-in-class) | Custom LLM wrappers |
| **Memory** | Vector + BM25 hybrid with decay, consolidation, promotion | Simple RAG or none |
| **Personality** | CLAUDE.md soul file — human-readable, version-controlled | Hardcoded prompts |
| **Channels** | Telegram, Discord, Slack, iMessage, SMS, email | Usually one platform |
| **Storage** | SQLite — single file, zero infra, fully portable | Postgres, Redis, etc. |
| **Setup** | `pip install` + edit two files | Docker, cloud services, API keys |
| **Cost** | Claude Code subscription only | Per-token API billing |

## Open Questions

1. **Naming** — Keep "Pinky" or rebrand for open source? (Pinky has history and personality. But maybe something more generic for a framework?)

2. **Embedding provider** — Default to OpenAI embeddings (best quality) or ship with a local model (zero external deps)? Could offer both with local as default.

3. **Outreach scope** — Ship all platforms in v1 or start with Telegram only and add others incrementally?

4. **Voice** — Include Twilio voice calling in v1 or defer? It's complex and requires paid Twilio account.

5. **License** — MIT? Apache 2.0? AGPL?

6. **Claude Code dependency** — Framework is tightly coupled to Claude Code. Should there be an abstraction layer for other agent runtimes? (Probably not — Claude Code IS the differentiator.)

## Phases

### Phase 1: Memory MCP (Week 1)
Extract `pinky/mcp/` into `pinky-memory`. Standalone MCP server with reflect, recall, introspect, query, hygiene tools. SQLite storage, OpenAI embeddings. Ship as pip package.

### Phase 2: Soul System + CLI (Week 2)
CLAUDE.md template system. `pinky init` CLI that scaffolds a new project. `pinky connect` that writes Claude Code MCP config. Documentation for writing good soul files.

### Phase 3: Outreach MCP (Week 3-4)
Extract messaging into `pinky-outreach`. Start with Telegram, add Discord and Slack. Inbound webhook + outbound send. Message history and search.

### Phase 4: Polish + Launch (Week 5)
Docs, examples, demo video. GitHub repo, PyPI package. README that shows the 5-minute setup. Blog post: "How I turned Claude Code into a personal AI that remembers everything."

---

*This spec is a living document. Updated as decisions are made.*
