# PinkyBot

Personal AI companion framework powered by Claude Code. Manages persistent AI agents with identity (soul), memory, messaging, and tool access via MCP servers.

## Architecture

- **`src/pinky_daemon/`** — Core daemon: FastAPI API (`api.py`), agent registry, session management, message handling, scheduling, dreams, skills, content scanning
- **`src/pinky_memory/`** — MCP server for agent long-term memory (vector + semantic search)
- **`src/pinky_messaging/`** — MCP server for Telegram/Discord/Slack messaging
- **`src/pinky_outreach/`** — MCP server for proactive outreach tools (send, thread, react, broadcast)
- **`src/pinky_self/`** — MCP server for agent self-awareness (read own config, update soul, manage directives)
- **`src/pinky_calendar/`** — MCP server for Google Calendar / CalDAV
- **`src/pinky_cli/`** — CLI entry point
- **`frontend-svelte/`** — Svelte 5 SPA: agent management, chat, dashboard, memories, tasks, research

## Key Concepts

- **Agent** — Registered entity with soul, boundaries, users, model, permissions, tools, directives, skills
- **Soul** — Agent personality/identity stored in DB `soul` field; assembled with boundaries, directives, skills, owner profile into `CLAUDE.md` via `build_system_prompt()` in `agent_registry.py`
- **CLAUDE.md** — Compiled system prompt written to agent working dir (`data/agents/{name}/CLAUDE.md`); what Claude Code actually reads
- **Streaming Session** — Long-lived Claude Agent SDK session per agent; messages routed through broker
- **Skills** — SKILL.md-based capability plugins assigned per agent; contribute directives and MCP servers
- **Directives** — Priority-ordered instructions injected into system prompt

## Running

```bash
# Daemon (API server)
python -m pinky_daemon --mode api --port 8888

# Frontend dev
cd frontend-svelte && npm run dev  # :5173

# Tests
pytest
```

## Coding Conventions

- Python 3.11+, ruff for linting (line-length 100, select E/F/I/N/W)
- FastAPI with Pydantic request models; all API endpoints in `api.py`
- SQLite everywhere, WAL mode; schema migration via `_ensure_columns()` pattern
- Tests in `tests/`, pytest-asyncio, naming `test_*.py`
- Frontend: Svelte 5 SPA with `svelte-spa-router`, inline styles (no CSS framework), monospace font aesthetic
- Components in `frontend-svelte/src/components/`, pages in `src/pages/`, shared utils in `src/lib/`

## Deploy

- Production on Mac Mini (`oleg@10.0.0.209`)
- Self-update: agents call `update_and_restart()` (pinky-self MCP) — by default it deploys the **latest published GitHub Release tag**, verified against the GitHub Releases API over TLS (must be a published, non-draft/non-prerelease Release whose commit matches the local tag), then `git checkout`s that exact ref (detached HEAD) and restarts. It no longer pulls `main` HEAD, so prod runs exactly the verified release. Pass `target="YY.MM.NNN"` to deploy a specific release, or `target="<sha>"` to pin a commit (operator override — existence-checked only). `force=True` bypasses verification (e.g. GitHub API unreachable). Trunk-based since #450; only `PINKYBOT_CHANNEL=stable` is supported.
- Releases are cut manually via the `Release` workflow (Actions → Release → Run workflow → version `YY.MM.NNN`). Production picks up new releases on the next `update_and_restart()`.
- CI auto-runs on push (`ci.yml`); `main` is branch-protected and requires passing CI + PR review
- Agent data in `data/agents/{name}/` (gitignored)
