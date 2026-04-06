---
name: web-browsing
description: Stealth web browsing and scraping using Camoufox (anti-detect Firefox). Use when you need to scrape websites, search the web, take screenshots, extract page elements, or crawl sites. Bypasses bot detection that blocks standard HTTP requests and headless browsers.
mcp_servers:
  pinky-web:
    command: "{PINKY_VENV}/bin/python"
    args: ["-m", "pinky_web"]
    cwd: "{PINKY_ROOT}/src"
---

# Web Browsing (Camoufox)

Stealth web access powered by Camoufox — an anti-detect Firefox build that rotates fingerprints at the C++ level and evades bot detection.

## Available Tools

### scrape(url, only_main_content?, wait_for?, timeout?)
Fetch a page and return clean markdown. Best for reading articles, docs, READMEs.
```
scrape("https://example.com")
scrape("https://example.com", only_main_content=False)  # full page
scrape("https://example.com", wait_for=".content")  # wait for element
```

### screenshot(url, full_page?, wait_for?, output_path?)
Capture a PNG screenshot. Returns the file path.
```
screenshot("https://example.com")
screenshot("https://example.com", full_page=True, output_path="/tmp/shot.png")
```

### search(query, num_results?, engine?)
Web search via DuckDuckGo (default) or Google. Returns structured results.
```
search("python async best practices")
search("site:github.com camoufox", engine="google")
```

### extract(url, selectors, wait_for?)
Extract specific elements by CSS selector. Returns text + attributes for each match.
```
extract("https://example.com", selectors=["h1", "a.nav-link", ".price"])
```

### crawl(url, max_pages?, same_domain?, only_main_content?)
Crawl a site starting from a URL, following links. Returns markdown per page.
```
crawl("https://docs.example.com", max_pages=10)
crawl("https://example.com", max_pages=5, same_domain=True)
```

## When to Use This vs Direct HTTP

- **Use pinky-web** when: sites block bots, need JS rendering, need screenshots, need to interact with dynamic content
- **Use httpx/fetch** when: hitting APIs, downloading files, simple requests that don't need a browser

## Tips
- `scrape` with `only_main_content=True` (default) strips nav/footer/ads for cleaner output
- `search` with DuckDuckGo is less likely to get captcha'd than Google
- `crawl` respects `max_pages` (capped at 20) and stays on the same domain by default
- The browser instance is reused across calls for speed
- All tools use a fresh fingerprint per browser session
