---
name: rask-frontend
description: The console — rask's one SvelteKit 3 + Svelte 5 single-page app in `frontend/` with no backend of its own: its route tree and navbars, browser sign-in (OIDC + PKCE against Dex, tokens in memory, `/config.json`, the CSP `serve.ts` sends), the browser data layer (`reads.svelte.ts`, `*.api.ts`, the bearer provider, same-origin `/api/*` per service), the bell (the reader's own activity log), the `/workbench` dock (horizon-layout), and the gates (oxlint/oxfmt, svelte-check, vitest, the Playwright suite behind `e2e/edge.ts` and a mock issuer). Use when touching any `.svelte` or `frontend/` file, `#lib/api`, `#lib/flow` or `#lib/dock`; when adding a route, a page, a read or write, or a frontend dependency; when wiring nav or a guard; when building a dock or panel; when the bell lists a write wrongly or not at all; or when sign-in loops, a call 401s/404s from the browser, the CSP blocks something, or a frontend gate fails.
---

# rask frontend — the console

Every JS/TS file lives under `frontend/`, its own bun 1.4.4 workspace root (`package.json`, `bun.lock`, `knip.json`,
`.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`). Invoke everything scoped: `bun --cwd=frontend run <task>`.
The `--cwd=` form matters — `bun --cwd <path>` with a space silently no-ops. There is no Turborepo: the root scripts
are plain bun (`bun run --filter '*' <task>` or explicit sequences), so a task over a package runs only if that
package declares the script.

Workspace membership is **globbed** (`["console", "packages/*"]`), so a `packages/<x>` carrying a `package.json` is
enrolled automatically and one without it is **silently skipped** — bun prints "Done!" and the package is never
installed, built or linted. There are two members (ADR 0201): the console, and `@rask/ui`, the design system, which
Storybook also builds. Code with one consumer lives in that consumer: the console's API client is `#lib/api`, its graph
views `#lib/flow`, its dock `#lib/dock`. `@rask/ui`'s `exports` name its `src/`, so the console compiles it from source
and nothing builds first.

Styling and component authoring live in **`rask-styling`**. Svelte 5 and SvelteKit idioms live in `svelte-skills:*`
and the `svelte` MCP. This skill is the plane above them: the app, its data, its sign-in, its gates.

The console is **the lakehouse's own UI** (ADR 0193): it reaches the catalog and lineage only through the public doors
any client uses, and the **product chart** ships it (`chart/templates/console.yaml`, `console.enabled`): the Deployment
and Service `rask-console` (image `web-console`, port `console.port` 3000), the ServiceAccount `rask-sa-console` with no
token mounted (the console calls no door as itself), the ConfigMap `rask-console-config` holding `/config.json`, and the
HTTPRoute `rask-console` sending `/` to it. `make k3s-up` installs it with the lakehouse. The no-client lint
(`scripts/lakehouse_names_no_client.py`) scopes `frontend/` too, so no file here names a client.

## The one app

`frontend/console` (package `console`) is a SvelteKit app built with `@sveltejs/adapter-static` and
`fallback: 'index.html'`; the root `+layout.ts` sets `ssr = false` and `prerender = false`. Nothing renders on a
server, so browser globals are fine anywhere, and there is no `+server.ts`, `+page.server.ts`, `hooks.server.ts` or
`server` directory — adding one is adding a backend, which the owner ruled out ("SPA, no backend", 2026-11-04).

**SvelteKit 3's config and filename rules.** The project config is the `sveltekit({...})` call in
`console/vite.config.ts` (adapter-static, the CSP, `compilerOptions.experimental.async`); a `svelte.config.js` in the
console is a build error. `#lib/*` is a Node subpath import declared in the console's `package.json` `imports` and
written with the file's extension (`#lib/doors.ts`, `#lib/reads.svelte.ts`, `#lib/data/StageBadge.svelte`), as
`sv migrate sveltekit-3` writes it and `$app/tsconfig`'s `allowImportingTsExtensions` allows. Kit 3 reads a file named
`remote.*` (`/[/.]remote\.[^/]+$/`) as a remote-functions module, which is a build error without
`experimental.remoteFunctions`, and any `server` directory or `*.server.*` / `server.*` file as server-only
(`src/exports/vite/utils.js`); a directory named `remote` does not match, so the `remote/` folders of `*.api.ts`
modules are fine, and the data layer is `reads.svelte.ts`. `tsconfig.json` extends `$app/tsconfig`, which
`svelte-kit sync` writes into `node_modules/$app/`.

