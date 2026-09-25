"""Read the text out of an Anthropic Messages API response.

On models with extended thinking (always on for Claude Opus 5.5), the first
content block is often a ``thinking`` block, so positional reads like
``response.content[0].text`` break. Select blocks by ``type`` instead.
https://platform.claude.com/docs/en/models/opus-5-5/migration-guide
"""

from __future__ import annotations

from typing import Any


def message_text(response: Any) -> str:
    """Join the text of every ``text`` block in a Messages API response.

    Raises ValueError when there is no text (empty content, only thinking or
    tool_use blocks, or output cut off by max_tokens before any text) so callers
    fail loudly instead of treating "" as a result.
    """
    blocks = getattr(response, "content", None) or []
    text = "".join(block.text for block in blocks if getattr(block, "type", None) == "text")
    if not text:
        raise ValueError(
            "no text block in Claude response "
            f"(stop_reason={getattr(response, 'stop_reason', None)!r}, "
            f"block_types={[getattr(block, 'type', None) for block in blocks]})"
        )
    return text
