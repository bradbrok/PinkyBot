"""Generated hook subprocesses must answer before the platform timeout."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from pinky_daemon import agent_registry
from pinky_daemon.auth import verify_internal_request

UNREACHABLE = "Denied: policy service unavailable; retry the same call in 10 seconds."
TIMEOUT = (
    "Denied: no owner decision within the approval window; do not retry automatically, "
    "tell the owner what you needed."
)
RULE_DENY = "Denied by policy rule outbound.broadcast: broadcast requires owner review."
SECRET = "hook-test-secret"


def _script(tmp_path):
    source = getattr(agent_registry, "_tool_policy_hook_source", None)
    assert callable(source), "missing managed tool-policy hook generator"
    directory = tmp_path / ".claude"
    directory.mkdir(exist_ok=True)
    path = directory / "hook_tool_policy.py"
    path.write_text(source("sample"))
    (directory / "settings.json").write_text('{"hooks":{}}\n')
    return path


def _payload(tool="mcp__pinky-messaging__broadcast"):
    return json.dumps(dict(session_id="session-1", tool_use_id="tool-1", tool_name=tool,
                           tool_input={"text": "private-input"}, cwd="/workspace"))


def _env(url="http://192.0.2.1:9", **changes):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PINKY_")}
    env.update(PINKY_TOOL_POLICY="enforce", PINKY_AGENT_KEY=SECRET, PINKY_DAEMON_URL=url,
               PINKY_TOOL_POLICY_HOOK_DEADLINE_SEC="4")
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _run(path, env, payload=None, timeout=8, wrapper=None):
    command = [sys.executable, str(path)] if wrapper is None else [sys.executable, "-c", wrapper, str(path)]
    return subprocess.run(command, input=_payload() if payload is None else payload,
                          env=env, capture_output=True, text=True, timeout=timeout)


def _decision(result, decision, reason=None):
    assert result.returncode == (0 if decision == "allow" else 2), result.stderr
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert output["hookSpecificOutput"]["permissionDecision"] == decision
    if reason is not None:
        assert output["hookSpecificOutput"]["permissionDecisionReason"] == reason
    if decision == "deny":
        rendered = output["hookSpecificOutput"]["permissionDecisionReason"]
        assert "private-input" not in rendered
        assert "tool-1" not in rendered
        assert "session-1" not in rendered
        assert "sample" not in rendered


@contextmanager
def _daemon(responses):
    requests = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(dict(method=self.command, path=self.path,
                                 headers=dict(self.headers), body=json.loads(body) if body else None))
            response = responses[min(len(requests) - 1, len(responses) - 1)]
            if response == "silent":
                release.wait(timeout=5)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps(response).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_POST = respond  # noqa: N815 - http.server protocol
        do_GET = respond  # noqa: N815 - http.server protocol

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("mode", [None, "off"])
def test_off_has_no_request_and_does_not_parse_stdin(tmp_path, mode):
    path = _script(tmp_path)
    with _daemon([{"decision": "deny"}]) as (url, requests):
        result = _run(path, _env(url, PINKY_TOOL_POLICY=mode), payload="malformed")
        assert result.returncode == 0
        assert requests == []


@pytest.mark.parametrize("tool", ["Read", "TodoWrite", "mcp__pinky-memory__recall"])
def test_static_fast_path_has_no_http_or_source_settings_reads(tmp_path, tool):
    path = _script(tmp_path)
    # Compile before the guard so only the generated hook's runtime reads are checked.
    wrapper = '''import pathlib, sys
path = pathlib.Path(sys.argv[1])
code = compile(path.read_text(), str(path), "exec")
def guard(event, args):
    if event == "open" and str(args[0]) in {str(path), str(path.parent / "settings.json")}:
        raise RuntimeError("policy fast path attempted a managed-file read")
    if event in {"socket.connect", "sqlite3.connect"}:
        raise RuntimeError("policy fast path attempted I/O")
sys.addaudithook(guard)
exec(code, {"__name__": "__main__", "__file__": str(path)})
'''
    with _daemon([{"decision": "deny"}]) as (url, requests):
        result = _run(path, _env(url), payload=_payload(tool), wrapper=wrapper)
        _decision(result, "allow")
        assert requests == []


@pytest.mark.parametrize("tool", ["WebFetch", "WebSearch"])
def test_outbound_web_tools_are_evaluated_not_static(tmp_path, tool):
    path = _script(tmp_path)
    with _daemon([{"decision": "allow"}]) as (url, requests):
        _decision(_run(path, _env(url), payload=_payload(tool)), "allow")
        assert len(requests) == 1


@pytest.mark.parametrize("key_source", ["agent", "fallback"])
def test_allow_request_has_valid_signature_hashes_and_unknown_principal(tmp_path, key_source):
    path = _script(tmp_path)
    with _daemon([{"decision": "allow"}]) as (url, requests):
        env = _env(url)
        if key_source == "fallback":
            env.pop("PINKY_AGENT_KEY")
            env["PINKY_SESSION_SECRET"] = SECRET
        _decision(_run(path, env), "allow")
        [request] = requests
        headers = {key.lower(): value for key, value in request["headers"].items()}
        assert request["path"] == "/agents/sample/policy/evaluate"
        assert verify_internal_request(
            SECRET, agent_name="sample", method="POST", path=request["path"],
            timestamp=headers["x-pinky-timestamp"], signature=headers["x-pinky-signature"],
        )
        body = request["body"]
        assert body["principal_class"] == "unknown"
        assert body["tool_use_id"] == "tool-1"
        assert body["hook_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert body["settings_sha256"] == hashlib.sha256((path.parent / "settings.json").read_bytes()).hexdigest()


def test_rule_deny_is_exit_two_and_has_fixed_model_reason(tmp_path):
    path = _script(tmp_path)
    with _daemon([{"decision": "deny", "reason": RULE_DENY, "rule_id": "outbound.broadcast"}]) as (url, _):
        _decision(_run(path, _env(url)), "deny", RULE_DENY)


@pytest.mark.parametrize("result", ["allow", "deny"])
def test_pause_waits_for_signed_pending_resolution(tmp_path, result):
    path = _script(tmp_path)
    responses = [
        {"decision": "pause", "pending_id": "tp_0123456789abcdef", "deadline_ts": time.time() + 570,
         "poll_after_s": 25},
        {"state": "pending"},
        {"state": "resolved", "result": result, "resolved_by": "timeout" if result == "deny" else "owner:test",
         "reason": "no owner decision within the approval window" if result == "deny" else "reviewed"},
    ]
    with _daemon(responses) as (url, requests):
        response = _run(path, _env(url))
        _decision(response, result, TIMEOUT if result == "deny" else None)
        assert len(requests) == 3
        for request in requests[1:]:
            assert request["method"] == "GET"
            assert request["path"] == "/agents/sample/policy/pending/tp_0123456789abcdef?wait=25"
            headers = {key.lower(): value for key, value in request["headers"].items()}
            assert verify_internal_request(
                SECRET, agent_name="sample", method="GET", path=urlsplit(request["path"]).path,
                timestamp=headers["x-pinky-timestamp"], signature=headers["x-pinky-signature"],
            )


@pytest.mark.parametrize("phase", ["evaluate", "pending"])
def test_silent_daemon_cannot_outlive_hook_deadline(tmp_path, phase):
    path = _script(tmp_path)
    responses = ["silent"] if phase == "evaluate" else [
        {"decision": "pause", "pending_id": "tp_0123456789abcdef", "deadline_ts": time.time() + 570,
         "poll_after_s": 25}, "silent",
    ]
    with _daemon(responses) as (url, _):
        started = time.monotonic()
        response = _run(path, _env(url, PINKY_TOOL_POLICY_HOOK_DEADLINE_SEC="0.4"), timeout=3)
        assert 0.4 <= time.monotonic() - started < 3
        _decision(response, "deny", TIMEOUT)


def test_open_stdin_is_also_bounded_by_hook_deadline(tmp_path):
    path = _script(tmp_path)
    child = subprocess.Popen([sys.executable, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             env=_env(PINKY_TOOL_POLICY_HOOK_DEADLINE_SEC="0.4"))
    try:
        # Keep stdin open: timeout must cover parsing, not only the HTTP loop.
        child.wait(timeout=2)
        output = child.stdout.read()
        error = child.stderr.read()
        _decision(subprocess.CompletedProcess([], child.returncode, output, error), "deny", TIMEOUT)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()


def test_daemon_down_has_three_attempts_and_fixed_reason(tmp_path):
    path = _script(tmp_path)
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]  # reserved but not listening
        attempts = tmp_path / "attempts.txt"
        wrapper = '''import pathlib, sys, urllib.request
path = pathlib.Path(sys.argv[1])
code = compile(path.read_text(), str(path), "exec")
original = urllib.request.urlopen
def count(*args, **kwargs):
    with open(path.parent.parent / "attempts.txt", "a") as stream: stream.write("attempt\\n")
    return original(*args, **kwargs)
urllib.request.urlopen = count
exec(code, {"__name__": "__main__", "__file__": str(path)})
'''
        started = time.monotonic()
        response = _run(path, _env(f"http://127.0.0.1:{port}"), wrapper=wrapper)
        assert 2 <= time.monotonic() - started < 5
        _decision(response, "deny", UNREACHABLE)
        assert attempts.read_text().splitlines() == ["attempt"] * 3


@pytest.mark.parametrize("payload", ["not-json private-input", "[]", "null",
                                     '{"tool_name":"Bash","tool_input":[],"extra":"private-input"}'])
def test_malformed_stdin_denies_without_request(tmp_path, payload):
    path = _script(tmp_path)
    with _daemon([{"decision": "allow"}]) as (url, requests):
        _decision(_run(path, _env(url), payload=payload), "deny")
        assert requests == []


def test_missing_secret_names_required_env_and_denies(tmp_path):
    path = _script(tmp_path)
    with _daemon([{"decision": "allow"}]) as (url, requests):
        response = _run(path, _env(url, PINKY_AGENT_KEY=None, PINKY_SESSION_SECRET=None))
        _decision(response, "deny")
        assert "PINKY_AGENT_KEY" in response.stdout
        assert "PINKY_SESSION_SECRET" in response.stdout
        assert requests == []


@pytest.mark.parametrize("reply", [{"decision": "ask"}, [], {"state": "resolved"}])
def test_malformed_daemon_response_fails_closed(tmp_path, reply):
    path = _script(tmp_path)
    with _daemon([reply]) as (url, _):
        _decision(_run(path, _env(url)), "deny")


def test_unexpected_exception_becomes_sanitized_deny_json(tmp_path):
    path = _script(tmp_path)
    wrapper = '''import pathlib, sys, urllib.request
path = pathlib.Path(sys.argv[1])
code = compile(path.read_text(), str(path), "exec")
def broken(*args, **kwargs): raise RuntimeError("private-input tool-1 sample")
urllib.request.urlopen = broken
exec(code, {"__name__": "__main__", "__file__": str(path)})
'''
    response = _run(path, _env("http://127.0.0.1:1"), wrapper=wrapper)
    _decision(response, "deny")
    assert "Traceback" not in response.stderr


@pytest.mark.parametrize("override,expected", [(None, 600), ("5000", 600), ("4", 4)])
def test_hook_is_importable_and_deadline_override_can_only_shorten(tmp_path, monkeypatch, override, expected):
    path = _script(tmp_path)
    tree = ast.parse(path.read_text())
    assert any(isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
               for node in tree.body)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "effective_deadline" for n in ast.walk(main))
    spec = importlib.util.spec_from_file_location("generated_policy_hook", path)
    module = importlib.util.module_from_spec(spec)

    def unexpected(*args, **kwargs):
        raise AssertionError("importing the hook must not run main or perform I/O")

    # Load source before patching open; the guard covers module execution only.
    code = spec.loader.get_code(spec.name)
    with monkeypatch.context() as patch:
        patch.setattr("builtins.input", unexpected)
        patch.setattr("sys.stdin", type("Unreadable", (), {"read": unexpected})())
        patch.setattr("socket.socket", unexpected)
        patch.setattr("builtins.open", unexpected)
        patch.setattr("pathlib.Path.open", unexpected)
        exec(code, module.__dict__)
    assert callable(module.main)
    assert callable(module.effective_deadline)
    env = {} if override is None else {"PINKY_TOOL_POLICY_HOOK_DEADLINE_SEC": override}
    assert module.effective_deadline(env) == expected
