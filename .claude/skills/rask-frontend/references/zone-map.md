# Zone map

Per-zone routes, data sources, and local libs. Ground truth as of `feat/lance-ns-merge`.

The composition manifest is `microfrontends/home/microfrontends.json`, owned by `home` as the default app. Home has no `routing` block, so anything not matching the six zone prefixes falls through to it.

## `home` — catch-all, base `''`, port 5273

| Route | What |
|---|---|
| `/` | Project gallery. Cards link cross-zone to `/lakehouse/data/projects/<p>` with `data-sveltekit-reload` |
| `/[project]` | 307 → `/${params.project}/overview` |
| `/auth/login` | OIDC PKCE start → Dex authorize; no-op `redirect(302,'/')` when unconfigured |
| `/auth/callback` | CSRF-state check, code exchange, seals `SESSION_COOKIE` at `path:'/'` — origin-wide, so the session is cross-zone |
| `/auth/logout` | GET + POST, deletes the cookie at `path:'/'` |
| `/capi/v1/projects` | GET-only bearer-forwarding pass-through to catalog via `makeCatalogProxy(env)` |

The only zone with both `hooks.server.ts` and `hooks.client.ts`, and the only user of `makeZoneHooks` (session hydration + gateway rewrite).

Data: `+layout.server.ts` awaits `fetchMe({catalogUrl: CATALOG_API})` — `env.CATALOG_API ?? 'http://localhost:2333'`, **direct to catalog, not through the gateway**. `+page.server.ts` fetches its own `/capi/v1/projects` only for estate admins; otherwise the gallery derives from `me.projects`.

`lib/nav.ts` declares **one** leaf, so `AppShell` renders no sidebar (`hasNav = leaves.length > 1`) — deliberate. `lib/remote/home.remote.ts`'s `getProjects` is **dead code**; `knip.json:13` ignores `src/**/*.remote.ts`, so the gate cannot see it.

`home/e2e/auth.spec.ts` is the cross-zone contract test and the most current source of truth on the estate: `ZONE_ENTRIES` maps all seven dirs → navbar title/kind, `:52` asserts the key set equals `zoneDirs()`, and `:71-77` asserts every non-home zone link carries `data-sveltekit-reload`. Its `networkidle` wait was replaced with a click-retry `toPass` because **every zone now holds a live query open for the bell**, so networkidle can never fire.

## `lakehouse` — base `/lakehouse`, port 5174

Five areas that used to be five separate apps, merged under one router: `data`, `lineage`, `models`, `admin`, `storage`. Area resolution is path-segment-based, so a hop between areas is a soft nav. 84 route files; `+layout.ts` sets `ssr = true, prerender = false` zone-wide.

`+layout.svelte` is the control tower: `me` fetched **on mount** via `fetchMeViaBff()`, one `lineageFeed()` live query opened in `onMount`, a client-side estate-admin gate that renders `ForbiddenPage` and nulls `zoneNav` so admin routes are not advertised, `canvasArea = area === 'lineage'` to give SvelteFlow a sized flex parent, and `onNavigate → document.startViewTransition`.

**`data`** — table registry, table detail (`TableDetail.svelte`, 1711 lines, four tabs derived from `?tab=`), namespaces (derived from `<ns>$<table>` ids — there is no list endpoint), projects, warehouses. The **warehouse + project** registry (list/describe/create/activate/bind) rides `lib/data/remote/warehouses.remote.ts`; its three `/capi/v1/warehouses*` routes are gone. The **table lifecycle** rides `lib/data/remote/catalog.remote.ts` (the transport ruling area 1): the registry list, the `fetchTableDetail` aggregate (still a six-read server-side fan-out — just no longer a route), policy/GC/compaction, tags/branches/restore, schema evolution, indexes, drop/deregister/rename/declare and the row update/delete; 17 `/capi/v1/table*` routes are gone with it, allowlist segments and all. Tabular reads (table preview/query, insert) stay Arrow on `+server.ts`, as does the `[id]/[...rest]` GET proxy that serves blob `<img>` bytes and the #113 commit log.

**`lineage`** — Svelte Flow DAG explorer (depth layout with longest-path + iteration cap, theme-live via `useColorMode()`), datasets, dataset detail (governance, grants, read audit, upstream/downstream), jobs, job detail (a **two-read split**: 200 events with `summary:true` ≈46 kB, plus one raw event for facets), runs, column-level lineage.

