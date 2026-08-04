# The global workbench — built, shipped, RETIRED

*Final ruling 2026-08-03 (evening). The cross-zone compositor zone is DELETED. `@rask/dockview`
stays and is now used the way it should have been from the start: **a dock lives INSIDE its zone**,
composing that zone's own components over one shared store. `/explorer/workbench` (results + atlas
+ player over one search) is the estate's ONE dock — see "The standing decision" for why it is the
only one.*

## Why it was retired

**The feature was YAGNI.** The compositor's only unique capability was mixing panels from
DIFFERENT zones in one dock. No one had that workflow. Every complaint it drew was about panel
QUALITY, never about composition — which is the tell: the thing being asked for was "the zone's
real view, arrangeable", and that never needed a compositor.

**Its cost was fidelity, and the cost was structural, not sloppiness:**

- A custom element **cannot import a remote function** (`.remote.ts` endpoints are per-app), so
  every panel's data had to be re-plumbed through whatever GET-only proxy the zone happened to
  expose. Some surfaces had no such path at all — JetStream/streams and the FGA check/expand
  family are POST-only or remote-only, and were reported BLOCKED rather than faked.
- A custom element **cannot reuse a component that touches `$app/*` or the zone's live tick**,
  which is most of the good ones. Those panels had to be MIRRORED — re-implemented to look like
  the zone page. That mirroring is what the user kept seeing as "weird tables".
- Everything else — per-zone element bundles, their own compiled Tailwind, cache-busting, budget
  ceilings, a valibot-gated event contract — was overhead in service of the one feature nobody
  needed.

**What a dock inside the zone gets for free:** the zone's real components, its remote functions,
its live stores through ordinary context, its own navbar (the compositor even managed to paint
over the shared navbar's dropdowns — a bug that cannot exist in a zone page). One store, no
transport, no mirroring.

## What was kept

`@rask/dockview` in full — the binding, the G1–G4 chrome (split menus, the searchable "+" picker,
panel alerts, named views + `ViewSidebar`), `@rask/api/dock-layout` + `dock-views`, and the
catalog's `dock-layout` / `dock-layout-library` user-state envelopes. All of it is zone-agnostic
and all of it now serves the in-zone docks. `dock-reachability.test.ts` pins the docks the estate
ships, so a compositor cannot return unnoticed.

## What was deleted

The `workbench` zone and its eight registration points; both zones' `src/lib/elements/**`,
`vite.elements.config.ts`, `elements.css` and chained element builds; the `elements` budget
ceilings and `element-budget.test.ts`; `@rask/dockview/contract` and its pin test; the
`/api/audit` shim that existed only for an element; the top-level Workbench navbar entry.

## The lesson worth keeping

Two reversals in one day, in opposite directions, taught the same thing: **the zone is the unit of
ownership.** Moving a zone's panels into a shared package hollows it (the `@rask/panels`
reversal); moving a zone's panels behind a cross-zone element boundary starves them (this one).
Extract the *mechanism* into a library — `@rask/dockview`, `@rask/flow` — and keep the *domain*,
with its data and its components, in the zone that owns it.

---

## The reversal that preceded it

*Status: **the build-time design below was built, shipped, and REVERSED**. 2026-08-03. The reversal
restores the three per-zone workbenches; the cross-zone ambition continues in
a runtime-composition plan (now shipped — see the header). The original decision text is kept at
the bottom because its measurements (the 44-file move-set) and its corrected claims are still true —
only the conclusion drawn from them was wrong.*

## What was reversed, and why

The 2026-07-29 decision below created a `@rask/panels` workspace package, moved the lineage and
compute panels (and their stores, clients and graph components) out of their zones into it, added a
`workbench` zone that imported everything, and deleted the three per-zone workbench routes. It
worked. It was still wrong, for reasons that were on the record before it was built:

