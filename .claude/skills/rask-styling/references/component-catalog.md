# Component catalog & packaging

`frontend/packages/ui` — 207 files, ~3.8k lines of `.svelte` under `src/lib/components` plus a 1.3k-line `src/lib/shell`.

## Build

`svelte-package` (`@sveltejs/package` 2.5.8) emits `src/lib/` → `dist/`, stripping the `src/lib` prefix — which is why every `exports` entry reads `./dist/components/<x>` while the source sits at `src/lib/components/<x>`. `dist/` and `storybook-static/` are gitignored; the `files` allowlist keeps stories and tests out of the tarball. Turbo caches `dist/**`, and both `check` and `test` depend on the package's **own** `build`.

`tsconfig.json`: `strict` + `noUncheckedIndexedAccess`, **no** `exactOptionalPropertyTypes` (the Bits UI "union too complex" incompatibility — leave it off), `target: ES2022` (`run-status.ts:121-123` avoids `toSorted` for this reason), `verbatimModuleSyntax`.

`vite.config.ts` serves Storybook + vitest, and its `test` block is load-bearing (`vite.config.ts:8-19`): `include` names **both** homes — `tests/**` and `src/**` — and `exclude` drops `dist/**` and `.svelte-kit/**`. `svelte-package` copies test files into both build trees, so vitest's default discovery ran every suite three times against regenerated output; narrowing to `src` alone silently dropped 117 tests to 10. Change neither half without checking the other.

## The 41 export subpaths

**Components** — `dist/components/<name>/index.js`, conditions `svelte` + `types` only:

`alert-dialog · avatar · badge · button · card · checkbox · chip · collapsible · data-table · dialog · dropdown-menu · field · gated-action · grants-panel · hierarchy · identity · input · navigation-menu · popover · progress · radio-group · resizable-split · search-bar · select · separator · sheet · sidebar · skeleton · slider · sort-header · status-board · switch · table · textarea · tooltip`

**`./gated-action`** — ruling #143's implementation, and missing from this list until 2026-08-16 even though `package.json` has always exported it. `{ allowed, reason, action?, class?, children }`: when allowed it renders the children with NO wrapper at all; when refused it wraps them in a `Tooltip.Trigger` `child` span (`data-slot="gated-action"`, `aria-disabled="true"`, `class="contents"` so geometry is unchanged) with a capture-phase click/keydown guard. It deliberately does NOT set the native `disabled` attribute — that would kill the tooltip and drop the control from the tab order — so **a child that sets `disabled` itself defeats the mechanism**; make the child's own `disabled` conditional on the same verdict. Wrapping in a `Tooltip.Trigger` button instead of a span would be a nested-button SSR crash.

