"""Run competing registration handlers in independently spawned app processes."""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import tempfile
import traceback
from pathlib import Path


def _registration_contender(
    index, root, payloads, workspace_race, barrier, first_done, ready, results
):
    private = Path(root) / f"contender-{index}"
    private.mkdir(mode=0o700)
    tempfile.tempdir = str(private)
    with (private / "process.log").open("w") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            try:
                from fastapi.testclient import TestClient

                from pinky_daemon.api import create_api
                from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

                app = create_api(
                    max_sessions=10,
                    default_working_dir=root,
                    db_path=str(Path(root) / "agents.db"),
                )
                registry = app.state.agents
                original = registry._refuse_workspace_overlap
                reached_handler = False
                call_count = 0

                def witness_and_gate(name, workspace):
                    nonlocal reached_handler, call_count
                    reached_handler = True
                    original(name, workspace)
                    call_count += 1
                    # Route and registry advisory checks both see an empty DB.
                    # The third call remains the real in-transaction recheck.
                    if call_count <= 2:
                        barrier.wait(timeout=30)
                    if workspace_race and call_count == 2 and index == 1:
                        assert first_done.wait(timeout=30)

                registry._refuse_workspace_overlap = witness_and_gate
                # Exercise request handlers without starting unrelated daemon
                # background services. Process exit closes each child's stores.
                with contextlib.closing(TestClient(app)) as client:
                    client.cookies.set(
                        SESSION_COOKIE_NAME,
                        create_session_cookie(os.environ["PINKY_SESSION_SECRET"]),
                    )
                    ready.put({"index": index, "ready": True})
                    barrier.wait(timeout=60)
                    try:
                        response = client.post("/agents", json=payloads[index])
                    finally:
                        if index == 0:
                            first_done.set()
                    barrier.wait(timeout=30)
                    rows = {}
                    for payload in payloads:
                        name = payload["name"]
                        agent = registry.get(name)
                        rows[name] = None if agent is None else {
                            field: getattr(agent, field)
                            for field in ("name", "display_name", "model", "soul", "working_dir")
                        }
                    results.put({
                        "index": index,
                        "status": response.status_code,
                        "body": response.json(),
                        "reached_handler": reached_handler,
                        "rows": rows,
                        "names": [agent.name for agent in registry.list()],
                        "main_agent": registry.get_main_agent(),
                        "signing_keys": {
                            name: bool(registry.get_signing_key(name)) for name in rows
                        },
                        "soul_versions": {
                            name: registry.get_soul_versions(name) for name in rows
                        },
                        "tokens": {name: registry.list_tokens(name) for name in rows},
                    })
                    # Neither child tears down stores while the other snapshots.
                    barrier.wait(timeout=30)
            except BaseException:
                results.put({"index": index, "error": traceback.format_exc()})
                raise


def run_registration_race(root, payloads, *, workspace_race=False):
    """Require two completed handlers and reap both children even on failure."""
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    first_done = context.Event()
    ready = context.Queue()
    results = context.Queue()
    children = []
    try:
        # Bootstrap each app fully before starting the next; only the requests race.
        for index in range(2):
            child = context.Process(
                target=_registration_contender,
                args=(index, str(root), payloads, workspace_race,
                      barrier, first_done, ready, results),
            )
            child.start()
            children.append(child)
            assert ready.get(timeout=60) == {"index": index, "ready": True}
        outcomes = [results.get(timeout=60) for _ in children]
        assert {result["index"] for result in outcomes} == {0, 1}, outcomes
        assert all("error" not in result for result in outcomes), outcomes
        assert all(result["reached_handler"] for result in outcomes), outcomes
        for child in children:
            child.join(timeout=30)
            assert child.exitcode == 0, f"contender {child.pid}: exit={child.exitcode}"
        return sorted(outcomes, key=lambda result: result["index"])
    finally:
        unreaped = []
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(timeout=5)
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
            if child.is_alive():
                unreaped.append(child.pid)
        ready.close()
        results.close()
        ready.join_thread()
        results.join_thread()
        assert not unreaped, f"unreaped contenders: {unreaped}"
