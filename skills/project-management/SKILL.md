---
name: project-management
description: |
  Manage projects using PinkyBot's built-in task system. Use when breaking down a
  project into tasks, planning a sprint, tracking progress across multiple tasks,
  or coordinating work across agents. Covers decomposition, sprint planning,
  milestones, daily rhythm, and multi-agent handoffs.
allowed-tools:
  - mcp__pinky-self__create_task
  - mcp__pinky-self__claim_task
  - mcp__pinky-self__complete_task
  - mcp__pinky-self__get_next_task
  - mcp__pinky-self__block_task
  - mcp__pinky-self__list_agents
---

# Project Management

Managing projects in PinkyBot means breaking down work into well-scoped tasks, keeping state visible, and coordinating across agents without stepping on each other. This skill covers the full lifecycle.

## When to use this skill

- Breaking a plain-language goal into concrete deliverables
- Starting a sprint or planning a week of work
- Picking up a project mid-stream (what's done, what's next, what's blocked)
- Handing off work to another agent
- Checking fleet status before delegating

---

## Core workflow

### 1. Project decomposition

Before creating a single task, answer three questions:
1. What is the final deliverable? (Be specific — not "build the API" but "API returns 200 for all endpoints in spec v1.2")
2. What are the natural phases? (Research → Design → Build → Test → Ship)
3. What can be parallelized?

Then create tasks. Each task needs:
- **title** — one clear action + deliverable ("Write OpenAPI spec for /users endpoints")
- **description** — what done looks like (acceptance criteria), plus context and gotchas
- **priority** — `urgent`, `high`, `normal`, `low`
- **tags** — technology, domain, type (e.g., `["api", "auth", "backend"]`)
- **blocked_by** — task IDs that must complete first

```
create_task(
  title="Write OpenAPI spec for /users endpoints",
  description="Draft OpenAPI 3.1 spec covering GET /users, POST /users, GET /users/{id}, PATCH /users/{id}. Acceptance: spec validates with swagger-cli, all error codes documented. Gotcha: /users/{id} returns 404 not 403 for missing users per security decision 2025-11-02.",
  priority="high",
  tags=["api", "users", "spec"],
  project_id="proj_abc"
)
```

### 2. Sprint planning

A sprint is a time-boxed container for a specific, measurable goal. Rules:
- One active sprint per project at a time
- Duration: 1–2 weeks (default 1 week for fast-moving projects)
- Sprint goal must be a sentence a non-engineer can evaluate: "Users can sign up and log in with email/password"
- Assign only tasks you expect to finish; keep a backlog for the rest
- Set realistic dates — pad 20% for unknowns

```
create_sprint(
  project_id="proj_abc",
  name="Auth MVP",
  goal="Users can sign up, verify email, and log in. Passwords stored with bcrypt.",
  start_date="2026-04-07",
  end_date="2026-04-14"
)
```

After creating the sprint, assign tasks by adding `sprint_id` to each task, or move existing tasks into the sprint. Prioritize: `urgent` tasks first, then `high`, then fill remaining capacity with `normal`.

### 3. Milestone setting

Milestones mark completion of a meaningful phase — they are checkpoints, not tasks.

Use a milestone when:
- A phase of work closes (design complete, alpha deployed, beta shipped)
- An external dependency triggers (contract signed, API keys received)
- A demo or review is scheduled

Do not create a milestone for individual tasks. A milestone represents 3–20 tasks converging on a shared outcome.

Naming convention: `[Phase] [Outcome]` — "Design: wireframes approved", "Backend: auth endpoints live", "Launch: v1 shipped to production"

Link tasks to a milestone via `milestone_id`. When all linked tasks are complete, mark the milestone complete.

### 4. Daily rhythm

**Start of session** — get oriented before picking up work:
```
get_next_task(project_id="proj_abc")
```
This returns the highest-priority unclaimed task that is unblocked. Review it, check `blocked_by`, then claim it:
```
claim_task(task_id="task_xyz")
```

**During work** — if you hit a blocker, don't leave the task `in_progress` silently:
```
block_task(task_id="task_xyz", reason="Waiting on DB credentials from owner — can't run integration tests")
```

**Completing work** — always write a result summary. Future agents (and you, two weeks from now) will thank you:
```
complete_task(
  task_id="task_xyz",
  result="OpenAPI spec written and validated. File at /docs/api/users.yaml. Skipped PATCH /users/{id}/avatar — deferred to task_890 per owner decision."
)
```

**End of session** — if handing off, write a `[HANDOFF]` note (see Multi-agent coordination below).

### 5. Multi-agent coordination

**Before delegating**, check the fleet:
```
list_agents()
```
Look at each agent's skills and current load. Delegate to the agent best suited for the task, not just the first available one.

**Assign a task to another agent** by setting `assigned_agent` when creating or updating the task:
```
create_task(
  title="...",
  assigned_agent="research-agent",
  ...
)
```

**Handoff convention** — use prefixed notes in task descriptions/comments:

`[CLAIM]` — written by the agent picking up a task. States what they understood, what they plan to do first.
> `[CLAIM] barsik 2026-04-02: Taking this on. Starting with the existing /docs/api/users.yaml spec. Will add auth endpoints before touching user endpoints.`

`[HANDOFF]` — written when leaving a task for another agent. States: what was done, current state, what's next, any gotchas.
> `[HANDOFF] barsik 2026-04-02: Spec complete for /users. Auth endpoints still need /refresh and /logout — see task_891. Note: the 401 vs 403 distinction is documented in /docs/decisions/auth-errors.md, don't skip it.`

Never pick up a task that has an active `[CLAIM]` from another agent unless the task has been explicitly reassigned or the claiming agent has been idle for 24+ hours.

---

## pinky-self MCP tools

| Tool | When to use |
|------|-------------|
| `create_task` | Add a new task to a project or backlog |
| `claim_task` | Mark yourself as actively working on a task — do this before starting work |
| `complete_task` | Mark done; always include a result summary |
| `get_next_task` | Get the highest-priority unclaimed, unblocked task for a project |
| `block_task` | Mark a task blocked with a specific reason; don't leave it `in_progress` |
| `list_agents` | See all agents in the fleet before delegating |

---

## Best practices

**One task = one clear deliverable.** If you can't write acceptance criteria in two sentences, the task is too big. Split it.

**Claim before working.** The claim is a lock. Without it, two agents can start the same task.

**Complete with a result summary.** "Done" is not a summary. Write what was produced, where it lives, and what was deferred.

**Keep tasks session-sized.** If a task will take more than one session, break it into subtasks. Long-running `in_progress` tasks are invisible work — they don't show up in progress metrics and they block dependents.

**Check `blocked_by` before claiming.** `get_next_task` filters out blocked tasks, but if you're manually picking, always check. Claiming a blocked task achieves nothing and hides the blockage.

**Use tags consistently.** Tags are your search surface. Pick 2–4 per task. Use the same vocabulary across the project: `["backend", "auth", "blocking"]` not `["server-side", "authentication", "high-priority"]`.

**One active sprint per project.** Having two active sprints splits attention and makes progress reporting meaningless.

**Sprint goals must be evaluable.** "Make progress on auth" is not a goal. "Users can sign up, verify email, and log in" is a goal. The test: can someone who didn't do the work tell whether the goal was met?

---

## Anti-patterns

**No acceptance criteria.** If the task just says "build the login page," it will never be clearly done. Always answer: what does done look like?

**Silent `in_progress`.** A task that's been `in_progress` for two days with no update is a black hole. If it's blocked, call `block_task`. If it's too big, split it and complete the subtasks.

**Grabbing too much.** Taking on 5 tasks simultaneously means none of them get a `[CLAIM]` note, context is fragmented, and completion rate drops. Max 2–3 active tasks. Finish before picking up more.

**Ignoring dependencies.** Starting task B before task A is done wastes work. Check `blocked_by` before claiming. If you don't know the dependency, ask.

**Vague handoffs.** `[HANDOFF] I worked on this` is useless. The receiving agent needs to know what was done, what the current state is, where files are, and what comes next.

**Milestone as a task.** A milestone is a checkpoint, not a unit of work. If your milestone is "write the spec," it should be a task, not a milestone. Milestones mark when a phase closes.

**Delegating without checking fit.** Assigning a frontend task to a backend-only agent because they're available wastes both agents' time. Check `list_agents` and match skills.

---

## Example: decomposing a plain-language goal

**Input:** "Build a Slack bot that lets users query our internal knowledge base."

**Decomposition:**

Phase 1 — Foundation
- Research Slack Bolt SDK and available MCP tools [normal, `["slack", "research"]`]
- Set up Slack app, get bot token, configure OAuth scopes [high, `["slack", "infra", "blocking"]`]

Phase 2 — Knowledge base integration
- Index existing docs into vector DB [high, `["kb", "indexing"]`, blocked_by: phase 1]
- Write query tool: embed user query, retrieve top-5 chunks, format response [high, `["kb", "search"]`, blocked_by: indexing task]

Phase 3 — Bot logic
- Handle Slack slash command `/ask`, route to query tool, return formatted response [high, `["slack", "bot"]`, blocked_by: query tool]
- Handle direct messages, thread replies [normal, `["slack", "bot"]`, blocked_by: slash command task]

Phase 4 — Hardening
- Write tests for query tool (unit) and bot handler (integration) [normal, `["testing"]`]
- Deploy to production, set up error alerting [high, `["infra", "deploy"]`, blocked_by: tests]

**Milestones:**
- "Foundation: Slack app connected and responding to pings"
- "MVP: /ask returns relevant results from knowledge base"
- "Launch: deployed to production with alerting"
