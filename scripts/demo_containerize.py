#!/usr/bin/env python3
"""Demo for one-click containerize (#204) — backend feature walkthrough.

Part A hits the REAL preflight endpoint (GET /system/container-runtime) over a
TestClient, runtime-off then runtime-on, to show the honest readiness signal the
UI button keys on.

Part B drives the REAL ContainerLifecycle state machine (the actual provisioner
logic over a recording podman double — no real podman) and prints every status
transition, proving the provision → probe → persist → bounce ordering and the
decontainerize keep-the-home-volume contract.

Run: .venv/bin/python scripts/demo_containerize.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pinky_daemon import container_ops as cops  # noqa: E402
from pinky_daemon.agent_registry import Agent  # noqa: E402
from pinky_daemon.provisioning import ContainerNames, ContainerProvisioner  # noqa: E402

# Reuse the recording double + fakes from the test module.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
from test_container_ops import FakeRegistry, RecordingContainerOps, _sync_runner  # noqa: E402


def hr(title: str) -> None:
    print(f"\n{'─' * 70}\n  {title}\n{'─' * 70}")


def part_a() -> None:
    hr("PART A · GET /system/container-runtime (real endpoint, real auth)")
    from fastapi.testclient import TestClient

    from pinky_daemon.api import create_api
    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

    secret = "demo-session-secret"
    os.environ["PINKY_SESSION_SECRET"] = secret
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    client = TestClient(create_api(max_sessions=4, default_working_dir="/tmp", db_path=path))
    client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(secret))

    os.environ.pop("PINKY_CONTAINER_RUNTIME", None)
    os.environ.pop("PINKY_CONTAINER_DEFAULT_IMAGE", None)
    print("runtime OFF  →", client.get("/system/container-runtime").json())

    os.environ["PINKY_CONTAINER_RUNTIME"] = "podman"
    os.environ["PINKY_CONTAINER_DEFAULT_IMAGE"] = "ghcr.io/acme/pinky-agent:1"
    print("runtime ON   →", client.get("/system/container-runtime").json())

    os.environ["PINKY_CONTAINER_RUNTIME"] = "docker"
    print("docker host  →", client.get("/system/container-runtime").json())
    os.environ.pop("PINKY_CONTAINER_RUNTIME", None)


class _LoudRegistry(FakeRegistry):
    def register(self, name, **kwargs):
        print(f"    [DB]   persist isolation_mode={kwargs.get('isolation_mode')} "
              f"container_image={kwargs.get('container_image','-')} "
              f"isolated={kwargs.get('isolated')} transport={kwargs.get('transport','-')}")
        return super().register(name, **kwargs)


def _deps(agent, ops, reg, *, has_live=True):
    locks: dict[str, asyncio.Lock] = {}

    async def _disc(n):
        print("    [sess] stop + unregister all labels")
        return 1

    async def _start(n):
        print("    [sess] cold-start FRESH session from updated row")

    return cops.ContainerLifecycleDeps(
        registry=reg,
        provisioner_for=lambda: ContainerProvisioner(
            ops=ops, image_provider=lambda a: a.container_image or "ghcr.io/acme/pinky-agent:1",
            signing_key_provider=lambda n: f"key-{n}",
        ),
        write_mcp_json=lambda n: print("    [mcp]  regenerate .mcp.json (SSE↔stdio)"),
        disconnect_sessions=_disc,
        start_session=_start,
        has_live_session=lambda n: has_live,
        lifecycle_lock=lambda n: locks.setdefault(n, asyncio.Lock()),
        op_registry=_TracingRegistry(),
        runner=_sync_runner,
    )


class _TracingRegistry(cops.ContainerOpRegistry):
    def update(self, agent, status, message=None, **kw):
        rec = super().update(agent, status, message, **kw)
        print(f"    [op]   {status:<11} {message or ''}")
        return rec


async def part_b() -> None:
    hr("PART B · containerize lifecycle (real provisioner, recording podman)")
    ops = RecordingContainerOps()
    agent = Agent(name="dymok", model="opus", working_dir="/tmp/dymok", isolation_mode="local")
    reg = _LoudRegistry(agent)
    deps = _deps(agent, ops, reg, has_live=True)
    lc = cops.ContainerLifecycle(deps)

    print("\n  CONTAINERIZE dymok  (image=ghcr.io/acme/pinky-agent:1, was: local/sdk)")
    deps.op_registry.begin("dymok", cops.OP_CONTAINERIZE, image="ghcr.io/acme/pinky-agent:1")
    await lc.containerize("dymok", image="ghcr.io/acme/pinky-agent:1", start=True)
    n = ContainerNames.for_agent("dymok")
    print(f"    podman: volume={ops.volume_exists(n.volume)} secret={ops.secret_exists(n.secret)} "
          f"container={ops.container_exists(n.container)}")
    print(f"    agent now: isolation_mode={reg.get('dymok').isolation_mode} "
          f"transport={reg.get('dymok').transport}")

    print("\n  DECONTAINERIZE dymok  (return to local — home volume kept)")
    deps.op_registry.begin("dymok", cops.OP_DECONTAINERIZE)
    await lc.decontainerize("dymok", start=True)
    print(f"    podman: volume={ops.volume_exists(n.volume)} (KEPT) "
          f"secret={ops.secret_exists(n.secret)} container={ops.container_exists(n.container)}")
    print(f"    agent now: isolation_mode={reg.get('dymok').isolation_mode} "
          f"isolated={reg.get('dymok').isolated}")

    hr("PART C · failure safety (bad image → DB never touched)")
    ops2 = RecordingContainerOps(fail_predicate=lambda a: len(a) > 1 and a[1] == "exec")
    agent2 = Agent(name="ryzhik", model="opus", working_dir="/tmp/ryzhik", isolation_mode="local")
    reg2 = _LoudRegistry(agent2)
    deps2 = _deps(agent2, ops2, reg2, has_live=True)
    lc2 = cops.ContainerLifecycle(deps2)
    print("\n  CONTAINERIZE ryzhik with an image MISSING tmux/claude/python3")
    deps2.op_registry.begin("ryzhik", cops.OP_CONTAINERIZE, image="busybox:latest")
    await lc2.containerize("ryzhik", image="busybox:latest", start=True)
    print(f"    persisted? {bool(reg2.register_calls)}  "
          f"agent still: isolation_mode={reg2.get('ryzhik').isolation_mode}  ✅ intact")


def main() -> None:
    part_a()
    asyncio.run(part_b())
    print("\n✅ demo complete\n")


if __name__ == "__main__":
    main()
