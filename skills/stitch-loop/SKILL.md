---
name: stitch-loop
description: Teaches agents to iteratively build websites using Stitch with an autonomous baton-passing loop pattern
---

# Stitch Build Loop

You are an **autonomous frontend builder** participating in an iterative site-building loop. Your goal is to generate a page using Stitch, integrate it into the site, and prepare instructions for the next iteration.

## Overview

The Build Loop pattern enables continuous, autonomous website development through a "baton" system. Each iteration:
1. Reads the current task from `.stitch/next-prompt.md`
2. Generates a page using Stitch MCP tools
3. Integrates the page into the site structure
4. Writes the next task to the baton file for the next iteration

## Prerequisites

- Access to the Stitch MCP Server
- A Stitch project (existing or will be created)
- A `.stitch/DESIGN.md` file (generate one using the `design-md` skill if needed)
- A `.stitch/SITE.md` file documenting the site vision and roadmap

## The Baton System

`.stitch/next-prompt.md` is the relay baton between iterations:

```markdown
---
page: about
---
A page describing how jules.top tracking works.

**DESIGN SYSTEM (REQUIRED):**
[Copy from .stitch/DESIGN.md]

**Page Structure:**
1. Header with navigation
2. Explanation of tracking methodology
3. Footer with links
```

Critical rules:
- The `page` field in YAML frontmatter determines the output filename
- The prompt content must include the design system block from `.stitch/DESIGN.md`
- You MUST update this file before completing your work to continue the loop

## Execution Protocol

### Step 1: Read the Baton
Parse `.stitch/next-prompt.md` — extract page name from `page` frontmatter and prompt from body.

### Step 2: Consult Context Files

| File | Purpose |
|------|---------|
| `.stitch/SITE.md` | Site vision, Stitch Project ID, existing pages (sitemap), roadmap |
| `.stitch/DESIGN.md` | Required visual style for Stitch prompts |

Do NOT recreate pages that already exist in the sitemap.

### Step 3: Generate with Stitch

1. Run `list_tools` to find the Stitch MCP prefix
2. If `.stitch/metadata.json` exists, use `projectId` from it; otherwise call `[prefix]:create_project` and save metadata
3. Call `[prefix]:generate_screen_from_text` with projectId, prompt (including design system block), and deviceType
4. Download assets to `.stitch/designs/{page}.html` and `.stitch/designs/{page}.png` (append `=w{width}` to screenshot URL)

### Step 4: Integrate into Site

1. Move HTML from `.stitch/designs/{page}.html` to `site/public/{page}.html`
2. Fix asset paths to be relative to the public folder
3. Wire up existing placeholder links (`href="#"`) to the new page
4. Ensure consistent headers/footers across all pages

### Step 4.5: Visual Verification (Optional)
If Chrome DevTools MCP is available, start a dev server, navigate to the page, capture a screenshot, and compare against the Stitch screenshot.

### Step 5: Update Site Documentation
Modify `.stitch/SITE.md`: add new page to sitemap with `[x]`, update roadmap.

### Step 6: Prepare the Next Baton (Critical)
**You MUST update `.stitch/next-prompt.md` before completing.** Check roadmap for next item, or invent something that fits the site vision.

## File Structure

```
project/
├── .stitch/
│   ├── metadata.json   # Stitch project & screen IDs
│   ├── DESIGN.md       # Visual design system
│   ├── SITE.md         # Site vision, sitemap, roadmap
│   ├── next-prompt.md  # The baton
│   └── designs/        # Staging area for Stitch output
└── site/public/        # Production pages
```

## metadata.json Schema

```json
{
  "name": "projects/6139132077804554844",
  "projectId": "6139132077804554844",
  "title": "My App",
  "designTheme": { "colorMode": "DARK", "font": "INTER", "roundness": "ROUND_EIGHT", "customColor": "#40baf7" },
  "deviceType": "MOBILE",
  "screens": {
    "index": { "id": "...", "sourceScreen": "projects/.../screens/...", "x": 0, "y": 0, "width": 390, "height": 1249 }
  }
}
```

## Common Pitfalls

- Forgetting to update `.stitch/next-prompt.md` (breaks the loop)
- Recreating a page that already exists in the sitemap
- Not including the design system block from `.stitch/DESIGN.md` in the prompt
- Leaving placeholder links (`href="#"`) instead of wiring real navigation
- Forgetting to persist `.stitch/metadata.json` after creating a new project