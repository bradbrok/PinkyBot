"""Pinky Self — MCP server for agent self-management.

Gives agents tools to manage their own lifecycle:
- Wake schedules (set/list/remove cron jobs)
- Context management (save/load continuation state)
- Task management (claim, complete, block, get next)
- Health monitoring (check own status)
- Sleep/wake control (request deep sleep, set wake timers)

This server runs alongside the agent and connects to the
PinkyBot API on localhost. Agents call these tools naturally
during their work loop.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from pinky_daemon.auth import build_internal_auth_headers


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def create_server(
    *,
    agent_name: str = "",
    api_url: str = "http://localhost:8888",
    host: str = "127.0.0.1",
    port: int = 8010,
) -> FastMCP:
    """Create the pinky-self MCP server.

    Args:
        agent_name: The agent's own name (injected at startup).
        api_url: PinkyBot API URL.
    """
    mcp = FastMCP("pinky-self", host=host, port=port)

    def _api(method: str, path: str, body: dict | None = None) -> dict:
        """Call the PinkyBot API."""
        url = f"{api_url}{path}"
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        secret = os.environ.get("PINKY_SESSION_SECRET", "")
        headers.update(build_internal_auth_headers(
            secret,
            agent_name=agent_name,
            method=method,
            path=path,
        ))
        req = urllib.request.Request(
            url, data=data, method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            return {"error": error_body, "status": e.code}
        except Exception as e:
            return {"error": str(e)}

    def _format_age(seconds: float | int | None) -> str:
        """Render compact age text for saved context metadata."""
        if seconds is None:
            return "unknown"
        total = max(int(seconds), 0)
        if total < 60:
            return f"{total}s"
        minutes, sec = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m" if sec == 0 else f"{minutes}m {sec}s"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
        days, hours = divmod(hours, 24)
        return f"{days}d" if hours == 0 else f"{days}d {hours}h"

    def _format_timestamp(ts: float | int | None) -> str:
        """Render a saved timestamp in UTC for clarity across restarts."""
        if not ts:
            return "unknown time"
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Wake Schedules ─────────────────────────────────────

    @mcp.tool()
    def set_wake_schedule(
        cron: str,
        name: str = "",
        prompt: str = "",
        timezone: str = "America/Los_Angeles",
        direct_send: bool = False,
        target_channel: str = "",
    ) -> str:
        """Set a cron-based schedule. Two modes:

        MODE 1 — Wake (default): The prompt is sent TO YOU as input. You wake up,
        read the prompt, and act on it. Use this for tasks where you need to think,
        use tools, or compose a response.

        Example: set_wake_schedule(cron="0 8 * * *", name="morning_check",
                 prompt="Check inbox, summarize overnight messages, then use send(chat_id='6770805286', platform='telegram', text='...') to send Brad a status update.")

        MODE 2 — Direct Send: The prompt is sent DIRECTLY to a chat as a message,
        bypassing you entirely. Use this for simple scheduled messages that don't
        need any processing.

        Example: set_wake_schedule(cron="0 9 * * 1-5", name="standup_reminder",
                 prompt="Good morning! Time for standup.", direct_send=True, target_channel="6770805286")

        IMPORTANT:
        - In wake mode, the prompt is an INSTRUCTION to you, not a message to send.
          To send a message to someone, your prompt should tell you to do that.
        - Use your Pinky MCP tools in your response, not Claude Code built-in tools.
        - For outbound chat messages, use explicit pinky-messaging tools like
          send(chat_id, platform, text) or thread(message_id, text) for quoting.
        - When using send(...), use chat IDs (e.g. "6770805286"), not display names.

        Args:
            cron: Cron expression (e.g. "0 8 * * *" for daily at 8am,
                  "*/30 * * * *" for every 30 min, "0 9 * * 1-5" for weekdays 9am).
            name: Human-friendly schedule name (e.g. "morning_check").
            prompt: Wake mode: instruction for you. Direct mode: message to send.
            timezone: Timezone for the schedule (default: America/Los_Angeles).
            direct_send: If true, prompt is sent directly as a message (no agent processing).
            target_channel: Chat ID for direct_send mode (e.g. "6770805286").
        """
        result = _api("POST", f"/agents/{agent_name}/schedules", {
            "name": name or "self_scheduled",
            "cron": cron,
            "prompt": prompt or f"Self-scheduled wake: {name}",
            "timezone": timezone,
            "direct_send": direct_send,
            "target_channel": target_channel,
        })
        if "error" in result:
            return f"Failed to set schedule: {result['error']}"
        mode = "direct-send" if direct_send else "wake"
        return f"Schedule '{result.get('name', name)}' set: {cron} ({timezone}), mode={mode}. ID: {result.get('id')}"

    @mcp.tool()
    def list_my_schedules() -> str:
        """List all your cron schedules with status and last run time.

        WHEN TO USE: You want to see what schedules you have set up, check if
        they're enabled, or find a schedule ID to remove.
        """
        result = _api("GET", f"/agents/{agent_name}/schedules?enabled_only=false")
        schedules = result.get("schedules", [])
        if not schedules:
            return "No schedules set."
        lines = []
        for s in schedules:
            status = "active" if s["enabled"] else "disabled"
            last = s.get("last_run", 0)
            last_str = f"last ran at {last}" if last else "never run"
            lines.append(f"#{s['id']} [{status}] {s['name']}: {s['cron']} — {s.get('prompt', '')[:80]} ({last_str})")
        return "\n".join(lines)

    @mcp.tool()
    def remove_wake_schedule(schedule_id: int) -> str:
        """Delete a wake schedule by ID.

        WHEN TO USE: A schedule is no longer needed. Get the ID from
        list_my_schedules first.

        Args:
            schedule_id: The schedule ID to remove (from list_my_schedules).
        """
        result = _api("DELETE", f"/agents/{agent_name}/schedules/{schedule_id}")
        if result.get("deleted"):
            return f"Schedule #{schedule_id} removed."
        return f"Failed to remove schedule: {result.get('error', 'not found')}"

    # ── Context Management ─────────────────────────────────

    @mcp.tool()
    def save_my_context(
        task: str = "",
        context: str = "",
        notes: str = "",
        blockers: list[str] | None = None,
        priority_items: list[str] | None = None,
    ) -> str:
        """Snapshot your current working state for restoration after restart/sleep.

        WHEN TO USE: Before calling context_restart, request_sleep, or when
        context is getting high. Also required by the restart guard — you
        MUST call this before any restart or sleep will be allowed.
        NOT FOR: Long-term memory (use reflect for cross-session insights).

        Args:
            task: What you were working on (brief description).
            context: Key context/state that must be preserved.
            notes: Freeform notes for your future self.
            blockers: List of things blocking progress.
            priority_items: What to do first when you wake up.
        """
        result = _api("PUT", f"/agents/{agent_name}/context", {
            "task": task,
            "context": context,
            "notes": notes,
            "blockers": blockers or [],
            "priority_items": priority_items or [],
            "metadata": {"source": "save_my_context", "server": "pinky-self"},
        })
        if "error" in result:
            return f"Failed to save context: {result['error']}"
        return "Context saved. You'll see this when you wake up."

    @mcp.tool()
    def load_my_context() -> str:
        """Restore your working state from before the last restart/sleep.

        WHEN TO USE: On wake-up or after a context restart — call this first
        to pick up where you left off (task, notes, blockers, priorities).
        NOT FOR: Searching long-term memory (use recall).
        """
        result = _api("GET", f"/agents/{agent_name}/context")
        if result.get("context") is None:
            return "No saved context. This is a fresh start."
        ctx = result
        freshness = ctx.get("freshness", {})
        parts = []
        updated_at = ctx.get("updated_at")
        if updated_at:
            age = freshness.get("age_human") or _format_age(freshness.get("age_seconds"))
            parts.append(f"Saved: {_format_timestamp(updated_at)} ({age} ago)")
        if freshness.get("stale_warning"):
            parts.append("WARNING: Saved context is old. Verify it against current work before trusting it.")
        if ctx.get("task"):
            parts.append(f"Task: {ctx['task']}")
        if ctx.get("context"):
            parts.append(f"Context: {ctx['context']}")
        if ctx.get("notes"):
            parts.append(f"Notes: {ctx['notes']}")
        if ctx.get("blockers"):
            parts.append(f"Blockers: {', '.join(ctx['blockers'])}")
        if ctx.get("priority_items"):
            parts.append(f"Priority: {', '.join(ctx['priority_items'])}")
        return "\n".join(parts) if parts else "Context is empty."

    # ── Task Management ────────────────────────────────────

    @mcp.tool()
    def get_next_task(agent_name_override: str = "") -> str:
        """Get the highest-priority unblocked task assigned to you (or the specified agent).

        WHEN TO USE: You want to know what to work on next. Respects blocked_by
        dependencies — skips tasks where any blocker task isn't completed yet.
        Call this on wake-up after loading context, or whenever you finish a task.
        NOT FOR: Creating new tasks (use create_task), checking inbox (use check_inbox),
        claiming unassigned tasks (use claim_task).

        Args:
            agent_name_override: Agent name to query for (defaults to yourself).
        """
        target = agent_name_override or agent_name
        result = _api("GET", f"/tasks?assigned_agent={target}&status=pending&limit=50")
        tasks = result if isinstance(result, list) else result.get("tasks", [])
        if not tasks:
            return "No unblocked tasks available."

        # Check blockers, caching results to avoid redundant API calls
        blocker_cache: dict[int, str] = {}

        def blocker_status(blocker_id: int) -> str:
            if blocker_id not in blocker_cache:
                r = _api("GET", f"/tasks/{blocker_id}")
                blocker_cache[blocker_id] = r.get("status", "unknown")
            return blocker_cache[blocker_id]

        priority_order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        unblocked = []
        for t in tasks:
            blocked_by = t.get("blocked_by") or []
            if all(blocker_status(bid) == "completed" for bid in blocked_by):
                unblocked.append(t)

        if not unblocked:
            return "No unblocked tasks available."

        unblocked.sort(key=lambda t: priority_order.get(t.get("priority", "normal"), 2))
        t = unblocked[0]
        project_id = t.get("project_id") or 0
        project_str = f"Project: #{project_id}" if project_id else "Project: none"
        return (
            f"#{t['id']}: {t['title']}\n"
            f"Priority: {t.get('priority', 'normal')} | Status: {t.get('status', 'pending')}\n"
            f"Description: {t.get('description') or 'none'}\n"
            f"Tags: {', '.join(t.get('tags') or []) or 'none'}\n"
            f"{project_str}"
        )

    @mcp.tool()
    def claim_task(task_id: int) -> str:
        """Assign an unassigned task to yourself and set it to in_progress.

        WHEN TO USE: get_next_task returned an unassigned task you want to work on.

        Args:
            task_id: The task ID to claim.
        """
        result = _api("POST", f"/tasks/claim/{task_id}?agent_name={agent_name}")
        if "error" in result:
            return f"Failed to claim task: {result['error']}"
        return f"Claimed task #{task_id}: {result.get('title', '')}. Status: in_progress."

    @mcp.tool()
    def complete_task(task_id: int, summary: str = "") -> str:
        """Mark a task as done and notify its creator.

        WHEN TO USE: You finished the work for a task. Include a summary of
        what was done so the creator has context.

        Args:
            task_id: The task ID to complete.
            summary: Brief summary of what was done / results.
        """
        result = _api("POST", f"/tasks/complete/{task_id}?agent_name={agent_name}&summary={urllib.request.quote(summary)}")
        if "error" in result:
            return f"Failed to complete task: {result['error']}"
        return f"Task #{task_id} completed. Creator has been notified."

    @mcp.tool()
    def block_task(task_id: int, reason: str = "") -> str:
        """Flag a task as blocked and record why.

        WHEN TO USE: You can't make progress on a task — waiting on another
        agent, missing information, or need owner input. Always include a reason.

        Args:
            task_id: The task ID to block.
            reason: Why the task is blocked.
        """
        result = _api("POST", f"/tasks/block/{task_id}?agent_name={agent_name}&reason={urllib.request.quote(reason)}")
        if "error" in result:
            return f"Failed to block task: {result['error']}"
        return f"Task #{task_id} marked as blocked: {reason}"

    @mcp.tool()
    def create_task(
        title: str,
        description: str = "",
        priority: str = "normal",
        assigned_agent: str = "",
        tags: list[str] | None = None,
    ) -> str:
        """Create a new task for yourself or delegate to another agent.

        WHEN TO USE: You have work to track, or you want to delegate work to
        another agent. Set assigned_agent to target a specific agent, or leave
        empty to self-assign.
        NOT FOR: Messaging another agent directly (use send_to_agent).

        Args:
            title: Task title / what needs to be done.
            description: Detailed description, acceptance criteria.
            priority: low, normal, high, or urgent.
            assigned_agent: Agent name to assign to (leave empty for self, or name a worker).
            tags: Tags for categorization.
        """
        result = _api("POST", "/tasks", {
            "title": title,
            "description": description,
            "priority": priority,
            "assigned_agent": assigned_agent or agent_name,
            "created_by": agent_name,
            "tags": tags or [],
        })
        if "error" in result:
            return f"Failed to create task: {result['error']}"
        assignee = result.get("assigned_agent", agent_name)
        return f"Task #{result['id']} created: {title} (assigned to {assignee})"

    @mcp.tool()
    def decompose_project(project_id: int, tasks: list[dict], description: str = "") -> str:
        """Decompose a project into tasks (PRD-style breakdown).

        WHEN TO USE: You've planned a project and want to create all its tasks
        in one shot from a structured breakdown. The AI does the reasoning;
        this tool just persists the result.
        NOT FOR: Creating a single task (use create_task), ad-hoc bulk imports
        (use bulk_create_tasks).

        Args:
            project_id: The project to attach tasks to.
            tasks: List of task dicts — title (required), description, priority
                   (low/normal/high/urgent), tags (list of strings), assigned_agent,
                   milestone_id, sprint_id, blocked_by (list of task IDs).
            description: Optional updated project description to persist.
        """
        if description:
            _api("PUT", f"/projects/{project_id}", {"description": description})

        created = []
        failed = []
        for task in tasks:
            body = {
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "priority": task.get("priority", "normal"),
                "assigned_agent": task.get("assigned_agent", agent_name),
                "created_by": agent_name,
                "tags": task.get("tags", []),
                "project_id": project_id,
            }
            if task.get("milestone_id"):
                body["milestone_id"] = task["milestone_id"]
            if task.get("sprint_id"):
                body["sprint_id"] = task["sprint_id"]
            if task.get("blocked_by"):
                body["blocked_by"] = task["blocked_by"]
            result = _api("POST", "/tasks", body)
            if "error" in result:
                failed.append(f"'{body['title']}': {result['error']}")
            else:
                created.append(f"#{result['id']} {result.get('title', body['title'])}")

        lines = [f"Decomposed project #{project_id} — created {len(created)} tasks:"]
        lines.extend(f"  - {entry}" for entry in created)
        if failed:
            lines.append(f"Failed {len(failed)}:")
            lines.extend(f"  - {entry}" for entry in failed)
        return "\n".join(lines)

    @mcp.tool()
    def bulk_create_tasks(project_id: int, tasks: list[dict]) -> str:
        """Create multiple tasks for a project at once.

        WHEN TO USE: You've decomposed a project into subtasks and want to create
        them all in one shot. Ideal after planning a sprint or milestone breakdown.
        NOT FOR: Creating a single task (use create_task), updating existing tasks.

        Args:
            project_id: The project to attach all tasks to.
            tasks: List of task dicts, each with keys:
                   title (required), description, priority (low/normal/high/urgent),
                   tags (list of strings), assigned_agent.
        """
        created = []
        failed = []
        for task in tasks:
            body = {
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "priority": task.get("priority", "normal"),
                "assigned_agent": task.get("assigned_agent", agent_name),
                "created_by": agent_name,
                "tags": task.get("tags", []),
                "project_id": project_id,
            }
            result = _api("POST", "/tasks", body)
            if "error" in result:
                failed.append(f"'{body['title']}': {result['error']}")
            else:
                created.append(f"#{result['id']} {result.get('title', body['title'])}")

        lines = [f"Created {len(created)} tasks:"]
        lines.extend(f"  - {entry}" for entry in created)
        if failed:
            lines.append(f"Failed {len(failed)}:")
            lines.extend(f"  - {entry}" for entry in failed)
        return "\n".join(lines)

    # ── Presentations ──────────────────────────────────────

    @mcp.tool()
    def get_presentation_template(variant: str = "default") -> str:
        """Return the branded base HTML/CSS shell for building a new presentation.

        WHEN TO USE: Always call this before create_presentation(). It gives you
        the canonical brand template — colors, fonts, nav, transitions — so you
        only need to add slide content. Load the brand-presentation skill first
        for the full style guide.
        NOT FOR: Fetching an existing presentation (use list_presentations).

        Args:
            variant: Template variant — "default" (dark, purple accent),
                     "minimal" (no transitions, print-safe),
                     "figma" (light, Inter font, Figma design aesthetic),
                     "stitch" (Material You, Google Blue, rounded cards).
        """
        _TEMPLATE_DEFAULT = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d0d0f; --surface: #141417; --surface2: #1c1c21;
    --border: #2a2a32; --text: #e8e8f0; --muted: #666680;
    --accent: #7c6af7; --accent2: #f7c56a; --accent3: #6af7b8;
    --font: \'Space Grotesk\', system-ui, sans-serif;
    --mono: \'Space Mono\', monospace;
  }
  html, body { width:100%; height:100%; overflow:hidden; background:var(--bg); color:var(--text); font-family:var(--font); }
  .deck { width:100%; height:100%; position:relative; }
  .slide {
    position:absolute; inset:0; display:flex; flex-direction:column;
    justify-content:center; align-items:center; padding:4rem;
    opacity:0; pointer-events:none;
    transition:opacity 0.4s ease, transform 0.4s ease;
    transform:translateX(40px);
  }
  .slide.active { opacity:1; pointer-events:all; transform:translateX(0); }
  .slide.prev { transform:translateX(-40px); }
  /* Tag chips */
  .tag {
    font-family:var(--mono); font-size:0.7rem; letter-spacing:0.15em;
    text-transform:uppercase; color:var(--accent);
    background:rgba(124,106,247,0.12); border:1px solid rgba(124,106,247,0.25);
    padding:0.25rem 0.75rem; border-radius:999px; margin-bottom:1.5rem;
  }
  /* Typography */
  h1 { font-size:clamp(2rem,5vw,3.5rem); font-weight:700; line-height:1.1; text-align:center; }
  h2 { font-size:clamp(1.5rem,3vw,2.2rem); font-weight:600; line-height:1.2; margin-bottom:1rem; }
  .sub { color:var(--muted); font-size:1.05rem; text-align:center; margin-top:1rem; max-width:540px; line-height:1.6; }
  .highlight  { color:var(--accent);  }
  .highlight2 { color:var(--accent2); }
  .highlight3 { color:var(--accent3); }
  /* Cards */
  .grid { display:grid; gap:1rem; width:100%; max-width:800px; }
  .grid-2 { grid-template-columns:1fr 1fr; }
  .grid-3 { grid-template-columns:1fr 1fr 1fr; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.25rem 1.5rem; }
  .card .icon { font-size:1.5rem; margin-bottom:0.5rem; }
  .card h3 { font-size:0.95rem; font-weight:600; margin-bottom:0.4rem; }
  .card p { font-size:0.82rem; color:var(--muted); line-height:1.55; }
  /* Code */
  .code {
    background:var(--surface); border:1px solid var(--border); border-radius:10px;
    font-family:var(--mono); font-size:0.78rem; line-height:1.7;
    padding:1.25rem 1.5rem; max-width:700px; width:100%; color:#c8c8e0;
  }
  .code .k { color:var(--accent); } .code .s { color:var(--accent3); }
  .code .c { color:var(--muted); } .code .n { color:var(--accent2); }
  /* Flow */
  .flow { display:flex; align-items:center; gap:0.75rem; flex-wrap:wrap; justify-content:center; max-width:760px; }
  .flow-step { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:0.75rem 1.1rem; font-size:0.82rem; text-align:center; min-width:100px; }
  .flow-step .label { font-size:0.7rem; color:var(--muted); margin-top:0.2rem; font-family:var(--mono); }
  .arrow { color:var(--muted); font-size:1.2rem; }
  /* Stats */
  .stats-row { display:flex; gap:3rem; justify-content:center; margin-top:1rem; }
  .stat { text-align:center; }
  .stat .num { font-size:3rem; font-weight:700; color:var(--accent); font-family:var(--mono); }
  .stat .lbl { font-size:0.78rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.1em; margin-top:0.25rem; }
  /* Title bg glow */
  .title-slide { background:radial-gradient(ellipse at 60% 40%,rgba(124,106,247,0.12) 0%,transparent 60%),radial-gradient(ellipse at 20% 80%,rgba(106,247,184,0.06) 0%,transparent 50%); }
  .wordmark { font-family:var(--mono); font-size:0.85rem; color:var(--muted); margin-bottom:2rem; letter-spacing:0.2em; }
  /* Nav */
  .nav {
    position:fixed; bottom:2rem; left:50%; transform:translateX(-50%);
    display:flex; align-items:center; gap:1rem; z-index:100;
    background:var(--surface2); border:1px solid var(--border);
    border-radius:999px; padding:0.5rem 1rem;
  }
  .nav button { background:none; border:none; color:var(--text); cursor:pointer; font-size:1.1rem; padding:0.3rem 0.6rem; border-radius:6px; transition:background 0.15s; }
  .nav button:hover { background:var(--border); }
  .nav button:disabled { color:var(--muted); cursor:default; }
  .counter { font-family:var(--mono); font-size:0.8rem; color:var(--muted); min-width:3rem; text-align:center; }
  .dots { display:flex; gap:0.4rem; align-items:center; }
  .dot { width:6px; height:6px; border-radius:50%; background:var(--border); transition:background 0.2s,transform 0.2s; cursor:pointer; }
  .dot.active { background:var(--accent); transform:scale(1.4); }
</style>
</head>
<body>
<div class="deck" id="deck">

  <!-- SLIDE 1: Title -->
  <div class="slide title-slide active" data-index="0">
    <div class="wordmark">PINKYBOT</div>
    <div class="tag">{{TAG_1}}</div>
    <h1>{{TITLE_LINE_1}}<br><span class="highlight">{{TITLE_LINE_2}}</span></h1>
    <p class="sub">{{SUBTITLE}}</p>
  </div>

  <!-- SLIDE 2: Section -->
  <div class="slide" data-index="1">
    <div class="tag">{{TAG_2}}</div>
    <h2>{{HEADING_2} with a <span class="highlight">key word</span></h2>
    <div class="grid grid-3" style="margin-top:2rem;">
      <div class="card"><div class="icon">📌</div><h3>Point 1</h3><p>Description here.</p></div>
      <div class="card"><div class="icon">📌</div><h3>Point 2</h3><p>Description here.</p></div>
      <div class="card"><div class="icon">📌</div><h3>Point 3</h3><p>Description here.</p></div>
    </div>
  </div>

  <!-- ADD MORE SLIDES HERE following brand-presentation skill patterns -->
  <!-- Patterns: title-slide, section+cards, data/stats, flow diagram, code block, closing -->

  <!-- LAST SLIDE: Closing -->
  <div class="slide title-slide" data-index="2">
    <div class="tag">{{CLOSING_TAG}}</div>
    <h1>{{CLOSING_LINE_1}}<br><span class="highlight3">{{CLOSING_LINE_2}}</span></h1>
    <p class="sub">{{CLOSING_SUBTITLE}}</p>
  </div>

</div>
<div class="nav">
  <button id="prev" onclick="go(-1)" disabled>←</button>
  <div class="dots" id="dots"></div>
  <span class="counter" id="counter"></span>
  <button id="next" onclick="go(1)">→</button>
</div>
<script>
  const slides = document.querySelectorAll(\'.slide\');
  const dotsEl = document.getElementById(\'dots\');
  const counter = document.getElementById(\'counter\');
  let current = 0;
  slides.forEach((_,i) => {
    const d = document.createElement(\'div\');
    d.className = \'dot\' + (i===0?\' active\':\'\');
    d.onclick = () => goTo(i);
    dotsEl.appendChild(d);
  });
  counter.textContent = \'1 / \' + slides.length;
  function goTo(n) {
    slides[current].classList.remove(\'active\');
    slides[current].classList.add(\'prev\');
    setTimeout(() => slides[current].classList.remove(\'prev\'), 400);
    current = n;
    slides[current].classList.add(\'active\');
    document.querySelectorAll(\'.dot\').forEach((d,i) => d.classList.toggle(\'active\',i===current));
    counter.textContent = (current+1) + \' / \' + slides.length;
    document.getElementById(\'prev\').disabled = current===0;
    document.getElementById(\'next\').disabled = current===slides.length-1;
  }
  function go(dir) { const n=current+dir; if(n>=0&&n<slides.length) goTo(n); }
  document.addEventListener(\'keydown\', e => {
    if(e.key===\'ArrowRight\'||e.key===\' \') go(1);
    if(e.key===\'ArrowLeft\') go(-1);
  });
</script>
</body>
</html>'''

        _TEMPLATE_MINIMAL = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{TITLE}}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=Space+Mono&display=swap');
  :root { --bg:#0d0d0f; --surface:#141417; --border:#2a2a32; --text:#e8e8f0; --muted:#666680; --accent:#7c6af7; --accent2:#f7c56a; --accent3:#6af7b8; }
  *, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:\'Space Grotesk\',sans-serif; background:var(--bg); color:var(--text); }
  .slide { min-height:100vh; display:flex; flex-direction:column; justify-content:center; align-items:center; padding:4rem; border-bottom:1px solid var(--border); }
  h1 { font-size:2.5rem; font-weight:700; text-align:center; }
  h2 { font-size:1.8rem; font-weight:600; margin-bottom:1rem; }
  .sub { color:var(--muted); font-size:1rem; text-align:center; margin-top:1rem; max-width:540px; line-height:1.6; }
  .tag { font-family:\'Space Mono\',monospace; font-size:0.7rem; letter-spacing:0.15em; text-transform:uppercase; color:var(--accent); background:rgba(124,106,247,0.12); border:1px solid rgba(124,106,247,0.25); padding:0.25rem 0.75rem; border-radius:999px; margin-bottom:1.5rem; }
  .highlight { color:var(--accent); } .highlight2 { color:var(--accent2); } .highlight3 { color:var(--accent3); }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.25rem 1.5rem; }
  .grid { display:grid; gap:1rem; width:100%; max-width:800px; }
  .grid-2 { grid-template-columns:1fr 1fr; } .grid-3 { grid-template-columns:1fr 1fr 1fr; }
</style>
</head>
<body>
  <!-- Scrollable slides — good for print/PDF export -->
  <!-- Each .slide is a full-viewport section -->
  <div class="slide">
    <div class="tag">{{TAG}}</div>
    <h1>{{TITLE}}</h1>
    <p class="sub">{{SUBTITLE}}</p>
  </div>
  <!-- Add more .slide sections here -->
</body>
</html>'''

        _TEMPLATE_FIGMA = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #FFFFFF; --surface: #F9FAFB; --surface2: #F3F4F6;
    --border: #E5E7EB; --text: #111827; --muted: #6B7280;
    --accent: #7C3AED; --accent-light: rgba(124,58,237,0.08);
    --accent-border: rgba(124,58,237,0.2);
    --font: \'Inter\', system-ui, sans-serif;
  }
  html, body { width:100%; height:100%; overflow:hidden; background:var(--bg); color:var(--text); font-family:var(--font); }
  .deck { width:100%; height:100%; position:relative; }
  .slide {
    position:absolute; inset:0; display:flex; flex-direction:column;
    justify-content:center; align-items:center; padding:4rem;
    opacity:0; pointer-events:none;
    transition:opacity 0.35s ease, transform 0.35s ease;
    transform:translateX(32px);
  }
  .slide.active { opacity:1; pointer-events:all; transform:translateX(0); }
  .slide.prev { transform:translateX(-32px); }
  .slide-inner { width:100%; max-width:860px; }
  .tag {
    display:inline-block; font-family:var(--font); font-size:0.7rem; font-weight:500;
    letter-spacing:0.08em; text-transform:uppercase; color:var(--accent);
    background:var(--accent-light); border:1px solid var(--accent-border);
    padding:0.2rem 0.65rem; border-radius:4px; margin-bottom:1.25rem;
  }
  h1 { font-size:clamp(1.8rem,4vw,3rem); font-weight:700; line-height:1.15; color:var(--text); }
  h2 { font-size:clamp(1.4rem,2.5vw,2rem); font-weight:600; line-height:1.2; color:var(--text); margin-bottom:0.75rem; }
  .sub { color:var(--muted); font-size:1rem; margin-top:0.75rem; max-width:520px; line-height:1.65; }
  .highlight { color:var(--accent); }
  .wordmark { font-size:0.78rem; font-weight:600; letter-spacing:0.12em; text-transform:uppercase; color:var(--muted); margin-bottom:2rem; }
  .title-divider { width:48px; height:3px; background:var(--accent); border-radius:2px; margin:1.25rem 0; }
  .grid { display:grid; gap:1rem; width:100%; margin-top:1.75rem; }
  .grid-3 { grid-template-columns:1fr 1fr 1fr; }
  .card { background:var(--bg); border:1.5px solid var(--border); border-radius:8px; padding:1.25rem 1.5rem; transition:border-color 0.15s, box-shadow 0.15s; }
  .card:hover { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-light); }
  .card .icon { font-size:1.4rem; margin-bottom:0.6rem; }
  .card h3 { font-size:0.9rem; font-weight:600; color:var(--text); margin-bottom:0.35rem; }
  .card p { font-size:0.82rem; color:var(--muted); line-height:1.55; }
  .title-bg { background:linear-gradient(135deg, #faf5ff 0%, #ede9fe 100%); }
  .nav { position:fixed; bottom:1.75rem; left:50%; transform:translateX(-50%); display:flex; align-items:center; gap:0.75rem; z-index:100; background:var(--bg); border:1.5px solid var(--border); border-radius:8px; padding:0.45rem 0.9rem; box-shadow:0 4px 12px rgba(0,0,0,0.08); }
  .nav button { background:none; border:none; color:var(--muted); cursor:pointer; font-size:1rem; padding:0.25rem 0.5rem; border-radius:4px; transition:background 0.15s, color 0.15s; }
  .nav button:hover { background:var(--surface2); color:var(--text); }
  .nav button:disabled { color:var(--border); cursor:default; }
  .counter { font-size:0.78rem; color:var(--muted); min-width:2.5rem; text-align:center; }
  .dots { display:flex; gap:0.35rem; align-items:center; }
  .dot { width:5px; height:5px; border-radius:50%; background:var(--border); transition:background 0.2s,transform 0.2s; cursor:pointer; }
  .dot.active { background:var(--accent); transform:scale(1.5); }
</style>
</head>
<body>
<div class="deck" id="deck">
  <div class="slide title-bg active" data-index="0">
    <div class="slide-inner">
      <div class="wordmark">{{WORDMARK}}</div>
      <div class="tag">{{TAG_1}}</div>
      <h1>{{TITLE_LINE_1}}<br><span class="highlight">{{TITLE_LINE_2}}</span></h1>
      <div class="title-divider"></div>
      <p class="sub">{{SUBTITLE}}</p>
    </div>
  </div>
  <div class="slide" data-index="1">
    <div class="slide-inner">
      <div class="tag">{{TAG_2}}</div>
      <h2>{{HEADING_2}} — <span class="highlight">{{HEADING_2_ACCENT}}</span></h2>
      <div class="grid grid-3">
        <div class="card"><div class="icon">🔷</div><h3>Component A</h3><p>Describe the first component or concept clearly.</p></div>
        <div class="card"><div class="icon">🔶</div><h3>Component B</h3><p>Describe the second component or concept clearly.</p></div>
        <div class="card"><div class="icon">🔵</div><h3>Component C</h3><p>Describe the third component or concept clearly.</p></div>
      </div>
    </div>
  </div>
  <div class="slide title-bg" data-index="2">
    <div class="slide-inner">
      <div class="tag">{{CLOSING_TAG}}</div>
      <h1>{{CLOSING_LINE_1}}<br><span class="highlight">{{CLOSING_LINE_2}}</span></h1>
      <div class="title-divider"></div>
      <p class="sub">{{CLOSING_SUBTITLE}}</p>
    </div>
  </div>
</div>
<div class="nav">
  <button id="prev" onclick="go(-1)" disabled>←</button>
  <div class="dots" id="dots"></div>
  <span class="counter" id="counter"></span>
  <button id="next" onclick="go(1)">→</button>
</div>
<script>
  const slides = document.querySelectorAll(\'.slide\');
  const dotsEl = document.getElementById(\'dots\');
  const counter = document.getElementById(\'counter\');
  let current = 0;
  slides.forEach((_,i) => { const d=document.createElement(\'div\'); d.className=\'dot\'+(i===0?\' active\':\'\'); d.onclick=()=>goTo(i); dotsEl.appendChild(d); });
  counter.textContent = \'1 / \' + slides.length;
  function goTo(n) {
    slides[current].classList.remove(\'active\'); slides[current].classList.add(\'prev\');
    setTimeout(()=>slides[current].classList.remove(\'prev\'),350);
    current=n; slides[current].classList.add(\'active\');
    document.querySelectorAll(\'.dot\').forEach((d,i)=>d.classList.toggle(\'active\',i===current));
    counter.textContent=(current+1)+\' / \'+slides.length;
    document.getElementById(\'prev\').disabled=current===0;
    document.getElementById(\'next\').disabled=current===slides.length-1;
  }
  function go(dir){const n=current+dir;if(n>=0&&n<slides.length)goTo(n);}
  document.addEventListener(\'keydown\',e=>{if(e.key===\'ArrowRight\'||e.key===\' \')go(1);if(e.key===\'ArrowLeft\')go(-1);});
</script>
</body>
</html>'''

        _TEMPLATE_STITCH = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;600;700&family=Roboto:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #FAFAFA; --surface: #FFFFFF; --surface2: #F1F3F4;
    --border: #E8EAED; --text: #202124; --muted: #5F6368;
    --primary: #1A73E8; --primary-light: rgba(26,115,232,0.1);
    --accent-red: #EA4335; --accent-green: #34A853; --accent-yellow: #FBBC04;
    --shadow-1: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.08);
    --shadow-2: 0 3px 8px rgba(0,0,0,0.12), 0 1px 3px rgba(0,0,0,0.08);
    --shadow-3: 0 8px 24px rgba(0,0,0,0.12), 0 2px 6px rgba(0,0,0,0.08);
    --font: \'Google Sans\', \'Roboto\', system-ui, sans-serif;
    --font-body: \'Roboto\', system-ui, sans-serif;
    --radius: 16px;
  }
  html, body { width:100%; height:100%; overflow:hidden; background:var(--bg); color:var(--text); font-family:var(--font); }
  .deck { width:100%; height:100%; position:relative; }
  .slide {
    position:absolute; inset:0; display:flex; flex-direction:column;
    justify-content:center; align-items:center; padding:4rem;
    opacity:0; pointer-events:none;
    transition:opacity 0.4s cubic-bezier(0.4,0,0.2,1), transform 0.4s cubic-bezier(0.4,0,0.2,1);
    transform:translateX(32px);
  }
  .slide.active { opacity:1; pointer-events:all; transform:translateX(0); }
  .slide.prev { transform:translateX(-32px); }
  .slide-inner { width:100%; max-width:880px; }
  .badge { display:inline-flex; align-items:center; font-family:var(--font-body); font-size:0.72rem; font-weight:500; letter-spacing:0.04em; text-transform:uppercase; color:var(--primary); background:var(--primary-light); padding:0.3rem 0.9rem; border-radius:999px; margin-bottom:1.5rem; }
  h1 { font-size:clamp(2rem,4.5vw,3.2rem); font-weight:700; line-height:1.15; color:var(--text); }
  h2 { font-size:clamp(1.5rem,3vw,2.2rem); font-weight:600; line-height:1.2; color:var(--text); margin-bottom:0.75rem; }
  .sub { font-family:var(--font-body); color:var(--muted); font-size:1.05rem; margin-top:1rem; max-width:560px; line-height:1.7; font-weight:300; }
  .highlight { color:var(--primary); } .highlight-green { color:var(--accent-green); }
  .wordmark { font-size:0.8rem; font-weight:600; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted); margin-bottom:2rem; font-family:var(--font-body); }
  .color-bar { display:flex; gap:6px; margin:1.25rem 0 0; }
  .color-bar span { height:4px; border-radius:2px; flex:1; }
  .bar-blue{background:var(--primary);} .bar-red{background:var(--accent-red);} .bar-yellow{background:var(--accent-yellow);} .bar-green{background:var(--accent-green);}
  .grid { display:grid; gap:1.25rem; width:100%; margin-top:2rem; }
  .grid-3 { grid-template-columns:1fr 1fr 1fr; }
  .card { background:var(--surface); border-radius:var(--radius); padding:1.5rem; box-shadow:var(--shadow-1); transition:box-shadow 0.2s, transform 0.2s; }
  .card:hover { box-shadow:var(--shadow-3); transform:translateY(-2px); }
  .card .icon { font-size:1.6rem; margin-bottom:0.75rem; }
  .card h3 { font-size:0.95rem; font-weight:600; color:var(--text); margin-bottom:0.4rem; }
  .card p { font-family:var(--font-body); font-size:0.83rem; color:var(--muted); line-height:1.6; }
  .title-bg { background:linear-gradient(160deg, #e8f0fe 0%, #fce8e6 40%, #e6f4ea 100%); }
  .nav { position:fixed; bottom:1.75rem; left:50%; transform:translateX(-50%); display:flex; align-items:center; gap:0.75rem; z-index:100; background:var(--surface); border-radius:999px; padding:0.5rem 1.1rem; box-shadow:var(--shadow-2); }
  .nav button { background:none; border:none; color:var(--muted); cursor:pointer; font-size:1.05rem; padding:0.3rem 0.6rem; border-radius:999px; transition:background 0.15s, color 0.15s; }
  .nav button:hover { background:var(--primary-light); color:var(--primary); }
  .nav button:disabled { color:var(--border); cursor:default; }
  .counter { font-family:var(--font-body); font-size:0.78rem; color:var(--muted); min-width:2.5rem; text-align:center; }
  .dots { display:flex; gap:0.4rem; align-items:center; }
  .dot { width:6px; height:6px; border-radius:50%; background:var(--border); transition:background 0.2s,transform 0.2s; cursor:pointer; }
  .dot.active { background:var(--primary); transform:scale(1.4); }
</style>
</head>
<body>
<div class="deck" id="deck">
  <div class="slide title-bg active" data-index="0">
    <div class="slide-inner">
      <div class="wordmark">{{WORDMARK}}</div>
      <div class="badge">{{TAG_1}}</div>
      <h1>{{TITLE_LINE_1}}<br><span class="highlight">{{TITLE_LINE_2}}</span></h1>
      <div class="color-bar"><span class="bar-blue"></span><span class="bar-red"></span><span class="bar-yellow"></span><span class="bar-green"></span></div>
      <p class="sub">{{SUBTITLE}}</p>
    </div>
  </div>
  <div class="slide" data-index="1">
    <div class="slide-inner">
      <div class="badge">{{TAG_2}}</div>
      <h2>{{HEADING_2}} — <span class="highlight">{{HEADING_2_ACCENT}}</span></h2>
      <div class="grid grid-3">
        <div class="card"><div class="icon">🔵</div><h3>Feature One</h3><p>A clear, concise description of this feature or concept.</p></div>
        <div class="card"><div class="icon">🔴</div><h3>Feature Two</h3><p>A clear, concise description of this feature or concept.</p></div>
        <div class="card"><div class="icon">🟢</div><h3>Feature Three</h3><p>A clear, concise description of this feature or concept.</p></div>
      </div>
    </div>
  </div>
  <div class="slide title-bg" data-index="2">
    <div class="slide-inner">
      <div class="badge">{{CLOSING_TAG}}</div>
      <h1>{{CLOSING_LINE_1}}<br><span class="highlight-green">{{CLOSING_LINE_2}}</span></h1>
      <div class="color-bar"><span class="bar-blue"></span><span class="bar-red"></span><span class="bar-yellow"></span><span class="bar-green"></span></div>
      <p class="sub">{{CLOSING_SUBTITLE}}</p>
    </div>
  </div>
</div>
<div class="nav">
  <button id="prev" onclick="go(-1)" disabled>←</button>
  <div class="dots" id="dots"></div>
  <span class="counter" id="counter"></span>
  <button id="next" onclick="go(1)">→</button>
</div>
<script>
  const slides = document.querySelectorAll(\'.slide\');
  const dotsEl = document.getElementById(\'dots\');
  const counter = document.getElementById(\'counter\');
  let current = 0;
  slides.forEach((_,i) => { const d=document.createElement(\'div\'); d.className=\'dot\'+(i===0?\' active\':\'\'); d.onclick=()=>goTo(i); dotsEl.appendChild(d); });
  counter.textContent = \'1 / \' + slides.length;
  function goTo(n) {
    slides[current].classList.remove(\'active\'); slides[current].classList.add(\'prev\');
    setTimeout(()=>slides[current].classList.remove(\'prev\'),400);
    current=n; slides[current].classList.add(\'active\');
    document.querySelectorAll(\'.dot\').forEach((d,i)=>d.classList.toggle(\'active\',i===current));
    counter.textContent=(current+1)+\' / \'+slides.length;
    document.getElementById(\'prev\').disabled=current===0;
    document.getElementById(\'next\').disabled=current===slides.length-1;
  }
  function go(dir){const n=current+dir;if(n>=0&&n<slides.length)goTo(n);}
  document.addEventListener(\'keydown\',e=>{if(e.key===\'ArrowRight\'||e.key===\' \')go(1);if(e.key===\'ArrowLeft\')go(-1);});
</script>
</body>
</html>'''

        templates = {
            "default": _TEMPLATE_DEFAULT,
            "minimal": _TEMPLATE_MINIMAL,
            "figma": _TEMPLATE_FIGMA,
            "stitch": _TEMPLATE_STITCH,
        }
        tmpl = templates.get(variant, _TEMPLATE_DEFAULT)
        return (
            f"Brand template ({variant}) — replace all {{{{PLACEHOLDER}}}} values with real content.\n"
            f"See brand-presentation skill for full style guide and slide pattern examples.\n\n"
            f"```html\n{tmpl}\n```"
        )

    @mcp.tool()
    def create_presentation(
        title: str,
        html_content: str,
        description: str = "",
        research_topic_id: int = 0,
        tags: str = "",
    ) -> str:
        """Create a new presentation with full HTML content and publish it to the gallery.

        WHEN TO USE: You've generated a polished HTML presentation and want to
        publish it so the owner can view and share it. Always provide complete,
        self-contained HTML with inline CSS.
        NOT FOR: Updating an existing presentation (use update_presentation).

        Args:
            title: Presentation title shown in the gallery.
            html_content: Complete, self-contained HTML document with inline CSS.
                          May use stable CDN scripts (e.g. reveal.js).
            description: Short description of what this presentation covers.
            research_topic_id: Research topic ID this was generated from (0 = none).
            tags: Comma-separated tags, e.g. "quarterly,finance,charts".
        """
        tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        body = {
            "title": title,
            "html_content": html_content,
            "description": description,
            "created_by": agent_name,
            "tags": tags_list,
        }
        if research_topic_id:
            body["research_topic_id"] = research_topic_id
        result = _api("POST", "/presentations", body)
        if "error" in result:
            return f"Failed to create presentation: {result['error']}"
        return (
            f"Presentation created: #{result['id']} '{result['title']}'\n"
            f"Slug: {result['slug']}\n"
            f"Share token: {result['share_token']}\n"
            f"View at: /p/{result['share_token']}\n"
            f"Version: {result['current_version']}"
        )

    @mcp.tool()
    def update_presentation(
        presentation_id: int,
        html_content: str,
        description: str = "",
    ) -> str:
        """Update an existing presentation with new HTML content (creates a new version).

        WHEN TO USE: You want to revise a presentation you've already published
        or incorporate feedback. Old versions are preserved and can be restored.
        NOT FOR: Creating a new presentation (use create_presentation).

        Args:
            presentation_id: The integer ID of the presentation to update.
            html_content: New complete HTML content replacing the current version.
            description: Optional note describing what changed in this version.
        """
        body = {
            "html_content": html_content,
            "description": description,
            "created_by": agent_name,
        }
        result = _api("PUT", f"/presentations/{presentation_id}", body)
        if "error" in result:
            return f"Failed to update presentation: {result['error']}"
        return (
            f"Presentation #{presentation_id} updated to version {result['current_version']}.\n"
            f"Share token: {result['share_token']}\n"
            f"View at: /p/{result['share_token']}"
        )

    @mcp.tool()
    def list_presentations(limit: int = 20) -> str:
        """List recent presentations in the gallery.

        WHEN TO USE: You want to see what presentations exist before creating a
        new one, or need a presentation ID to update.
        NOT FOR: Creating or updating presentations.
        """
        result = _api("GET", f"/presentations?limit={limit}")
        if "error" in result:
            return f"Failed to fetch presentations: {result['error']}"
        items = result.get("presentations", [])
        if not items:
            return "No presentations found."
        lines = [f"Found {result['count']} presentation(s):"]
        for p in items:
            lines.append(
                f"  #{p['id']} '{p['title']}' by {p['created_by']} "
                f"(v{p['current_version']}, /p/{p['share_token']})"
            )
        return "\n".join(lines)

    # ── Health & Status ────────────────────────────────────

    @mcp.tool()
    def who_am_i() -> str:
        """Check your own identity, model, session, and configuration.

        WHEN TO USE: You need to know what model you're running, your session ID,
        context usage, group memberships, or working directory. Good for
        self-introduction or debugging.
        NOT FOR: Health diagnostics (use check_my_health), context details
        (use context_status).
        """
        agent_info = _api("GET", f"/agents/{agent_name}")
        if "error" in agent_info:
            return f"Name: {agent_name}\n(Could not fetch full details: {agent_info['error']})"

        parts = []
        parts.append(f"Name: {agent_info.get('name', agent_name)}")
        if agent_info.get("display_name"):
            parts.append(f"Display name: {agent_info['display_name']}")
        parts.append(f"Model: {agent_info.get('model', 'unknown')}")
        parts.append(f"Permission mode: {agent_info.get('permission_mode', 'default')}")
        parts.append(f"Working directory: {agent_info.get('working_dir', 'unknown')}")

        groups = agent_info.get("groups", [])
        if groups:
            parts.append(f"Groups: {', '.join(groups)}")

        # Main agent status
        main_resp = _api("GET", "/settings/main-agent")
        main_name = main_resp.get("agent", "") if isinstance(main_resp, dict) else ""
        if main_name == agent_name:
            parts.append("System role: Main Agent (primary system agent)")
        elif main_name:
            parts.append(f"Main agent: {main_name}")
        else:
            parts.append("Main agent: (none set)")

        # Get active session info
        sessions = _api("GET", f"/agents/{agent_name}/sessions")
        if isinstance(sessions, list) and sessions:
            s = sessions[0]
            parts.append(f"Session: {s.get('id', 'unknown')}")
            parts.append(f"Context usage: {s.get('context_used_pct', 0)}%")
            parts.append(f"Messages: {s.get('message_count', 0)}")
        elif isinstance(sessions, dict) and sessions.get("sessions"):
            s = sessions["sessions"][0]
            parts.append(f"Session: {s.get('id', 'unknown')}")
            parts.append(f"Context usage: {s.get('context_used_pct', 0)}%")
            parts.append(f"Messages: {s.get('message_count', 0)}")

        return "\n".join(parts)

    @mcp.tool()
    def get_owner_profile() -> str:
        """Get your owner/operator's profile for personalization.

        WHEN TO USE: You need the owner's name, timezone, pronouns, communication
        style, or identity code word — to personalize messages, schedule things
        in their timezone, or verify identity.
        NOT FOR: Your own config (use who_am_i).
        """
        result = _api("GET", "/settings/owner-profile")
        if "error" in result:
            return f"Could not fetch owner profile: {result['error']}"

        field_labels = {
            "name": "Name",
            "pronouns": "Pronouns",
            "timezone": "Timezone",
            "role": "Role / About",
            "comm_style": "Communication Style",
            "languages": "Languages",
            "code_word": "Identity Code Word",
        }
        parts = []
        for key, label in field_labels.items():
            val = result.get(key, "")
            if val:
                parts.append(f"{label}: {val}")

        if not parts:
            return "No owner profile configured yet."
        return "\n".join(parts)

    @mcp.tool()
    def check_my_health() -> str:
        """Run a full health diagnostic — session, context, heartbeat, tasks, costs.

        WHEN TO USE: Routine health check, or when something feels wrong.
        Returns a recommendation (e.g. "restart needed", "healthy").
        NOT FOR: Just checking context usage (use context_status), or
        your identity (use who_am_i).
        """
        result = _api("GET", f"/agents/{agent_name}/health")
        if "error" in result:
            return f"Health check failed: {result['error']}"

        parts = [f"Agent: {result['agent']} | Recommendation: {result['recommendation']}"]

        session = result.get("session")
        if session:
            parts.append(f"Session: {session['state']} | Context: {session['context_used_pct']}% | Messages: {session['message_count']}")
            if session.get("needs_restart"):
                parts.append("WARNING: Context is full, restart recommended")

        hb = result.get("heartbeat")
        if hb:
            parts.append(f"Heartbeat: {hb['status']} (age: {hb['age_seconds']}s)")

        tasks_info = result.get("tasks", {})
        parts.append(f"Tasks: {tasks_info.get('pending', 0)} pending, {tasks_info.get('in_progress', 0)} in progress, {tasks_info.get('blocked', 0)} blocked")

        costs = result.get("costs", {})
        if costs.get("total_cost_usd", 0) > 0:
            parts.append(f"Cost: ${costs['total_cost_usd']:.4f} ({costs['query_count']} queries)")

        errors = result.get("recent_errors", [])
        if errors:
            parts.append(f"Recent errors: {len(errors)}")

        return "\n".join(parts)

    # ── Context Management ─────────────────────────────────

    @mcp.tool()
    def context_status() -> str:
        """Check context window usage, token counts, cost, and saved state.

        WHEN TO USE: You want to know how full your context is and whether a
        restart is needed. Also shows your saved continuation state. Good for
        deciding whether to call context_restart or save_my_context.
        NOT FOR: Full health check (use check_my_health), identity info (use who_am_i).
        """
        parts = []

        # Streaming session info
        result = _api("GET", f"/agents/{agent_name}/streaming/status")
        if "error" not in result:
            parts.append(f"Session: {result.get('session_id', 'none')} | Connected: {result.get('connected', False)}")

            ctx = result.get("context", {})
            if ctx:
                pct = ctx.get("percentage", 0)
                total = ctx.get("total_tokens", 0)
                max_t = ctx.get("max_tokens", 0)
                parts.append(f"Context: {pct:.1f}% ({total:,}/{max_t:,} tokens)")
                if pct > 70:
                    parts.append("⚠️ Context above 70% — consider calling context_restart")

            stats = result.get("stats", {})
            parts.append(f"Turns: {stats.get('turns', 0)} | Messages sent: {stats.get('messages_sent', 0)}")
            parts.append(f"Cost: ${stats.get('cost_usd', 0):.4f}")
            guard = result.get("saved_context", {})
            if guard.get("restart_safe"):
                parts.append("Restart gate: explicit save is current.")
            elif guard.get("message"):
                parts.append(f"Restart gate: {guard['message']}")
        else:
            parts.append("No streaming session active")

        # Saved continuation context
        saved = _api("GET", f"/agents/{agent_name}/context")
        if saved and saved.get("task"):
            parts.append("")
            parts.append("── Saved State ──")
            freshness = saved.get("freshness", {})
            if saved.get("updated_at"):
                age = freshness.get("age_human") or _format_age(freshness.get("age_seconds"))
                parts.append(f"Saved: {_format_timestamp(saved.get('updated_at'))} ({age} ago)")
            if freshness.get("stale_warning"):
                parts.append("WARNING: Saved context is old. Verify it before relying on it.")
            if saved.get("task"):
                parts.append(f"Task: {saved['task']}")
            if saved.get("context"):
                parts.append(f"Context: {saved['context']}")
            if saved.get("notes"):
                parts.append(f"Notes: {saved['notes']}")
            blockers = saved.get("blockers", [])
            if blockers:
                parts.append(f"Blockers: {', '.join(blockers)}")
            priority = saved.get("priority_items", [])
            if priority:
                parts.append(f"Priority: {', '.join(priority)}")
        else:
            parts.append("\nNo saved continuation state.")

        return "\n".join(parts)

    @mcp.tool()
    def context_restart() -> str:
        """Restart your session with a fresh context window.

        WHEN TO USE: context_status shows >70% usage, or you need a clean slate.
        Conversation history is lost but soul/personality and MCP tools persist.
        IMPORTANT: You MUST call save_my_context() first — the restart guard
        blocks restart without a recent save.
        NOT FOR: Just saving state (use save_my_context), or sleeping (use request_sleep).
        """
        result = _api("POST", f"/agents/{agent_name}/streaming/restart")
        if "error" in result:
            return f"Restart failed: {result.get('error', 'unknown')}"

        old_id = result.get("old_session_id", "")
        old_turns = result.get("old_turns", 0)
        return f"Context restarted. Previous session: {old_id} ({old_turns} turns). Fresh context ready."

    # ── Sleep/Wake Control ─────────────────────────────────

    @mcp.tool()
    def request_sleep(wake_cron: str = "", wake_prompt: str = "") -> str:
        """Shut down your session to conserve resources until next wake.

        WHEN TO USE: You have no pending work and want to save compute. Closes
        your session entirely. You'll wake via existing schedules, inbound
        messages, or the optional wake_cron you provide here.
        IMPORTANT: Requires a recent save_my_context() call.
        NOT FOR: Context restart (use context_restart — keeps you running).

        Args:
            wake_cron: Optional cron expression for when to wake up (e.g. "0 8 * * *").
            wake_prompt: What to do when woken from this sleep.
        """
        # Set a wake schedule if requested
        if wake_cron:
            _api("POST", f"/agents/{agent_name}/schedules", {
                "name": "sleep_wake",
                "cron": wake_cron,
                "prompt": wake_prompt or "Waking from requested sleep. Check context and resume.",
            })

        # Request deep sleep
        result = _api("POST", f"/agents/{agent_name}/sleep")
        if "error" in result:
            return f"Sleep request failed: {result['error']}"

        sessions_closed = result.get("sessions_closed", 0)
        wake_info = f" Wake scheduled: {wake_cron}" if wake_cron else " No wake scheduled — rely on existing schedules."
        return f"Entering deep sleep. {sessions_closed} session(s) closed. Context preserved.{wake_info}"

    @mcp.tool()
    def send_heartbeat(status: str = "alive") -> str:
        """Signal that you're still alive and working.

        WHEN TO USE: During long-running tasks — call periodically so the
        system doesn't mark you as stuck or dead. Use "busy" when deep in work,
        "finishing" when wrapping up.

        Args:
            status: Your status — "alive" (default), "busy", or "finishing".
        """
        result = _api("POST", f"/agents/{agent_name}/heartbeat", {
            "session_id": f"{agent_name}-main",
            "status": status,
        })
        if "error" in result:
            return f"Heartbeat failed: {result['error']}"
        return f"Heartbeat sent: {status}"

    # ── Research Pipeline ─────────────────────────────────

    @mcp.tool()
    def submit_research_brief(
        topic_id: int,
        content: str,
        summary: str = "",
        sources: str = "",
        key_findings: str = "",
    ) -> str:
        """Submit your research findings for an assigned topic.

        WHEN TO USE: You've completed research on a topic assigned to you and
        want to submit the brief for review or publication.

        Args:
            topic_id: ID of the research topic
            content: Full markdown content of the research brief
            summary: One-paragraph summary of findings
            sources: Comma-separated list of sources consulted
            key_findings: Comma-separated list of key findings
        """
        sources_list = [s.strip() for s in sources.split(",") if s.strip()] if sources else []
        findings_list = [f.strip() for f in key_findings.split(",") if f.strip()] if key_findings else []
        result = _api("POST", f"/research/{topic_id}/brief", {
            "author_agent": agent_name,
            "content": content,
            "summary": summary,
            "sources": sources_list,
            "key_findings": findings_list,
        })
        if "error" in result:
            return f"Failed to submit brief: {result['error']}"
        return f"Brief submitted for topic {topic_id} (brief_id={result.get('id', '?')}, version {result.get('version', 1)}). Status: {result.get('status', 'draft')}"

    @mcp.tool()
    def submit_research_review(
        topic_id: int,
        brief_id: int,
        verdict: str = "approve",
        comments: str = "",
        confidence: int = 3,
    ) -> str:
        """Review another agent's research brief (approve/request changes/reject).

        WHEN TO USE: A research topic is in_review and you're assigned as a
        reviewer, or you want to provide feedback on a brief.

        Args:
            topic_id: ID of the research topic
            brief_id: ID of the specific brief to review
            verdict: Your verdict — approve, request_changes, or reject
            comments: Your review comments (markdown)
            confidence: How confident you are in your review (1-5)
        """
        result = _api("POST", f"/research/{topic_id}/reviews", {
            "brief_id": brief_id,
            "reviewer_agent": agent_name,
            "verdict": verdict,
            "comments": comments,
            "confidence": confidence,
        })
        if "error" in result:
            return f"Failed to submit review: {result['error']}"
        return f"Review submitted for brief {brief_id}: {verdict}"

    @mcp.tool()
    def get_my_research_assignments() -> str:
        """See your research assignments and open topics you can claim.

        WHEN TO USE: Checking what research work is waiting for you, or looking
        for open topics to volunteer for. Shows your assignments, review duties,
        and unclaimed topics.
        """
        all_topics = _api("GET", "/research")
        if "error" in all_topics:
            return f"Failed to fetch: {all_topics['error']}"

        topics = all_topics.get("topics", [])
        assigned = [t for t in topics if t.get("assigned_agent") == agent_name]
        reviewing = [t for t in topics if agent_name in (t.get("reviewer_agents") or [])]
        open_topics = [t for t in topics if t.get("status") == "open" and not t.get("assigned_agent")]
        in_review = [t for t in topics if t.get("status") == "in_review" and agent_name not in (t.get("reviewer_agents") or []) and t.get("assigned_agent") != agent_name]

        parts = []
        if assigned:
            parts.append("ASSIGNED TO YOU:")
            for t in assigned:
                parts.append(f"  [{t['id']}] {t['title']} (status: {t['status']}, priority: {t['priority']})")
        if reviewing:
            parts.append("ASSIGNED TO REVIEW:")
            for t in reviewing:
                parts.append(f"  [{t['id']}] {t['title']} (status: {t['status']})")
        if open_topics:
            parts.append("OPEN — AVAILABLE TO CLAIM:")
            for t in open_topics:
                parts.append(f"  [{t['id']}] {t['title']} — {t.get('description','')[:80]} (priority: {t['priority']})")
        if in_review:
            parts.append("NEEDS REVIEWERS:")
            for t in in_review:
                parts.append(f"  [{t['id']}] {t['title']} (researcher: {t.get('assigned_agent','')})")
        if not assigned and not reviewing and not open_topics and not in_review:
            parts.append("No research assignments or open topics.")
        return "\n".join(parts)

    @mcp.tool()
    def claim_research_topic(topic_id: int) -> str:
        """Volunteer to research an open topic by assigning it to yourself.

        WHEN TO USE: get_my_research_assignments showed an open topic you want
        to work on.

        Args:
            topic_id: ID of the open topic to claim
        """
        result = _api("POST", f"/research/{topic_id}/assign", {
            "agent_name": agent_name,
        })
        if "error" in result:
            return f"Failed to claim topic: {result['error']}"
        return f"Claimed topic [{topic_id}] '{result.get('title', '')}'. Status: {result.get('status', 'assigned')}. Start researching!"

    @mcp.tool()
    def create_research_topic(
        title: str,
        description: str = "",
        priority: str = "normal",
        tags: str = "",
        scope: str = "",
        auto_assign: bool = True,
    ) -> str:
        """Create a new research topic and optionally self-assign it.

        WHEN TO USE: You've identified something worth researching — a question,
        trend, or investigation. Creates the topic in the pipeline for you or
        another agent to work on.

        Args:
            title: Research question or topic title
            description: Full description of what to research
            priority: low, normal, high, or urgent
            tags: Comma-separated tags for categorization
            scope: Boundaries or constraints for the research
            auto_assign: If true, automatically assign the topic to yourself
        """
        tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        result = _api("POST", "/research", {
            "title": title,
            "description": description,
            "submitted_by": agent_name,
            "priority": priority,
            "tags": tags_list,
            "scope": scope,
        })
        if "error" in result:
            return f"Failed to create topic: {result['error']}"

        topic_id = result.get("id")
        msg = f"Created research topic [{topic_id}] '{title}' (priority: {priority})"

        if auto_assign and topic_id:
            assign_result = _api("POST", f"/research/{topic_id}/assign", {
                "agent_name": agent_name,
            })
            if "error" not in assign_result:
                msg += " — assigned to you. Start researching!"
            else:
                msg += f" — auto-assign failed: {assign_result['error']}"

        return msg

    @mcp.tool()
    def publish_research(topic_id: int) -> str:
        """Publish a research topic directly, skipping peer review.

        WHEN TO USE: The research is time-sensitive, solo, or already validated
        and doesn't need formal peer review. Publishes the latest brief as final.
        NOT FOR: Research that should be reviewed first (use submit_research_brief
        and let reviewers weigh in).

        Args:
            topic_id: ID of the research topic to publish
        """
        result = _api("POST", f"/research/{topic_id}/publish")
        if "error" in result:
            return f"Failed to publish: {result['error']}"
        return f"Published topic [{topic_id}]. Brief is now final."

    @mcp.tool()
    def list_research_topics(
        status: str = "",
        limit: int = 20,
    ) -> str:
        """Browse all research topics, optionally filtered by status.

        WHEN TO USE: You want an overview of the research pipeline — all topics
        across all agents. Use status filter to narrow (open, assigned, in_review,
        published, archived).

        Args:
            status: Filter by status (open, assigned, in_review, published, archived). Empty = all.
            limit: Max results to return.
        """
        params = f"?limit={limit}"
        if status:
            params += f"&status={status}"
        result = _api("GET", f"/research{params}")
        if "error" in result:
            return f"Failed to list: {result['error']}"

        topics = result.get("topics", [])
        if not topics:
            return "No research topics found."

        lines = []
        for t in topics:
            brief_count = t.get("brief_count", 0)
            lines.append(
                f"[{t['id']}] {t['title']} — {t['status']} "
                f"(priority: {t.get('priority', 'medium')}, "
                f"briefs: {brief_count}, "
                f"by: {t.get('assigned_agent') or 'unassigned'})"
            )
        return "\n".join(lines)

    @mcp.tool()
    def get_research_detail(topic_id: int) -> str:
        """Get full detail on a research topic — briefs, reviews, and metadata.

        WHEN TO USE: You need the complete picture of a specific research topic
        before reviewing, continuing work, or sharing findings.

        Args:
            topic_id: ID of the research topic.
        """
        result = _api("GET", f"/research/{topic_id}")
        if "error" in result:
            return f"Failed to get topic: {result['error']}"

        topic = result.get("topic", {})
        briefs = result.get("briefs", [])
        reviews = result.get("reviews", [])

        parts = [
            f"# [{topic['id']}] {topic['title']}",
            f"Status: {topic['status']} | Priority: {topic.get('priority', 'medium')}",
            f"Researcher: {topic.get('assigned_agent') or 'unassigned'}",
            f"Description: {topic.get('description', 'None')}",
        ]

        if briefs:
            parts.append(f"\n## Briefs ({len(briefs)} version(s))")
            for b in briefs:
                parts.append(f"  brief_id={b.get('id', '?')} v{b.get('version', '?')} — {b.get('status', '?')} ({b.get('created_at', '')})")
                if b.get("summary"):
                    parts.append(f"    Summary: {b['summary'][:200]}")
                if b.get("key_findings"):
                    parts.append(f"    Key findings: {b['key_findings'][:200]}")

        if reviews:
            parts.append(f"\n## Reviews ({len(reviews)})")
            for r in reviews:
                parts.append(f"  {r.get('reviewer', '?')}: {r.get('verdict', '?')} (confidence: {r.get('confidence', '?')})")
                if r.get("comments"):
                    parts.append(f"    {r['comments'][:150]}")

        return "\n".join(parts)

    @mcp.tool()
    def export_research_pdf(topic_id: int) -> str:
        """Export a research brief as a formatted PDF.

        WHEN TO USE: You want to share research findings as a PDF — generates
        from the latest brief including peer reviews. Returns a file path you
        can send via send_document from pinky-messaging.
        NOT FOR: General markdown-to-PDF (use render_pdf).

        Args:
            topic_id: ID of the research topic to export
        """
        import urllib.error as _ue
        import urllib.request as _ur
        url = f"{api_url}/research/{topic_id}/export?format=pdf"
        try:
            req = _ur.Request(url, method="GET")
            with _ur.urlopen(req, timeout=60) as resp:
                # The API returns a file response — save it locally
                content_disp = resp.headers.get("content-disposition", "")
                filename = f"research_{topic_id}.pdf"
                if "filename=" in content_disp:
                    filename = content_disp.split("filename=")[-1].strip('"')
                import os
                export_dir = os.path.join(os.path.dirname(api_url.replace("http://localhost:8888", ".")), "data", "exports")
                os.makedirs(export_dir, exist_ok=True)
                path = os.path.join(export_dir, filename)
                with open(path, "wb") as f:
                    f.write(resp.read())
                return json.dumps({"success": True, "path": os.path.abspath(path), "filename": filename})
        except _ue.HTTPError as e:
            return json.dumps({"error": e.read().decode(), "status": e.code})
        except Exception as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    def render_pdf(
        content: str,
        filename: str = "document.pdf",
        title: str = "Document",
    ) -> str:
        """Convert arbitrary markdown text into a PDF file.

        WHEN TO USE: You need to generate a PDF from any markdown content —
        reports, summaries, documentation. Returns a file path you can send
        via send_document from pinky-messaging.
        NOT FOR: Research briefs (use export_research_pdf for those).

        Args:
            content: Markdown content to render as PDF.
            filename: Output filename (default: document.pdf).
            title: Document title for the PDF header.
        """
        result = _api("POST", "/render/pdf", {
            "content": content,
            "filename": filename,
            "title": title,
        })
        if "error" in result:
            return json.dumps({"error": f"Failed to render PDF: {result['error']}"})
        return json.dumps(result)

    # ── Inter-Agent Communication ───────────────────────────

    @mcp.tool()
    def send_to_agent(
        to: str,
        message: str,
        content_type: str = "text",
        reply_to: int | None = None,
        priority: str = "normal",
    ) -> str:
        """Send a direct message to another agent (delivered or queued).

        WHEN TO USE: You need to communicate with another agent -- ask a question,
        share findings, coordinate work, or REPLY to a message you received from
        them. Goes into their context like a user message. If they're offline,
        it's queued in their inbox.
        IMPORTANT: When you receive a message from another agent, always reply via
        send_to_agent -- do NOT message the owner unless the situation specifically
        requires human attention. For system-level issues, escalate to the main
        agent instead.
        NOT FOR: Delegating tracked work (use create_task), sending files
        (use send_file_to_agent), messaging a human user (use send from pinky-messaging).

        Args:
            to: The agent name to message (e.g., "barsik", "oleg").
            message: The message content.
            content_type: Message type — "text", "task_request", "task_response", "status".
            reply_to: Optional message ID to reply to (for threading).
            priority: "normal", "high", or "urgent".
        """
        priority_map = {"normal": 0, "high": 1, "urgent": 2}
        result = _api("POST", f"/agents/{to}/message", {
            "from_agent": agent_name,
            "message": message,
            "content_type": content_type,
            "parent_message_id": reply_to,
            "priority": priority_map.get(priority, 0),
        })
        if "error" in result:
            return f"Failed to message {to}: {result.get('error', 'unknown')}"
        if result.get("queued"):
            return f"Message queued for {to} (they're offline). They'll see it in their inbox when they wake up."
        return f"Message delivered to {to}."

    @mcp.tool()
    def send_file_to_agent(
        to_agent: str,
        file_path: str,
        description: str = "",
    ) -> str:
        """Transfer a file to another agent via shared directory.

        WHEN TO USE: You need to share a file (research PDF, code, data) with
        another agent. Copies it to a shared location and notifies them.
        NOT FOR: Sending text messages (use send_to_agent), sending files to
        a human user (use send_document from pinky-messaging).

        Args:
            to_agent: Name of the recipient agent/session.
            file_path: Absolute path to the file to send.
            description: Optional description of what the file is.
        """
        if not os.path.isabs(file_path):
            return json.dumps({"error": "file_path must be an absolute path"})
        if not os.path.isfile(file_path):
            return json.dumps({"error": f"File not found: {file_path}"})

        # Use the API to send the file transfer
        result = _api("POST", f"/agents/{to_agent}/file", {
            "from_agent": agent_name,
            "file_path": file_path,
            "description": description,
        })
        if "error" not in result:
            return json.dumps({
                "sent": True,
                "to": to_agent,
                "file_name": result.get("file_name", os.path.basename(file_path)),
                "transferred_path": result.get("transferred_path", ""),
            })

        # Fallback: do the transfer locally via AgentComms
        try:
            from pinky_daemon.agent_comms import AgentComms
            comms = AgentComms()
            msg = comms.send_file(
                from_session=agent_name,
                to_session=to_agent,
                file_path=file_path,
                description=description,
            )
            comms.close()
            return json.dumps({
                "sent": True,
                "to": to_agent,
                "file_name": msg.metadata.get("file_name", os.path.basename(file_path)),
                "transferred_path": msg.metadata.get("file_path", ""),
            })
        except FileNotFoundError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"File transfer failed: {e}"})

    @mcp.tool()
    def list_agents() -> str:
        """List all other agents and whether they're online.

        WHEN TO USE: You want to see who else is available — before delegating
        work, coordinating, or checking the multi-agent landscape.
        NOT FOR: Checking one specific agent (use agent_status)."""
        result = _api("GET", "/agents")
        if "error" in result:
            return f"Failed to list agents: {result['error']}"
        agents_list = result if isinstance(result, list) else result.get("agents", [])
        main_name = result.get("main_agent", "") if isinstance(result, dict) else ""
        if not agents_list:
            return "No agents found."

        # Fetch presence info
        presence_result = _api("GET", "/agents/presence")
        presence_map = {}
        if "error" not in presence_result:
            for p in presence_result.get("agents", []):
                presence_map[p["agent"]] = p.get("status", "unknown")

        parts = []
        for a in agents_list:
            name = a.get("name", "?")
            if name == agent_name:
                continue  # Skip self
            model = a.get("model", "?")
            role = a.get("role", "")
            status = a.get("status", "active")
            presence = presence_map.get(name, "unknown")
            display = a.get("display_name", name)
            line = f"- {display} ({name}) | model: {model} | {presence}"
            if name == main_name:
                line += " | [main]"
            if role:
                line += f" | role: {role}"
            if status != "active":
                line += f" | {status}"
            parts.append(line)
        return "\n".join(parts) if parts else "No other agents found."

    @mcp.tool()
    def check_inbox(unread_only: bool = True, limit: int = 20) -> str:
        """Read messages from other agents (queued while you were offline or asleep).

        WHEN TO USE: On wake-up (after load_my_context), or periodically during
        work to see if another agent sent you something. Messages are auto-marked
        as read when retrieved.
        NOT FOR: Checking messages from human users (those arrive in your context
        via the broker), or searching past conversations (use search_history).

        Args:
            unread_only: Only show unread messages (default True).
            limit: Maximum messages to return (default 20).
        """
        result = _api(
            "GET",
            f"/sessions/{agent_name}/inbox?unread_only={'true' if unread_only else 'false'}&limit={limit}",
        )
        if "error" in result:
            return f"Failed to check inbox: {result.get('error', 'unknown')}"

        messages = result.get("messages", [])
        unread = result.get("unread", 0)

        if not messages:
            return "Inbox empty — no unread messages."

        # Auto-mark retrieved messages as read
        msg_ids = [m["id"] for m in messages]
        _api("POST", f"/sessions/{agent_name}/inbox/read", msg_ids)

        parts = [f"Inbox: {len(messages)} message(s) ({unread} unread total)\n"]
        for m in messages:
            sender = m.get("from", "?")
            content = m.get("content", "")
            msg_type = m.get("content_type", m.get("type", "text"))
            priority = m.get("priority", 0)
            msg_id = m.get("id", "")
            parent = m.get("parent_message_id")

            header = f"[#{msg_id}] From {sender}"
            if msg_type != "text":
                header += f" ({msg_type})"
            if priority > 0:
                header += f" {'!' * priority}PRIORITY"
            if parent:
                header += f" (reply to #{parent})"

            parts.append(f"{header}\n{content}\n")
        return "\n".join(parts)

    @mcp.tool()
    def agent_status(name: str) -> str:
        """Check if a specific agent is online before messaging or delegating.

        WHEN TO USE: You want to know if a specific agent is available before
        sending them a message or task. Shows presence and last seen time.
        NOT FOR: Listing all agents (use list_agents).

        Args:
            name: The agent name to check (e.g., "barsik").
        """
        result = _api("GET", f"/agents/{name}/presence")
        if "error" in result:
            return f"Agent '{name}' not found."
        status = result.get("status", "unknown")
        display = result.get("display_name", name)
        streaming = result.get("streaming", False)
        last_seen = result.get("last_seen", 0)

        line = f"{display} ({name}): {status}"
        if streaming:
            line += " (streaming session active)"
        if last_seen:
            dt = datetime.fromtimestamp(last_seen, tz=timezone.utc)
            line += f" | last seen: {dt.strftime('%Y-%m-%d %H:%M UTC')}"
        return line

    @mcp.tool()
    def get_agent_card(name: str) -> str:
        """See another agent's capabilities, role, model, and groups.

        WHEN TO USE: You need to know what another agent can do before
        delegating work or asking for help — their role, model, and capabilities.
        NOT FOR: Just checking if they're online (use agent_status).

        Args:
            name: The agent name (e.g., "barsik").
        """
        result = _api("GET", f"/agents/{name}/card")
        if "error" in result:
            return f"Agent '{name}' not found."
        parts = [
            f"Agent: {result.get('display_name', name)} ({result.get('name', name)})",
            f"Role: {result.get('role', 'none')}",
            f"Model: {result.get('model', '?')}",
            f"Status: {result.get('status', 'unknown')}",
        ]
        groups = result.get("groups", [])
        if groups:
            parts.append(f"Groups: {', '.join(groups)}")
        caps = result.get("capabilities", [])
        if caps:
            parts.append("Capabilities:")
            for c in caps:
                parts.append(f"  - {c}")
        return "\n".join(parts)

    # ── Conversation History Search ───────────────────────

    @mcp.tool()
    def search_history(query: str, context_messages: int = 3) -> str:
        """Search your past conversation messages by keyword.

        WHEN TO USE: You need to find something said in a previous conversation —
        a decision, instruction, or detail from your chat history.
        NOT FOR: Long-term memory search (use recall from pinky-memory), or
        checking agent-to-agent messages (use check_inbox).

        Args:
            query: Search term to find in past messages.
            context_messages: Number of messages before/after each match to include (default 3).
        """
        # Search across all agent sessions via the agent chat-history endpoint
        result = _api("GET", f"/agents/{agent_name}/chat-history?q={query}&limit=30")
        messages = result.get("messages", [])
        if not messages:
            return f"No messages found matching '{query}'."

        # Format as conversation snippets
        parts = [f"Found {len(messages)} message(s) matching '{query}':"]
        for msg in messages[:15]:
            role = msg.get("role", "?")
            content = (msg.get("content", "") or "")[:300]
            ts = msg.get("timestamp", 0)
            time_str = ""
            if ts:
                from datetime import datetime
                time_str = f" ({datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')})"
            parts.append(f"\n[{role}]{time_str} {content}")
        return "\n".join(parts)

    # ── Skill Management ──────────────────────────────────

    @mcp.tool()
    def list_my_skills() -> str:
        """See what skills you currently have equipped.

        WHEN TO USE: You want to check which skills are active and available
        to you, including their categories and how they were assigned.
        NOT FOR: Browsing skills you don't have (use list_available_skills).
        """
        result = _api("GET", f"/agents/{agent_name}/skills")
        if "error" in result:
            return f"Failed to list skills: {result['error']}"

        skill_list = result.get("skills", [])
        if not skill_list:
            return "No skills assigned."

        parts = [f"You have {len(skill_list)} active skill(s):\n"]
        for s in skill_list:
            name = s.get("name", "?")
            desc = s.get("description", "")
            cat = s.get("category", "general")
            assigned = s.get("assigned_by", "?")
            skill_type = s.get("skill_type", "?")
            line = f"- **{name}** [{cat}] ({skill_type}, assigned by: {assigned})"
            if desc:
                line += f"\n  {desc}"
            parts.append(line)
        return "\n".join(parts)

    @mcp.tool()
    def list_available_skills(category: str = "") -> str:
        """Browse skills you don't have yet that you can self-assign.

        WHEN TO USE: You need a capability you don't currently have, or want
        to explore what's available. Shows self-assignable skills only.
        NOT FOR: Checking your current skills (use list_my_skills), or loading
        a skill's instructions (use load_skill).

        Args:
            category: Filter by category (e.g. 'productivity', 'development'). Empty = all.
        """
        params = "self_assignable=true"
        if category:
            params += f"&category={category}"
        result = _api("GET", f"/agents/{agent_name}/skills/available?{params}")
        if "error" in result:
            return f"Failed to list available skills: {result['error']}"

        skill_list = result.get("skills", [])
        if not skill_list:
            return "No additional skills available to add." + (
                f" (filtered by category: {category})" if category else ""
            )

        parts = [f"{len(skill_list)} skill(s) available to add:\n"]
        for s in skill_list:
            name = s.get("name", "?")
            desc = s.get("description", "")
            cat = s.get("category", "general")
            requires = s.get("requires", [])
            line = f"- **{name}** [{cat}]"
            if desc:
                line += f" — {desc}"
            if requires:
                line += f"\n  Requires: {', '.join(requires)}"
            parts.append(line)
        return "\n".join(parts)

    @mcp.tool()
    def load_skill(skill_name: str) -> str:
        """Load a skill's full instructions into your context.

        WHEN TO USE: Your CLAUDE.md lists a skill by name/description but you
        need the full body to actually use it. Call this before executing a
        skill's workflow for the first time in a session.
        NOT FOR: Adding a new skill to yourself (use add_skill).

        Args:
            skill_name: The skill name from your Available Skills list.
        """
        result = _api("GET", f"/skills/{skill_name}")
        if "error" in result:
            status = result.get("status", 500)
            if status == 404:
                return f"Skill '{skill_name}' not found."
            return f"Failed to load skill: {result['error']}"

        directive = result.get("directive", "")
        if not directive:
            return f"Skill '{skill_name}' has no instructions body."

        desc = result.get("description", "")
        header = f"# Skill: {skill_name}"
        if desc:
            header += f"\n{desc}"
        return f"{header}\n\n{directive}"

    @mcp.tool()
    def add_skill(skill_name: str) -> str:
        """Equip a skill from the catalog and restart to activate its tools.

        WHEN TO USE: You found a skill via list_available_skills that you need.
        Only works for self-assignable skills. Session restarts automatically
        to activate new MCP tools — context is auto-saved.
        NOT FOR: Just reading a skill's instructions (use load_skill).

        Args:
            skill_name: The skill name from list_available_skills().
        """
        # Assign the skill
        result = _api(
            "POST",
            f"/agents/{agent_name}/skills/{skill_name}",
            {"assigned_by": "self"},
        )
        if "error" in result:
            status = result.get("status", 500)
            error = result.get("error", "Unknown error")
            if status == 403:
                return f"Cannot add '{skill_name}' — it is not self-assignable."
            if status == 400:
                return f"Cannot add '{skill_name}' — {error}"
            if status == 404:
                return f"Skill '{skill_name}' not found in the catalog."
            return f"Failed to add skill: {error}"

        # Apply changes (this will restart the session)
        apply_result = _api("POST", f"/agents/{agent_name}/skills/apply")
        if "error" in apply_result:
            return (
                f"Skill '{skill_name}' was assigned but failed to apply: {apply_result['error']}. "
                f"It will take effect on next session restart."
            )

        restarted = apply_result.get("session_restarted", False)
        tools = apply_result.get("tool_patterns", [])
        return (
            f"Skill '{skill_name}' added successfully.\n"
            f"New tool patterns: {', '.join(tools) if tools else 'none'}\n"
            + ("Session is restarting to activate new tools. Your context was auto-saved."
               if restarted else "Changes applied. Session did not need restart.")
        )

    @mcp.tool()
    def remove_skill(skill_name: str) -> str:
        """Unequip a skill and restart to deactivate its tools.

        WHEN TO USE: You no longer need a skill and want to reduce tool clutter.
        Cannot remove core skills (pinky-memory, pinky-self, pinky-messaging, file-access).
        Session restarts to deactivate MCP tools.

        Args:
            skill_name: The skill to remove.
        """
        # Remove the skill
        result = _api("DELETE", f"/agents/{agent_name}/skills/{skill_name}")
        if "error" in result:
            status = result.get("status", 500)
            error = result.get("error", "Unknown error")
            if status == 400:
                return f"Cannot remove '{skill_name}' — {error}"
            if status == 404:
                return f"Skill '{skill_name}' is not assigned to you."
            return f"Failed to remove skill: {error}"

        # Apply changes
        apply_result = _api("POST", f"/agents/{agent_name}/skills/apply")
        if "error" in apply_result:
            return (
                f"Skill '{skill_name}' was removed but failed to apply: {apply_result['error']}. "
                f"It will take effect on next session restart."
            )

        restarted = apply_result.get("session_restarted", False)
        return (
            f"Skill '{skill_name}' removed.\n"
            + ("Session is restarting. Your context was auto-saved."
               if restarted else "Changes applied.")
        )

    @mcp.tool()
    def discover_skills() -> str:
        """Scan for new SKILL.md files and plugins on the filesystem.

        WHEN TO USE: New skills were added to the skills directory (manually or
        via git) and need to be registered in the catalog. Also discovers plugins.
        NOT FOR: Installing from a git URL (use install_skill), or creating from
        scratch (use create_skill).
        """
        # Discover SKILL.md files
        skill_result = _api("POST", "/skills/discover")
        if "error" in skill_result:
            return f"Failed to discover skills: {skill_result['error']}"

        # Discover plugins
        plugin_result = _api("POST", "/plugins/discover")

        parts = ["Skill discovery complete.\n"]

        discovered = skill_result.get("discovered", 0)
        registered = skill_result.get("registered", [])
        updated = skill_result.get("updated", [])
        skipped = skill_result.get("skipped", [])

        parts.append(f"SKILL.md files scanned: {discovered}")
        if registered:
            parts.append(f"New skills registered: {', '.join(registered)}")
        if updated:
            parts.append(f"Skills updated: {', '.join(updated)}")
        if skipped:
            parts.append(f"Unchanged (skipped): {len(skipped)}")

        if "error" not in plugin_result:
            plugin_found = plugin_result.get("discovered", [])
            if plugin_found:
                parts.append(f"\nPlugins discovered: {', '.join(plugin_found)}")
            else:
                parts.append("\nNo new plugins found.")

        return "\n".join(parts)

    @mcp.tool()
    def install_skill(url: str) -> str:
        """Clone a skill from a git repo, register it, and assign it to you.

        WHEN TO USE: You want to install a published skill from GitHub or another
        git host. Clones, finds SKILL.md files (agentskills.io standard),
        registers them, and assigns them to you.
        NOT FOR: Skills already on disk (use discover_skills), or creating a
        new skill from scratch (use create_skill).

        Examples:
            install_skill("https://github.com/anthropics/skills")
            install_skill("https://github.com/someone/my-skill")
            install_skill("https://github.com/org/skills/tree/main/data-analysis")

        Args:
            url: Git repository URL (GitHub, GitLab, etc.)
        """
        result = _api("POST", "/skills/from-git", {
            "url": url,
            "agent_name": agent_name,
        })

        if "error" in result:
            status = result.get("status", 500)
            error = result.get("error", "Unknown error")
            if status == 400:
                return f"Failed: {error}"
            return f"Failed to install: {error}"

        repo = result.get("repo", "?")
        registered = result.get("registered", [])
        updated = result.get("updated", [])
        assigned = result.get("assigned_skills", [])
        total = len(registered) + len(updated)

        if total == 0:
            return f"Cloned {repo} but no new skills found."

        parts = [f"Installed {total} skill(s) from {repo}:"]
        if registered:
            parts.append(f"  New: {', '.join(registered)}")
        if updated:
            parts.append(f"  Updated: {', '.join(updated)}")
        if assigned:
            parts.append(f"Assigned to you: {', '.join(assigned)}")
            parts.append("Use add_skill() or apply to activate in your session.")
        return "\n".join(parts)

    @mcp.tool()
    def create_skill(name: str, description: str, instructions: str) -> str:
        """Author a new skill from scratch and assign it to yourself.

        WHEN TO USE: You want to capture a reusable workflow, domain expertise,
        or specialized procedure as a skill. Creates a SKILL.md (agentskills.io
        standard), registers it, and assigns it to you.
        NOT FOR: Installing an existing skill (use install_skill or add_skill).

        Args:
            name: Skill name (lowercase, hyphens, e.g. 'data-analysis').
            description: What the skill does and when to use it (1-2 sentences).
            instructions: The full skill instructions in markdown.
        """
        # Build SKILL.md content
        skill_md = f"---\nname: {name}\ndescription: {description}\n---\n\n{instructions}"

        result = _api("POST", "/skills/from-md", {
            "content": skill_md,
            "agent_name": agent_name,
        })

        if "error" in result:
            status = result.get("status", 500)
            error = result.get("error", "Unknown error")
            if status == 400:
                return f"Invalid skill: {error}"
            return f"Failed to create skill: {error}"

        created_name = result.get("name", name)
        assigned = result.get("assigned_to", "")

        parts = [f"Skill '{created_name}' created and registered."]
        if assigned:
            parts.append(f"Assigned to you ({assigned}).")
            parts.append("Call apply_skills or add_skill to activate it in your current session.")
        else:
            parts.append("Use add_skill() to assign it to yourself.")

        return "\n".join(parts)

    # ── System Admin ──────────────────────────────────────

    @mcp.tool()
    def check_for_updates() -> str:
        """Check for pending PinkyBot code updates (dry run, no changes).

        WHEN TO USE: You want to see if there are new commits on the remote
        before deciding to update. Safe — doesn't apply anything.
        NOT FOR: Actually updating (use update_and_restart).
        """
        result = _api("POST", "/admin/update?dry_run=true")
        if "error" in result:
            return f"Failed to check for updates: {result['error']}"

        if result.get("up_to_date"):
            return f"Already up to date at {result.get('current_hash', '?')} on {result.get('branch', 'main')}."

        commits = result.get("commits", [])
        parts = [
            f"Updates available on {result.get('branch', 'main')}:",
            f"Current: {result.get('current_hash', '?')}",
            f"Pending: {result.get('pending_commits', 0)} commit(s)",
            "",
        ]
        for c in commits[:20]:
            parts.append(f"  {c}")
        if len(commits) > 20:
            parts.append(f"  ... and {len(commits) - 20} more")
        return "\n".join(parts)

    @mcp.tool()
    def update_and_restart(branch: str = "main") -> str:
        """Pull latest code, rebuild if needed, and restart the daemon.

        WHEN TO USE: check_for_updates showed pending commits and you want
        to apply them. Pulls code, rebuilds deps/frontend if changed, and
        restarts. Your session resumes automatically.
        NOT FOR: Just checking what's available (use check_for_updates), or
        restarting without updating (use restart_daemon).

        Args:
            branch: Git branch to pull from (default: main).
        """
        result = _api("POST", f"/admin/update?branch={branch}")
        if "error" in result:
            return f"Update failed: {result['error']}"

        if not result.get("updated"):
            return f"Unexpected response: {result}"

        parts = [f"Update applied: {result.get('before_hash', '?')} -> {result.get('after_hash', '?')}"]

        commits = result.get("commits", [])
        if commits:
            parts.append(f"\nChanges ({len(commits)} commit(s)):")
            for c in commits[:10]:
                parts.append(f"  {c}")

        if result.get("deps_rebuilt"):
            parts.append("\nDependencies rebuilt.")
        if result.get("frontend_rebuilt"):
            parts.append("Frontend rebuilt.")

        if result.get("restarting"):
            parts.append("\nDaemon is restarting. Your session will resume automatically.")
        else:
            parts.append("\nNo restart needed (already up to date).")

        return "\n".join(parts)

    @mcp.tool()
    def restart_daemon() -> str:
        """Restart the daemon process without pulling code updates.

        WHEN TO USE: You need a clean restart to pick up config changes or
        recover from errors, but don't need new code. Session resumes automatically.
        NOT FOR: Updating code (use update_and_restart), or restarting just
        your context (use context_restart).
        """
        result = _api("POST", "/admin/restart")
        if "error" in result:
            return f"Restart failed: {result['error']}"
        return f"Daemon is restarting (was at {result.get('git_hash', '?')}). Your session will resume automatically."

    # ── Trigger Tools ──────────────────────────────────────────

    @mcp.tool()
    async def create_trigger(
        name: str,
        trigger_type: str,
        prompt_template: str = "",
        url: str = "",
        condition: str = "status_changed",
        condition_value: str = "",
        interval_seconds: int = 300,
    ) -> str:
        """Create a trigger that wakes this agent when it fires.

        trigger_type: 'webhook' (returns a URL to give to external services) or
        'url' (polls a URL on an interval and fires when a condition is met).

        For webhook type: returns the secret webhook URL to configure in external services.
        For url type: provide url, condition, condition_value, interval_seconds.

        condition options (url type):
          - 'status_changed'      — fires when HTTP status code changes
          - 'status_is'          — fires when status equals condition_value (e.g. "200")
          - 'body_contains'      — fires when body contains condition_value string
          - 'json_field_equals'  — condition_value: JSON {"path": "a.b", "value": "x"}
          - 'json_field_changed' — condition_value: JSON {"path": "a.b"}

        prompt_template: what to inject when trigger fires.
          Supports {{body.field}} for webhook payloads and url responses.
          Leave empty for a sensible default.

        Examples:
          create_trigger(name="github-prs", trigger_type="webhook",
                         prompt_template="New PR: {{body.pull_request.title}}")

          create_trigger(name="status-page", trigger_type="url",
                         url="https://status.example.com/api/status.json",
                         condition="json_field_changed",
                         condition_value='{"path": "status.indicator"}',
                         interval_seconds=60)
        """
        body = {
            "name": name,
            "trigger_type": trigger_type,
            "prompt_template": prompt_template,
            "url": url,
            "condition": condition,
            "condition_value": condition_value,
            "interval_seconds": interval_seconds,
        }
        result = _api("POST", f"/agents/{agent_name}/triggers", body)
        if "error" in result:
            return f"Error creating trigger: {result['error']}"
        t = result
        if t.get("trigger_type") == "webhook" and t.get("token"):
            token = t["token"]
            api_base = api_url.rstrip("/")
            webhook_url = f"{api_base}/hooks/{token}"
            return (
                f"Webhook trigger '{name}' created (id={t['id']}).\n"
                f"Webhook URL: {webhook_url}\n"
                f"Token: {token}\n\n"
                f"Give this URL to your external service. It will POST to it and wake you."
            )
        return f"Trigger '{name}' created (id={t['id']}, type={t['trigger_type']})."

    @mcp.tool()
    async def list_triggers() -> str:
        """List all triggers assigned to this agent.

        WHEN TO USE: Check what triggers are active, find a trigger ID to
        delete or test, or verify a trigger was created successfully.
        """
        result = _api("GET", f"/agents/{agent_name}/triggers")
        if "error" in result:
            return f"Error: {result['error']}"
        triggers = result.get("triggers", [])
        if not triggers:
            return "No triggers configured."
        lines = []
        for t in triggers:
            status = "ON" if t.get("enabled") else "OFF"
            count = t.get("fire_count", 0)
            fired = f"fired {count}x" if count else "never fired"
            ttype = t.get("trigger_type", "?")
            tid = t.get("id", "?")
            tname = t.get("name", "?")
            lines.append(f"[{tid}] {tname} ({ttype}) — {status}, {fired}")
        return "\n".join(lines)

    @mcp.tool()
    async def delete_trigger(trigger_id: int) -> str:
        """Delete a trigger by ID. For webhooks, the token is immediately invalidated.

        WHEN TO USE: A trigger is no longer needed. Get the ID from list_triggers first.

        Args:
            trigger_id: The trigger ID to delete (from list_triggers).
        """
        result = _api("DELETE", f"/agents/{agent_name}/triggers/{trigger_id}")
        if "error" in result:
            return f"Error: {result['error']}"
        return f"Trigger {trigger_id} deleted."

    @mcp.tool()
    async def test_trigger(trigger_id: int) -> str:
        """Manually fire a trigger to test it.

        WHEN TO USE: Verify that a trigger is wired up correctly and the prompt
        template looks right. Does not affect fire_count or last_fired_at.

        Args:
            trigger_id: The trigger ID to test (from list_triggers).
        """
        result = _api("POST", f"/agents/{agent_name}/triggers/{trigger_id}/test")
        if "error" in result:
            return f"Error: {result['error']}"
        woken = result.get("agent_woken", False)
        prompt = result.get("prompt", "")
        status = "Agent woken successfully." if woken else "Wake attempt failed (check daemon logs)."
        return f"Trigger {trigger_id} test fired. {status}\n\nRendered prompt:\n{prompt[:500]}"

    return mcp
