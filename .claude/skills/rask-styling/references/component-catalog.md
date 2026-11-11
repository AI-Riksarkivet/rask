# Component catalog & packaging

`frontend/packages/ui` — the design system: components with more than one use, the shell every page renders in, and the theme; `src/lib/shell` is ~930 lines (measured 2026-11-11). Code with one consumer lives in that consumer (ADR 0201): the access-control features (`gated-action`, `grant-form`, `grants-panel`, `identity`) are the console's, under `console/src/lib/access/` (ADR 0202).

## Build

No build step: `package.json` `exports` name the source (`./src/lib/components/<x>/index.ts`), and the console compiles `@rask/ui` from it with its own Vite and svelte-check, so the workspace's `check` and `test` build nothing first (ADR 0201). `storybook-static/` is gitignored.

`tsconfig.json`: `strict` + `noUncheckedIndexedAccess`, **no** `exactOptionalPropertyTypes` (the Bits UI "union too complex" incompatibility — leave it off), `target: ES2022` (so `toSorted` and the other ES2023 array methods are out), `verbatimModuleSyntax`.

`vite.config.ts` serves Storybook + vitest, and its `test` block is load-bearing: `include` names **both** homes — `tests/**` and `src/**` — because narrowing to one silently drops the other.

## The 20 export subpaths

**Components** — `src/lib/components/<name>/index.ts`, conditions `svelte` + `types` only:

`alert-dialog · badge · button · card · checkbox · data-table · dialog · dropdown-menu · input · popover · select · sheet · skeleton · table · tooltip` (measured 2026-11-11 against `package.json` `exports`). `avatar`, `collapsible`, `separator` and `sidebar` are in `src/lib/components/` and exported by no subpath: the shell alone uses them, by relative import.

**Non-components** (`./utils` and `./motion` carry a `default` condition; `./shell` does not — it is `types` + `svelte` like a component, and shares rough edge 3; `./styles/tokens.css` is a bare path):

| Subpath | Contents |
|---|---|
| `./shell` | `AppShell`, `AppError` + `nav-config` (the sidebar's and the bell's types; the route matchers are the console's, `#lib/nav-match.ts`). The frame's parts (`app-sidebar`, `page-nav`, `navbar-user`, the bell `activity-center`, `breadcrumb`) are not exported: `AppShell` mounts them. The bell is fed through props and callbacks; the console owns the log (`rask-frontend`) |
| `./utils` | `cn`, `toDate/formatAbsolute/formatRelative/formatTimestamp/formatZoned/utcOffsetLabel`, `WithElementRef/WithoutChildren/WithoutChild/WithoutChildrenOrChild` |
| `./motion` | GSAP `{@attach}` factories |
| `./styles/tokens.css` | the theme |

The root barrel `.` is a **curated subset** (`src/lib/index.ts`): button, badge, dialog, card, utils, select, motion. `README.md:30-31` names the subpaths authoritative — prefer `@rask/ui/button` over `@rask/ui`.

## Two barrel conventions

They coexist deliberately; match the one the component already uses.

1. **shadcn dual export** — `Root` + `Sidebar` + `SidebarRoot` (`sidebar/index.ts`). Consumed as `import * as Sidebar from '../components/sidebar/index.js'` by the shell.
2. **Namespace object** — `export const Dialog = { Root, Trigger, … }` (`dialog/index.ts:7`; same for `Table`, `AlertDialog`). Consumed as `import { Dialog } from '@rask/ui/dialog'`.

Re-exporting a Bits UI primitive unchanged needs an explicit `typeof` annotation; a bare re-export infers a non-portable type and silently skips the `.d.ts` declaration (`collapsible/index.ts:3-4`, repeated in `dropdown-menu`, `popover`, `alert-dialog`).

## The `child` escape hatch

`sidebar-menu-button.svelte:68-77` declares a local `{#snippet Button({ props })}`, merges with `mergeProps(buttonProps, props)` from `bits-ui`, and forwards to a caller-supplied `child?: Snippet<[{ props: Record<string, unknown> }]>`. `shell/page-nav.svelte` (the sidebar's rows) declares a `leafLink` snippet at `:32` and passes `child` at four call sites (`:73, :90, :106, :119`): three render `leafLink` (`:74, :107, :120`) to emit an `<a>` where the component would otherwise emit a `<button>`, and one (`:90`) renders a `Collapsible.Trigger` in place of a group label. Reach for this instead of forking a component to change its tag.

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

Coverage is thin: **5 story files for 15 component subpaths** (the 20 `exports` less `.`, `./shell`, `./utils`, `./motion` and `./styles/tokens.css`, counted 2026-11-11) — `button.stories.svelte` (CSF via `defineMeta` + `{#snippet template(args)}`), `card.stories.ts` (CSF3 `Meta/StoryObj`), `dialog.stories.svelte`, `shell/activity-center.stories.svelte`, `Welcome.stories.svelte`. A new component should ship a story.

Vitest suites are pure-function or SSR-string. Four live in `tests/`; a suite may also sit beside its source — `vite.config.ts` includes both homes, `tests/**` and `src/**`. No jsdom and no `@testing-library/svelte` (adding either changes the workspace lockfile).

## Known rough edges

Do not treat these as patterns to copy.

1. An extensionless relative import violates the package's own `.js` convention: `motion.ts:10` → `'./gsap'`.
2. Component subpaths declare no `default` condition — a tool resolving without the `svelte` condition gets an unresolvable specifier.
