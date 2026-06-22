# Frontend microfrontend decomposition (plan)

> Status: **planning + in-progress**. Source: MFE-decomposition workflow (4 read-only
> analyses + synthesis, 2026-06-18). Loyal to rask conventions: Bun, svelte-adapter-bun,
> valibot, `components/apps/*` explicit workspaces, `kit.paths.base` per app, shared
> `@rask/ui` lib, gateway behind the SvelteKit server.

## MFE apps (target)

| App (`components/apps/*`) | `kit.paths.base` | Routes it owns |
| --- | --- | --- |
| `compute-frontend`   | `/compute`   | overview, cluster, jobs (+ `[id]`), actors, serve, logviewer, api-docs |
| `documents-frontend` | `/documents` | search, browse, viewer/[volume]/[page] |
| `batches-frontend`   | `/batches`   | batches (hosts chunks + orchestrator submit/sync controls) |
| `storage-frontend`   | `/storage`   | **new** S3 UI ported from ra-hcp |

Notes from the code:
- **`/browse` IS the catalog UI** (`browseCatalog`/`CatalogHit`) — DOCUMENTS-facing, even though
  catalog *ingest* is a BATCHES concern. No `/catalog`, `/chunks`, `/orchestrator` routes exist.
- **Chunks + orchestrator live inside `/batches`** (`listChunks`/`submitChunk` + chunk strip).
- **STORAGE is greenfield** — only `frontend` + `runner` exist under `components/apps/` today.

## Unified sidebar groups (shadcn-svelte Sidebar)

- **Compute:** Overview, Cluster, Jobs, Actors, Logs, Serve
- **Documents:** Search, Viewer, Browse
- **Batches:** Batches
- **Storage:** S3 (new)

Icons reuse the exact lucide glyphs `ray-shell.svelte` already uses. Cross-MFE links must be
plain full-navigation `<a>` (SPA `goto` across apps errors).

## Double-sidebar risk (the user's explicit concern)

`ray-shell.svelte` is NOT a SvelteKit layout — every one of the 12 routes imports and wraps
itself in `<RayShell>`. RayShell renders **both** a top bar (logo, health badge, mode toggle,
per-page snippets) **and** a 56px left icon-rail (its own sidebar). The new shadcn sidebar must
**replace** that icon-rail; RayShell keeps only the top bar (moved into `Sidebar.Inset`'s header).

## Component audit → @rask/ui

**Move to @rask/ui (`packages/component-lib`, net-new):** `layout/sort-header.svelte` (5 routes),
`ui/badge/*` (11 routes), `ui/progress/*`, `ui/separator/*`, `ui/tooltip/*` (@rask/ui-grade but dead today).

**Dedupe (@rask/ui already exports):** `ui/button/*`, `ui/card/*` → switch frontend imports to
`@rask/ui/button` + `/card`, delete local copies (verify @rask/ui's surface matches first).

**Delete (unused):** `catalog-hit-card.svelte` (zero importers, domain-specific).
`ui/progress`, `ui/separator`, `ui/tooltip/*` are dead in-app — promote to @rask/ui rather than discard.

## Execution order (non-breaking — keep the app working at every step)

1. ✅ Phase 1 audit cleanup · ✅ Phase 2 SSR fix
2. Phase 3 — build the unified sidebar **in the current single app**, replacing ray-shell's rail
3. Phase 4 — port ra-hcp S3 UI into a `/s3` route (single app), under the Storage group
4. Phase 5 — add turborepo (`turbo.json`) over the existing Bun workspace (non-breaking)
5. Phase 6 — extract shared packages (@rask/ui additions, `@rask/api`, configs)
6. Phase 7 — split routes into the four `*-frontend` apps with `kit.paths.base`
7. Phase 8 — `microfrontends.json` + `turbo dev` proxy (dev) / gateway routing (prod) + docs
