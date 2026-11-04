---
name: rask-frontend
description: The console — rask's one SvelteKit 2 + Svelte 5 single-page app in `frontend/` with no backend of its own: its route tree and navbars, browser sign-in (OIDC + PKCE against Dex, tokens in memory, `/config.json`, the CSP `serve.ts` sends), the browser data layer (`remote.svelte.ts`, `*.api.ts`, the bearer provider, same-origin `/api/*` per service), `@rask/dockview` workbenches, and the gates (oxlint/oxfmt, svelte-check, vitest, the Playwright suite behind `e2e/edge.ts` and a mock issuer). Use when touching any `.svelte` or `frontend/` file, `@rask/api`, `@rask/dockview` or `@rask/flow`; when adding a route, a page, a read or write, or a frontend dependency; when wiring nav or a guard; when building a dock or panel; or when sign-in loops, a call 401s/404s from the browser, the CSP blocks something, or a frontend gate fails.
---

# rask frontend — the console

Every JS/TS file lives under `frontend/`, its own bun 1.3.14 workspace root (`package.json`, `bun.lock`, `knip.json`,
`.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`). Invoke everything scoped: `bun --cwd=frontend run <task>`.
The `--cwd=` form matters — `bun --cwd <path>` with a space silently no-ops. There is no Turborepo: the root scripts
are plain bun (`bun run --filter '*' <task>` or explicit sequences), so a task over a package runs only if that
package declares the script.

Workspace membership is **globbed** (`["console", "packages/*"]`), so a `packages/<x>` carrying a `package.json` is
enrolled automatically and one without it is **silently skipped** — bun prints "Done!" and the package is never
installed, built or linted.

Styling and component authoring live in **`rask-styling`**. Svelte 5 and SvelteKit idioms live in `svelte-skills:*`
and the `svelte` MCP. This skill is the plane above them: the app, its data, its sign-in, its gates.

The console is a **client of the lakehouse**: it reaches the catalog, lineage and the notifications inbox only through
their published doors, and the lakehouse names it nowhere (`scripts/lakehouse_names_no_client.py`). Its release is
`deploy/fixtures/console` (the web pod and the inbox), installed by `make k3s-clients`.

## The one app

`frontend/console` (package `console`) is a SvelteKit app built with `@sveltejs/adapter-static` and
`fallback: 'index.html'`; the root `+layout.ts` sets `ssr = false` and `prerender = false`. Nothing renders on a
server, so browser globals are fine anywhere, and there is no `+server.ts`, `+page.server.ts`, `hooks.server.ts` or
`$lib/server` — adding one is adding a backend, which the owner ruled out ("SPA, no backend", 2026-11-04).

| path | what it is |
|---|---|
| `/` | the estate landing |
| `/projects`, `/projects/[project]` | the project gallery and one project's overview (create, delete, policies) |
| `/notifications`, `/preferences`, `/preferences/notifications` | the inbox behind the bell, and a person's notification preferences |
| `/settings`, `/settings/access`, `/settings/audit` | estate configuration, estate-admin only (`settings/+layout.ts`) |
| `/catalog` | the lakehouse overview of the active project |
| `/catalog/{warehouses,namespaces,tables,promotions,storage}` (+ `[id]`, `[table]`, `[instance_id]`) | the catalog; `storage` is the tier view (`/catalog/stores` redirects there) |
| `/admin`, `/admin/{events,streams,dlq}` | operations, estate-admin only (`admin/+layout.ts`) |
| `/workbench` | the dock (events, tables) |
| `/auth/{login,callback,logout}` | the sign-in round trip, client pages |

There is no zone and no base path: every link is a plain in-app link, and SvelteKit soft-navigates all of them.

**Two navbars, chosen by path.** `isMainMenu(pathname)` in `@rask/ui`'s `shell/nav-config.ts` decides which bar
renders: `/`, `/projects` and `/settings/**` are the ESTATE level (`mainMenuNav()`: Home · Projects · Settings),
everything else — `/projects/<id>` included, opening a project is what puts you inside one — gets `topNav()`, one
**Lakehouse** entry whose panel holds Workspace and Catalog columns, plus Operations for an estate admin. Settings for
a non-admin renders DISABLED with the FGA denial's wording, and its `items` are dropped so the entry hands out no child
links.