| path | what it is |
|---|---|
| `/` | the estate landing |
| `/projects`, `/projects/[project]` | the project gallery and one project's overview (create, delete, policies) |
| `/settings`, `/settings/access`, `/settings/audit` | estate configuration, estate-admin only (`settings/+layout.ts`) |
| `/catalog` | the lakehouse overview of the active project |
| `/catalog/{warehouses,namespaces,tables,promotions,storage}` (`warehouses/[id]`, `namespaces/[id]`, `tables/[table]`, `promotions/[table]`) | the catalog; `storage` is the tier view, which states the catalog's refusal (below) |
| `/admin`, `/admin/{events,streams,dlq}` | operations, estate-admin only (`admin/+layout.ts`) |
| `/workbench` | the dock (events, tables) |
| `/auth/{login,callback,logout}` | the sign-in round trip, client pages |

There is no base path (the `sveltekit()` options set no `paths.base`), and SvelteKit soft-navigates every in-app link. Every `href` and
`goto` still goes through `resolve()` from `$app/paths`, which fills a route id's parameters: oxlint's
`svelte/no-navigation-without-resolve` is an error (`frontend/.oxlintrc.json`), and the two `-without-base` rules are off
because they flag `resolve()` itself (`frontend/TOOLING.md`). A path that carries a catalog name or id is written in the
route-id form, `resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(ns) })`: kit 3 reads any string that
starts with `/` as a route id, and a name interpolated into one is split into segments with one shaped like a route
group dropped, so a namespace named `(staging)` would link to the list page
(`e2e/admin/tables-registry.spec.ts` pins it). A pathname
with a query is written without the leading `/`, ``resolve(`auth/login?redirect=${…}`)``, and `goto` takes kit 3's options
(`replace`, `reset: false`, `refreshAll`) and rejects a URL that names no route. `@rask/ui` cannot import `$app/paths`, so the rule is off under
`packages/ui/**`; its root paths are what `resolve()` gives in the console.

**Page titles.** Every page's `<title>` is its heading followed by ` · rask` (`Lineage DLQ · rask`), and a
page about one named object puts the name, as the catalog writes it, before its collection (`db1$t · tables · rask`).

**Two navbars, chosen by path.** `isMainMenu(pathname)` in `@rask/ui`'s `shell/nav-config.ts` decides which bar
renders: `/`, `/projects` and `/settings/**` are the ESTATE level (`mainMenuNav()`: Home · Projects · Settings),
everything else — `/projects/<id>` included, opening a project is what puts you inside one — gets `topNav()`, one
**Lakehouse** entry whose panel holds Workspace and Catalog columns, plus Operations for an estate admin. Settings for
a non-admin renders DISABLED with the FGA denial's wording, and its `items` are dropped so the entry hands out no child
links.

**The audit and streams pages cannot work.** They need the platform's telemetry store and the NATS monitor (`/jsz`),
which no Gateway route serves to a browser; the owner kept them anyway (2026-11-04), so `audit.api.ts` and
`jetstream.api.ts` answer with that failure, naming what is missing, without a request. So do the doors the catalog does not serve:
`#lib/storage/remote/storage.api.ts` (the stores-by-tier registry and its attach write) answers an honest 501 with a
`detail` naming what no door keeps, and the storage page renders that refusal ("The stores registry is unavailable (HTTP 501) — …"). The e2e mocks
serve only doors that exist. Promotions read the catalog's own doors:
`GET /management/v1/promotions` for the open holds, and `POST /management/v1/table/{id}/promotion/{approve,reject}` for a
decision, keyed by table.

## Sign-in and tokens

