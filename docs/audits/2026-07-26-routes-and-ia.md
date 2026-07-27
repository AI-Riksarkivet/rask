# Route audit — lance-ns frontend (branch `main`, HEAD `f9fe691`)

Read-only audit. Every claim below is anchored to `path:line` and to a command that was actually run.
Paths are relative to `/home/blackwell/Desktop/lance-ns` unless absolute.

**Method / scope notes**
- Route enumeration: `find <zone>/src/routes -type f` per zone (four zones, complete file listing, no
  filtering) — so the inventory is the filesystem, not a reading of the nav config.
- Zone base paths read from each zone's `svelte.config.js` (`paths.base`), and the edge routing from
  `chart/values.yaml:561-573` + `chart/templates/ingress.yaml:31-72`.
- "reachable-from" for pages = a literal or template-built `href=` / `goto()` / nav-config entry in
  **product code** (`src/**`). Playwright `page.goto()` in `e2e/**` is explicitly **not** counted as
  reachability — a test can reach a URL the product cannot.

**Zone bases (verified)**
| zone | `paths.base` | evidence | edge rule |
|---|---|---|---|
| home | `''` (origin root, catch-all) | `frontend/components/frontends/home/svelte.config.js:10-11` (comment: "The DEFAULT app (home) owns '/'") | `chart/values.yaml:562` `{ name: home, catchAll: true }` → `ingress.yaml:62-71` `path: /` |
| lakehouse | `/lakehouse` | `frontend/components/frontends/lakehouse/svelte.config.js:12` | `chart/values.yaml:567` |
| media | `/media` | `frontend/components/frontends/media/svelte.config.js:26` | `chart/values.yaml:572` |
| annotator | `/annotator` | `frontend/components/frontends/annotator/svelte.config.js:19` | `chart/values.yaml:573` |

**Counts (command: `find <zone>/src/routes -name '+page.svelte' | wc -l` and same for `+server.ts`)**

| zone | pages | endpoints |
|---|---|---|
| home | 1 | 4 |
| lakehouse | 26 | 50 |
| media | 6 | 10 |
| annotator | 1 | 7 |
| **total** | **34** | **71** |

Plus one page-less route: `frontend/components/frontends/lakehouse/src/routes/+page.ts` — a
`redirect(307, ${base}/data)` with no `+page.svelte` (`:7`).

---

## 1. Route inventory

Kind: `page` = has `+page.svelte`; `endpoint` = `+server.ts`; `redirect` = `+page.ts` only.
`reachable-from` cites the inbound references found; the orphan sweep in §2 is the exhaustive pass.

### home (base `''`)

| zone | route | kind | reachable-from |
|---|---|---|---|
| home | `/` | page (`+page.svelte` + `+page.server.ts`) | `HOME_ZONE_NAV` leaf `frontend/components/frontends/home/src/lib/nav.ts:8`; shell error page default `frontend/packages/ui/src/lib/shell/app-error.svelte:10,23`; `forbidden-page.svelte:11,24`; project-switcher "Main menu" `project-switcher.svelte:21,65` |
| home | `/auth/login` | endpoint | `frontend/packages/ui/src/lib/shell/navbar-user.svelte:34,84`; `frontend/components/frontends/home/src/routes/+page.svelte:58`; 20 in-zone `loginHref` sites (e.g. `lakehouse/src/lib/data/TableDetail.svelte:72,923`) |
| home | `/auth/logout` | endpoint | `frontend/packages/ui/src/lib/shell/navbar-user.svelte:76` |
| home | `/auth/callback` | endpoint | OIDC `redirect_uri`, not a UI link: `chart/templates/dex.yaml:43` (`{{ publicOrigin }}/auth/callback`); comment at `dex.yaml:39` ("the home zone owns /auth/callback") |
| home | `/capi/v1/projects` | endpoint | `frontend/components/frontends/home/src/routes/+page.server.ts:39` `fetch('/capi/v1/projects')` |

### lakehouse (base `/lakehouse`) — pages

| zone | route | kind | reachable-from |
|---|---|---|---|
| lakehouse | `/lakehouse` | redirect (`+page.ts:7`) | breadcrumb crumb `lakehouse` → `/lakehouse` (`frontend/packages/ui/src/lib/shell/breadcrumb.ts:39-45`, rendered `app-shell.svelte:83,124`). No nav-config entry targets it |
| lakehouse | `/lakehouse/data` | page | **breadcrumb only on desktop** — `breadcrumb.ts:43` builds `/lakehouse/data` as the 2nd crumb of any `/lakehouse/data/*` path; narrow-viewport overflow row `top-navbar.svelte:94-105` ("Lakehouse home"). It is the `topNav` entry href (`nav-config.ts:236`) but that href renders as a `<button>` trigger, not a link, when `groups` is set (`top-navbar.svelte:173-175`) — see F-7 |
| lakehouse | `/lakehouse/data/projects` | page | `DATA_ZONE_NAV` leaf `lakehouse/src/lib/data/nav.ts:13`; `DATA_ITEMS` `packages/ui/src/lib/shell/nav-config.ts:92`; back-link `lakehouse/src/routes/data/projects/[project]/+page.svelte:53` |
| lakehouse | `/lakehouse/data/projects/[project]` | page | `lakehouse/src/routes/data/projects/+page.svelte:106`; `lakehouse/src/lib/data/WarehouseAdmin.svelte:373,402`; `routes/data/warehouses/[id]/+page.svelte:113` |
| lakehouse | `/lakehouse/data/tables` | page | `DATA_ZONE_NAV` leaf `data/nav.ts:19`; `DATA_ITEMS` `nav-config.ts:95`; `TableDetail.svelte:256` `goto(${base}/data/tables)` |
| lakehouse | `/lakehouse/data/tables/[table]` | page | `lib/data/TableRegistry.svelte:200,312,313`; `TableDetail.svelte:285` `goto(...)`; `lib/admin/DlqPanel.svelte:308`; `lib/admin/AuditViewer.svelte:170-172` (`resourceHref`); `routes/data/namespaces/[id]/+page.svelte:285` |
| lakehouse | `/lakehouse/data/namespaces` | page | `DATA_ZONE_NAV` leaf `data/nav.ts:25`; `DATA_ITEMS` `nav-config.ts:98`; `lib/admin/TenantsPanel.svelte:203`; `routes/data/namespaces/[id]/+page.svelte:249` |
| lakehouse | `/lakehouse/data/namespaces/[id]` | page | `lib/data/NamespaceRegistry.svelte:211,295`; `lib/data/TableRegistry.svelte:210,300`; `routes/data/warehouses/[id]/+page.svelte:136`; `AuditViewer.svelte:173-174` |
| lakehouse | `/lakehouse/data/warehouses` | page | `DATA_ZONE_NAV` leaf `data/nav.ts:31`; `DATA_ITEMS` `nav-config.ts:102`; `TenantsPanel.svelte:202,290`; `NamespaceRegistry.svelte:238`; `AuditViewer.svelte:175` |
| lakehouse | `/lakehouse/data/warehouses/[id]` | page | `lib/data/WarehouseAdmin.svelte:238,399`; `routes/data/projects/[project]/+page.svelte:92` |
| lakehouse | `/lakehouse/lineage` | page | `LINEAGE_ZONE_NAV` Graph leaf `lakehouse/src/lib/lineage/nav.ts:37`; `LINEAGE_ITEMS` `nav-config.ts:131` |
| lakehouse | `/lakehouse/lineage/datasets` | page | `LINEAGE_ZONE_NAV` `lineage/nav.ts:13`; `LINEAGE_ITEMS` `nav-config.ts:111`; `routes/lineage/+page.svelte:364`; `routes/lineage/columns/+page.svelte:85`; `routes/lineage/datasets/[name]/+page.svelte:89` |
| lakehouse | `/lakehouse/lineage/datasets/[name]` | page | `routes/lineage/datasets/+page.svelte:148,201` (`goto`); `routes/lineage/+page.svelte:285` (`goto(${base}/lineage/${kind}/…)`); `routes/lineage/jobs/+page.svelte:136`; `routes/lineage/jobs/[...job]/+page.svelte:79,87,120`; `routes/lineage/runs/+page.svelte:228` |
| lakehouse | `/lakehouse/lineage/jobs` | page | `LINEAGE_ZONE_NAV` `lineage/nav.ts:19`; `LINEAGE_ITEMS` `nav-config.ts:115`; `routes/lineage/jobs/[...job]/+page.svelte:68` |
| lakehouse | `/lakehouse/lineage/jobs/[...job]` | page | `routes/lineage/jobs/+page.svelte:56-57,127` (`jobHref`); `routes/lineage/runs/+page.svelte:64-65,174`; `routes/lineage/+page.svelte:285` |
| lakehouse | `/lakehouse/lineage/runs` | page | `LINEAGE_ZONE_NAV` `lineage/nav.ts:25`; `LINEAGE_ITEMS` `nav-config.ts:119` |
| lakehouse | `/lakehouse/lineage/columns` | page | `LINEAGE_ZONE_NAV` `lineage/nav.ts:31`; `LINEAGE_ITEMS` `nav-config.ts:123`; `routes/lineage/datasets/[name]/+page.svelte:92`; self `goto` `routes/lineage/columns/+page.svelte:31` |
| lakehouse | `/lakehouse/models` | page | `MODELS_ZONE_NAV` Registry leaf `lakehouse/src/lib/models/nav.ts:11`; `MODEL_ITEMS` `nav-config.ts:145` |
| lakehouse | `/lakehouse/models/pipeline` | page | `MODELS_ZONE_NAV` `models/nav.ts:17`; `MODEL_ITEMS` `nav-config.ts:152` |
| lakehouse | `/lakehouse/models/experiments` | page | `MODELS_ZONE_NAV` `models/nav.ts:23`; `MODEL_ITEMS` `nav-config.ts:147` |
| lakehouse | `/lakehouse/admin` | page | **breadcrumb only** (`breadcrumb.ts:43`) — no nav-config leaf, no `href=`, no `goto()`. See F-1 |
| lakehouse | `/lakehouse/admin/access` | page | `ADMIN_ZONE_NAV` `lakehouse/src/lib/admin/nav.ts:36`; `GOVERNANCE_ITEMS` `nav-config.ts:163` |
| lakehouse | `/lakehouse/admin/tenants` | page | `ADMIN_ZONE_NAV` `admin/nav.ts:11`; `GOVERNANCE_ITEMS` `nav-config.ts:168` |
| lakehouse | `/lakehouse/admin/audit` | page | `ADMIN_ZONE_NAV` `admin/nav.ts:17`; `GOVERNANCE_ITEMS` `nav-config.ts:173`; `TenantsPanel.svelte:278,285` |
| lakehouse | `/lakehouse/admin/streams` | page | `ADMIN_ZONE_NAV` `admin/nav.ts:23`; `OPERATIONS_ITEMS` `nav-config.ts:183` |
| lakehouse | `/lakehouse/admin/dlq` | page | `ADMIN_ZONE_NAV` `admin/nav.ts:27`; `OPERATIONS_ITEMS` `nav-config.ts:187`; `lib/admin/StreamConsumers.svelte:208` |
| lakehouse | `/lakehouse/admin/events` | page | `ADMIN_ZONE_NAV` `admin/nav.ts:31`; `OPERATIONS_ITEMS` `nav-config.ts:179` |

