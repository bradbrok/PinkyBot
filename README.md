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
        +-- pinky-outreach  (telegram, discord, slack)
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

## Development

```bash
# Clone
git clone https://github.com/bradbrok/PinkyBot.git
cd PinkyBot

# Install in dev mode
pip install -e ".[dev]"

# Run tests
pytest

# Run API server
python -m pinky_daemon --mode api --port 8888
```

## License

MIT

## Credits

Built by [Brad Brockman](https://github.com/bradbrok) and Oleg.
