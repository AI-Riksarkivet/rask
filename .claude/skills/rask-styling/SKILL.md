---
name: rask-styling
description: How the rask console uses its design system, `@riksarkivet/ui` (the `AI-Riksarkivet/ui` repository, pinned to a commit) — the stylesheet `app.css` imports, the tokens and themes, the appearance (the Vite plugin, the boot script, the store, the theme toggle), the legacy `--ink/--line` bridge, subpath imports, the console's own shell, and vendor stylesheets themed through `layer(base)`. Use when the console renders unstyled, when picking a colour or writing `class=`, when a component is missing or needs changing (it is changed in the ui repository, then the pin moves), when the theme or mode misbehaves or flashes, when a vendor sheet out-specifies Tailwind utilities or a dock needs theming (the workbench's horizon-layout `--hl-*`, Svelte Flow), or when touching `app.css` / `app.html` / `appearance-options.ts` / `vite.config.ts`'s plugins.
---

# rask styling (`@riksarkivet/ui`)

The console's components, tokens and themes come from **`@riksarkivet/ui`**, the design system of the
`AI-Riksarkivet/ui` repository (ADR 0223). The console supplies routes, data and its own shell; **the design system
supplies the pixels**. A styled `div` in the console is a component the library is missing.

The library's own documents are the specification, read at the pinned commit: `DESIGN.md` (principles, geometry,
type, colour, motion, component conventions, accessibility) and `docs/CONSUMING.md` (the contract with an app). In a
checkout they are under `frontend/console/node_modules/@riksarkivet/ui/`. Generic CSS mechanics live in
`svelte-skills:svelte-styling`.

## The dependency

`frontend/console/package.json` pins the library to a full commit sha, never a branch:
`"@riksarkivet/ui": "github:AI-Riksarkivet/ui#<sha>"`, and `frontend/bun.lock` records it. Its exports name its
`src/`, so the console compiles it from source with its own Vite and svelte-check; there is no build step.

The console owns **one copy of each peer** (`svelte`, `bits-ui`, `tailwindcss`, `vite`, optionally `gsap`): two copies
of `bits-ui` would each keep their own registry of dismissable layers. `bits-ui` is pinned at 2.18.1, the version the
library is tested on, with the library's `patches/bits-ui@2.18.1.patch` applied through `patchedDependencies` in
`frontend/package.json` (`frontend/TOOLING.md` § The bits-ui patch).

**A component is changed in the ui repository, never in rask.** No patch of `@riksarkivet/ui` in `frontend/patches/`
and no copy of a component in the console: make the change there, then move the pin (`bun --cwd=frontend install`
after editing the sha) and read the commits in between for anything a consumer must change.

## Wiring the app (get this wrong and everything renders unstyled)

`frontend/console/src/app.css`:

```css
@import 'tailwindcss';
@import '@riksarkivet/ui/styles.css';
@import '@xyflow/svelte/dist/style.css'                 layer(base);
@import './lib/flow/styles.css'                         layer(base);   /* …immediately after it */
@import 'horizon-layout/horizon-layout.css'             layer(base);
@import '@riksarkivet/ui/styles/horizon-layout.css'     layer(base);   /* …immediately after it */
```

`styles.css` brings the fonts (IBM Plex Sans and Mono, self-hosted), `tw-animate-css`, the themes, the tokens and an
`@source` over the library's own sources (its stories and tests left out). Tailwind 4 skips `node_modules`, and an
`@source` path is read relative to the sheet that declares it, so the console writes **no** `@source` for the library.
Its own `lib`/`routes` are found by Tailwind's default scan of the SvelteKit tree.

## Colour comes from tokens

A theme sets only seeds on `[data-theme]` on `<html>`; every colour token is derived from them in OKLCH, and `.dark`
picks the dark half of each pair (`DESIGN.md` § Colour). Reach for the utility (`bg-card`, `text-muted-foreground`,
`border-border`, `border-field-line`, `bg-success`, `bg-warning`), which follows every theme and both modes at once.
A colour the console needs and the tokens lack is a change in the ui repository.

**No legacy palette name.** The tokens still alias `--ink/--mut/--faint/--line/--panel/--panel-2/--ok/--fail/--warn/--amber`
for other consumers; the console references none of them (0 under `frontend/console/src`, measured 2026-11-23). A
component's own `<style>` names the token itself (`var(--foreground)`, `var(--muted-foreground)`, `var(--border)`,
`var(--card)`, `var(--muted)`, `var(--success)`, `var(--destructive)`, `var(--warning)`), and new markup uses the
utility.

`e2e/admin/tables-registry.spec.ts` measures the contrast of the label chips, muted text and primary fills in light
and dark in the built app.

## Appearance: theme and mode

The console's options live in one module both callers import, `frontend/console/src/lib/appearance-options.ts`:
`storageKey: 'rask-console-appearance'`, `defaults: { theme: 'ink', mode: 'dark' }`.

- `vite.config.ts` runs `riksarkivetUi(APPEARANCE)` (after `consoleConfig()`, before `tailwindcss()`). It keeps the
  library out of Vite's dependency pre-bundling and emits `/appearance-boot.js` with those options inlined.
- `src/app.html` loads `<script src="%sveltekit.assets%/appearance-boot.js">` in `<head>`, before anything paints. It
  is same-origin, so the console's CSP (`script-src 'self'`) admits it with no hash. It puts the kept theme and mode on
  `<html>` (`data-theme`, the `dark` class, `color-scheme`).
- The root layout runs `$effect(() => appearance.start(APPEARANCE))`: the store takes over, keeps each change under the
  key, and follows the system when the mode is `system`.
