---
name: rask-styling
description: Styling, theming and design-system work in the rask console — OKLCH tokens, Tailwind 4 `@source`, `tv()` + `cn()`, `data-slot`, dark mode, the legacy `--ink/--line` bridge, export subpaths, Storybook, and vendor stylesheets themed through `layer(base)`. Use when the console renders unstyled, when picking a colour or writing `class=`, when adding or skinning a `@rask/ui` component (its subpath export, its story), when a vendor sheet out-specifies Tailwind utilities or a dock needs theming (dockview `--dv-*`, Svelte Flow), or when touching `tokens.css` / `app.css` / a `*.stories.svelte`.
---

# rask styling (`@rask/ui`)

Every styled surface in rask comes from `frontend/packages/ui`. The console supplies routes and data; **the design system supplies the pixels**. A styled `div` in the console is a bug report against this package.

Generic CSS mechanics live in `svelte-skills:svelte-styling` (the `style:` directive, custom properties, `:global`). The component API lineage is `shadcn-svelte`, with **two rask deviations**: commands run under `bunx`, and consumers import from the published subpath `@rask/ui/button` — never `$lib/components/ui/button`.

## Wiring the app (get this wrong and everything renders unstyled)

`frontend/console/src/app.css` imports Tailwind, the tokens and the vendor sheets, and points `@source` at the design
system's build; from `frontend/console/src/` the depth is **two** `../`:

```css
@import 'tailwindcss';
@import 'tw-animate-css';
@import '@rask/ui/styles/tokens.css';
@source '../../packages/ui/dist';
```

The console mounts a **dock** at `/workbench`, so it also imports `@rask/dockview/styles.css` under `layer(base)`; the
why is § *Third-party stylesheets*.

Tailwind 4 skips `node_modules`, so without `@source` every `@rask/ui` class is silently dropped — no error, no warning, just an unstyled page. It points at **`dist`**, so a component edit reaches the console's CSS only after `svelte-package` reruns (`bun --cwd=frontend/packages/ui run build`, or the `dev` watcher).

## Colour comes from tokens

`frontend/packages/ui/src/lib/styles/tokens.css` declares raw OKLCH under `:root` and `.dark`, then `@theme inline` maps each to a `--color-*` utility. Reach for the utility (`bg-card`, `text-muted-foreground`, `border-border`), which themes both modes at once.

Beyond the shadcn set, rask defines **`success` / `warning`** pairs — consumed by `badgeVariants` (`badge.svelte:14-15`).

**The legacy bridge** (`tokens.css:44-65`) aliases `--ink/--mut/--faint/--line/--panel/--panel-2/--ok/--fail/--warn/--amber` onto the real tokens. Those names carry ~500 references in 34 files under `frontend/` (measured 2026-11-06), and without the bridge each is **undefined**, so `border-color: var(--line)` falls back to `currentColor` and paints every hairline in full text colour. When you touch a component still on the bridge, migrate it to the token name; the block retires when `grep -r "var(--ink" frontend/` comes back empty.

## Third-party stylesheets — `layer(base)`, always

A vendor sheet ships unlayered CSS whose selectors out-specify Tailwind utilities, so the console's own
`class=` on that library's content silently loses. Import it into `base` and every utility sits above
it, permanently. The console imports three this way:

```css
@import '@xyflow/svelte/dist/style.css' layer(base);
@import '@rask/flow/styles.css'         layer(base);   /* …immediately after it */
@import '@rask/dockview/styles.css'     layer(base);
```

The xyflow sheet needs **two** lines, in that order: the vendor sheet, then `@rask/flow/styles.css`,
which maps `--xy-*` onto the OKLCH tokens. Skip the second and the vendor's light-mode hex defaults
(`#fefefe` controls, `#f7f9fb` edge labels, `#b1b1b7` edges) survive — the Controls widget and every
edge label render as a white blob on rask's dark surfaces. Same shape as the `--dv-*` block below and
for the same reason: every `--xy-*` resolves through a token that is itself re-declared under `.dark`,
so `colorMode` on `<SvelteFlow>` is left unset.

`@rask/dockview/styles.css` is **one import, not two** — it pulls dockview's own 124 KB sheet and then
the rask theme block. There is no `@source` to add for it: the package ships no Tailwind utility
classes, only `--dv-*` custom properties.

## Theming dockview — 40 custom properties, zero JavaScript

dockview exposes 113 `--dv-*` custom properties and 18 shipped themes. rask defines its own,
`.dockview-theme-rask`, mapping ~40 of them onto OKLCH tokens — surfaces to `--card`/`--background`,
the four tab states to `--foreground`/`--muted-foreground`, sashes to `--ring`, the drag overlay to a
`color-mix()` of `--primary`, radii to `calc(var(--radius) - …)`.

