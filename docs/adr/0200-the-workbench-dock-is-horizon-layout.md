# 0200. The workbench's dock is horizon-layout (2026-11-11)

**Status:** accepted. Source: the owner, 2026-11-11, "Please use horizon layout", after the dependency upgrade of FE-021
held `dockview` at 7.0.4; and, the same day through the question tool, "In the browser" for where saved layouts and
named views live.

## Context

Measured 2026-11-11 at main `610561590`:

- **The console's workbench (`/workbench`) docked two panels on dockview 7.0.4**, through the package `@rask/dockview`
  (3,760 lines with 61 unit tests): a Svelte renderer that mounted each panel once and re-parented it on every move,
  a theme of about 40 `--dv-*` properties, a chrome layer (group actions, a split menu, a context menu, a panel
  picker, alerts), autosave, and the named-views sidebar. The page turned popouts off.
- **dockview 7.0.4 (2026-07-22) is the last 7.x.** dockview 8.0.0 followed on 2026-08-10 and 8.9.0 is the latest. In
  8.9.0, `dockview-core`'s `ENTERPRISE_MODULE_NAMES` lists `KeyboardNavigation` and `KeyboardDocking` among the
  modules that ship in `dockview-enterprise`, a separately licensed package (`SEE LICENSE IN LICENCE.md`). On 8.9.0 the
  workbench logged "`keyboardNavigation` requires the KeyboardNavigation module, which ships in dockview-enterprise"
  and lost its keyboard navigation, so the FE-021 upgrade held dockview at 7.0.4.
- **horizon-layout 1.0.5** (npm, published 2026-11-10; 1.0.0 published 2026-06-12; read at commit `80e748d`): MIT, peer
  `svelte ^5.0.0`, no runtime dependency, about 1,800 lines of source. It is headless: every element carries a BEM
  class from `baseClass`, and an optional sheet supplies structure and a palette. The layout is one bindable plain
  object, `LayoutConfig` (a tree of splits and tab groups, `maximizedView`, `popouts`), with `parseLayoutConfig` and
  `validateConfig` exported. Keyboard control is built in: the tab list takes arrows, Shift+arrows reorder, Ctrl+arrows
  move the active tab to the neighbouring group, Alt+arrows split it out, and each resizer is a `slider` that arrows
  move. Views are a `SvelteMap` of `{title, snippet, tabControls}`, and `tabgroupControls` are snippets in every
  group's toolbar.
- **What horizon-layout does not do**, read from its source: it renders only the active tab's snippet
  (`TabGroup.svelte`), and only the maximized view while one is maximized, so a panel is destroyed when its tab is not
  active, when it is moved, maximized over or popped out. A popout `mount`s the view's snippet into a new
  `about:blank` window outside the page's component tree, after copying the page's `<link>` and `<style>` sheets into
  it. There are no in-window floating groups, no context menu and no tab-group chips.

## Decision

- **The workbench's dock is horizon-layout 1.0.5**, pinned exactly in `frontend/console/package.json`. `dockview`,
  `@rask/dockview` and `frontend/console/static/popout.html` are deleted.
