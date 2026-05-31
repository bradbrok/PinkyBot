"""Unified completed-turn payload for agent transport backends."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(init=False)
class TurnResponse:
    """Completed assistant turn details for broker/API handling.

    The SDK, Codex CLI, and tmux transcript backends all emit this single
    callback shape. Transport-specific parsers may populate only the fields
    they know; routing consumers can rely on the routing fields, text,
    tool-use fields, and usage dictionaries being present.
    """

    agent_name: str = ""
    session_id: str = ""
    platform: str = ""
    chat_id: str = ""
    message_id: str = ""
    text: str = ""
    thinking: str = ""
    tool_uses: list[dict] = field(default_factory=list)
    used_outreach_tools: bool = False
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)
    model: str = ""
    model_usage: dict = field(default_factory=dict)
    total_cost_usd: float = 0.0
    num_turns: int = 0
    prevented_continuation: bool = False
    duration_ms: int = 0
    assistant_entry_count: int = 0

    def __init__(
        self,
        *,
        agent_name: str = "",
        session_id: str = "",
        platform: str = "",
        chat_id: str = "",
        message_id: str = "",
        text: str = "",
        response_text: str | None = None,
        thinking: str = "",
        tool_uses: list[dict] | None = None,
        used_outreach_tools: bool = False,
        stop_reason: str = "",
        usage: dict | None = None,
        model: str = "",
        model_usage: dict | None = None,
        total_cost_usd: float = 0.0,
        num_turns: int = 0,
        prevented_continuation: bool = False,
        duration_ms: int = 0,
        assistant_entry_count: int = 0,
    ) -> None:
        self.agent_name = agent_name
        self.session_id = session_id
        self.platform = platform
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text if response_text is None else response_text
        self.thinking = thinking
        self.tool_uses = list(tool_uses or [])
        self.used_outreach_tools = used_outreach_tools
        self.stop_reason = stop_reason
        self.usage = dict(usage or {})
        self.model = model
        self.model_usage = dict(model_usage or {})
        self.total_cost_usd = total_cost_usd
        self.num_turns = num_turns
        self.prevented_continuation = prevented_continuation
        self.duration_ms = duration_ms
        self.assistant_entry_count = assistant_entry_count

    @property
    def response_text(self) -> str:
        """Compatibility alias for the pre-#498 API callback field."""
        return self.text

    @response_text.setter
    def response_text(self, value: str) -> None:
        self.text = value