**`./hierarchy`** — `ProjectHierarchy` + `HierarchyNode` (#109): project › warehouse › namespace › table as a vertical graph. **The one subpath with a vendor-canvas dependency** — it draws on `@rask/flow`'s `StaticFlow`, so `@rask/flow` and `@xyflow/svelte` are `@rask/ui` dependencies for this component alone, and a zone mounting it must declare both itself AND import their two stylesheets `layer(base)` in its `app.css` (vendor sheet first, then `@rask/flow/styles.css`) or the canvas paints in the library's light-mode hex. Home's `/projects/<id>` and the lakehouse's Overview both render it; it lives here rather than in either zone because zones may not import each other, and transport-agnostic (the GrantsPanel precedent) because the library owns no API client — the two rung reads arrive as async props and each zone passes its own remote functions.

**`./identity`** — `Subject` + `subjectDisplay` (#87). An OIDC `sub` is an opaque base64 blob; the helper keeps readable identifiers verbatim and ellipsizes opaque ones in the middle, and the FULL subject always rides `title` so hover/copy still yields the value a tuple write needs. Render it with `<Subject value={…} />`, never `subjectDisplay(...).label` — the component is what makes the title invariant structural, after a call site dropped it within days of the helper shipping. Its own subpath, not under `grants-panel`: the concern is any surface that prints a sub (FGA subjects, control-event actors, audit stamps).

**Non-components** (`./utils`, `./motion`, `./runs`, `./color-mode` carry a `default` condition; `./shell` does not — it is `types` + `svelte` like a component, and shares rough edge 3; `./styles/tokens.css` is a bare path):

| Subpath | Contents |
|---|---|
| `./shell` | `AppShell, AppSidebar, AppError, TopNavbar, NotificationCenter, NotificationList, ForbiddenPage` + `nav-config` + `breadcrumb` |
| `./utils` | `cn`, `toDate/formatAbsolute/formatRelative/formatTimestamp`, `WithElementRef/WithoutChildren/WithoutChild/WithoutChildrenOrChild` |
| `./motion` | GSAP `{@attach}` factories |
| `./runs` | `runPhase/runPhaseLabel/runNotificationId/runJobLabel/runProgress/visibleRuns/unreadRuns`, `RunStatusLike` |
| `./color-mode` | `useColorMode()` |
| `./styles/tokens.css` | the theme |

Usage ranking across zones: `shell` (24) ≈ `select` (24) > `button` (20) > `badge` (19) > `data-table` (16) > `card` (15) > root barrel (14) > `utils` (12) > `motion` (11).

The root barrel `.` is a **curated subset** (`src/lib/index.ts`): button, badge, dialog, card, sort-header, utils, select, motion, chip, search-bar, status-board, runs, grants-panel. `README.md:30-31` names the subpaths authoritative — prefer `@rask/ui/button` over `@rask/ui`.

## Three barrel conventions

They coexist deliberately; match the one the component already uses.

1. **shadcn dual export** — `Root` + `Sidebar` + `SidebarRoot` (`sidebar/index.ts`). Consumed as `import * as Sidebar from '@rask/ui/sidebar'`.
2. **Namespace object** — `export const Dialog = { Root, Trigger, … }` (`dialog/index.ts:7`; same for `Table`, `AlertDialog`). Consumed as `import { Dialog } from '@rask/ui/dialog'`.
3. **Single default** — `export { default as Chip }`.

Re-exporting a Bits UI primitive unchanged needs an explicit `typeof` annotation; a bare re-export infers a non-portable type and silently skips the `.d.ts` declaration (`collapsible/index.ts:3-4`, repeated in `dropdown-menu`, `popover`, `alert-dialog`).

## The `child` escape hatch

`sidebar-menu-button.svelte:68-77` declares a local `{#snippet Button({ props })}`, merges with `mergeProps(buttonProps, props)` from `bits-ui`, and forwards to a caller-supplied `child?: Snippet<[{ props: Record<string, unknown> }]>`. `zone-nav.svelte` uses it at four call sites (`:66, :83, :99, :112`), each rendering the `leafLink` snippet at `:40-52`, to emit an `<a>` where the component would otherwise emit a `<button>`. Reach for this instead of forking a component to change its tag.

## Data-table stack

A vendored shadcn-svelte adapter over `@tanstack/table-core` 8.21.

- `createSvelteTable` (`create-svelte-table.svelte.ts:27-68`) takes **getter-based** options, holds `TableState` in `$state`, re-syncs in `$effect.pre`.
- `mergeObjects` is a lazy `Proxy` preserving getter semantics, later sources winning — the reactivity seam, unit-tested at `tests/data-table.test.ts:14-32`.
- `FlexRender` resolves a `ColumnDef` template that is a string, a `renderComponent()` config, a `renderSnippet()` config, or a primitive.
- `selectionColumn()` ships the indeterminate select-all column.
- `types.ts:11-14` augments tanstack's `ColumnMeta` with `{ headerClass, cellClass }` and is **type-re-exported** from the barrel so the augmentation reaches consumers through the `.d.ts` chain.
- The barrel re-exports the tanstack row models, so a zone needs no direct `@tanstack` dependency.

Containment is deliberate: `Card` and the DataTable card both carry `min-w-0 max-w-full`, and horizontal scroll lives on `Table`'s inner `[data-slot=table-container] overflow-x-auto`.

## Storybook & tests

**Storybook 10.4.6** with `@storybook/svelte-vite` (not `-sveltekit` — this is a library). Addons: docs, svelte-csf, a11y. `preview.ts` imports `tokens.css`; `tags: ['autodocs']` globally. Run `make storybook` (→ `:6006`).

Coverage is thin: **4 story files for ~35 exported components** — `button.stories.svelte` (CSF via `defineMeta` + `{#snippet template(args)}`), `card.stories.ts` (CSF3 `Meta/StoryObj`), `dialog.stories.svelte`, `Welcome.stories.svelte`. A new component should ship a story.

8 vitest suites, 103 tests, all pure-function or SSR-string. Seven live in `tests/`; a suite may also sit beside its source (`src/lib/components/identity/subject.test.ts`) — `vite.config.ts:17` includes both homes. No jsdom and no `@testing-library/svelte` (adding either changes the workspace lockfile). `run-status.test.ts` pins field names against the committed `docs/lineage-openapi.json`, so a backend rename fails the test rather than rendering blanks.

`harness/` is a standalone Vite page that mounts `NotificationCenter` in a real browser, because the popover is **portalled** and `svelte/server`'s `render()` emits nothing for a portal — SSR tests can assert the bell and the rows but never the open panel, focus trap, or dismiss click. Run manually: `bunx vite --config vite.config.ts harness --port 5411`, then `bun harness/drive.mjs`.

## Known rough edges

Do not treat these as patterns to copy.

1. `harness/drive.mjs:7` hardcodes an absolute path into a **different repo** (`/home/blackwell/Desktop/lance-ns/...`) and imports `@playwright/test`, which is not a dependency of `@rask/ui`. The harness cannot run as committed on another machine.
2. Three extensionless relative imports violate the package's own `.js` convention and survive into `dist`: `motion.ts:10` → `'./gsap'`, `status-board.svelte:9` → `'../../motion'`, and `search-bar.svelte:8` → `'../chip'` (a bare **directory** import, resolvable only by a bundler's implicit `/index` lookup).
3. Component subpaths declare no `default` condition — a tool resolving without the `svelte` condition gets an unresolvable specifier. Conversely `./color-mode` promises a `default` while shipping raw `$state`/`$derived`.
4. `seenOnClose` is exported from `runs/run-status.ts:152` and consumed by `notification-center.svelte:73` but omitted from `runs/index.ts` — invisible on the public surface.
5. Eight exported subpaths have zero consumers outside the package (`avatar, collapsible, dropdown-menu, navigation-menu, sidebar, tooltip, runs, status-board`); they exist to serve `shell/`.
6. Two near-duplicate sort headers: `SortHeader` (renders its own `<th>`, takes `sortKey/sortDir/onsort`) and `DataTableHeaderButton` (tanstack-driven, renders a bare `<button>`).
7. `GrantsPanel` (423 lines) is the largest component and is arguably domain logic — it hardcodes the FGA relation ladder `['reader','writer','validator','owner']`. It was hoisted here only after the data and lineage copies had silently drifted apart.
8. `ResizableSplit` defaults `storageKey = 'lance-media-split'` — a zone-specific localStorage key baked into a shared default.
9. `tsconfig.json:15`'s `baseUrl` triggers a TS 7 deprecation warning on every `check`.
