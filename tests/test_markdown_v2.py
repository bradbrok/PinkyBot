"""Tests for pinky_outreach MarkdownV2 formatter."""

from __future__ import annotations

from pinky_outreach.markdown_v2 import escape_v2, markdown_to_v2, plain_to_v2


class TestEscapeV2:
    def test_no_special_chars(self):
        assert escape_v2("hello world") == "hello world"

    def test_dot(self):
        assert escape_v2("hello.") == "hello\\."

    def test_exclamation(self):
        assert escape_v2("wow!") == "wow\\!"

    def test_parentheses(self):
        assert escape_v2("(test)") == "\\(test\\)"

    def test_multiple_specials(self):
        assert escape_v2("a.b!c") == "a\\.b\\!c"

    def test_hash(self):
        assert escape_v2("# heading") == "\\# heading"

    def test_pipe(self):
        assert escape_v2("a | b") == "a \\| b"

    def test_dash(self):
        assert escape_v2("- item") == "\\- item"

    def test_empty(self):
        assert escape_v2("") == ""


class TestMarkdownToV2:
    def test_plain_text(self):
        result = markdown_to_v2("Hello world")
        assert result == "Hello world"

    def test_bold(self):
        result = markdown_to_v2("This is **bold** text")
        assert "*bold*" in result
        assert "**" not in result

    def test_italic(self):
        result = markdown_to_v2("This is *italic* text")
        assert "_italic_" in result

    def test_inline_code(self):
        result = markdown_to_v2("Use `pip install` to install")
        assert "`pip install`" in result

    def test_code_block(self):
        text = "Here is code:\n```python\nprint('hello')\n```"
        result = markdown_to_v2(text)
        assert "```python\nprint('hello')\n```" in result

    def test_code_block_no_lang(self):
        text = "Code:\n```\nfoo\n```"
        result = markdown_to_v2(text)
        assert "```\nfoo\n```" in result

    def test_code_block_not_escaped(self):
        text = "```\na.b + c! = d\n```"
        result = markdown_to_v2(text)
        # Inside code blocks, special chars should NOT be escaped
        assert "a.b + c! = d" in result

    def test_inline_code_not_escaped(self):
        text = "Run `rm -rf .` carefully"
        result = markdown_to_v2(text)
        assert "`rm -rf .`" in result

    def test_link(self):
        result = markdown_to_v2("Visit [Google](https://google.com)")
        assert "[Google](https://google.com)" in result

    def test_strikethrough(self):
        result = markdown_to_v2("This is ~~deleted~~ text")
        assert "~deleted~" in result
        assert "~~" not in result

    def test_special_chars_escaped(self):
        result = markdown_to_v2("Price is $10.99!")
        assert "\\." in result
        assert "\\!" in result

    def test_bold_with_special_chars(self):
        result = markdown_to_v2("**hello!**")
        assert "*hello\\!*" in result

    def test_mixed_formatting(self):
        text = "**Bold** and *italic* and `code`"
        result = markdown_to_v2(text)
        assert "*Bold*" in result
        assert "_italic_" in result
        assert "`code`" in result

    def test_empty(self):
        assert markdown_to_v2("") == ""

    def test_blockquote(self):
        result = markdown_to_v2("> This is a quote")
        assert result.startswith(">")
        assert "This is a quote" in result


class TestPlainToV2:
    def test_escapes_everything(self):
        result = plain_to_v2("Hello! How are you? (fine)")
        assert "\\!" in result
        assert "\\?" not in result  # ? is not a special char
        assert "\\(" in result
        assert "\\)" in result

    def test_plain_text(self):
        assert plain_to_v2("just text") == "just text"
