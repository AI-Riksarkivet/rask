# Design: Project-first URLs (drop `/default/`, host carries the project)

**Date:** 2026-06-30
**Status:** Approved (brainstorm) — pending implementation plan
**Repos:** rask (frontends + `@rask/ui` + single-tenant chart) and rask-operator (per-project chart ingress).

## Problem

Every per-project URL is served under a frozen `/default/` segment — e.g. `demo2.localhost/default/overview` — even though the project's identity is already the **host** (`demo2.localhost`). The `/default/` segment is a vestige of the original single-tenant, single-host, path-based IA (one project named `default`). After the host-based per-project pivot it is redundant and confusing: it reads as "the default project" when it is just a static base prefix shared by every project. We want clean, project-first URLs where the **host carries the project** and the **path carries the domain**: `demo2.localhost/overview`, `demo2.localhost/storage`, and bare `demo2.localhost/` → overview.

## Decisions (from brainstorming)

1. **Option 1 — host carries the project; path carries only the domain.** The SvelteKit base changes from a static `/default/<domain>` to a static `/<domain>`. Chosen over echoing the project in the path (Option 2) or single-host path-based projects (Option 3), both of which reintroduce the deliberately-deferred dynamic-base problem and add no value over the host.
2. **Static base, shared images preserved.** `/<domain>` is still a build-time constant, identical for all projects, so the one-image-per-MFE model is untouched. The `/<domain>` segment keeps the six apps distinct in the dev turbo proxy; the host keeps projects distinct in prod. No dynamic base, no per-project builds.
3. **Bare host → overview via ingress redirect.** The home/picker MFE is *not* deployed per project (only the six domain MFEs are), so `<project>.host/` has no owner. A Traefik redirect on the per-project ingress sends `/` → `/overview`. No extra app.
4. **No project segment in the path.** Per-project API is already scoped by deployment (each MFE's `RASK_GATEWAY_URL` points at its own project gateway), so the URL needs no project segment for correctness — only the host.

## Architecture

```
 Before:  demo2.localhost /default/overview        (host=project, but path repeats a frozen "default")
 After:   demo2.localhost /overview                (host=project, path=domain)
          demo2.localhost /                         → 302 /overview (ingress redirect)

 base (build-time, shared image):  /default/<domain>  ->  /<domain>
 nav hrefs (per host):             /<project>/<domain> (fed "default")  ->  /<domain>
 ingress route:                    /default/<domain>  ->  /<domain>
```

The MFE knows nothing about the project from the URL; the host routes to the right project's services and each MFE talks to its own project gateway. The sidebar nav, intra-app links, dev proxy zones, and ingress all drop the project-carrying segment.

## Components

### Component 1 — MFE base path (rask frontends)
All six domain apps (`overview, compute, discover, storage, train, studio`): change `kit.paths.base` in `svelte.config.js` from `/default/<domain>` to `/<domain>`, and update every in-app reference to the old base — the `+layout.ts` (load/base handling), `+layout.svelte`, and `+page` files that hardcode `/default/...` (links, redirects, asset refs). Each app continues to serve its routes under its own `/<domain>` base.

### Component 2 — Shared nav (`@rask/ui`)
`packages/ui/src/lib/shell/nav-config.ts`: `navMain()` currently builds `/${project}/<domain>` hrefs (and `match`); change it to build `/<domain>` hrefs (drop the project segment) and update the `match` predicates accordingly. Update all callers that pass `'default'` (the per-app `+layout.svelte` shells) to the new signature. Rebuild `@rask/ui` (`dist/`) so consumers pick it up.

### Component 3 — Home / picker + dev proxy (rask `home`)
- `components/frontends/home/microfrontends.json`: dev turbo-proxy zone paths `/default/<domain>` → `/<domain>` (and `:path*`).
- `components/frontends/home/src/lib/components/top-nav.svelte` and the picker: intra-host links to `/<domain>`; cross-project links remain **host-based** (`<project>.<projectDomain>`), unchanged in shape.

### Component 4 — Ingress routes (both charts)
- rask-operator `charts/project/templates/ingress.yaml`: route `path: /<domain>` (was `/default/<domain>`) → `<proj>-<domain>:3000`; add the bare-`/`→`/overview` redirect (Traefik middleware / annotation on this ingress).
- rask `chart/templates/ingress.yaml` (single-tenant, currently gated off): mirror the `/<domain>` paths so the gated path stays consistent if ever re-enabled.

### Component 5 — Bare-host redirect
On the per-project ingress, a Traefik `redirectRegex`/middleware (or equivalent ingress annotation) maps a bare host request (`/`) to `/overview`, so `<project>.localhost/` lands on the project overview.

## Data flow (after)
1. Browser hits `demo2.localhost/` → ingress redirect → `demo2.localhost/overview`.
2. `demo2.localhost/overview` → ingress `/overview` → `demo2-overview:3000` (base `/overview`) → SSR overview.
3. Sidebar nav hrefs are `/storage`, `/compute`, … → ingress routes each to its MFE on the same host. API calls go to the project gateway via `RASK_GATEWAY_URL`.

## Testing
- **Frontend gates (rask-frontend):** `make check` (svelte-check / ty / lint) green across the six apps + home + `@rask/ui`; no `/default` left in `components/frontends` or `packages/ui` source.
- **Dev composition:** the turbo microfrontends proxy (`:3024`) serves each app at `/<domain>`; cross-app nav works.
- **Live k3s (browser-verified):** rebuild + import the MFE images and the operator; for two projects, `<project>.localhost/overview` and `/storage` render, sidebar nav lands on `/<domain>` pages, **bare `<project>.localhost/` redirects to `/overview`**, and no URL contains `/default`. Observed in a browser (Playwright), not just HTTP 200.

## Risks / open items
- **Image rebuild required.** The base is compiled in, so all six MFE images must be rebuilt + re-imported to k3s and the per-project frontends rolled (the chart already has the `checksum/config` roll for config, but image changes need a fresh tag or rollout). The plan states the rebuild/import/rollout steps.
- **Stale base references.** Any missed `/default/...` literal (asset, link, redirect) 404s. The "no `/default` in source" grep gate guards this.
- **Traefik redirect shape.** The bare-host redirect must not shadow `/api` or `/<domain>` routes — scope the regex to exactly `/` (and empty path). The plan pins the exact middleware.

## Non-goals
- Project name in the path (Option 2/3); dynamic/runtime base; per-project home/picker MFE; deploying the catch-all `home` app per project; any API path change (`/api/*` is unaffected).
