"""Pinned SDK protocol-phase characterization without launching a real CLI.

The scripted peer establishes where each wire sequence is surfaced; it does
not establish which sequence a particular CLI emits for a missing local file.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._errors import ProcessError
from claude_agent_sdk._internal.transport import Transport
from claude_agent_sdk.client import ClaudeSDKClient


class ScriptedPeer(Transport):
    def __init__(self, phase):
        self.phase = phase
        self.messages = asyncio.Queue()
        self.closed = False
        self.writes = []

    async def connect(self):
        pass

    async def write(self, data):
        frame = json.loads(data)
        self.writes.append(frame)
        initialize = frame.get("type") == "control_request"
        if initialize and self.phase == "query":
            await self.messages.put({
                "type": "control_response", "response": {
                    "subtype": "success", "request_id": frame["request_id"],
                    "response": {"commands": []},
                },
            })
        elif initialize or frame.get("type") == "user":
            await self.messages.put({
                "type": "result", "subtype": "error_during_execution", "is_error": True,
                "errors": ["No conversation found with session ID: missing-target"],
                "duration_ms": 0, "duration_api_ms": 0, "num_turns": 0,
                "session_id": "", "total_cost_usd": 0,
            })
            await self.messages.put(ProcessError("process exited", exit_code=1))

    async def read_messages(self):
        while True:
            frame = await self.messages.get()
            if frame is None:
                return
            if isinstance(frame, BaseException):
                raise frame
            yield frame

    async def close(self):
        self.closed = True
        await self.messages.put(None)

    def is_ready(self):
        return not self.closed

    async def end_input(self):
        pass


async def test_initialize_rejection_is_raised_by_real_sdk_connect():
    peer = ScriptedPeer("initialize")
    client = ClaudeSDKClient(ClaudeAgentOptions(resume="missing-target"), transport=peer)
    with pytest.raises(ProcessError, match="No conversation found"):
        await asyncio.wait_for(client.connect(), 2)
    assert peer.closed
    assert all(frame["type"] != "user" for frame in peer.writes)


async def test_post_initialize_rejection_is_observed_on_read_after_query():
    peer = ScriptedPeer("query")
    client = ClaudeSDKClient(ClaudeAgentOptions(resume="missing-target"), transport=peer)
    try:
        await asyncio.wait_for(client.connect(), 2)
        assert not peer.closed
        await client.query("One prompt")
        with pytest.raises(Exception, match="No conversation found"):
            async for _ in client.receive_messages():
                pass
        assert len([frame for frame in peer.writes if frame["type"] == "user"]) == 1
    finally:
        await client.disconnect()
    assert peer.closed
