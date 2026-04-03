---
name: web-artifacts-builder
description: Suite of tools for creating elaborate, multi-component claude.ai HTML artifacts using modern frontend web technologies (React, Tailwind CSS, shadcn/ui). Use for complex artifacts requiring state management, routing, or shadcn/ui components - not for simple single-file HTML/JSX artifacts.
---

# Web Artifacts Builder

Build powerful frontend claude.ai artifacts using React + TypeScript + Tailwind CSS + shadcn/ui.

**IMPORTANT**: Avoid "AI slop" — no excessive centered layouts, purple gradients, uniform rounded corners, Inter font.

## Quick Start

### Step 1: Initialize Project
```bash
bash scripts/init-artifact.sh <project-name>
cd <project-name>
```
Creates: React + TypeScript (Vite), Tailwind CSS 3.4.1, path aliases, 40+ shadcn/ui components, Parcel configured.

### Step 2: Develop
Edit generated files. See Common Development Tasks for guidance.

### Step 3: Bundle
```bash
bash scripts/bundle-artifact.sh
```
Creates `bundle.html` — self-contained artifact with all JS, CSS, dependencies inlined.
Requirements: `index.html` must exist in root directory.

### Step 4: Share
Share `bundle.html` in conversation with user as an artifact.

### Step 5: Testing (Optional)
Use available tools (Playwright, Puppeteer) only if needed or requested. Avoid upfront testing — it adds latency.

## Reference
- shadcn/ui components: https://ui.shadcn.com/docs/components