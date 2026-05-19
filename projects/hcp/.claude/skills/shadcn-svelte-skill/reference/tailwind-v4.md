# Tailwind CSS v4 Reference

Comprehensive reference for Tailwind CSS v4.x in SvelteKit 2 + Deno projects.

**Latest stable:** v4.2.x (Feb 2026). Docs: https://tailwindcss.com/docs

## Table of Contents

1. [Installation with Deno](#installation-with-deno)
2. [CSS-First Configuration](#css-first-configuration)
3. [@theme Directive](#theme-directive)
4. [@utility Directive](#utility-directive)
5. [Content Detection](#content-detection)
6. [Key New Features in v4](#key-new-features-in-v4)
7. [Dark Mode](#dark-mode)
8. [shadcn-svelte Theming](#shadcn-svelte-theming)
9. [Migration from v3](#migration-from-v3)
10. [Troubleshooting](#troubleshooting)

## Installation with Deno

```bash
deno add -D npm:tailwindcss npm:@tailwindcss/vite
```

```typescript
// vite.config.ts
import { defineConfig } from 'vite'
import { sveltekit } from '@sveltejs/kit/vite'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
})
```

```css
/* src/app.css — single line, replaces old @tailwind directives */
@import "tailwindcss";
```

**Not needed in v4:** PostCSS, Autoprefixer, `tailwind.config.js` (for basic projects), `postcss.config.js`.

### Vite Plugin Options

```typescript
// Disable Lightning CSS optimization (for debugging)
tailwindcss({ optimize: false })

// Keep Lightning CSS but disable minification
tailwindcss({ optimize: { minify: false } })
```

## CSS-First Configuration

v4 replaces `tailwind.config.js` with CSS-native configuration. All customization happens in your CSS file:

```css
@import "tailwindcss";

@theme {
  --font-display: "Satoshi", "sans-serif";
  --breakpoint-3xl: 1920px;
  --color-brand: oklch(0.6 0.2 250);
  --color-brand-light: oklch(0.8 0.15 250);
}
```

If you still need a JS config for advanced use cases (plugins, legacy compatibility), it works alongside:

```css
@import "tailwindcss";
@config "../tailwind.config.js";
```

## @theme Directive

The `@theme` directive defines design tokens as CSS custom properties. These become available as utility classes automatically:

```css
@import "tailwindcss";

@theme {
  /* Colors — become bg-brand, text-brand-light, etc. */
  --color-brand: oklch(0.6 0.2 250);
  --color-brand-light: oklch(0.8 0.15 250);

  /* Fonts — become font-display, font-body */
  --font-display: "Poppins", sans-serif;
  --font-body: "Inter", sans-serif;

  /* Spacing — extends spacing scale */
  --spacing-gutter: 1rem;
  --spacing-header: 4rem;

  /* Breakpoints — become sm:, md:, etc. */
  --breakpoint-3xl: 1920px;

  /* Radius */
  --radius-pill: 9999px;
}
```

### Reset and Override

To clear all default values for a namespace and only use your own:

```css
@theme {
  --font-*: initial;  /* Clear all default fonts */
  --font-display: "Poppins", sans-serif;
  --font-body: "Inter", sans-serif;
}
```

### Inline Theme (for scoped variables)

```css
@theme inline {
  --color-chart-1: var(--chart-1);
  --color-chart-2: var(--chart-2);
}
```

This registers the variables with Tailwind without generating default CSS custom properties — used by shadcn-svelte charts.

## @utility Directive

Define custom utilities directly in CSS (replaces `@layer utilities` + `@apply` pattern):

```css
@utility btn-primary {
  @apply inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90;
}

@utility container-narrow {
  max-width: 42rem;
  margin-inline: auto;
  padding-inline: 1rem;
}
```

> The `@apply` directive still works in v4 but the team recommends moving toward explicit CSS properties or `@utility` for new code.

## Content Detection

v4 auto-detects template files — no `content` array needed. The Vite plugin scans your project automatically.

For custom scan paths:

```css
@import "tailwindcss";
@source "../src/lib/components";
@source "../packages/shared-ui/src";
```

## Key New Features in v4

### Container Queries (Built-in)

No plugin needed:

```svelte
<div class="@container">
  <div class="grid grid-cols-1 @sm:grid-cols-3 @lg:grid-cols-4">
    <!-- Responsive to container, not viewport -->
  </div>
</div>
```

Max-width container queries:

```svelte
<div class="@container">
  <div class="grid grid-cols-3 @max-md:grid-cols-1">...</div>
</div>
```

### Text Shadows (v4.1+)

```html
<h1 class="text-shadow-lg">Heading with shadow</h1>
<p class="text-shadow-sm text-shadow-black/20">Subtle shadow</p>
```

### Masks (v4.1+)

```html
<div class="mask-linear-gradient mask-from-black mask-to-transparent">
  Faded content
</div>
```

### 3D Transforms

```html
<div class="perspective-distant">
  <div class="rotate-x-12 rotate-z-6 transform-3d">3D element</div>
</div>
```

### OKLCH Colors (Default)

v4 ships colors in OKLCH for wide-gamut display support:

```css
@theme {
  --color-brand: oklch(0.6 0.2 250);
}
```

### Composable Variants

```html
<div class="group-has-focus:opacity-100 peer-checked:bg-green-500">...</div>
```

### New v4.2 Features

- `@tailwindcss/webpack` plugin
- New default colors: mauve, olive, mist, taupe
- Block-direction logical property utilities
- `font-features-*` utility for OpenType features
- `inline-*` / `block-*` size utilities

## Dark Mode

### With mode-watcher (Recommended for shadcn-svelte)

```bash
deno add npm:mode-watcher
```

```svelte
<!-- src/routes/+layout.svelte -->
<script lang="ts">
  import { ModeWatcher } from "mode-watcher";
  import '../app.css';
  let { children } = $props();
</script>

<ModeWatcher />
{@render children()}
```

### CSS Variables for Dark Mode

```css
@import "tailwindcss";

@layer theme {
  :root {
    --color-background: 0 0% 100%;
    --color-foreground: 0 0% 3.6%;
    --color-primary: 0 0% 9%;
    --color-primary-foreground: 0 0% 100%;
  }

  .dark {
    --color-background: 0 0% 3.6%;
    --color-foreground: 0 0% 98%;
    --color-primary: 0 0% 98%;
    --color-primary-foreground: 0 0% 9%;
  }
}
```

### Class-Based Dark Mode

Use the `dark:` variant — works with class strategy by default in v4:

```html
<div class="bg-white dark:bg-gray-900 text-black dark:text-white">
  Adapts to dark mode
</div>
```

## shadcn-svelte Theming

shadcn-svelte uses CSS variables for its theme. After `init`, your `app.css` will have variables like:

```css
:root {
  --background: 0 0% 100%;
  --foreground: 0 0% 3.6%;
  --primary: 0 0% 9%;
  --primary-foreground: 0 0% 100%;
  --secondary: 0 0% 96.1%;
  --secondary-foreground: 0 0% 9%;
  --destructive: 0 84% 60%;
  --muted: 0 0% 96.1%;
  --muted-foreground: 0 0% 45.1%;
  --border: 0 0% 89.8%;
  --ring: 0 0% 3.6%;
  --radius: 0.5rem;
}

.dark {
  --background: 0 0% 3.6%;
  --foreground: 0 0% 98%;
  /* ... */
}
```

For chart colors (used by LayerChart integration):

```css
:root {
  --chart-1: oklch(0.646 0.222 41.116);
  --chart-2: oklch(0.6 0.118 184.704);
  --chart-3: oklch(0.398 0.07 227.392);
  --chart-4: oklch(0.828 0.189 84.429);
  --chart-5: oklch(0.769 0.188 70.08);
}

.dark {
  --chart-1: oklch(0.488 0.243 264.376);
  --chart-2: oklch(0.696 0.17 162.48);
  --chart-3: oklch(0.769 0.188 70.08);
  --chart-4: oklch(0.627 0.265 303.9);
  --chart-5: oklch(0.645 0.246 16.439);
}

@theme inline {
  --color-chart-1: var(--chart-1);
  --color-chart-2: var(--chart-2);
  --color-chart-3: var(--chart-3);
  --color-chart-4: var(--chart-4);
  --color-chart-5: var(--chart-5);
}
```

## Migration from v3

### Automated Upgrade

```bash
npx @tailwindcss/upgrade
```

Handles ~90% of changes: class renames, directive conversions, config migration.

### Key Class Renames

| v3 | v4 |
|----|-----|
| `bg-gradient-to-r` | `bg-linear-to-r` |
| `bg-opacity-50` | `bg-black/50` (modifier syntax) |
| `!flex` | `flex!` (important suffix) |
| `flex-shrink-0` | `shrink-0` |
| `flex-grow` | `grow` |
| `outline-none` | `outline-hidden` |
| `shadow-sm` | `shadow-xs` |
| `shadow` | `shadow-sm` |

### Directive Changes

| v3 | v4 |
|----|-----|
| `@tailwind base; @tailwind components; @tailwind utilities;` | `@import "tailwindcss";` |
| `@layer utilities { .foo { @apply ... } }` | `@utility foo { @apply ... }` |
| `tailwind.config.js` theme | `@theme { ... }` in CSS |

### Browser Support

v4 targets: Safari 16.4+, Chrome 111+, Firefox 128+. v4.1+ added improved fallbacks for older browsers.

## Troubleshooting

**Classes not applying:** Confirm `tailwindcss()` in vite.config plugins and `@import "tailwindcss"` in CSS. Restart dev server.

**Custom colors not working:** Ensure they're defined in `@theme { }` block, not loose in CSS.

**Storybook not picking up Tailwind:** Import `../src/app.css` in `.storybook/preview.ts`.

**v3 commands failing on v4:** Don't run `npx tailwindcss init -p` — that's v3. v4 uses the Vite plugin with zero config.
