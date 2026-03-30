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
    def who_am_i() -> str:
        """Get information about yourself — your name, model, session, configuration.

        Returns your agent name, display name, model, permission mode,
        working directory, session ID, context usage, and group memberships.
        Use this when you need to know what model you're running on or
        any other details about your own identity and configuration.
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

    # ── Context Management ─────────────────────────────────

    @mcp.tool()
    def context_status() -> str:
        """Check your streaming session's context usage and saved state.

        Returns token counts, percentage used, turn count, cost,
        and your saved continuation context (task, notes, blockers).
        Call this on wake to restore your state.
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
        else:
            parts.append("No streaming session active")

        # Saved continuation context
        saved = _api("GET", f"/agents/{agent_name}/context")
        if saved and saved.get("task"):
            parts.append("")
            parts.append("── Saved State ──")
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
        """Restart your streaming session with fresh context.

        This disconnects your current CC session and starts a new one.
        Your conversation history is lost, but your soul/personality and
        memory MCP tools are preserved. Save important state to memory
        before calling this.

        Use when context_status shows high usage (>70%) or when you
        want a clean slate.
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

    # ── Research Pipeline ─────────────────────────────────

    @mcp.tool()
    def submit_research_brief(
        topic_id: int,
        content: str,
        summary: str = "",
        sources: str = "",
        key_findings: str = "",
    ) -> str:
        """Submit a research brief for a topic you've been assigned to investigate.

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
        return f"Brief submitted for topic {topic_id} (version {result.get('version', 1)}). Status: {result.get('status', 'draft')}"

    @mcp.tool()
    def submit_research_review(
        topic_id: int,
        brief_id: int,
        verdict: str = "approve",
        comments: str = "",
        confidence: int = 3,
    ) -> str:
        """Submit a peer review for a research brief.

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
        """Check the research queue — your assignments AND open topics available to claim.

        Shows:
        - Topics assigned to you as researcher
        - Topics assigned to you as reviewer
        - Open topics in the queue that nobody has claimed yet
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
        """Claim an open research topic and assign it to yourself.

        Args:
            topic_id: ID of the open topic to claim
        """
        result = _api("POST", f"/research/{topic_id}/assign", {
            "agent_name": agent_name,
        })
        if "error" in result:
            return f"Failed to claim topic: {result['error']}"
        return f"Claimed topic [{topic_id}] '{result.get('title', '')}'. Status: {result.get('status', 'assigned')}. Start researching!"

    return mcp
