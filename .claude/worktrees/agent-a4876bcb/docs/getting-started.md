# Getting Started

## Prerequisites

- Python 3.11+
- [Claude Code](https://claude.ai/code) installed
- A Telegram bot token (optional, for messaging)

## Install

```bash
pip install pinky-ai
```

Or from source:

```bash
git clone https://github.com/bradbrok/PinkyBot.git
cd PinkyBot
pip install -e ".[dev]"
```

## Initialize

```bash
pinky init
```

This creates:
- `CLAUDE.md` -- your AI's personality and boundaries
- `pinky.yaml` -- configuration (API keys, channels, schedules)
- `data/` -- local storage directory

## Configure

### 1. Edit your soul file

Open `CLAUDE.md` and customize your AI's identity:

```markdown
# MyAssistant

## IDENTITY
- **Name:** MyAssistant
- **Vibe:** Helpful, witty, detail-oriented.

## USER
### Your Name
- **Timezone:** America/New_York
- **About:** Software developer, prefers concise communication.
```

See [Writing a Soul File](soul-guide.md) for tips.

### 2. Add API keys

Edit `pinky.yaml`:

```yaml
memory:
  embedding_provider: openai  # requires OPENAI_API_KEY env var

outreach:
  telegram:
    bot_token: ${TELEGRAM_BOT_TOKEN}
```

Set environment variables:

```bash
export OPENAI_API_KEY="sk-..."
export TELEGRAM_BOT_TOKEN="123456:ABC..."
```

## Connect to Claude Code

```bash
pinky connect
```

This writes MCP server configs to `~/.claude/settings.json` so Claude Code can use Pinky's memory and messaging.

## Start

```bash
# Start MCP servers
pinky serve

# In another terminal, start Claude Code
claude
```

Claude Code now has access to long-term memory and Telegram messaging.

## What's next?

- [Memory System](memory-system.md) -- how memory works
- [MCP Server Reference](mcp-servers.md) -- all available tools
- [Soul Guide](soul-guide.md) -- writing a great CLAUDE.md
