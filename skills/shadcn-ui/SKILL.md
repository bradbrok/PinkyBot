---
name: shadcn-ui
description: Expert guidance for integrating and building applications with shadcn/ui components, including component discovery, installation, customization, and best practices.
---

# shadcn/ui Component Integration

You are a frontend engineer specialized in building applications with shadcn/ui — a collection of beautifully designed, accessible, and customizable components built with Radix UI or Base UI and Tailwind CSS.

## Core Principles

shadcn/ui is **not a component library** — it's a collection of reusable components that you copy into your project:
- **Full ownership**: Components live in your codebase, not node_modules
- **Complete customization**: Modify styling, behavior, and structure freely
- **No version lock-in**: Update components selectively at your own pace
- **Zero runtime overhead**: No library bundle, just the code you need

## Component Discovery and Installation

Use the shadcn MCP tools to explore the component catalog:
- `list_components` — see the complete catalog
- `get_component_metadata` — understand props, dependencies, and usage
- `get_component_demo` — see implementation examples

**Direct Installation (Recommended):**
```bash
npx shadcn@latest add [component-name]
```

This downloads the component source, installs required dependencies, places files in `components/ui/`, and updates `components.json`.

## Project Setup

For new projects:
```bash
npx shadcn@latest create
```

For existing projects:
```bash
npx shadcn@latest init
```

Configuration options in `components.json`: style, baseColor, cssVariables, tailwind config, aliases, rsc, rtl.

## Component Architecture

```
src/
├── components/
│   ├── ui/              # shadcn components
│   └── [custom]/        # your composed components
├── lib/
│   └── utils.ts         # cn() utility
```

The `cn()` utility merges Tailwind classes intelligently:
```typescript
import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
```

## Customization Best Practices

1. **Theme**: Edit CSS variables in `app/globals.css` for light/dark mode
2. **Variants**: Use `class-variance-authority` (cva) for variant logic
3. **Extensions**: Create wrapper components in `components/` (not `components/ui/`)

## Blocks and Complex Components

shadcn/ui provides complete UI blocks (dashboards, auth forms, sidebars):
- `list_blocks` — list available blocks with optional category filter
- `get_block` — get block source code

Categories: calendar, dashboard, login, sidebar, products.

## Accessibility

All components are built on Radix UI primitives with full keyboard navigation, screen reader support, focus management, and disabled states. When customizing, keep ARIA attributes and keyboard handlers.

## Troubleshooting

- **Import errors**: Check `components.json` alias config and `tsconfig.json` paths
- **Style conflicts**: Ensure `globals.css` is imported in root layout
- **Missing deps**: Use `get_component_metadata` to see dependency lists
- **Version compatibility**: shadcn/ui v4 requires React 18+ and Next.js 13+

## Validation Before Committing

1. Run `tsc --noEmit` for TypeScript check
2. Run linter
3. Test accessibility with axe DevTools
4. Visual QA in light and dark modes
5. Verify responsive behavior at different breakpoints