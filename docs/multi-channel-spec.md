# Multi-Channel Agent Routing — Spec

## Problem

An agent like Barsik needs to be present in multiple places at once:
- TG DMs (multiple users)
- TG group chats (multiple groups)
- Discord channels
- Slack channels

All feeding into one streaming session. The agent needs to know WHERE each message came from and route responses BACK to the right place.

## Current State

- One TG bot token per agent → one BrokerTelegramPoller
- Messages tagged with `[telegram | sender | chat_id | timestamp | msg_id]`
- Responses go back to the chat_id they came from
- Only Telegram supported, DMs and groups handled the same way

## Design: Unified Channel Router

### Core Concept

Each agent has one streaming session but multiple **channels**. A channel is a specific place the agent can receive/send messages:

```
Channel = (platform, chat_id, channel_type)

Examples:
  ("telegram", "6770805286", "dm")         — Brad's DM
  ("telegram", "-100123456", "group")       — Team group chat
  ("discord", "1234567890", "channel")      — #general on Discord
  ("slack", "C0123ABC", "channel")          — #random on Slack
```

### Message Format (what the agent sees)

```
[telegram | dm | Brad | 6770805286 | 2026-03-30 15:10:05 PT | msg_id:38]
Hey Barsik, what's the status?

[telegram | group | Team Chat | -100123456 | 2026-03-30 15:11:00 PT | msg_id:42]
@barsik can you check PR #5?

[discord | channel | #general | 1234567890 | 2026-03-30 15:12:00 PT]
Barsik, what did we decide about the API?

[slack | channel | #butter-pos | C0123ABC | 2026-03-30 15:13:00 PT]
@barsik deploy status?
```

All in one context. Agent responds naturally. Pinky figures out where to send the reply.

### Response Routing

**Problem:** The agent's response is a single text block. How does Pinky know which channel to send it to?

**Solution: Last-sender routing + explicit channel tags**

1. **Default:** Response goes to the chat_id of the message that triggered it (tracked in `_pending_chats`)
2. **Explicit:** Agent can prefix response with `@channel:chat_id` to target a specific channel
3. **Broadcast:** Agent can prefix with `@all` to send to all active channels

In practice, the streaming session handles this naturally since messages queue with chat_ids. Each inbound message pushes its chat_id to `_pending_chats`, and the reader loop pops it when the response comes.

### Multi-message edge case

If 3 messages come in fast from different channels before agent responds:
```
[telegram | dm | Brad | 6770805286] How's the deploy?
[discord | #general | 1234567890] What's the ETA?
[slack | #butter-pos | C0123ABC] Status update?
```

The agent sees all three and responds once. That response goes to the FIRST pending chat_id (Brad's DM). The other senders don't get a reply unless the agent explicitly addresses them or sends follow-ups.

**Better approach:** Buffer messages for 2-3 seconds, then deliver as one batch. Response goes to ALL senders in the batch.

### Platform Adapters

Each platform needs a poller and a sender:

```
Platform Adapters:
├── TelegramAdapter (existing) — getUpdates polling + sendMessage
├── DiscordAdapter (new) — gateway websocket + send
└── SlackAdapter (new) — events API + postMessage
```

### Agent Token Table (existing)

Already supports multi-platform:
```sql
agent_tokens (agent_name, platform, token, enabled, settings)
-- ("barsik", "telegram", "bot_token...", 1, {})
-- ("barsik", "discord", "bot_token...", 1, {"guild_ids": [...]})
-- ("barsik", "slack", "bot_token...", 1, {"channels": [...]})
```

### Group Chat Behavior

**In groups, agents should NOT respond to every message.** Only respond when:
1. Mentioned by name (`@barsik` or `barsik`)
2. Replied to (the message is a reply to the bot's message)
3. Configured to respond to all (per-group setting)

This is a per-group-chat setting stored in the `group_chats` table:
```sql
-- Add to group_chats:
respond_mode TEXT NOT NULL DEFAULT 'mention'  -- 'mention', 'reply', 'all', 'silent'
```

### Architecture

```
                    ┌─── TG Poller ──────┐
                    │  DMs + Groups       │
                    │                     │
TG Bot Token ──────┤                     ├──→ MessageBroker ──→ StreamingSession
                    │  Group filter:      │         │
                    │  mention/reply only │         │
                    └─────────────────────┘         │
                                                    │
                    ┌─── Discord Poller ──┐         │
Discord Token ─────┤  Gateway WS         ├──→ ─────┘
                    │  Channel filter     │         │
                    └─────────────────────┘         │
                                                    ├──→ TG send (chat_id)
                    ┌─── Slack Poller ────┐         ├──→ Discord send (channel_id)
Slack Token ───────┤  Events API         ├──→ ─────┘──→ Slack send (channel)
                    │  Channel filter     │
                    └─────────────────────┘
```

### Implementation Phases

**Phase 1: Multi-group TG (current tokens, minimal work)**
- Update BrokerTelegramPoller to filter group messages by mention/reply
- Add `respond_mode` to group_chats table
- Update broker prompt format to include channel type (dm/group)
- UI: group chat settings (respond mode per group)

**Phase 2: Discord adapter**
- Build DiscordPoller (gateway websocket via discord.py or raw WS)
- DiscordAdapter for sending (webhook or bot API)
- Plug into broker with same flow as TG

**Phase 3: Slack adapter**
- Build SlackPoller (Socket Mode or Events API)
- SlackAdapter for sending (Web API)
- Plug into broker

**Phase 4: Channel management UI**
- Unified "Channels" section in agent detail
- Shows all connected channels across platforms
- Enable/disable per channel
- Respond mode per channel
- Activity stats per channel

## Files to Create/Modify

### Phase 1
| File | Action |
|------|--------|
| `src/pinky_daemon/pollers.py` | MODIFY — group mention/reply filtering |
| `src/pinky_daemon/agent_registry.py` | MODIFY — respond_mode on group_chats |
| `src/pinky_daemon/broker.py` | MODIFY — channel type in prompt format |
| `frontend-svelte/src/pages/Agents.svelte` | MODIFY — group respond mode UI |

### Phase 2
| File | Action |
|------|--------|
| `src/pinky_outreach/discord.py` | CREATE — Discord adapter |
| `src/pinky_daemon/pollers.py` | MODIFY — add BrokerDiscordPoller |
| `src/pinky_daemon/api.py` | MODIFY — start Discord pollers |

### Phase 3
| File | Action |
|------|--------|
| `src/pinky_outreach/slack.py` | CREATE — Slack adapter |
| `src/pinky_daemon/pollers.py` | MODIFY — add BrokerSlackPoller |
| `src/pinky_daemon/api.py` | MODIFY — start Slack pollers |
