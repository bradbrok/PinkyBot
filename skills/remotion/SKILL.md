---
name: remotion
description: Generate walkthrough videos from Stitch projects using Remotion with smooth transitions, zooming, and text overlays
---

# Stitch to Remotion Walkthrough Videos

You are a video production specialist focused on creating engaging walkthrough videos from app designs. You combine Stitch's screen retrieval capabilities with Remotion's programmatic video generation to produce smooth, professional presentations.

## Overview

This skill enables you to create walkthrough videos that showcase app screens with professional transitions, zoom effects, and contextual text overlays. The workflow retrieves screens from Stitch projects and orchestrates them into a Remotion video composition.

## Prerequisites

- Access to the Stitch MCP Server
- Access to the Remotion MCP Server (or Remotion CLI)
- Node.js and npm installed
- A Stitch project with designed screens

## Retrieval and Networking

### Step 1: Discover Available MCP Servers

Run `list_tools` to identify available MCP servers and their prefixes:
- **Stitch MCP**: Look for `stitch:` or `mcp_stitch:` prefix
- **Remotion MCP**: Look for `remotion:` or `mcp_remotion:` prefix

### Step 2: Retrieve Stitch Project Information

1. Call `[stitch_prefix]:list_projects` with `filter: "view=owned"` to find the target project
2. Call `[stitch_prefix]:list_screens` to identify all screens for the walkthrough
3. For each screen, call `[stitch_prefix]:get_screen` to retrieve `screenshot.downloadUrl`, `width`, `height`
4. Download screenshots to `assets/screens/{screen-name}.png`

### Step 3: Set Up Remotion Project

Check for existing Remotion project (`remotion.config.ts`). If none exists:
```bash
npm create video@latest -- --blank
cd video
npm install @remotion/transitions @remotion/animated-emoji
```

## Video Composition Strategy

Create a modular Remotion composition:

1. **`ScreenSlide.tsx`** — Individual screen display component with zoom-in animation and fade transitions
2. **`WalkthroughComposition.tsx`** — Main composition sequencing multiple `ScreenSlide` components
3. **`config.ts`** — Video configuration (fps, dimensions, duration)

Use `@remotion/transitions` for professional effects: fade, slide, zoom via spring animations.

Create a `screens.json` manifest with screen metadata (title, description, imagePath, width, height, duration).

## Execution Steps

1. Gather screen assets from Stitch → save to `assets/screens/`
2. Create `screens.json` manifest
3. Generate `ScreenSlide.tsx` using `useCurrentFrame()` and `spring()` for animations
4. Generate `WalkthroughComposition.tsx` sequencing screens with `<Sequence>` components
5. Preview in Remotion Studio: `npm run dev`
6. Render: `npx remotion render WalkthroughComposition output.mp4`

## Common Patterns

- **Simple Slide Show**: 3-5 seconds per screen, cross-fade transitions, bottom text overlay
- **Feature Highlight**: Zoom into specific regions, animated callouts
- **User Flow**: Sequential slides with numbered steps overlay

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Blurry screenshots | Append `=w{width}` to screenshot URL before downloading |
| Choppy animations | Increase fps to 60; use proper spring configurations |
| Remotion build fails | Check Node version; ensure all deps installed |

## References

- Remotion Documentation: https://www.remotion.dev/docs/
- Remotion Skills: https://www.remotion.dev/docs/ai/skills
- Remotion MCP: https://www.remotion.dev/docs/ai/mcp