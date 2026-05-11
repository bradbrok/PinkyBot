"""Tests for AutonomyEngine push_event wake gating.

Boot policy (2026-05-11): only the main agent's autonomy loop starts at boot.
Sibling agents wake on demand the first time `push_event` routes an event to
them. These tests pin the wake gate — it must depend only on `enabled`, not
the legacy `auto_start` flag (which used to gate wake too and would strand
siblings under the new boot policy).
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.autonomy import AgentEvent, AutonomyEngine, EventType
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.task_store import TaskStore


@pytest.fixture
def stores():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    reg = AgentRegistry(db_path=path)
    tasks = TaskStore(db_path=path)
    convos = ConversationStore(db_path=path)
    yield reg, tasks, convos
    reg.close()
    os.unlink(path)


def _build_engine(registry, tasks, convos):
    # session_sender is unused for push_event wake tests; the loop, once
    # started, will block on its event queue waiting for more work.
    return AutonomyEngine(
        registry,
        tasks,
        convos,
        session_sender=None,
        idle_check_interval=3600,  # don't fire idle exit during the test
    )


class TestPushEventWakeGate:
    """The wake gate in `push_event` decides whether an inbound event should
    spin up an agent's autonomy loop. Under the post-2026-05-11 boot policy
    siblings start out without a loop, so this gate is the on-demand wake
    path. It must remain permissive for enabled agents and refuse disabled ones.
    """

    @pytest.mark.asyncio
    async def test_enabled_non_auto_start_agent_wakes_on_event(self, stores):
        registry, tasks, convos = stores
        # Critical: auto_start=False (sibling default under the new policy)
        registry.register("murzik", model="opus", auto_start=False, enabled=True)
        engine = _build_engine(registry, tasks, convos)
        await engine.start()
        try:
            assert "murzik" not in engine._running_loops

            await engine.push_event(AgentEvent(
                type=EventType.message_received,
                agent_name="murzik",
            ))

            # Loop should be started despite auto_start=False
            assert "murzik" in engine._running_loops
        finally:
            await engine.stop()

    @pytest.mark.asyncio
    async def test_disabled_agent_does_not_wake_on_event(self, stores):
        registry, tasks, convos = stores
        registry.register("retired", model="opus", auto_start=True, enabled=False)
        engine = _build_engine(registry, tasks, convos)
        await engine.start()
        try:
            await engine.push_event(AgentEvent(
                type=EventType.manual_wake,
                agent_name="retired",
            ))

            # Disabled agent must NOT have its loop started, even with auto_start=True
            assert "retired" not in engine._running_loops
        finally:
            await engine.stop()

    @pytest.mark.asyncio
    async def test_event_for_unknown_agent_does_not_wake(self, stores):
        registry, tasks, convos = stores
        engine = _build_engine(registry, tasks, convos)
        await engine.start()
        try:
            await engine.push_event(AgentEvent(
                type=EventType.schedule_wake,
                agent_name="ghost",
            ))

            assert "ghost" not in engine._running_loops
        finally:
            await engine.stop()

    @pytest.mark.asyncio
    async def test_running_loop_is_not_double_started(self, stores):
        registry, tasks, convos = stores
        registry.register("barsik", model="opus", auto_start=True, enabled=True)
        engine = _build_engine(registry, tasks, convos)
        await engine.start()
        try:
            await engine.start_agent_loop("barsik")
            first_task = engine._running_loops["barsik"]

            await engine.push_event(AgentEvent(
                type=EventType.message_received,
                agent_name="barsik",
            ))

            # Same task object — no replacement
            assert engine._running_loops["barsik"] is first_task
        finally:
            await engine.stop()

    @pytest.mark.asyncio
    async def test_event_is_queued_even_when_wake_blocked(self, stores):
        """A disabled agent shouldn't have its loop started, but events still
        land on the queue. If the agent is later re-enabled and its loop is
        started manually, the queued event is available — push_event never
        drops the message itself.
        """
        registry, tasks, convos = stores
        registry.register("retired", model="opus", auto_start=False, enabled=False)
        engine = _build_engine(registry, tasks, convos)
        await engine.start()
        try:
            ev = AgentEvent(
                type=EventType.message_received,
                agent_name="retired",
                data={"text": "hello"},
            )
            await engine.push_event(ev)

            assert "retired" not in engine._running_loops
            # Queue exists and has the event ready to pop
            engine._event_queue.ensure_queue("retired")
            popped = await asyncio.wait_for(
                engine._event_queue.pop("retired", timeout=0.5),
                timeout=1.0,
            )
            assert popped is not None
            assert popped.data == {"text": "hello"}
        finally:
            await engine.stop()
