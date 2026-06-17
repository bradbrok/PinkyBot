"""A fake stdio ``codex app-server`` for shim/supervisor tests.

Speaks the same NDJSON dialect CodexAppServerClient expects (see
codex_app_server.py): one JSON object per line, requests carry ``id`` +
``method``, responses carry ``id`` + ``result``. Lets the bridge be exercised
end-to-end with no real ``codex`` binary.

  * ``initialize``         -> {"id", "result": {"userAgent": "fake/1", ...}}
  * any other request      -> {"id", "result": {"echo": <params>}}  (round-trips
                              arbitrary payloads, incl. frames >64 KiB)
  * notifications (no id)  -> ignored

Env knobs:
  * FAKE_AS_EXIT_AFTER_INIT=1  -> exit right after answering initialize (used to
    test the child-dies-under-the-bridge path)
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    exit_after_init = os.environ.get("FAKE_AS_EXIT_AFTER_INIT") == "1"
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
            resp = {"id": mid, "result": {"userAgent": "fake/1", "ok": True}}
        else:
            resp = {"id": mid, "result": {"echo": msg.get("params")}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
        if method == "initialize" and exit_after_init:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