`#lib/auth.svelte.ts`, on `oidc-client-ts`: OpenID Connect authorization code with PKCE, as a PUBLIC client
(`console.oidc.clientId`, `rask-console` by default, which holds no secret). The dev Dex declares it as the lakehouse's
own, under its own `console` value (`deploy/fixtures/dex/chart/values.yaml`), with redirects to
`http://localhost:8080/auth/callback` and `http://localhost:5173/auth/callback`, apart from the `clients` seam so no
client's values can drop it; a platform's issuer registers the same public client. The dev Dex enforces PKCE
for every authorization code (`oauth2.pkce.enforce: true`, `codeChallengeMethodsSupported: [S256]`,
`deploy/fixtures/dex/chart/templates/dex.yaml`), and its own `rask-web` client is confidential with no redirect, signed in
as by the live lanes' password grant, so no browser signs in through it. Tokens live **in memory only** (`InMemoryWebStorage` user store); only the
transient PKCE state rides `sessionStorage` for one redirect. A reload therefore starts signed out and goes back to the
issuer: one that keeps a session answers at once, and the dev Dex, which keeps none, shows its login form. The root `+layout.ts` sends a signed-out visit to the
issuer and returns to the requested path; the `/auth/*` pages are the round trip. Sign-out (`logout()`) calls
`signoutRedirect()` while the user is still held, so the end-session request carries `id_token_hint`; `signoutRedirect`
removes the user itself before it navigates. Without an `end_session_endpoint` it removes the user and goes to
`/auth/login?prompt=login`, so an issuer that keeps a session authenticates whoever signs in next instead of reusing it.
The dev Dex keeps no session: Dex v2.46.1 keeps sessions, reads `prompt` (`server/authflow/login.go`) and advertises its
end-session endpoint (`server/discovery/discovery.go`) only with its sessions flag on, `DEX_SESSIONS_ENABLED`, which is
off by default and unset in the fixture, so it shows its login form on every sign-in and takes the `prompt=login` path.

**The issuer, client and scopes come from `/config.json`** (`{issuer, clientId, scopes}`, validated with valibot),
which the product chart renders from its values (`rask-console-config`: `auth.oidc.issuer`, `console.oidc.clientId`,
`console.oidc.scopes`), so the built app carries no environment. `issuer` may be absolute or a
same-origin path. Sign-in is OFF only where the config says so, `{"signIn": false}` (no bearer is sent; the auth-off
Playwright project serves `e2e/console-config.off.json`). A missing or malformed `/config.json` fails closed:
`auth.svelte.ts` sets `configError`, and the root `+layout.ts` answers 503 saying why, with no `/api` call.

**The scopes are load-bearing.** The lakehouse's governed doors refuse a token without `federated:id`
(`lakehouse.governed.idp_identity`: "a client asks for the federated:id scope") and one whose `aud` does not name the
lakehouse, which Dex grants through `audience:server:client_id:<audience>`. The chart adds both to
`console.oidc.scopes` itself (the audience scope is `auth.oidc.audienceScope`, or built from `auth.oidc.audience`), so
no value can drop them; with `auth.enabled=false` it renders `{"signIn": false}`.

**The CSP travels with the app.** The `csp` option of `sveltekit()` in `vite.config.ts` declares it (mode `hash`: `default-src 'self'`,
`script-src 'self'` plus the bootstrap hash, `connect-src 'self'`, `style-src 'self' 'unsafe-inline'` for Svelte's and
Bits UI's inline styles). `serve.ts` lifts that policy out of the built `index.html`, removes the meta tag, and sends it
as a HEADER, adding the configured issuer's origin to `connect-src` and `frame-ancestors 'none'`, plus
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The meta tag must go: a meta policy and a header
policy both apply, so a meta `connect-src 'self'` would block a cross-origin issuer whatever the header said. A library
that compiles code at runtime is blocked by `script-src`: `patches/apache-arrow@21.2.0.patch` replaces apache-arrow's
`new Function` null check with a closure that keeps upstream's semantics (held to upstream's compiled function by
`console/src/lib/arrow-null-values.test.ts`; the one divergence is in `frontend/TOOLING.md`), without which the
insert-rows and create-with-rows forms break.

**A client guard is disclosure, not authorization.** `admin/+layout.ts` and `settings/+layout.ts` refuse a non-admin
in the browser, and a person can bypass them. What authorizes is the catalog's and lineage's own check on every request
(FGA, estate-admin on the root); the guards keep the UI from offering what will be refused.

## Fetching data

The browser calls every API itself, **same-origin**, with the signed-in person's bearer; the platform's Gateway routes
each prefix to its service (`rask-services-fleet`). `#lib/upstreams.ts` is the one table of bases, the same bytes on
every estate:

| base | service | what the browser sends |
|---|---|---|
| `/api/catalog` | the catalog | its own paths below the prefix (`/api/catalog/management/v1/me`, `/api/catalog/v1/table/{id}/query`); the catalog strips the prefix itself (`rask.catalogPublicPrefix`) |
| `/api/lineage` | lineage | `/api/lineage/events?…`, `/api/lineage/admin/dlq…`; the Gateway rewrites the prefix to lineage's root |

