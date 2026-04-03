---
name: claude-api
description: Build apps with the Claude API or Anthropic SDK. TRIGGER when: code imports `anthropic`/`@anthropic-ai/sdk`/`claude_agent_sdk`, or user asks to use Claude API, Anthropic SDKs, or Agent SDK. DO NOT TRIGGER when: code imports `openai`/other AI SDK, general programming, or ML/data-science tasks.
---

# Building LLM-Powered Applications with Claude

## Defaults
- Model: `claude-opus-4-6` (always, unless user explicitly specifies otherwise)
- Use adaptive thinking: `thinking: {type: "adaptive"}` for anything complicated
- Default to streaming for long input/output requests

## Language Detection
Detect from project files: .py → Python, .ts/.tsx → TypeScript, .java/pom.xml → Java, .go/go.mod → Go, .rb/Gemfile → Ruby, .cs → C#, .php → PHP

## Surface Selection
| Use Case | Surface |
|----------|---------|
| Classification, summarization, Q&A | Claude API (single call) |
| Multi-step pipelines with your own tools | Claude API + tool use |
| Agent needing file/web/terminal access | Agent SDK |
| Open-ended agent, your own tools | Claude API agentic loop |

## Current Models (2026-02-17)
| Model | ID | Context |
|-------|-----|---------|
| Claude Opus 4.6 | `claude-opus-4-6` | 200K (1M beta) |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | 200K (1M beta) |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K |

**ALWAYS use `claude-opus-4-6` unless user explicitly names another model.**
**Use exact model ID strings — no date suffixes.**

## Thinking
- Opus 4.6/Sonnet 4.6: `thinking: {type: "adaptive"}` — do NOT use `budget_tokens` (deprecated)
- Effort: `output_config: {effort: "low"|"medium"|"high"|"max"}` (max = Opus 4.6 only)
- Older models only if explicitly requested: `thinking: {type: "enabled", budget_tokens: N}`

## Key Pitfalls
- Don't truncate inputs
- Opus 4.6 prefill removed (use structured outputs instead)
- `max_tokens`: ~16000 non-streaming, ~64000 streaming
- Use `output_config: {format: {...}}` not deprecated `output_format`
- Use SDK types, don't redefine interfaces
- Prompt caching: stable content first, verify with `usage.cache_read_input_tokens`
- Compaction (beta): requires header `compact-2026-01-12`, preserve `response.content` not just text