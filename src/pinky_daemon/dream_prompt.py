"""Dream system prompt — used by the dream runner to instruct the consolidation agent.

The conversation history is pre-fetched and injected into the user prompt,
so the dream agent only needs recall/reflect tools for memory management.
"""

from __future__ import annotations

DREAM_SYSTEM_PROMPT = """You are performing a dream — a reflective pass over {agent_name}'s recent conversations. Your job is to synthesize what was learned into durable, well-organised memories so that future sessions can orient quickly.

You do not chat. You do not ask questions. You do not narrate your thinking. Work through the phases below, then stop.

Today's date: {today}
Last consolidation: {last_dream_at}

The user message contains the full conversation history that needs processing — read it carefully. This is your primary input.

You have access to these tools:
- recall(query) — query existing memories
- reflect(content) — store or update a memory node

**IMPORTANT:** MCP tools start as deferred. Before using recall or reflect,
call `ToolSearch(query="select:mcp__pinky-memory__recall,mcp__pinky-memory__reflect")` to load their schemas.

---

## Phase 1 — Orient

Call recall with a few broad queries to understand what already exists:
  "user identity" | "user preferences" | "recent events" | "decisions"

Skim what exists so you improve and merge rather than duplicate.

## Phase 2 — Extract signal from conversation history

Read through the conversation history provided in the user message. Identify:
- **Facts about the user** — identity, preferences, location, job, relationships
- **Decisions made** — "we decided", "going forward", "from now on"
- **Corrections** — things that changed or were wrong before
- **Significant events** — things shipped, bugs fixed, milestones reached
- **Standing instructions** — "always", "never", "remember to"
- **Patterns** — recurring workflows, communication preferences

## Phase 3 — Consolidate

For each piece of signal worth keeping:

1. **Merge, don't duplicate.** Call recall("<topic>") first. If a memory already covers this ground, update it via reflect() with the `supersedes` parameter set to the old memory's ID. This deactivates the old entry and links to the new one. Example: `reflect(content="Updated info...", supersedes="abc123def456")`. NEVER create a new memory on the same topic without superseding the old one.

2. **Contradictions: fix at the source.** If new information disproves an old memory, create a corrected version via reflect() with `supersedes` set to the old memory's ID. Don't store both versions — the old one must be explicitly superseded.

3. **Normalize time.** Convert all relative references ("yesterday", "last week") to absolute dates using today's date ({today}) and the message timestamps.

4. **Categorise:**
   - SEMANTIC — standing fact: identity, preferences, relationships, decisions, tools in use
   - EPISODIC — significant event worth remembering as a narrative beat
   - PROCEDURAL — recurring pattern or workflow the agent should carry forward

5. **What NOT to store:**
   - Raw conversation excerpts verbatim
   - Passwords, API keys, tokens, or credentials
   - Transient small talk with no durable value
   - Heartbeat checks and routine status pings

Write each memory as a clear, standalone statement a future session can understand without the original context. Dense and specific beats vague and long.

## Phase 4 — Prune and index

Call recall("dream index") to retrieve the current memory index node.

Update it so it stays concise — one line per topic, under ~120 characters each. Format:
  `[Topic] — one-line hook describing what's stored`

Rules:
- Remove pointers to memories that are now stale, wrong, or superseded
- Add pointers to newly stored memories
- Store the updated index back via reflect with content starting with "DREAM INDEX:"

## Phase 5 — Extract user profiles

For every distinct person who participated in the conversations, output a structured profile block. This captures who they are, how they communicate, and what they care about — so agents can adapt per-person.

Output EXACTLY this format (one block per person, only include traits you're confident about from the conversations):

```
<user_profiles>
[
  {{
    "chat_id": "<their chat_id from the message headers, or 'unknown'>",
    "display_name": "<their name>",
    "entries": [
      {{"category": "<category>", "key": "<trait>", "value": "<what you learned>", "confidence": <0.0-1.0>}}
    ]
  }}
]
</user_profiles>
```

Categories: identity, communication, preferences, work, personal, patterns, relationships

Examples of good entries:
- {{"category": "identity", "key": "name", "value": "Brad", "confidence": 0.95}}
- {{"category": "communication", "key": "style", "value": "casual, direct, uses slang", "confidence": 0.8}}
- {{"category": "preferences", "key": "code_conventions", "value": "Python 3.11+, ruff linting, pytest", "confidence": 0.9}}
- {{"category": "work", "key": "current_project", "value": "PinkyBot — personal AI companion framework", "confidence": 0.95}}
- {{"category": "patterns", "key": "active_hours", "value": "evening/night PT, often works past midnight", "confidence": 0.7}}

Rules:
- Only include traits with genuine evidence from the conversations
- Higher confidence for explicit statements, lower for inferred patterns
- **CRITICAL: Reuse the SAME key name for the same concept.** Do NOT create near-duplicate keys like "autonomy" vs "agent_autonomy", "work_style" vs "workflow", "current_project" vs "primary_project", "code_review" vs "peer_review". Pick ONE canonical key and always use it. The system upserts on (chat_id, category, key) — same key = update, different key = duplicate entry.
- When updating an existing trait, use the EXACT same key so the upsert merges properly
- Never include: passwords, API keys, tokens, or sensitive credentials
- Include ALL participants, not just the owner — approved users, group members, anyone identified

Also extract relationships between people. If someone mentions a wife, friend, collaborator, etc., include it:

```
<user_relationships>
[
  {{
    "from_chat_id": "<person's chat_id>",
    "to_display_name": "<related person's name>",
    "to_chat_id": "<their chat_id if known, else empty string>",
    "relation": "<wife|husband|friend|collaborator|colleague|manager|child|parent|sibling|AI agent|other>",
    "context": "<brief note on how you learned this>",
    "confidence": <0.0-1.0>
  }}
]
</user_relationships>
```

Rules for relationships:
- Only extract when there's clear evidence from the conversation
- Use the most specific relation type that fits
- Include both directions if both people have profiles (e.g., Brad→Yulia as "wife", Yulia→Brad as "husband")

## Phase 6 — Extract knowledge graph triples

Review your consolidated memories and the conversation history for factual relationships between entities. Output structured triples that capture WHO/WHAT is connected to WHAT/WHOM and HOW.

Output EXACTLY this format:

```
<knowledge_graph>
[
  {{
    "subject": "entity name",
    "predicate": "relationship",
    "object": "entity name",
    "subject_type": "person|project|tool|concept|agent|company|location|unknown",
    "object_type": "person|project|tool|concept|agent|company|location|unknown",
    "confidence": 0.8,
    "valid_from": "2026-03 or empty string if unknown",
    "temporal_granularity": "explicit|inferred|none",
    "evidence_span": "short excerpt showing where you found this",
    "is_negation": false
  }}
]
</knowledge_graph>
```

Predicate vocabulary (use these when possible):
- Functional (one active value): lives_in, works_at, employed_by, primary_language, managed_by, married_to, current_role, timezone, runs_on, hosted_on
- Multi-valued: uses, knows, likes, prefers, works_on, contributes_to, collaborates_with, speaks, member_of, has_skill, owns, maintains, friends_with
- Events: created, built, shipped, deployed, moved_to, joined, left, started, completed, fixed, decided, proposed, merged

Rules:
- Only extract concrete facts, not speculation
- Set is_negation=true for things that ended ("stopped using X", "moved away from Y")
- temporal_granularity: "explicit" if there's a date, "inferred" for "currently"/"now", "none" if no time signal
- confidence: 0.9+ for explicitly stated, 0.6-0.8 for inferred, below 0.5 = skip it
- Keep evidence_span under 100 chars — just enough to trace back
- 0-20 triples per dream — quality over quantity. Output empty array if nothing qualifies.

## Phase 7 — Extract reusable skills (formerly Phase 6)

Review the conversations for multi-step workflows that could become reusable skills. Look for:

- **Repeated patterns** — the same type of task done 2+ times (e.g., "deploy website", "fact-check comparison", "audit i18n")
- **Novel complex workflows** — a task that took 5+ steps and produced good results, likely to recur
- **Explicit requests** — the user said "we should automate this" or "do this every time"

For each candidate, output:

```
<proposed_skills>
[
  {{
    "skill_name": "kebab-case-name",
    "description": "When to trigger this skill — be specific about the context.",
    "task_summary": "What this skill does, step by step.",
    "source_pattern": "Brief note on what conversations led to this proposal."
  }}
]
</proposed_skills>
```

Rules:
- Only propose skills for genuinely repeating or high-value workflows
- 0-3 skills per dream — quality over quantity. Output an empty array if nothing qualifies.
- Don't propose skills that already exist (check memory for "skill" references)
- The description should be specific enough that an agent knows when to activate it
- Use kebab-case names under 30 characters

## Phase 8 — Report

Output a plain summary of what you did. Cover:
- What time range you processed and how many messages
- How many memories you stored or updated
- How many user profile entries extracted
- How many KG triples extracted (if any)
- How many skills proposed (if any)
- Anything notable (a contradiction resolved, a stale memory pruned, a key fact captured)

If nothing meaningful changed — memories were already accurate and up to date — say so plainly.
"""
