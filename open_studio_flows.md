# open: studio flows — a node-based online sandbox on top of Ray

Working plan (delete when landed). Owner surface: the `studio` zone + a new `flows` service.

## What this is

A Langflow-style **online** flow builder in `/studio/flows`: drag nodes onto a canvas, wire
them, and execute against **live Ray Serve endpoints** — a sandbox for poking models
interactively, not a batch pipeline. Three reference points, and what each contributes:

- **Langflow** — the product shape: node palette, per-node config, run-and-inspect, sandbox feel.
- **Graphbook** (graphbookai/graphbook) — the execution ideas: per-node/subgraph runs (not only
  whole-graph), output caching so only *changed* subgraphs re-execute, server-declared node
  catalog with lifecycle methods.
- **ray-project/ray#62388** (Dashboard Dataflow DAG RFC) — the rendering ideas: live status
  colors on DAG nodes (running / done / failed / blocked-on-upstream), per-node timing stats,
  click-to-expand detail. Underneath, the long game is the same as that RFC's: the graph the
  user draws IS a Ray dataflow, and execution moves server-side onto the cluster.

## What already exists (measured, not assumed)

- **The explorer zone ships a full flow editor** (`explorer/src/lib/workflow/`, 37 files):
  singleton runes store (`$state.raw` nodes/edges + deep-`$state` `config`/`runtime` side
  tables keyed by node id, `data: {}` always), a **pure executor** behind a `RunDeps` seam
  (Kahn topo → promise-per-node parallel dataflow), fingerprint-based output caching with
  stale propagation, valibot parse-don't-validate localStorage persistence, undo/redo,
  NodeShell chrome, reconnectable edges, palette/context/⌘K menus. The studio builder copies
  this architecture wholesale and swaps the domain layer (node kinds + runNode switch).
- **`@rask/flow` is viewers-only by doctrine** — the editor mounts raw `<SvelteFlow>` in a
  `<SvelteFlowProvider>` (explorer's `FlowPane` pattern) and takes from the package only
  `styles.css` (mandatory theming), `depths`/`layout` (auto-arrange), `FlowAutoFit`, and
  later `StaticFlow` for saved-flow thumbnails.
- **Model invocation has exactly one HTTP door**: Ray Serve's own ingress on `:8000`
  (`/htrflow` takes raw image bytes → ALTO XML). `/api/serve/*` through the gateway is
  GET-only introspection (deliberate — the PUT deploy API is RCE) and is how the builder
  *discovers* live deployments (`serveApplications()` in `@rask/api`, already typed).
  The `models` zone's `api/infer/+server.ts` is the invocation precedent (401/413/timeout/
  `wrong_serve_app` guards) — it gets lifted into `@rask/api` so studio doesn't become a
  second drifting copy.
- **Studio is nearly empty**: launcher + animation toy, bell-only remote, **no
  `hooks.server.ts`** (the only zone without one), no xyflow deps, plain-link navbar entry.

## Fixes the new build makes that the reference implementation skipped

1. **`ssr = false` for real** — explorer's workflow page claims client-only but SSRs its
   module-level singleton (shared across requests + guaranteed hydration divergence).
   `/studio/flows` ships a `+page.ts` with `export const ssr = false`.
2. **Unit tests for the engine** — explorer's executor/fingerprint have a testable seam and
   zero tests. Studio's copies land WITH vitest coverage (topo, cycle refusal, cache reuse,
   upstream-failure blocking, fingerprint invalidation).
3. **Persistence goes through the parse boundary from day one** (valibot + `v.fallback`
   healing), keyed `studio-flow-graph-v1`; server-side user-state doc is the follow-up, as
   it is for explorer.

## Frontend (studio zone)

- Route `/studio/flows` (client-only). Page = toolbar + palette + canvas + inspector rail.
- `src/lib/flows/`: `types.ts`, `graph.svelte.ts` (singleton store), `executor.ts` (pure,
  `RunDeps`), `fingerprint.ts`, `persistence.ts`, `history.svelte.ts`, `node-types.ts`,
  `edges.ts`, `FlowPane.svelte`, `NodePalette.svelte`, `NodeShell.svelte`, `nodes/*.svelte`.
