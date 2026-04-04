"""OpenClaw → PinkyBot Claude-assisted mapper.

Calls Claude (via anthropic SDK directly, not claude-agent-sdk) to:
    - split_soul_boundaries   : separate personality from ethics/limits
    - parse_heartbeat_schedules : plain English → cron entries
    - classify_memories        : chunk + classify MEMORY.md content
    - split_directives         : AGENTS.md paragraphs → individual directives

All functions are async and return structured data. They use simple
system+user prompts and parse JSON from Claude's response.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field

# ── Model string translation table ────────────────────────────────────────────

MODEL_MAP: dict[str, tuple[str, str | None]] = {
    "anthropic/claude-opus-4-5": ("opus", None),
    "anthropic/claude-sonnet-4-5": ("sonnet", None),
    "anthropic/claude-haiku-4-5": ("haiku", None),
    "openai/gpt-4o": ("gpt-4o", "openrouter"),
    "google/gemini-2-flash": ("gemini-2-flash", "openrouter"),
    # Add more as OpenClaw's model list expands
}

# Mapper uses claude-haiku-3 for speed/cost — all tasks are structural, not creative
_MAPPER_MODEL = "claude-haiku-4-5-20251001"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Output dataclasses ─────────────────────────────────────────────────────────


@dataclass
class ScheduleEntry:
    """A parsed heartbeat schedule entry, ready to insert as AgentSchedule."""

    name: str = ""
    description: str = ""          # Original plain-English description
    cron: str = ""                  # Cron expression (5-field)
    prompt: str = ""                # Message to inject when this schedule fires
    timezone: str = "America/Los_Angeles"
    confidence: str = "high"        # high | medium | low


@dataclass
class ReflectionDraft:
    """A classified memory chunk, ready to insert as a Reflection."""

    content: str = ""
    reflection_type: str = "fact"   # fact | insight | project_state | interaction_pattern | continuation
    context: str = ""
    project: str = ""
    salience: int = 3               # 1–5
    entities: list[str] = field(default_factory=list)


@dataclass
class DirectiveDraft:
    """An extracted directive from AGENTS.md, ready to insert as AgentDirective."""

    directive: str = ""
    priority: int = 0
    source_paragraph: str = ""      # Original text for diff/review in UI


# ── Model translation ──────────────────────────────────────────────────────────


def translate_model(openclaw_model: str) -> tuple[str, str | None]:
    """Translate an OpenClaw provider/model string to PinkyBot model + provider.

    Returns (model_str, provider_ref) where provider_ref is None for Anthropic
    (the default) or a string like "openrouter" for third-party models.

    Falls back to (raw_string, "custom") for unknown model strings.
    """
    if openclaw_model in MODEL_MAP:
        return MODEL_MAP[openclaw_model]

    # Partial match — e.g. "anthropic/claude-opus-4" or "anthropic/claude-3-opus"
    for key, value in MODEL_MAP.items():
        if openclaw_model.startswith(key.rsplit("/", 1)[0]):
            # Same provider prefix — return the model part as-is
            model_part = openclaw_model.split("/", 1)[-1] if "/" in openclaw_model else openclaw_model
            provider = value[1]
            return (model_part, provider)

    # Unknown model — preserve and mark as custom
    model_part = openclaw_model.split("/", 1)[-1] if "/" in openclaw_model else openclaw_model
    return (model_part, "custom")


# ── Claude call helpers ────────────────────────────────────────────────────────


def _get_anthropic_client():
    """Lazy-import and construct an anthropic.Anthropic client."""
    try:
        import anthropic
    except ImportError as e:
        raise ImportError(
            "anthropic SDK is required for migration mapper. "
            "Install with: pip install anthropic"
        ) from e

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return anthropic.Anthropic(api_key=api_key)


def _call_claude(system: str, user: str, *, max_tokens: int = 2048) -> str:
    """Synchronous Claude call. Returns the text content of the response.

    Uses haiku for speed — all mapper tasks are structural parsing, not creative.
    """
    client = _get_anthropic_client()
    response = client.messages.create(
        model=_MAPPER_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text if response.content else ""


def _extract_json(text: str) -> Any:
    """Extract the first JSON object or array from a Claude response string.

    Claude sometimes wraps JSON in ```json ... ``` fences or adds prose around it.
    """
    # Try raw parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try code fence extraction
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding the first { or [ and parsing from there
    for start_char, end_char in (("{", "}"), ("[", "]")):
        idx = text.find(start_char)
        if idx >= 0:
            # Find the matching close by counting braces
            depth = 0
            for i in range(idx, len(text)):
                if text[i] == start_char:
                    depth += 1
                elif text[i] == end_char:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[idx:i + 1])
                        except json.JSONDecodeError:
                            break

    raise ValueError(f"_extract_json: no valid JSON found in Claude response: {text[:200]!r}")


# ── Public mapper functions ────────────────────────────────────────────────────


def split_soul_boundaries(soul_md: str) -> tuple[str, str]:
    """Use Claude to split OpenClaw SOUL.md into separate soul and boundaries.

    OpenClaw SOUL.md blends personality/identity with ethics/limits in a single
    file. PinkyBot stores these in separate DB fields:
    - soul      : personality, tone, communication style, values, interests
    - boundaries: ethics rules, what to avoid, privacy constraints, limits

    Returns (soul_text, boundaries_text). If the input is empty, returns two
    empty strings. Falls back to (soul_md, "") on Claude errors.
    """
    if not soul_md.strip():
        return ("", "")

    system = """You are a migration assistant splitting an AI agent configuration file.