`/lakehouse/admin/*` additionally sits behind a server-side estate-admin gate:
`frontend/components/frontends/lakehouse/src/routes/admin/+layout.server.ts`.

### lakehouse — endpoints (50)

Two catch-alls back most of the surface: `capi/[...path]/+server.ts` (→ catalog) and
`api/[...path]/+server.ts` (→ lineage), per `frontend/packages/api/src/bff.ts:273-289`.
These are same-origin BFF routes called by the zone's own clients, never linked from the UI.

| route | kind | reachable-from |
|---|---|---|
| `/lakehouse/api/[...path]` | endpoint | lineage client `frontend/packages/api/src/lineage/client.ts` via `request('/api', …)`; `bff.ts:287-289` |
| `/lakehouse/api/projects` | endpoint | `'/api/projects'` literal (grep of api-path literals) |
| `/lakehouse/api/audit` | endpoint | `lib/admin/AuditViewer.svelte` |
| `/lakehouse/api/jetstream` | endpoint | `'/api/jetstream'` literal — `lib/admin/StreamsPanel.svelte` / `StreamConsumers.svelte` |
| `/lakehouse/api/experiments` | endpoint | `lib/models/Experiments.svelte` |
| `/lakehouse/api/datasets/[name]/description` | endpoint | dataset description write (lineage dataset detail) |
| `/lakehouse/api/datasets/[name]/tags/[tag]` | endpoint | dataset tag write (same) |
| `/lakehouse/api/admin/dlq/[run_id]/replay` | endpoint | `lib/admin/DlqPanel.svelte` replay action |
| `/lakehouse/capi/[...path]` | endpoint | `lib/{data,lineage,models}/catalog.ts:51` `request('/capi', path, …)`; `bff.ts:282` |
| `/lakehouse/capi/v1/me` | endpoint | `frontend/packages/api/src/client.ts:119` `fetch(bffPath('/capi/v1/me'))` |
| `/lakehouse/capi/v1/table` | endpoint | `lib/data/catalog.ts:110` `requestJSON<TablesList>('v1/table')` |
| `/lakehouse/capi/v1/table/[id]/detail` | endpoint | `lib/data/catalog.ts:114` |
| `/lakehouse/capi/v1/table/[id]/[...rest]` | endpoint | GET subpaths under a table id (`capi/v1/table/[id]/[...rest]/+server.ts:4`); e.g. `…/blobs` `lib/data/TableDetail.svelte:706` |
| `/lakehouse/capi/v1/table/[id]/query` | endpoint | `lib/data/catalog.ts:121` `requestBin('/capi', v1/table/${enc(table)}/query, …)` |
| `/lakehouse/capi/v1/table/[id]/{insert,update,delete,declare,rename,restore,drop,deregister}` | endpoints (8) | `lib/data/catalog.ts:171-313` write helpers, driven from `lib/data/TableDetail.svelte` |
| `/lakehouse/capi/v1/table/[id]/policy` | endpoint | `lib/data/catalog.ts:129` |
| `/lakehouse/capi/v1/table/[id]/maintenance/[action]` | endpoint | `lib/data/catalog.ts:144-162` (gc preview/run, compact) |
| `/lakehouse/capi/v1/table/[id]/index/create`, `…/index/[name]/drop` | endpoints (2) | `lib/data/catalog.ts:418-436` |
| `/lakehouse/capi/v1/table/[id]/columns/[op]` | endpoint | `lib/data/catalog.ts:471-504` |
| `/lakehouse/capi/v1/table/[id]/branches/[action]`, `…/tags`, `…/tags/[action]` | endpoints (3) | table-format ops from `TableDetail.svelte` |
| `/lakehouse/capi/v1/table/[id]/access/{check,grant,revoke,list,graph}` | endpoints (5) | `lib/data/catalog.ts:70-105` |
| `/lakehouse/capi/v1/namespace/[id]/access/{check,grant,revoke,list,graph}` | endpoints (5) | namespace grants panel (namespace detail page) |
| `/lakehouse/capi/v1/namespace/[id]/policy`, `…/policy/describe`, `…/drop` | endpoints (3) | `lib/data/catalog.ts:529` + namespace detail |
| `/lakehouse/capi/v1/access/{check,model,tuples}` | endpoints (3) | `lib/admin/AccessCheck.svelte`, `AccessModel.svelte`, `AccessTuples.svelte`; `catalog.ts:352` |
| `/lakehouse/capi/v1/model/[model]/promote` | endpoint | `lib/data/catalog.ts:61` |
| `/lakehouse/capi/v1/warehouses`, `…/warehouses/[id]/[action]` | endpoints (2) | `lib/data/catalog.ts:324,337,358,362` |
| `/lakehouse/medallion/[action]` | endpoint | `lib/models/PipelineControl.svelte` produce/train trigger |

### media (base `/media`)