**The bearer is attached in one place.** `#lib/api/client.ts` takes a provider the app installs once
(`setBearerProvider`, called by `auth.svelte.ts`), and `upstreamJSON` / `requestJSON` send it on every call;
`#lib/doors.ts::catalogJSON` is the catalog's door over it. The result is the shared `ApiResult` union: 0 unreachable, n
answered-n with the upstream's `{detail}`, 502 contract drift (a payload that fails its valibot schema).

**`#lib/reads.svelte.ts` is the data layer**: `query` (a cached, refreshable read), `query.live` (a cursor that
advances), and `command` (a write), in the browser. Every `*.api.ts` module (`#lib/remote/`, `#lib/data/remote/`,
`#lib/admin/remote/`, `#lib/lineage/remote/`, `#lib/live/`, `#lib/storage/remote/`) declares its reads and writes with
them, and a component holds a read as `await read()`, `read().current`, `read().refresh()`, and a live cursor as
`live().current` and `live().error`: a live cursor is no promise, because a template `await` of one held the page's
render batch open until its first value, and Svelte 5.57.3 read state written in that batch as its old value
elsewhere (the sidebar's groups stayed `hidden` on a hard load of `/admin/events`). A `command` is declared
with its label first, `command(label, schema, run)`: a string or a function of the caller's input, naming the action
and its object for the activity log (below). Calling a read with the same
arguments returns the SAME instance, so a write's `void read().refresh()` updates every holder; a read settled within
1 s is reused (a burst deduplicates), and a failing read rejects and leaves `.current` as it was. A generation fence
lets only the latest start write a read's state: a refresh started while an older read is in flight supersedes it, so
the older answer, however late, never overwrites the newer one or clears `loading` early. The instance caches stay
unbounded on purpose, because "same arguments, same instance" is what a write's refresh relies on. Input is validated
against the function's valibot schema, defaults applied, so a command's body carries its schema's defaults.

**The bell is the reader's own activity log** (`#lib/activity.svelte.ts`, ADR 0193), as Lakekeeper's console keeps its
notifications. Every `command` records its outcome: a success, an `ApiResult` failure with its status and `detail`, or
a thrown error (a schema refusal included), which is recorded and re-thrown. Reads record nothing: a read's failure
renders on its own page. The log lives in `sessionStorage` under the signed-in principal, in memory when storage is
unavailable (every access in try/catch), at most 100 entries and none older than 24 h, and `activity.forget()` clears
it at sign-out. Not `localStorage`: the next person on the same browser profile would find it. Nothing in it is read
from or sent to a service. The bell and its panel are `@rask/ui`'s `shell/activity-center.svelte`, fed through props and
callbacks (`@rask/ui` imports nothing from the console): the badge counts entries since the panel was last opened, and
the panel lists newest first, grouped by day, and can clear the log.

**Liveness rides cursors, not timers.** `#lib/live/tick.svelte.ts` wraps the shared `lineageFeed`/`controlCursor` streams
of `feeds.api.ts` as `lineageTick`/`controlTick`, read through `liveRead` (`#lib/api/live.svelte.ts`), which opens on mount and
treats a cursor's arrival as no change. Data mutations move the lineage cursor; governance mutations (grants,
warehouses, tenants) move the control cursor. A timer is right only when nothing publishes the change (a liveness probe)
or time itself is the change (a countdown).

## The workbench — `horizon-layout`

The dock lives at `/workbench` (`#lib/dock/`, ADR 0200) on **horizon-layout** (pinned exactly in the console's
`package.json`): headless, Svelte 5, MIT, with keyboard control built in (the tab list takes arrows, Shift/Ctrl/Alt
moves and splits; the resizer is a slider), and a patch (`patches/horizon-layout@1.0.5.patch`, `frontend/TOOLING.md`)
adds the tab list's ARIA state and keeps the keyboard on a tab it moves. Two panels: `EventsPanel` (lineage's `GET /events` through the workbench's
one `EventFeed`, `#lib/dock/bench.svelte.ts`) and `TablesPanel` (the very registry the catalog page renders). A panel
RENDERS THE PAGE'S COMPONENT: it is not a smaller re-implementation. `Dock.svelte` adds what the library leaves to its
host, from `@rask/ui` components: a close button on each tab (`tabControls`), and per group (`tabgroupControls`) the panel
picker for closed panels, maximize (with a Restore bar above the dock while one is maximized) and pop out. There are no
floating groups and no context menu.

**Four invariants.** Break any and the dock is wrong:

1. **A panel holds nothing in component state that must survive a remount.** The library renders only the active tab
   of a group, and remounts a panel when it is moved, maximized or popped out. Data lives in a module
   (`bench.svelte.ts`) or a shared read instance (`TableRegistry` seeds from `fetchTables().current`); scroll position,
   search text and sort order reset, and that is accepted. `e2e/workbench/dock.spec.ts` pins it with reads that stop
   answering.
2. **A panel reads no context.** A popout mounts the panel in another window, outside the page's component tree, with
   the page's stylesheets copied in; `Dock.svelte`'s `followTheme` mirrors the page's `<html>` classes onto that window
   so light and dark follow. A bits-ui overlay inside a popped-out panel still portals to the page's document.
3. **The layout is one value.** The page binds one `LayoutConfig`; every control writes a new value through
   `#lib/dock/layout.ts`, and a stored layout is read with `parseLayoutConfig` then `validateConfig` (`readStoredLayout`).
   Its three outcomes are `ok` / `absent` / **`unreadable`**, which shows the default with a notice and refuses every
   save (`autosave.ts`, `views.svelte.ts`) until the person presses Reset layout, so nothing is written over a layout
   that exists and could not be read. Where layouts and named views are kept is decided in `#lib/dock/store.ts` alone:
   this browser's `localStorage`, one key per person (`rask-workbench:` and the SHA-256 of the principal's id), holding
   the layout and the views and nothing else; memory where storage is refused, said in one line. Sign-out keeps them
   (a preference), unlike the activity log.
4. **The dock's stylesheets load with the app.** `app.css` imports `horizon-layout/horizon-layout.css` and the
   `--hl-*` → token mapping (`#lib/dock/theme.css`) under `layer(base)`, so a popout copies them when it opens. A dock's
   parent must be a SIZED flex item (`flex: 1 1 0`, `min-height: 0`), or the split has no height to divide.

## Develop

| loop | command | what runs |
|---|---|---|
| UI against the cluster | `make dev-k3s` | reuses :8080 when it already serves the issuer, else port-forwards `kube-system/traefik` 8080:80, waits for `/dex` and `/api/lineage/livez`, runs `vite dev` (:5173) with `/api` proxied there, and stops the forward by PID |
| UI against an edge you name | `make dev` (`RASK_EDGE_URL`, default `http://localhost:8080`) | `vite dev` on :5173; an edit to `@rask/ui` reaches it through HMR |
| Storybook | `make storybook` | `@rask/ui`'s Storybook on :6006 |

