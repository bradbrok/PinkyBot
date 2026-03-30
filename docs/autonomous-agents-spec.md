# Autonomous Agents — Spec

Three features to close the gap to fully autonomous Pinky agents.

## 1. Auto Context Restart at 40%

### Problem
Streaming sessions accumulate context until they hit the CC limit and crash. Agents can manually call `context_restart` but they might forget or not check.

### Solution
After each turn in the streaming session reader loop, check context usage via `get_context_usage()`. At 40%, inject a system message telling the agent to save state and prepare for restart. At 80%, force restart.

### Flow
```
Turn completes → reader loop checks context %
  If >= 40% and < 80%:
    Inject message: "Context at {X}%. Save your current state with save_my_context,
    then call context_restart. You have ~{remaining}K tokens before forced restart."
  If >= 80%:
    Auto-save context from last known state
    Force disconnect + reconnect (fresh session with wake context)
    Log: "Auto-restarted at {X}% context"
```

### Implementation
- **streaming_session.py**: Add `_check_context()` called after each ResultMessage in reader loop
- **streaming_session.py**: Add `auto_restart_threshold` (default 80) and `warn_threshold` (default 40) to config
- **streaming_session.py**: Add `force_restart()` method that saves context + reconnects
- **agent_registry.py**: No changes needed — `save_my_context` already persists to agent_contexts

### Files
| File | Action |
|------|--------|
| `src/pinky_daemon/streaming_session.py` | MODIFY — add context checking + auto-restart |

---

## 2. Inter-Agent Communication via Broker

### Problem
Agents can't talk to each other. `AgentComms` exists (SQLite message store) but it's not wired into streaming sessions. Agents need to message each other through Pinky, same as users message them.

### Solution
Add a `send_to_agent` tool in pinky-self that routes through the broker. The receiving agent gets the message as a regular inbound prompt, formatted with the sender's identity. No new MCP needed — just a new tool + API endpoint.

### Flow
```
Agent A calls send_to_agent(to="barsik", message="Hey, check PR #5")
  → POST /agents/barsik/inbox {from: "agent-a", message: "..."}
  → Broker injects into barsik's streaming session:
    "[agent | agent-a | internal]\nHey, check PR #5"
  → Barsik responds naturally
  → Response stored in AgentComms for Agent A to retrieve
```

### Message Format
```
[agent | sender_name | internal | timestamp]
message content
```

### Implementation
- **api.py**: Add `POST /agents/{name}/inbox` — inject message into streaming session
- **pinky_self/server.py**: Add `send_to_agent(to, message)` tool
- **pinky_self/server.py**: Add `check_inbox()` tool — read messages from AgentComms
- **broker.py**: Add `inject_agent_message()` — route internal messages to streaming session
- **agent_comms.py**: Already exists, use as response store

### Tools for Agents
```python
send_to_agent(to: str, message: str) -> str
    """Send a message to another agent. They'll see it in their context."""

check_inbox() -> str
    """Check for messages from other agents."""

list_agents() -> str
    """List all active agents you can communicate with."""
```

### Files
| File | Action |
|------|--------|
| `src/pinky_daemon/api.py` | MODIFY — add inbox endpoint |
| `src/pinky_daemon/broker.py` | MODIFY — add inject_agent_message() |
| `src/pinky_self/server.py` | MODIFY — add send_to_agent, check_inbox, list_agents tools |

---

## 3. Conversation History Logging for Streaming Sessions

### Problem
Streaming session messages aren't logged to the conversation store. The regular Session class logs to `ConversationStore` on every send/receive, but StreamingSession bypasses this. Chat UI history only shows messages from the regular session.

### Solution
Log both inbound (user prompt) and outbound (agent response) messages from the streaming session to the ConversationStore. This gives:
- Full chat history in the dashboard
- Message search/audit
- Context for analytics (turns, costs, patterns)

### Flow
```
Broker sends prompt to streaming session:
  → Log: ConversationStore.append(session_id, "user", prompt)
Reader loop gets response:
  → Log: ConversationStore.append(session_id, "assistant", response)
```

### Implementation
- **streaming_session.py**: Accept a `conversation_store` parameter
- **streaming_session.py**: Log user messages in `send()`, assistant messages in reader loop
- **api.py**: Pass ConversationStore when creating streaming sessions
- Use session ID format: `{agent_name}-streaming` for the conversation store key

### Files
| File | Action |
|------|--------|
| `src/pinky_daemon/streaming_session.py` | MODIFY — add conversation logging |
| `src/pinky_daemon/api.py` | MODIFY — pass conversation store |

---

## Implementation Order

1. **History logging** (smallest, unblocks dashboard visibility)
2. **Auto context restart** (critical for stability)
3. **Inter-agent comms** (biggest, enables collaboration)

## Total Estimate
- History logging: ~30 lines
- Auto context restart: ~50 lines
- Inter-agent comms: ~100 lines (tools + endpoint + broker method)
