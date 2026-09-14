"""Standalone managed hook template; no daemon imports are needed inside a pane."""

HOOK_TEMPLATE = r'''#!/usr/bin/env python3
import json
import math
import os
import threading
import sys
import time

_STARTED = time.monotonic()
_RESULT_LOCK = threading.Lock()
_RESULT_CODE = None
AGENT = __POLICY_AGENT__
STATIC = frozenset(__POLICY_STATIC__)
DEADLINE = __POLICY_DEADLINE__
UNAVAILABLE = __POLICY_UNAVAILABLE__
TIMEOUT = __POLICY_TIMEOUT__


class DeadlineExpired(BaseException):
    pass


def effective_deadline(env):
    try:
        value = float(env.get("PINKY_TOOL_POLICY_HOOK_DEADLINE_SEC", DEADLINE))
        return min(DEADLINE, value) if math.isfinite(value) and value > 0 else DEADLINE
    except (ValueError, TypeError):
        return DEADLINE


def _write_result(decision, reason=None):
    global _RESULT_CODE
    detail = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if reason is not None:
        detail["permissionDecisionReason"] = reason
    print(json.dumps({"hookSpecificOutput": detail}), flush=True)
    _RESULT_CODE = 0 if decision == "allow" else 2
    return _RESULT_CODE


def emit(decision, reason=None):
    with _RESULT_LOCK:
        if _RESULT_CODE is not None:
            return _RESULT_CODE
        return _write_result(decision, reason)


def hard_timeout():
    # An independent thread terminates even when stdin, DNS, or a native read
    # cannot unwind on a signal. Serialize the one terminal response.
    with _RESULT_LOCK:
        if _RESULT_CODE is None:
            _write_result("deny", TIMEOUT)
            os._exit(2)


def request(method, path, payload, secret, deadline):
    import base64
    import hashlib
    import hmac
    import http.client
    import urllib.error
    import urllib.request

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.timeout = min(0.5, max(0.001, deadline - time.monotonic()))
            super().connect()
            self.sock.settimeout(max(0.001, deadline - time.monotonic()))

    class SecureConnection(http.client.HTTPSConnection):
        def connect(self):
            self.timeout = min(0.5, max(0.001, deadline - time.monotonic()))
            super().connect()
            self.sock.settimeout(max(0.001, deadline - time.monotonic()))

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(Connection, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(SecureConnection, req, context=self._context)

    # The configured daemon is a direct destination; ambient proxy discovery
    # must not extend the connection-attempt budget or forward signed calls.
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.ProxyHandler({}), HTTPHandler(), HTTPSHandler(),
    ))
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExpired()
        ts = int(time.time())
        signature = base64.urlsafe_b64encode(hmac.new(
            secret.encode(), f"{AGENT}\n{method}\n{path.split('?', 1)[0]}\n{ts}".encode(), hashlib.sha256,
        ).digest()).decode().rstrip("=")
        req = urllib.request.Request(
            os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888").rstrip("/") + path,
            data=None if payload is None else json.dumps(payload).encode(), method=method,
            headers={"Content-Type": "application/json", "X-Pinky-Agent": AGENT,
                     "X-Pinky-Timestamp": str(ts), "X-Pinky-Signature": signature},
        )
        try:
            with urllib.request.urlopen(req, timeout=remaining) as response:
                result = json.loads(response.read())
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            return result
        except urllib.error.HTTPError:
            raise ValueError("policy request rejected") from None
        except (urllib.error.URLError, ConnectionError, OSError):
            if attempt == 2:
                raise ConnectionError("policy service unavailable") from None
            time.sleep(1)


def run_call(deadline):
    body = json.load(sys.stdin)
    if not isinstance(body, dict) or not isinstance(body.get("tool_name"), str) or not isinstance(
        body.get("tool_input"), dict
    ):
        raise ValueError("invalid input")
    if body["tool_name"] in STATIC:
        return emit("allow")
    if not all(isinstance(body.get(key), str) and body[key] for key in ("session_id", "tool_use_id")):
        raise ValueError("missing call identity")
    secret = os.environ.get("PINKY_AGENT_KEY", "").strip() or os.environ.get("PINKY_SESSION_SECRET", "").strip()
    if not secret:
        return emit("deny", "Denied: missing PINKY_AGENT_KEY or PINKY_SESSION_SECRET.")
    import hashlib
    from pathlib import Path

    own_path = Path(__file__)
    payload = {key: body[key] for key in ("session_id", "tool_use_id", "tool_name", "tool_input")}
    payload.update(transport="tmux", principal_class="unknown", cwd=body.get("cwd", ""),
                   hook_sha256=hashlib.sha256(own_path.read_bytes()).hexdigest(),
                   settings_sha256=hashlib.sha256(own_path.with_name("settings.json").read_bytes()).hexdigest())
    response = request("POST", f"/agents/{AGENT}/policy/evaluate", payload, secret, deadline)
    decision = response.get("decision")
    if decision == "allow":
        return emit("allow")
    if decision == "deny":
        reason = response.get("reason")
        if not isinstance(reason, str) or not reason or "\n" in reason:
            raise ValueError("invalid deny response")
        return emit("deny", reason)
    if decision != "pause":
        raise ValueError("invalid decision")
    import re
    pending_id = response.get("pending_id")
    if not isinstance(pending_id, str) or not re.fullmatch(r"tp_[0-9a-f]{16}", pending_id):
        raise ValueError("invalid pending id")
    while True:
        response = request("GET", f"/agents/{AGENT}/policy/pending/{pending_id}?wait=25",
                           None, secret, deadline)
        if response.get("state") == "pending":
            continue
        if response.get("state") != "resolved" or response.get("result") not in {"allow", "deny"}:
            raise ValueError("invalid resolution")
        if response["result"] == "allow":
            return emit("allow")
        return emit("deny", TIMEOUT if response.get("resolved_by") == "timeout"
                    else "Denied: owner declined this tool call; do not retry automatically.")


def main():
    if os.environ.get("PINKY_TOOL_POLICY", "off") == "off":
        return 0
    deadline = _STARTED + effective_deadline(os.environ)
    timer = threading.Timer(max(0, deadline - time.monotonic()), hard_timeout)
    timer.daemon = True
    timer.start()
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExpired()
        return run_call(deadline)
    except DeadlineExpired:
        return emit("deny", TIMEOUT)
    except Exception:
        return emit("deny", TIMEOUT if time.monotonic() >= deadline else UNAVAILABLE)
    finally:
        timer.cancel()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(emit("deny", UNAVAILABLE))
'''
