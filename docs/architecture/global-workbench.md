# The global workbench — reversed, and what replaces it

*Status: **the build-time design below was built, shipped, and REVERSED**. 2026-08-03. The reversal
restores the three per-zone workbenches; the cross-zone ambition continues in
`open_workbench.md` (repo root) as a runtime-composition plan. The original decision text is kept at
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
- The three workbench routes (`/lakehouse/lineage/workbench`, `/media/workbench`,
  `/compute/workbench`) and their navbar/sidebar rows.
- The `workbench` zone and `@rask/panels` deleted; all eight registration points unwound.
- **Kept:** `@rask/flow` (a real library), `@rask/dockview` including the G4 views store +
  `ViewSidebar`, the `dock-layout-library` backend envelope, and the dock-reachability gate
  (floor: the three in-zone docks).

## The standing decision (corrected 2026-08-03, same day)

- **ONE global workbench, or none — NO per-zone workbenches.** The first version of this reversal
  restored the three local workbench routes; that over-shot. The user's decision predating the
  reversal stands: the workbench is a single cross-zone surface. The local routes, their `lib/dock`
  wrappers, nav rows and per-zone user-state proxies were removed the same day;
  `dock-reachability.test.ts` pins the dock count at zero until the global one ships.
- **Panels' domain code stays in its zone** (the actual lesson of the reversal). The global
  workbench composes it at RUNTIME via custom elements — each zone builds and serves
  `rask-<zone>-<panel>` elements; a thin compositor zone loads them from the owning zone's
  deployment. Zone ownership, independent deploys, and the bundle boundary stay honest.
- **The in-progress plan lives at `open_workbench.md` (repo root)** — the open-work convention;
  this file records only what is decided. Spike-first: one panel proves light-DOM styling and
  move-without-remount before anything else is built.
- **Iframes remain rejected** for first-party panels (they are the *untrusted-code* tool — VS Code
  webviews, Grafana plugins) — though the dockview fork's never-re-parent guarantee makes them
  viable if an untrusted-plugin surface ever appears.

---

## Appendix: the reversed decision of 2026-07-29 (kept for its measurements)

The original text follows, unedited in substance. Its factual corrections stand — a component *can*
cross a bundle boundary by import (`@rask/ui` proves it seven times), module federation is
unavailable under Vite 8 + rolldown, `@rask/media-api`'s base guard is per-process and no blocker.
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
