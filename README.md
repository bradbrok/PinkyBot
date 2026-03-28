# Pinky

**A personal AI companion framework powered by Claude Code.**

Pinky gives Claude Code a soul, long-term memory, and the ability to talk to people across messaging platforms -- turning it from a coding tool into a full personal AI sidekick.

## What is this?

Pinky is a set of MCP (Model Context Protocol) servers that extend Claude Code with:

- **Long-term memory** -- Vector + keyword hybrid search, salience decay, automatic consolidation
- **Multi-platform messaging** -- Telegram, Discord, Slack, iMessage, email
- **Google services** -- Calendar and Gmail integration
- **Personality** -- A soul file (CLAUDE.md) that gives your AI its identity

Claude Code handles all the hard stuff (LLM orchestration, context management, tool execution). Pinky handles everything else.

## Quick Start

```bash
# Install
pip install pinky-ai

# Initialize a new project
pinky init

# Edit your soul file
vim CLAUDE.md

# Add API keys to config
vim pinky.yaml

# Start MCP servers
pinky serve

# Connect to Claude Code
pinky connect

# Talk to your AI
claude
```

## Architecture

```
Claude Code (brain)
    |
    +-- CLAUDE.md (soul/personality)
    |
    +-- MCP Servers (capabilities)
        |
        +-- pinky-memory    (long-term memory with vector search)
        +-- pinky-outreach   (telegram, discord, slack, imessage)
        +-- pinky-google     (calendar, gmail)
```

## Components

### Memory (`pinky-memory`)

Reflection-based memory system with:
- **Hybrid search** -- Vector embeddings (OpenAI or local) + BM25 keyword search
- **Salience decay** -- Memories fade over time unless reinforced
- **Consolidation** -- Similar memories auto-merge
- **Promotion** -- Recurring patterns get promoted to insights
- **Spaced review** -- Periodic re-evaluation of stored knowledge

All stored in a single SQLite file. Zero infrastructure.

### Outreach (`pinky-outreach`)

Multi-platform messaging:
- Telegram (bot API)
- Discord (bot)
- Slack (bot)
- iMessage (macOS)
- Email (IMAP/SMTP)

### Google (`pinky-google`)

- Google Calendar (list, create, update, delete events)
- Gmail (read, send, search)

### Soul System (`CLAUDE.md`)

Your AI's personality, values, and boundaries -- all in a single markdown file:
- Identity (name, vibe, role)
- Core values and behavioral principles
- User profiles
- Boundaries and ethics
- Communication channels

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

# Run memory server standalone
python -m pinky_memory
```

## License

MIT

## Credits

Built by [Brad Brockman](https://github.com/bradbrok) and Oleg.