1. **It hollowed the zones.** The panels ARE the zones' domain code — `LineageGraph` and its store
   are what the lakehouse lineage area *is*; the jobs/cluster/actors panels are what compute *is*.
   Moving them to a shared package left the zones as routing shells around code they no longer
   owned, which defeats the reason this estate keeps micro-frontends at all: separately owned,
   separately deployed domain slices. The intended sharing layer was always the **design system**
   (`@rask/ui`) — things that make zones look alike — never the zones' own features.

2. **It picked the wrong row of the composition table.** The micro-frontends skill distinguishes
   build-time composition ("releases are coupled") from runtime composition ("fine-grained in-page
   composition", "favor native browser features — custom elements, CustomEvent"). A workbench that
   composes *views from different zones* is the textbook runtime-composition case. Build-time
   composition answered it by making the zones stop being different zones — solving the problem by
   deleting its premise.

3. **It broke the Turborepo package rule.** `frontend/packages/*` are **libraries** — design system,
   API clients, contracts, a canvas engine. `@rask/panels` was a *domain slice* wearing a package's
   clothes: it had one consumer per panel and existed only to launder zone code across a bundle
   boundary. The correct extraction from that episode was `@rask/flow` — the generic Svelte Flow
   binding (`GraphCanvas`, `FlowAutoFit`, `layout`) with no domain imports — and that package
   **stays**. The rule it demonstrates: extract the *mechanism* into a library, keep the *domain*
   in its zone.

4. **Panels went stale in the move.** The dock's whole point in-zone is that panels share the zone's
   live store (`getAllContexts()` hands the tree across). The package versions took one-shot data
   snapshots because the store seams were cut at the boundary — the compute panels lost their
   `liveRead` refresh, and 106 lines of `compute.remote.ts` were duplicated because remote functions
   are per-app by construction. Both regressions were consequences of the architecture, not bugs in
   the execution.

### What the reversal restored

- Lineage panels + `LineageGraph`/`MedallionNode`/`JobNode` + `store.svelte.ts` → back in
  `lakehouse/src/lib/{dock,lineage}`; compute's three panels → back in `compute/src/lib/dock/panels`
  with their direct `$lib/remote/compute.remote` imports (live data again).
- The three workbench routes (`/lakehouse/lineage/workbench`, `/media/workbench` — now
  `/explorer/workbench`, `/compute/workbench`) and their navbar/sidebar rows. Two of the three were
  reverted again hours later; see "The standing decision".
- The `workbench` zone and `@rask/panels` deleted; all eight registration points unwound.
- **Kept:** `@rask/flow` (a real library), `@rask/dockview` including the G4 views store +
  `ViewSidebar`, the `dock-layout-library` backend envelope, and the dock-reachability gate
  (now an EXACT pin, not a floor: `['/explorer/workbench']`).

## The standing decision (final, 2026-08-03 evening)

This section replaces an earlier "ONE global workbench, or none" ruling that the retirement above
overturned. Two rulings landed after it, in this order:

- **A dock lives INSIDE its zone.** Not a compositor, not custom elements. A dock panel is the
  zone's REAL component, importing the zone's own remote functions and sharing one store through
  `createContext` — which is precisely what an element could never do (endpoints are per-app; a
  `$app`-bound component cannot be mounted from a foreign bundle), and why the compositor's panels
  had to be mirrored copies. That fidelity cost, not the bundle size, is what retired it.
- **One dock per zone that earns one, at ZONE level — three today.** The instruction that shaped
  this was *"why are you putting workbench on lineage? … **start with** workbench actually only in
  media"*, and it carried two separate corrections that were briefly collapsed into one. The first
  is permanent: `/lakehouse/lineage/workbench` buried a ZONE surface inside one AREA, so it was
  invisible from catalog or models — docks now live at `/<zone>/workbench`, full stop. The second
  was sequencing, not cancellation: prove the recipe on ONE zone before spending it on three. That
  was read as "media only, forever" for a while; it was not.

  The explorer's dock shipped and was proven end to end (results → atlas ring → player, saved views
  persisting per subject), so lakehouse and compute followed on the same recipe — lakehouse with
  lineage graph + runs + events over ONE `LineageState` plus the catalog's own tables and object
  browser, compute with jobs + cluster + actors + serve over its own remotes.

  A dock is still EARNED, never granted by symmetry: it costs ~100 KB deferred and only pays where a
  multi-panel view of ONE subject is the actual workflow. train and studio carry placeholder data,
  home is the catch-all, and the annotator is already a canvas — none of them qualify.
  `dock-reachability.test.ts` pins the set EXACTLY, so neither a compositor, nor a symmetry-dock,
  nor a nested path can return unnoticed.
