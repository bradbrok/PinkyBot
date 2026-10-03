"""System prompts reach the SDK with snapshot disabled.

Claude Code >= 2.1.265 records a custom system prompt on the first request
and reuses it after resume unless the session opts out. Agents rebuild their
prompt from soul, directives and skills, so a resumed session must pick up
the new prompt instead of the recorded one.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import ClaudeSDKClient, Transport

from pinky_daemon.sdk_runner import SDKRunner, SDKRunnerConfig
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig

EXPECTED = {"type": "custom", "prompt": "SOUL v2", "snapshot": False}


class CaptureTransport(Transport):
    """Answers control requests and records every line the SDK writes."""

    def __init__(self) -> None:
        self.writes: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        pass

    async def write(self, data: str) -> None:
        message = json.loads(data)
        self.writes.append(message)
        if message.get("type") == "control_request":
            await self._inbox.put({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": message["request_id"],
                    "response": {},
                },
            })

    async def read_messages(self):
        while (item := await self._inbox.get()) is not None:
            yield item

    async def close(self) -> None:
        await self._inbox.put(None)

    def is_ready(self) -> bool:
        return True

    async def end_input(self) -> None:
        pass


async def _streaming_options(tmp_path, monkeypatch, resume_handle=""):
    stop = RuntimeError("captured")
    client = SimpleNamespace(connect=AsyncMock(side_effect=stop), disconnect=AsyncMock())
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", factory)
    session = StreamingSession(StreamingSessionConfig(
        agent_name="sample",
        working_dir=str(tmp_path),
        system_prompt="SOUL v2",
        resume_handle=resume_handle,
    ))
    with pytest.raises(RuntimeError):
        await session.connect()
    return factory.call_args.args[0]


@pytest.mark.parametrize("resume_handle", ["", "saved-session"])
async def test_streaming_session_disables_prompt_snapshot(tmp_path, monkeypatch, resume_handle):
    options = await _streaming_options(tmp_path, monkeypatch, resume_handle)
    assert options.system_prompt == EXPECTED


async def test_streaming_initialize_sends_snapshot_false(tmp_path, monkeypatch):
    options = await _streaming_options(tmp_path, monkeypatch, "saved-session")
    monkeypatch.undo()
    transport = CaptureTransport()
    client = ClaudeSDKClient(options, transport=transport)
    await client.connect()
    try:
        initialize = [
            w["request"] for w in transport.writes
            if w.get("type") == "control_request" and w["request"]["subtype"] == "initialize"
        ]
        assert len(initialize) == 1
        assert initialize[0]["systemPromptSnapshot"] is False
    finally:
        await client.disconnect()


async def test_sdk_runner_disables_prompt_snapshot(monkeypatch):
    captured = {}

    async def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options
        return
        yield

    monkeypatch.setattr("claude_agent_sdk.query", fake_query)
    runner = SDKRunner(SDKRunnerConfig(working_dir="/tmp"))
    await runner.run("hello", system_prompt="SOUL v2", session_id="saved-session", resume=True)
    assert captured["options"].system_prompt == EXPECTED
