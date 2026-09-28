"""Every startup entry must honor per-label unfinished cleanup."""

import asyncio

import pytest
from fastapi import HTTPException

from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSession
from pinky_daemon.transport_state import SessionState
from tests.recovery_test_support import closure_value, set_flags
from tests.recovery_test_support import lifecycle_harness as lifecycle_harness


async def invoke(h, entry):
    if entry == "ensure":
        return await h.app.state.broker._ensure_session_callback("sample")
    if entry == "container":
        lifecycle = closure_value(h.app, "_container_lifecycle")
        return await lifecycle.deps.start_session("sample")
    route = {
        "post": "/agents/sample/streaming-sessions",
        "skills": "/agents/sample/skills/apply",
        "replacement": "/admin/force-restart-agent/sample",
    }[entry]
    return await h.client.post(route)


CASES = [
    (entry, mode)
    for entry in ["ensure", "post", "skills", "container"]
    for mode in ["a", "b", "both", "off", "q2"]
]
CASES += [("replacement", mode) for mode in ["a", "both"]]


async def test_create_cannot_bypass_failed_tmux_replacement_cleanup(lifecycle_harness):
    """Independent direct-POST review probe with the actual tmux disconnect."""
    h = lifecycle_harness
    h.seed()
    h.app.state.agents.register("sample", runtime="codex_cli", transport="tmux")
    h.control.start_error = RuntimeError("Candidate startup failed")
    h.control.cleanup_error = RuntimeError("Owned pane still alive")
    response = await h.client.post("/admin/force-restart-agent/sample")
    assert response.status_code >= 400
    candidates = [ss for ss in h.sessions if type(ss) is CodexTmuxSession]
    assert len(candidates) == 1
    failed = candidates[0]
    assert failed._tmux.kill_session.await_count >= 1
    h.control.start_error = h.control.cleanup_error = None
    response = await h.client.post("/agents/sample/streaming-sessions")
    assert response.status_code >= 400, "Create bypassed retained pane cleanup ownership"
    assert len([ss for ss in h.sessions if type(ss) is CodexTmuxSession]) == 1


