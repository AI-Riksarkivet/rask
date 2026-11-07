# Component catalog & packaging

`frontend/packages/ui` — 173 tracked files; `src/lib/shell` alone is ~2.1k lines (measured 2026-11-04).

## Build

`svelte-package` (`@sveltejs/package` 2.5.8) emits `src/lib/` → `dist/`, stripping the `src/lib` prefix — which is why every `exports` entry reads `./dist/components/<x>` while the source sits at `src/lib/components/<x>`. `dist/` and `storybook-static/` are gitignored; the `files` allowlist keeps stories and tests out of the tarball. The workspace's `check` and `test` scripts run `build` first (`frontend/package.json`), because the console reads `dist/`.

`tsconfig.json`: `strict` + `noUncheckedIndexedAccess`, **no** `exactOptionalPropertyTypes` (the Bits UI "union too complex" incompatibility — leave it off), `target: ES2022` (so `toSorted` and the other ES2023 array methods are out), `verbatimModuleSyntax`.

`vite.config.ts` serves Storybook + vitest, and its `test` block is load-bearing (`vite.config.ts:8-19`): `include` names **both** homes — `tests/**` and `src/**` — and `exclude` drops `dist/**` and `.svelte-kit/**`. `svelte-package` copies test files into both build trees, so vitest's default discovery ran every suite three times against regenerated output; narrowing to `src` alone silently dropped 117 tests to 10. Change neither half without checking the other.

## The 31 export subpaths

**Components** — `dist/components/<name>/index.js`, conditions `svelte` + `types` only:

`alert-dialog · avatar · badge · button · card · checkbox · collapsible · data-table · dialog · dropdown-menu · gated-action · grant-form · grants-panel · hierarchy · identity · input · navigation-menu · popover · select · separator · sheet · sidebar · skeleton · switch · table · tooltip` (measured 2026-11-04 against `package.json` `exports`)

**`./gated-action`** — ruling #143's implementation. `{ allowed, reason, action?, class?, children }`: when allowed it renders the children with NO wrapper at all; when refused it wraps them in a `Tooltip.Trigger` `child` span (`data-slot="gated-action"`, `aria-disabled="true"`, `class="contents"` so geometry is unchanged) with a capture-phase click/keydown guard. It deliberately does NOT set the native `disabled` attribute — that would kill the tooltip and drop the control from the tab order — so **a child that sets `disabled` itself defeats the mechanism**; make the child's own `disabled` conditional on the same verdict. Wrapping in a `Tooltip.Trigger` button instead of a span would nest a button inside a button.

