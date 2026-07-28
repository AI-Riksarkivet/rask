---
name: rask-styling
description: Styling and component authoring in rask's `@rask/ui` design system — OKLCH tokens, Tailwind 4 `@source`, `tv()` variants, `data-slot`, dark mode, the legacy palette bridge. Use when a zone renders unstyled, when picking a colour or writing `class=`, when adding or skinning a `@rask/ui` component, when touching `tokens.css` / `app.css` / a `*.stories.svelte`, or when a Bits UI primitive needs wrapping.
---

# rask styling (`@rask/ui`)

Every styled surface in rask comes from `frontend/packages/ui`. Zones supply routes and data; **the design system supplies the pixels**. A styled `div` in a zone is a bug report against this package.

Generic CSS mechanics live in `svelte-skills:svelte-styling` (the `style:` directive, custom properties, `:global`). The component API lineage is `shadcn-svelte`, with **two rask deviations**: commands run under `bunx`, and consumers import from the published subpath `@rask/ui/button` — never `$lib/components/ui/button`.

## Wiring a zone (get this wrong and everything renders unstyled)

Every zone's `src/app.css` is exactly this, and the `@source` depth is **three** `../`:

```css
@import 'tailwindcss';
@import 'tw-animate-css';
@import '@rask/ui/styles/tokens.css';
@source '../../../packages/ui/dist';
```

Tailwind 4 skips `node_modules`, so without `@source` every `@rask/ui` class is silently dropped — no error, no warning, just an unstyled page. It points at **`dist`**, so a component edit reaches a zone's CSS only after `svelte-package` reruns (`bun --cwd=frontend/packages/ui run build`, or the `dev` watcher).

> `docs/architecture/frontend-conventions.md:319,347` ships this line with **four** `../`. That copy is wrong; three is correct — verified at `frontend/microfrontends/home/src/app.css:7`.

## Colour comes from tokens

`frontend/packages/ui/src/lib/styles/tokens.css` declares raw OKLCH under `:root` and `.dark`, then `@theme inline` maps each to a `--color-*` utility. Reach for the utility (`bg-card`, `text-muted-foreground`, `border-border`), which themes both modes at once.

Beyond the shadcn set, rask defines **`success` / `warning`** pairs — consumed by `badgeVariants` (`badge.svelte:14-15`). Radius derives from `--radius: 0.625rem` via `calc()`. Fonts are Inter and JetBrains Mono.

**The legacy bridge** (`tokens.css:48-68`) aliases `--ink/--mut/--faint/--line/--panel/--panel-2/--ok/--fail/--warn/--amber` onto the real tokens. Those names were referenced by ~800 sites across 45 components while being **undefined** — so `border-color: var(--line)` fell back to `currentColor` and painted every hairline in full text colour. That was the estate's long-standing "why does this look weird". When you touch a component still on the bridge, migrate it to the token name; the block retires when `grep -r "var(--ink" frontend/` comes back empty.

## Dark mode

`@custom-variant dark (&:is(.dark *))` — class-based, toggled by `mode-watcher`'s `toggleMode`. **`ModeWatcher` is not re-exported**, so each zone mounts it in its own root layout. To *read* the mode in JS, use `useColorMode()` from `@rask/ui/color-mode`: a `MutationObserver` on `<html>`'s class returning `{ current, isDark }` as getters, deliberately framework-agnostic so Svelte Flow, the WebGPU atlas, and canvas charts share one source.

## View transitions

Cross-document view transitions stay **off** on purpose (`tokens.css:3-11`). Each zone is a separate document with its own shell, so a cross-zone nav would crossfade an identical sidebar and read as a flicker. In-app navs animate through `onNavigate → startViewTransition` in each zone's root layout. Leave the at-rule out.

## Authoring a component

The canonical shape — `button.svelte`, `badge.svelte`, `table-cell.svelte`, `sidebar-menu-button.svelte` all follow it:

```svelte
<script lang="ts" module>
  import { tv, type VariantProps } from 'tailwind-variants';
  export const xVariants = tv({
    base: '…',
    variants: { variant: { … }, size: { … } },
    defaultVariants: { variant: 'default', size: 'default' },
  });
  export type XVariant = VariantProps<typeof xVariants>['variant'];
  export type XProps = WithElementRef<HTMLButtonAttributes> & { variant?: XVariant };
</script>

<script lang="ts">
  let { class: className, variant = 'default', ref = $bindable(null),
        children, ...restProps }: XProps = $props();
</script>

<button bind:this={ref} data-slot="x"
        class={cn(xVariants({ variant }), className)} {...restProps}>
  {@render children?.()}
</button>
```

Six rules carry that snippet:

1. **`tv()` lives in the module script and is exported.** Variants use `tailwind-variants`, not cva. Exporting the `tv` call lets a caller put the same **chrome** on a plain element — `top-navbar.svelte:86` applies `navigationMenuTriggerStyle()` to bare links so triggers and links stay dimensionally identical.
2. **`className` is the last argument to `cn()`**, so the consumer wins. `cn = twMerge(clsx(…))`.
3. **`ref = $bindable(null)` + `bind:this={ref}`** is the ref-forwarding contract, typed by `WithElementRef<T, El>`.
4. **`data-slot="<name>"`** on the root element — CSS descendant selectors and the e2e harness both locate by it.
5. **Runes and snippets throughout**: `$props()` destructuring, `{@render children?.()}`. The package contains no `<slot>`. Generic components declare `<script lang="ts" generics="TData">`.
6. **Stay transport-agnostic.** The library never owns an API client — `GrantsPanel` takes a `client` prop of async functions, `SearchBar` takes `search: (q) => Promise<SearchHit[]>`. Shapes are declared structurally rather than imported from `@rask/api`, and `@rask/ui` imports no `$app/*` or `$lib` (browser detection is `typeof window !== 'undefined'`).

Wrapping a **Bits UI** primitive that needs no skin: re-export it with an explicit `typeof` annotation. A bare re-export infers a non-portable type and silently skips the declaration (`collapsible/index.ts:3-4`).

Shipping it: create the directory + `<name>.svelte` + `index.ts` + a story, then **add the subpath to `exports` in `package.json`** — an unexported component is unreachable. Relative imports carry the `.js` extension (source `.ts` emits `.js`).

## Checklist

- [ ] Colour is a token utility; new semantic colours land in `tokens.css`, not in a zone.
- [ ] The component comes from `@rask/ui` via its subpath; new chrome lands in the package.
- [ ] `class` adjusts layout; variants adjust appearance.
- [ ] Dynamic values go through `style:` or a custom property.
- [ ] `bun --cwd=frontend/packages/ui run build` before checking a zone's rendering.
- [ ] `bun --cwd=frontend run lint fmt check` green.

## Where to go deeper

- `references/tokens-and-theming.md` — the full token table, the `@theme inline` mapping, per-zone `@source` variance, and the bridge migration.
- `references/component-catalog.md` — all 39 export subpaths, the three barrel conventions, the data-table stack, Storybook, and the known rough edges.
- `rask-frontend` — zones, routing, data fetching, and the gates that grade this work.