**The audit and streams pages cannot work.** They need the platform's telemetry store and the NATS monitor (`/jsz`),
which no Gateway route serves to a browser; the owner kept them anyway (2026-11-04), so `audit.api.ts` and
`jetstream.api.ts` answer with that failure, naming what is missing, without a request. Promotions read the catalog's own doors:
`GET /management/v1/promotions` for the open holds, and `POST /management/v1/table/{id}/promotion/{approve,reject}` for a
decision, keyed by table.

## Sign-in and tokens

`$lib/auth.svelte.ts`, on `oidc-client-ts`: OpenID Connect authorization code with PKCE, as a PUBLIC client (Dex's
`rask-web`, which holds no secret). Tokens live **in memory only** (`InMemoryWebStorage` user store); only the
transient PKCE state rides `sessionStorage` for one redirect. A reload therefore starts signed out and goes back to the
issuer, which answers at once while it still holds a session. The root `+layout.ts` sends a signed-out visit to the
issuer and returns to the requested path; the `/auth/*` pages are the round trip.

**The issuer, client and scopes come from `/config.json`** (`{issuer, clientId, scopes}`, validated with valibot),
which the release renders from its values, so the built app carries no environment. `issuer` may be absolute or a
same-origin path. A stack that serves no config runs with sign-in OFF (no bearer is sent), which is what the auth-off
Playwright project and a bare `make dev` exercise.

**The scopes are load-bearing.** The lakehouse's governed doors refuse a token without `federated:id`
(`service_kit.governed.idp_identity`: "a client asks for the federated:id scope") and one whose `aud` does not name the
lakehouse, which Dex grants through `audience:server:client_id:<audience>`. The chart's default scopes carry both
(`deploy/fixtures/console/chart/values.yaml` `web.config.scopes`).

**The CSP travels with the app.** `svelte.config.js` declares it (`kit.csp`, mode `hash`: `default-src 'self'`,
`script-src 'self'` plus the bootstrap hash, `connect-src 'self'`, `style-src 'self' 'unsafe-inline'` for Svelte's and
Bits UI's inline styles). `serve.ts` lifts that policy out of the built `index.html`, removes the meta tag, and sends it
as a HEADER, adding the configured issuer's origin to `connect-src` and `frame-ancestors 'none'`, plus
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The meta tag must go: a meta policy and a header
policy both apply, so a meta `connect-src 'self'` would block a cross-origin issuer whatever the header said. A library
that compiles code at runtime is blocked by `script-src`: `patches/apache-arrow@21.2.0.patch` replaces apache-arrow's
`new Function` null check with a closure of the same semantics, without which the insert-rows and create-with-rows
forms break.

**A client guard is disclosure, not authorization.** `admin/+layout.ts` and `settings/+layout.ts` refuse a non-admin
in the browser, and a person can bypass them. What authorizes is the catalog's and lineage's own check on every request
(FGA, estate-admin on the root); the guards keep the UI from offering what will be refused.

## Fetching data

The browser calls every API itself, **same-origin**, with the signed-in person's bearer; the platform's Gateway routes
each prefix to its service (`rask-services-fleet`). `$lib/upstreams.ts` is the one table of bases, the same bytes on
every estate:

| base | service | what the browser sends |
|---|---|---|
| `/api/catalog` | the catalog | its own paths below the prefix (`/api/catalog/management/v1/me`, `/api/catalog/v1/table/{id}/query`); the catalog strips the prefix itself (`rask.catalogPublicPrefix`) |
| `/api/lineage` | lineage | `/api/lineage/events?…`, `/api/lineage/admin/dlq…`; the Gateway rewrites the prefix to lineage's root |
| `/api/notifications` | the inbox | `/inbox`, `/inbox/unread`, `/inbox/seen`, `/inbox/dismiss`, `/watches`, `/watches/{project_id}`, `/prefs` under the prefix, which the inbox mounts itself (no rewrite) |

**The bearer is attached in one place.** `@rask/api/client` takes a provider the app installs once
(`setBearerProvider`, called by `auth.svelte.ts`), and `upstreamJSON` / `requestJSON` send it on every call;
`$lib/doors.ts::catalogJSON` is the catalog's door over it. The result is the shared `ApiResult` union: 0 unreachable, n
answered-n with the upstream's `{detail}`, 502 contract drift (a payload that fails its valibot schema).

**`$lib/remote.svelte.ts` is the data layer**: `query` (a cached, refreshable read), `query.live` (a cursor that
advances), and `command` (a write), in the browser. Every `*.api.ts` module (`$lib/remote/`, `$lib/data/remote/`,
`$lib/admin/remote/`, `$lib/lineage/remote/`, `$lib/live/`, `$lib/storage/remote/`) declares its reads and writes with
them, and a component holds a read as `await read()`, `read().current`, `read().refresh()`. Calling a read with the same
arguments returns the SAME instance, so a write's `void read().refresh()` updates every holder; a read settled within
1 s is reused (a burst deduplicates), and a failing read rejects and leaves `.current` as it was. Input is validated
against the function's valibot schema, defaults applied, so a command's body carries its schema's defaults.

**Liveness rides cursors, not timers.** `$lib/live/tick.svelte.ts` wraps the shared `lineageFeed`/`controlCursor` streams
of `feeds.api.ts` as `lineageTick`/`controlTick`, read through `liveRead` (`@rask/api/live`), which opens on mount and
treats a cursor's arrival as no change. Data mutations move the lineage cursor; governance mutations (grants,
warehouses, tenants) move the control cursor. A timer is right only when nothing publishes the change (a liveness probe)
or time itself is the change (a countdown).

