---
name: rask-frontend
description: The rask `frontend/` plane — seven SvelteKit 2 + Svelte 5 zones composed by Turborepo's microfrontend proxy, the remote-function data plane (BFF only for binary/Arrow payloads), the `@rask/dockview` workbenches, and the oxlint/oxfmt/zone-contract gates. Use when touching a zone, `@rask/api`, `@rask/dockview`, `@rask/zone-contract`, `microfrontends.json`, or any `.svelte`; when adding a route or fetching data; when working on a dock, panel, workbench or saved layout; when a panel loses its state on drag or a cross-zone link 404s or an SSR fetch hairpins; or when adding a zone or a frontend dependency.
---

# rask frontend

Every JS/TS file lives under `frontend/`, its own bun 1.3.14 + Turborepo 2.10.7 workspace root (`package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`). Invoke everything scoped: `bun --cwd=frontend run <task>`. The `--cwd=` form matters — `bun --cwd <path>` with a space silently no-ops.

Workspace membership is **globbed** (`microfrontends/*`, `packages/*`), so a directory carrying a `package.json` is enrolled automatically and one without it is **silently skipped** — bun prints "Done!" and the package is never installed, built, or linted.

Styling and component authoring live in **`rask-styling`**. Svelte 5 and SvelteKit idioms live in `svelte-skills:*` and the `svelte` MCP. This skill is the plane above them: zones, data, composition, gates.

## The seven zones

Package name equals directory name for all seven (`manifest.test.ts:53`). Base is a bare `/<zone>` — **no `/default/` segment exists**, and `cross-zone-reload.test.ts:38` asserts `/default/lakehouse` is not a zone path.

