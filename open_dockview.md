# open_dockview — `@rask/dockview` workbench backlog

The workbench features the owner asked for on 2026-07-29 after driving the deployed docks. **None are
started.** Each is design work; they are written down so a separate session can pick one up whole
rather than have it half-built in the middle of unrelated work.

Kept out of `OPEN-WORK.md` on the owner's instruction: this is one component's backlog with its own
lifetime, and folding it into the estate register would bury a package-scoped list inside a
merge-scoped one.

## G. `@rask/dockview` — the workbench features that are missing *(new, 2026-07-29)*

Raised by the owner after driving the deployed docks. **Not started, and deliberately not started
here** — these are four independent pieces of design work, and the owner's instruction was to record
them for a separate session rather than half-build them mid-session.

**Read `.claude/skills/rask-frontend` § *Workbenches* first.** Its four invariants are load-bearing
and every item below has to hold them: panels mount ONCE (`mount()` in `init()`, `unmount()` only in
`dispose()`, so a drag never remounts), `defaultRenderer: 'always'` (or DOM-held state like scrollTop
dies on a move), layout is per-subject via `@rask/api/dock-layout` and **never** localStorage, and the
dock is dynamically imported so ~100 KB gzipped stays out of the entry graph.

What already exists, so nobody rebuilds it: `GroupActions.svelte` (split · maximize · float · popout ·
close, mounted into `.dv-right-actions-container`), `chrome.ts` (the `DockChrome` opt-out flags with
working defaults), `context-menu.ts`, `split.ts`, `persistence.ts`, and `types.ts`'s `PanelRegistry` —
the zone's panel catalogue, keyed by the `component` string dockview also resolves saved layouts by.

### G1 · Add-panel with a chosen direction — *replaces* the split button

**The split button is the wrong primitive and should go.** It duplicates the ACTIVE panel into a new
group (`split.ts:54` `containerApi.addPanel(...)`), so the only thing a user can create is a second
copy of what they are already looking at. The owner's words: *"completely useless."*

What is wanted is the Zed/VS Code shape: a **`+` in the group header** that opens the zone's
`PanelRegistry` minus whatever is already open, and where the *direction* is part of the choice —
add as a tab, or split above / below / left / right. One control, one decision, no duplication.

- `containerApi.addPanel({ id, component, position: { referenceGroup, direction } })` already takes
  `direction: 'above'|'below'|'left'|'right'|'within'` — the API is there; the UI is not.
- Panel ids must stay stable and unique: `component` is the contract a serialized layout resolves by,
  so an id scheme that collides breaks layout restore (see `types.ts:43`).
- A registry entry needs a human label and an icon for the menu. `PanelRegistry` is currently
  `Record<string, PanelComponent>` — it has nowhere to put either. Widening it is the first step.

### G2 · Panel watchers — notify on change

A panel should be able to declare a watcher so the dock can badge its **tab** when the data behind it
moves while the panel is not the visible one. Today a background panel changes silently and the user
finds out by clicking.

- `api.onDidVisibilityChange` gives the visible/hidden signal; the tab renderer is where a badge
  lands. `GroupActions.svelte` already derives everything from dockview events rather than polling —
  match that.
- Do **not** invent a transport. Every zone already opens exactly one `query.live` for the
  notification bell (`rask-frontend` § *Fetching data*), always inside `onMount`; a watcher rides
  that, or it is a second live connection per panel and the server holds them all.
- Panels stay mounted while hidden (invariant 1 + `defaultRenderer: 'always'`), so a watcher can keep
  running — which is exactly why the badge is meaningful and also why an unbounded one leaks.

### G3 · A GLOBAL workbench — any panel from any zone

Today a registry is per-zone: `media` offers atlas/treemap/topic, `compute` offers jobs/cluster/actors,
`lakehouse` offers graph/runs/events. The owner wants one workbench that can hold **any** panel from
**any** zone.

**This is the hard one and it is an architecture decision, not a feature.** Each zone is a separately
built SvelteKit app with its own bundle and its own `paths.base`; a panel is a Svelte component, and a
component cannot cross a bundle boundary by being imported. Three routes, none free:

1. **Promote shared panels into a package** (`@rask/panels`?) that every zone imports. Simplest, keeps
   one bundle per zone, but only works for panels whose data access is already zone-agnostic — and it
   makes the "global" set a curated subset, not "any panel".
2. **iframe per foreign panel.** True isolation, works today, no build changes. Costs: a second SSR
   round-trip per panel, no shared context across the mount boundary (`<Dock>`'s `getAllContexts()`
   hand-off stops at the frame), and the drag/resize story gets worse.
3. **Module federation.** The textbook answer, and the largest change to the build — see
   `micro-frontends` skill. Turborepo's microfrontends proxy composes *routes*, not modules.

Decide the route before writing code. Whatever wins must keep the dynamic import (G-invariant 4) and
must not pull a foreign zone's whole entry graph in behind one panel.

### G4 · Named, saveable, shareable layouts

Today there is exactly **one implicit layout per user**, autosaved: `LayoutStore.load()/save()` over
the catalog's `dock-layout` user-state document. Wanted: name a layout, keep several, load one back,
and hand one to a colleague.

- Keep the three-outcome `LayoutRead` contract intact — `ok` / `absent` / **`unreadable`** — and keep
  refusing to save on `unreadable`. That distinction exists because treating unreadable as absent makes
  the next autosave overwrite a workspace that is still there (`types.ts`). Named layouts multiply the
  chances of hitting it, they do not remove it.
- Sharing means a layout must be addressable by something other than the caller's subject, which is a
  change to the state-document key shape, and it needs an authz answer: a shared layout names panels,
  and a panel names data the recipient may not be entitled to see. **The layout is not the data**, but
  a layout that references a table the recipient cannot read has to fail gracefully, not blankly.
- A shared layout is untrusted input by the time it is loaded. It resolves `component` strings against
  the registry — an unknown one must be dropped with a visible note, never silently.

### Known constraint that touches G1 and G3

`dndStrategy: 'pointer'` is set deliberately (Linux reliability + Playwright testability) and it
**disables cross-window drag**, which is exactly what popout needs. Any work that leans on dragging a
panel between windows resolves that trade first. `rask-styling` also notes: do not animate dock chrome
with GSAP — dockview rewrites panel transforms every frame under `defaultRenderer: 'always'` and a
tween on the same property is a second writer.
