# The global workbench — one dock, any panel

*Status: **decided, not implemented**. 2026-07-29. Supersedes the three-route sketch in `open_dockview.md` § G3.*

## The decision

**One new zone at `/workbench`**, holding a single `@rask/dockview` dock plus a sidebar of saved
views, with every panel imported from a new `@rask/panels` package. The three per-zone workbenches
(`/lakehouse/lineage/workbench`, `/media/workbench`, `/compute/workbench`) are deleted.

**Build-time composition — an ordinary workspace package, imported.** Not module federation, not
iframes, not web components.

## Why, and the claim this corrects

`open_dockview.md` § G3 opened with *"a component cannot cross a bundle boundary by being imported."*
**That is false, and this repo disproves it seven times over**: `@rask/ui` is imported by all seven
zones and `@rask/dockview` by three. Components cross zone boundaries by import routinely.

What is true is narrower: build-time sharing **duplicates bytes** — each importing zone bundles its
own copy, because there is no shared runtime. That is a budget cost, not an impossibility, and it is
the cost this design pays deliberately by concentrating the panels in ONE zone rather than three.

Three further claims made during the G3 assessment were also wrong and are corrected here:

- *"`@rask/media-api` refuses to re-base, so Atlas cannot move."* Its own docstring says the base is
  *"only safe because a zone is a PROCESS"*. A new zone is a new process, sets the base once, and
  the guard never fires. Not a blocker.
- *"Module federation is the textbook answer."* It ships for webpack and rspack; rask builds every
  zone with Vite 8 + rolldown, and `grep -rn 'federation|single-spa|importmap'` over `frontend/`
  returns nothing. Unavailable, not merely large.
- *"Web components are not an option."* They are — Svelte 5 compiles to custom elements — and they
  were omitted from the original three routes. Rejected here for different reasons: Shadow DOM cuts
  the page off from rask's Tailwind/token cascade, Svelte context does not cross the boundary, and
  the estate has zero custom elements today. Reconsider only if a non-Svelte consumer ever appears.

## Rejected: iframe per foreign panel

Cheap (~1 KB, no new dependency) and a foreign panel would cost the host zone zero client bytes. It
loses the thing that makes the docks worth having: `<Dock>` hands its whole context tree to every
panel via `getAllContexts()`, so lakehouse polls **one** `LineageState` shared by graph, runs and
events — they can never be a poll apart. A frame boundary stops that hand-off dead and turns three
cooperating panels into three independent pollers. Rejecting an in-process design in favour of one
that structurally cannot share state is the wrong trade for a workbench.

## What it does NOT deliver

Stated plainly, because § G3 demands it:

- **Not "any panel" on day one.** The catalogue is what `@rask/panels` contains. A panel that has
  not been moved is not available, and the media trio is expensive to move (below).
- **No cross-*process* isolation.** One heavy panel that throws on mount can break the dock's render
  pass. `MissingPanelRenderer` covers an unknown `component` string, not a panel that crashes.
- **No independent deploy per panel.** Panels ship with the workbench zone's image. A panel change
  redeploys that zone. That is the price of build-time composition and it is accepted.

## The measured move-set — 44 files, and why the media trio is not cheap

Traced recursively over static imports, dynamic `import()` and `export … from`, resolving `$lib/*`.
**The union is 44 files plus one co-located test** — lakehouse 12, media 31, compute 1. That number is
measured, not estimated; three earlier estimates in this work were all low, which is the reason it is
recorded here rather than left to the next person to re-derive.

| Panel group | Zone-local modules that must move | Coupling to break |
|---|---|---|
| lineage (Graph · Runs · Events) | 9: `lineage-context.ts`, `LineageGraph`, `MedallionNode`, `JobNode`, `FlowAutoFit`, `layout.ts`, `store.svelte.ts`, `api.ts`, `http.ts` | `LineageGraph` imports `$app/paths` + `$app/navigation` |
| media Treemap · Topics | ~7: `dock/context.ts`, `workbench.svelte.ts`, `topic-treemap`, `topic-sankey`, `topic-results-panel`, `hit-list`, `player-pane` | `topic-results-panel` (1) and `player-pane` (2) import `$app/paths` |
| **media Atlas** | **~20**: the whole `lib/atlas/*` subtree (`mount-atlas`, `AtlasMap`, `AtlasLegend`, `AtlasTooltip`, `gpu-scatter`, `atlas-colors`, `atlas-geometry`, `atlas-grid`, `atlas-legend`, `cross-filter.svelte.ts`, `gpu-support`) plus `hit-table`, `transcript-window`, `transcript-highlighter`, `chunk-timeline`, `diarization-timeline`, `utils.ts`, `voice-search.svelte.ts`, `audio-preview.svelte.ts` | descriptor bootstrap must run first — `activeView()` THROWS if unloaded |
| compute (Jobs · Cluster · Actors) | 0 files — but see below | `compute.remote.ts` **cannot move** |

**`$app/*` cannot appear in a package.** `@rask/ui` already establishes the answer: it imports no
`$app/*` and does browser detection with `typeof window !== 'undefined'`. So `base` and a `navigate`
callback become props on the four coupled components. Four files, not a redesign.

**Remote functions are per-app by construction.** SvelteKit hashes a remote function's endpoint id
from its path *relative to the app*, so a package-shipped `.remote.ts` would be re-registered and
re-served per zone — and would then execute under that zone's `handleFetch`, which for `lakehouse`
defaults to `LANCE_GATEWAY_URL` (`:8001`, the lineage service) rather than the gateway. The compute
panels therefore take their fetchers from context; the workbench zone declares its own three-line
`.remote.ts` and provides them. The underlying `/api/ray/*` paths are already root-absolute and
answer from any origin.

## Recommended sequencing

1. **lineage + compute trios first** — 9 files and 2 `$app` couplings, plus fetcher injection. Six of
   nine panels, and it proves the whole shape end to end.
2. **media Treemap + Topics** — 7 more files, 3 couplings.
3. **Atlas last, or never.** ~20 files including a WebGPU renderer and a descriptor bootstrap that
   throws when unloaded. It is the media zone's identity, not a panel. If it stays put, say so in the
   catalogue rather than letting the picker offer something that cannot mount.

`LineageGraph` and the media components are used by non-workbench routes (`/lakehouse/lineage`,
`/media/tree`, `/media/`), so they move to `@rask/panels` and those zones **re-import** them. Normal
package extraction — but it means lakehouse and media gain a dependency on `@rask/panels`.

## Known consequences

- **Saved layouts reset once.** Existing `dock-layout` documents are keyed by workbench id
  (`lineage`, `media`, `compute`); one global dock has one id, so current arrangements are orphaned.
  It degrades gracefully — `MissingPanelRenderer` renders a closeable placeholder rather than
  crashing — but it is a real one-time loss and should be in the release note.
- **Three routes 404 after deletion.** They are linked from the navbar and three sidebars; add
  redirects to `/workbench`.
- **A new pod.** `chart/values.yaml frontend.apps` gains an entry → one more Deployment, Service and
  Ingress rule.
- **Eight registration points must agree** or `@rask/zone-contract` fails: `microfrontends.json`,
  `svelte.config.js`, `vite.config.ts` (strictPort), `chart/values.yaml`, `Makefile ZONES`,
  `budget.json`, the BFF routes, and the nav. That gate firing is the good outcome.