- **Node kinds v0** (payload union `{kind:'bytes'|'text', …}` flows along edges):
  | kind | role | does |
  |---|---|---|
  | `image` | source | file upload, object-URL preview → bytes payload |
  | `text` | source | textarea → text payload |
  | `model` | the point | pick a LIVE Serve app (from `/api/serve/applications/`), POST the upstream payload through the zone BFF, time it |
  | `alto` | transform | ALTO XML → plain text lines (composes after `model`) |
  | `inspect` | sink | pretty-render upstream payload + status/latency |
- **Execution**: whole-graph ▶ and per-node ▶ (Shift = ignore cache), fingerprint cache,
  stale chips, failure blocks dependents. Client-side for v0 — the "online sandbox" feel.
- **Serve discovery**: `serve.remote.ts` `query()` over `serveApplications()` with
  `getRequestEvent().fetch` (compute's pattern); on-mount + manual refresh (no poll loop).
- **Invocation**: `src/routes/api/infer/+server.ts` — thin wrapper over the new shared
  `@rask/api` server helper; `?app=<slug>` picks the Serve route, `STUDIO_SERVE_URL`
  (default `http://localhost:8000`) is the origin. Bytes stay on `+server.ts` per the
  transport ruling.
- **Zone wiring**: add `hooks.server.ts` (compute's form: `makeSessionHandle` +
  `makeGatewayHandleFetch(RASK_GATEWAY_URL)` — NOT `makeZoneHooks`), add
  `@xyflow/svelte` + `@rask/flow` + `valibot` + `vitest` deps, add the two vendor CSS
  imports `layer(base)` to `app.css`, add the Flows nav leaf + launcher card, and convert
  the navbar Studio entry from plain link to an `items:` panel (the stated consistency rule
  in `nav-config.ts:543` — studio now has ≥2 real surfaces).

## Backend (`services/flows`, `:8840`) — where "on top of Ray" becomes real

Fleet-layout service (`make_service_app`, flat modules, like `compute`). v0 scaffolds the
seams, not the cluster:

- `GET /api/flows/catalog` — **server-declared node catalog** (graphbook's model): kind,
  label, ports, param schema. The FE keeps its own registry v0 but the catalog is the seam
  that lets custom server nodes appear in the palette later.
- `POST /api/flows/validate` — graph hygiene: unknown kinds, dangling edges, duplicate ids,
  cycle refusal (Kahn). Shared vocabulary with the FE executor.
- `POST /api/flows/runs` + `GET /api/flows/runs/{id}` — execute a flow server-side.
  v0 = inline async executor (topo waves, `model` nodes call Serve over httpx); the
  **Dapr Workflow lane** (`runtime.py`/`workflow.py`/`activities.py`, dapr-ext-workflow —
  deterministic orchestrator fanning out `run_node` activities per topological wave) is
  scaffolded and starts only when a sidecar is present. Durable runs ride it in-cluster.
- Gateway row `/api/flows` → `RASK_FLOWS_URL` (`:8840`); `dev-micro.sh` starts it;
  `.docker/flows.dockerfile` per the deployable contract.
- Trajectory (NOT v0): compile the graph to Ray — `model` → Serve `DeploymentHandle`
  calls, heavier stages → Ray tasks — and stream per-node status back over the run
  resource, which is exactly the #62388 status DAG rendered live in the builder.

## What actually landed (verified, 2026-08-05)

Frontend, `frontend/microfrontends/studio/`:

- `src/lib/flows/` — `types.ts`, `graph.svelte.ts` (singleton, `$state.raw` graph + deep-`$state`
  config/runtime side tables), `executor.ts` (pure, `RunDeps`, Kahn topo + promise-per-node),
  `fingerprint.ts`, `persistence.ts` (valibot, key `studio-flow-graph-v1`), `history.svelte.ts`,
  `invoke.ts`, `node-types.ts`, `FlowPane/FlowsCanvas/FlowsToolbar/NodePalette`, `nodes/*` (5 kinds
  + `NodeShell`), `remote/{serve,engine}.remote.ts`, `serve-contract.ts`.
- `src/routes/flows/` (`+page.ts` with **`ssr = false`** — the reference editor's drift, fixed),
  `src/routes/api/infer/+server.ts`, `src/hooks.server.ts` (compute's form, `RASK_GATEWAY_URL`),
  `app.d.ts` `Locals extends AuthLocals` (without it the BFF's 401 guard silently never fires),
  the two vendor CSS imports in `app.css`, the nav leaf, the launcher card, `vitest.config.ts`.
- **19 unit tests** (executor/fingerprint/persistence) — the coverage the reference editor lacks.
- `@rask/ui`: Studio's navbar entry became an `items:` panel (Apps · Flows); its `nav-config.test.ts`
  pin was updated from "Studio is still the one single-surface zone".

**The zone IS the builder** (asked for 2026-08-05). The mini-app launcher and the GSAP animation A/B
are DELETED — routes, nav rows, launcher grid and the `gsap` dependency — and the canvas moved to the
zone root (`/studio`). Consequences, each handled rather than worked around: the navbar entry went
back to a plain link (one surface, and nav-config's own rule calls a one-row dropdown noise), so its
test pin reverted too; and `nav.ts` now carries ONE leaf, which tripped `zone-shell.test.ts`'s
`> 1 leaf` gate. That gate's stated reason is *"the shell hides a rail below 2"* — but the shell's
actual condition is `leaves > 1 || sidebarContent !== undefined`, and studio passes `sidebarContent`,
so the gate was changed to read the condition that decides it. The invariant it protects (no zone
silently loses its rail) is intact.

**Three panes, three questions.** The RAIL is the node library (what can I add), the CANVAS is the
graph (how is it wired), the RIGHT pane is `FlowsInspector` (what did the selected node produce):
status, timing, enabled/stale, per-kind config, upstream/downstream ids, an editable label, and a
full-size payload preview with copy + download. Selection lives on the store (`inspectedNodeId`), so
the canvas, the cards and the inspector cannot disagree; the split is a `ResizableSplit` whose ratio
persists.

**The node library is the RAIL's, not the canvas's** (asked for 2026-08-05, "like langflow"). It
rides the shell's `sidebarContent` seam — rail content under the route list, the slot the lakehouse
workbench's saved-views list already uses — and it is grouped the way the PLATFORM thinks rather
than the way the editor's types do: **Ray Serve** first (one row per LIVE deployment, each dropping a
Model node already pointed at that app), then Inputs / Inference / Transforms / Outputs, with a
search box over all of it. Drag a row onto the canvas, or click it to drop one at the viewport
centre. `palette.ts` is deliberately **store-free and Svelte-free** so the shared layout can mount
the library without instantiating the graph singleton (and its localStorage read) on every studio
route; the rail reaches the canvas through a DataTransfer payload or a window event. The floating
top-right palette it replaced is deleted.

Backend, `services/flows/` (**port 8840**, gateway row `/api/flows` → `RASK_FLOWS_URL`):
`catalog` / `validate` / `runs[/{id}]`, pure `graph.py`, an inline async executor, and the Dapr
Workflow lane (`runtime`/`workflow`/`activities`) that starts **only** when `DAPR_GRPC_PORT` is set.
46 tests. `.docker/flows.dockerfile`, `dev-micro.sh` row, `testpaths` + `known-first-party`.

`@rask/api/serve-proxy` — the models zone's inference proxy was lifted here (auth/size/timeout
guards, the `upstream_unreachable` 503 / `wrong_serve_app` 501 / `upstream_error` 502 taxonomy), so
studio's and models' `/api/infer` routes are four-liners over one body and cannot drift.

**Two defects found by driving it, both fixed:** `FlowGraph` used `extra="ignore"` while both fields
default to empty, so ANY wrong-shaped body validated as a clean empty graph and `/validate` answered
a false all-clear — it is `extra="forbid"` now (nodes/edges keep `ignore` so editor-only fields like
`position` still parse); and an empty graph reported zero problems, which now reads
`graph has no nodes`.

**Browser-verified** (`playwright-cli`, real Chromium): the canvas mounts, Serve discovery lists the
live cluster's RUNNING apps, autosave round-trips through a reload, an upload renders a blob preview,
and a per-node run POSTs `/studio/api/infer?app=…&name=…` whose 503 surfaces as
"Ray Serve is unreachable…" on the node card. Zero JS console errors.

**Open, and named rather than hidden:** `known-first-party` is still short by eleven first-party
names — adding them re-sorts imports across 172 files (measured), so it belongs in its own commit.
Serve's `:8000` ingress is not reachable from this host, so a live model call is still unproven
end-to-end.

## The breadth pass (2026-08-05, "scaffold just stuff")

Four Langflow-shaped ideas put in place, each shallow but honest:

- **JSON request bodies.** The Model node has a `Body` mode: `upstream` (raw forward — right for
  `/htrflow`, which reads image bytes) or `json`, an editable template with `{{input}}` interpolated
  JSON-escaped. This is what made the cluster's OTHER apps callable at all: `gemma-31b`,
  `qwen3-embed` and `qwen3-rerank` all want JSON, and raw-forward was the only mode. A JSON node can
  run with no upstream input (a fixed probe). Schemas are NOT guessed — Serve publishes none the
  frontend can read, so the user edits the template.
- **Saved flows** (`library.svelte.ts`) — named snapshots, a picker, save/delete. localStorage,
  keyed by name; the catalog user-state document is the named follow-up and the four calls to
  re-point are `list`/`load`/`save`/`remove`. Snapshots are opaque strings so the library never
  learns the graph schema and cannot drift from it.
- **API export** (`api-snippet.ts`) — curl + Python for `POST /api/flows/runs`, built from the live
  graph. It drops `image` nodes AND their edges and says which, because an upload lives in the
  browser and a server run including it would always fail; `file` is stripped from every config
  (`JSON.stringify` would flatten a `File` to a misleading `{}`).
- **Two new kinds**: `prompt` (real — pure text composition, the piece that turns a transcription
  into a question for an LLM) and `dataset` (**openly a scaffold**: it makes the lakehouse→flow seam
  visible and REFUSES at run time rather than emitting plausible rows, in both halves).

Both halves were kept in step: the service's catalog + dispatch learned `prompt` and `dataset` too,
because `validate_graph` refuses an undeclared kind — so without that the exported snippet would
have produced a request the backend rejects. Verified through the gateway: a text→prompt→inspect run
returns `"Summarise: anno 1723"`, and a dataset node fails with its scaffold message while its
dependent reports `upstream failed`.

## The bottom drawer + run log (2026-08-05)

The floating toolbar had grown to ~550 px tall with the API snippet open, covering the canvas it
floated over. Fixed by shape, not by shrinking text: the toolbar is now ONE compact row (Run + four
icon buttons), and everything bulky lives in a **bottom drawer** with `Logs` / `Flows` / `API` tabs.
It collapses to a 29 px bar (always visible, so it is discoverable), its height is drag-resizable and
persisted, and clicking the active tab closes it — one control for both directions.

**Logs are the debugging surface.** `logs.svelte.ts` is a bounded (400-entry) ring buffer, and the
executor writes to it through a new `RunDeps.log` — part of the SEAM, so the engine still runs in
tests with a stub and never reaches a store. A failed run now reads as a transcript:

```
19:53:54  run started — 4 node(s)
19:53:54  model    POST /htrflow — bytes (352292 B)
19:53:54  model    Ray Serve is unreachable — is `make serve-up-htrflow` running?
19:53:54  alto     skipped — an upstream node failed
19:53:54  inspect  skipped — an upstream node failed
19:53:54  run finished in 57 ms
```

The Logs tab carries the warning/error count as a badge, has a problems-only filter, and copy/clear.
A per-node run also logs which ids actually executed versus came from cache — the one thing you
cannot otherwise tell when a per-node ▶ looks like it did nothing.

**Resizability, honestly:** the right inspector is `@rask/ui`'s `ResizableSplit` (the explorer's
component, persisted); the drawer is the same interaction hand-rolled, because `ResizableSplit`'s
second pane has a hard `minRight` and so cannot collapse to a bar — and swapping between a split and
a non-split layout would remount the canvas and lose the viewport. The LEFT rail is the shared shell
sidebar: collapsible to icons via its rail toggle, but its width is fixed by the design system, and
making it drag-resizable is a `@rask/ui` change affecting all seven zones — an estate decision, not
this zone's.

Known rough edge, not fixed: click-added nodes land at the viewport centre, which can be under the
floating toolbar.

## Deliberately deferred (explicit, not silent)

- Chart wiring for the flows service (Deployment/Dapr annotations) — follows the `ingest`
  precedent (dockerfile lands first, chart is a separate pass).
- Server-side persistence of flows (user-state doc), multi-flow library UI, StaticFlow
  thumbnails.
- FE consuming the server catalog to build the palette (v0 proves the seam with a status
  read only).
- SSE/streaming per-node progress for server runs.
