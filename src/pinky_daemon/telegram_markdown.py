"""Telegram MarkdownV2 formatter for outbound messages.

Converts standard Markdown (as agents and humans tend to write it) to
Telegram's MarkdownV2 dialect. Handles bold, italic (both `*x*` and `_x_`),
strikethrough, inline code, fenced code blocks, headings (rendered as bold),
links, and blockquotes. Escapes the special-character set required outside
of code entities.

There is a sibling implementation in ``pinky_outreach.markdown_v2``
(``markdown_to_v2``) used by the outreach surface; they diverge mainly in
sentinel choices and which Markdown features they support. Consolidation
is desirable but out of scope for this fix — see TODO at end of file.

Bug history:
    Until 2026-05-13, this function used a single-pass placeholder restore.
    When a blockquote line contained inline code, the IC sentinel was
    nested inside the BQ replacement string; a single `sub` substituted
    the BQ but never re-scanned the substituted content, so the IC
    sentinel leaked into the final text. Telegram then stripped the
    `\\x00` null bytes and rendered raw "IC0", "IC1", ... in the message
    body. Fixed by looping the restore step until stable (bounded).
"""

from __future__ import annotations

import re

# Characters that must be escaped in MarkdownV2 outside of code entities.
# https://core.telegram.org/bots/api#markdownv2-style
_SPECIAL_CHARS = set(r'_*[]()~`>#+-=|{}.!')

# Regexes for each Markdown construct we recognize.
_CODE_BLOCK_RE = re.compile(r'```(\w*)\n(.*?)```', re.DOTALL)
_INLINE_CODE_RE = re.compile(r'`([^`]+)`')
_HEADING_RE = re.compile(r'^#{1,6}\s+(.+)$', re.MULTILINE)
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
_ITALIC_UNDERSCORE_RE = re.compile(r'(?<!\w)_(.+?)_(?!\w)')
_ITALIC_STAR_RE = re.compile(r'(?<!\*)\*([^*]+?)\*(?!\*)')
_STRIKE_RE = re.compile(r'~~(.+?)~~')
_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
_BLOCKQUOTE_RE = re.compile(r'^(>{1,})\s?(.*)$', re.MULTILINE)
# Spoiler: ``||hidden||`` → MarkdownV2 spoiler (tap-to-reveal). Non-greedy so
# ``||a|| ||b||`` yields two spoilers, not one spanning both.
_SPOILER_RE = re.compile(r'\|\|(.+?)\|\|')
# Expandable blockquote: a contiguous run of lines each prefixed with ``>!``
# becomes one collapsed-by-default quote (Bot API 7.4). Block-level, so it is
# stashed before the per-line blockquote step (which would otherwise eat the
# ``>`` and leave a literal ``!``).
_EXPANDABLE_BQ_RE = re.compile(r'(?:^>![^\n]*(?:\n|$))+', re.MULTILINE)

# Single regex that matches every placeholder sentinel we emit during the
# transform. The two-letter prefix selects the placeholder kind; the
# trailing index is shared across kinds (one flat list).
_PLACEHOLDER_RE = re.compile(r'\x00(CB|IC|HD|BD|ST|IT|IS|LK|BQ|SP|XB)(\d+)\x00')

# Bounded restore-loop depth. The actual loop terminates as soon as a pass
# is a no-op (``new_text == text``); this cap is purely a runaway guard.
# Real inputs converge in two or three passes — e.g. ``> **bold and `code` text**``
# nests IC inside BD inside BQ and needs three substitution passes. 10 is
# generous for any plausible future nesting.
_MAX_RESTORE_PASSES = 10


def _escape(text: str) -> str:
    """Backslash-escape every MarkdownV2 special character in ``text``."""
    out: list[str] = []
    for ch in text:
        if ch in _SPECIAL_CHARS:
            out.append('\\')
        out.append(ch)
    return ''.join(out)


