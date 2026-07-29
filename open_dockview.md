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

### G1 · The group header control set — owner-specified 2026-07-29

Four buttons, in this order, plus the two that already work. Specified by the owner; recorded verbatim
in intent so a later session does not re-litigate it.

| # | Control | Behaviour |
|---|---|---|
| 1 | **Split pane** | Prompts for a DIRECTION — up / down / left / right — the way Zed does it. Not a silent duplicate. |
| 2 | **`+` add panel** | Opens the registered-panel list and adds the chosen one. **With a search box**, because the list grows past eyeballing once G3 lands. |
| 3 | **Fullscreen** | Expand this group to fill the dock; flips to restore while maximized. Exists today as `maximize`. |
| 4 | **Popout** | Real second browser window. Exists today, gated on `popoutUrl`. |
| — | **Bell / alert** | See G2. Highlights the WHOLE panel on a change or event, not just a tab dot. |
| — | **Remove panel** | Close. Exists today (the per-tab ✕ is dockview's only stock control). |
| — | **Snapping** | Already works — the five-way group drop and the widened `dndEdges` in `DEFAULT_CHROME`. Do not rebuild it. |

**Split and `+` are DIFFERENT controls and both are wanted.** An earlier draft of this file said add-panel
*replaces* split; that was wrong. Split acts on the pane — divide this space in a direction. `+` acts on
the content — put a named panel here. Conflating them is what produced today's useless button.

**Corrected 2026-07-29 — the paragraph that stood here was wrong about the current code.** It said split
"only ever duplicates the ACTIVE panel … so the sole thing a user can create is a second copy of what
they are already looking at". `split.ts:43` refutes that: with **2 or more** panels in the group split
MOVES the active panel into a new adjacent group — the real split — and duplicates *only* at exactly
one panel, where dockview's `moveTo` onto a group's own single-panel group is a documented silent
no-op. `context-menu.ts:37-42` already offers all **four** directions, and `GroupActions.svelte:122-139`
already ships two direction buttons (right, down).

So the gap is narrower and more specific than the doc claimed: **the direction affordance in the
HEADER**, not the direction logic and not the split semantics. Two of four directions are unreachable
without opening a right-click menu, and nothing announces that the menu holds the other two. Do not
rewrite `splitPanel` — it is correct; give its four positions a discoverable control.

**Open question for whoever takes this:** after a direction-prompted split, does the new pane get a copy
of the current panel (Zed's behaviour, since Zed splits an *editor*), or does it open EMPTY and wait for
a `+`? Zed's model assumes the thing being split is the thing you want twice; a workbench panel usually
is not. Decide it deliberately — it changes whether split needs the panel picker too.

Implementation notes:

- `containerApi.addPanel({ id, component, position: { referenceGroup, direction } })` already accepts
  `direction: 'above'|'below'|'left'|'right'|'within'`. The API exists; only the UI is missing.
- Panel ids must be stable and unique — `component` is the string a serialized layout resolves by
  (`types.ts:43`), so a colliding id scheme breaks layout restore, not just the click.
- `PanelRegistry` is `Record<string, PanelComponent>` today: **no label, no icon, no keywords**. The
  picker needs all three (search matches on label and keywords), so widening that type is step one and
  every zone's registry has to be updated with it.
- The controls mount into `.dv-right-actions-container` via `GroupActions.svelte`, which already derives
  its reactive state from dockview events (`onDidLocationChange`, `onDidMaximizedGroupChange`) rather
  than polling. Match that; do not add a store.

### G2 · Panel watchers — the bell, and highlighting the whole panel

A panel declares a watcher; when the data behind it moves the dock raises an alert. The owner's
specification is that the **whole panel** highlights on a change or event — not a dot on the tab.
That is the point: a background panel changing silently is the failure, and a 6px dot on a tab the
user is not looking at reproduces it.

- `api.onDidVisibilityChange` gives visible/hidden. The bell button in the header is the persistent
  affordance (acknowledge / mute); the panel-wide highlight is the transient one.
- Do **not** invent a transport. Every zone already opens exactly one `query.live` for the notification
  bell (`rask-frontend` § *Fetching data*), always inside `onMount` — a watcher rides that. One live
  connection per panel means the server holds them all.
- This works at all because panels stay mounted while hidden (dock invariant 1 +
  `defaultRenderer: 'always'`), so a watcher keeps running when its panel is not visible — which is
  exactly why the alert is meaningful, and exactly why an unbounded one leaks. Bound it.
- The highlight is chrome, so it is `--dv-*` custom properties and rask tokens, not GSAP: dockview
  rewrites panel transforms every frame under `defaultRenderer: 'always'` and a tween on the same
  property is a second writer (`rask-styling` § *Theming dockview*).

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