- **The dock is the console's own code**, in `frontend/console/src/lib/dock/`: `layout.ts` (the panel registry, the
  default arrangement of Tables beside Events, and the edits the controls make), `Dock.svelte` (the library with a close
  button on each tab, and per group the panel picker, maximize and pop out, from `@rask/ui` components; a Restore bar
  while a panel is maximized), `autosave.ts`, `views.svelte.ts` with `ViewSidebar.svelte` (the named views, now over
  `LayoutConfig`), `store.ts` (where they are kept) and `theme.css` (the `--hl-*` palette mapped onto the tokens; the
  library's sheet keeps the structure). There is no context menu.
- **A panel keeps nothing in component state that must survive a remount**, and reads no context. Its data lives in a
  module (`bench.svelte.ts`'s one `EventFeed`) or a shared read instance (`TableRegistry` seeds from `fetchTables()`);
  scroll position and view state (search, sort, page) reset when a panel remounts.
- **Saved layouts and named views live in this browser** (owner, "In the browser"): `localStorage`, one key per
  signed-in person, `rask-workbench:` and the SHA-256 of the principal's id (never the id itself), one fixed key where
  nobody signs in. The value holds the last layout and the named views and nothing else: panel ids, split points and
  the names a person gave their views, no token and no lakehouse data. Every access is guarded; where the browser
  refuses storage the workbench keeps them in memory for the tab and says so in one line. Sign-out does not clear them,
  unlike the activity log: a layout is a preference. `#lib/dock/store.ts` alone decides this.
- **A stored layout is read with `parseLayoutConfig` then `validateConfig`**, and one that fails either (or a value
  that is not JSON) is `unreadable`: the default is shown, a notice says so, and nothing is saved over the stored one
  until the person presses Reset layout. The catalog's user-state read and write, which answered 501, and
  `@rask/api`'s `dock-layout` and `dock-views` stores are deleted.
- **Popouts are on.** The console's CSP is unchanged: a popout is `about:blank` under the page's own policy, its sheets
  are same-origin links, and its script is the page's. The dock mirrors the page's `<html>` classes onto the window so
  dark mode follows.
- **Floating groups go.** horizon-layout has none, and the workbench offers none.

## Considered options

| Option | GitOps | Cloud native | Lance-idiomatic | Zero trust | Outcome |
|---|---|---|---|---|---|
| Stay on dockview 7.0.4 | meets (locked) | meets | n/a | meets | rejected: the last 7.x, no fixes after it |
| dockview 8 with `dockview-enterprise` | meets | meets | n/a | meets | rejected: keyboard control behind a separate licence |
| dockview 8 without keyboard control | meets | meets | n/a | meets | rejected: keyboard control is the reason for a dock |
| **horizon-layout 1.0.5** | meets (locked) | meets | n/a | meets | **chosen** |
| A dock written here | meets | meets | n/a | meets | not chosen: horizon-layout is that size already, MIT, and can be carried if it stops |

## Consequences

- **Panels remount.** A move, a maximize, a popout or a tab switch rebuilds the panel; its data comes back at once from
  the module or read instance, and its scroll position and view state do not.
- **No floating groups, no context menu, no tab-group chips.** Splitting, moving and maximizing are what remain.
- **A young dependency with one maintainer account.** horizon-layout's first npm release is 2026-06-12 and its public
  repository has one squashed commit; it is pinned exactly, and at about 1,800 lines of MIT source it can be carried in
  this repository if it stops being maintained.
- **A bits-ui overlay inside a popped-out panel** (the registry's stage select) still portals into the page's document.
- **A layout follows a person on one browser only.** Another browser or machine starts from the default; clearing site
  data clears it. A shared or synced home for layouts would be a decision of its own.
- **Zero trust is kept**: what is stored names no table and carries no credential, and the key does not name the
  person.
- **Tests**: `@rask/dockview`'s 61 unit tests go with it; the console gains unit tests of the layout edits, the default
  layout's validity, the stored-layout read, the browser store (ok, absent, unreadable, refused storage) and the
  autosave's refusal; `e2e/workbench/dock.spec.ts` drives the dock on the suite's edge, keyboard included, and
  `e2e/admin/workbench-layouts.spec.ts` proves a layout survives a reload and its new sign-in, and that a named view
  applies.

## Amendment (2026-11-11): the tab lists are patched

A walk of the deployed workbench with playwright-cli found two gaps in horizon-layout 1.0.5's tab lists; the keys
themselves all work. Its tabs carry no `aria-selected` and its shown view no `tabpanel` role, so assistive technology is
told neither which tab is shown nor what the panel holds. A keyboard move or split re-renders the group the keyboard
was in, so focus falls to the page and the next key does nothing. Both are fixed in the library's `TabGroup.svelte` by
`frontend/patches/horizon-layout@1.0.5.patch` (bun's `patchedDependencies`, as the Arrow patch is), described in
`frontend/TOOLING.md`. The patch goes once upstream ships the same.