**The light/dark flip needs no JavaScript**, and that is the design point: every `--dv-*` resolves
through a rask token that is itself re-declared under `.dark`, so mode-watcher toggling the class
re-themes the dock underneath. `DockviewTheme.colorScheme` is therefore **deliberately unset** — it is
a static field on a theme object and would be a second source of truth that could only go stale.

**Two** groups are deliberately unmapped, and re-mapping them is a regression:

- `--dv-color-{abyss,gh,mocha,monokai,nord,sol}-*` — the other shipped themes' private palettes. Read
  only by their own theme classes; dead weight here.
- `--dv-tab-group-color-*` — the nine user-pickable tab-group accents. A user's semantic choice per
  group, not a theme surface. Overriding them collapses nine distinguishable colours onto one.

`--dv-overlay-z-index` is the opposite case — **map it (rask uses 999)**. dockview does not theme it
at all: only the built-in theme classes define it, so a custom theme that skips it leaves the
PopupService wrapper (the tab-overflow dropdown) at `z-index: auto`, painted UNDER the dock it is
prepended to, and the dropdown opens invisibly (`frontend/packages/dockview/src/styles.css` sets it).

The non-CSS half lives in `theme.ts` (`gap`, `dndTabIndicator`, `tabAnimation`, `dndOverlayMounting`,
`tabGroupIndicator`) — behavioural fields CSS cannot express. **Do not add GSAP to the dock chrome:**
dockview rewrites panel transforms every frame under `defaultRenderer: 'always'`, so a tween on the
same property is a second writer and reads as jank. Animate *inside* a panel if you want motion.

## Dark mode

`@custom-variant dark (&:is(.dark *))` — class-based, toggled by `mode-watcher`'s `toggleMode`. **`ModeWatcher` is not re-exported**, so the console mounts it in its root layout (`<ModeWatcher defaultMode="dark" />`). To read the mode in JS, read `mode-watcher`'s own `mode` store; the design system ships no colour-mode hook.

## View transitions

Cross-document view transitions stay **off** (`tokens.css:3-7`): the console is one document, its only cross-document
navigations are the sign-in round trip (where a whole-viewport crossfade reads as a flicker), and its soft navs animate
through `onNavigate → startViewTransition` in the root layout (`frontend/console/src/routes/+layout.svelte`). Leave it
out.

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

1. **`tv()` lives in the module script and is exported.** Variants use `tailwind-variants`, not cva. Exporting the `tv` call lets a caller put the same **chrome** on a plain element — `shell/top-navbar.svelte:86` takes `chrome = navigationMenuTriggerStyle()` and applies it (`:164, :250, :345`) to bare links and placeholders so triggers and links stay dimensionally identical.
2. **`className` is the last argument to `cn()`**, so the consumer wins. `cn = twMerge(clsx(…))`.
3. **`ref = $bindable(null)` + `bind:this={ref}`** is the ref-forwarding contract, typed by `WithElementRef<T, El>`.
4. **`data-slot="<name>"`** on the root element — CSS descendant selectors and the e2e harness both locate by it.
5. **Runes and snippets throughout**: `$props()` destructuring, `{@render children?.()}`. The package contains no `<slot>`. Generic components declare `<script lang="ts" generics="TData">`.
6. **Stay transport-agnostic.** The library never owns an API client — `GrantsPanel` takes a `client` prop of async functions. Shapes are declared structurally rather than imported from `@rask/api`, and `@rask/ui` imports no `$app/*` or the console's `#lib/*` (browser detection is `typeof window !== 'undefined'`).

Wrapping a **Bits UI** primitive that needs no skin: re-export it with an explicit `typeof` annotation. A bare re-export infers a non-portable type and silently skips the declaration (`collapsible/index.ts:3-4`).

Shipping it: create the directory + `<name>.svelte` + `index.ts` + a story, then **add the subpath to `exports` in `package.json`** — an unexported component is unreachable. Relative imports carry the `.js` extension (source `.ts` emits `.js`).

## Checklist

- [ ] Colour is a token utility; new semantic colours land in `tokens.css`, not in the console.
- [ ] The component comes from `@rask/ui` via its subpath; new chrome lands in the package.
- [ ] `class` adjusts layout; variants adjust appearance.
- [ ] Dynamic values go through `style:` or a custom property.
- [ ] `bun --cwd=frontend/packages/ui run build` before checking the console's rendering.
- [ ] `bun --cwd=frontend run lint`, `fmt:check` and `check` green.

## Where to go deeper

- `references/tokens-and-theming.md` — the full token table, the `@theme inline` mapping, the `@source` line, the bridge migration, reading the mode from JS, the view-transition decision, and why `components.json` is stale scaffolding.
- `references/component-catalog.md` — all 31 export subpaths, the three barrel conventions, **the `<Subject>` rule for printing an OIDC `sub`**, the data-table stack, Storybook, **the `vite.config.ts` two-homes test config**, and the known rough edges.
- `rask-frontend` — the console's routes, data fetching, and the gates that grade this work.
