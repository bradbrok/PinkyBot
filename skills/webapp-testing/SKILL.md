---
name: webapp-testing
description: Toolkit for interacting with and testing local web applications using Playwright via Camoufox (anti-detect Firefox). Supports verifying frontend functionality, debugging UI behavior, capturing browser screenshots, and viewing browser logs.
---

# Web Application Testing

Write native Python scripts using Camoufox (stealth Playwright) to test local web applications.

**Helper Scripts**: `scripts/with_server.py` manages server lifecycle. Always run `--help` first.

## Decision Tree
```
Is it static HTML?
├── Yes → Read HTML to identify selectors → Write Camoufox script
└── No (dynamic) → Is server running?
    ├── No → python scripts/with_server.py --help, then use helper
    └── Yes → Reconnaissance-then-action:
        1. Navigate + wait for networkidle
        2. Screenshot or inspect DOM
        3. Identify selectors
        4. Execute actions
```

## Using with_server.py
```bash
# Single server
python scripts/with_server.py --server "npm run dev" --port 5173 -- python automation.py

# Multiple servers
python scripts/with_server.py \
  --server "cd backend && python server.py" --port 3000 \
  --server "cd frontend && npm run dev" --port 5173 \
  -- python automation.py
```

## Script Template (Camoufox)
```python
from camoufox.sync_api import Camoufox

with Camoufox(headless=True) as browser:
    page = browser.new_page()
    page.goto('http://localhost:5173')
    page.wait_for_load_state('networkidle')  # CRITICAL: Wait for JS
    # ... automation logic — same Playwright API as before
    page.screenshot(path='/tmp/inspect.png', full_page=True)
```

## Async Template
```python
from camoufox.async_api import AsyncCamoufox

async with AsyncCamoufox(headless=True) as browser:
    page = await browser.new_page()
    await page.goto('http://localhost:5173')
    await page.wait_for_load_state('networkidle')
    # ... async automation
```

## Reconnaissance Pattern
1. `page.screenshot(path='/tmp/inspect.png', full_page=True)`
2. Identify selectors from inspection
3. Execute actions with discovered selectors

## Critical Pitfall
- Don't inspect DOM before `networkidle` on dynamic apps
- Always wait for `page.wait_for_load_state('networkidle')` first

## Best Practices
- Use bundled scripts as black boxes (run `--help` before reading source)
- Use descriptive selectors: `text=`, `role=`, CSS, or IDs
- Add waits: `page.wait_for_selector()` or `page.wait_for_timeout()`
- Always close browser when done (or use `with` block)
- Camoufox uses Firefox under the hood — some Chromium-only APIs won't work

## Examples in `examples/`
- `element_discovery.py`
- `static_html_automation.py`
- `console_logging.py`