| zone | route | kind | reachable-from |
|---|---|---|---|
| media | `/media` | page | `MEDIA_ZONE_NAV` Search leaf `media/src/lib/nav.ts:13`; `MEDIA_ITEMS` `nav-config.ts:137`; `lib/components/hit-card.svelte:89` `goto(${base}/)`; `lib/components/player-pane.svelte:128`; `routes/graph/+page.svelte:362`; `routes/guide/+page.svelte:1925`; `lib/components/topic-results-panel.svelte:69` |
| media | `/media/atlas` | page | `MEDIA_ZONE_NAV` `media/src/lib/nav.ts:14`; `MEDIA_ITEMS` `nav-config.ts:138`; `routes/guide/+page.svelte:1994` |
| media | `/media/tree` | page | `MEDIA_ZONE_NAV` `media/src/lib/nav.ts:15`; `MEDIA_ITEMS` `nav-config.ts:139` |
| media | `/media/graph` | page | `MEDIA_ZONE_NAV` `media/src/lib/nav.ts:16`; `MEDIA_ITEMS` `nav-config.ts:140` |
| media | `/media/workflow` | page | `MEDIA_ZONE_NAV` `media/src/lib/nav.ts:17`; `MEDIA_ITEMS` `nav-config.ts:141` |
| media | `/media/guide` | page | `MEDIA_ZONE_NAV` `media/src/lib/nav.ts:25`; `lib/components/help-popover.svelte:70`. **Not** in `MEDIA_ITEMS` (`nav-config.ts:136-142`) — see F-9 |
| media | `/media/diagram` | endpoint | **no caller in `src/**`** — see F-2 |
| media | `/media/capi/v1/me` | endpoint | `frontend/packages/api/src/client.ts:119` |
| media | `/media/api/[...path]` | endpoint | media client (`${base}/api/media/…`, `/api/topics`, `/api/thumbnail/…`, `/api/documents`, `/api/columns`, `/api/diarization/…`, `/api/doc-transcript/…`) |
| media | `/media/api/search` | endpoint | `'/api/search'` literal |
| media | `/media/api/atlas/chunks` | endpoint | `'/api/atlas/chunks'` literal |
| media | `/media/api/graph/cypher` | endpoint | `'/api/graph/cypher'` — `routes/graph/+page.svelte` |
| media | `/media/api/annotations/tags` | endpoint | `'/api/annotations/tags'` literal |
| media | `/media/api/voice/similar` | endpoint | `'/api/voice/similar'` literal |
| media | `/media/api/jobs/apply` | endpoint | `'/api/jobs/apply'` literal |
| media | `/media/api/jobs/[...path]` | endpoint | `/api/jobs/${encodeURIComponent(…)}` |

### annotator (base `/annotator`)

| zone | route | kind | reachable-from |
|---|---|---|---|
| annotator | `/annotator` | page (`+page.svelte` + `+page.ts` `ssr=false`) | `topNav` Annotate entry `nav-config.ts:257`; `MEDIA_ZONE_NAV` Annotate leaf (`reload: true`) `media/src/lib/nav.ts:19-24`; `media/src/routes/+page.svelte:192` `location.assign(/annotator?keys=…)`; self `goto` `annotator/src/routes/+page.svelte:56,63` |
| annotator | `/annotator/capi/v1/me` | endpoint | `frontend/packages/api/src/client.ts:119` |
| annotator | `/annotator/api/config` | endpoint | `${base}/api/config` (annotator `$lib/http.ts`) |
| annotator | `/annotator/api/annotations/[...path]` | endpoint | `${base}/api/annotations/${key}${ds}` |
| annotator | `/annotator/api/assist/[...path]` | endpoint | `'/api/assist/'` literals (AI assist) |
| annotator | `/annotator/api/jobs/apply` | endpoint | `'/api/jobs/apply'` |
| annotator | `/annotator/api/jobs/[...path]` | endpoint | `/api/jobs/${encodeURIComponent(…)}` |
| annotator | `/annotator/api/[...path]` | endpoint | `${base}/api/media/${doc}${ds}`, `${base}/api/chunk-frame/${key}${ds}` |

The annotator has **no `ZoneNav`** — it mounts `TopNavbar` directly with its own canvas shell
(`annotator/src/routes/+layout.svelte:42-49`, comment at `:15-18`). That matches `nav-config.ts:251-258`,
which declares Annotate "a single surface, so a plain link rather than a panel". CONFORMS.

---

## 2. Orphan sweep + broken-link findings

Commands run:
`grep -rn "goto(" --include=*.svelte --include=*.ts components/frontends packages/*/src` and
`grep -rn "href=" --include=*.svelte components/frontends/*/src packages/ui/src`.

### Orphan pages (no inbound `href=` / `goto()` / nav-config entry anywhere in `src/**`)

**F-1 — `/lakehouse/admin` is an orphan AND a stale P0 scaffold. Verdict: BUG.**
`frontend/components/frontends/lakehouse/src/routes/admin/+page.svelte:1-8` is the whole file:

```
<h1 class="text-2xl font-semibold">Admin</h1>
<p …>The Admin zone (P0 scaffold). Routes move here from apps/web in P3.</p>
```

- No `ADMIN_ZONE_NAV` leaf points at it (`lakehouse/src/lib/admin/nav.ts:6-40` lists
  tenants/audit/streams/dlq/events/access only).
- No `href=` or `goto()` resolves to `/lakehouse/admin`. The only `admin`-rooted hrefs are
  `{base}/admin/dlq` (`StreamConsumers.svelte:208`) and `{base}/admin/audit?resource=…`
  (`TenantsPanel.svelte:278,285`).
- It **is** clickable via the `admin` breadcrumb crumb on any `/lakehouse/admin/*` page
  (`frontend/packages/ui/src/lib/shell/breadcrumb.ts:39-45` → rendered `app-shell.svelte:83,124`), so a
  user can land on it — and the copy names `apps/web`, a directory that no longer exists
  (`ls frontend/components` → `frontends` only). Shipping UI copy that names a deleted layout is a
  product defect, not merely dead code.

**F-2 — `/media/diagram` endpoint has no caller. Verdict: BUG (dead route).**
`frontend/components/frontends/media/src/routes/diagram/+server.ts` SSR-renders
`$lib/diagram/Flow.svelte` into a standalone HTML document (`:52`). Grep for `diagram` across
`components/frontends/*/src` and `packages/*/src` returns only: the endpoint itself,
`media/src/lib/diagram/Flow.svelte:4-5` (its own comment "Used by src/routes/diagram/+server.ts"), and
unrelated prose inside `media/src/routes/guide/+page.svelte`. The file claims the use case is
"docs / OG images / non-JS" but nothing in the repo fetches or links it.

### Broken links — the target page exists, but the href points at a path no zone serves

All five below use pre-merge (`/data`, `/lineage`) prefixes. After the 7→4 merge those prefixes belong to
nobody: `chart/templates/ingress.yaml:31-41` pins only `/lakehouse`, `/media`, `/annotator`, so `/data*`
and `/lineage*` fall through to the `/` catch-all (`ingress.yaml:62-71`) = the **home** zone, whose entire
route manifest is `/`, `/auth/*`, `/capi/v1/projects` (`find home/src/routes -type f`). Result: 404.

**F-3 — the home project gallery cards link to the dead `/data/projects/*`. Verdict: BUG.**
`frontend/components/frontends/home/src/routes/+page.svelte:25`:
```
<a href={`/data/projects/${p.project}`} data-sveltekit-reload class="group block">
```
with the comment on `:24` still reading "Cross-zone card into the data zone's project page". The live
route is `/lakehouse/data/projects/<project>`
(`lakehouse/src/routes/data/projects/[project]/+page.svelte`). This is the **primary call-to-action of
the signed-in landing page** — every card 404s.

**F-4 — the shared project switcher links to the dead `/data`. Verdict: BUG.**
`frontend/packages/ui/src/lib/shell/project-switcher.svelte:55`:
```
<a href="/data" data-sveltekit-reload class="flex w-full items-center gap-2 px-2 py-1.5">
```
It lives in `@repo/ui`, so it is broken in **all four zones** — `nav-config.ts:214-215` states the
switcher "sits at the head of the bar on every zone". Correct target: `/lakehouse/data`.

**F-5 — three "explorer / Lineage" links point at the dead `/lineage`. Verdict: BUG.**
- `lakehouse/src/lib/data/TableDetail.svelte:930` — `<a href="/lineage" data-sveltekit-reload>explorer</a>` (the "not a catalog-registered table" empty state)
- `lakehouse/src/lib/data/TableDetail.svelte:938` — same href in the `denied` empty state
- `lakehouse/src/lib/models/PipelineControl.svelte:86` — `<a href="/lineage" data-sveltekit-reload>Lineage</a>`

