"""Synthetic credentials must not cross exec diagnostics or worker logging."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.codex_session import CodexSession
from pinky_daemon.resume_recovery import drain_diagnostic
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.transport_state import SessionState

TOKEN = "synthetic-secret-value"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c3ludGhldGlj"
DIAGNOSTICS = [
    ("json-key", '{"api_key": "' + TOKEN + '"}', TOKEN),
    ("json-token", '{"token": "' + TOKEN + '"}', TOKEN),
    ("basic", "Authorization: Basic " + TOKEN, TOKEN),
    ("bearer", "Authorization: Bearer " + TOKEN, TOKEN),
    ("url", "https://user:" + TOKEN + "@proxy.invalid/v1", TOKEN),
    ("url-percent", "https://user:synthetic%2Dsecret%2Dvalue@proxy.invalid", "synthetic%2Dsecret"),
    ("key", "upstream rejected sk-" + TOKEN, TOKEN),
    ("jwt", "upstream rejected " + JWT, JWT),
    ("ansi", '{"api_key": "\x1b[31m' + TOKEN + '\x1b[0m"}', TOKEN),
    ("multiline", '{\n "TOKEN" :\n "' + TOKEN + '"\n}', TOKEN),
    ("escaped", '{"api_key": "synthetic\\"secret-value"}', "synthetic"),
    ("unterminated-json", '{"token":"' + TOKEN, TOKEN),
    ("unterminated-url", "https://user:" + TOKEN, TOKEN),
    ("input-boundary", " " * 4078 + '{"token":"' + TOKEN + '"}', "synthe"),
    ("url-boundary", " " * 4060 + "https://user:" + TOKEN + "@proxy.invalid", "synthetic"),
    ("output-cap", '{"token":"' + TOKEN * 100 + '"} SAFE_SUFFIX', TOKEN),
]


class ChunkStream:
    def __init__(self, payload, chunk_size):
        self.payload = payload
        self.position = 0
        self.chunk_size = chunk_size
        self.read_sizes = []
        self.eof = False

    async def read(self, size):
        self.read_sizes.append(size)
        assert 0 < size <= 4096, "Unbounded stderr read"
        if self.position == len(self.payload):
            self.eof = True
            return b""
        stop = min(len(self.payload), self.position + min(size, self.chunk_size))
        chunk = self.payload[self.position : stop]
        self.position = stop
        await asyncio.sleep(0)
        return chunk


@pytest.mark.parametrize("case,diagnostic,secret", DIAGNOSTICS, ids=[c[0] for c in DIAGNOSTICS])
@pytest.mark.parametrize("chunk_size", [3, 4096], ids=["split", "whole"])
async def test_exec_drain_result_and_worker_log_redact_credentials(
    tmp_path,
    monkeypatch,
    case,
    diagnostic,
    secret,
    chunk_size,
):
    monkeypatch.setenv("PINKY_RESUME_FAILSAFE", "1")
    ss = CodexSession(StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path)))
    ss._use_app_server = False
    ss._state_machine._state = SessionState.CONNECTED
    stdout = asyncio.StreamReader()
    stdout.feed_eof()
    stderr = ChunkStream((diagnostic + "\n" + " " * 9000).encode(), chunk_size)
    proc = SimpleNamespace(
        stdin=SimpleNamespace(
            write=MagicMock(), drain=AsyncMock(), close=MagicMock(), wait_closed=AsyncMock()
        ),
        stdout=stdout,
        stderr=stderr,
        returncode=1,
        wait=AsyncMock(return_value=1),
        kill=MagicMock(),
    )
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("pinky_daemon.codex_session.asyncio.create_subprocess_exec", spawn)
    results, logs = [], []
    completed = asyncio.Event()
    real_exec = ss._exec_codex

    async def observe_exec(*args, **kwargs):
        result = await real_exec(*args, **kwargs)
        results.append(result)
        return result

    def log(line):
        logs.append(line)
        if "turn failed:" in line:
            completed.set()

    monkeypatch.setattr(ss, "_exec_codex", observe_exec)
    monkeypatch.setattr("pinky_daemon.codex_session._log", log)
    accepted = MagicMock(return_value=True)
    receipt = await ss.send_scheduler_prompt("One prompt", on_accept=accepted)
    worker = asyncio.create_task(ss._message_worker())
    try:
        await asyncio.wait_for(completed.wait(), 3)
        assert await receipt is True
        assert spawn.await_count == accepted.call_count == 1
        proc.stdin.write.assert_called_once_with(b"One prompt")
        proc.stdin.close.assert_called_once()
        assert stderr.eof and stderr.position == len(stderr.payload)
        assert len(results) == 1 and results[0].failed
        assert all(len(error) <= 1024 for error in results[0].errors)
        assert any("turn failed:" in line for line in logs), "Worker log sink was not reached"
        assert secret not in " ".join(results[0].errors), "Credential survived result retention"
        assert secret not in "\n".join(logs), "Credential survived diagnostic/worker log sink"
        assert "[redacted]" in " ".join(results[0].errors)
        if case == "output-cap":
            assert "SAFE_SUFFIX" in " ".join(results[0].errors), "Cap applied before redaction"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await ss.disconnect()


async def test_stderr_drain_consumes_large_stream_with_bounded_retention():
    import tracemalloc

    class GeneratedStream:
        remaining = 8 * 1024 * 1024
        eof = False

        async def read(self, size):
            assert 0 < size <= 4096
            if not self.remaining:
                self.eof = True
                return b""
            count = min(size, self.remaining)
            self.remaining -= count
            return b"x" * count

    stream = GeneratedStream()
    tracemalloc.start()
    try:
        diagnostic = await drain_diagnostic(stream)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert stream.eof
    assert len(diagnostic) <= 1024
    assert peak < 512 * 1024, "Drain retained the raw stream instead of a bounded prefix"


@pytest.mark.parametrize("diagnostic", ["permission denied", "request timed out", "exit code 1"])
async def test_harmless_diagnostic_is_preserved(diagnostic):
    stream = ChunkStream(diagnostic.encode(), 3)
    assert await drain_diagnostic(stream) == diagnostic
    assert stream.eof