@pytest.mark.parametrize("entry,mode", CASES)
async def test_failed_start_cleanup_fences_next_real_create(
    lifecycle_harness,
    monkeypatch,
    entry,
    mode,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    old = h.seed(("codex_cli", "sdk") if entry == "replacement" else ("claude_sdk", "sdk"))
    if entry == "replacement":
        h.app.state.agents.register("sample", runtime="claude_sdk")
    elif entry not in {"skills"}:
        h.app.state.broker.unregister_streaming("sample", label="main")
    h.control.start_error = RuntimeError("initialize failed")
    h.control.cleanup_error = RuntimeError("child still alive")
    before = len(h.clients)
    try:
        response = await invoke(h, entry)
    except (RuntimeError, HTTPException):
        response = None
    assert response is None or response.status_code >= 400
    assert len(h.clients) == before + 1, "First startup did not reach the SDK boundary: " + (
        response.text if response else "raised"
    )
    failed_owner = next(ss for ss in h.sessions if ss is not old and type(ss) is StreamingSession)
    retained = failed_owner._client
    h.control.start_error = h.control.cleanup_error = None
    response = await h.client.post("/agents/sample/streaming-sessions")
    if mode in {"off", "q2"}:
        assert response.status_code == 200, response.text
        assert len(h.clients) == before + 2
        return
    assert response.status_code >= 400, "Create endpoint bypassed unfinished cleanup"
    assert len(h.clients) == before + 1, "A second SDK client was constructed over cleanup debt"
    assert failed_owner._client is retained and retained is not None
    # Once strict cleanup really succeeds, the next caller may start once.
    retained.disconnect.side_effect = retained.close
    response = await h.client.post("/agents/sample/streaming-sessions")
    assert response.status_code == 200, response.text
    assert len(h.clients) == before + 2
    current = h.app.state.broker._streaming["sample"]["main"]
    assert await h.app.state.broker._ensure_session_callback("sample") is current


@pytest.mark.parametrize("mode", ["a", "b", "both"])
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_interrupted_cold_start_retains_cleanup_owner(
    lifecycle_harness,
    monkeypatch,
    mode,
    failure,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    h.seed()
    h.app.state.broker.unregister_streaming("sample", label="main")
    entered = asyncio.Event()

    async def pending(ss):
        entered.set()
        await asyncio.Event().wait()

    h.control.start_hook = pending
    h.control.cleanup_error = TimeoutError("child cleanup timed out")
    if failure == "timeout":
        monkeypatch.setattr("pinky_daemon.api.COLD_START_CONNECT_TIMEOUT_SEC", 0.02)
    start = asyncio.create_task(h.app.state.broker._ensure_session_callback("sample"))
    await asyncio.wait_for(entered.wait(), 1)
    if failure == "cancel":
        start.cancel()
    outcome = await asyncio.gather(start, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError if failure == "cancel" else TimeoutError)
    before = len(h.clients)
    failed = h.sessions[-1]
    peer = h.clients[-1]
    if mode == "a":
        assert peer.disconnect.await_count == 1, "Startup cleanup acquired a second budget"
    h.control.start_hook = h.control.cleanup_error = None
    response = await h.client.post("/agents/sample/streaming-sessions")
    assert response.status_code >= 400, "Interrupted startup lost cleanup ownership"
    assert len(h.clients) == before
    assert failed._client is peer
    # Late persistence from the unregistered failed candidate cannot restore it.
    if failed._on_resume_handle:
        await failed._on_resume_handle("sample", "late-unregistered-handle")
    assert h.app.state.agents.get_streaming_session_id("sample") != "late-unregistered-handle"


async def test_cleanup_debt_is_label_scoped(lifecycle_harness):
    h = lifecycle_harness
    h.seed()
    h.app.state.broker.unregister_streaming("sample", label="main")
    h.control.start_error = RuntimeError("initialize failed")
    h.control.cleanup_error = RuntimeError("child still alive")
    with pytest.raises(RuntimeError):
        await h.app.state.broker._ensure_session_callback("sample")
    h.control.start_error = h.control.cleanup_error = None
    response = await h.client.post("/agents/sample/streaming-sessions?label=secondary")
    assert response.status_code == 200, response.text
    sibling = h.app.state.broker._streaming["sample"]["secondary"]
    response = await h.client.post("/agents/sample/streaming-sessions")
    assert response.status_code >= 400
    assert h.app.state.broker._streaming["sample"]["secondary"] is sibling
    assert sibling.state == SessionState.CONNECTED


async def test_repeated_ensure_does_not_discard_partial_sdk_owner(lifecycle_harness):
    """Independent cold-start review probe, retaining real connect/disconnect."""
    h = lifecycle_harness
    h.seed()
    h.app.state.broker.unregister_streaming("sample", label="main")
    h.control.start_error = RuntimeError("initialize failed")
    h.control.cleanup_error = RuntimeError("child still alive")
    before = len(h.clients)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await h.app.state.broker._ensure_session_callback("sample")
    assert len(h.clients) == before + 1, "Second ensure constructed over an uncleaned child"


@pytest.mark.parametrize("entry", ["ensure", "container", "post"])
@pytest.mark.parametrize("mode", ["a", "b", "both"])
async def test_each_cold_start_entry_consults_existing_cleanup_debt(
    lifecycle_harness,
    monkeypatch,
    entry,
    mode,
):
    h = lifecycle_harness
    set_flags(monkeypatch, mode)
    h.seed()
    h.app.state.broker.unregister_streaming("sample", label="main")
    h.control.start_error = RuntimeError("initialize failed")
    h.control.cleanup_error = RuntimeError("child still alive")
    with pytest.raises(RuntimeError):
        await h.app.state.broker._ensure_session_callback("sample")
    before = len(h.clients)
    h.control.start_error = h.control.cleanup_error = None
    try:
        outcome = await invoke(h, entry)
    except (RuntimeError, HTTPException):
        outcome = None
    refused = outcome is None or (hasattr(outcome, "status_code") and outcome.status_code >= 400)
    assert refused, "Startup admitted work while previous cleanup remained uncertain"
    assert len(h.clients) == before, "Startup constructed a second SDK client over debt"


async def test_concurrent_create_and_ensure_share_one_startup_owner(lifecycle_harness):
    h = lifecycle_harness
    h.seed()
    h.app.state.broker.unregister_streaming("sample", label="main")
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(ss):
        entered.set()
        await release.wait()

    h.control.start_hook = pause
    before = len(h.clients)
    create = asyncio.create_task(h.client.post("/agents/sample/streaming-sessions"))
    await asyncio.wait_for(entered.wait(), 1)
    ensure = asyncio.create_task(h.app.state.broker._ensure_session_callback("sample"))
    await asyncio.sleep(0)
    release.set()
    response, current = await asyncio.gather(create, ensure)
    assert response.status_code == 200, response.text
    assert len(h.clients) == before + 1
    assert h.app.state.broker._streaming["sample"]["main"] is current


async def test_cleanup_debt_never_tears_down_a_superseded_label_owner(lifecycle_harness):
    h = lifecycle_harness
    h.seed()
    h.app.state.broker.unregister_streaming("sample", label="main")
    h.control.start_error = RuntimeError("initialize failed")
    h.control.cleanup_error = RuntimeError("child still alive")
    with pytest.raises(RuntimeError):
        await h.app.state.broker._ensure_session_callback("sample")
    peer = h.clients[-1]
    before = peer.disconnect.await_count
    h.control.start_error = h.control.cleanup_error = None
    replacement = h.seed()
    try:
        await invoke(h, "container")
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        pytest.fail("Superseded cleanup owner was admitted")
    assert peer.disconnect.await_count == before, "Cleanup touched a superseded label"
    assert h.app.state.broker._streaming["sample"]["main"] is replacement


async def test_post_connect_refusal_keeps_failed_cleanup_owner(lifecycle_harness, monkeypatch):
    h = lifecycle_harness
    set_flags(monkeypatch, "a")
    h.seed()
    h.app.state.broker.unregister_streaming("sample", label="main")

    async def change_launch_row(ss):
        h.app.state.agents.register("sample", model="haiku")

    h.control.start_hook = change_launch_row
    h.control.cleanup_error = RuntimeError("child still alive")
    response = await h.client.post("/agents/sample/streaming-sessions")
    assert response.status_code >= 400
    failed = h.sessions[-1]
    assert failed._client is h.clients[-1], "Post-connect refusal discarded an uncleaned client"
    h.control.start_hook = h.control.cleanup_error = None
    count = len(h.clients)
    response = await h.client.post("/agents/sample/streaming-sessions")
    assert response.status_code >= 400
    assert len(h.clients) == count
