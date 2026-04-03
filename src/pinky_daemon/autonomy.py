"""Autonomy Engine — self-directed agent work loop.

This is the brain stem. It gives agents the ability to:
1. Wake up and assess their situation (inbox, tasks, events)
2. Decide what to work on (priority-based decision)
3. Execute work through their main session
4. Report results (update tasks, post comments, notify)
5. Check for more work or go to sleep

The engine runs as an async background task per agent, triggered by:
- Cron schedules (periodic wake)
- Event triggers (new message, task assigned, alert)
- Manual wake (API call)

Architecture:
    Event Source → Event Queue → Decision Engine → Session.send() → Result Handler
                                                                   ↓
                                                            Task Updates
                                                            Heartbeats
                                                            Notifications
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from enum import Enum

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.task_store import TaskStore


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class EventType(str, Enum):
    """Types of events that can wake an agent."""

    message_received = "message_received"  # New message in inbox
    task_assigned = "task_assigned"         # Task assigned to agent
    task_updated = "task_updated"           # Task status changed
    schedule_wake = "schedule_wake"         # Cron schedule fired
    manual_wake = "manual_wake"            # Manual API trigger
    worker_report = "worker_report"        # Worker finished a task
    alert = "alert"                        # High-priority alert
    idle_check = "idle_check"              # Periodic idle check


@dataclass
class AgentEvent:
    """An event that triggers agent action."""

    type: EventType
    agent_name: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    priority: int = 0  # 0=normal, 1=high, 2=urgent

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "agent_name": self.agent_name,
            "data": self.data,
            "timestamp": self.timestamp,
            "priority": self.priority,
        }


class EventQueue:
    """Per-agent event queue with priority ordering."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.PriorityQueue] = {}

    def ensure_queue(self, agent_name: str) -> None:
        if agent_name not in self._queues:
            self._queues[agent_name] = asyncio.PriorityQueue()

    async def push(self, event: AgentEvent) -> None:
        """Push an event to an agent's queue."""
        self.ensure_queue(event.agent_name)
        # Priority queue: lower number = higher priority, negate for urgency
        await self._queues[event.agent_name].put(
            (-event.priority, event.timestamp, event)
        )

    async def pop(self, agent_name: str, timeout: float = 0) -> AgentEvent | None:
        """Pop highest-priority event from an agent's queue."""
        self.ensure_queue(agent_name)
        try:
            if timeout > 0:
                _, _, event = await asyncio.wait_for(
                    self._queues[agent_name].get(), timeout=timeout
                )
            else:
                _, _, event = self._queues[agent_name].get_nowait()
            return event
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return None

    def pending_count(self, agent_name: str) -> int:
        """Count pending events for an agent."""
        if agent_name not in self._queues:
            return 0
        return self._queues[agent_name].qsize()

    def all_counts(self) -> dict[str, int]:
        """Get pending event counts for all agents."""
        return {name: q.qsize() for name, q in self._queues.items()}


def build_wake_prompt(
    agent_name: str,
    *,
    events: list[AgentEvent],
    pending_tasks: list[dict],
    inbox_messages: list[dict],
    context: dict | None = None,
) -> str:
    """Build a comprehensive wake prompt for an agent.

    This is the decision engine — it gives the agent all the context
    it needs to decide what to do autonomously.
    """
    parts = []

    # Continuation context (if any)
    if context:
        if context.get("task"):
            parts.append(f"## Continuation\nYou were working on: {context['task']}")
        if context.get("context"):
            parts.append(f"### Previous Context\n{context['context']}")
        if context.get("notes"):
            parts.append(f"### Notes\n{context['notes']}")

    # Events that triggered this wake
    if events:
        event_lines = []
        for e in events:
            if e.type == EventType.message_received:
                sender = e.data.get("sender", "unknown")
                content = e.data.get("content", "")[:200]
                event_lines.append(f"- Message from {sender}: {content}")
            elif e.type == EventType.task_assigned:
                title = e.data.get("title", "")
                event_lines.append(f"- Task assigned: {title}")
            elif e.type == EventType.worker_report:
                worker = e.data.get("worker", "")
                result = e.data.get("result", "")[:200]
                event_lines.append(f"- Worker {worker} reported: {result}")
            elif e.type == EventType.schedule_wake:
                prompt = e.data.get("prompt", "Scheduled wake")
                event_lines.append(f"- Scheduled: {prompt}")
            elif e.type == EventType.alert:
                msg = e.data.get("message", "")
                event_lines.append(f"- ALERT: {msg}")
            elif e.type == EventType.manual_wake:
                prompt = e.data.get("prompt", "Manual wake")
                event_lines.append(f"- Manual trigger: {prompt}")
        if event_lines:
            parts.append("## What Happened\n" + "\n".join(event_lines))

    # Pending inbox messages
    if inbox_messages:
        msg_lines = []
        for m in inbox_messages[:10]:
            sender = m.get("from", "unknown")
            content = m.get("content", "")[:150]
            msg_lines.append(f"- [{sender}] {content}")
        parts.append(f"## Inbox ({len(inbox_messages)} messages)\n" + "\n".join(msg_lines))

    # Assigned tasks
    if pending_tasks:
        task_lines = []
        for t in pending_tasks[:10]:
            status = t.get("status", "pending")
            priority = t.get("priority", "normal")
            title = t.get("title", "")
            task_lines.append(f"- [{priority}] [{status}] {title} (#{t.get('id', '?')})")
        parts.append(f"## Your Tasks ({len(pending_tasks)} active)\n" + "\n".join(task_lines))

    # Instructions for autonomous operation
    parts.append("""## Instructions
You are operating autonomously. Assess the situation above and take action:

1. **Process messages** — respond to anything that needs a reply
2. **Work on tasks** — pick the highest-priority task and make progress
3. **Report results** — when you complete work, update the task status
4. **Delegate if needed** — create subtasks for workers if the work is complex
5. **Save your state** — before stopping, save continuation context so you can resume

If there's nothing to do, say so. Don't invent work.""")

    return "\n\n".join(parts)


