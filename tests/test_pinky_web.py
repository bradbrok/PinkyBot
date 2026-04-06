"""Tests for pinky_web MCP server."""

from __future__ import annotations

import json

import pytest


def test_server_creation():
    """Server creates with all expected tools."""
    from pinky_web.server import create_server

    server = create_server()
    tool_names = {t.name for t in server._tool_manager._tools.values()}
    assert tool_names == {"scrape", "screenshot", "search", "extract", "crawl"}


def test_html_to_markdown_basic():
    """HTML to markdown conversion strips noise."""
    from pinky_web.server import _html_to_markdown

    html = """
    <html><body>
        <script>alert('hi')</script>
        <nav>nav stuff</nav>
        <h1>Hello World</h1>
        <p>This is a test paragraph.</p>
        <footer>footer stuff</footer>
    </body></html>
    """
    md = _html_to_markdown(html)
    assert "Hello World" in md
    assert "test paragraph" in md
    assert "alert" not in md
    assert "nav stuff" not in md
    assert "footer stuff" not in md


def test_html_to_markdown_hidden():
    """Hidden elements are stripped."""
    from pinky_web.server import _html_to_markdown

    html = """
    <div>
        <p>Visible</p>
        <p style="display:none">Hidden by style</p>
        <p hidden>Hidden by attr</p>
        <p aria-hidden="true">Hidden by aria</p>
    </div>
    """
    md = _html_to_markdown(html)
    assert "Visible" in md
    assert "Hidden by style" not in md
    assert "Hidden by attr" not in md
    assert "Hidden by aria" not in md


def test_extract_main_content():
    """Main content extraction finds the right element."""
    from pinky_web.server import _extract_main_content

    html = """
    <html><body>
        <nav>Navigation</nav>
        <main><h1>Main Content</h1><p>Important text here that is long enough.</p></main>
        <footer>Footer</footer>
    </body></html>
    """
    result = _extract_main_content(html)
    assert "Main Content" in result
    assert "Important text" in result


def test_truncate():
    """Truncation works correctly."""
    from pinky_web.server import _truncate

    short = "hello"
    assert _truncate(short, 100) == short

    long = "x" * 200
    result = _truncate(long, 100)
    assert len(result) < 200
    assert "truncated" in result


def test_browser_manager_init():
    """BrowserManager initializes without launching browser."""
    from pinky_web.server import BrowserManager

    mgr = BrowserManager(headless=True, default_timeout=5000)
    assert mgr._browser is None
    assert mgr.headless is True
    assert mgr.default_timeout == 5000


@pytest.mark.slow
def test_scrape_live():
    """Live scrape test against example.com."""
    from pinky_web.server import BrowserManager, _html_to_markdown, _extract_main_content

    mgr = BrowserManager(headless=True)
    try:
        page = mgr.new_page()
        page.goto("https://example.com", wait_until="domcontentloaded")
        html = page.content()
        title = page.title()
        md = _html_to_markdown(_extract_main_content(html))
        page.close()

        assert title == "Example Domain"
        assert "Example Domain" in md
    finally:
        mgr.close()