**`./hierarchy`** — `ProjectHierarchy` + `HierarchyNode` (#109): project › warehouse › namespace › table as a vertical graph. **The one subpath with a vendor-canvas dependency** — it draws on `@rask/flow`'s `StaticFlow`, so `@rask/flow` and `@xyflow/svelte` are `@rask/ui` dependencies for this component alone, and the app mounting it must declare both itself AND import their two stylesheets `layer(base)` in its `app.css` (vendor sheet first, then `@rask/flow/styles.css`) or the canvas paints in the library's light-mode hex. The console's `/projects/<id>` and `/catalog` overview both render it. It is transport-agnostic (the GrantsPanel precedent) because the library owns no API client: the two rung reads arrive as async props and the console passes its own `*.api.ts` reads.

**`./identity`** — `Subject` + `subjectDisplay` (#87). An OIDC `sub` is an opaque base64 blob; the helper keeps readable identifiers verbatim and ellipsizes opaque ones in the middle, and the FULL subject always rides `title` so hover/copy still yields the value a tuple write needs. Render it with `<Subject value={…} />`, never `subjectDisplay(...).label` — the component is what makes the title invariant structural, where a call site reading `.label` can drop it. Its own subpath, not under `grants-panel`: the concern is any surface that prints a sub (FGA subjects, control-event actors, audit stamps).

**Non-components** (`./utils` and `./motion` carry a `default` condition; `./shell` does not — it is `types` + `svelte` like a component, and shares rough edge 3; `./styles/tokens.css` is a bare path):

| Subpath | Contents |
|---|---|
| `./shell` | `AppShell, AppSidebar, AppError, TopNavbar, ActivityCenter, ForbiddenPage` + `nav-config` + `breadcrumb`. `ActivityCenter` is the bell and its panel over the reader's own activity log, fed through props and callbacks: the console owns the log (`rask-frontend`) |
| `./utils` | `cn`, `toDate/formatAbsolute/formatRelative/formatTimestamp`, `WithElementRef/WithoutChildren/WithoutChild/WithoutChildrenOrChild` |
| `./motion` | GSAP `{@attach}` factories |
| `./styles/tokens.css` | the theme |

The root barrel `.` is a **curated subset** (`src/lib/index.ts`): button, badge, dialog, card, utils, select, motion, grants-panel. `README.md:30-31` names the subpaths authoritative — prefer `@rask/ui/button` over `@rask/ui`.

## Three barrel conventions

They coexist deliberately; match the one the component already uses.

1. **shadcn dual export** — `Root` + `Sidebar` + `SidebarRoot` (`sidebar/index.ts`). Consumed as `import * as Sidebar from '@rask/ui/sidebar'`.
2. **Namespace object** — `export const Dialog = { Root, Trigger, … }` (`dialog/index.ts:7`; same for `Table`, `AlertDialog`). Consumed as `import { Dialog } from '@rask/ui/dialog'`.
3. **Single default** — `export { default as Switch }` (`switch/index.ts`).

Re-exporting a Bits UI primitive unchanged needs an explicit `typeof` annotation; a bare re-export infers a non-portable type and silently skips the `.d.ts` declaration (`collapsible/index.ts:3-4`, repeated in `dropdown-menu`, `popover`, `alert-dialog`).

## The `child` escape hatch

`sidebar-menu-button.svelte:68-77` declares a local `{#snippet Button({ props })}`, merges with `mergeProps(buttonProps, props)` from `bits-ui`, and forwards to a caller-supplied `child?: Snippet<[{ props: Record<string, unknown> }]>`. `shell/zone-nav.svelte` (the in-page sidebar navigation) declares a `leafLink` snippet at `:32` and passes `child` at four call sites (`:73, :90, :106, :119`): three render `leafLink` (`:74, :107, :120`) to emit an `<a>` where the component would otherwise emit a `<button>`, and one (`:90`) renders a `Collapsible.Trigger` in place of a group label. Reach for this instead of forking a component to change its tag.

## Data-table stack

A vendored shadcn-svelte adapter over `@tanstack/table-core` 8.21.

- `createSvelteTable` (`create-svelte-table.svelte.ts:27-68`) takes **getter-based** options, holds `TableState` in `$state`, re-syncs in `$effect.pre`.
- `mergeObjects` is a lazy `Proxy` preserving getter semantics, later sources winning — the reactivity seam, unit-tested at `tests/data-table.test.ts:14-32`.
- `FlexRender` resolves a `ColumnDef` template that is a string, a `renderComponent()` config, a `renderSnippet()` config, or a primitive.
- `selectionColumn()` ships the indeterminate select-all column.
- `types.ts:11-14` augments tanstack's `ColumnMeta` with `{ headerClass, cellClass }` and is **type-re-exported** from the barrel so the augmentation reaches consumers through the `.d.ts` chain.
- The barrel re-exports the tanstack row models, so the console needs no direct `@tanstack` dependency.

Containment is deliberate: `Card` and the DataTable card both carry `min-w-0 max-w-full`, and horizontal scroll lives on `Table`'s inner `[data-slot=table-container] overflow-x-auto`.

## Storybook & tests

**Storybook 10.4.6** with `@storybook/svelte-vite` (not `-sveltekit` — this is a library). Addons: docs, svelte-csf, a11y. `preview.ts` imports `tokens.css`; `tags: ['autodocs']` globally. Run `make storybook` (→ `:6006`).

Coverage is thin: **6 story files for 26 component subpaths** (the 31 `exports` less `.`, `./shell`, `./utils`, `./motion` and `./styles/tokens.css`, counted 2026-11-07) — `button.stories.svelte` (CSF via `defineMeta` + `{#snippet template(args)}`), `card.stories.ts` (CSF3 `Meta/StoryObj`), `dialog.stories.svelte`, `gated-action.stories.svelte`, `shell/activity-center.stories.svelte`, `Welcome.stories.svelte`. A new component should ship a story.

Vitest suites are pure-function or SSR-string. Seven live in `tests/`; a suite may also sit beside its source (`src/lib/components/identity/subject.test.ts`) — `vite.config.ts:17` includes both homes. No jsdom and no `@testing-library/svelte` (adding either changes the workspace lockfile).

## Known rough edges

Do not treat these as patterns to copy.

1. An extensionless relative import violates the package's own `.js` convention and survives into `dist`: `motion.ts:10` → `'./gsap'`.
2. Component subpaths declare no `default` condition — a tool resolving without the `svelte` condition gets an unresolvable specifier.
3. Six exported subpaths have zero consumers outside the package (`avatar, collapsible, dropdown-menu, navigation-menu, sidebar, tooltip`); they exist to serve `shell/`.
4. `GrantsPanel` (420 lines) is the largest component and is arguably domain logic: its form carries the grantable FGA relations as `GRANTABLE` (`grant-form/grant-form.svelte:58`, `['reader','writer','validator','owner','pass_grants','manage_grants']`). It lives here so the console's surfaces that grant share one copy.