Your task: split a SOUL.md file (which blends personality and ethics) into two
separate sections for a new platform.

Output ONLY valid JSON with exactly two keys:
{
  "soul": "...",
  "boundaries": "..."
}

"soul" should contain: personality, tone, communication style, values, interests,
how the agent thinks, what it cares about, how it relates to its owner.

"boundaries" should contain: ethics rules, what the agent will/won't do, privacy
constraints, limits on behavior, how to handle sensitive situations.

Preserve the original Markdown formatting within each section. If the file is
entirely personality (no ethics/limits), put everything in "soul" and leave
"boundaries" empty. Do not add, invent, or summarize — only redistribute."""

    user = f"Split this SOUL.md:\n\n{soul_md}"

    try:
        raw = _call_claude(system, user, max_tokens=4096)
        parsed = _extract_json(raw)
        soul = parsed.get("soul", "").strip()
        boundaries = parsed.get("boundaries", "").strip()
        return (soul, boundaries)
    except Exception as e:
        _log(f"mapper.split_soul_boundaries: Claude call failed ({e}), returning unsplit soul")
        return (soul_md, "")


def parse_heartbeat_schedules(heartbeat_md: str) -> list[ScheduleEntry]:
    """Use Claude to parse HEARTBEAT.md plain-English tasks into cron schedules.

    HEARTBEAT.md contains natural-language scheduled tasks like:
        "Every morning at 9am, check the project board and send a daily standup"
        "Weekly on Fridays at 5pm, summarize the week"

    Returns a list of ScheduleEntry objects. Returns empty list on empty input
    or if Claude fails to produce parseable output.
    """
    if not heartbeat_md.strip():
        return []

    system = """You are a migration assistant converting plain-English scheduled tasks to cron expressions.

For each task in the HEARTBEAT.md file, extract:
- name: short snake_case identifier (e.g. "morning_standup")
- description: original plain-English description (verbatim from the file)
- cron: 5-field cron expression (min hour dom month dow)
- prompt: what message to inject when the schedule fires (concise, imperative)
- timezone: best guess IANA timezone (default "America/Los_Angeles" if not specified)
- confidence: "high" if the schedule is unambiguous, "medium" if you had to guess the time, "low" if very ambiguous

Output ONLY a valid JSON array:
[
  {
    "name": "morning_standup",
    "description": "Every morning at 9am...",
    "cron": "0 9 * * *",
    "prompt": "Run morning standup: check project board and send daily update",
    "timezone": "America/Los_Angeles",
    "confidence": "high"
  }
]

If there are no parseable tasks, return an empty array: []"""

    user = f"Parse these scheduled tasks:\n\n{heartbeat_md}"

    try:
        raw = _call_claude(system, user, max_tokens=2048)
        parsed = _extract_json(raw)
        if not isinstance(parsed, list):
            return []
        entries = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            entry = ScheduleEntry(
                name=str(item.get("name", "")).strip(),
                description=str(item.get("description", "")).strip(),
                cron=str(item.get("cron", "")).strip(),
                prompt=str(item.get("prompt", "")).strip(),
                timezone=str(item.get("timezone", "America/Los_Angeles")).strip(),
                confidence=str(item.get("confidence", "medium")).strip(),
            )
            if entry.cron:  # Only include entries with a valid cron expression
                entries.append(entry)
        return entries
    except Exception as e:
        _log(f"mapper.parse_heartbeat_schedules: Claude call failed ({e}), returning []")
        return []


def classify_memories(memory_md: str) -> list[ReflectionDraft]:
    """Use Claude to chunk and classify MEMORY.md into typed Reflection records.

    MEMORY.md is an unstructured Markdown document with long-term facts. This
    function chunks it into discrete memory items, then classifies each with:
    - reflection_type : fact | insight | project_state | interaction_pattern | continuation
    - salience        : 1–5 importance score
    - entities        : person/entity names mentioned
    - project         : project slug if applicable

    Processes in batches of 30 chunks to avoid token limits. Large MEMORY.md
    files will require multiple Claude calls.
    """
    if not memory_md.strip():
        return []

    system = """You are a migration assistant classifying memories from an AI agent's memory file.

Each paragraph or distinct fact in the file should become one memory record.