`vite.config.ts` serves `/config.json` from `CONSOLE_CONFIG_FILE`, else the committed `frontend/console/dev/config.json`
(issuer `http://localhost:8080/dex`, client `rask-console`, the chart's scopes), so a dev server signs in against whichever
issuer that file names. Under `vite dev` SvelteKit sends that `csp` as a header (measured on kit 3.1.1), so the dev server widens `connect-src` by
exactly the issuer's origin, as `serve.ts` does for the image; the built app's own policy is untouched. The Vite origin
(:5173) is cross-origin to Dex on :8080, which answers CORS for it (the Dex fixture's `console.allowedOrigins`). The image is `.docker/frontend.dockerfile`
(`make console-image` → `web-console:<tag>`, through `dagger call image --name=frontend`): a builder stage builds
the app (with `@rask/ui` from source), the runtime stage holds only `build/` and `serve.ts`, runs as uid 10001 under tini on :3000, and
fits a read-only root filesystem.

## Gates

| command | runs |
|---|---|
| `bun --cwd=frontend run build` | the app's `vite build`, compiling `@rask/ui` from source |
| `bun --cwd=frontend run check` | `svelte-check` in the console (three times: the app, `tsconfig.tools.json` for `serve.ts` and the e2e code, and `tsconfig.api.json`, which adds `exactOptionalPropertyTypes` to `src/lib/api`) and in `@rask/ui` |
| `bun --cwd=frontend run test` | vitest in the console (its `#lib/api` suites included) and in `@rask/ui` |
| `bun --cwd=frontend run lint` / `fmt:check` | oxlint / rsvelte-fmt in every package |
| `bun --cwd=frontend run knip` | the whole JS graph at once (`knip.json`) |
| `bun --cwd=frontend run test:e2e` | the console's Playwright suite, which builds the app |
| `make frontend-check` | check, lint, fmt:check, test and knip |
| CI | `dagger call frontend` (check, test, lint, fmt:check, every one run and the job failing if any did) and `web-e2e` (`test:e2e`) |

Lint is **oxlint** (`svelte/require-each-key: error`, `svelte/no-reactive-reassign: error`,
`svelte/no-navigation-without-resolve: error`, via `@rsvelte/oxlint-plugin`);
format is **oxfmt** with tabs, single quotes, `printWidth: 100`. ESLint and Prettier are not installed.

**The Playwright suite runs the BUILT app** (`frontend/console/playwright.config.ts`) behind `e2e/edge.ts`, a Bun-native
stand-in for the Gateway: one origin that routes each `/api/*` prefix to a seed-driven mock upstream (`e2e/mock-*.ts`,
`e2e/*/mock-*.ts`) and everything else to the shipped `serve.ts`, so its CSP and SPA fallback are what the specs meet.
Vite preview is not used: it crashes under Bun's `node:http` proxy, and the host has no Node. Three origins of the one
build, one per family of specs: **auth-off** (`e2e/*.spec.ts`, `e2e/catalog/`, `e2e/workbench/`, served `e2e/console-config.off.json`,
`{"signIn": false}`), **estate** (`e2e/projects/`, `e2e/settings/`, `e2e/activity/`) and **admin** (`e2e/admin/`).
An auth-on origin serves `e2e/console-config.json`, naming `e2e/mock-oidc.ts` under `/oidc`, so sign-in runs the app's
real PKCE round trip: the mock issuer refuses, at `/auth`, any client but `rask-console`, a `redirect_uri` other than
the origin's `/auth/callback` and a request without an S256 challenge, and at `/token` a verifier that does not match it, and serves an `end_session_endpoint` (omitted for a context
carrying the `mock_oidc_no_end_session` cookie, `withoutEndSession()` in `e2e/admin/session.ts`). Its session is the
identity cookie, and like an issuer that keeps one, it answers `prompt=login` with its login page even while it holds it. There is no test-only
bypass in the app. Ports are declared in `e2e/ports.ts` and `e2e/admin/ports.ts`. A read the
browser makes can also be stubbed per spec with `page.route`, except in a spec that opens a popout: with a route on the context, Playwright holds the stylesheet requests of a window opened as `about:blank`, and the window renders unstyled for a reason the app does not have (measured 2026-11-11, Playwright 1.65).

**TypeScript.** `strict` and `noUncheckedIndexedAccess` are on in the app (`frontend/console/tsconfig.json`);
`exactOptionalPropertyTypes` stays off for an upstream reason in Svelte code (Bits UI "union too complex"), and is on for
the API client (`tsconfig.api.json`). The app's `tsconfig.json` extends `$app/tsconfig` and covers `src`; the console's `check` runs
`svelte-check` a second time with `tsconfig.tools.json`, which type-checks the code that runs under Bun and Playwright
rather than in the browser (`serve.ts`, `playwright.config.ts`, `e2e/**`, with `types: ["bun"]`). Validation is
**valibot**.

**`derived_inert`.** Svelte warns `Reading a derived belonging to a now-destroyed effect` when a closing (inert) subtree's
derived is read. bits-ui raised it from its dismissible and escape layers until 2.21.0's dismissible layer kept a
snapshot; measured 2026-11-11 on bits-ui 2.21.0 and svelte 5.57.3 under `vite dev`, no unpatched layer open/close in the
console (menus, the navbar panel, the bell, a select, the row drawer, single, doubled and overlapping Escapes) warns, so
no bits-ui patch is carried. To locate such a warning in a browser, use CDP (`Runtime.consoleAPICalled`'s
`stackTrace`), not `page.on('console')`, which Vite's client re-wraps.

Run the Svelte autofixer on every `.svelte` file you touched (`svelte:svelte-code-writer`, or the `svelte` MCP when it
connects) until it reports clean.

## Staying on-stack

Animation → **GSAP** via `{@attach}`. Charts → **LayerChart**. Graph editors → **Svelte Flow** via `#lib/flow`.
Components → `@rask/ui`. Validation → **valibot**. Sign-in → **oidc-client-ts**. A dependency that duplicates the stack is
a no; extend the stack instead.

## Where to go deeper

- `rask-styling` — tokens, `@source`, component authoring.
- `rask-services-fleet` — the Gateway routes for `/api/*` and the services behind them.
- `rask-architecture` — workspace planes and membership.
- `docs/AUTHZ.md` — what the console's guards are and are not.
