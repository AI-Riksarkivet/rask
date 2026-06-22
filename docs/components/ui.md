# UI Components

The browser UI is an **SSR SvelteKit** app (`svelte-adapter-bun` Bun server,
served behind the gateway `:8888`), being split into per-domain microfrontends
(`frontend` / `storage-frontend` / `compute-frontend`) under Turborepo, with a
separate Svelte component library developed in Storybook.

## Frontend app — `components/apps/frontend`

- **Stack:** Svelte 5, SvelteKit 2, Vite 8, `svelte-adapter-bun` (SSR Bun
  server), Tailwind 4, Bits UI. SSR on (`ssr = true`, `prerender = false`).
- **Backend calls:** go through the `@rask/api` package (`packages/api`), split
  into `ray` / `batches` / `search` / `volumes` / `types` modules, over
  same-origin `fetch('/api/...')`. The Vite dev proxy forwards `^/api(/.*)?$` to
  `VIEWER_BACKEND` (default `http://localhost:8888`) — a **regex**, not a plain
  prefix, so `/api-docs` isn't wrongly proxied.

### Routes

| Route | Purpose |
|---|---|
| `/` → `/batches` | Canonical entry (redirect). |
| `/batches` | Batch dashboard — filterable/sortable table, chunk list, Ray job + cluster summary; sync and submit chunks. |
| `/viewer/[volume]/[page]` | Document viewer — zoom/pan canvas, ALTO text overlay with line boxes/polygons, page nav, catalog metadata; honors `?line=` from search. |
| `/search` | Two-tab search: transcribed lines + catalog, with a tier filter. |
| `/browse` | Browse cached catalog volumes by tier. |
| `/jobs`, `/jobs/[id]` | Ray jobs list + job detail/logs. |
| `/overview`, `/cluster`, `/serve`, `/actors`, `/logviewer` | Ray dashboard views (events, nodes/GPU, Serve apps, actors, log tail). |
| `/api-docs` | Embeds the FastAPI Swagger UI via the proxy. |

The app shell is the grouped `@rask/ui/shell` `AppShell` / `AppSidebar`
(Compute / Documents / Batches / Storage) shared by every microfrontend;
`$lib/components/layout/ray-shell.svelte` composes it with the Ray health badge
polled every 5s. The viewer's zoom/pan + ALTO parsing live in
`$lib/canvas.ts` and `$lib/alto.ts`; there's a lightweight Svelte-5 i18n
(`$lib/i18n.svelte.ts`, English + Swedish).

## Component library — `packages/ui`

A standalone Svelte 5 + Bits UI + Tailwind 4 library (package name
`@rask/ui`), built with `@sveltejs/package` and showcased in **Storybook**
(`make storybook` → `:6006`). It ships **Button**, **Badge**, **Card**,
**Dialog**, **Sidebar**, **SortHeader** (plus `input`, `separator`, `sheet`,
`skeleton`, `tooltip`) as subpath exports (`@rask/ui/button`, `@rask/ui/badge`,
…), plus design tokens (`@rask/ui/styles/tokens.css`) and a `cn()` helper
(`@rask/ui/utils`). The **`@rask/ui/shell`** export is the shared `AppShell` /
`AppSidebar` / `nav-config` that every microfrontend imports so they all render
the same grouped sidebar with zero drift.

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
