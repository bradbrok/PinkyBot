---
name: skill-creator
description: Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a skill from scratch, edit, or optimize an existing skill, run evals to test a skill, benchmark skill performance with variance analysis, or optimize a skill's description for better triggering accuracy.
---

# Skill Creator

A skill for creating new skills and iteratively improving them.

## The Loop
1. Understand what the skill should do
2. Write a draft SKILL.md
3. Create test prompts and run claude-with-access-to-the-skill
4. Evaluate results (qualitative + quantitative)
5. Rewrite based on feedback
6. Repeat until satisfied
7. Expand test set and try at larger scale

## Skill Anatomy
```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description required)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - Executable code
    ├── references/ - Docs loaded as needed
    └── assets/     - Files used in output
```

## SKILL.md Structure
- **name**: Skill identifier (lowercase, hyphens)
- **description**: When to trigger + what it does. All "when to use" goes here. Make descriptions slightly "pushy" to combat undertriggering.
- **body**: Instructions for the skill

## Writing Principles
- Explain the WHY behind instructions (LLMs respond better to reasoning than rigid rules)
- Keep SKILL.md under 500 lines; add references for longer content
- Prefer imperative form
- If writing ALWAYS/NEVER in all caps, stop and explain the reasoning instead
- Remove things that aren't pulling their weight
- Generalize from examples, don't overfit

## Running Evals
1. Spawn all runs (with-skill AND baseline) in same turn
2. While running, draft assertions
3. Capture timing data from notifications
4. Grade, aggregate, launch viewer: `python -m scripts.aggregate_benchmark <workspace>/iteration-N`
5. Read feedback.json

## Description Optimization
Generate 20 eval queries (mix of should-trigger and should-not-trigger). Run optimization loop:
```bash
python -m scripts.run_loop --eval-set <path> --skill-path <path> --model <model-id> --max-iterations 5
```

## Claude.ai / Cowork Adaptations
- No subagents: run test cases serially, skip baselines
- No browser: present results inline, ask for feedback in conversation
- Cowork: use `--static <output_path>` for eval viewer, generate viewer BEFORE evaluating yourself