Output ONLY a valid JSON array. Each item must have:
{
  "content": "the memory text (verbatim or minimally cleaned)",
  "reflection_type": "fact | insight | project_state | interaction_pattern | continuation",
  "context": "brief context string (e.g. 'From MEMORY.md import')",
  "project": "project slug if this memory belongs to a project, else empty string",
  "salience": 3,  // 1=trivial, 2=low, 3=normal, 4=important, 5=critical
  "entities": ["person names or important entities mentioned"]
}

Type guide:
- fact: concrete facts about the world, owner, or context
- insight: lessons learned, patterns observed, strategic conclusions
- project_state: current state of a specific project or task
- interaction_pattern: how the agent/owner tends to behave or communicate
- continuation: unfinished thoughts, next-time reminders, pending items

Return ALL distinct memories as separate objects. Do not merge or summarize.
If the input is empty, return []."""

    # Split into chunks of ~30 paragraphs to avoid hitting token limits
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", memory_md) if p.strip()]
    batch_size = 30
    all_drafts: list[ReflectionDraft] = []

    for batch_start in range(0, len(paragraphs), batch_size):
        batch = paragraphs[batch_start:batch_start + batch_size]
        batch_text = "\n\n".join(batch)
        user = f"Classify these memory entries:\n\n{batch_text}"

        try:
            raw = _call_claude(system, user, max_tokens=4096)
            parsed = _extract_json(raw)
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if not isinstance(item, dict) or not item.get("content"):
                    continue
                # Validate and clamp salience
                salience = int(item.get("salience", 3))
                salience = max(1, min(5, salience))
                # Validate type
                valid_types = {"fact", "insight", "project_state", "interaction_pattern", "continuation"}
                rtype = str(item.get("reflection_type", "fact")).strip()
                if rtype not in valid_types:
                    rtype = "fact"
                entities_raw = item.get("entities", [])
                entities = [str(e) for e in entities_raw if e] if isinstance(entities_raw, list) else []

                all_drafts.append(ReflectionDraft(
                    content=str(item["content"]).strip(),
                    reflection_type=rtype,
                    context=str(item.get("context", "Imported from OpenClaw MEMORY.md")).strip(),
                    project=str(item.get("project", "")).strip(),
                    salience=salience,
                    entities=entities,
                ))
        except Exception as e:
            _log(f"mapper.classify_memories: batch {batch_start} failed ({e}), skipping batch")
            continue

    return all_drafts


def split_directives(agents_md: str) -> list[DirectiveDraft]:
    """Use Claude to extract individual directives from AGENTS.md.

    AGENTS.md contains operating procedures, workflows, and behavioral rules
    written as prose. This function extracts each distinct rule or instruction
    as a separate DirectiveDraft ready to insert into agent_directives.

    Returns a list of DirectiveDraft objects, ordered by approximate priority
    (higher priority items earlier in the source get higher priority numbers).
    Falls back to paragraph-splitting on Claude errors.
    """
    if not agents_md.strip():
        return []

    system = """You are a migration assistant extracting operational directives from an AI agent's procedures file.

AGENTS.md contains operating procedures, workflows, and behavioral rules.
Extract each distinct rule, instruction, or operating principle as a separate directive.

Output ONLY a valid JSON array. Each item must have:
{
  "directive": "a clear, imperative directive statement (1-3 sentences max)",
  "priority": 50,  // 0-100, higher = more important (important rules get higher numbers)
  "source_paragraph": "the original text this was extracted from"
}

Guidelines:
- Each directive should be self-contained and actionable
- Do not merge unrelated rules into one directive
- Do not split a single rule into multiple directives unless they are truly independent
- Preserve the intent and constraints from the original text
- Priority 80-100: critical safety/ethics rules
- Priority 50-79: important behavioral guidelines
- Priority 20-49: workflow preferences and conventions
- Priority 0-19: nice-to-have suggestions

If there are no distinct directives, return []."""

    user = f"Extract directives from this AGENTS.md:\n\n{agents_md}"

    try:
        raw = _call_claude(system, user, max_tokens=4096)
        parsed = _extract_json(raw)
        if not isinstance(parsed, list):
            return _fallback_paragraph_split(agents_md)

        drafts = []
        for item in parsed:
            if not isinstance(item, dict) or not item.get("directive"):
                continue
            priority = int(item.get("priority", 50))
            priority = max(0, min(100, priority))
            drafts.append(DirectiveDraft(
                directive=str(item["directive"]).strip(),
                priority=priority,
                source_paragraph=str(item.get("source_paragraph", "")).strip(),
            ))
        return drafts if drafts else _fallback_paragraph_split(agents_md)

    except Exception as e:
        _log(f"mapper.split_directives: Claude call failed ({e}), falling back to paragraph split")
        return _fallback_paragraph_split(agents_md)


def _fallback_paragraph_split(agents_md: str) -> list[DirectiveDraft]:
    """Naive fallback: split AGENTS.md by paragraphs and return each as a directive."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", agents_md) if p.strip()]
    return [
        DirectiveDraft(directive=p, priority=50, source_paragraph=p)
        for p in paragraphs
    ]


# Type alias for _extract_json return
from typing import Any