## Workbenches — `@rask/dockview`

The dock lives at `/workbench` with two panels, `EventsPanel` (lineage's `GET /events` through the dock's one
`EventFeed`, `$lib/dock/bench.svelte.ts`) and `TablesPanel` (the very registry the catalog page renders). A panel
RENDERS THE PAGE'S COMPONENT: it is not a smaller re-implementation, and when its data differs it passes rows as a prop
rather than giving the board its own fetch. A panel with no page counterpart (`EventsPanel`) is fine.

Layout persistence goes through `$lib/dock/user-state-fetch.ts`, which maps the dock stores' two calls onto the
catalog's user-state read and write and keeps their three outcomes (`ok` / `absent` / **`unreadable`**, which must refuse
to save). **The catalog serves no user-state door** ([[LH-513]]): those calls answer 404, so no layout or named view
persists until a client keeps that state in its own release.

It is a **thin binding, not a wrapper**: consumers hold the real `DockviewApi`. Depend on **`dockview`, never
`dockview-core`**: `dockview`'s runtime entry registers ContextMenu, KeyboardDocking, AdvancedDnD, TabGroupChips and
Accessibility, and with core all five are silently absent.

**Four invariants.** Break any and the dock is wrong:

1. **Panels mount once.** `SveltePanelRenderer` calls Svelte's `mount()` in `init()` and `unmount()` only in `dispose()`;
   every move re-parents the same instance, so an interval, an `@xyflow/svelte` viewport and a subscription survive a
   drag. Anything that mounts outside `init()` breaks it.
2. **`defaultRenderer: 'always'`.** DOM-held state (scrollTop, focus, `<video>` position) does not survive a move under
   the default `onlyWhenVisible`, which removes the element from the document; `'always'` parks panels in the overlay
   container instead.
3. **Layout is per-subject, not localStorage**, with `unreadable` refusing to save: treat it as empty and the next
   autosave overwrites a workspace that is still there.
4. **The dock is dynamically imported** (~100 KB gzipped, one route); its stylesheet is imported statically in
   `app.css` under `layer(base)`, or the grid never lays out and panels stack as bare text. A dock's parent must be a
   SIZED flex item: `flex: 1 1 0` against a block parent mounts every group at zero height.

## Develop

| loop | command | what runs |
|---|---|---|
| UI against the cluster | `make dev-k3s` | port-forwards k3s Traefik to :8888 and runs `vite dev` (:5173) with `/api` proxied there |
| UI against an edge you name | `make dev` (`RASK_EDGE_URL`, default `http://localhost:8888`) | builds `@rask/ui`, then `vite dev` on :5173 |
| the design system live | `bun --cwd=frontend/packages/ui run dev` in a second terminal | `svelte-package -w` for `@rask/ui` |
| Storybook | `make storybook` | `@rask/ui`'s Storybook on :6006 |

`vite.config.ts` serves `/config.json` from `CONSOLE_CONFIG_FILE` exactly as `serve.ts` does, so a dev server signs in
against whichever issuer that file names, and runs with sign-in off without it. The image is `.docker/frontend.dockerfile`
(`make console-image` → `web-console:<tag>`, through `dagger call image --name=frontend`): a builder stage builds
`@rask/ui` and the app, the runtime stage holds only `build/` and `serve.ts`, runs as uid 1000 under tini on :3000, and
fits a read-only root filesystem.

