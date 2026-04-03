---
name: doc-coauthoring
description: Guide users through a structured workflow for co-authoring documentation. Use when user wants to write documentation, proposals, technical specs, decision docs, or similar structured content. Trigger when user mentions writing docs, creating proposals, drafting specs, or similar documentation tasks.
---

# Doc Co-Authoring Workflow

Three-stage workflow: Context Gathering → Refinement & Structure → Reader Testing.

## Stage 1: Context Gathering

Ask meta-context: doc type, audience, desired impact, template/format, constraints.

Encourage info dump: background, related discussions, why alternatives not used, org context, timeline, technical dependencies, stakeholder concerns.

Ask 5-10 clarifying questions. User can answer in shorthand.

Exit when questions show understanding of edge cases and trade-offs.

## Stage 2: Refinement & Structure

Build section by section. For each section:
1. Ask 5-10 clarifying questions
2. Brainstorm 5-20 options
3. User curates (keep/remove/combine)
4. Gap check
5. Draft (use `str_replace` for edits, never reprint whole doc)
6. Iterate until satisfied

Create initial document structure with placeholders first. Use `create_file` for artifacts or create markdown file.

**Key instruction**: Ask user to indicate changes rather than editing directly — helps learn their style.

After 3 iterations with no substantial changes, ask if anything can be removed.

At 80%+ completion: re-read entire doc for flow, consistency, redundancy, "slop".

## Stage 3: Reader Testing

Test document with a fresh Claude (no context) to verify it works for readers.

**With sub-agents available:**
1. Predict 5-10 reader questions
2. Test with sub-agent using just document + question
3. Run additional checks for ambiguity, false assumptions, contradictions
4. Fix issues found

**Without sub-agents:**
1. Generate 5-10 reader questions
2. User opens fresh Claude conversation and tests
3. Iterate based on results

Exit when Reader Claude consistently answers correctly.

## Final Review
- Recommend user does final read-through
- Suggest double-checking facts, links, technical details
- Consider linking this conversation in appendix
- Use appendices for depth without bloating main doc