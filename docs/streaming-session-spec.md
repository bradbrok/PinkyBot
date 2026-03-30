# Streaming Session — Spec

## Overview

Replace the current `query()` based SDKRunner with `ClaudeSDKClient` for broker-connected agents. This gives us:
- Persistent bidirectional connection to Claude Code
- Messages stream into context as they arrive (no blocking)
- Agent responds when ready, broker routes response back to platform
- Interrupts supported (can inject priority messages)

## Current Architecture

```
TG User → BrokerPoller → MessageBroker → Session.send(prompt)
                                           └── SDKRunner.run(prompt)
                                                └── query() — one-shot, blocks until done
                                                     └── new CC subprocess per call
```

Session.send() holds an asyncio lock. While one message processes, others buffer in the broker's coalescing queue.

## New Architecture

```
TG User → BrokerPoller → MessageBroker → StreamingSession.send(prompt)
                                           └── ClaudeSDKClient.query(prompt)
                                                └── writes to persistent CC subprocess
                                                     └── non-blocking, returns immediately

StreamingSession._reader_loop():
    async for message in client.receive_messages():
        if ResultMessage → extract response, send back via broker callback
        if AssistantMessage → extract text blocks for response
```

## Components

### 1. StreamingSession (new: `src/pinky_daemon/streaming_session.py`)

Wraps `ClaudeSDKClient` with PinkyBot session semantics.

```python
class StreamingSession:
    """Persistent bidirectional Claude Code session via SDK client.

    Unlike Session which blocks on each send(), StreamingSession:
    - Connects once and stays connected
    - send() writes to transport and returns immediately
    - A background reader loop processes responses
    - Response callback fires when agent finishes a turn
    """

    def __init__(self, agent_name, model, working_dir,
                 response_callback, ...):
        """
        response_callback: async fn(agent_name, response_text)
            Called when the agent completes a response turn.
        """

    async def connect(self):
        """Connect to Claude Code. Starts the reader loop."""

    async def send(self, prompt: str) -> None:
        """Send a message to the agent. Non-blocking."""

    async def disconnect(self):
        """Disconnect from Claude Code."""

    @property
    def is_connected(self) -> bool

    @property
    def context_used_pct(self) -> float
```

### 2. Broker Integration

Update `MessageBroker._route_batch()` to use `StreamingSession.send()` instead of `Session.send()`. Since send() is non-blocking, the drain loop becomes simpler — just send and return, the reader loop handles responses.

The response callback wired into StreamingSession fires `_broker_send()` to deliver the agent's response back to the TG user.

### 3. SessionManager Changes

Add support for creating StreamingSession alongside regular Session. Broker-connected agents use StreamingSession; UI chat sessions keep using regular Session.

### 4. Reader Loop

The reader loop runs as a background task, consuming `client.receive_messages()`:

```python
async def _reader_loop(self):
    async for msg in self._client.receive_messages():
        if isinstance(msg, AssistantMessage):
            # Extract text from content blocks
            text = "\n".join(b.text for b in msg.content if hasattr(b, 'text'))
            if text:
                self._last_response = text
        elif isinstance(msg, ResultMessage):
            # Turn complete — fire response callback
            if self._last_response and self._response_callback:
                await self._response_callback(self._agent_name, self._last_response)
            self._last_response = ""
            self._stats["turns"] += 1
```

## Implementation Order

1. Build `streaming_session.py` — StreamingSession class
2. Update broker to detect and use StreamingSession
3. Update API startup to create StreamingSession for broker agents
4. Test with Barsik

## Files to Create/Modify

| File | Action |
|------|--------|
| `src/pinky_daemon/streaming_session.py` | **CREATE** |
| `src/pinky_daemon/broker.py` | MODIFY — use StreamingSession.send() |
| `src/pinky_daemon/api.py` | MODIFY — create StreamingSessions on startup |