- **Panels' domain code stays in its zone** — the lesson of the `@rask/panels` reversal, and now
  structural rather than a rule to remember: with the dock inside the zone there is no other place
  for a panel to live. `frontend/packages/*` stays mechanism-only (`@rask/dockview`, `@rask/flow`).
- **The plan executed spike-first** — one panel proved light-DOM styling and move-without-remount
  before anything else was built; the work file that tracked it is deleted (see the header).
- **Iframes remain rejected** for first-party panels (they are the *untrusted-code* tool — VS Code
  webviews, Grafana plugins) — though the dockview fork's never-re-parent guarantee makes them
  viable if an untrusted-plugin surface ever appears.

---

## Appendix: the reversed decision of 2026-07-29 (kept for its measurements)

The original text follows, unedited in substance. Its factual corrections stand — a component *can*
cross a bundle boundary by import (`@rask/ui` proves it seven times), module federation is
unavailable under Vite 8 + rolldown, `@rask/explorer-api`'s base guard is per-process and no blocker.
Its conclusion — therefore centralize the panels in one zone — is the part the reversal rejects:
"possible" was answered, "wise" was never asked.

### The measured move-set — 44 files, and why the media trio is not cheap

Traced recursively over static imports, dynamic `import()` and `export … from`, resolving `$lib/*`.
**The union is 44 files plus one co-located test** — lakehouse 12, media 31, compute 1.

| Panel group | Zone-local modules that must move | Coupling to break |
|---|---|---|
| lineage (Graph · Runs · Events) | 9: `lineage-context.ts`, `LineageGraph`, `MedallionNode`, `JobNode`, `FlowAutoFit`, `layout.ts`, `store.svelte.ts`, `api.ts`, `http.ts` | `LineageGraph` imports `$app/paths` + `$app/navigation` |
| media Treemap · Topics | ~7: `dock/context.ts`, `workbench.svelte.ts`, `topic-treemap`, `topic-sankey`, `topic-results-panel`, `hit-list`, `player-pane` | `topic-results-panel` (1) and `player-pane` (2) import `$app/paths` |
| **media Atlas** | **~20**: the whole `lib/atlas/*` subtree plus `hit-table`, `transcript-window`, `transcript-highlighter`, `chunk-timeline`, `diarization-timeline`, `utils.ts`, `voice-search.svelte.ts`, `audio-preview.svelte.ts` | descriptor bootstrap must run first — `activeView()` THROWS if unloaded |
| compute (Jobs · Cluster · Actors) | 0 files — but `compute.remote.ts` **cannot move** | remote functions are per-app |

**Remote functions are per-app by construction.** SvelteKit hashes a remote function's endpoint id
from its path *relative to the app*, so a package-shipped `.remote.ts` would be re-registered and
re-served per zone — and would then execute under that zone's `handleFetch`, which for `lakehouse`
defaults to `LANCE_GATEWAY_URL` (`:8001`) rather than the gateway. This is one of the two hard walls
that make build-time panel sharing lossy (the other is the context/store seam), and it is exactly
the wall the web-components plan does not hit: a custom element ships with its OWN zone's build and
keeps its zone's fetchers.

These numbers are why the runtime plan is spike-first: the Atlas subtree in particular is the media
zone's identity, not a panel, and any plan that requires moving it has already failed.