All three are now **same-zone** targets (`/lakehouse/lineage`), so besides 404-ing they also carry a
pointless `data-sveltekit-reload`. The correct form is `{base}/lineage`, which the same tree already uses
correctly elsewhere (e.g. `routes/lineage/columns/+page.svelte:85`).

**F-6 — the lineage dataset detail's "graph" link lands on the catalog scaffold, not the graph.
Verdict: BUG.**
`lakehouse/src/routes/lineage/datasets/[name]/+page.svelte:95`:
```
<a class="viewlink" href="{base}/"><Network size={12} /> graph</a>
```
`{base}` is `/lakehouse`, so this resolves to `/lakehouse/` → `+page.ts:7` `redirect(307, '/lakehouse/data')`
→ the data scaffold page (F-8). The intended target is `/lakehouse/lineage` (the DAG canvas,
`routes/lineage/+page.svelte`). Pre-merge, when the lineage zone's own base *was* `/lineage`, `{base}/`
was correct. Cross-check that this is a miss and not a convention: the sibling back-link six lines up
(`:89`) *was* updated to `{base}/lineage/datasets`.

### Design-intent deviations

**F-7 — the wide navbar provides no link to `/lakehouse/data` (the zone root), contradicting an explicit
in-code rule. Verdict: BUG.**
`frontend/packages/ui/src/lib/shell/top-navbar.svelte:215-218` states the rule for the `items` branch:
```
<!-- The zone root itself — a panel must not be the only way in, and the
     trigger is a button, not a link. Skipped when a row already IS the
     root (lineage's Graph, media's Search), so no href appears twice. -->
```
and prepends a root row accordingly (`:216-233`). The **`groups` branch** — the one Lakehouse uses
(`nav-config.ts:240` sets `groups`, never `items`) — has no such row: `top-navbar.svelte:168-208` renders
only `entry.groups[*].items`, and the `NavigationMenu.Trigger` at `:173-175` is a button with no `href`.
The narrow/collapsed path *does* honour the rule (`top-navbar.svelte:94-105` `overflowItems` prepends a
"Lakehouse home" row), so the two breakpoints disagree: on desktop, `/lakehouse` and `/lakehouse/data`
are reachable only by typing a URL or clicking a breadcrumb from a deeper page.

**F-8 — `/lakehouse/data` is a P0 scaffold, and it is the zone's landing target. Verdict: BUG.**
`lakehouse/src/routes/data/+page.svelte:1-8` in full:
```
<h1 class="text-2xl font-semibold">Data</h1>
<p …>The Data zone (P0 scaffold). Routes move here from apps/web in P3.</p>
```
It is the destination of (a) `+page.ts:7`'s `redirect(307, ${base}/data)` for bare `/lakehouse`,
(b) the `topNav` Lakehouse entry href `nav-config.ts:236`, (c) the narrow-bar "Lakehouse home" row
(`top-navbar.svelte:99-103`), and (d) F-6's mislinked "graph" affordance. So the canonical entry point of
the estate's main zone renders placeholder copy naming a deleted directory. `DATA_ZONE_NAV` has four
leaves and none of them is this page (`lib/data/nav.ts:10-35`), which is why it is invisible from inside
the sidebar.

**F-9 — `/media/guide` is in the sidebar but not in the cross-zone panel. Verdict:
DEVIATES-WITH-REASON.** `MEDIA_ZONE_NAV` carries Guide (`media/src/lib/nav.ts:25`) but `MEDIA_ITEMS`
(`nav-config.ts:136-142`) does not. `nav-config.ts:81-83` documents the reason — "Deliberately a SUBSET of
the zone's own sidebar (`ZoneNav`): this is the cross-zone jump list, not a mirror of in-zone
navigation" — and the page has two other inbound references (`lib/components/help-popover.svelte:70`).

### Pages with confirmed inbound references (not orphans)

33 of 34 pages have at least one inbound `href=`/`goto()`/nav entry — see §1. The single orphan is
`/lakehouse/admin` (F-1). Dynamic segments were credited from template-built hrefs, quoted:
- `[table]` — `TableRegistry.svelte:200` `` href={`${base}/data/tables/${encodeURIComponent(row.id)}`} ``
- `[id]` (namespace) — `NamespaceRegistry.svelte:211` `` href={`${base}/data/namespaces/${encodeURIComponent(row.ns)}`} ``
- `[id]` (warehouse) — `WarehouseAdmin.svelte:238` `` href={`${base}/data/warehouses/${encodeURIComponent(w.id)}`} ``
- `[project]` — `routes/data/projects/+page.svelte:106` `` href={`${base}/data/projects/${encodeURIComponent(p.project)}`} ``
- `[name]` (dataset) — `routes/lineage/datasets/+page.svelte:148` `` href={`${base}/lineage/datasets/${encodeURIComponent(row.name)}`} ``
- `[...job]` — `routes/lineage/jobs/+page.svelte:56-57` `` `${base}/lineage/jobs/${j.namespace ? `${encodeURIComponent(j.namespace)}/` : ''}${encodeURIComponent(j.name)}` ``

---

## 3. Cross-check against git history: nothing lost in the 7 → 4 zone merge

Merge commit: `bb099df` — "frontend: merge data+lineage+models+admin into one lakehouse zone"
(`git log --oneline -1 bb099df`). Single-parent commit, parent `5917b96`
(`git rev-list --parents -n 1 bb099df`), so `bb099df^` is the pre-merge tree.

**Zone count confirmed 7 → 4** (`git ls-tree -r --name-only <rev> -- frontend/components/frontends`,
zones = dirs holding `src/`):
- pre (`bb099df^`): `admin, annotator, data, home, lineage, media, models` — 7
- post (`HEAD`): `annotator, home, lakehouse, media` — 4

### Pages — counts on both sides and the set difference in both directions

Method: enumerate `+page.svelte` under `<zone>/src/routes` in each tree, map every pre-merge route to its
canonical post-merge URL `/<zone-dir> + route` → `/lakehouse/<area>/…` (the URL scheme the commit message
declares at `bb099df`: "URLs move rather than being redirected … `/data/tables` -> `/lakehouse/data/tables`"),
then diff the two URL sets.

| pre-merge zone | pages | routes |
|---|---|---|
| data | 9 | `/`, `/namespaces`, `/namespaces/[id]`, `/projects`, `/projects/[project]`, `/tables`, `/tables/[table]`, `/warehouses`, `/warehouses/[id]` |
| lineage | 7 | `/`, `/columns`, `/datasets`, `/datasets/[name]`, `/jobs`, `/jobs/[...job]`, `/runs` |
| models | 3 | `/`, `/experiments`, `/pipeline` |
| admin | 7 | `/`, `/access`, `/audit`, `/dlq`, `/events`, `/streams`, `/tenants` |
| **pre total** | **26** | |
| **post total (`lakehouse`)** | **26** | |

- **LOST (pre-merge page with no post-merge counterpart): NONE**
- **GAINED (post-merge page with no pre-merge origin): NONE**

Both set differences are empty. Verdict: **CONFORMS** — the page inventory is exactly preserved, including
the two placeholder roots (`data/` and `admin/`, which map to `/lakehouse/data` and `/lakehouse/admin`).

Note tying §2 to history: the two scaffold pages of F-1 and F-8 are **not** merge damage. Their pre-merge
content is byte-comparable placeholder copy —
`git show bb099df^:frontend/components/frontends/data/src/routes/+page.svelte` and
`…/admin/src/routes/+page.svelte` both already read "The Data/Admin zone (P0 scaffold). Routes move here
from apps/web in P3." They were introduced by `a6bec2b` ("P0 — restructure to rask-style micro-frontend
zones (coexists with apps/web)") and survived the merge untouched
(`git log --oneline -3 -- …/lakehouse/src/routes/data/+page.svelte …/data/src/routes/+page.svelte`).
The merge carried them faithfully; what is stale is that P3 happened (apps/web is gone) and the scaffolds
were never replaced.

### Endpoints — counts on both sides and the set difference in both directions

| side | `+server.ts` FILES | DISTINCT BFF paths |
|---|---|---|
| pre-merge (data 39 + admin 10 + models 6 + lineage 5) | 60 | 50 |
| post-merge (`lakehouse`) | 50 | 50 |