| zone | base | dev port | nav label | what it is |
|---|---|---|---|---|
| `home` | `''` catch-all | 5273 | Home | The ESTATE LEVEL: `/` (an insights landing, scaffold-badged), `/projects` (+ `/projects/<id>`, the gallery/table list, create, and one project's overview) and `/settings` (estate config, admin-gated SERVER-side) — plus the **OIDC BFF** (`/auth/{login,callback,logout}`) |
| `lakehouse` | `/lakehouse` | 5174 | Lakehouse | The big one (dock at `/lakehouse/workbench`) — areas `catalog`, `lineage`, `models`, `admin`, `governance`, `workbench`; **storage is not an area**, it is `/lakehouse/catalog/storage`. 47 route files, **7 `+server.ts` routes** — 4 keep-bytes (Arrow query/insert, blob bytes + the #113 commit log, the `/api/explorer/**` storage-browser seam), 1 keep-flow (`capi/v1/me`), 2 catch-alls; 15 `.remote.ts` modules carry the rest. `requestJSON` is **not** at zero here — 4 call sites: the object browser (`lib/storage/storage.ts:69,73`), the #113 commit log and the row insert (`lib/data/catalog.ts:119,126`) |
| `explorer` | `/explorer` | 5173 | **Explorer** | Corpus search workbench (with a dock at `/explorer/workbench`): FTS/vector/hybrid, WebGPU atlas, Cypher KG, Svelte-Flow editor; **6 `+server.ts` routes** (was 13) — 4 keep-bytes + `api/search`/`api/atlas/chunks`, which keep their route (multipart / rowid-list POST) but answer **Arrow IPC**; every JSON value surface rides one of 5 `.remote.ts` modules (the transport ruling area 3) |
| `annotator` | `/annotator` | 5177 | **Annotate** | PixiJS/WebGPU canvas over Arrow-backed rows, plus a `/browse` corpus surface; **4 `+server.ts` routes** (was 9) — the Arrow annotations transport, the Arrow annotation-IMPORT proxy (`api/tasks/[task_id]/import`, `requireSession`), `capi/v1/me`, the viewer catch-all; every JSON value surface rides one of 6 `.remote.ts` modules |
| `compute` | `/compute` | 5175 | Compute | Ray/Serve observability, 10 pages + a dock at `/compute/workbench` |
| `train` | `/train` | 5178 | Train | **Placeholder data only** — every page badges it |
| `studio` | `/studio` | 5176 | Studio | Mini-app launcher, one tenant |

Nav labels name the JOB, not the directory — but they agree with it wherever they can: `explorer` was relabelled from Search once the directory itself was renamed (`nav-config.ts:480-491`). `annotator` → **Annotate** is the one deliberate split left.

## The nine packages

Only `@rask/ui` has a build (`svelte-package` → `dist/`); the rest are consumed JIT as raw TS.

| package | what it is |
|---|---|
| `@rask/ui` | Design system + `@rask/ui/shell`. → **`rask-styling`** |
| `@rask/api` | Gateway client (`ray`, `ingest`, `projects`, `me`) **plus** the OIDC/BFF plane (`bff.ts`, `oidc.ts`), the lineage client, and `@rask/api/dock-layout` + `dock-views` |
| `@rask/dockview` | Svelte 5 binding over **dockview 7** — the docked workbenches. → **§ Workbenches** |
| `@rask/flow` | Generic Svelte Flow binding: `GraphCanvas`, `StaticFlow`, `FlowAutoFit`, `depths`/`layout`. **Mechanism only — domain graphs (LineageGraph, FGA) stay in their zones** |
| `@rask/explorer-api` | Arrow-backed explorer/viewer client (media bytes, Arrow batches) |
| `@rask/engine` | Framework-agnostic PixiJS/WebGPU annotation canvas (ra-anno lineage) |
| `@rask/labeling` | The `LabelOp` model + annotator Arrow-IPC transport |
| `@rask/zone-contract` | **Test-only** — 16 files / 853 tests gating the estate's shape (2 are RED at HEAD — see § Gates) |
| `@rask/config` | One shared `tsconfig.base.json`, extended by 8 of the 16 workspace packages — six of which inherit its weaker settings unchanged |

**A `frontend/packages/*` entry is a LIBRARY, never a domain slice.** A zone's panels, stores and
graphs are the zone — moving them into a shared package hollows the zone, couples releases, and
cuts them off from their live stores and per-app remote functions (tried once, reversed:
`docs/architecture/global-workbench.md`). Cross-zone composition was tried twice and
retired twice — as a shared package (`@rask/panels`, hollowed the zones) and as runtime custom
elements (the global workbench, starved the panels of remote functions and `$app`). Extract the
*mechanism* into a library (`@rask/dockview`, `@rask/flow`); keep the *domain*, its data and its
components in the zone that owns them.

## Workbenches — `@rask/dockview`

**A dock lives INSIDE its zone, at ZONE level — `/<zone>/workbench`** (the record is
`docs/architecture/global-workbench.md`). THREE ship, each composing that zone's OWN components over
its own stores and remotes:

| dock | panels | what they share |
|---|---|---|
| `/explorer/workbench` | results · atlas · player | ONE `Bench` search store via `createContext` — a hit picked in results is the hit the atlas highlights and the player loads |
| `/lakehouse/workbench` | lineage graph · runs · events · tables · storage | ONE `LineageState`, polled once, so the DAG and the run board can never be a poll apart; Tables/Storage are the very `TableRegistry` / `ObjectBrowser` the catalog pages render |
| `/compute/workbench` | jobs · cluster · actors · serve | the zone's own `getRayJobs`/`getRayCluster`/`getActors`/`getServe` remotes on the zone's own poll clock |

**ZONE level is load-bearing.** The lakehouse's briefly sat at `/lakehouse/lineage/workbench`, which
buried a zone surface inside one AREA and hid it from anyone standing in catalog or models — it was
reverted for exactly that. `dock-reachability.test.ts` pins the set EXACTLY, so a nested path is
simply not in the list. A dock is still EARNED by a real multi-panel workflow, never granted by
symmetry: train and studio carry placeholder data, home is the catch-all, the annotator is already
a canvas.

**A PANEL RENDERS THE PAGE'S COMPONENT. It is not a smaller re-implementation.**

This is the rule the estate has broken twice, in opposite directions, and the second time from inside
a zone where nothing was watching. The compositor was retired because an element *could not* import a
page component and every panel had to be mirrored — then the in-zone panels were hand-written anyway:
`/compute/actors` was 416 lines with sorting, filters and search; `ActorsPanel` was 49 lines, four
columns, no controls, sharing ZERO components. They drifted immediately. Fixed 2026-08-04.

The shape to copy:

| | |
|---|---|
| the view lives in | `$lib/boards/<X>Board.svelte` (compute) or beside its domain (`$lib/lineage/RunsBoard.svelte`) |
| the route is | `<svelte:head>` + `<XBoard />` — nine lines |
| the panel is | `<XBoard />`, plus a box if it needs one |

**When the panel's DATA differs, pass rows as a PROP — do not give the board its own fetch.** The
lakehouse run board is the case: the route reads `listRuns` on the lineage cursor, the dock's panel
reads the same rows out of the shared `LineageState` that also feeds the graph and the event feed. One
poll for three panels is the property that earns an in-zone dock; a self-fetching board would have
re-mirrored the view and broken it in one move. So the board owns PRESENTATION (sort, filter,
pagination, drill-in) and the caller owns where the rows came from.

A panel with no page counterpart is fine and is NOT a mirror — `EventsPanel` renders
`LineageState.events`, which no route renders (`/admin/events` is the *governance* feed, a different
plane). Do not invent a pairing to satisfy the rule.

**Adding a dock to a zone? Four things fail QUIETLY if you skip them** (all four were skipped once,
each surfacing differently): declare `@rask/dockview` in that zone's `package.json` (bun hoisting
hides an undeclared import until the clean container build); `@import '@rask/dockview/styles.css'
layer(base)` in its `app.css` (else the grid never lays out and panels stack as bare text); give the
zone a session handle if its user-state is OIDC-gated (a bearer-less write 401s and the layout
silently never persists); and make sure the dock's parent is a SIZED flex item — `flex: 1 1 0`
against a block parent mounts every group at ZERO height, which reads as "the dock is broken" rather
than "the dock has no height".

The cross-zone compositor ZONE that briefly existed is DELETED, and with it the whole custom-element
machinery (per-zone `src/lib/elements/**`, `vite.elements.config.ts`, element budgets, the
`rask:select` contract). Its one capability — mixing panels from different zones — was a workflow
nobody had, and it cost fidelity structurally: an element cannot import a remote function
(endpoints are per-app) or a `$app`-bound component, so every panel had to be MIRRORED.
`dock-reachability.test.ts` pins the docks the estate ships (sidebar row AND navbar row), so a dock
can never be unnavigable and a compositor cannot return unnoticed.

**A dock's persistence goes through its zone's remote functions.** The `capi/v1/user-state`
proxies died in the transport convergence, so `makeDockLayoutStore`/`makeDockViewsStore` take a
`fetcher` shim (`$lib/dock/user-state-fetch.ts`) that maps their two calls onto the zone's
`readUserStateDoc`/`writeUserStateDoc` and answers with a real `Response` — the store's three
outcomes (`ok` / `absent` / **`unreadable`**) survive because a status stays a status.

It is a **thin binding, not a wrapper**: consumers hold the real `DockviewApi` and call its
documented methods.

Depend on **`dockview`, never `dockview-core`.** Their *type* entrypoints are identical
(`export * from 'dockview-core'`), which makes core look like the leaner honest choice — it is not.
`dockview`'s runtime entry is a 37 KB layer that `registerModules(...)` for **ContextMenu,
KeyboardDocking, AdvancedDnD, TabGroupChips and Accessibility**. Import core and all five are
silently absent, including the aria-live announcements; the library logs the mistake once, at
runtime, where nothing fails.

**Four invariants.** Break any and the dock is wrong:

1. **Panels mount once.** `SveltePanelRenderer` calls Svelte's `mount()` in `init()` and `unmount()`
   only in `dispose()`. Verified in dockview's shipped bundle: a panel's renderer is built once in the
   `DockviewPanelModel` constructor and disposed only from `_doRemovePanel` when `skipDispose` is
   falsy — every *move* path re-parents the same instance. So a running interval, an `@xyflow/svelte`
   viewport and an open subscription all survive a drag. Anything that mounts outside `init()` breaks it.
2. **`defaultRenderer: 'always'`.** Component state survives a move under either renderer, but
   **DOM-held** state (scrollTop, focus, `<video>` position) does not — the default `onlyWhenVisible`
   *removes the element from the document*. Measured: a list scrolled to 260 px returned at 0 while the
   panel's own counter ticked straight through. `'always'` parks panels in the overlay container instead.
3. **Layout is per-subject, not localStorage.** One `@rask/api/dock-layout` store for the estate, over
   the catalog's `dock-layout` user-state document on the Dapr state store. Three outcomes —
   `ok` / `absent` / **`unreadable`** — and `unreadable` must refuse to save. Treat it as empty and the
   next autosave overwrites a workspace that is still there.
4. **The dock is dynamically imported.** ~100 KB gzipped in-bundle, on one route of ~11–25. It must
   land in `deferredGzipKB`, never the entry graph. Its *stylesheet* is imported statically (10 KB, and
   deferring it buys a flash of unstyled dock).

Context crosses the mount boundary: `<Dock>` captures its own tree with `getAllContexts()` and hands
it to every panel mount, so a zone uses ordinary `createContext` above the dock and panels call the
getter. Layout is SSR-read via a remote `query()` and passed as `initial`, so the saved arrangement is
the first paint rather than a replacement for seeded defaults.

**Chrome shipped 2026-08 (the G1–G4 wave):** direction-split menus (`SplitMenu`, `split.ts`), the
"+" add-panel picker with search (`PanelPicker`, registries carry `label`/`icon`), whole-panel
watcher alerts (`alerts.svelte.ts` — bounded, released on dispose, surfaced via `PanelProps.alert`),
and **named views** (`DockViews` store + `ViewSidebar` — list/active/diverged as runes, persisted in
the catalog's `dock-layout-library` user-state document, a SEPARATE envelope because `DockLayouts`
is `extra="forbid"`). Dock chrome popovers use the native Popover API; scope `display` under
`:popover-open` or the author rule beats the UA's closed-state `display:none` **by origin** and the
closed popover eats clicks. No GSAP on dock chrome. Popout and floating groups remain unwired —
`dndStrategy: 'pointer'` (chosen for Linux reliability and Playwright testability) *disables
cross-window drag*, which is exactly what popout needs; resolve that trade before wiring it.

## Fetching data — remote functions are the direction, BFF only where payloads demand it

`experimental.remoteFunctions: true` + `compilerOptions.experimental.async` are set in **every** zone's `svelte.config.js`. **The standing rule (2026-08-03) is one transport per payload KIND:**

| Payload | Transport |
|---|---|
| Typed app **values** (configs, verdicts, registries, small lists) | remote functions — valibot at the boundary, `query.live` where a change signal exists, single-flight mutations |
| **Tabular / bulk / binary** (row batches, query results at scale, tiles) | Arrow IPC / raw bytes on `+server.ts`, streamed |

Both halves are idiomatic SvelteKit — `+server.ts` is the framework's own tool for non-HTML resources, not a legacy dialect. The reason bytes never ride remote functions is **measured, not categorical**: devalue (5.8.1, verified in-tree) *does* carry `ArrayBuffer`/TypedArrays — as base64 inside the payload string (`stringify.js:308`) — which costs +33% on the wire, triple-buffers the whole payload (bytes → base64 string → bytes, no streaming), and loses HTTP semantics (content-type, ETags, ranges). Same reason nobody serves images inside JSON. The rule cuts both ways: a JSON route carrying big tabular rows is the mirrored mistake — **promote it to Arrow**, don't convert it to a remote function (known candidate: lakehouse's table-detail row query returns JSON rows while the explorer reads the same class of data as Arrow). When you touch a JSON *value* surface still on `createBffClient`, converge it; do not add new BFF JSON routes. Also permanently `+server.ts`: the OIDC endpoints (redirect flows, not function calls).

**(a) Remote `query()`/`command()` — every zone's JSON value plane** (the the transport ruling convergence, landed 2026-08-03). A `.remote.ts` function runs on the zone server and reaches its upstream with the session bearer via `getRequestEvent()`. On every polled refresh, `.refresh().catch(() => {})` is **mandatory**: one uncaught rejection evicts the query from cache and silently kills the poll loop (`compute/src/lib/remote/compute.remote.ts:25-40`). The FGA workbench (`lakehouse/src/lib/admin/remote/access.remote.ts`) is the reference migration: queries + a write/delete-tuple `command()` pair with a single-flight `fetchStore().refresh()`, `ApiResult<T>` union returns on the dock-layout precedent (status-driven UI states, not exception flow), valibot parsing at the wire boundary, contracts kept in a sibling non-remote module (a `.remote.ts` may export only remote functions).

**(b) Same-origin BFF — permanently the binary/Arrow planes and the OIDC flow.** The JSON convergence is done in **six** zones: `requestJSON` has zero call sites in `home`, `explorer`, `annotator`, `compute`, `train`, `studio`. The **lakehouse is the residual** — four `requestJSON` call sites: the object browser's `listObjects`/`headObject` (`lib/storage/storage.ts:69,73`, over the `/api/explorer/**` seam), `fetchTableHistory` (`lib/data/catalog.ts:119`, the #113 commit log over the `capi/v1/table/[id]/[...rest]` proxy), and `insertRows` (`:126`, which POSTs an Arrow body and only reads a JSON ack — a keep-bytes route spelled with the JSON helper). Converge the first three when you touch them; do not add new BFF JSON routes. Elsewhere `createBffClient` survives only where the payload is bytes (Arrow, blobs, multipart).

**(c) Bell only — `studio`, `train`.** Page data is hardcoded arrays; the single `.remote.ts` each is `lib/live/feeds.remote.ts`, the estate-wide `query.live` bell.

**The media-plane BFF reads are AUTHORIZED now, not just proxied (2026-08-04).** `makeViewerProxy` — the `api/[...path]` catch-all that explorer and annotator both mount — carries `requireSession: true` (`packages/api/src/bff.ts:307-322`), so a bearer-less page-image or atlas read 401s at the BFF on an auth-enabled stack. The bearer path is sealed cookie → the zone's session handle → `makeBackendProxy` (`authorization: Bearer …`) → gateway → viewer, where `/api/page` checks `can_read_data` and `/api/pages` `can_get_metadata` on `table:<catalog id>`, and the S3 object routes behind `/api/explorer/**` check `can_browse_storage`. The lakehouse's `/api/explorer/[...rest]` forwards the caller's bearer but does **not** `requireSession`, so an empty `/lakehouse/catalog/storage` on a governed stack is an authz answer, not an outage.

Estate-wide: `command()` 71 across 21 remote modules (mutations single-flight their reads: `void query().refresh()` in the handler), `form()` 0, `query.batch()` 0, `{#await}` 0. `query.live` is the LIVENESS spine, not just the bell: every zone's `feeds.remote.ts` (the bell) and lakehouse's `controlEvents`/`controlCursor`/`jetstreamCursor`. (The explorer's service-health is NOT one — it is a single deduped poll in `lib/service-health.svelte.ts`, whose own comment rejects a cursor because liveness has no event.) Consume cursors through `$lib/live/tick.svelte.ts` (`liveRead` + `lineageTick`/`controlTick`) — it replaced thirteen hand-rolled `$effect`+`setInterval` pollers, and its rules (open on mount, cursor arrival is not a change) each exist because breaking them broke a test. Data mutations move the LINEAGE cursor; governance mutations (grants, warehouses, tenants — including raw `/v1/access/tuples` writes, which emit `grant_added`) move the CONTROL cursor.

### The SSR hairpin

Under `svelte-adapter-bun` a relative `/api/*` resolves against the **incoming external origin**, so a server-side fetch leaves the cluster and comes back. `makeGatewayHandleFetch` (`packages/api/src/gateway.ts:33-44`) rewrites `origin + '/api/'` → `gatewayBase + path` during SSR.

⚠️ The two wirings disagree on the env var. `compute/src/hooks.server.ts:11` reads **`RASK_GATEWAY_URL`**; `home`/`lakehouse` go through `makeZoneHooks(env, {gateway:true})`, which reads **`LANCE_GATEWAY_URL`** and defaults to `http://localhost:8001` (`bff.ts:241,267`) — the lineage port, not the gateway. Local dev sets only `RASK_GATEWAY_URL`. Treat a "works in `compute`, fails in `lakehouse`" SSR fetch as this.

## Composition — dev and prod share only the base path

**One local loop for UI: `make dev-frontends`** — Vite HMR, sub-second, `/api` mocked or proxied.
For anything only reproducible IN-CLUSTER (auth/OIDC, FGA, Dapr, the gateway's real routing) there is
no hot loop: build the zone image with `dagger call zone-image --zone=<zone> publish …` and roll it
out. That costs minutes, not seconds, and it is the deliberate trade — the in-cluster hot-reload
(Tilt) was removed 2026-08-04 because it was a second writer to the cluster and nobody was using it.

**Dev.** `make dev-frontends` builds `@rask/ui` + `@rask/api` first, then runs `turbo run dev --filter='./microfrontends/*'`. That filter is load-bearing: an unfiltered `turbo run dev` also starts `@rask/ui`'s `svelte-package -w`, which rewrites `dist/` while zones read it, and turbo tears the run down.

Turborepo 2.10.7 has a **built-in** microfrontends proxy. It reads `microfrontends/home/microfrontends.json` and binds `:3024`. `@vercel/microfrontends` is not installed and is not needed. Flow: `browser → :3024 → longest-prefix match → 127.0.0.1:517x (vite, strictPort) → SvelteKit with paths.base=/<zone>`. No path stripping.

> A second, hand-rolled proxy sits at `packages/zone-contract/src/proxy.ts` (`PROXY_PORT ?? 5200`). Its `dev:proxy` turbo task is invoked by no root script and no Makefile target, and its claim that `bun run dev` starts it is false. `:3024` is the live dev origin; `:5200` survives only because `explorer`'s e2e defaults to it.

**Prod.** One Ingress per release, rules specific-first: `/api` → `rask-gateway:8888`, `/<zone>` → `rask-web-<zone>:3000`, `/` → `rask-web-home:3000` last. `pathType: Prefix`, **no `rewrite-target`** — the pod receives `/compute/jobs` and `paths.base` consumes it. Images are tagged `web-<zone>:<tag>`.

This works only because of `patches/svelte-adapter-bun@1.0.1.patch`: upstream roots sirv at `client/<base>`, but SvelteKit already emits base-prefixed assets *inside* `build/client/`, so `/compute/_app/x.js` resolved to `client/compute/compute/_app/x.js` → 404. Probes are TCP, not httpGet, because a zone's `/` 404s under its base.

## The estate has TWO levels, and the navbar says which one you are on

Ruled 2026-08-03. `isMainMenu(pathname)` in `nav-config.ts` decides which bar renders, and the two
are different functions, not one filtered list:

| Level | Where | Bar |
|---|---|---|
| **estate** | `/`, `/projects`, `/settings` | `mainMenuNav()` → Home · Projects · Settings |
| **inside a project** | every zone route, and `/projects/<id>` | `topNav()` → Lakehouse · Compute ⟵gap⟶ Explorer · Annotate · Train · Studio |

The boundary that trips people: **`/projects` is the estate level, `/projects/<id>` is not** — opening
a project is what puts you inside one, so its page gets the zone bar. Scoping is by CONTEXT, never
URL: no zone's `paths.base`, ingress rule or `microfrontends.json` key encodes a project. (Rejected on
cost, not taste — prefixing zone paths with `/projects/<id>/` rewrites eight zones and ~50 links to
express what the switcher already carries. `projectFromHost` still parses a host-scoped project.)

- **`tier: 'primary'`** (Lakehouse, Compute) renders a visible **gap** before the rest — one spacer
  derived from the data, so re-tiering moves it. The skeleton must reserve that gap too: it did not
  at first, and the bar jumped 26px when `/v1/me` landed.
- **Settings is estate-admin only and ABSENT otherwise** — fail-closed on both surfaces. Its page gate
  is SERVER-side (`/settings/+page.server.ts`): 404 for a resolvable non-admin (do not advertise that
  estate config lives there), 403 for an UNRESOLVED identity (a broken lookup is not a missing page).
  Hiding a navbar entry is presentation, not authorization.
- The in-project bar is **identity-independent**: an admin earns panel COLUMNS (Lakehouse's
  Operations), never a top-level entry.
- **The shared panel follows its trigger.** Upstream shadcn-svelte pins the viewport at `left-0` of
  the bar; with eight entries that put a right-hand trigger's panel ~400px from its button, and
  crossing the gap dropped the hover. `navigation-menu.svelte` measures the open trigger and centres
  under it, clamped into the window.

## Cross-zone links

A link is cross-zone when `zoneOf(href) !== zoneOf(pathname)`. `zoneOf` is the first path segment — **except for `HOME_ROUTES` (`projects`, `settings`), which map to the home zone**, because `home` is the catch-all and its own routes would otherwise read as zones of their own (`/projects` looked like a `projects` zone, so the navbar's own link to it cost a document load from `/`). Cross-zone anchors carry **`data-sveltekit-reload`** — without it SvelteKit soft-navigates into a route the zone does not own and 404s. The shell applies this itself (`top-navbar.svelte`); `ZoneNavLeaf.reload` is the sidebar equivalent. `@rask/zone-contract`'s gate keeps its own copy of `HOME_ROUTES` (it cannot import the shell) and takes the OWNING zone from each component's path, so a lakehouse link into `/projects` without the attribute now fails the suite instead of 404-ing at runtime.

Hrefs are **flat and absolute** (`/lakehouse/catalog`, `/explorer/`, `/compute/`) — there is no project prefix. The project comes from the **request host**: `projectFromHost` maps `demo.localhost` → `demo` (`shell/breadcrumb.ts:5-8`).

**Trailing slashes on zone-root hrefs are load-bearing.** Each zone's `paths.base` serves the trailing form, so a bare `/compute` costs a 308 per hop (`packages/ui/tests/nav-config.test.ts:49-60`).

## Adding a zone — five places

Globbed membership means there is no list to append to, but five files must agree or the gates fail:

1. `microfrontends/home/microfrontends.json` — port + routing key
2. `svelte.config.js` — `paths.base`
3. `vite.config.ts` — port + `strictPort: true` (and the `dev` script must not also pass `--port`; that race made `annotator` drift onto another zone's port)
4. `chart/values.yaml` `frontend.apps`
5. `Makefile` `ZONES`

`manifest.test.ts` and `deploy-path.test.ts` pin all five. R15 is law: a zone missing from the shared navbar is a defect regardless of scaffold status.

(There was a sixth: a `budget.json` ceiling per zone, gated by `budget.test.ts`. Removed 2026-08-04 — see that commit for why.)

## TypeScript strictness is split

`strict` is on everywhere. `noUncheckedIndexedAccess` is on for the five rask-origin zones + `@rask/ui` + `@rask/api` (hand-inlined) and for `@rask/dockview` + `@rask/flow` (which extend the shared base and then re-inline it), and **off** for `annotator`, `explorer`, `engine`, `labeling`, `explorer-api`, `zone-contract` — those six extend `@rask/config/tsconfig.base.json` alone, and it sets neither flag. The shared base is weaker than the inlined copy, so the two lance-imported zones are the least strictly typed in the estate. That is a defect, not a design.

`exactOptionalPropertyTypes` is on only for `@rask/api`; it stays off on Svelte packages for a real upstream reason (Bits UI "union too complex"). Leave that one alone. Validation is **valibot**.

## Gates

ESLint and Prettier are **deleted**. `toolchain.test.ts` enforces three things about every workspace package:

- No `.prettierrc*` / `eslint.config.*` / `.oxlintrc.json` / `.oxfmtrc.json` inside a package — those configs live only at the frontend root.
- No script may match `/\b(eslint|prettier)\b/` — a package spawning a removed tool looks green while checking nothing.
- Every package **declares all three scripts verbatim**: `fmt: 'rsvelte-fmt .'`, `fmt:check: 'rsvelte-fmt --check .'`, and `lint: 'oxlint .'` — or `lint: 'oxlint --no-error-on-unmatched-pattern .'` for a package with no lintable file (`@rask/config` ships two JSON files, where plain `oxlint .` exits 1). The flag is **forbidden** where source exists, so it can never mask a zone whose paths stopped matching. Required, not optional: a package shipping *no* lint/fmt scripts leaves turbo nothing to run and sits silently outside the toolchain while every gate stays green — which is exactly what `@rask/config` did until 2026-07-25.

| Command | Runs |
|---|---|
| `make check` | `fmt` (mutating) + `lint` + Python `uvx ty` + `knip` |
| `bun --cwd=frontend run check test` | svelte-check + the vitest suites (zone-contract alone is 853, across 16 files) |
| CI (`.dagger/frontend.go:53`) | `bunx turbo run check check:tsgo test lint fmt:check` |

⚠ **The zone-contract suite is RED at HEAD — 2 of 853.** `bff-routes.test.ts:181` asserts *"the annotator has no search route; drop SEARCH_API"*, but the annotator's `select/remote/similar.remote.ts` now calls `SEARCH_API` for its k-NN "more like this" — the gate and the chart's per-zone upstream list are what need updating, not the code. `nav-truth.test.ts:96`'s own scanner guard (`ALL.length > 30`) sits at exactly 30 after a nav leaf moved. Fix both before adding to either file: a red gate hides the next regression.

**There are two separate e2e layers — `make e2e` is not the frontend one.**

| Layer | What | How it runs |
|---|---|---|
| Per-zone Playwright | `home`, `lakehouse`, `explorer`, `annotator` each ship `e2e/` + `"test:e2e": "playwright test"`. **Hermetic**, in TWO stand-in styles now: a read the BROWSER makes is mocked with `page.route`; a read the ZONE SERVER makes (every remote function, and a route that builds its own Arrow body) cannot be — those get a **mock upstream**, a tiny seed/ledger Bun server started as a second `webServer` with the dev server's `*_API` pointed at it (`lakehouse/e2e/admin/mock-catalog.ts`, `explorer/e2e/mock-media-services.ts`). Ports are declared once per zone in `e2e/ports.ts` and gated for collisions by `@rask/zone-contract` | `bun --cwd=frontend run test:e2e`, and in CI as *"Playwright e2e — all zones"* with `--concurrency=1` (each zone spins a dev server + chromium; parallel first-compiles blow the startup window, and `lakehouse` runs **two** servers — auth-off and auth-on) |
| `tests/e2e` | A standalone Playwright project with its **own lockfile**, driving a **running deploy** | `make e2e` (`RASK_E2E_BASE_URL`, default `http://localhost`) |

So `make e2e` never touches the zone suites, and the zone suites never touch a real backend. `home`'s `auth.spec.ts` (4 tests) is the MAIN-MENU contract now — Home + Projects and no zone, no Settings, for an anonymous visitor. The cross-zone `data-sveltekit-reload` contract it used to carry moved to the lakehouse and explorer suites, which run INSIDE a zone where the bar exists; `@rask/zone-contract`'s `cross-zone-reload.test.ts` is the static half. A fresh worktree needs `bun install` first — `svelte-package` is not on `PATH` otherwise and `@rask/ui#build` fails with exit 127 before any test runs.

`make check` reaches **neither** svelte-check nor the frontend tests, so run the second row before declaring a change done. `knip` is the inverse — local only, absent from CI.

Lint is **oxlint** (`svelte/require-each-key: error`, `svelte/no-reactive-reassign: error`, via `@rsvelte/oxlint-plugin`); format is **oxfmt** with tabs, single quotes, `printWidth: 100`. The cross-zone link rule is a **vitest test**, not a lint rule — oxlint reads a `.svelte` `<script>` block, not its markup, so an anchor-attribute rule cannot live there.

Validate `.svelte` edits with the `svelte` MCP autofixer. The standing rule that gives it teeth: **a Svelte defect class found twice becomes an oxlint `error` or a zone-contract test.** Three warnings sit unfixed in `compute` today, which is what an unenforced convention looks like.

## Staying on-stack

Animation → **GSAP** via `{@attach}` (+ Lenis). Charts → **LayerChart**. Graph/canvas editors → **Svelte Flow** via `@rask/flow`. Components → `@rask/ui`. Validation → **valibot**. A dependency that duplicates the stack is a no; extend the stack instead.

## Where to go deeper

- `references/zone-map.md` — per-zone routes, endpoints, and libs; the nav/shell contract.
- `rask-styling` — tokens, `@source`, component authoring.
- `rask-services-fleet` — the `/api/*` gateway and the services these zones call.
- `rask-architecture` — workspace planes and membership.
- `docs/architecture/frontend-conventions.md` — the long-form canon. Its `@source` line (`:319`, `:347`) has four `../`; three is correct.
