# Review heuristics

This directory holds shared review and authoring heuristics for the agent
pool — frameworks that compound across PRs when every agent applies the
same checklist at *write* time, not just at review time.

## Why this directory is tracked

`docs/` is gitignored by default so scratch design notes and local-only
investigation don't accidentally get committed. `docs/review/` is carved
out as an explicit tracked subdir because the heuristics here are
intended to outlive any single PR and to be discoverable next to the
source they shape.

If you want to add another always-tracked docs subdirectory in the
future, follow the same pattern: add `!docs/<name>/` to `.gitignore`
in its own small PR, then ship the actual content separately so the
review of the carve-out doesn't get tangled with the review of the
content.

## Current docs

- _Placeholder — heuristics doc landing in a follow-up PR._

## Adding a new heuristic

Heuristics earn their place by surviving the same discipline they
impose:

1. **n=2 cross-domain** for case-shaped detectors (mechanical detectors
   for bug-shape patterns).
2. **Mechanical detector required** — broad pattern recognition without
   a detector belongs in an open-observations appendix, not a numbered
   case.
3. **Self-application** — if the framework can't survive being applied
   to itself, it's not strong enough yet.

Open observations (n=1 candidates) and case candidates that lack sharp
detectors live in appendices, not the main framework.
