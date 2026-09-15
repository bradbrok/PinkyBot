"""Recovery owners must stop before a different transport is published."""

import asyncio

import pytest

from pinky_daemon.streaming_session import StreamingSession
from pinky_daemon.transport_state import SessionState
from tests.recovery_test_support import lifecycle_harness as lifecycle_harness
from tests.recovery_test_support import set_flags


@pytest.mark.parametrize("destination_transport", ["sdk", "tmux"])
@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("route", ["force", "ensure"])
async def test_backoff_owner_cannot_resurrect_after_replacement(
    lifecycle_harness,
    monkeypatch,
    source,
    route,
    destination_transport,
):
    """Adapted independent review floor: four force failures, four ensure controls."""
    h = lifecycle_harness
    old = h.seed((source, "sdk"))
    old._RECONNECT_BACKOFF = (137.0,)
    entered, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep
    owners = []

    async def pause(delay):
        if delay == 137.0:
            owners.append(asyncio.current_task())
            entered.set()
            await release.wait()
        else:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", pause)
    reconnect = asyncio.create_task(old.attempt_reconnect())
    observations = []
    register = h.app.state.broker.register_streaming

    def publish(name, ss, **kwargs):
        if ss is not old:
            observations.append((owners[0].done(), old.state, old._state_machine._in_flight))
        return register(name, ss, **kwargs)

    monkeypatch.setattr(h.app.state.broker, "register_streaming", publish)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        h.app.state.agents.register(
            "sample",
            runtime=("codex_cli" if source == "claude_sdk" else "claude_sdk"),
            transport=destination_transport,
        )
        if route == "force":
            response = await h.client.post("/admin/force-restart-agent/sample")
            assert response.status_code == 200, response.text
        else:
            ensure = asyncio.create_task(h.app.state.broker._ensure_session_callback("sample"))
            await real_sleep(0)
            assert not ensure.done()
            release.set()
            await asyncio.gather(reconnect, return_exceptions=True)
            h.app.state.agents.set_context(
                "sample",
                task="Saved work",
                metadata={"source": "save_my_context"},
                updated_by=old.resume_handle,
            )
            await asyncio.wait_for(ensure, 3)
    finally:
        release.set()
        await asyncio.gather(reconnect, return_exceptions=True)
    current = h.app.state.broker._streaming["sample"]["main"]
    assert current is not old
    assert old.state == SessionState.DEAD, "Retired object reconnected after replacement"
    assert observations == [(True, SessionState.DEAD, None)]
    assert current.state == SessionState.CONNECTED


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("mode", ["a", "both"])
async def test_late_reference_cannot_start_recovery_on_retired_object(
    lifecycle_harness,
    monkeypatch,
    source,
    mode,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    old = h.seed((source, "sdk"))
    old._RECONNECT_BACKOFF = (0,)
    h.app.state.agents.register(
        "sample", runtime=("codex_cli" if source == "claude_sdk" else "claude_sdk")
    )
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code == 200, response.text
    before = (len(h.clients), len(h.trace))
    try:
        await old.attempt_reconnect()
    except RuntimeError:
        pass  # Explicit retired-owner refusal is also valid.
    assert old.state == SessionState.DEAD
    assert (len(h.clients), len(h.trace)) == before, "Stale reference spawned a substrate"


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
@pytest.mark.parametrize("phase", ["backoff", "startup"])
async def test_replacement_quiesces_owner_with_failsafe_disabled(
    lifecycle_harness,
    monkeypatch,
    source,
    phase,
):
    h = lifecycle_harness
    set_flags(monkeypatch, "a")
    old = h.seed((source, "sdk"))
    old._RECONNECT_BACKOFF = (137.0 if phase == "backoff" else 0,)
    entered, release = asyncio.Event(), asyncio.Event()
    owner = []
    real_sleep = asyncio.sleep

    async def barrier():
        owner.append(asyncio.current_task())
        entered.set()
        await release.wait()

    async def sleep(delay):
        if delay == 137.0:
            await barrier()
        else:
            await real_sleep(delay)

    async def start(ss):
        if ss is old:
            await barrier()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    if phase == "startup":
        h.control.start_hook = start
    reconnect = asyncio.create_task(old.attempt_reconnect())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        h.app.state.agents.register(
            "sample", runtime=("codex_cli" if source == "claude_sdk" else "claude_sdk")
        )
        response = await h.client.post("/admin/force-restart-agent/sample")
        assert response.status_code == 200, response.text
        assert owner[0].done(), "Replacement returned while old startup owner was alive"
        assert old.state == SessionState.DEAD
        assert old._state_machine._in_flight is None
    finally:
        if not reconnect.done():
            reconnect.cancel()
        release.set()
        await asyncio.gather(reconnect, return_exceptions=True)


async def test_replacement_refuses_self_owned_recovery_without_self_cancel(lifecycle_harness):
    h = lifecycle_harness
    old = h.seed()
    h.app.state.agents.register("sample", runtime="codex_cli")
    task = asyncio.current_task()
    old._reconnect_task = task
    try:
        response = await h.client.post("/admin/force-restart-agent/sample")
        assert not task.cancelling(), "Replacement cancelled its own caller"
        assert response.status_code >= 400, "Self-owned replacement must refuse before teardown"
        assert h.app.state.broker._streaming["sample"]["main"] is old
        assert old.state == SessionState.CONNECTED
    finally:
        old._reconnect_task = None


async def test_direct_replacement_refuses_current_recovery_owner(lifecycle_harness):
    old = lifecycle_harness.seed()
    task = asyncio.current_task()
    old._reconnect_task = old._owned_reconnect_task = task
    outcome = None
    try:
        await old.restart_transport(target_preflight=old._preflight_transport_replacement)
    except BaseException as exc:
        outcome = exc
    finally:
        old._reconnect_task = old._owned_reconnect_task = None
    assert isinstance(outcome, RuntimeError), "Current owner must be refused before cancellation"
    assert not task.cancelling()


@pytest.mark.parametrize("mode", ["off", "q2"])
async def test_disabled_rebuild_retains_original_object(lifecycle_harness, monkeypatch, mode):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    old = h.seed()
    h.app.state.agents.register("sample", runtime="codex_cli")
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code == 200, response.text
    assert h.app.state.broker._streaming["sample"]["main"] is old
    assert type(old) is StreamingSession


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
async def test_cancelled_recovery_settles_token_and_state(lifecycle_harness, monkeypatch, source):
    h = lifecycle_harness
    set_flags(monkeypatch, "a")
    old = h.seed((source, "sdk"))
    entered = asyncio.Event()
    real_sleep = asyncio.sleep
    old._RECONNECT_BACKOFF = (137.0,)

    async def pause(delay):
        if delay == 137.0:
            entered.set()
            await asyncio.Event().wait()
        else:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", pause)
    task = asyncio.create_task(old.attempt_reconnect())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert old.state == SessionState.DEAD
    assert old._state_machine._in_flight is None, "Cancelled owner stranded transition subscribers"


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
async def test_concurrent_reconnect_callers_share_one_owner(lifecycle_harness, monkeypatch, source):
    h = lifecycle_harness
    old = h.seed((source, "sdk"))
    entered, release = asyncio.Event(), asyncio.Event()
    owners = []
    real_sleep = asyncio.sleep
    old._RECONNECT_BACKOFF = (137.0,)

    async def pause(delay):
        if delay == 137.0:
            owners.append(asyncio.current_task())
            entered.set()
            await release.wait()
        else:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", pause)
    first = asyncio.create_task(old.attempt_reconnect())
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(old.attempt_reconnect())
    await real_sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert len(owners) == 1
    assert old.state == SessionState.CONNECTED
    assert old._state_machine._in_flight is None


async def test_target_preflight_failure_does_not_cancel_old_recovery(
    lifecycle_harness, monkeypatch
):
    h = lifecycle_harness
    old = h.seed()
    entered = asyncio.Event()
    owners = []
    real_sleep = asyncio.sleep
    old._RECONNECT_BACKOFF = (137.0,)

    async def pause(delay):
        if delay == 137.0:
            owners.append(asyncio.current_task())
            entered.set()
            await asyncio.Event().wait()
        else:
            await real_sleep(delay)

    def invalid_home(*args, **kwargs):
        raise RuntimeError("Target filesystem unavailable")

    monkeypatch.setattr(asyncio, "sleep", pause)
    monkeypatch.setattr("pinky_daemon.codex_tmux_session.validate_agent_codex_home", invalid_home)
    task = asyncio.create_task(old.attempt_reconnect())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        h.app.state.agents.register("sample", runtime="codex_cli", transport="tmux")
        response = await h.client.post("/admin/force-restart-agent/sample")
        assert response.status_code >= 400
        assert not owners[0].done() and not owners[0].cancelling()
        assert h.app.state.broker._streaming["sample"]["main"] is old
        assert old.resume_handle == "saved"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("source", ["claude_sdk", "codex_cli"])
async def test_replacement_awaits_delayed_owner_cancellation(lifecycle_harness, source):
    h = lifecycle_harness
    old = h.seed((source, "sdk"))
    old._RECONNECT_BACKOFF = (0,)
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def external_start(ss):
        if ss is old:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()
                raise

    h.control.start_hook = external_start
    recovery = asyncio.create_task(old.attempt_reconnect())
    await asyncio.wait_for(entered.wait(), 1)
    h.app.state.agents.register(
        "sample", runtime="codex_cli" if source == "claude_sdk" else "claude_sdk",
    )
    replacement = asyncio.create_task(h.client.post("/admin/force-restart-agent/sample"))
    try:
        await asyncio.wait_for(cancelling.wait(), 1)
        await asyncio.sleep(0)
        assert not any(action == "connect" and ss is not old for action, ss in h.trace), (
            "Replacement started before old owner acknowledged cancellation"
        )
        assert not recovery.done()
    finally:
        release.set()
        await asyncio.gather(recovery, return_exceptions=True)
    response = await replacement
    assert response.status_code == 200, response.text
    assert old.state == SessionState.DEAD
    assert old._state_machine._in_flight is None
