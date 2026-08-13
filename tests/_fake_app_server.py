"""A fake stdio ``codex app-server`` for shim/supervisor tests.

Speaks the same NDJSON dialect CodexAppServerClient expects (see
codex_app_server.py): one JSON object per line, requests carry ``id`` +
``method``, responses carry ``id`` + ``result``. Lets the bridge be exercised
end-to-end with no real ``codex`` binary.

  * default mode: ``initialize`` returns a small success and every other
    request echoes params (including frames >64 KiB)
  * phase-1 modes exercise the 0.144 lifecycle used by CodexSession: happy
    turns, init hang/error/death, terminal error notification, and exit after
    a completed turn; EOF-only modes close stdout while the process and stdin
    remain alive
  * notifications (no id)  -> ignored

Env knobs:
  * FAKE_AS_EXIT_AFTER_INIT=1  -> exit right after answering initialize (used to
    test the child-dies-under-the-bridge path)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

_PHASE1_MODES = {
    "happy",
    "error-notification",
    "eof-after-acceptance",
    "eof-after-terminal",
    "exit-after-turn",
}


def _send(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _close_stdout() -> None:
    """Emit EOF to the parent while keeping this process and stdin alive."""
    sys.stdout.flush()
    os.close(sys.stdout.fileno())


def _thread(thread_id: str) -> dict:
    """Minimal 0.144-shaped Thread object for scripted responses."""
    now = int(time.time())
    return {
        "id": thread_id,
        "preview": "",
        "ephemeral": False,
        "modelProvider": "openai",
        "createdAt": now,
        "updatedAt": now,
        "status": {"type": "idle"},
        "cwd": os.getcwd(),
        "cliVersion": "0.144.0-fake",
        "source": "appServer",
        "sessionId": thread_id,
        "turns": [],
    }


def _thread_response(thread_id: str) -> dict:
    return {
        "thread": _thread(thread_id),
        "model": "gpt-5",
        "modelProvider": "openai",
        "cwd": os.getcwd(),
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": {"type": "dangerFullAccess"},
    }


def _turn(turn_id: str, status: str) -> dict:
    return {"id": turn_id, "items": [], "status": status, "error": None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "echo",
            "happy",
            "hang-init",
            "error-init",
            "die-pre-init",
            "error-notification",
            "backpressure-eof",
            "eof-between-turns",
            "eof-after-acceptance",
            "eof-after-terminal",
            "exit-after-turn",
        ),
        default="echo",
    )
    mode = parser.parse_args(argv).mode
    exit_after_init = os.environ.get("FAKE_AS_EXIT_AFTER_INIT") == "1"
    thread_id = f"fake-thread-{os.getpid()}"
    turn_count = 0
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            continue  # notification — ignore
        if method == "initialize":
            if mode == "hang-init":
                continue
            if mode == "error-init":
                _send({"id": mid, "error": {"code": -32000, "message": "init refused"}})
                continue
            if mode == "die-pre-init":
                return 7
            _send({
                "id": mid,
                "result": {
                    "userAgent": "fake/1",
                    "codexHome": os.getcwd(),
                    "platformFamily": "unix",
                    "platformOs": "fake",
                },
            })
            if mode == "backpressure-eof":
                # Wait for the first byte of the next request, then stop
                # reading stdin so the parent's 16 MiB write backpressures.
                # Close only stdout: the process and stdin stay live until the
                # test explicitly tears the child down.
                os.read(sys.stdin.fileno(), 1)
                time.sleep(0.05)
                _close_stdout()
                while True:
                    time.sleep(3600)
            if mode == "eof-between-turns":
                _close_stdout()
            if exit_after_init:
                return 0
            continue

        if mode not in _PHASE1_MODES:
            _send({"id": mid, "result": {"echo": msg.get("params")}})
            continue

        if method == "thread/start":
            _send({"id": mid, "result": _thread_response(thread_id)})
            continue
        if method == "thread/resume":
            thread_id = (msg.get("params") or {}).get("threadId", thread_id)
            _send({"id": mid, "result": _thread_response(thread_id)})
            continue
        if method == "turn/start":
            turn_count += 1
            turn_id = f"fake-turn-{turn_count}"
            _send({"id": mid, "result": {"turn": _turn(turn_id, "inProgress")}})
            if mode == "eof-after-acceptance":
                # Exact May-wedge shape: prompt acceptance is positive, then
                # the child exits cleanly without any terminal notification.
                return 0
            if mode == "error-notification":
                _send({
                    "method": "error",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "willRetry": False,
                        "error": {"message": "scripted terminal error"},
                    },
                })
                continue
            _send({
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": "item-1",
                        "type": "agentMessage",
                        "text": "fake app-server reply",
                    },
                },
            })
            _send({
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": _turn(turn_id, "completed"),
                },
            })
            if mode == "eof-after-terminal":
                _close_stdout()
            if mode == "exit-after-turn":
                return 0
            continue

        _send({"id": mid, "result": {}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