- The account menu's **Toggle theme** (`#lib/shell/navbar-user.svelte`) calls
  `appearance.set({ mode: appearance.dark ? 'light' : 'dark' })`; its Sun/Moon icons follow `.dark`.

Read the mode in JS from `appearance.dark` / `appearance.resolved`, never from the DOM. Dark mode in CSS is the
`dark:` variant (`@custom-variant dark (&:is(.dark *))`). `AppearanceMenu` and `AccentDialog`
(`@riksarkivet/ui/appearance`) offer every theme and a custom accent; the console does not mount them.

## Importing components

Import from the per-component subpath, `@riksarkivet/ui/<name>` (`button`, `data-table`, `select`, `sheet`, `sidebar`,
`utils`, `motion`, …; the full list is `exports` in the library's `package.json`). The root `@riksarkivet/ui` is a short
convenience list; the subpaths are canonical. `cn` and `formatZoned` (every moment the console prints, as
`YYYY-MM-DD HH:mm:ss UTC+01:00`) come from `@riksarkivet/ui/utils`.

Two behaviours the console relies on:

- **A clickable `DataTable` row** (`onrowclick`) keeps its table semantics and opens from a visually hidden button in
  its first cell, named `Open <that cell's value>` unless `rowLabel` names it (`CommitLogTable`: `Show version <n>`). A
  click elsewhere on the row still opens it; a click on a control inside the row is that control's.
- **A `Select`** is named by its `ariaLabel` and the value it shows ("Sort by Created"); `getByLabel('<ariaLabel>')`
  still finds it.

## The console's shell

The shell every page renders in is the console's own code, `frontend/console/src/lib/shell` (imported as
`#lib/shell/index.ts`): `AppShell`, `AppError`, the sidebar (`app-sidebar`, `page-nav`), the account menu
(`navbar-user`), the bell (`activity-center`) and the breadcrumb (`breadcrumb.ts`, tested beside it). It is built from
the library's `sidebar` (with `useSidebar`, `IsMobile` and `SHELL_COLLAPSE_BREAKPOINT`), `dropdown-menu`, `popover`,
`avatar`, `collapsible` and `separator`. At phone width the sidebar is a sheet titled "Pages" (`mobileTitle`,
`mobileDescription`, `mobileCloseLabel` on `Sidebar.Root`). The shell's links are root paths, so oxlint's
`svelte/no-navigation-without-resolve` is off under `console/src/lib/shell/**` (`frontend/TOOLING.md`).

`page-nav.svelte` renders an `<a>` where a sidebar button would emit a `<button>` through the component's `child`
snippet (merged with `mergeProps` from `bits-ui`): reach for `child` instead of forking a component to change its tag.

## Third-party stylesheets — `layer(base)`, always

A vendor sheet ships unlayered CSS whose selectors out-specify Tailwind utilities, so the console's own `class=` on
that library's content silently loses. Import it into `base` and every utility sits above it.

The xyflow sheet needs **two** lines, in order: the vendor sheet, then `#lib/flow/styles.css`, which maps `--xy-*`
onto the tokens. Skip the second and the vendor's light-mode hex defaults survive: the Controls widget and every edge
label render as a white blob on a dark canvas. Every `--xy-*` resolves through a token that follows the mode, and the
vendor's own `.dark` palette is only the `--xy-*-default` fallbacks. The canvas still carries the page's mode
(`colorMode={appearance.dark ? 'dark' : 'light'}`, in `GraphCanvas` and `StaticFlow`, the console's two mounts of
`<SvelteFlow>`): Svelte Flow stamps `colorMode` on its root as a class, `light` when unset, and the design system
declares its light token set on `.light` too, so an unset mode paints every token inside the canvas light under the
page's light text (`e2e/projects/project-overview.spec.ts` measures the hierarchy's node names in both modes).

## Theming the workbench dock — `--hl-*`, zero JavaScript

horizon-layout is headless: every element carries a BEM class from its `baseClass`, and its sheet supplies the
structure (split panes, the resizer, drop zones and hints, a floating group, the popout frame) plus a palette of
`--hl-*` properties. `@riksarkivet/ui/styles/horizon-layout.css` sets every `--hl-*` on `.horizon-layout` and on a
popout's root to a token, so the dock follows the theme, mode and accent; the library's `.light` variant is never set.
horizon-layout mirrors `<html>`'s class, `data-theme` and inline custom properties into a popout window and copies the
page's stylesheets, which is why both sheets are imported from `app.css` rather than from the route. There is no
`@source` to add: horizon-layout ships no Tailwind utilities.

## View transitions

Cross-document view transitions stay **off** in the tokens: the console is one document, its only cross-document
navigations are the sign-in round trip (where a whole-viewport crossfade reads as a flicker), and its soft navs animate
through `onNavigate → startViewTransition` in the root layout (`frontend/console/src/routes/+layout.svelte`).

## Checklist

- [ ] Colour is a token utility; a missing token or component is a change in the ui repository, then a pin move.
- [ ] The component comes from `@riksarkivet/ui/<name>`; nothing under `frontend/patches/` patches the library.
- [ ] `class` adjusts layout; variants adjust appearance.
- [ ] Dynamic values go through `style:` or a custom property.
- [ ] `make frontend-check` green; a change to the shell or a clickable table also runs the Playwright suite.

## Where to go deeper

- The library's `DESIGN.md` and `docs/CONSUMING.md`, at the pinned commit.
- `rask-frontend` — the console's routes, data fetching, the dock, and the gates that grade this work.
