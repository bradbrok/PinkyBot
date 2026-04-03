# PinkyBot

**A personal AI companion framework powered by Claude Code.**

PinkyBot turns Claude Code into a persistent AI agent with identity, long-term memory, messaging, scheduling, and a web dashboard — your AI sidekick that runs 24/7, connects to Telegram/Slack/Discord, and gets smarter over time.

## What it does

- **Persistent agents** — Named AI agents with souls, memory, and roles. They wake up on a schedule, respond to messages, and remember everything across sessions.
- **Long-term memory** — Vector + keyword hybrid search. Agents reflect on experiences and recall them semantically across sessions.
- **Multi-platform messaging** — Telegram, Discord, Slack. Send and receive messages, photos, documents, voice notes, reactions.
- **Event-driven triggers** — Agents wake up automatically on webhooks, URL changes, or file changes.
- **Project & task management** — Milestones, sprints, burndown. Agents decompose projects and track progress.
- **Dreaming** — Agents autonomously consolidate memories and process the day while you sleep.
- **Skills** — Modular capability plugins (`SKILL.md`-based) agents can install to learn new things.
- **Web dashboard** — Chat, fleet management, memory browser, activity feed, presentations, project hub.
- **Works with your Claude account** — Uses Claude Code under the hood with your existing Max/Pro subscription. No separate API bills.

## Quick Start

### Prerequisites

- **Python 3.11+**
- **Claude Code** — install with:
  ```bash
  curl -fsSL https://claude.ai/install.sh | bash
  ```
- **A Claude Max or Pro subscription** (or an API key)

### Install

```bash
git clone https://github.com/bradbrok/PinkyBot.git
cd PinkyBot

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[all,dev]"
```

### Start

```bash
python -m pinky_daemon --mode api --port 8888 --host 0.0.0.0
```

Open the dashboard at `http://localhost:8888`.

### Create your first agent

Via the web UI at `http://localhost:8888/#/agents`, or via API:

```bash
# Create an agent
curl -X POST localhost:8888/agents \
  -H 'Content-Type: application/json' \
  -d '{"name": "myagent", "model": "claude-sonnet-4-6", "display_name": "My Agent"}'

# Wake it up
curl -X POST "localhost:8888/agents/myagent/wake?prompt=Hello"

# Chat with it
curl -X POST localhost:8888/agents/myagent/chat \
  -H 'Content-Type: application/json' \
  -d '{"content": "What can you do?"}'
```

### Connect to Telegram (optional)

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Go to Settings at `http://localhost:8888/#/settings`
3. Enter your bot token under Outreach Platforms
4. Assign the bot to your agent

## Architecture

```
PinkyBot Daemon (FastAPI :8888)
├── Agent Registry        — named agents, soul, config, permissions
├── Streaming Sessions    — long-lived Claude Agent SDK sessions per agent
├── Message Broker        — routes inbound messages to the right agent
├── Scheduler             — heartbeats, URL watchers, wake schedules
├── Trigger Store         — webhook, URL, and file event triggers
├── Activity Log          — auditable log of all agent actions
└── MCP Servers (per agent session)
    ├── pinky-memory      — long-term memory (vector + BM25 + salience)
    ├── pinky-self        — agent self-management (tasks, schedules, triggers, health)
    ├── pinky-messaging   — outbound messaging (send, thread, react, broadcast)
    └── pinky-calendar    — Google Calendar / CalDAV (optional)

Frontend (Svelte 5 SPA)
├── Dashboard             — stats, burndown, activity feed
├── Chat                  — per-agent conversation with tool use + thinking display
├── Fleet                 — multi-agent overview, status, comms
├── Agents                — agent config, MCP servers, skills, triggers
├── Memory                — browse and search long-term memory
├── Projects              — project hub, milestones, sprints
└── Presentations         — shareable AI-generated slide decks
```

## Core concepts

### Agents

An agent is a named entity with:
- **Soul** — personality and values in `CLAUDE.md` (you edit this)
- **Model** — which Claude model to use (`claude-sonnet-4-6`, `claude-opus-4-6`, etc.)
- **Tools** — what MCP tools it has access to
- **Users** — who can talk to it and how
- **Skills** — installed capability plugins
- **Triggers** — what events wake it up

### Memory

All persistent memory goes through the `pinky-memory` MCP server:
- `reflect(content)` — store a cross-session learning or fact
- `recall(query)` — semantic search across all memories
- `introspect()` — list your stored memories

Memory persists across context restarts, daemon restarts, and sessions. Agents consolidate memories autonomously during dream runs.

### Triggers

Agents can be woken by external events:

- **Webhook** — POST to a unique URL; agent wakes with the payload
- **URL watcher** — polls a URL on a schedule, wakes on change
- **File watcher** — watches a file path for changes

Agents manage their own triggers via the `pinky-self` MCP.

### Skills

Skills are `SKILL.md` files that add capabilities to agents. Skills can contribute:
- Instructions and domain knowledge
- Additional MCP servers
- Persistent directives

## Project structure

```
PinkyBot/
├── src/
│   ├── pinky_daemon/       # Core: FastAPI, broker, scheduler, sessions, triggers
│   ├── pinky_memory/       # MCP: long-term memory with vector + keyword search
│   ├── pinky_self/         # MCP: schedules, tasks, triggers, health, context
│   ├── pinky_messaging/    # MCP: outbound messaging (send, thread, react, broadcast)
│   ├── pinky_outreach/     # MCP: platform adapters (Telegram, Discord, Slack)
│   ├── pinky_calendar/     # MCP: Google Calendar / CalDAV
│   ├── pinky_hub/          # Hub: cross-instance registry, presentation sync
│   └── pinky_cli/          # CLI: init, serve, run
├── frontend-svelte/        # Svelte 5 SPA source
├── frontend-dist/          # Built frontend (served by daemon)
├── docs/                   # Architecture docs and specs
├── tests/                  # pytest suite
└── data/                   # Runtime data (SQLite, agent files) — gitignored
```

## Development

```bash
pip install -e ".[all,dev]"

# Run tests
pytest

# Frontend dev server
cd frontend-svelte && npm install && npm run dev  # :5173

# Build frontend
cd frontend-svelte && npm run build
```

## License

[AGPL-3.0](LICENSE) — free for personal and open source use. Commercial use requires a license.

## Built by

[Brad Brockman](https://brockmanlabs.com)