## Gates

| command | runs |
|---|---|
| `bun --cwd=frontend run build` | `@rask/ui`'s `svelte-package`, then the app's `vite build` |
| `bun --cwd=frontend run check` | build, then `svelte-check` in every package that declares `check` |
| `bun --cwd=frontend run test` | build, then vitest in every package that declares `test` (`api`, `dockview`, `ui`, `console`) |
| `bun --cwd=frontend run lint` / `fmt:check` | oxlint / rsvelte-fmt in every package |
| `bun --cwd=frontend run knip` | the whole JS graph at once (`knip.json`) |
| `bun --cwd=frontend run test:e2e` | builds, then the console's Playwright suite |
| `make frontend-check` | check, lint, fmt:check, test and knip |
| CI | `dagger call frontend` (check, test, lint, fmt:check, every one run and the job failing if any did) and `web-e2e` (`test:e2e`) |

Lint is **oxlint** (`svelte/require-each-key: error`, `svelte/no-reactive-reassign: error`, via `@rsvelte/oxlint-plugin`);
format is **oxfmt** with tabs, single quotes, `printWidth: 100`. ESLint and Prettier are not installed.

**The Playwright suite runs the BUILT app** (`frontend/console/playwright.config.ts`) behind `e2e/edge.ts`, a Bun-native
stand-in for the Gateway: one origin that routes each `/api/*` prefix to a seed-driven mock upstream (`e2e/mock-*.ts`,
`e2e/*/mock-*.ts`) and everything else to the shipped `serve.ts`, so its CSP and SPA fallback are what the specs meet.
Vite preview is not used: it crashes under Bun's `node:http` proxy, and the host has no Node. Three origins of the one
build, one per family of specs: **auth-off** (`e2e/*.spec.ts`, `e2e/catalog/`, no `/config.json`), **estate**
(`e2e/projects/`, `e2e/settings/`, `e2e/notifications/`) and **lakehouse** (`e2e/admin/`). An auth-on origin serves
`e2e/console-config.json`, naming `e2e/mock-oidc.ts` under `/oidc`, so sign-in runs the app's real PKCE round trip;
there is no test-only bypass in the app. Ports are declared in `e2e/ports.ts` and `e2e/admin/ports.ts`. A read the
browser makes can also be stubbed per spec with `page.route`.

`tests/e2e` is not the frontend suite: it holds only the notifications inbox's two browser verify scripts, run by hand
against a deployed estate.

**TypeScript.** `strict` and `noUncheckedIndexedAccess` are on in the app (`frontend/console/tsconfig.json`);
`exactOptionalPropertyTypes` only in `@rask/api`, and it stays off in Svelte code for an upstream reason (Bits UI "union
too complex"). Validation is **valibot**.

**`derived_inert` is UPSTREAM.** Svelte 5.56 warns `Reading a derived belonging to a now-destroyed effect` from bits-ui's
dismissible layer (`watch([() => this.opts.enabled.current, () => this.opts.ref.current], …)` reading prop-boxed deriveds
while a panel closes); `patches/bits-ui@2.18.1.patch` snapshots the global layer maps, the minor instance, and the
watch itself cannot be patched without breaking it. To locate such a warning in a browser, use CDP
(`Runtime.consoleAPICalled`'s `stackTrace`), not `page.on('console')`, which Vite's client re-wraps.

Run the Svelte autofixer on every `.svelte` file you touched (`svelte:svelte-code-writer`, or the `svelte` MCP when it
connects) until it reports clean.

## Staying on-stack

Animation → **GSAP** via `{@attach}`. Charts → **LayerChart**. Graph editors → **Svelte Flow** via `@rask/flow`.
Components → `@rask/ui`. Validation → **valibot**. Sign-in → **oidc-client-ts**. A dependency that duplicates the stack is
a no; extend the stack instead.

## Where to go deeper

- `rask-styling` — tokens, `@source`, component authoring.
- `rask-services-fleet` — the Gateway routes for `/api/*` and the services behind them.
- `rask-architecture` — workspace planes and membership.
- `docs/AUTHZ.md` — what the console's guards are and are not.
