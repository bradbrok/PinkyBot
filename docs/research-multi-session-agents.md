# Research: Multi-Session Agent Architectures & Channel Routing

**Date:** 2026-03-29
**Author:** Misha ʕ •ᴥ•ʔ
**Purpose:** Deep research on how existing platforms handle multi-session agents, channel routing, and context management — informing PinkyBot's architecture decisions.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Claude Agent SDK — Sessions & Streaming](#claude-agent-sdk)
3. [OpenAI — Assistants, Responses, and Agents SDK](#openai)
4. [LangGraph — State Management & Checkpointing](#langgraph)
5. [CrewAI — Role-Based Orchestration](#crewai)
6. [AutoGen / AG2 — Conversation Patterns](#autogen--ag2)
7. [Botpress — Channel Integration Architecture](#botpress)
8. [Rasa — Channel Connectors](#rasa)
9. [Voiceflow — Multi-Channel Deployment](#voiceflow)
10. [Dialogflow CX & Microsoft Bot Framework](#dialogflow-cx--microsoft-bot-framework)
11. [Google ADK — Context-Aware Multi-Agent Framework](#google-adk)
12. [Multi-Session Architecture Patterns](#multi-session-architecture-patterns)
13. [Memory Architecture Across Sessions](#memory-architecture-across-sessions)
14. [Gap Analysis & What Makes PinkyBot Unique](#gap-analysis)
15. [Actionable Recommendations for PinkyBot](#recommendations)

---

## Executive Summary

The AI agent platform landscape in 2026 is converging on several patterns but still has significant gaps. Most frameworks solve either multi-agent orchestration (CrewAI, AutoGen) or multi-channel deployment (Botpress, Rasa, Voiceflow), but almost none solve both together. The concept of a single agent maintaining multiple specialized sessions across channels — each with its own context window but sharing a unified memory layer — is largely unaddressed.

Key findings:
- **Claude Agent SDK** now has mature session management (resume, fork, continue) and streaming input mode, making it the strongest foundation for PinkyBot's multi-session architecture.
- **OpenAI** is deprecating Assistants API (August 2026) in favor of Responses API + Conversations API, with their new Agents SDK providing session backends (Redis, SQLite, SQLAlchemy, encrypted).
- **LangGraph** has the most sophisticated state management with thread-based checkpointing + cross-thread Store for shared memory.
- **No platform** natively supports session-per-channel with shared memory — this is PinkyBot's opportunity.

---

## Claude Agent SDK

### Session Management

The Claude Agent SDK (formerly Claude Code SDK) provides three session operations:

| Operation | How It Works | Use Case |
|-----------|-------------|----------|
| **Continue** | Finds most recent session in current directory | Single-conversation apps |
| **Resume** | Takes explicit session ID | Multi-user apps, process restarts |
| **Fork** | Creates new session from copy of original history | Exploring alternatives |

Sessions are stored as JSONL files at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. The path encoding replaces non-alphanumeric characters with `-`.

### ClaudeSDKClient — Multi-Turn Sessions

`ClaudeSDKClient` manages session IDs internally. Each `client.query()` call automatically continues the same session:

```python
async with ClaudeSDKClient(options=options) as client:
    await client.query("Analyze the auth module")
    async for message in client.receive_response():
        print_response(message)

    # Automatically continues same session — full context retained
    await client.query("Now refactor it to use JWT")
    async for message in client.receive_response():
        print_response(message)
```

### Streaming Input Mode

The recommended mode. Key capabilities:
- **Persistent interactive session** — long-lived process that accepts user input
- **Message queueing** — send multiple messages that process sequentially
- **Interrupts** — cancel mid-execution
- **Image uploads** — attach images directly to messages
- **Hooks** — lifecycle callbacks (PreToolUse, PostToolUse, Stop, SessionStart, SessionEnd)

Uses an AsyncGenerator pattern for streaming input:

```python
async def message_generator():
    yield {"type": "user", "message": {"role": "user", "content": "First message"}}
    await asyncio.sleep(2)
    yield {"type": "user", "message": {"role": "user", "content": "Follow-up"}}

async with ClaudeSDKClient(options) as client:
    await client.query(message_generator())
    async for message in client.receive_response():
        handle(message)
```

### Multiple Instances

The SDK doesn't explicitly document running multiple `ClaudeSDKClient` instances, but the architecture supports it:
- Each instance gets its own session ID
- Sessions are file-based, keyed by directory + session ID
- No shared process state between instances
- Multiple instances can run concurrently in the same process

**Implication for PinkyBot:** We can run one `ClaudeSDKClient` per agent, or potentially multiple per agent for session specialization. The constraint is that each instance spawns a Claude Code subprocess, so resource usage scales linearly.

### Session Lifecycle Functions

Both Python and TypeScript expose:
- `list_sessions()` — enumerate sessions on disk
- `get_session_messages()` — read session transcript
- `get_session_info()` — session metadata
- `rename_session()` — human-readable titles
- `tag_session()` — organize by tag

### Cross-Host Resume

Sessions are local to the machine. Two options for distributed:
1. Move the `.jsonl` session file to the new host (same `cwd` required)
2. Capture results as application state, pass into fresh session prompt

### Subagents

The SDK supports spawning specialized subagents with scoped tools:

```python
agents={
    "code-reviewer": AgentDefinition(
        description="Expert code reviewer",
        prompt="Analyze code quality and suggest improvements.",
        tools=["Read", "Glob", "Grep"],
    )
}
```

Subagent messages include `parent_tool_use_id` for tracking.

---

## OpenAI

### Assistants API (Deprecated August 2026)

The Assistants API used Threads as persistent conversation containers and Runs as execution steps. Key design:
- Thread = conversation session between Assistant and user
- Messages = individual turns within a thread
- Run = async execution of the assistant against a thread
- Thread persisted server-side by OpenAI

### Responses API + Conversations API (Current)

The replacement architecture:
- **Responses API** — stateful-by-default, multimodal from the ground up
- **Conversations API** — creates and manages long-running conversations
- `previous_response_id` chains responses together without explicit thread management
- 40-80% better cache utilization vs Chat Completions

### OpenAI Agents SDK

The Python SDK provides mature session management with multiple backends:

| Backend | Description |
|---------|-------------|
| `SQLiteSession` | Local dev, in-memory or file-based |
| `AsyncSQLiteSession` | Async SQLite via aiosqlite |
| `RedisSession` | Distributed, low-latency shared memory |
| `SQLAlchemySession` | Production with existing DB infrastructure |
| `DaprSession` | Cloud-native with Dapr sidecars |
| `OpenAIConversationsSession` | Server-managed via Conversations API |
| `EncryptedSession` | Wraps any backend with encryption + TTL |

Key patterns:
- **Separate threads** via distinct session IDs: `SQLiteSession("user_123", "conversations.db")`
- **Shared sessions** across agents: both agents see same conversation history
- **History limiting**: `SessionSettings(limit=N)` for recent-only retrieval
- **Compaction**: `OpenAIResponsesCompactionSession` auto-compacts stored history
- Sessions cannot coexist with `conversation_id` or `previous_response_id` — clear separation between SDK-managed and API-managed history

### Multi-Channel

OpenAI doesn't provide native multi-channel deployment. Channel routing is entirely the developer's responsibility. Third-party platforms like ChatMaxima handle multi-channel deployment on top of the API.

---

## LangGraph

### Checkpointing Architecture

LangGraph automatically saves graph state as checkpoints at each super-step. Checkpoints enable:
- Human-in-the-loop workflows
- Conversational memory
- Time travel debugging
- Fault-tolerant execution

### Thread-Based State Management

Each checkpoint associates with a `thread_id`. The `StateSnapshot` contains:
- `values` — current channel state
- `next` — nodes scheduled for execution
- `config` — thread ID, checkpoint namespace, checkpoint ID
- `metadata` — execution source, node writes, super-step counter
- `parent_config` — previous checkpoint reference

Checkpoint namespacing uses `checkpoint_ns`: empty string = root graph, `"node_name:uuid"` = subgraph.

### Cross-Thread Memory: The Store Interface

This is LangGraph's most important contribution to multi-session architecture. While checkpointers handle within-thread persistence, the `Store` interface enables cross-thread information sharing:

```python
# Compile graph with both checkpointer and store
builder.compile(checkpointer=InMemorySaver(), store=store)

# Nodes access store via injected runtime context
await runtime.store.aput(("user_123", "memories"), "preference_1", {"likes": "dark mode"})
results = await runtime.store.asearch(("user_123", "memories"), query="preferences", limit=3)
```

Store features:
- **Namespace-based organization** — tuple keys like `(user_id, "memories")`
- **Semantic search** — embedding-based retrieval when configured
- **Item metadata** — `created_at`, `updated_at` timestamps
- **Separate from thread state** — persists across all threads

### Checkpointer Backends

| Backend | Use Case |
|---------|----------|
| `InMemorySaver` | Development (data lost on restart) |
| `SqliteSaver` | Single-server production |
| `PostgresSaver` | Distributed production |
| `CosmosDBSaver` | Azure production |

All implement `BaseCheckpointSaver` with sync and async variants. Optional `EncryptedSerializer` using AES keys.

### Relevance to PinkyBot

LangGraph's separation of **thread state** (per-conversation checkpoints) from **cross-thread memory** (Store) directly maps to PinkyBot's need for per-channel sessions with shared agent memory. The pattern is:
- Each channel gets its own thread_id (session isolation)
- User preferences, learned facts, and agent personality persist in Store (shared memory)

---

## CrewAI

### Architecture Model

CrewAI uses four primitives: **Agents**, **Tasks**, **Tools**, and **Crew**. The orchestration model is role-based rather than graph-based.

### Orchestration Patterns

- **Sequential**: Tasks execute in order, output feeds next task
- **Parallel**: Independent tasks run concurrently
- **Hierarchical**: Senior agents can override junior agent decisions
- **Conditional**: Dynamic routing based on task outcomes

### Memory System

CrewAI implements four memory types:
1. **Short-term memory** — within-crew execution context
2. **Long-term memory** — persists across crew executions
3. **Entity memory** — structured knowledge about entities
4. **Contextual memory** — task-specific context injection

Memory uses embeddings for semantic retrieval, enabling agents to "learn from past executions."

### Planning Agent

CrewAI can inject a planning agent that creates step-by-step plans before execution, refining them iteratively.

### Relevance to PinkyBot

CrewAI's role-based model maps well to PinkyBot's agent concept but doesn't address multi-channel or multi-session patterns. Its memory system is crew-scoped, not session-scoped. However, the planning agent concept could inform PinkyBot's approach to complex multi-step tasks across channels.

---

## AutoGen / AG2

### Architecture Evolution

AutoGen evolved into AG2 (November 2024), rearchitected with:
- Event-driven core
- Async-first execution
- Pluggable orchestration strategies

### Conversation Patterns

| Pattern | Description |
|---------|-------------|
| **Two-agent chat** | Direct back-and-forth between agents |
| **Sequential chat** | Ordered multi-agent pipeline |
| **Group chat** | Multiple agents in shared conversation with speaker selection |
| **Nested chat** | Agents spawn sub-conversations |
| **Swarm** | Dynamic agent routing based on context |

### GroupChat

The primary coordination pattern:
- Multiple agents in a shared conversation
- A selector determines who speaks next
- Natural for tasks like code review (writer + reviewer), content generation (writer + editor + fact-checker)
- Conversation continues until a termination condition is met

### State Management

AutoGen/AG2 has **no native session management**. Conversation history must be manually maintained. The framework focuses on conversational patterns rather than structured state persistence.

### Token Efficiency

AutoGen uses approximately 8,000 tokens for tasks that LangGraph handles in ~2,000 and CrewAI in ~3,500, due to conversational overhead.

### Relevance to PinkyBot

AG2's GroupChat pattern is interesting for multi-agent scenarios (multiple PinkyBot agents coordinating), but its lack of session management makes it unsuitable as a session layer. The swarm pattern could inform dynamic agent routing.

---

## Botpress

### Channel Architecture

Botpress deploys across 10+ channels (website, WhatsApp, Telegram, Slack, Teams, social platforms). Key design principles:

- **Build once, deploy everywhere** — single bot logic reused across channels
- **Prebuilt connectors** handle channel-specific formatting and events
- **Channel abstraction** — `channels` object in integration config, each key = supported channel
- **Message types** per channel (text, images, cards, etc.)

### Agent Router

Introduced in 2025, the Agent Router enables:
- Multiple AI agents communicating with each other
- Task assignment between agents
- Proactive help-seeking when agents get stuck
- LLM-powered routing with state management

### Shared Memory Layer

Agent orchestration uses a central controller with a shared memory layer:
- Memory moves between agents but is scoped
- No single agent can undo another's work
- Write access controlled per agent

### Integration Architecture

```
Channel (Telegram/Slack/etc.)
  → Webhook receives message
  → Integration layer normalizes to internal format
  → Bot logic processes
  → Response normalized to channel-specific format
  → Sent back via channel API
```

Each integration requires:
- `channels` definition with message types
- Webhook URL configuration
- Authentication (e.g., `x-bp-secret` header)

### Relevance to PinkyBot

Botpress's channel abstraction is the closest to PinkyBot's current multi-channel spec. The key insight: normalize inbound messages to a common format, process with channel-agnostic logic, then format outbound for each platform. PinkyBot already does this with the `[platform | type | sender | chat_id | timestamp]` format. Botpress adds the Agent Router concept which PinkyBot could adopt for multi-agent coordination.

---

## Rasa

### InputChannel / OutputChannel Pattern

Rasa's channel architecture uses a clean separation:

```
User → InputChannel (receives) → Rasa Core (processes) → OutputChannel (sends) → User
```

**InputChannel**: Subclass `rasa.core.channels.channel.InputChannel`, implement `blueprint()` and `name()` methods. The `name()` defines the URL prefix for the webhook.

**OutputChannel**: Either implement custom output methods (send text, images, etc.) or use `CollectingOutputChannel` to batch responses.

### Multi-Channel Configuration

```yaml
# credentials.yml
telegram:
  access_token: "bot_token"
  verify: "bot_name"
  webhook_url: "https://..."

slack:
  slack_token: "xoxb-..."
  slack_channel: "#general"
  slack_signing_secret: "..."
```

Same trained model serves all channels simultaneously. Each channel has its own webhook endpoint.

### Custom Connector Pattern

```python
class MyChannel(InputChannel):
    @classmethod
    def name(cls) -> Text:
        return "mychannel"

    def blueprint(self, on_new_message: Callable) -> Blueprint:
        webhook = Blueprint("mychannel_webhook", __name__)

        @webhook.route("/", methods=["POST"])
        async def receive(request):
            payload = request.json
            await on_new_message(
                UserMessage(
                    text=payload["text"],
                    output_channel=MyOutputChannel(),
                    sender_id=payload["sender"],
                )
            )
            return response.text("")

        return webhook
```

### Relevance to PinkyBot

Rasa's InputChannel/OutputChannel separation is clean and battle-tested. PinkyBot's adapter pattern (TelegramAdapter, DiscordAdapter, SlackAdapter) follows the same principle. The key difference: Rasa processes one message at a time per channel, while PinkyBot's streaming session model handles multiple channels feeding into one continuous context.

---

## Voiceflow

### V4 Architecture (2026)

Ground-up rearchitecture with:
- **Context Engine** — layered context components
- **Playbooks** — autonomous conversation flows
- **Workflows** — deterministic process flows
- **Tool integrations** — API calls, JavaScript, MCP servers, Salesforce, Zendesk, Shopify

### Multi-Channel Deployment

- Build once, deploy to web, chat, voice, WhatsApp
- Single control layer and knowledge base across channels
- Hosting options: SaaS, VPC, on-premise

### Enterprise Features

- Versioning and governance
- Analytics across channels
- Central hub for all conversational AI projects

### Relevance to PinkyBot

Voiceflow's "build once, deploy everywhere" philosophy aligns with PinkyBot's design. The Context Engine concept — layered context components — is worth studying for PinkyBot's context management across sessions. Voiceflow's approach to separating playbooks (autonomous) from workflows (deterministic) maps to PinkyBot's distinction between agent conversation sessions and structured task execution.

---

## Dialogflow CX & Microsoft Bot Framework

### Dialogflow CX

- **Agent** — top-level container housing flows, pages, routes
- **State machine model** — explicit states and transitions for multi-turn management
- **Mega agents** — router that connects specialized sub-agents
- **Native multichannel** — direct integration with major platforms
- Single agent handles customer support, order tracking, feedback collection under one umbrella

### Microsoft Bot Framework

- **Modular architecture** — channel adapters for Teams, Slack, Facebook Messenger
- **Activity-based** — all interactions are "activities" (messages, typing, reactions)
- **Turn context** — each inbound activity creates a turn with full context
- **Bot Framework Emulator** — local testing across channels

### Relevance to PinkyBot

Dialogflow's mega-agent/router pattern (route to specialized sub-agents) could inform PinkyBot's approach when an agent needs different "modes" across channels. The Bot Framework's activity model (everything is an activity with context) is a clean abstraction that PinkyBot could adopt.

---

## Google ADK

### Context Architecture Thesis

ADK treats **context as a compiled view over a richer stateful system**, not a mutable string buffer. Three layers:

1. **Working Context** — ephemeral, per-call view sent to the LLM
2. **Session** — durable log of structured Event objects (messages, tool calls, errors)
3. **Memory & Artifacts** — long-lived, searchable knowledge and externalized data

### Session as Event Log

Sessions store chronological Event records:
- User messages, agent replies, tool calls, errors
- State scratchpad for structured variables
- Model-agnostic storage (decoupled from prompt format)
- Supports time-travel debugging

### Memory Service

Two retrieval patterns:
1. **Reactive recall** — agents explicitly call tools when they recognize knowledge gaps
2. **Proactive recall** — pre-processors run similarity searches on latest input, inject relevant snippets before model invocation

### Multi-Agent Context Sharing

Two interaction patterns:

**Agents as Tools**: Specialized agents get focused prompts with minimal context — just specific instructions and necessary artifacts, suppressing ancestral history.

**Agent Transfer (Hierarchy)**: Full control handoff with scoped access. Crucially, ADK performs **active translation during handoff** to reframe conversations, preventing agents from misattributing prior assistant messages to themselves.

### Production Patterns

| Pattern | Description |
|---------|-------------|
| **Context Compaction** | Async LLM-driven summarization of older events within sliding windows |
| **Context Caching** | Stable prefix (system instructions, summaries) + variable suffix (latest turns) for attention optimization |
| **Artifact Externalization** | Large payloads stored separately with lightweight references, loaded on demand |
| **Pipeline Processing** | Ordered processor lists compile context deterministically |

### Relevance to PinkyBot

ADK's architecture is the most sophisticated for multi-session context management. Key insights for PinkyBot:
1. **Context as compiled view** — don't treat the session as a growing string buffer; compile it from richer state
2. **Active translation during handoff** — when sharing context between sessions, reframe it so the receiving session doesn't get confused
3. **Proactive recall** — automatically inject relevant memory before the agent sees a message, rather than waiting for the agent to ask
4. **Artifact externalization** — large attachments (images, files) should be references, not inline context

---

## Multi-Session Architecture Patterns

### Pattern 1: Session-Per-Channel

Each communication channel gets its own isolated session.

```
Agent "Barsik"
├── Session: Telegram DM (Brad)     — full conversation history with Brad
├── Session: Telegram Group (Team)  — group chat context
├── Session: Discord (#general)     — Discord-specific context
└── Session: Slack (#dev)           — Slack-specific context
```

**Pros:**
- Clean isolation — group chat banter doesn't pollute DM context
- Channel-appropriate behavior (formal in Slack, casual in Telegram)
- Independent context windows — one channel filling up doesn't affect others

**Cons:**
- Agent doesn't know what it said in other channels
- Context fragmentation — agent might answer the same question differently
- More resource usage (multiple Claude processes)

**Who does this:** No one fully. LangGraph's thread model is closest. OpenAI Agents SDK sessions support it mechanically but don't prescribe the pattern.

### Pattern 2: Session-Per-User

Each user gets their own session regardless of channel.

```
Agent "Barsik"
├── Session: Brad (via TG, Discord, Slack)
├── Session: Matt (via Discord, Slack)
└── Session: Yulia (via TG)
```

**Pros:**
- Consistent experience per user across channels
- User preferences and context persist regardless of platform

**Cons:**
- Group chat is awkward — which user's session handles it?
- Loses channel-specific context (tone, formatting)

**Who does this:** Oracle Generative AI Agents uses session-per-user. OpenAI Conversations API effectively does this.

### Pattern 3: Shared Session (PinkyBot Current)

All channels feed into one streaming session.

```
Agent "Barsik"
└── Single StreamingSession
    ├── receives from: TG DMs, TG Groups, Discord, Slack
    └── routes responses back to: originating channel
```

**Pros:**
- Agent has full cross-channel awareness
- Simplest architecture
- Single context = consistent behavior

**Cons:**
- Context window fills fast with multi-channel traffic
- Group chat noise drowns out DM context
- Can't specialize session for different purposes

**Who does this:** PinkyBot (current spec). Most chatbot platforms effectively do this at the bot logic level, then fan out to channels.

### Pattern 4: Hybrid — Shared Memory + Specialized Sessions

The most sophisticated pattern. Each channel gets its own session, but they share a memory layer.

```
Agent "Barsik"
├── Session: TG DM (Brad)        ──┐
├── Session: TG Group (Team)     ──┤── Shared Memory Store
├── Session: Discord (#general)  ──┤   (preferences, facts, decisions)
└── Session: Slack (#dev)        ──┘
```

**Pros:**
- Best of both worlds — session isolation with shared knowledge
- Agent remembers cross-channel context without the noise
- Channel-appropriate behavior with consistent core knowledge
- Independent context lifetimes

**Cons:**
- Most complex to implement
- Memory synchronization challenges
- Write conflicts when multiple sessions update shared memory

**Who does this:** LangGraph's thread + Store pattern. Google ADK's session + memory architecture. No one does it specifically for channel routing.

### Pattern 5: Session Specialization

Different sessions for different purposes, not just channels.

```
Agent "Barsik"
├── Session: Chat (handles all DMs and group messages)
├── Session: Code Review (handles PR review requests)
├── Session: Research (handles deep research tasks)
└── Session: Ops (handles monitoring, alerts, deployments)
```

**Pros:**
- Each session has tools and prompts optimized for its purpose
- Code review session gets code tools; chat session gets messaging tools
- Context stays focused

**Cons:**
- Routing complexity — which session handles "deploy the fix from PR #5"?
- Cross-session coordination needed

**Who does this:** VS Code Copilot Chat implements this with agent-specific sessions. Claude Agent SDK's subagent pattern partially addresses this.

---

## Memory Architecture Across Sessions

### Three-Tier Memory Model (Emerging Standard)

Most frameworks are converging on a three-tier model:

| Tier | Scope | Persistence | Examples |
|------|-------|-------------|----------|
| **Working Memory** | Single session/turn | Ephemeral | Context window, scratchpad |
| **Session Memory** | Single conversation thread | Session lifetime | Chat history, tool results |
| **Long-Term Memory** | Cross-session, cross-channel | Indefinite | User preferences, learned facts, entity knowledge |

### Implementation Approaches

**LangGraph**: Thread checkpoints (session) + Store (long-term). Store supports semantic search via embeddings.

**CrewAI**: Short-term (execution) + Long-term (cross-execution) + Entity + Contextual. Embedding-based retrieval.

**OpenAI Agents SDK**: Session backends (SQLite/Redis/Postgres) + compaction. No explicit long-term memory layer.

**Google ADK**: Event-based session log + Memory service with reactive/proactive recall + Artifact externalization.

**File-Based (PinkyBot/Claude Code)**: MEMORY.md (long-term) + memory/*.md topic files (structured knowledge) + session transcripts (session memory). Human-auditable, grep-searchable.

### Memory Synthesis Pattern

Raw session events are periodically synthesized into long-term knowledge:
1. Daily files capture raw events
2. Synthesis process extracts patterns, decisions, preferences
3. Long-term memory updated with synthesized insights
4. Old raw events can be pruned

This is the pattern PinkyBot's hybrid memory system already uses (CLAUDE.md + memory/*.md + reflect/recall MCP).

---

## Gap Analysis

### What Existing Platforms Are Missing

1. **No platform natively combines multi-session + multi-channel + shared memory.** LangGraph has the primitives (threads + Store) but doesn't prescribe channel routing. Botpress has channel routing but uses a single bot logic layer, not multiple sessions. Rasa has channel connectors but no cross-channel memory.

2. **Session specialization is ad-hoc.** VS Code does it for IDE contexts, but there's no general framework for "this session handles code, that one handles chat, they share memory."

3. **Channel-aware context compilation is absent.** No platform formats inbound messages with channel metadata in a way that the LLM can reason about routing. PinkyBot's `[platform | type | sender | chat_id | timestamp]` format is novel.

4. **Response routing is primitive.** Most platforms assume one input = one output to the same channel. Multi-channel response routing (agent decides which channel to respond in) is not addressed.

5. **Context lifecycle management is weak.** When a session fills up, most platforms either truncate or fail. Few offer graceful context compaction, session forking, or session retirement with memory preservation.

6. **No open-source framework combines Claude Agent SDK sessions with multi-channel routing.** This is PinkyBot's exact value proposition.

### What Would Make PinkyBot Unique

1. **Channel-tagged streaming context.** Messages from all channels flow into the session with rich metadata. The agent sees the full picture and reasons about cross-channel interactions.

2. **Hybrid session model.** Start with shared session (simple), graduate to session-per-channel with shared memory (sophisticated) as traffic grows. The framework supports both without architecture changes.

3. **Session lifecycle management.** Create sessions, persist them, resume them, fork them for exploration, retire them when context fills up — all while preserving knowledge to long-term memory.

4. **Built on Claude Agent SDK.** Full access to streaming input, hooks, subagents, and MCP servers. No other open-source framework wraps the Claude Agent SDK for multi-channel deployment.

5. **Human-auditable memory.** Markdown-based memory files that humans can read, edit, and curate — unlike embedding-only systems that are opaque.

### Interesting Patterns from Open Source

- **Google's Always On Memory Agent** — ditches vector databases for LLM-driven persistent memory. Ingests information continuously, consolidates in background, retrieves later. Worth studying for PinkyBot's memory synthesis.

- **ADK's active translation during handoff** — when passing context between sessions/agents, reframe the conversation so the receiving agent doesn't get confused by another agent's assistant messages.

- **LangGraph's Store with semantic search** — embedding-based retrieval over structured memory namespaces. Could complement PinkyBot's file-based memory with vector search.

- **OpenAI's EncryptedSession** — transparent encryption + TTL for session data. Important for PinkyBot as an open-source framework handling user conversations.

---

## Recommendations

### Architecture Evolution for PinkyBot

#### Phase 1: Current (Shared Session)
Keep the single StreamingSession per agent. All channels feed into one session. This is simple, works, and gives the agent full cross-channel awareness.

**When to graduate:** When context window usage regularly exceeds 50% due to multi-channel noise, or when channel-specific behavior divergence is needed.

#### Phase 2: Session-Per-Channel with Shared Memory
Each channel gets its own `ClaudeSDKClient` session. A shared memory layer (memory/*.md files + semantic search) provides cross-session knowledge.

```
Agent
├── ChannelSession("telegram_dm_brad", tools=[...])
├── ChannelSession("telegram_group_team", tools=[...])
├── ChannelSession("discord_general", tools=[...])
└── SharedMemoryStore
    ├── user_preferences/
    ├── decisions/
    ├── entity_knowledge/
    └── cross_channel_context/
```

Key design decisions:
- **Memory write policy:** Only the session that learned something writes to shared memory. Read is universal.
- **Context compilation:** Each session compiles its working context from shared memory + channel-specific history.
- **Session lifecycle:** Auto-retire sessions when context exceeds threshold. Preserve key context to shared memory before retirement.

#### Phase 3: Session Specialization
Add purpose-specific sessions alongside channel sessions:

```
Agent
├── ChatSession (handles casual conversation across channels)
├── TaskSession (handles structured tasks with full tool access)
├── ReviewSession (handles code review with read-only tools)
└── SharedMemoryStore
```

Routing logic determines which session handles each inbound message based on content analysis.

### Concrete Implementation Suggestions

1. **Add session tagging to PinkyBot.** Use Claude Agent SDK's `tag_session()` to categorize sessions by channel and purpose. This enables session discovery and management.

2. **Implement a MemoryStore interface.** Abstract the shared memory layer so it can be backed by files (current), SQLite, or eventually vector search. Interface should support:
   - `put(namespace, key, value)`
   - `get(namespace, key)`
   - `search(namespace, query, limit)`

3. **Add context compilation.** Before each agent turn, compile the working context from:
   - Session history (channel-specific)
   - Relevant shared memory (proactive recall)
   - Agent identity (CLAUDE.md / soul)
   - Channel-specific instructions (tone, format, permissions)

4. **Implement session retirement.** When context usage exceeds a threshold:
   - Summarize session highlights
   - Write summary to shared memory
   - Fork or create new session with summary as initial context
   - Archive old session

5. **Add cross-channel context bridging.** When an agent needs context from another channel's session:
   - Query shared memory first
   - If not found, use `get_session_messages()` on the other session
   - Inject relevant context with active translation (reframe as third-party information)

---

## Sources

### Claude Agent SDK
- [Agent SDK Overview](https://platform.claude.com/docs/en/agent-sdk/overview)
- [Sessions Documentation](https://platform.claude.com/docs/en/agent-sdk/sessions)
- [Streaming Input](https://platform.claude.com/docs/en/agent-sdk/streaming-vs-single-mode)
- [Streaming Output](https://platform.claude.com/docs/en/agent-sdk/streaming-output)
- [GitHub: claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)

### OpenAI
- [Assistants API Deep Dive](https://platform.openai.com/docs/assistants/deep-dive)
- [Assistants Migration Guide](https://platform.openai.com/docs/assistants/migration)
- [Responses API](https://developers.openai.com/blog/responses-api)
- [OpenAI Agents SDK Sessions](https://openai.github.io/openai-agents-python/sessions/)
- [Assistants API Deprecation Guide](https://ragwalla.com/docs/guides/openai-assistants-api-deprecation-2026-migration-guide-wire-compatible-alternatives)

### LangGraph / LangChain
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Mastering LangGraph State Management 2025](https://sparkco.ai/blog/mastering-langgraph-state-management-in-2025)
- [Production Multi-Agent System with LangGraph](https://markaicode.com/langgraph-production-agent/)
- [LangGraph Multi-Agent Orchestration Guide](https://latenode.com/blog/ai-frameworks-technical-infrastructure/langgraph-multi-agent-orchestration/langgraph-multi-agent-orchestration-complete-framework-guide-architecture-analysis-2025)

### CrewAI
- [CrewAI Official](https://crewai.com/)
- [CrewAI Framework 2025 Review](https://latenode.com/blog/ai-frameworks-technical-infrastructure/crewai-framework/crewai-framework-2025-complete-review-of-the-open-source-multi-agent-ai-platform)
- [CrewAI Practical Guide](https://www.digitalocean.com/community/tutorials/crewai-crash-course-role-based-agent-orchestration)

### AutoGen / AG2
- [AutoGen Multi-Agent Conversation](https://microsoft.github.io/autogen/0.2/docs/Use-Cases/agent_chat/)
- [AutoGen Conversation Patterns](https://microsoft.github.io/autogen/0.2/docs/tutorial/conversation-patterns/)
- [AG2 GitHub](https://github.com/ag2ai/ag2)

### Botpress
- [Botpress Channel Integration](https://botpress.com/blog/channel-integration)
- [AI Agent Routing Guide](https://botpress.com/blog/ai-agent-routing)
- [Botpress Channels Documentation](https://botpress.com/docs/developers/concepts/channels/)

### Rasa
- [Rasa Custom Connectors](https://rasa.com/docs/reference/channels/custom-connectors/)
- [Building Multi-Channel Chatbot with Rasa](https://medium.com/datadriveninvestor/building-a-multi-channel-chatbot-with-rasa-bf8d74a938a1)

### Voiceflow
- [Voiceflow V4 Launch](https://www.voiceflow.com/pathways/everything-we-launched-v4)
- [AI Agent Framework Comparison](https://www.voiceflow.com/blog/ai-agent-framework-comparison)

### Google ADK
- [Architecting Context-Aware Multi-Agent Framework](https://developers.googleblog.com/architecting-efficient-context-aware-multi-agent-framework-for-production/)
- [Multi-Agent Patterns in ADK](https://developers.googleblog.com/developers-guide-to-multi-agent-patterns-in-adk/)

### Comparisons & Analysis
- [LangGraph vs CrewAI vs AutoGen 2026 Guide](https://dev.to/pockit_tools/langgraph-vs-crewai-vs-autogen-the-complete-multi-agent-ai-orchestration-guide-for-2026-2d63)
- [Best Multi-Agent Frameworks 2026](https://gurusup.com/blog/best-multi-agent-frameworks-2026)
- [Agentic OS Architecture](https://www.mindstudio.ai/blog/agentic-os-architecture-four-patterns-claude-code)
- [Agent Memory Architecture](https://dev.to/mfs_corp/agent-memory-architecture-how-our-ai-remembers-across-sessions-j8l)
- [AI Agent Design Patterns — Azure](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns)
- [Amazon Bedrock AgentCore Memory](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-memory-building-context-aware-agents/)
