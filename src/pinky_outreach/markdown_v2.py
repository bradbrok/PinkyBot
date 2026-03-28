"""Telegram MarkdownV2 formatter.

Converts standard markdown to Telegram's MarkdownV2 format.

Telegram MarkdownV2 requires escaping these characters outside of
code blocks: _ * [ ] ( ) ~ ` > # + - = | { } . !

Reference: https://core.telegram.org/bots/api#markdownv2-style
"""

from __future__ import annotations

import re


# Characters that must be escaped in MarkdownV2 (outside code/pre blocks)
_ESCAPE_CHARS = r"_*[]()~`>#+=|{}.!-"
_ESCAPE_RE = re.compile(r"([" + re.escape(_ESCAPE_CHARS) + r"])")


def escape_v2(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    return _ESCAPE_RE.sub(r"\\\1", text)


def markdown_to_v2(text: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2.

    Handles:
    - **bold** -> *bold*
    - *italic* / _italic_ -> _italic_
    - `inline code` -> `inline code`
    - ```code blocks``` -> ```code blocks```
    - [text](url) -> [text](url)
    - ~~strikethrough~~ -> ~strikethrough~
    - > blockquote -> >blockquote
    - Proper escaping of special characters

    Args:
        text: Standard markdown text.

    Returns:
        Telegram MarkdownV2 formatted string.
    """
    if not text:
        return ""

    # Extract code blocks first (they don't get escaped)
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def save_code_block(match):
        lang = match.group(1) or ""
        code = match.group(2)
        idx = len(code_blocks)
        code_blocks.append(f"```{lang}\n{code}```")
        return f"\x00CODEBLOCK{idx}\x00"

    def save_inline_code(match):
        code = match.group(1)
        idx = len(inline_codes)
        inline_codes.append(f"`{code}`")
        return f"\x00INLINE{idx}\x00"

    # Preserve code blocks
    result = re.sub(r"```(\w*)\n([\s\S]*?)```", save_code_block, text)

    # Preserve inline code
    result = re.sub(r"`([^`]+)`", save_inline_code, result)

    # Convert bold: **text** -> *text*
    # First extract bold markers, then escape, then re-apply
    bolds: list[str] = []

    def save_bold(match):
        content = match.group(1)
        idx = len(bolds)
        bolds.append(content)
        return f"\x00BOLD{idx}\x00"

    result = re.sub(r"\*\*([^*]+)\*\*", save_bold, result)

    # Convert italic: *text* -> _text_
    italics: list[str] = []

    def save_italic(match):
        content = match.group(1)
        idx = len(italics)
        italics.append(content)
        return f"\x00ITALIC{idx}\x00"

    result = re.sub(r"\*([^*]+)\*", save_italic, result)

    # Convert strikethrough: ~~text~~ -> ~text~
    strikes: list[str] = []

    def save_strike(match):
        content = match.group(1)
        idx = len(strikes)
        strikes.append(content)
        return f"\x00STRIKE{idx}\x00"

    result = re.sub(r"~~([^~]+)~~", save_strike, result)

    # Convert links: [text](url) -> [text](url)
    links: list[tuple[str, str]] = []

    def save_link(match):
        text_part = match.group(1)
        url = match.group(2)
        idx = len(links)
        links.append((text_part, url))
        return f"\x00LINK{idx}\x00"

    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", save_link, result)

    # Convert blockquotes: > text -> >text (Telegram uses > without space)
    quotes: list[str] = []

    def save_quote(match):
        content = match.group(1)
        idx = len(quotes)
        quotes.append(content)
        return f"\x00QUOTE{idx}\x00"

    result = re.sub(r"^> (.+)$", save_quote, result, flags=re.MULTILINE)

    # Now escape everything that's left
    result = escape_v2(result)

    # Restore bold
    for i, content in enumerate(bolds):
        result = result.replace(f"\x00BOLD{i}\x00", f"*{escape_v2(content)}*")

    # Restore italic
    for i, content in enumerate(italics):
        result = result.replace(f"\x00ITALIC{i}\x00", f"_{escape_v2(content)}_")

    # Restore strikethrough
    for i, content in enumerate(strikes):
        result = result.replace(f"\x00STRIKE{i}\x00", f"~{escape_v2(content)}~")

    # Restore links
    for i, (text_part, url) in enumerate(links):
        result = result.replace(f"\x00LINK{i}\x00", f"[{escape_v2(text_part)}]({url})")

    # Restore blockquotes
    for i, content in enumerate(quotes):
        result = result.replace(f"\x00QUOTE{i}\x00", f">{escape_v2(content)}")

    # Restore code blocks (no escaping inside)
    for i, block in enumerate(code_blocks):
        result = result.replace(f"\x00CODEBLOCK{i}\x00", block)

    # Restore inline code (no escaping inside)
    for i, code in enumerate(inline_codes):
        result = result.replace(f"\x00INLINE{i}\x00", code)

    return result


def plain_to_v2(text: str) -> str:
    """Escape plain text for safe use in MarkdownV2 messages.

    Use this when you have plain text (not markdown) that needs
    to be sent with parse_mode=MarkdownV2.
    """
    return escape_v2(text)