**`storage`** and **`models`** are areas here — they are not zones. `/data` itself is a scaffold stub. The model registry (list/describe/**promote**) rides `lib/models/remote/models.remote.ts` — the `capi/v1/model/[model]/promote` route is gone; `/pipeline`'s medallion triggers ride `lib/models/remote/medallion.remote.ts` (two `command()`s with the idempotency key as an arg; the `medallion/[action]` route is gone).

8 `+server.ts` routes (4 keep-bytes, 1 keep-flow, 2 catch-alls — the custom-element blocker — plus the thin `/api/audit` shim whose logic lives in `lib/server/audit-core.ts`, shared with `admin/remote/audit.remote.ts`), 0 `requestJSON` calls, 15 `.remote.ts` modules. All gates green: svelte-check 0 errors, oxlint 0 errors.

## `explorer` — base `/explorer`, port 5173, labelled **Explorer**

Multimodal corpus search: FTS / vector / hybrid / voice search, a WebGPU embedding atlas, a Cypher knowledge-graph explorer, and a Svelte-Flow dataflow editor — plus a dock at `/explorer/workbench` (results + atlas + player over one shared search store, this zone own components). Reaches `:8101`/`:8102`/`:8103` (plus the catalog and lineage) through its own BFF rather than the gateway.

**6 `+server.ts` routes** after the transport ruling area 3, down from 13 — 4 keep-bytes (`api/atlas/points` Arrow + its zone cache, `api/voice/similar` multipart-in, the `api/[...path]` viewer catch-all, `diagram` SSR'd SVG/HTML), plus the 2 **promote-arrow** routes that keep their `+server.ts` because their REQUEST shape pins them there: `api/search` (the POST carries a File) and `api/atlas/chunks` (a read spelled as a POST). Both now answer **Arrow IPC** built by `$lib/server/rows-arrow.ts` — a corpus row is a loose object, so the encoder derives the schema per response and names the JSON-carried columns in the schema metadata; `@rask/explorer-api`'s `rowsFromArrow` decodes them and OMITS null cells (`RowSchema`'s optional fields admit `undefined`, never `null`).

Four `.remote.ts` modules carry every JSON value surface: `lib/catalog/remote/catalog.remote.ts` (identity + the two user-state documents), `lib/projects/remote/projects.remote.ts` (the SEND half of the annotation funnel), `lib/graph/remote/graph.remote.ts` (the Cypher console — a `command()`, because a `query` is cached per argument and Run must re-run), `lib/workflow/remote/labeling.remote.ts` (the tag write + batch-job submit), beside the pre-existing `lib/live/feeds.remote.ts`. Seven routes are gone: `capi/v1/me`, `capi/v1/user-state/[document]`, `api/graph/cypher`, `api/annotations/tags`, `api/jobs/apply`, `api/projects/[...path]`, and `api/jobs/[...path]` — the last one collapsed rather than ported, its only intended caller (`jobStatus` in `@rask/labeling`) being exported dead code with zero call sites in the estate.

Its e2e suite runs a mock UPSTREAM (`e2e/mock-media-services.ts`, one Bun server standing in for the search service and the annotator's projects plane) beside the browser-side `page.route` mocks, because a read that moved to the zone server cannot be intercepted in the page. Single-worker on purpose: that mock's seed/ledger is global, since an auth-off suite has no bearer to key it by.

Fourteen files import from the **root barrel** `'@rask/ui'` rather than a subpath — migration residue confined to this zone. `explorer` and `annotator` are also the two zones that add explicit `@source './lib' './routes' './app.html'` to `app.css`.

## `annotator` — base `/annotator`, port 5177, labelled **Annotate**

One page: a PixiJS/WebGPU annotation canvas over Arrow-backed rows, built on `@rask/engine` (drawing tools, editors, a hand-written `cornerMinEigenVal` reimplementation) and `@rask/labeling` (the `LabelOp` model, Arrow-IPC transport, optimistic concurrency via `X-Annotations-Version`).

**3 `+server.ts` routes** after the transport ruling area 4, down from 9 — the Arrow annotations transport (`api/annotations/[...path]`, byte-identical), `capi/v1/me` (keep-flow), and the `api/[...path]` viewer catch-all (image bytes). Six routes are gone; their surfaces ride 6 `.remote.ts` modules (`projects/remote/projects.remote.ts` + `tasks.remote.ts` — the RECEIVE half of the annotation funnel: claim/release/submit leases — `viewer/remote/{jobs,assist,config}.remote.ts`, `live/feeds.remote.ts`), which bind the shared `@rask/labeling` functions server-side rather than duplicating them. Its e2e suite runs `e2e/mock-annotator.ts` as the mock upstream on the lakehouse seed/ledger pattern, plus a `warmup.setup.ts` that pre-compiles the heavy routes on both app servers.

It renders `AppShell` with **`canvas={true}`** — drops sidebar and breadcrumb, keeps the header, gives children full height. That variant exists precisely because the annotator had forked its own header and drifted: "a missing variant is why a zone forks the shell; adding the variant is the fix" (`app-shell.svelte:76-81`).

## `compute` — base `/compute`, port 5175

~2960 lines across 9 routes: `/` (overview), `/jobs`, `/jobs/[id]`, `/actors`, `/cluster`, `/serve`, `/logviewer`, `/api-docs` (iframes `/api/docs`), `/new` (`ingestIIIFVolume()` → `POST /api/ingest-iiif` with `Idempotency-Key: ui-<volumeId>`).

The reference implementation of dialect (a): 10 queries in `lib/remote/compute.remote.ts`, 3 param-keyed, polled at 5 s via `.refresh().catch(()=>{})` and read imperatively through `.current`.

Endpoints (`packages/api/src/ray.ts`): `/api/ray/{health,jobs,cluster,actors,tasks,overview}`, `/api/ray/jobs/{id}/logs?tail=`, `/api/ray/logs?node_id=`, `/api/serve/applications/`.

## `train` — base `/train`, port 5178

**100% hardcoded arrays.** Every page carries a "Placeholder data" badge, and there is no network I/O beyond the notification bell. Despite the gateway defining a `/api/train` row, **nothing in the frontend calls it** — a grep for `api/train` across `frontend/` returns zero hits. Treat this zone as a scaffold awaiting a backend.

## `studio` — base `/studio`, port 5176

Mini-app launcher with exactly one tenant: a GSAP vs `svelte/transition` A/B.

## The nav contract

`topNav(estateAdmin)` (`nav-config.ts:255-347`) returns seven entries, order pinned by `tests/nav-config.test.ts:14-35`.

| title | href | panel |
|---|---|---|
| Home | `/` | plain link |
| Lakehouse | `/lakehouse/data` | groups: Catalog, Models, Lineage (+ Governance, Operations for admins) |
| Search | `/explorer/` | items: Search, Atlas, Tree, Graph, Workflow |
| Annotate | `/annotator/` | plain link |
| Compute | `/compute/` | items: Overview, Jobs, Cluster, Actors, Serve, Logs, API docs |
| Train | `/train/` | plain link |
| Studio | `/studio/` | plain link |

Two-level IA: the **top navbar is cross-zone** (one entry per zone; a zone with sub-areas becomes a `NavigationMenu` trigger + panel), the **sidebar is in-zone only** and never lists other zones. The admin columns append only for `me.estate_admin` and fail closed — `topNav(false)` when `me` is null; `tests/nav-config.test.ts:42-67` asserts both polarities and that `/lakehouse/admin/access` never appears in a non-admin's href set.

Matchers exported for zones to build configs: `norm` (drops one trailing slash), `seg` (prefix-segment), `exact` (root-leaf, so `/lakehouse/models` does not stay lit on `/models/pipeline`), `under(...prefixes)`.

`prefetchDocument(href)` / `prefetchOnIntent(href)` inject `<link rel="prefetch">` on pointerenter/focus, once per href, SSR no-op — honoured by Chromium and Firefox, not Safari. Implemented as a native `addEventListener` inside an `{@attach}` so Bits UI's own pointer handlers on the node survive.

Notifications: `NotificationFeed { runs, seen?, dismissed?, onseen?, ondismiss?, allHref? }`. `null` renders **no bell**, not an empty one. Identity is `run_id@STATE`, so a run read at START re-rings on COMPLETE/FAIL while progress ticks inside RUNNING stay quiet. `visibleRuns` sorts **failures first, then newest** — across 891 real runs the first FAIL sat at position 445, so a newest-8 panel never showed a failure.
