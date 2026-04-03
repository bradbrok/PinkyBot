---
name: brand-presentation
description: Style guide and base template for building branded Pinky presentations. Use whenever an agent is creating an HTML presentation — ensures consistent typography, color, layout, and tone across all decks. Call get_presentation_template() to get the base HTML shell.
---

# Pinky Brand — Presentation Style Guide

## When to use this skill

Load this skill whenever you're about to call `create_presentation()` or `update_presentation()`. It defines how all presentations should look and feel. **Always start from the base template** (call `get_presentation_template()`), then fill in your content.

---

## Brand Identity

**Tone:** Smart, direct, a little personality. Not corporate. Not breathless. The writing should sound like a sharp person talking, not a press release.

**Visual character:** Dark-mode native. High contrast. Monospace accents. Purple-primary palette with strategic warm highlights. Minimal chrome, content-first.

---

## Color System

| Token | Hex | Use |
|---|---|---|
| `--bg` | `#0d0d0f` | Page/slide background |
| `--surface` | `#141417` | Card backgrounds |
| `--surface2` | `#1c1c21` | Elevated surfaces, nav |
| `--border` | `#2a2a32` | Borders, dividers |
| `--text` | `#e8e8f0` | Primary text |
| `--muted` | `#666680` | Secondary text, labels |
| `--accent` | `#7c6af7` | Primary accent — purple |
| `--accent2` | `#f7c56a` | Warm highlight — amber |
| `--accent3` | `#6af7b8` | Cool highlight — mint |

**Rules:**
- Background is always `--bg` (`#0d0d0f`). Never use white backgrounds.
- One accent color per slide concept — don't stack all three.
- Purple (`--accent`) = primary / structural. Amber (`--accent2`) = highlight / warning. Mint (`--accent3`) = success / call-out.
- Borders are always `--border`, never lighter.

---

## Typography

| Role | Font | Weight | Size |
|---|---|---|---|
| Display / H1 | Space Grotesk | 700 | `clamp(2rem, 5vw, 3.5rem)` |
| Section / H2 | Space Grotesk | 600 | `clamp(1.5rem, 3vw, 2.2rem)` |
| Body | Space Grotesk | 400 | `1rem` — `1.05rem` |
| Labels / Tags | Space Mono | 400 | `0.7rem` — `0.82rem` |
| Code | Space Mono | 400 | `0.78rem` — `0.85rem` |

**Rules:**
- Import both fonts from Google Fonts CDN: `Space Grotesk` and `Space Mono`
- Tag labels (category chips above headings) always use `Space Mono`, uppercase, letter-spaced
- Code snippets always use `Space Mono`
- Line height: 1.1 for headings, 1.6 for body, 1.7 for code

---

## Slide Layout Patterns

### Title Slide
- Centered content, radial gradient glow in background (use `--accent` at 12% opacity)
- Order: `wordmark` → `tag chip` → `H1` → `subtitle`
- Wordmark: "PINKYBOT" in `Space Mono`, `0.85rem`, muted, letter-spaced

### Section Slide (feature / concept)
- Tag chip at top (category label)
- H2 headline with one accent-colored word
- Content below: grid of cards OR single diagram OR code block

### Data / Stats Slide
- Large monospace numbers in `--accent`, small uppercase labels below
- Max 4 stats in a row

### Flow Diagram Slide
- Horizontal steps connected by `→` arrows
- Each step: rounded card, icon + label + sublabel
- Arrows in `--muted` color

### Code Slide
- Dark code block, full width
- Syntax: keywords in `--accent`, strings in `--accent3`, comments in `--muted`, names in `--accent2`
- Always include a comment explaining what the code does

### Closing / Meta Slide
- Same as title slide
- End with something memorable — a one-liner, a joke, a fact, not "Thank you"

---

## Cards

```css
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.25rem 1.5rem;
}
```

- Icons: single emoji, `1.5rem`, above h3
- Card H3: `0.95rem`, `font-weight: 600`
- Card body: `0.82rem`, `color: var(--muted)`, `line-height: 1.55`

---

## Navigation

Every presentation must include:
- Left/right arrow buttons (← →) in a fixed pill at bottom-center
- Dot indicators clickable to jump to any slide
- Keyboard support: `ArrowRight` / `Space` = next, `ArrowLeft` = prev
- Current slide counter: `N / Total` in `Space Mono`

---

## Tag Chips

Every slide should have a category tag chip above the heading:

```html
<div class="tag">Category Name</div>
```

```css
.tag {
  font-family: var(--mono);
  font-size: 0.7rem;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--accent);
  background: rgba(124,106,247,0.12);
  border: 1px solid rgba(124,106,247,0.25);
  padding: 0.25rem 0.75rem;
  border-radius: 999px;
  margin-bottom: 1.5rem;
}
```

---

## Slide Transitions

- Slides use `opacity` + `translateX` transitions, 0.4s ease
- Active: `opacity:1`, `transform: translateX(0)`
- Entering from right: starts at `translateX(40px)`
- Exiting to left: goes to `translateX(-40px)` via `.prev` class

---

## Content Rules

- **Max 6 words in a headline.** If it needs more, split across lines with a `<br>`.
- **One idea per slide.** If you're explaining two things, use two slides.
- **Never use bullet lists on slides.** Use cards, diagrams, or numbered callouts instead.
- **Every slide needs visual weight** — at least one of: accent color on a word, a card grid, a code block, or a diagram.
- **End strong.** The last slide should be memorable — a punchy one-liner, a callback to the title, something that lands.

---

## How to Build a Presentation

1. Load this skill ✓
2. Call `get_presentation_template()` to get the base HTML/CSS shell
3. Plan your slides — aim for 6–10
4. For each slide, choose the layout pattern (title / section / data / flow / code / closing)
5. Fill in content following the rules above
6. Call `create_presentation(title, html_content, description, tags)` to publish
7. Send the share URL to the owner

---

## Anti-Patterns (Never Do These)

- ❌ White or light backgrounds
- ❌ More than 3 accent colors on one slide
- ❌ Bullet lists — use cards or diagrams
- ❌ Generic "Thank You" closing slide
- ❌ Lorem ipsum or placeholder content — always real content
- ❌ External images (they'll break offline) — use emoji or CSS shapes
- ❌ Font sizes below `0.75rem` — illegible in iframe
- ❌ Horizontal scrolling — keep everything in viewport
