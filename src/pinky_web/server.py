"""Pinky Web MCP server — stealth web tools powered by Camoufox.

Provides: scrape, screenshot, search, extract, crawl.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from urllib.parse import quote_plus, urljoin, urlparse

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRIP_TAGS = {
    "script", "style", "noscript", "svg", "iframe", "object", "embed",
    "nav", "footer", "header",
}

_MAIN_SELECTORS = [
    "main", "article", "[role='main']", "#content", "#main", ".content",
    ".main-content", ".post-content", ".article-content", ".entry-content",
]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _html_to_markdown(html: str, base_url: str = "") -> str:
    """Convert HTML to clean markdown, stripping noise."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")

    # Strip noisy tags
    for tag_name in _STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Remove hidden elements
    for el in soup.find_all(style=lambda v: v and "display:none" in v.replace(" ", "")):
        el.decompose()
    for el in soup.find_all(attrs={"hidden": True}):
        el.decompose()
    for el in soup.find_all(attrs={"aria-hidden": "true"}):
        el.decompose()

    md = markdownify(str(soup), heading_style="ATX", strip=["img"])

    # Clean up excessive whitespace
    lines = md.split("\n")
    cleaned = []
    blank_count = 0
    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            blank_count += 1
            if blank_count <= 2:
                cleaned.append("")
        else:
            blank_count = 0
            cleaned.append(stripped)

    return "\n".join(cleaned).strip()


def _extract_main_content(html: str) -> str:
    """Try to extract just the main content area from HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for selector in _MAIN_SELECTORS:
        el = soup.select_one(selector)
        if el and len(el.get_text(strip=True)) > 100:
            return str(el)
    # Fallback: return body
    body = soup.find("body")
    return str(body) if body else html


def _truncate(text: str, max_chars: int = 50000) -> str:
    """Truncate text with a note if too long."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"


# ---------------------------------------------------------------------------
# Browser Manager
# ---------------------------------------------------------------------------

