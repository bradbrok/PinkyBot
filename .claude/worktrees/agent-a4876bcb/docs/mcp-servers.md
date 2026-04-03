# MCP Server Reference

Pinky consists of standalone MCP servers that extend Claude Code. Each server runs independently and can be used separately.

## pinky-memory

Long-term semantic memory (Tier 2). SQLite + vector embeddings + BM25 keyword search.

Working memory (Tier 1) is handled by Claude Code's native MEMORY.md / memory/*.md files and does not require an MCP server.

### Running

```bash
# Default (data/memory.db)
python -m pinky_memory

# Custom DB path
python -m pinky_memory --db ./data/memory.db
```

### Claude Code Config

```json
{
  "mcpServers": {
    "pinky-memory": {
      "command": "python",
      "args": ["-m", "pinky_memory", "--db", "./data/memory.db"],
      "env": {}
    }
  }
}
```

### Tools

`reflect`, `recall`, `introspect`, `memory_query`, `memory_links`

See [Memory System](memory-system.md) for details.

## pinky-outreach

Multi-platform messaging. Currently supports Telegram, with Discord and Slack planned.

### Running

```bash
# Requires TELEGRAM_BOT_TOKEN env var
python -m pinky_outreach --token $TELEGRAM_BOT_TOKEN
```

### Claude Code Config

```json
{
  "mcpServers": {
    "pinky-outreach": {
      "command": "python",
      "args": ["-m", "pinky_outreach"],
      "env": {
        "TELEGRAM_BOT_TOKEN": "your-bot-token"
      }
    }
  }
}
```

### Tools

| Tool | Args | Description |
|------|------|-------------|
| `send_message` | content, chat_id, platform, reply_to, parse_mode, silent | Send a text message |
| `check_messages` | platform, timeout, limit | Poll for new inbound messages |
| `send_photo` | chat_id, file_path, caption, platform | Send a photo |
| `send_document` | chat_id, file_path, caption, platform | Send a file |
| `get_chat_info` | chat_id, platform | Get chat metadata |
| `add_reaction` | chat_id, message_id, emoji, platform | React to a message |
| `bot_info` | platform | Get bot identity info |

### Platforms

| Platform | Status | Transport |
|----------|--------|-----------|
| Telegram | Supported | Bot API (httpx) |
| Discord | Planned | discord.py |
| Slack | Planned | slack-sdk |
| iMessage | Planned | AppleScript (macOS) |
| Email | Planned | IMAP/SMTP |

## Using with `pinky` CLI

The CLI handles server lifecycle:

```bash
# Start all configured servers
pinky serve

# Start a specific server
pinky serve --server memory
pinky serve --server outreach

# Register servers with Claude Code
pinky connect
```

## Transport

All servers support two MCP transports:

- **stdio** (default) -- for Claude Code integration
- **sse** -- for HTTP-based clients

Pass `--transport sse` to use HTTP transport.
