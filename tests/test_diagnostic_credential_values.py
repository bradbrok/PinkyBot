"""Credential values remain private across quoting, escape, and flag boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon.codex_session import CodexSession
from pinky_daemon.resume_recovery import sanitized_diagnostic
from pinky_daemon.streaming_session import StreamingSessionConfig
from tests import test_exec_diagnostic_boundaries as boundary

VALUES = [
    ("quoted-bare-key", 'token: "my secret value"', "secret value"),
    ("bearer-spaces", "Authorization: Bearer abc def", "def"),
    ("basic-spaces", "Authorization: Basic abc def", "def"),
    ("single-quote", "password='my secret value'", "secret value"),
    ("escaped-quote", 'secret: "my \\"secret value"', "secret value"),
    ("unterminated", 'token: "my secret value', "secret value"),
    ("json-bare-value", '{"api_key": bareword}', "bareword"),
    ("stripe-live", "upstream sk_live_synthetic123456", "synthetic123456"),
    ("stripe-test", "upstream sk_test_synthetic123456", "synthetic123456"),
    ("escape-key", "to\x1bken=synthetic123456", "synthetic123456"),
    ("escape-twochar", "to\x1b7ken=synthetic123456", "synthetic123456"),
    ("escape-osc", "to\x1b]0;title\x07ken=synthetic123456", "synthetic123456"),
    ("escape-osc-st", "to\x1b]0;title\x1b\\ken=synthetic123456", "synthetic123456"),
    ("cap-quoted", " " * 4070 + 'token: "my secret value with a tail"', "secret"),
]


@pytest.mark.parametrize("case,diagnostic,secret", VALUES, ids=[v[0] for v in VALUES])
@pytest.mark.parametrize("chunk_size", [3, 4096])
async def test_credential_values_at_both_real_exec_sinks(
    tmp_path, monkeypatch, case, diagnostic, secret, chunk_size,
):
    await boundary.test_exec_drain_result_and_worker_log_redact_credentials(
        tmp_path, monkeypatch, case, diagnostic, secret, chunk_size,
    )


@pytest.mark.parametrize("flag", [None, "0", "1"])
@pytest.mark.parametrize("diagnostic,secret", [
    ('token: "my secret value"', "secret value"),
    ("Authorization: Bearer abc def", "def"),
    ('{"api_key": "synthetic123456"}', "synthetic123456"),
    ("permission denied", None),
])
async def test_exec_stderr_log_is_sanitized_independently_of_fallback_flag(
    tmp_path, monkeypatch, flag, diagnostic, secret,
):
    if flag is None:
        monkeypatch.delenv("PINKY_RESUME_FAILSAFE", raising=False)
    else:
        monkeypatch.setenv("PINKY_RESUME_FAILSAFE", flag)
    ss = CodexSession(StreamingSessionConfig(agent_name="sample", working_dir=str(tmp_path)))
    ss._use_app_server = False
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_eof()
    stderr.feed_data(diagnostic.encode())
    stderr.feed_eof()
    proc = SimpleNamespace(
        stdin=SimpleNamespace(write=MagicMock(), drain=AsyncMock(), close=MagicMock(),
                              wait_closed=AsyncMock()),
        stdout=stdout, stderr=stderr, returncode=1, wait=AsyncMock(return_value=1),
        kill=MagicMock(),
    )
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("pinky_daemon.codex_session.asyncio.create_subprocess_exec", spawn)
    logs = []
    monkeypatch.setattr("pinky_daemon.codex_session._log", logs.append)
    result = await ss._exec_codex("One prompt")
    stderr_logs = [line for line in logs if ": stderr:" in line]
    assert len(stderr_logs) == 1
    assert spawn.await_count == 1
    proc.stdin.write.assert_called_once_with(b"One prompt")
    proc.stdin.close.assert_called_once()
    assert result.failed
    if secret:
        assert secret not in stderr_logs[0]
        assert "[redacted]" in stderr_logs[0]
    else:
        assert diagnostic in stderr_logs[0]
    if flag != "1":
        assert result.errors == ["codex exited with code 1"]
    else:
        assert result.errors == [sanitized_diagnostic(diagnostic)]


@pytest.mark.parametrize("value", [
    "request timed out", "permission denied", "https://example.invalid:443/path",
    "some harmless text after an ordinary error",
])
def test_harmless_values_remain_readable(value):
    assert sanitized_diagnostic(value) == value
