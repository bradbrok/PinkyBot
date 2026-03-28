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
import sys
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP


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
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            return {"error": error_body, "status": e.code}
        except Exception as e:
            return {"error": str(e)}

    # ── Wake Schedules ─────────────────────────────────────

    @mcp.tool()
    def set_wake_schedule(
        cron: str,
        name: str = "",
        prompt: str = "",
        timezone: str = "America/Los_Angeles",
    ) -> str:
        """Set a cron-based wake schedule for yourself.

        You'll be woken at the specified times with the given prompt.
        Use this to schedule periodic check-ins, reviews, or any recurring work.

        Args:
            cron: Cron expression (e.g. "0 8 * * *" for daily at 8am,
                  "*/30 * * * *" for every 30 min, "0 9 * * 1-5" for weekdays 9am).
            name: Human-friendly schedule name (e.g. "morning_check").
            prompt: What to do when woken (e.g. "Check inbox and process pending tasks").
            timezone: Timezone for the schedule (default: America/Los_Angeles).
        """
        result = _api("POST", f"/agents/{agent_name}/schedules", {
            "name": name or "self_scheduled",
            "cron": cron,
            "prompt": prompt or f"Self-scheduled wake: {name}",
            "timezone": timezone,
        })
        if "error" in result:
            return f"Failed to set schedule: {result['error']}"
        return f"Schedule '{result.get('name', name)}' set: {cron} ({timezone}). ID: {result.get('id')}"

    @mcp.tool()
    def list_my_schedules() -> str:
        """List all your wake schedules.

        Returns your current cron schedules with their status and last run time.
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
        """Remove one of your wake schedules.

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
        """Save your continuation context before a restart or sleep.

        This state will be restored when you wake up, so save everything
        you need to continue where you left off.

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
        })
        if "error" in result:
            return f"Failed to save context: {result['error']}"
        return "Context saved. You'll see this when you wake up."

    @mcp.tool()
    def load_my_context() -> str:
        """Load your saved continuation context.

        Returns the state you saved before your last restart/sleep.
        """
        result = _api("GET", f"/agents/{agent_name}/context")
        if result.get("context") is None:
            return "No saved context. This is a fresh start."
        ctx = result
        parts = []
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
    def get_next_task() -> str:
        """Get the next task you should work on.

        Returns your highest-priority assigned task, or the next
        unassigned task you could claim.
        """
        result = _api("GET", f"/tasks/next?agent_name={agent_name}")
        task = result.get("task")
        if not task:
            return "No tasks available. You're all caught up."
        source = result.get("source", "")
        t = task
        return (
            f"[{source}] #{t['id']}: {t['title']}\n"
            f"Priority: {t['priority']} | Status: {t['status']}\n"
            f"Description: {t.get('description', 'none')}\n"
            f"Tags: {', '.join(t.get('tags', [])) or 'none'}"
        )

    @mcp.tool()
    def claim_task(task_id: int) -> str:
        """Claim an unassigned task and start working on it.

        Args:
            task_id: The task ID to claim.
        """
        result = _api("POST", f"/tasks/claim/{task_id}?agent_name={agent_name}")
        if "error" in result:
            return f"Failed to claim task: {result['error']}"
        return f"Claimed task #{task_id}: {result.get('title', '')}. Status: in_progress."

    @mcp.tool()
    def complete_task(task_id: int, summary: str = "") -> str:
        """Mark a task as completed.

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
        """Mark a task as blocked.

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
        """Create a new task (for yourself or to delegate to another agent).

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

    # ── Health & Status ────────────────────────────────────

    @mcp.tool()
    def check_my_health() -> str:
        """Check your own health and status.

        Returns session status, context usage, heartbeat health,
        task workload, and any recommendations.
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

    # ── Sleep/Wake Control ─────────────────────────────────

    @mcp.tool()
    def request_sleep(wake_cron: str = "", wake_prompt: str = "") -> str:
        """Request to go into deep sleep to save resources.

        Saves your context and closes your session. You'll be woken
        by your existing schedules, or by a new one if you provide wake_cron.

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
        """Send a heartbeat to confirm you're alive and working.

        Call this periodically during long tasks so the system knows you're not stuck.

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

    return mcp