- **LOST BFF paths: NONE**
- **GAINED BFF paths: NONE**

The 60 → 50 file drop is entirely **de-duplication**, not loss: the same BFF path existed as a separate
copy in several zones (e.g. `capi/v1/me/+server.ts` existed in all four pre-merge zones — see the
`git ls-tree` listing: `admin/…/capi/v1/me`, `data/…/capi/v1/me`, `lineage/…/capi/v1/me`,
`models/…/capi/v1/me`), and the merged zone needs one. Verdict: **CONFORMS**.

### The two zones that were NOT merged

media and annotator were deliberately left separate (`bb099df` message: "Media and annotator stay
separate (annotator keeps 17MB of Pixi+OpenCV out of a searcher's bundle)"). Their endpoint sets did change
between `bb099df^` and `HEAD`, so for completeness:

| zone | pre | post | lost | gained |
|---|---|---|---|---|
| media | 12 | 10 | `/api/annotations/[...path]`, `/api/assist/[...path]` | none |
| annotator | 8 | 7 | `/api/search` | none |

All three were deleted by a **later, separate** commit, not by the merge:
`git log --oneline --diff-filter=D -1 -- <path>` → `920f127` "frontend: media and annotator did not share a
backend — they shared dead routes" for all three. Verdict: **DEVIATES-WITH-REASON** (the reason is named in
the deleting commit; page inventory unaffected).

### One thing the merge commit claims to have fixed, that it did not finish

`bb099df`'s own message says:

> Also real, and only visible at runtime: three admin components linked to the catalog with hardcoded
> absolute paths (/data/warehouses) plus data-sveltekit-reload -- correct when the catalog was another
> zone, a base-less 404 and a pointless document reload now.

That is precisely the bug class of F-3, F-4 and F-5 — and five more instances of it survive on `HEAD`
(`home/src/routes/+page.svelte:25`, `packages/ui/src/lib/shell/project-switcher.svelte:55`,
`lakehouse/src/lib/data/TableDetail.svelte:930,938`, `lakehouse/src/lib/models/PipelineControl.svelte:86`),
plus the `{base}/`-instead-of-`{base}/lineage` variant at
`lakehouse/src/routes/lineage/datasets/[name]/+page.svelte:95`. The sweep was done inside the merged zone's
`lib/admin/**` but not across `packages/ui/**`, the home zone, or `lib/data`/`lib/models`. Verdict: **BUG**
(incomplete fix, already recorded as F-3…F-6).

The merge message also names an ESLint rule as "the load-bearing piece here" that flags cross-zone links.
That rule no longer exists — ESLint was removed in `968c8b6` ("frontend: delete ESLint and Prettier, move to
oxlint + rsvelte"). The replacement lives at `frontend/packages/zone-contract/src/cross-zone-reload.ts`
with `cross-zone-reload.test.ts` beside it. That gate does **not** cover the surviving
instances — verified by running it, see section 5 / F-10.

---

## 4. IA comparison: our lakehouse zone vs the Lakekeeper console

### What I could and could not verify about their UI

**Verified** (read directly, not inferred):
- The console's route set, from the Vue file-based-routing directory
  `https://github.com/lakekeeper/console/tree/main/src/pages` and `…/src/pages/warehouse` and
  `…/src/pages/roles` (fetched July 2026).
- The tab sets of three pages, read out of the `.vue` sources on `raw.githubusercontent.com`:
  `src/pages/warehouse/[id].vue`, `src/pages/warehouse/[id].namespace.[nsid].vue`,
  `src/pages/warehouse/[id].namespace.[nsid].table.[tid].vue`.
- The entity hierarchy and its inheritance semantics, from `https://docs.lakekeeper.io/docs/nightly/concepts/`
  and the Cedar/OpenFGA authorization pages: Server → Project → Warehouse → Namespace → Table/View;
  "Permissions in higher up entities are inherited to their children"; roles are Project-level and nestable;
  "Lakekeeper is no Identity Provider".
- Product-level feature claims from `https://docs.lakekeeper.io/`: "Soft-delete with time-bounded undrop and
  drop-protection across tables, views and generic tables"; "rich statistics served straight from the catalog,
  with no object-storage scans".

**NOT verified** (stated so I am not implying more than I checked):
- I did **not** run the console. No screenshots were rendered or read; the README's five images
  (home, warehouse, branch table, view history, warehouse tasks) are referenced by filename only in the text
  I retrieved.
- I did **not** read `src/router/**`, so route *guards*, redirects, and whether every `pages/**` file is
  actually mounted are unverified — I am treating the `pages/**` tree as the route list, which is the
  `unplugin-vue-router` convention but not something I confirmed in their config.
- The top-level navigation chrome lives in `AppBar`/`NavigationBar`, which
  `src/layouts/default.vue` renders but does not define; those components live in the separate
  `@lakekeeper/console-components` package, which I did not read. So **their sidebar/navbar item list is
  unverified** — I compare route sets and page tab sets, not nav chrome.
- Whether their "datasets" namespace tab is the generic-table surface is my inference from the sibling
  `generic-table.[tid]` route; not confirmed.

### Their route set (verified)

| area | routes |
|---|---|
| catalog hierarchy | `/warehouse` (index), `/warehouse/[id]`, `/warehouse/[id]/namespace/[nsid]`, `…/table/[tid]`, `…/view/[vid]`, `…/generic-table/[tid]` |
| identity & authz | `/roles` (index), `/roles/[id]`, `/identities`, `/user-profile` |
| server / ops | `/server-settings`, `/bootstrap`, `/dependencies`, `/license`, `/server-offline` |
| session | `/login`, `/logout`, `/callback`, `/no-access`, `/notfound` |
| landing | `/` (index → `<Home />`) |

Page tab sets (verified from the `.vue` sources):
- warehouse detail: `namespaces`, `Details`, `Tasks` (conditional), `Statistics` (conditional), `permissions`; plus a `Maintenance` section
- namespace detail: `namespaces`, `tables`, `views`, `datasets`, `deleted`, `Permissions` (conditional)
- table detail: `details`, `preview`, `health`, `versioning`, `files`, `Permissions` (conditional), `tasks` (conditional)

### Ours (from §1, all line-cited there)

| area | routes |
|---|---|
| catalog hierarchy | `/lakehouse/data/projects`, `…/projects/[project]`, `…/warehouses`, `…/warehouses/[id]`, `…/namespaces`, `…/namespaces/[id]`, `…/tables`, `…/tables/[table]` |
| lineage | `/lakehouse/lineage` (DAG), `…/datasets`, `…/datasets/[name]`, `…/jobs`, `…/jobs/[...job]`, `…/runs`, `…/columns` |
| models | `/lakehouse/models`, `…/models/experiments`, `…/models/pipeline` |
| governance / ops | `/lakehouse/admin/access`, `…/tenants`, `…/audit`, `…/streams`, `…/dlq`, `…/events` |

Our table detail tabs: `TABS = ['overview', 'preview', 'access']`
(`lakehouse/src/lib/data/TableDetail.svelte:67`), with the overview tab stacking sections `Stats` (`:968`),
`Schema` (`:1005`), `Insert rows` (`:1141`), `Update / delete rows` (`:1158`), `Blob preview` (`:1231`),
`Indexes` (`:1265`), `Versions, branches & tags` (`:1326`), `Maintenance policy` (`:1483`),
`Garbage collection` (`:1560`), `Danger zone` (`:1629`).
Our namespace detail sections: `Tables`, `Maintenance policy`, `Access` + `Authorization graph`
(`routes/data/namespaces/[id]/+page.svelte:278,293,375,385`).
Our access workbench tabs: `TABS = ['Graph', 'Tuples', 'Check', 'Model']`
(`routes/admin/access/+page.svelte:16`).

### What they surface that we do not

1. **Soft-deleted objects as a first-class view.** Their namespace detail has a `deleted` tab
   (`[id].namespace.[nsid].vue`), backed by "Soft-delete with time-bounded undrop and drop-protection"
   (docs.lakekeeper.io landing). We have **no** deleted/undrop surface: grep for
   `undrop|soft.delete|deleted table` across `lakehouse/src/**/*.svelte` returns one unrelated hit
   (`TableDetail.svelte:181`, a row-delete toast). Our `restore` endpoint
   (`capi/v1/table/[id]/restore`) is Lance *version* restore, not object undrop.
2. **Roles as a managed object.** `/roles` + `/roles/[id]`. We have no roles page; our authz UI is
   tuple-level (`admin/access` Tuples/Graph/Check tabs) plus per-object grants panels. Their model puts a
   reusable, nestable Project-level role between the principal and the grant; ours does not surface one.
3. **Identities / user directory.** `/identities` and `/user-profile`. We have no user list and no profile
   page — identity is read-only chrome (`navbar-user.svelte`) plus the frozen `/v1/me`
   (`packages/ui/src/lib/shell/nav-config.ts:40-46`).
4. **Server-level configuration and bootstrap.** `/server-settings`, `/bootstrap`, `/dependencies`,
   `/license`. We have no settings surface at all (this is a known deferral in the task list: #112
   "FUTURE (user-deferred): a Settings surface").
5. **Per-object task/queue visibility.** `Tasks` tab on both warehouse and table detail. Ours is
   estate-global instead (`admin/streams`, `admin/dlq`, `admin/events`) — you cannot ask "what is queued
   *for this table*".
6. **`health` and `files` tabs on table detail.** A storage-file explorer and a health view. We surface
   `Stats` and `Indexes` but no file-level listing.
7. **Views and generic tables as distinct object types** (`view/[vid]`, `generic-table/[tid]`, plus
   `views`/`datasets` namespace tabs). Our catalog has one object type — table — plus models as
   catalog objects (`nav-config.ts:145` "models are catalog objects too"). We have no view surface.
8. **Nested namespaces.** Their namespace detail's first tab is `namespaces` — namespaces nest arbitrarily
   (docs/concepts). Our namespace detail lists `Tables` only
   (`routes/data/namespaces/[id]/+page.svelte:278`) and our namespace ids are flat medallion tiers.

### What we surface that they do not

1. **Data lineage as a whole area** — 7 routes: a DAG canvas, dataset/job/run lists with detail pages, and
   **column-level** lineage (`/lakehouse/lineage/columns`). Nothing in their `pages/**` tree corresponds;
   the closest is `versioning` on one table.
2. **A model registry with a promotion pipeline** — `/lakehouse/models`, `…/experiments`, `…/pipeline`, and
   a promote endpoint (`capi/v1/model/[model]/promote`). No analogue in their route set.
3. **An authorization *workbench*** — not just per-object permission editing, but a relationship Graph
   explorer, a raw Tuples browser, a live Check simulator, and the compiled Model as read-only source
   (`routes/admin/access/+page.svelte:16`). Their `PermissionManager` is a per-object tab; we additionally
   answer "why does this principal have this?" and "what would happen if…".
4. **A compliance audit trail** — `/lakehouse/admin/audit`, with resource-pivot jump links back into the
   catalog (`lib/admin/AuditViewer.svelte:170-177`). Not in their route set.
5. **Event-plane operations** — `admin/events` (live control-event feed), `admin/streams` (JetStream
   consumers + lag), `admin/dlq` (dead-lettered runs with replay). They emit change events to Nats/Kafka
   (docs landing) but the console does not show the queue.
6. **Row-level write surface on table detail** — `Insert rows` (`TableDetail.svelte:1141`) and
   `Update / delete rows` (`:1158`). Their table detail is read + admin, not a data-writing surface.
7. **Projects as a browsable tenancy area** with members — `/data/projects`, `/data/projects/[project]`
   (Warehouses + Admins sections, `routes/data/projects/[project]/+page.svelte:82,106`) and
   `/admin/tenants` (project × warehouse × bucket × status × admins,
   `lib/admin/TenantsPanel.svelte:114-143`). Their projects exist as an entity but have **no page** in
   `pages/**` — projects appear only as a scoping concept.
8. **A blob/media preview inside table detail** (`TableDetail.svelte:1231` `Blob preview`) — multimodal
   payloads rendered in the catalog UI.

### One concrete recommendation

**Make the namespace and warehouse detail pages the hierarchy's landing surfaces, and delete the two
scaffold pages — specifically: replace `/lakehouse/data/+page.svelte` with the projects list, and drop
`/lakehouse/admin/+page.svelte`.**

Reason, in their terms and ours: Lakekeeper's IA has **no dead middle**. Every level of
Project → Warehouse → Namespace → Table is a page whose first tab is *the next level down*
(`warehouse/[id].vue` first tab = `namespaces`; `[id].namespace.[nsid].vue` first tab = `namespaces`, then
`tables`), so drilling in never lands on a page that explains nothing. Ours has two dead middles at exactly
the points a first-time user hits first: `/lakehouse` redirects to `/lakehouse/data`
(`routes/+page.ts:7`), which is the P0 scaffold reading "Routes move here from apps/web in P3"
(`routes/data/+page.svelte:6`, F-8) — that is the *canonical entry point of our main zone* — and
`/lakehouse/admin` is a second copy of the same placeholder (`routes/admin/+page.svelte:6`, F-1), reachable
by clicking the `admin` breadcrumb. Turning `/lakehouse/data` into the projects list (the real top of our
hierarchy, already built at `routes/data/projects/+page.svelte`) fixes four separate defects at once: F-8
(scaffold landing), F-6's mislinked "graph" affordance which lands there, F-7's missing zone-root
destination becomes worth reaching, and the desktop-vs-narrow-navbar disagreement stops mattering. Deleting
`/lakehouse/admin` removes the only orphan page in the repo (F-1) and the last reference to the deleted
`apps/web` in shipped UI copy. Backward compatibility is explicitly not a constraint here, so both files
can simply be replaced/removed rather than redirected.

Sources for §4: [lakekeeper/console (GitHub)](https://github.com/lakekeeper/console) ·
[console src/pages](https://github.com/lakekeeper/console/tree/main/src/pages) ·
[console src/pages/warehouse](https://github.com/lakekeeper/console/tree/main/src/pages/warehouse) ·
[Lakekeeper docs — Concepts](https://docs.lakekeeper.io/docs/nightly/concepts/) ·
[Lakekeeper docs — Authorization (Cedar)](https://docs.lakekeeper.io/docs/0.12.x/authorization-cedar/) ·
[Lakekeeper docs landing](https://docs.lakekeeper.io/)

---

## Findings summary

| id | verdict | what |
|---|---|---|
| F-1 | BUG | `/lakehouse/admin` — the repo's only orphan page; P0 scaffold naming the deleted `apps/web`; reachable via breadcrumb |
| F-2 | BUG | `/media/diagram` endpoint has no caller anywhere in `src/**` |
| F-3 | BUG | home gallery cards → dead `/data/projects/*` (`home/src/routes/+page.svelte:25`) |
| F-4 | BUG | shared project switcher → dead `/data` (`packages/ui/src/lib/shell/project-switcher.svelte:55`) — broken in all four zones |
| F-5 | BUG | three links → dead `/lineage` (`TableDetail.svelte:930,938`; `PipelineControl.svelte:86`) |
| F-6 | BUG | lineage dataset detail "graph" link → `{base}/` → the data scaffold, not the DAG (`lineage/datasets/[name]/+page.svelte:95`) |
| F-7 | BUG | wide navbar has no link to the Lakehouse zone root; the `groups` branch omits the root row the `items` branch's own comment declares mandatory (`top-navbar.svelte:168-208` vs `:215-233`) |
| F-8 | BUG | `/lakehouse/data` is a P0 scaffold and it is the zone's landing target (`routes/data/+page.svelte:6`; `routes/+page.ts:7`; `nav-config.ts:236`) |
| F-10 | BUG | the cross-zone-reload gate is structurally blind to a dead-path link, its test pins `/data/tables` and `/lineage` as acceptable, and it never scans `packages/ui/**` — 163 assertions green while 5 links 404 (section 5) |
| F-9 | DEVIATES-WITH-REASON | `/media/guide` in the sidebar but not the cross-zone panel; reason at `nav-config.ts:81-83` |
| §3 pages | CONFORMS | 26 pre-merge pages → 26 post-merge; set difference empty in both directions |
| §3 endpoints | CONFORMS | 50 distinct BFF paths on both sides; 60→50 file drop is de-duplication only |
| §3 media/annotator | DEVIATES-WITH-REASON | 3 endpoints removed after the merge by `920f127`, which names the reason |

---

## 5. Why nothing caught F-3…F-6: the cross-zone gate is structurally blind to it (verified by running it)

§3 left this as "NOT VERIFIED". It is now verified, and it is a finding of its own.

**F-10 — `@repo/zone-contract`'s cross-zone-reload gate cannot catch a link to a path no zone owns, and
its own test codifies the dead pre-merge paths as legitimate. Verdict: BUG (false-assurance gate).**

The gate lives at `frontend/packages/zone-contract/src/cross-zone-reload.ts` with the estate-wide assertion
in `cross-zone-reload.test.ts:84-106`. Three independent reasons it lets F-3…F-6 through:

**(a) It only checks one direction.** `findViolations` flags an `<a>` only when
`isCrossZonePath(path) && !hasReloadEnabled(attrs)` (`cross-zone-reload.ts:102`). It asks "is this a
cross-zone link that forgot to hard-navigate?" — never "does this path resolve to anything?". All five
broken links **do** carry `data-sveltekit-reload`, so even a hypothetically-cross-zone verdict would pass
them.

**(b) `isCrossZonePath` returns false for the dead paths, by construction.** `ZONES = ['lakehouse', 'media',
'annotator']` (`:20`) and `ZONE_PATH = ^\/(lakehouse|media|annotator)(?:\/|$)` (`:23`). `/data…` and
`/lineage` match nothing. Run against the real markup (script:
`/tmp/claude-1000/-home-blackwell-Desktop-lance-ns/88508a85-af5d-44c1-9ef1-92d04ece7015/scratchpad/probe.ts`,
`bun run` output):

```
isCrossZonePath("/data/projects/acme") = false
isCrossZonePath("/data")              = false
isCrossZonePath("/lineage")           = false
findViolations(home/+page.svelte:25)          -> []
findViolations(project-switcher.svelte:55)    -> []
findViolations(TableDetail.svelte:930)        -> []
findViolations(PipelineControl.svelte:86)     -> []
findViolations(lineage/datasets/[name]:95)    -> []
```

Empty for all five. And the whole gate is green on `HEAD`:
`cd frontend/packages/zone-contract && bunx vitest run src/cross-zone-reload.test.ts` →
**`Test Files 1 passed (1) / Tests 163 passed (163)`** — 163 green assertions while five product links 404.

**(c) The test asserts a false premise.** `cross-zone-reload.test.ts:21-29`:
```
it('does NOT match a hop BETWEEN areas of the merged lakehouse zone', () => {
    // data / lineage / models / admin used to be four zones, so a link between them had to
    // hard-navigate. They are one zone now: these are same-zone soft navs, …
    expect(isCrossZonePath('/data/tables')).toBe(false);
    expect(isCrossZonePath('/lineage')).toBe(false);
    …
```
A **literal** `/data/tables` is not a same-zone link in the merged zone — the same-zone form is
`{base}/data/tables`, which the flattener renders as `￿/data/tables` and which the test covers separately
at `:35`. So `:25-28` pins the exactly-wrong strings as acceptable: it does not merely fail to catch F-3…F-5,
it documents them as correct. (The comment's intent is right; the literals chosen to express it are the
pre-merge URLs.)

**(d) Scope gap on top of that.** The estate-wide assertion globs
`components/frontends/*/src/**/*.svelte` (`cross-zone-reload.test.ts:87`) — `packages/ui/src/**` is **not**
scanned at all. So F-4 (`packages/ui/src/lib/shell/project-switcher.svelte:55`), the one broken link that
ships into all four zones, is outside the gate's file set regardless of the predicate.

This also corrects the tail of §3: the merge commit `bb099df` named an ESLint rule as "the load-bearing
piece"; that rule became this gate (`cross-zone-reload.ts:9-11` says so explicitly). It is load-bearing for
the *reload* invariant and carries zero load for the *path-exists* invariant, which is the one the merge
actually broke.

---

## Verification

Adversarial re-read by a second pass, default = **REFUTED** unless the code itself carries the claim.
Every path:line below was opened independently; every command was re-run. Nine claims examined
(the five most consequential deep-verified, marked ★): **8 CONFIRMED, 1 REFUTED**, plus one completeness
defect in §4.

**Tree drift first.** The report is headed `HEAD f9fe691`; HEAD is now `dfa95f9`.
`git diff --stat f9fe691..HEAD` = `docs/GOAL-VERIFY-PULL.md (tracker retired 2026-07-27; git history)`, `packages/api/tests/oidc.test.ts`,
`packages/zone-contract/src/budget.json`, `budget.test.ts` — **no route, nav, chart or component file
touched**, so every finding still stands at HEAD. All ~30 path:line cites I opened resolved to the claimed
text; no off-by-one worth reporting.

### ★ F-3 — home gallery cards → dead `/data/projects/*` — **CONFIRMED**
`frontend/components/frontends/home/src/routes/+page.svelte:25` is verbatim
``<a href={`/data/projects/${p.project}`} data-sveltekit-reload class="group block">``, inside the
`{#each data.projects}` of the signed-in landing page — so it is the primary CTA, as claimed. The
consequence was the part worth attacking, and it survives three independent checks:
`find components/frontends/home/src -type f` = 16 files, route manifest `/`, `/auth/{login,logout,callback}`,
`/capi/v1/projects` — no `/data`; `grep -n "reroute\|/data" home/src/hooks*.ts` = **no match** (no
`reroute` hook rescuing it); and the **dev** edge agrees with the cluster edge —
`components/frontends/home/microfrontends.json` gives `routing.paths` only to `lakehouse`, `media`,
`annotator`, and `packages/zone-contract/src/proxy.ts:31-50` resolves anything else to the prefix-`''`
catch-all (home). `/data/projects/<p>` 404s in dev and on the cluster.

### ★ F-4 — shared project switcher → dead `/data`, all four zones — **CONFIRMED**
`packages/ui/src/lib/shell/project-switcher.svelte:55` = `<a href="/data" data-sveltekit-reload …>`,
**unconditional** (inside `DropdownMenu.Content`, no `{#if}`), and the component is mounted by
`app-shell.svelte:107` `<ProjectSwitcher project={shellProject} />` — the shell every zone renders, so the
"broken in all four zones" claim holds. Its sibling at `:65` (`href={homeUrl}`) is fine.

### ★ F-7 — wide navbar has no route to the Lakehouse zone root — **CONFIRMED (and understated)**
Chain verified end to end: `nav-config.ts:234-241` gives the Lakehouse entry `href: '/lakehouse/data'` and
`groups: lakehouse` (never `items`); `DATA_ITEMS` starts at `/lakehouse/data/projects`
(`nav-config.ts:92`), so **no group item's href equals the entry href**; `top-navbar.svelte:167-208` (the
`groups` branch) renders only `entry.groups[*].items` and its `NavigationMenu.Trigger` at `:173-175` is
`bits-ui`'s `NavigationMenuPrimitive.Trigger` — a `<button>`
(`packages/ui/src/lib/components/navigation-menu/navigation-menu-trigger.svelte`), no `href`. The `items`
branch's root row and its "a panel must not be the only way in" comment are at `:215-233`, exactly as
cited. Two extra pieces of evidence the report did not use, both strengthening it: `overflowItems`'
comment (`:91-93`) asserts the narrow bar prepends the root *"exactly like the desktop panel"* — a parity
the `groups` branch does not implement, so the code contradicts itself in a comment, not merely in
behaviour; and `zone-nav.svelte:17` renders the zone title as `Sidebar.GroupLabel` (plain text, not a
link), so the sidebar is not a third way in. Exhaustive grep for a link to the zone root
(`grep -rnE "(\$\{base\}|/lakehouse)/data(['\"\`}]|\$)"` excluding `/data/`) returns **only**
`routes/+page.ts:7` (the redirect) and `nav-config.ts:236` (the button trigger's href). Desktop reach is
breadcrumb-or-URL only. Not intended behaviour: no comment anywhere sanctions the omission.

### ★ F-8 / F-1 — both scaffold pages, and `/lakehouse/admin`'s orphan status — **CONFIRMED**
`routes/data/+page.svelte` is 8 lines, `:6` = "The Data zone (P0 scaffold). Routes move here from apps/web
in P3."; `routes/admin/+page.svelte:6` is the same sentence with "Admin". `routes/+page.ts:7` =
``redirect(307, `${base}/data`)``. `ls frontend/components` = `frontends` only → `apps/web` really is gone.
Orphan status of `/lakehouse/admin` re-tested with a *precise* pattern rather than a substring sweep:
`grep -rnE "(\$\{base\}|/lakehouse)/admin(['\"\`}]|\$)" … | grep -v /admin/` returns **three hits, all
Playwright negative assertions** (`home/e2e/auth.spec.ts:60`,
`lakehouse/e2e/{models,lineage}/shell.spec.ts:90,114` — `toHaveCount(0)`), i.e. zero product-code inbound
references, which is stronger than the report's wording. Breadcrumb reachability confirmed structurally:
`breadcrumb.ts:39-44` `pathCrumbs` makes every path segment a crumb with `href: /<accumulated prefix>`, and
`app-shell.svelte:89-91,120-127` renders every crumb but the last as an `<a>`. `ADMIN_ZONE_NAV`
(`lib/admin/nav.ts:6-40`) lists six leaves, none of them the area root.

### ★ F-10 — the cross-zone gate is false assurance — **CONFIRMED (one sub-claim softened)**
(a) `cross-zone-reload.ts:102` is literally `if (isCrossZonePath(path) && !hasReloadEnabled(attrs)) {` —
one direction only. (b) `:20` `export const ZONES = ['lakehouse', 'media', 'annotator'];` and `:23`
`ZONE_PATH = ^\/(lakehouse|media|annotator)(?:\/|$)` — `/data…`, `/lineage` match nothing. (d)
`cross-zone-reload.test.ts:87` globs `components/frontends/*/src/**/*.svelte`; `packages/ui/**` is absent,
so F-4 is outside the file set. Re-ran the gate myself:
`cd frontend/packages/zone-contract && bunx vitest run src/cross-zone-reload.test.ts` →
`Test Files 1 passed (1) / Tests 163 passed (163)` — the 163 figure is exact.
**Softened:** sub-claim (c) ("the test documents F-3…F-5 as correct") overstates. `:25-28` asserts what the
*predicate* returns for those strings, not that such hrefs are legitimate product code; the gate has no
path-exists notion to be wrong about. The comment's rationale ("these are same-zone soft navs") is
genuinely false for a *bare* `/data/tables` — that much is confirmed — but the report's own parenthetical
already concedes the point, so read (c) as "the comment misdescribes the literal", not "the suite blesses
the bug".

### F-5 — three links → dead `/lineage` — **CONFIRMED, and exhaustive**
`TableDetail.svelte:930` (notInCatalog empty state), `:938` (denied empty state),
`PipelineControl.svelte:86`, all `href="/lineage" data-sveltekit-reload`.
`grep -rnE 'href="/(data|lineage|models|admin)(/|")' components/frontends packages/*/src` returns exactly
those three plus `project-switcher.svelte:55` — so "five instances of this bug class" is complete for
static hrefs, and ``grep -rnE '`/(data|lineage|models|admin)[/`]' `` adds only the F-3 template literal.
`grep -rnE "goto\(['\"\`]/(data|lineage|models|admin)" …` = **no match**, so no `goto()` variant was missed.

### F-6 — the "graph" link lands on the scaffold — **CONFIRMED**
`routes/lineage/datasets/[name]/+page.svelte:95` = `<a class="viewlink" href="{base}/"><Network …/> graph</a>`;
the sibling back-link at `:89` is `{base}/lineage/datasets` and the column-lineage link at `:92` is
`{base}/lineage/columns?…`, so the miss is local, not a convention. Repo-wide there are exactly **two**
`href="{base}/"` links: this one and `media/src/routes/guide/+page.svelte:1925`, where `{base}/` = `/media/`
= the media Search page and is therefore correct — which is precisely why the pattern reads as legitimate
and is wrong here (`/lakehouse/` → `+page.ts:7` → the F-8 scaffold).

### F-2 — `/media/diagram` "BUG (dead route)" — **REFUTED (verdict), facts confirmed**
The factual half holds: repo-wide `grep -rn "media/diagram\|routes/diagram\|/diagram"` over `*.md *.ts
*.svelte *.yaml *.json *.sh` returns only the endpoint's own `import Flow from '$lib/diagram/Flow.svelte'`
and `Flow.svelte:4`'s back-reference — nothing, including `e2e/**`, `docs/**` and the chart, fetches it.
But the verdict is exactly the case I was asked to check for: `routes/diagram/+server.ts:1-3` states its
consumer is **outside the app** — "render the graph to an HTML string on the server (no browser), for docs
/ OG images / non-JS. Lives OUTSIDE /api (which is proxied to the FastAPI backend)" — and the estate's own
dead-route gate agrees with that framing on purpose: `bff-routes.test.ts:72-95` requires a caller only for
routes that `.filter((r) => r.startsWith('/api/'))`, because the harm it names is "an
unauthenticated-by-nobody proxy sitting on the deploy surface" (`:8-17`). `/diagram` proxies nothing and
punches no hole. So this is unreferenced-but-declared-external code, i.e. **DEVIATES-WITH-REASON**, not a
BUG on the same list as five 404-ing product links. Nothing here breaks a user flow.

### §3 "nothing lost in the 7 → 4 merge" (CONFORMS) — **CONFIRMED independently**
Recomputed from git, not from the report's numbers.
`git ls-tree -r --name-only <rev> -- frontend/components/frontends | grep '+page.svelte$'` = **34 on
`bb099df^` and 34 on HEAD**. Mapping each pre-merge `<zone>|<route>` to `/lakehouse/<zone>/<route>`: pre
`admin` 7 + `data` 9 + `lineage` 7 + `models` 3 = **26**, post `lakehouse` = **26**, and the two sorted URL
sets are **identical** (both set differences empty, including the `data|` and `admin|` roots →
`/lakehouse/data`, `/lakehouse/admin`). Endpoints: pre `+server.ts` files under the four merged zones =
**60**, post under `lakehouse` = **50**; distinct BFF paths after stripping `…/src/routes` and
`/+server.ts` = **50 vs 50**, `diff` of the sorted lists is **empty**. The 60→50 drop is de-duplication,
as claimed.

### §4 Lakekeeper route set ("verified, read directly") — **INCOMPLETE**
Re-fetched `github.com/lakekeeper/console/tree/main/src/pages`: the six `warehouse/**` files match the
report exactly (`index.vue`, `[id].vue`, `[id].namespace.[nsid].vue`, `…table.[tid].vue`, `…view.[vid].vue`,
`…generic-table.[tid].vue`). The top level has **14** page files; the report's table lists **13** —
`loqe.vue` is missing from it. No comparison conclusion turns on it, but the section claims the route set
was read directly, so the table should be 14 rows or say why not. The rest of §4 (their nav chrome, route
guards, screenshots) is explicitly listed as unverified by the report itself and I could not verify it
either — those parts remain unfalsifiable from this repo and are correctly labelled as such.

### Net
| claim | verdict |
|---|---|
| ★ F-3 home CTA → `/data/projects/*` 404 | CONFIRMED |
| ★ F-4 project switcher → `/data` 404, all zones | CONFIRMED |
| ★ F-7 no desktop route to the Lakehouse zone root | CONFIRMED (understated) |
| ★ F-8 + F-1 scaffold landing + orphan scaffold | CONFIRMED (orphan proof strengthened) |
| ★ F-10 gate blind to dead paths, 163 green | CONFIRMED; sub-claim (c) softened |
| F-5 three bare `/lineage` links (and the set is complete) | CONFIRMED |
| F-6 `{base}/` "graph" link → scaffold | CONFIRMED |
| F-2 `/media/diagram` = BUG | **REFUTED** → DEVIATES-WITH-REASON |
| §3 pages 26=26 / endpoints 50=50, CONFORMS | CONFIRMED |
| §4 "their route set (verified)" | INCOMPLETE (13 of 14 page files) |

Surviving most-important finding: **F-4** — one stale `href="/data"` in `@repo/ui`'s project switcher, in
the shell chrome of all four zones, 404s for every user in every zone; it is the affordance that would
otherwise satisfy F-7, and it sits in the one directory the cross-zone gate never scans (F-10d).
