# Pinky

**A personal AI companion framework powered by Claude Code.**

Pinky gives Claude Code a soul, long-term memory, and the ability to talk to people across messaging platforms -- turning it from a coding tool into a full personal AI sidekick.

## What is this?

Pinky is a set of MCP servers and a stateful API that extend Claude Code with:

- **Long-term memory** -- Vector + keyword hybrid search, salience decay, automatic consolidation
- **Multi-platform messaging** -- Telegram, Discord, Slack (iMessage, email planned)
- **Stateful sessions** -- REST API for managing Claude Code sessions with conversation history
- **Context management** -- Auto-restart with checkpoints when context fills up
- **Conversation store** -- Persistent, searchable transcript of every exchange
- **Personality** -- A soul file (CLAUDE.md) that gives your AI its identity

Claude Code handles all the hard stuff (LLM orchestration, context management, tool execution). Pinky handles everything else.

## Quick Start

```bash
# Install
pip install pinky-ai

# Start the API server
python -m pinky_daemon --mode api --port 8888

# Create a session
curl -X POST localhost:8888/sessions \
  -d '{"model": "sonnet", "system_prompt": "You are Pinky, a helpful AI companion."}'
# Returns: {"id": "pinky-a1b2c3", "state": "idle", ...}

# Send a message
curl -X POST localhost:8888/sessions/pinky-a1b2c3/message \
  -d '{"content": "Hey Pinky! What are you?"}'
# Returns: {"role": "assistant", "content": "I'm Pinky!", "duration_ms": 3200}

# Check context usage
curl localhost:8888/sessions/pinky-a1b2c3/context
# Returns: {"context_used_pct": 2.1, "needs_restart": false, ...}

# Search all conversations
curl "localhost:8888/conversations/search?q=restaurant"
```

## API

### Sessions

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/sessions` | Create a session (model, soul, tools, auto-restart) |
| `GET` | `/sessions` | List all active sessions |
| `GET` | `/sessions/{id}` | Get session info |
| `POST` | `/sessions/{id}/message` | Send a message, get response |
| `GET` | `/sessions/{id}/history` | In-memory conversation history |
| `GET` | `/sessions/{id}/context` | Context window status (tokens, %) |
| `POST` | `/sessions/{id}/restart` | Force checkpoint + context restart |
| `DELETE` | `/sessions/{id}` | Destroy session |

### Conversations (Persistent)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/conversations` | List all conversations |
| `GET` | `/conversations/{id}` | Get persisted history (survives restarts) |
| `GET` | `/conversations/search?q=...` | Full-text search across all conversations |

### Session Options

```json
{
  "model": "sonnet",
  "session_id": "my-session",
  "soul": "./CLAUDE.md",
  "system_prompt": "You are a helpful assistant.",
  "allowed_tools": ["Read", "Glob", "mcp__memory__*"],
  "max_turns": 25,
  "timeout": 300,
  "restart_threshold_pct": 80.0,
  "auto_restart": true
}
```

## Architecture

```
Pinky API Server (FastAPI)
    |
    +-- Session Manager
    |   +-- Session 1 (sonnet, chat context)
    |   +-- Session 2 (opus, deep work)
    |   +-- Session 3 (haiku, quick replies)
    |
    +-- Claude Agent SDK
    |   (streaming, hooks, real session management)
    |
    +-- Conversation Store (SQLite + FTS5)
    |
    +-- MCP Servers (capabilities)
        +-- pinky-memory    (long-term memory with vector search)
        +-- pinky-self      (schedules, tasks, research, health)
        +-- pinky-messaging (outbound messaging through broker)
```

**Claude Agent SDK** runs Claude Code programmatically -- streaming responses, real session IDs, tool permissions, hooks.

**CLAUDE.md** is your AI's personality. Edit it like any markdown file. Commit it to git.

**MCP Servers** provide capabilities. Memory, messaging -- each is a standalone server.

## Components

### Memory (Hybrid Two-Tier Architecture)

