"""Tests for pinky_daemon.telegram_markdown.

Most cases mirror tests/test_markdown_v2.py (the sibling outreach
implementation) so behavior parity is observable. The regression block
at the bottom locks in the blockquote-with-inline-code bug fix.
"""

from __future__ import annotations

from pinky_daemon.telegram_markdown import md_to_tg_mdv2


class TestBasicFormatting:
    def test_plain_text_passes_through(self):
        assert md_to_tg_mdv2("Hello world") == "Hello world"

    def test_empty_string(self):
        assert md_to_tg_mdv2("") == ""

    def test_bold_double_star_becomes_single(self):
        result = md_to_tg_mdv2("This is **bold** text")
        assert "*bold*" in result
        assert "**" not in result

    def test_italic_underscore(self):
        result = md_to_tg_mdv2("This is _italic_ text")
        assert "_italic_" in result

    def test_italic_single_star_becomes_underscore(self):
        result = md_to_tg_mdv2("This is *italic* text")
        assert "_italic_" in result

    def test_strikethrough(self):
        result = md_to_tg_mdv2("This is ~~deleted~~ text")
        assert "~deleted~" in result
        assert "~~" not in result

    def test_inline_code_preserved(self):
        result = md_to_tg_mdv2("Run `pip install` to install")
        assert "`pip install`" in result

    def test_inline_code_specials_not_escaped(self):
        result = md_to_tg_mdv2("Run `rm -rf .` carefully")
        assert "`rm -rf .`" in result

    def test_fenced_code_block(self):
        text = "Code:\n```python\nprint('hello')\n```"
        result = md_to_tg_mdv2(text)
        assert "```python\nprint('hello')\n```" in result

    def test_fenced_code_block_specials_not_escaped(self):
        text = "```\na.b + c! = d\n```"
        result = md_to_tg_mdv2(text)
        assert "a.b + c! = d" in result

    def test_link(self):
        result = md_to_tg_mdv2("Visit [Google](https://google.com)")
        assert "[Google](https://google.com)" in result

    def test_special_chars_escaped(self):
        result = md_to_tg_mdv2("Price is $10.99!")
        assert "\\." in result
        assert "\\!" in result

    def test_heading_becomes_bold(self):
        result = md_to_tg_mdv2("# Title\nbody")
        # heading is rendered as bold; pound is removed
        assert "*Title*" in result
        assert "#" not in result.split("\n")[0]


class TestBlockquote:
    def test_blockquote_plain(self):
        result = md_to_tg_mdv2("> This is a quote")
        assert result.startswith(">")
        assert "This is a quote" in result

    def test_blockquote_keeps_marker_depth(self):
        # Telegram supports nested-quote markers; we should not collapse them.
        result = md_to_tg_mdv2(">> nested quote")
        assert ">>" in result


class TestBlockquoteWithInlineCode:
    """Regression tests for the IC# leak.

    Before the fix, ``md_to_tg_mdv2`` did a single-pass placeholder restore.
    When a blockquote line contained inline code, the IC sentinel was
    nested inside the BQ replacement string and never re-scanned.
    Telegram then stripped the ``\\x00`` null bytes and the user saw raw
    ``IC0`` / ``IC1`` / ... in the rendered message body.
    """

    def test_blockquote_with_one_inline_code(self):
        result = md_to_tg_mdv2("> use `pip install` to install")
        # Inline code preserved verbatim inside the blockquote.
        assert "`pip install`" in result
        # No leaked sentinels.
        assert "IC0" not in result
        assert "\x00" not in result

    def test_blockquote_with_multiple_inline_codes(self):
        # Mirrors the real Telegram message that surfaced the bug
        # (Pushok's Q2 quote on PR #488).
        src = "> `session_id: str` vs `Optional[str]` / typed `ResumeHandle` now? → Keep `str`"
        result = md_to_tg_mdv2(src)
        # All four inline-code spans must survive.
        assert "`session_id: str`" in result
        assert "`Optional[str]`" in result
        assert "`ResumeHandle`" in result
        assert "`str`" in result
        # No sentinel garbage anywhere.
        for sentinel in ("IC0", "IC1", "IC2", "IC3", "BQ0"):
            assert sentinel not in result
        assert "\x00" not in result

    def test_inline_code_outside_blockquote_still_works(self):
        # The fix must not regress the simple case.
        result = md_to_tg_mdv2("Use `pip install foo` then `pytest`.")
        assert "`pip install foo`" in result
        assert "`pytest`" in result
        assert "\x00" not in result

    def test_blockquote_with_bold_and_inline_code(self):
        # Multiple nested placeholder types in one blockquote.
        result = md_to_tg_mdv2("> **important**: run `make test` first")
        assert "*important*" in result
        assert "`make test`" in result
        # No leaked sentinels of any kind.
        for prefix in ("CB", "IC", "HD", "BD", "ST", "IT", "IS", "LK", "BQ"):
            for idx in range(5):
                assert f"{prefix}{idx}" not in result
        assert "\x00" not in result

    def test_blockquote_with_link(self):
        # Link inside a blockquote — same nesting shape as IC inside BQ.
        result = md_to_tg_mdv2("> see [the docs](https://example.com) for details")
        assert "[the docs](https://example.com)" in result
        for sentinel in ("LK0", "BQ0"):
            assert sentinel not in result
        assert "\x00" not in result

    def test_blockquote_with_strikethrough(self):
        # Strikethrough inside a blockquote.
        result = md_to_tg_mdv2("> ~~old plan~~ scrapped")
        assert "~old plan~" in result
        for sentinel in ("ST0", "BQ0"):
            assert sentinel not in result
        assert "\x00" not in result

    def test_deepest_nesting_bq_bd_ic(self):
        # Pushok's worked example: BQ wraps BD wraps IC. This is the case
        # that requires three substitution passes; the docstring used to
        # under-claim it as two. Lock in correct behavior.
        result = md_to_tg_mdv2("> **bold and `code` text**")
        # All three constructs survive in their MarkdownV2 form.
        assert "`code`" in result
        # Bold becomes single-star around the rest (with escaped content).
        assert "*bold and `code` text*" in result or "*bold and" in result
        assert "\x00" not in result
        for prefix in ("BD0", "IC0", "BQ0"):
            assert prefix not in result

    def test_no_null_bytes_in_any_output(self):
        # Sanity: across a grab-bag of inputs, the formatter must never
        # leak null bytes into output (Telegram strips them, so a leak
        # produces visible sentinel garbage).
        samples = [
            "plain text",
            "**bold** and _italic_",
            "`code`",
            "> quoted",
            "> `code in quote`",
            "> **bold** and `code` in quote",
            "```python\nprint('hi')\n```",
            "# heading\n> quoted with `code`",
            "Visit [link](https://example.com) and run `cmd`.",
        ]
        for src in samples:
            result = md_to_tg_mdv2(src)
            assert "\x00" not in result, f"null byte leaked for: {src!r} → {result!r}"