def md_to_tg_mdv2(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2.

    Handles bold, italic, strikethrough, inline code, fenced code blocks,
    headings (rendered as bold — Telegram has no native heading), links,
    blockquotes, spoilers, and expandable blockquotes. Escapes all other
    special characters outside entities.

    Two PinkyBot-flavoured conventions extend standard Markdown:
      * ``||text||`` → a tap-to-reveal spoiler.
      * Lines prefixed with ``>!`` → a collapsed-by-default expandable
        blockquote (ideal for long digests behind a single tap).

    Args:
        text: Standard markdown source.

    Returns:
        A string safe to send with ``parse_mode=MarkdownV2``.
    """
    if not text:
        return ""

    placeholders: list[str] = []

    # Step 1: stash fenced code blocks (no inner escaping).
    def save_code_block(m: re.Match) -> str:
        lang = m.group(1)
        code = m.group(2)
        idx = len(placeholders)
        placeholders.append(f"```{lang}\n{code}```")
        return f"\x00CB{idx}\x00"
    text = _CODE_BLOCK_RE.sub(save_code_block, text)

    # Step 2: stash inline code (no inner escaping).
    def save_inline_code(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"`{m.group(1)}`")
        return f"\x00IC{idx}\x00"
    text = _INLINE_CODE_RE.sub(save_inline_code, text)

    # Step 2b: expandable blockquotes. A run of ``>!`` lines collapses into one
    # tap-to-expand quote. Built here (first line ``**>``, last line suffixed
    # ``||``) and stashed whole so the per-line blockquote step never sees it.
    # Inner text is escaped verbatim (no nested entities inside expandables).
    def save_expandable_bq(m: re.Match) -> str:
        block = m.group(0)
        trailing_nl = "\n" if block.endswith("\n") else ""
        bodies: list[str] = []
        for line in block.rstrip("\n").split("\n"):
            body = line[2:]  # strip leading '>!'
            if body.startswith(" "):
                body = body[1:]  # and at most one cosmetic space
            if body:  # drop blank '>!' lines — an empty quote line is malformed
                bodies.append(_escape(body))
        idx = len(placeholders)
        if not bodies:
            # Only blank '>!' markers: emit the raw block escaped as plain text
            # rather than a malformed empty-body expandable quote ('**>||').
            placeholders.append(_escape(block.rstrip("\n")))
            return f"\x00XB{idx}\x00{trailing_nl}"
        if len(bodies) == 1:
            built = f"**>{bodies[0]}||"
        else:
            built = f"**>{bodies[0]}\n"
            if len(bodies) > 2:
                built += "\n".join(f">{b}" for b in bodies[1:-1]) + "\n"
            built += f">{bodies[-1]}||"
        placeholders.append(built)
        return f"\x00XB{idx}\x00{trailing_nl}"
    text = _EXPANDABLE_BQ_RE.sub(save_expandable_bq, text)

    # Step 2c: spoilers — ``||hidden||`` → MarkdownV2 spoiler. Stashed before
    # the general escape so the literal ``||`` delimiters survive; inner text
    # is escaped.
    def save_spoiler(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"||{_escape(m.group(1))}||")
        return f"\x00SP{idx}\x00"
    text = _SPOILER_RE.sub(save_spoiler, text)

    # Step 3: headings → bold (TG has no heading support).
    def save_heading(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"*{_escape(m.group(1))}*")
        return f"\x00HD{idx}\x00"
    text = _HEADING_RE.sub(save_heading, text)

    # Step 4: bold.
    def save_bold(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"*{_escape(m.group(1))}*")
        return f"\x00BD{idx}\x00"
    text = _BOLD_RE.sub(save_bold, text)

    # Step 5: strikethrough.
    def save_strike(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"~{_escape(m.group(1))}~")
        return f"\x00ST{idx}\x00"
    text = _STRIKE_RE.sub(save_strike, text)

    # Step 6: italic via underscore.
    def save_italic(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"_{_escape(m.group(1))}_")
        return f"\x00IT{idx}\x00"
    text = _ITALIC_UNDERSCORE_RE.sub(save_italic, text)

    # Step 6b: italic via single asterisk.
    def save_italic_star(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"_{_escape(m.group(1))}_")
        return f"\x00IS{idx}\x00"
    text = _ITALIC_STAR_RE.sub(save_italic_star, text)

    # Step 7: links.
    def save_link(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"[{_escape(m.group(1))}]({m.group(2)})")
        return f"\x00LK{idx}\x00"
    text = _LINK_RE.sub(save_link, text)

    # Step 7b: blockquotes. Telegram supports `>` natively. The captured
    # body may already contain placeholders from earlier steps (e.g. IC
    # for inline code on the same line); those survive _escape because
    # `\x00` is not in _SPECIAL_CHARS, and the restore loop below catches
    # them after the BQ sentinel is replaced.
    def save_blockquote(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f"{m.group(1)} {_escape(m.group(2))}")
        return f"\x00BQ{idx}\x00"
    text = _BLOCKQUOTE_RE.sub(save_blockquote, text)

    # Step 8: escape every remaining special character.
    text = _escape(text)

    # Step 9: restore placeholders. Loop until stable because a single
    # `sub` pass does not re-scan the content it just inserted —
    # e.g. when BQ is substituted, the IC sentinels nested inside the
    # blockquote body are now visible and need another pass. Without
    # this loop, Telegram strips the leaked `\x00` null bytes and the
    # user sees raw "IC0", "IC1", ... in the rendered message.
    def restore(m: re.Match) -> str:
        return placeholders[int(m.group(2))]
    for _ in range(_MAX_RESTORE_PASSES):
        new_text = _PLACEHOLDER_RE.sub(restore, text)
        if new_text == text:
            break
        text = new_text

    return text


# TODO(consolidation): pinky_outreach.markdown_v2.markdown_to_v2 implements
# nearly the same transform with different sentinel names. Pick one, retire
# the other. Tracked at: (issue to be filed alongside this PR).