**Tier 1 — Working Memory (Claude Code native):** MEMORY.md and memory/*.md files. Auto-loaded every session, human-readable, git-trackable. Managed directly by the agent via Read/Write/Edit tools. No MCP server needed.

**Tier 2 — Long-Term Memory (`pinky-memory` MCP):** SQLite + vector embeddings (OpenAI) + BM25 keyword search. Semantic recall, salience decay, entity tagging, memory graph. Use `reflect()` to store cross-session learnings and `recall()` to search them.

Tier 1 handles active project state and preferences. Tier 2 handles everything that benefits from semantic search across hundreds of memories.

### Outreach (`pinky-outreach`)

Multi-platform messaging MCP server:
- **Telegram** -- Bot API (send, receive, photos, docs, reactions)
- **Discord** -- REST API (channels, DMs, files, reactions)
- **Slack** -- Web API (channels, threads, file uploads, reactions)

7 tools: `send_message`, `check_messages`, `send_photo`, `send_document`, `get_chat_info`, `add_reaction`, `bot_info`

### Daemon (`pinky-daemon`)

Two modes:

**API mode (default):** FastAPI server with stateful session management.
```bash
python -m pinky_daemon --mode api --port 8888
```

**Poll mode:** Auto-processes inbound messages from Telegram/Discord/Slack.
```bash
python -m pinky_daemon --mode poll --config pinky.yaml
```

### Soul System (`CLAUDE.md`)

Your AI's personality, values, and boundaries in a single markdown file:
- Identity (name, vibe, role)
- Core values and behavioral principles
- User profiles
- Boundaries and ethics
- Communication channels

### CLI

```bash
pinky init              # Scaffold a new project
pinky serve             # Start MCP servers
pinky connect           # Register with Claude Code
pinky run               # Start the daemon
```

## Context Management

Sessions track estimated token usage and auto-restart when context fills up:

1. Context approaches threshold (default 80%)
2. Checkpoint saves conversation summary
3. New Claude Code session starts with summary as context
4. Same session ID -- callers don't notice

Monitor from outside:
```bash
curl localhost:8888/sessions/my-session/context
# {"context_used_pct": 73.2, "needs_restart": false, "checkpoints": 2}
```

Force restart:
```bash
curl -X POST localhost:8888/sessions/my-session/restart
```

## Documentation

- [Getting Started](docs/getting-started.md)
- [Writing a Soul File](docs/soul-guide.md)
- [Memory System](docs/memory-system.md)
- [MCP Server Reference](docs/mcp-servers.md)
- [Full Spec](SPEC.md)

## Setup

### Prerequisites

- **Python 3.11+** (3.12-3.14 recommended)
- **Claude Code CLI** — install with `npm install -g @anthropic-ai/claude-code`
- **Claude authentication** — either a Max/Pro subscription or an API key

### Install

```bash
git clone https://github.com/bradbrok/PinkyBot.git
cd PinkyBot

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with all platform integrations
pip install -e ".[all,dev]"

# Or minimal (no Telegram/Discord/Slack):
pip install -e ".[dev]"
```

### Authenticate Claude

Pinky uses Claude Code under the hood. Authenticate before first run:

```bash
# Option 1: Log in with your Anthropic account (Max/Pro plan)
claude login

# Option 2: Use an API key
export ANTHROPIC_API_KEY=sk-ant-...
```

Check your auth status:
```bash
claude auth status
# Returns: {"loggedIn": true, "subscriptionType": "max", ...}
```

### Configure Telegram (optional)

To connect an agent to Telegram:

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Start the server and go to the Settings page at `http://localhost:8888/#/settings`
3. Enter your bot token under Outreach Platforms
4. Create an agent and set its bot token under the Agents page

Or configure via API:
```bash
curl -X PUT localhost:8888/outreach/platforms/telegram \
  -H 'Content-Type: application/json' \
  -d '{"token": "YOUR_BOT_TOKEN", "enabled": true}'
```

### Start the Server

```bash
# Start the API server with web UI
python -m pinky_daemon --mode api --port 8888 --host 0.0.0.0

# Open the dashboard
open http://localhost:8888
```

### Create Your First Agent

Via the web UI at `http://localhost:8888/#/agents`, or via API:

```bash
# Register an agent
curl -X POST localhost:8888/agents \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-agent", "model": "sonnet", "display_name": "My Agent"}'

# Wake it up
curl -X POST localhost:8888/agents/my-agent/wake?prompt=Hello

# Chat with it
curl -X POST localhost:8888/agents/my-agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"content": "What can you do?"}'
```

### Project Structure

```
PinkyBot/
├── src/
│   ├── pinky_daemon/       # Core: API server, broker, scheduler, sessions
│   ├── pinky_memory/       # MCP: long-term memory with vector search
│   ├── pinky_self/         # MCP: agent self-management (schedules, tasks, research)
│   ├── pinky_messaging/    # MCP: outbound messaging through broker
│   ├── pinky_outreach/     # MCP: direct platform adapters (Telegram, Discord, Slack)
│   └── pinky_cli/          # CLI: init, serve, connect, run
├── frontend-svelte/        # Svelte web UI (dashboard, agents, settings, chat)
├── frontend-dist/          # Built frontend (served by the daemon)
├── tests/                  # pytest suite
├── docs/                   # Architecture docs and specs
└── data/                   # SQLite databases (created at runtime)
```

### MCP Servers

Every agent session gets three MCP servers automatically:

| Server | Purpose | Tools |
|--------|---------|-------|
| `pinky-memory` | Long-term memory | `reflect`, `recall`, `forget`, `list_memories` |
| `pinky-self` | Agent lifecycle | schedules, tasks, research, health, context |
| `pinky-messaging` | Outbound messaging | `send_message`, `send_photo`, `send_document`, `add_reaction` |

## Development

```bash
# Install dev dependencies
pip install -e ".[all,dev]"

# Run tests
pytest

# Run with auto-reload (development)
uvicorn pinky_daemon.api:app --reload --port 8888

# Build the frontend (requires Node.js)
cd frontend-svelte && npm install && npm run build
```

## License

MIT

## Credits

Built by [Brad Brockman](https://github.com/bradbrok) and Oleg.
