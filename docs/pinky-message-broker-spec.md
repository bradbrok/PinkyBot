# Pinky Message Broker — Spec

## Overview

Pinky daemon becomes the single message broker for all agent <-> platform communication. Agents don't need their own outreach MCP. Instead:

- **Inbound**: TG/Discord message → Pinky routes to agent session as regular user input
- **Outbound**: Agent session output → Pinky routes back to TG/Discord as reply
- **Approval**: Unknown senders auto-added as "pending" in approved_users, shown in UI for approve/deny
- **Groups**: List all TG group chats the bot is in, with aliases

## Current State

- `TelegramPoller` does long-polling via `getUpdates`
- `MessageHandler` routes messages to Claude sessions
- `TelegramAdapter` sends/receives via Bot API (raw httpx, no framework)
- `approved_users` table exists with status: approved/denied/pending
- Sessions support `send(content)` → returns assistant response
- Per-agent bot tokens stored in `agent_tokens` table

## Architecture Change

### Before (current)
```
TG User → Poller → MessageHandler → Claude (with outreach MCP)
                                       ↓
                              Agent calls outreach MCP tools
                              to send_message, check_sms, etc.
```

### After (new)
```
TG User → Poller → Pinky Broker
                      ├─ Check approved_users → if unknown, add as "pending", hold message
                      ├─ If approved → POST to agent session as user input
                      ├─ Agent responds (plain text output)
                      └─ Pinky sends response back to TG chat
```

## Components to Build

### 1. Message Broker (new: `src/pinky_daemon/broker.py`)

Core routing logic:

```python
class MessageBroker:
    """Routes platform messages to agent sessions and back."""

    async def handle_inbound(self, platform: str, chat_id: str, sender: str,
                              sender_name: str, content: str, agent_name: str):
        """Handle an incoming message from a platform user."""
        # 1. Check if sender is approved
        status = agents.is_user_approved(agent_name, chat_id)

        if status == "denied":
            return  # Silently drop

        if status is None:  # Unknown user
            agents.approve_user(agent_name, chat_id,
                              display_name=sender_name,
                              approved_by="auto",
                              status="pending")
            # Store message in pending queue
            self._pending_messages.append(...)
            return

        # 2. Route to agent session
        session_id = f"{agent_name}-main"

        # Format as platform-aware user message
        prompt = f"[{platform} | {sender_name} | {chat_id}]\n{content}"

        # 3. Send to session
        response = await session.send(prompt)

        # 4. Route response back to platform
        if response.content:
            adapter.send_message(chat_id, response.content)

    async def handle_approval(self, agent_name: str, chat_id: str):
        """When a pending user is approved, deliver held messages."""
        held = self._get_pending(agent_name, chat_id)
        for msg in held:
            await self.handle_inbound(...)
```

### 2. Pending User Queue (DB table: `pending_messages`)

```sql
CREATE TABLE IF NOT EXISTS pending_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE
);
```

### 3. API Endpoints

```
GET  /agents/{name}/pending-messages     — List pending messages for unapproved users
POST /agents/{name}/approved-users       — Approve user (existing, triggers delivery of held messages)
GET  /agents/{name}/group-chats          — List TG group chats the bot is in
PUT  /agents/{name}/group-chats/{id}     — Set alias for a group chat
```

### 4. Group Chat Discovery

Use TG Bot API `getUpdates` to track group chats the bot has been added to. Store in new table:

```sql
CREATE TABLE IF NOT EXISTS group_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'telegram',
    chat_id TEXT NOT NULL,
    chat_title TEXT NOT NULL DEFAULT '',
    alias TEXT NOT NULL DEFAULT '',
    chat_type TEXT NOT NULL DEFAULT 'group',
    member_count INTEGER NOT NULL DEFAULT 0,
    joined_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (agent_name) REFERENCES agents(name) ON DELETE CASCADE,
    UNIQUE(agent_name, chat_id)
);
```

Track when bot is added to/removed from groups via `my_chat_member` updates in the poller.

### 5. Svelte UI Updates

**Agent Detail Panel — new sections:**

**Pending Approvals** (between Approved Users and Heart Files):
- Show count badge on section header
- List pending users with their held message preview
- Approve / Deny buttons (approve triggers message delivery)
- Auto-refresh every 10s to catch new pending users

**Group Chats** (after Approved Users):
- List all groups the bot is in
- Show chat title, alias (editable), member count
- Active/inactive toggle
- "Leave" button

**Approved Users** (update existing):
- Add "pending" status badge styling (yellow)
- Show pending count in section header

### 6. Poller Updates (`pollers.py`)

Update `TelegramPoller` to:
- Pass messages through broker instead of directly to MessageHandler
- Track `my_chat_member` updates for group join/leave events
- Store group chat metadata on discovery

### 7. Session Input Format

When Pinky routes a TG message to a session, format as:

```
[telegram | sender_name | chat_id]
message content here
```

This gives the agent platform context without needing an MCP tool. The agent just responds with plain text, and Pinky sends it back to the same chat.

### 8. What Agents Lose (and Gain)

**Lose:**
- Direct outreach MCP control (send_message, check_sms, etc.)
- Ability to proactively message users (unless Pinky exposes an API)

**Gain:**
- Zero setup for messaging — just approve users and go
- No MCP server dependency for basic chat
- Simpler agent architecture
- Pinky handles all platform quirks (markdown formatting, media, etc.)

**Keep (optional):**
- Agents that need proactive messaging can still use outreach MCP
- This change is about the default path, not removing capabilities

**Security gains:**
- Block exfiltration at source — agent can't send_message to unapproved channels since it doesn't have outreach MCP
- Pinky controls who gets messages, period. Security by architecture, not by prompt
- System messages (errors, warnings, "agent is busy") sent to users without burning agent context
- Approved user checks happen before the agent ever sees the message

## Implementation Order

1. `broker.py` — Core routing logic + pending message queue
2. `pending_messages` table in agent_registry
3. `group_chats` table in agent_registry
4. Update `pollers.py` to route through broker
5. API endpoints for pending messages + group chats
6. Svelte UI: pending approvals section
7. Svelte UI: group chats section
8. Test end-to-end with Barsik

## Files to Create/Modify

| File | Action |
|------|--------|
| `src/pinky_daemon/broker.py` | **CREATE** — Message broker |
| `src/pinky_daemon/agent_registry.py` | MODIFY — Add tables + CRUD |
| `src/pinky_daemon/pollers.py` | MODIFY — Route through broker |
| `src/pinky_daemon/api.py` | MODIFY — Add endpoints |
| `frontend-svelte/src/pages/Agents.svelte` | MODIFY — Add UI sections |
