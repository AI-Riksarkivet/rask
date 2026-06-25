# UI Components

The browser UI is a set of **SSR SvelteKit** apps (`svelte-adapter-bun` Bun
servers, behind the gateway `:8888`) composed as routing-based microfrontends
under Turborepo — the dev composition proxy on `:3024`, the k3s Ingress in prod —
with a separate Svelte component library developed in Storybook. There are seven
apps: a catch-all that owns `/` plus six domain apps (overview / compute /
discover / storage / train / studio), each pinned to base `/default/<domain>`.

## Catch-all app — `components/apps/frontend`

- **Stack:** Svelte 5, SvelteKit 2, Vite 8, `svelte-adapter-bun` (SSR Bun
  server), Tailwind 4, Bits UI. SSR on (`ssr = true`, `prerender = false`).
- **Role:** the platform home — it owns `/` only (a floating GSAP glass navbar +
  project picker; no sidebar, no data layer). The domain routes below live in the
  per-domain apps, not here. Its Vite dev proxy still forwards `^/api(/.*)?$` to
  `VIEWER_BACKEND` (default `http://localhost:8888`) — a **regex**, not a plain
  prefix, so `/api-docs` isn't wrongly proxied.

### Domain apps and their routes

The six domain apps render the same shared `@rask/ui/shell` `AppShell` sidebar
(grouped Overview / Compute / Discover / Storage / Train / Studio, project-prefixed
under `/default/<domain>`). Their backend reads go through the `@rask/api` package
(`packages/api`) via server-only remote `query()` functions; each data app carries
a `src/hooks.server.ts` (`makeGatewayHandleFetch`) that routes SSR `/api/*` to the
in-cluster gateway. Notable routes:

| App | Routes |
|---|---|
| **overview** (`overview-frontend`) | Project landing / batch dashboard — filterable/sortable table, chunk list, Ray job + cluster summary; sync and submit chunks. |
| **compute** (`compute-frontend`) | `cluster`, `jobs` / `jobs/[id]`, `actors`, `serve`, `logviewer` (Ray dashboard views), `api-docs` (embeds FastAPI Swagger UI via the proxy). |
| **discover** (`discover-frontend`) | `viewer/[volume]/[page]` (zoom/pan canvas, ALTO overlay, page nav, catalog metadata; honors `?line=`), `search` (transcribed lines + catalog, tier filter), `browse` (cached catalog volumes by tier). |
| **storage** (`storage-frontend`) | S3/volumes browser. |
| **train**, **studio** | Scaffolds for the training + design workspaces. |

The discover viewer's zoom/pan + ALTO parsing live in its `$lib/canvas.ts` and
`$lib/alto.ts`.

## Component library — `packages/ui`

A standalone Svelte 5 + Bits UI + Tailwind 4 library (package name
`@rask/ui`), built with `@sveltejs/package` and showcased in **Storybook**
(`make storybook` → `:6006`). It ships **Button**, **Badge**, **Card**,
**Dialog**, **Sidebar**, **SortHeader**, **Table** (plus `alert-dialog`,
`avatar`, `checkbox`, `collapsible`, `dropdown-menu`, `progress`) as subpath
exports (`@rask/ui/button`, `@rask/ui/badge`, …), plus design tokens
(`@rask/ui/styles/tokens.css`) and a `cn()` helper (`@rask/ui/utils`). The
**`@rask/ui/shell`** export is the shared `AppShell` / `AppSidebar` /
`nav-config` that every domain app imports so they all render the same grouped
sidebar with zero drift.

!!! note "Consumed via `workspace:*`"
    Every app consumes `@rask/ui` via `workspace:*` — the styled components live
    in the library, not in the apps. A consuming app must add a Tailwind
    `@source` pointing at `packages/ui/dist` in its `app.css`, or the library's
    classes render unstyled (Tailwind 4 doesn't scan `node_modules`).

## Toolchain

- **Bun only** — `bun` / `bunx`; `npm`/`npx`/`pnpm` are not on PATH.
- Prettier (tabs, single quotes, `printWidth: 100`), ESLint (flat config),
  `svelte-check`.

!!! tip "Dev server binds loopback-only"
    `make viewer-frontend` starts Vite without `--host`, so it binds `127.0.0.1`
    only. Pass `--host` to reach it over IPv4 localhost or the LAN. Only the
    backend (`make viewer`) binds `0.0.0.0:8888`.
