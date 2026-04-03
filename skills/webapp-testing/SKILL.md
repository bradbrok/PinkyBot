---
name: webapp-testing
description: Toolkit for interacting with and testing local web applications using Playwright. Supports verifying frontend functionality, debugging UI behavior, capturing browser screenshots, and viewing browser logs.
---

# Web Application Testing

Write native Python Playwright scripts to test local web applications.

**Helper Scripts**: `scripts/with_server.py` manages server lifecycle. Always run `--help` first.

## Decision Tree
```
Is it static HTML?
├── Yes → Read HTML to identify selectors → Write Playwright script
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

## Playwright Script Template
```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)  # Always headless
    page = browser.new_page()
    page.goto('http://localhost:5173')
    page.wait_for_load_state('networkidle')  # CRITICAL: Wait for JS
    # ... automation logic
    browser.close()
```

## Reconnaissance Pattern
1. `page.screenshot(path='/tmp/inspect.png', full_page=True)`
2. Identify selectors from inspection
3. Execute actions with discovered selectors

## Critical Pitfall
❌ Don't inspect DOM before `networkidle` on dynamic apps
✅ Always wait for `page.wait_for_load_state('networkidle')` first

## Best Practices
- Use bundled scripts as black boxes (run `--help` before reading source)
- Use descriptive selectors: `text=`, `role=`, CSS, or IDs
- Add waits: `page.wait_for_selector()` or `page.wait_for_timeout()`
- Always close browser when done

## Examples in `examples/`
- `element_discovery.py`
- `static_html_automation.py`
- `console_logging.py`