# UI Components

The browser UI is a **SvelteKit** SPA, with a separate Svelte component library
developed in Storybook.

## Frontend app — `components/apps/frontend`

- **Stack:** Svelte 5, SvelteKit 2, Vite 7, `adapter-static` (SPA with
  `index.html` fallback), Tailwind 4, Bits UI. Fully client-rendered
  (`ssr = false`, `prerender = false`).
- **Backend calls:** a single `$lib/api.ts` client over same-origin
  `fetch('/api/...')`. The Vite dev proxy forwards `^/api(/.*)?$` to
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

The app shell (`$lib/components/layout/ray-shell.svelte`) is an icon-rail nav with
a Ray health badge polled every 5s. The viewer's zoom/pan + ALTO parsing live in
`$lib/canvas.ts` and `$lib/alto.ts`; there's a lightweight Svelte-5 i18n
(`$lib/i18n.svelte.ts`, English + Swedish).

## Component library — `packages/ui`

A standalone Svelte 5 + Bits UI + Tailwind 4 library (package name
`@rask/ui`), built with `@sveltejs/package` and showcased in **Storybook**
(`make storybook` → `:6006`). It currently ships three components — **Button**,
**Card**, **Dialog** (compound) — plus design tokens and a `cn()` helper.

!!! note "Currently standalone"
    The frontend app does **not** import `@rask/ui` today — it ships its own
    local `$lib/components/ui/*` set (a small shadcn-svelte-style collection). The
    library is built and Storybook-showcased, but not yet consumed by the app.

## Toolchain

- **Bun only** — `bun` / `bunx`; `npm`/`npx`/`pnpm` are not on PATH.
- Prettier (tabs, single quotes, `printWidth: 100`), ESLint (flat config),
  `svelte-check`.

!!! tip "Dev server binds loopback-only"
    `make viewer-frontend` starts Vite without `--host`, so it binds `127.0.0.1`
    only. Pass `--host` to reach it over IPv4 localhost or the LAN. Only the
    backend (`make viewer`) binds `0.0.0.0:8888`.
