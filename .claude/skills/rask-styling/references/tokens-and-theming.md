# Tokens & theming

Source: `frontend/packages/ui/src/lib/styles/tokens.css` (154 lines). Tailwind 4 CSS-first — there is no `tailwind.config.js`.

## The four blocks

1. `@custom-variant dark (&:is(.dark *))` — line 1. Class-based dark mode.
2. `:root` (13-69) — light OKLCH values + the legacy bridge.
3. `.dark` (71-103) — dark OKLCH values.
4. `@theme inline` (105-142) — maps every raw name to a `--color-*` Tailwind utility, plus radii and fonts.
5. `@layer base` (144-154) — `border-border` on `*`; `bg-background text-foreground antialiased` on `body`, with `font-feature-settings: 'cv02','cv03','cv04','cv11'`.

Declaring a raw value without the matching `@theme inline` line gives you a custom property with **no utility class**. Add both.

## Token table

| Token | Light | Dark | Utility |
|---|---|---|---|
| `background` | `oklch(0.985 0.004 80)` warm off-white | `oklch(0.13 0.006 270)` | `bg-background` |
| `foreground` | `oklch(0.16 0.006 270)` | `oklch(0.985 0 0)` | `text-foreground` |
| `card` / `card-foreground` | `0.993 0.002 80` | `0.17 0.008 270` | `bg-card` |
| `popover` / `popover-foreground` | `0.997 0.001 80` | `0.19 0.01 270` | `bg-popover` |
| `primary` / `primary-foreground` | `0.37 0.19 250` blue | `0.68 0.16 250` | `bg-primary` |
| `secondary` / `secondary-foreground` | `0.965 0.005 250` | `0.22 0.008 270` | `bg-secondary` |
| `muted` / `muted-foreground` | `0.955 0.006 260` / `0.45 0.012 260` | `0.22 0.008 270` / `0.55 0.01 270` | `text-muted-foreground` |
| `accent` / `accent-foreground` | `0.94 0.008 250` | `0.26 0.01 270` | `bg-accent` |
| `destructive` | `0.577 0.245 27.325` | `0.704 0.191 22.216` | `text-destructive` |
| `border` | `0.915 0.006 260` | `oklch(1 0 0 / 8%)` | `border-border` |
| `input` | `0.905 0.008 260` | `oklch(1 0 0 / 12%)` | `border-input` |
| `ring` | `0.45 0.18 250` | `0.68 0.16 250` | `ring-ring` |
| **`success`** / `success-foreground` | `0.65 0.2 145` | same | `bg-success` |
| **`warning`** / `warning-foreground` | `0.75 0.18 75` | same | `bg-warning` |
| `sidebar*` (7 tokens) | `0.965 0.004 260` | `0.16 0.008 260` | `bg-sidebar` |

`success` and `warning` are rask additions to the shadcn set — `badgeVariants` (`badge.svelte:14-15`) is the reference consumer. Note the dark `border`/`input` are **alpha-on-white**, not opaque; a hand-written hairline that hardcodes an opaque grey will not match.

Radii: `--radius: 0.625rem`, with `--radius-sm/md/lg/xl` derived by `calc()` (−4px, −2px, +0, +4px).
Fonts: `--font-sans: 'Inter'`, `--font-mono: 'JetBrains Mono'`.

## The legacy bridge (`tokens.css:48-68`)

```css
--ink: var(--foreground);      --mut:     var(--muted-foreground);
--faint: var(--muted-foreground); --line:  var(--border);
--panel: var(--card);          --panel-2: var(--muted);
--ok: var(--success);          --fail:    var(--destructive);
--warn: var(--warning);        --amber:   var(--warning);
```

These names predate the shadcn migration and were referenced by ~800 sites across 45 components **while undefined**. An undefined custom property makes the whole declaration invalid: `background: var(--panel)` fell back to transparent, and `border-color: var(--line)` fell back to `currentColor` — every hairline painted in full text colour. The bridge repairs all of them in both themes at once, because the targets are themselves re-declared under `.dark`.

Migration is opportunistic: when you touch a component still on a legacy name, swap it for the token. `StatusBoard` — an exported component — still uses `var(--fail)/var(--ok)/var(--amber)/var(--mut)` (`status-board.svelte:26-31`). Delete the block when `grep -r "var(--ink" frontend/` is empty.

## Per-zone `@source`

All seven zones import the same three stylesheets and point `@source` at `../../../packages/ui/dist` (three `../`). Variance:

| Zone | Extra |
|---|---|
| `media`, `annotator` | also `@source './lib' './routes' './app.html'` |
| `lakehouse`, `media` | also `@import '@xyflow/svelte/dist/style.css' layer(base)` |
| `home`, `compute`, `studio`, `train` | rely on Tailwind's default scan |

The five zones without explicit `./lib`/`./routes` sources depend on Tailwind 4 auto-detecting the SvelteKit source tree. Adding an explicit `@source` is safe; removing one is not.

## Reading the mode from JS

`useColorMode()` — `@rask/ui/color-mode`, backed by `hooks/color-mode.svelte.ts`. A `MutationObserver` on `<html>`'s `class` attribute, returning `{ current: 'dark' | 'light', isDark }` as **getters** (so destructuring loses reactivity — hold the object). Framework-agnostic on purpose: Svelte Flow's `colorMode` prop, the WebGPU atlas, and the canvas charts all need the same answer without a Svelte context.

Writing the mode is `mode-watcher`'s `toggleMode` (`navbar-user.svelte:5,65`). `@rask/ui` does not re-export the `ModeWatcher` component — each zone mounts it in its own root layout, alongside the no-flash script in `app.html` that sets the class before first paint.

## View transitions

`tokens.css:3-11` records the decision: cross-document view transitions stay off. Each zone is a separate document with its own shell instance, so opting a cross-zone nav into a transition crossfades the whole viewport — identical sidebar included — and reads as a flicker. The browser's paint-held document swap looks static instead. Same-document navs animate via `onNavigate → startViewTransition` in each zone's root layout, which is independent of the at-rule.

## `components.json` is stale scaffolding

`frontend/packages/ui/components.json` claims `baseColor: "zinc"` and points at the shadcn registry. The live palette is a hand-tuned warm/blue OKLCH set with extra `success`/`warning` tokens. The file is useful for running `bunx shadcn-svelte@latest add <name>` to scaffold a new primitive — treat its `baseColor` as historical, and re-skin whatever the CLI emits against the real tokens.