class AutonomyEngine:
    """Manages autonomous agent work loops.

    Each agent with auto_start=True gets a work loop that:
    1. Waits for events (with timeout for periodic idle checks)
    2. Batches pending events
    3. Builds a decision prompt
    4. Sends to agent's main session
    5. Processes the response
    6. Loops
    """

    def __init__(
        self,
        registry: AgentRegistry,
        task_store: TaskStore,
        conversation_store: ConversationStore,
        *,
        session_sender=None,  # async fn(agent_name, session_id, prompt) -> response
        idle_check_interval: int = 300,  # Check for work every 5 min even without events
    ) -> None:
        self._registry = registry
        self._tasks = task_store
        self._convos = conversation_store
        self._session_sender = session_sender
        self._idle_check_interval = idle_check_interval
        self._event_queue = EventQueue()
        self._running_loops: dict[str, asyncio.Task] = {}
        self._running = False

    @property
    def event_queue(self) -> EventQueue:
        return self._event_queue

    async def start(self) -> None:
        """Start autonomy engine."""
        self._running = True
        _log("autonomy: engine started")

    async def stop(self) -> None:
        """Stop all work loops."""
        self._running = False
        for name, task in list(self._running_loops.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._running_loops.clear()
        _log("autonomy: engine stopped")

    async def start_agent_loop(self, agent_name: str) -> None:
        """Start the autonomous work loop for an agent."""
        if agent_name in self._running_loops:
            _log(f"autonomy: loop already running for {agent_name}")
            return

        self._event_queue.ensure_queue(agent_name)
        task = asyncio.create_task(self._agent_loop(agent_name))
        self._running_loops[agent_name] = task
        _log(f"autonomy: started work loop for {agent_name}")

    async def stop_agent_loop(self, agent_name: str) -> None:
        """Stop an agent's work loop."""
        task = self._running_loops.pop(agent_name, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            _log(f"autonomy: stopped work loop for {agent_name}")

    async def push_event(self, event: AgentEvent) -> None:
        """Push an event to an agent's queue. Starts loop if not running."""
        await self._event_queue.push(event)
        _log(f"autonomy: event {event.type.value} for {event.agent_name}")

        # Auto-start loop if agent has auto_start and loop isn't running
        if event.agent_name not in self._running_loops:
            agent = self._registry.get(event.agent_name)
            if agent and agent.auto_start and agent.enabled:
                await self.start_agent_loop(event.agent_name)

    async def _agent_loop(self, agent_name: str) -> None:
        """Main work loop for an agent."""
        _log(f"autonomy: {agent_name} work loop running")
        consecutive_idle = 0

        while self._running:
            try:
                # Wait for events (with timeout for idle checks)
                event = await self._event_queue.pop(
                    agent_name, timeout=self._idle_check_interval
                )

                if event is None:
                    # Timeout — idle check
                    consecutive_idle += 1
                    if consecutive_idle >= 3:
                        # 3 idle checks with no work — go to sleep
                        _log(f"autonomy: {agent_name} idle, entering sleep")
                        break
                    continue

                consecutive_idle = 0

                # Batch all pending events
                events = [event]
                while True:
                    more = await self._event_queue.pop(agent_name)
                    if more is None:
                        break
                    events.append(more)

                # Gather context
                agent_tasks = self._tasks.list(assigned_agent=agent_name)
                inbox = []
                try:
                    pass
                    # Inbox would be gathered here if comms is available
                except Exception:
                    pass

                wake_context = self._registry.get_context(agent_name)
                ctx_dict = wake_context.to_dict() if wake_context else None

                # Build decision prompt
                prompt = build_wake_prompt(
                    agent_name,
                    events=events,
                    pending_tasks=[t.to_dict() for t in agent_tasks],
                    inbox_messages=inbox,
                    context=ctx_dict,
                )

                # Send to agent's main session
                if self._session_sender:
                    try:
                        session_id = f"{agent_name}-main"
                        await self._session_sender(agent_name, session_id, prompt)
                        _log(f"autonomy: {agent_name} processed {len(events)} event(s)")
                    except Exception as e:
                        _log(f"autonomy: {agent_name} session error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                _log(f"autonomy: {agent_name} loop error: {e}")
                await asyncio.sleep(10)  # Back off on errors

        # Cleanup
        self._running_loops.pop(agent_name, None)
        _log(f"autonomy: {agent_name} work loop exited")

    def get_status(self) -> dict:
        """Get autonomy engine status."""
        return {
            "running": self._running,
            "active_loops": list(self._running_loops.keys()),
            "event_queues": self._event_queue.all_counts(),
        }
