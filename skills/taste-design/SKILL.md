---
name: taste-design
description: Semantic Design System Skill for Google Stitch. Generates agent-friendly DESIGN.md files that enforce premium, anti-generic UI standards — strict typography, calibrated color, asymmetric layouts, perpetual micro-motion, and hardware-accelerated performance.
---

# Stitch Design Taste — Semantic Design System Skill

## Overview
This skill generates `DESIGN.md` files optimized for Google Stitch screen generation. It translates battle-tested anti-slop frontend engineering directives into Stitch's native semantic design language — descriptive, natural-language rules paired with precise values that Stitch's AI agent can interpret to produce premium, non-generic interfaces.

## The Goal
Generate a `DESIGN.md` file that encodes:
1. **Visual atmosphere** — mood, density, and design philosophy
2. **Color calibration** — neutrals, accents, and banned patterns with hex codes
3. **Typographic architecture** — font stacks, scale hierarchy, and anti-patterns
4. **Component behaviors** — buttons, cards, inputs with interaction states
5. **Layout principles** — grid systems, spacing philosophy, responsive strategy
6. **Motion philosophy** — animation engine specs, spring physics, perpetual micro-interactions
7. **Anti-patterns** — explicit list of banned AI design clichés

## Analysis & Synthesis Instructions

### 1. Define the Atmosphere
Use evocative adjectives from the taste spectrum:
- **Density:** "Art Gallery Airy" (1–3) → "Daily App Balanced" (4–7) → "Cockpit Dense" (8–10)
- **Variance:** "Predictable Symmetric" (1–3) → "Offset Asymmetric" (4–7) → "Artsy Chaotic" (8–10)
- **Motion:** "Static Restrained" (1–3) → "Fluid CSS" (4–7) → "Cinematic Choreography" (8–10)

Default baseline: Creativity 9, Variance 8, Motion 6, Density 5.

### 2. Map the Color Palette
For each color: **Descriptive Name** + **Hex Code** + **Functional Role**.

Mandatory constraints:
- Maximum 1 accent color. Saturation below 80%
- "AI Purple/Blue Neon" aesthetic is strictly BANNED
- Use absolute neutral bases (Zinc/Slate) with high-contrast singular accents
- Never use pure black (`#000000`) — use Off-Black, Zinc-950, or Charcoal

### 3. Establish Typography Rules
- `Inter` is BANNED for premium/creative contexts — use `Geist`, `Outfit`, `Cabinet Grotesk`, or `Satoshi`
- Generic serif fonts (`Times New Roman`, `Georgia`, `Garamond`) are BANNED
- If serif needed: use only `Fraunces`, `Gambarino`, `Editorial New`, or `Instrument Serif`
- Serif is always BANNED in dashboards or software UIs
- Dashboard constraint: Sans-Serif pairings only (`Geist` + `Geist Mono` or `Satoshi` + `JetBrains Mono`)
- High-Density Override: When density > 7, all numbers must use Monospace

### 4. Define the Hero Section
- **Inline Image Typography:** Embed small contextual photos directly between words/letters in the headline
- **No Overlapping:** Text must never overlap images or other text
- **No Filler Text:** "Scroll to explore", "Swipe down", scroll arrows, bouncing chevrons are BANNED
- **Asymmetric Structure:** Centered Hero layouts BANNED when variance > 4
- **CTA Restraint:** Maximum one primary CTA. No secondary "Learn more" links

### 5. Describe Component Stylings
- **Buttons:** Tactile push feedback on active state. No neon outer glows. No custom mouse cursors
- **Cards:** Use ONLY when elevation communicates hierarchy. For high-density layouts, replace with border-top dividers
- **Inputs:** Label above input, error text below
- **Loading States:** Skeletal loaders matching layout dimensions — no generic circular spinners
- **Empty States:** Composed compositions indicating how to populate data

### 6. Define Layout Principles
- No overlapping elements — every element occupies its own clear spatial zone
- Centered Hero sections BANNED when variance > 4
- Generic "3 equal cards horizontally" is BANNED — use 2-column Zig-Zag or asymmetric grid
- CSS Grid over Flexbox math — never use `calc()` percentage hacks
- Full-height sections must use `min-h-[100dvh]` — never `h-screen`

### 7. Define Responsive Rules
- Mobile-First Collapse (< 768px): All multi-column layouts collapse to single column
- No Horizontal Scroll: Horizontal overflow on mobile is a critical failure
- Typography Scaling via `clamp()`. Body text minimum `1rem`/`14px`
- Touch Targets: All interactive elements minimum `44px`

### 8. Encode Motion Philosophy
- Spring Physics default: `stiffness: 100, damping: 20` — no linear easing
- Perpetual Micro-Interactions: Every active component should have an infinite loop state
- Staggered Orchestration: Never mount lists instantly — use cascade delays
- Animate exclusively via `transform` and `opacity`

### 9. Anti-Patterns (AI Tells)
NEVER DO:
- No emojis anywhere
- No `Inter` font
- No generic serif fonts
- No pure black (`#000000`)
- No neon/outer glow shadows
- No oversaturated accents
- No custom mouse cursors
- No overlapping elements
- No 3-column equal card layouts
- No generic names ("John Doe", "Acme", "Nexus")
- No fake round numbers (`99.99%`, `50%`)
- No fabricated data or statistics — never invent metrics, uptime percentages, response times
- No fake system/metric sections filled with invented data
- No `LABEL // YEAR` formatting
- No AI copywriting clichés ("Elevate", "Seamless", "Unleash", "Next-Gen")
- No filler UI text: "Scroll to explore", "Swipe down", scroll arrows
- No broken Unsplash links — use `picsum.photos` or SVG avatars
- No centered Hero sections (for high-variance projects)

## Output Format (DESIGN.md Structure)

```markdown
# Design System: [Project Title]

## 1. Visual Theme & Atmosphere
(Evocative description of mood, density, variance, and motion intensity.)

## 2. Color Palette & Roles
- **Canvas White** (#F9FAFB) — Primary background surface
- **Charcoal Ink** (#18181B) — Primary text
- **[Accent Name]** (#XXXXXX) — Single accent for CTAs, active states, focus rings

## 3. Typography Rules
- **Display:** [Font Name] — Track-tight, controlled scale
- **Body:** [Font Name] — Relaxed leading, 65ch max-width
- **Mono:** [Font Name] — For code, metadata, timestamps

## 4. Component Stylings
* **Buttons:** Flat, no outer glow. Tactile -1px translate on active.
* **Cards:** Generously rounded. Diffused whisper shadow. Used only when elevation serves hierarchy.
* **Inputs:** Label above, error below. Focus ring in accent color.

## 5. Layout Principles
(Grid-first responsive architecture. Asymmetric splits for Hero sections.)

## 6. Motion & Interaction
(Spring physics. Staggered cascade reveals. Hardware-accelerated transforms only.)

## 7. Anti-Patterns (Banned)
(Explicit list of forbidden patterns.)
```

## Best Practices
- Be Descriptive: "Deep Charcoal Ink (#18181B)" — not just "dark text"
- Be Functional: Explain what each element is used for
- Be Opinionated: This enforces a specific, premium aesthetic — not a neutral template
- Encode the bans: Anti-patterns are as important as the rules themselves