class BrowserManager:
    """Lazy-init Camoufox browser, reuse across tool calls."""

    def __init__(self, headless: bool = True, default_timeout: int = 30000):
        self.headless = headless
        self.default_timeout = default_timeout
        self._playwright = None
        self._browser = None
        self._lock = threading.Lock()

    def _ensure_browser(self):
        with self._lock:
            if self._browser is None:
                from camoufox.sync_api import Camoufox

                _log("[pinky-web] Launching Camoufox browser...")
                self._cm = Camoufox(headless=self.headless)
                self._browser = self._cm.__enter__()
                _log("[pinky-web] Browser ready.")
            return self._browser

    def new_page(self):
        browser = self._ensure_browser()
        page = browser.new_page()
        page.set_default_timeout(self.default_timeout)
        page.set_default_navigation_timeout(self.default_timeout)
        return page

    def close(self):
        if self._browser is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
            self._browser = None


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def create_server(
    headless: bool = True,
    default_timeout: int = 30000,
    host: str = "127.0.0.1",
    port: int = 8105,
) -> FastMCP:
    mcp = FastMCP(
        "pinky-web",
        host=host,
        port=port,
        instructions=(
            "Pinky Web — stealth web scraping powered by Camoufox (anti-detect Firefox). "
            "Tools: scrape, screenshot, search, extract, crawl. "
            "Uses real browser fingerprints and anti-bot evasion."
        ),
    )
    mgr = BrowserManager(headless=headless, default_timeout=default_timeout)

    # -------------------------------------------------------------------
    # scrape
    # -------------------------------------------------------------------
    @mcp.tool()
    async def scrape(
        url: str,
        only_main_content: bool = True,
        wait_for: str = "",
        timeout: int = 0,
    ) -> str:
        """Scrape a URL and return its content as clean markdown.

        Uses a stealth anti-detect browser to bypass bot protection.

        Args:
            url: The URL to scrape.
            only_main_content: Extract only the main content area (default True).
            wait_for: Optional CSS selector to wait for before extracting.
            timeout: Navigation timeout in ms (0 = use default).
        """
        def _sync():
            page = mgr.new_page()
            try:
                if timeout:
                    page.set_default_navigation_timeout(timeout)

                _log(f"[pinky-web] scrape: {url}")
                page.goto(url, wait_until="domcontentloaded")

                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                if wait_for:
                    page.wait_for_selector(wait_for, timeout=timeout or mgr.default_timeout)

                html = page.content()
                title = page.title()

                if only_main_content:
                    html = _extract_main_content(html)

                md = _html_to_markdown(html, base_url=url)

                result = f"# {title}\n\nURL: {url}\n\n{md}" if title else md
                return _truncate(result)
            except Exception as e:
                return json.dumps({"error": str(e), "url": url})
            finally:
                page.close()

        return await asyncio.to_thread(_sync)

    # -------------------------------------------------------------------
    # screenshot
    # -------------------------------------------------------------------
    @mcp.tool()
    async def screenshot(
        url: str,
        full_page: bool = False,
        wait_for: str = "",
        output_path: str = "",
    ) -> str:
        """Take a screenshot of a webpage.

        Args:
            url: The URL to screenshot.
            full_page: Capture full scrollable page (default False = viewport only).
            wait_for: Optional CSS selector to wait for before capturing.
            output_path: Save to this path. If empty, saves to /tmp/pinky-screenshot-{ts}.png.
        """
        def _sync():
            nonlocal output_path
            page = mgr.new_page()
            try:
                _log(f"[pinky-web] screenshot: {url}")
                page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                if wait_for:
                    page.wait_for_selector(wait_for)

                if not output_path:
                    ts = int(time.time())
                    output_path = f"/tmp/pinky-screenshot-{ts}.png"

                page.screenshot(path=output_path, full_page=full_page)
                _log(f"[pinky-web] screenshot saved: {output_path}")

                return json.dumps({
                    "path": output_path,
                    "url": url,
                    "full_page": full_page,
                })
            except Exception as e:
                return json.dumps({"error": str(e), "url": url})
            finally:
                page.close()

        return await asyncio.to_thread(_sync)

    # -------------------------------------------------------------------
    # search
    # -------------------------------------------------------------------
    @mcp.tool()
    async def search(
        query: str,
        num_results: int = 10,
        engine: str = "duckduckgo",
    ) -> str:
        """Search the web and return results.

        Uses a stealth browser to perform searches without getting blocked.

        Args:
            query: Search query string.
            num_results: Max results to return (default 10).
            engine: Search engine — "duckduckgo" (default) or "google".
        """
        def _sync():
            page = mgr.new_page()
            try:
                encoded = quote_plus(query)
                if engine == "google":
                    url = f"https://www.google.com/search?q={encoded}&num={num_results}"
                else:
                    url = f"https://duckduckgo.com/?q={encoded}"

                _log(f"[pinky-web] search ({engine}): {query}")
                page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                results = []

                if engine == "google":
                    items = page.query_selector_all("div.g")
                    for item in items[:num_results]:
                        title_el = item.query_selector("h3")
                        link_el = item.query_selector("a")
                        snippet_el = (
                            item.query_selector("div[data-sncf]")
                            or item.query_selector(".VwiC3b")
                            or item.query_selector("span.st")
                        )
                        if title_el and link_el:
                            href = link_el.get_attribute("href") or ""
                            results.append({
                                "title": title_el.inner_text(),
                                "url": href,
                                "snippet": snippet_el.inner_text() if snippet_el else "",
                            })
                else:
                    items = page.query_selector_all("[data-testid='result']")
                    if not items:
                        items = page.query_selector_all(".result")
                    if not items:
                        items = page.query_selector_all("article")

                    for item in items[:num_results]:
                        title_el = item.query_selector("h2 a") or item.query_selector("a")
                        snippet_el = (
                            item.query_selector("[data-testid='result-snippet']")
                            or item.query_selector(".result__snippet")
                            or item.query_selector("span")
                        )
                        if title_el:
                            href = title_el.get_attribute("href") or ""
                            results.append({
                                "title": title_el.inner_text(),
                                "url": href,
                                "snippet": snippet_el.inner_text() if snippet_el else "",
                            })

                if not results:
                    text = page.inner_text("body")
                    return json.dumps({
                        "query": query,
                        "engine": engine,
                        "results": [],
                        "raw_text": _truncate(text, 10000),
                    })

                return json.dumps({
                    "query": query,
                    "engine": engine,
                    "results": results,
                })
            except Exception as e:
                return json.dumps({"error": str(e), "query": query})
            finally:
                page.close()

        return await asyncio.to_thread(_sync)

    # -------------------------------------------------------------------
    # extract
    # -------------------------------------------------------------------
    @mcp.tool()
    async def extract(
        url: str,
        selectors: list[str] | None = None,
        wait_for: str = "",
    ) -> str:
        """Extract specific elements from a webpage by CSS selector.

        Args:
            url: The URL to extract from.
            selectors: List of CSS selectors to extract. Returns text + href for each match.
            wait_for: Optional CSS selector to wait for before extracting.
        """
        if not selectors:
            return json.dumps({"error": "selectors list is required"})

        def _sync():
            page = mgr.new_page()
            try:
                _log(f"[pinky-web] extract: {url} selectors={selectors}")
                page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                if wait_for:
                    page.wait_for_selector(wait_for)

                extracted: dict[str, list[dict[str, str]]] = {}
                for sel in selectors:
                    elements = page.query_selector_all(sel)
                    items = []
                    for el in elements[:50]:
                        item: dict[str, str] = {"text": el.inner_text()}
                        href = el.get_attribute("href")
                        if href:
                            item["href"] = href
                        src = el.get_attribute("src")
                        if src:
                            item["src"] = src
                        items.append(item)
                    extracted[sel] = items

                return json.dumps({"url": url, "extracted": extracted})
            except Exception as e:
                return json.dumps({"error": str(e), "url": url})
            finally:
                page.close()

        return await asyncio.to_thread(_sync)

    # -------------------------------------------------------------------
    # crawl
    # -------------------------------------------------------------------
    @mcp.tool()
    async def crawl(
        url: str,
        max_pages: int = 5,
        same_domain: bool = True,
        only_main_content: bool = True,
    ) -> str:
        """Crawl a website starting from a URL, following links.

        Returns scraped content from each page as markdown.

        Args:
            url: Starting URL.
            max_pages: Maximum pages to visit (default 5, max 20).
            same_domain: Only follow links on the same domain (default True).
            only_main_content: Extract only main content area (default True).
        """
        def _sync():
            _max_pages = min(max_pages, 20)
            domain = urlparse(url).netloc
            visited: set[str] = set()
            to_visit = [url]
            pages: list[dict[str, str]] = []

            page = mgr.new_page()
            try:
                while to_visit and len(pages) < _max_pages:
                    current = to_visit.pop(0)
                    normalized = current.rstrip("/")
                    if normalized in visited:
                        continue
                    visited.add(normalized)

                    _log(f"[pinky-web] crawl ({len(pages) + 1}/{_max_pages}): {current}")
                    try:
                        page.goto(current, wait_until="domcontentloaded")
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass

                        html = page.content()
                        title = page.title()

                        if only_main_content:
                            html = _extract_main_content(html)
                        md = _html_to_markdown(html, base_url=current)

                        pages.append({
                            "url": current,
                            "title": title or "",
                            "content": _truncate(md, 15000),
                        })

                        if len(pages) < _max_pages:
                            links = page.query_selector_all("a[href]")
                            for link in links:
                                href = link.get_attribute("href")
                                if not href or href.startswith(
                                    ("#", "javascript:", "mailto:")
                                ):
                                    continue
                                full = urljoin(current, href).split("#")[0].split("?")[0]
                                if same_domain and urlparse(full).netloc != domain:
                                    continue
                                if full.rstrip("/") not in visited:
                                    to_visit.append(full)

                    except Exception as e:
                        pages.append({
                            "url": current,
                            "title": "",
                            "content": f"Error: {e}",
                        })

                return json.dumps({
                    "start_url": url,
                    "pages_crawled": len(pages),
                    "pages": pages,
                })
            finally:
                page.close()

        return await asyncio.to_thread(_sync)

    return mcp
