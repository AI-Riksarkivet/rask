# Design: Openable projects (Spec I)

**Date:** 2026-06-29
**Status:** Approved (brainstorm) — pending implementation plan
**Scope:** Cross-repo — `rask` monorepo (frontend MFEs, controlplane, picker) **and** `rask-operator` (per-project chart). Builds on the project-picker work (`feat/project-picker`).

## Problem

The home picker (shipped in the project-picker slice) lists live operator `Project`s but the cards are **not click-through** — there is nothing to open. Each `project-<name>` namespace already runs a complete backend (its own `*-gateway` + domain services + postgres/rustfs/nats/ray), but the operator's per-project chart renders **no frontend and no ingress**, so a project has no reachable UI. "A picker you can't open isn't worth much."

## Goal and non-goals

**Goal.** Clicking a project in the picker opens its real rask UI (overview, storage, …) at a per-project host, served by per-project frontends that talk to that project's own gateway and show that project's data.

**Non-goals (this spec):**
- **Removing/decommissioning the default single-tenant stack.** That is **Spec II ("Strip platform to front-door only")**, sequenced *after* this — we prove projects are openable before tearing down the thing that currently works. In Spec I the default stack keeps running, untouched.
- Per-project auth / SSO. Out of scope.
- Creating/deleting projects from the UI (still `kubectl`-driven, read-only for site users).

## Decisions (from brainstorming)

1. **Topology: per-project frontends + ingress (Model A).** The operator's per-project chart renders the rask MFEs + an Ingress into each `project-<name>` namespace, each MFE pointing at that project's own gateway. Simplest routing (no cross-namespace API plumbing — each project is self-contained), true isolation, matches the operator's "complete isolated stack per namespace" design.
2. **URL scheme: host-based.** Project identity lives in the **hostname** (`<slug>.<projectDomain>`), not a path segment — because MFE base paths are build-time, so the same images must be reused across projects with the project in the host. The MFEs are rebuilt once with a fixed base `/<domain>` (dropping `/default`).
3. **`projectDomain` is configurable; nothing external.** Local dev resolves per-project hosts via a **wildcard `dnsmasq`/CoreDNS** (`*.<projectDomain>` → node IP) — entirely on-cluster/on-host, **no third-party DNS** (no `nip.io`/`sslip.io`). Prod uses a real wildcard DNS record + wildcard cert. Same code path; only the `projectDomain` value (and `http`/`https` scheme) differ.
4. **All 6 domain MFEs per project** (overview, compute, discover, storage, train, studio) — the full rask UI. ~6 small SSR pods per project.
5. **URLs are a single source of truth:** the controlplane **derives each project's `url` from its live Ingress host** (read from k8s), rather than recomputing from a duplicated domain config. `projectDomain` therefore lives **only in the operator**; the controlplane just reports what is actually deployed.

## Architecture

```
  Browser ── https://demo.<projectDomain>/overview ──► (wildcard DNS → node IP → Traefik)
                                                          │  Ingress (project-demo ns)
                          ┌───────────────────────────────┼───────────────────────────┐
                          │ /overview,/storage,… → MFE     │ /api → demo-gateway:8888    │
                          ▼                                 ▼                            │
                 per-project MFE pods (SSR)         demo-gateway → demo-* services        │
                 RASK_GATEWAY_URL=demo-gateway:8888  (postgres/rustfs/nats/ray/…)         │
                          └───────────────────────────────────────────────────────────┘

  Home picker (platform) ── card href = project.url ──► the project Ingress above
       ▲ url comes from controlplane, which reads each project's live Ingress host
```

Three components.

### Component 1 — Host-based MFEs (`rask` frontend)

- Change each of the 6 domain MFEs' build-time base from `/default/<domain>` → `/<domain>` (drop the `/default` project segment). Files: each app's `svelte.config.js` (`kit.paths.base`), `microfrontends.json` (dev proxy paths), cross-zone links (project-prefixed → `/<domain>`), and any place that hardcodes `/default`.
- The home/catch-all app stays at `/` (it is the platform landing, not per-project).
- The dormant `[project]` dynamic-base route scaffold is removed (project identity is now in the host, not the path).
- Result: the same MFE images serve at `/<domain>` under **any** host — reusable per project (and, after Spec II, as the only frontends).
- Gates: `make check` + each app's `svelte-check`; MFE images build; the existing `discover`/`overview`/etc. zones still compose under one host.

### Component 2 — Operator per-project frontends + ingress (`rask-operator`)

In `charts/project` (the per-project chart the operator renders):
- **6 frontend Deployments + Services** (one per domain MFE). Each: image e.g. `<repo>/overview:<tag>`, port 3000, `RASK_GATEWAY_URL=http://<proj>-gateway:8888` (SSR → the project's own gateway), and the `PROTOCOL_HEADER`/`HOST_HEADER`/`PORT`/`HOST` env mirroring the platform `frontends.yaml`, TCP probes.
- **One per-project `Ingress`** (`networking.k8s.io/v1`), host `<slug>.<projectDomain>`:
  - `/api` (Prefix) → `<proj>-gateway:8888`
  - `/<domain>` (Prefix) → that MFE Service:3000 (no strip; the app keeps its `/<domain>` base)
  - The project **entry surface is `/overview`** (each MFE keeps its own base, so there is no bare-root app to serve). A `/` → `/overview` redirect (Traefik redirect middleware) is a nice-to-have, not required; the picker links straight to `/overview`.
- **New operator config:** `projectDomain` (+ URL scheme `http`/`https`) and frontend image repo/tag, threaded into the chart values the operator renders. (The operator already injects `projectName`/`clusterQueue`; add these.)
- **New operator RBAC:** `networking.k8s.io` `ingresses` (create/update/delete) — it now manages Ingress objects via Helm.
- The 6 frontend Deployments carry the existing `platform.rask.io/project=<name>` label, so they fold into the existing `WorkloadReady` gate (all project Deployments Available) with no new condition.

### Component 3 — Picker click-through (`rask`: controlplane + home + @rask/api)

- **controlplane:** add `url: str` to `ProjectDTO`. For each project, read the **Ingress** in `project-<name>` (the one the operator created) and set `url = "<scheme>://" + ingress.spec.rules[0].host + "/overview"` (host is the single source of truth; `/overview` is the known entry path). If no Ingress yet (still provisioning), `url = ""` (empty → card not yet clickable). New read-only RBAC: `networking.k8s.io` `ingresses` `get/list/watch` added to the controlplane ClusterRole. Scheme via a controlplane config (`RASK_PROJECT_URL_SCHEME`, default `http` locally / `https` in prod). Unit-tested via the existing fake-reader seam (now also returning a fake Ingress).
- **@rask/api:** add `url: v.string()` to `ProjectSchema`.
- **home picker:** when `project.phase === 'Ready'` **and** `project.url` is non-empty, render the card as `<a href={project.url} data-sveltekit-reload>` (cross-host full navigation); otherwise a non-clickable card with a subtle "provisioning…" hint. Keep the phase chip.

## Data flow (clicking a project)

1. Picker SSR query → controlplane `GET /api/projects/` → for each project, controlplane reads its Ingress host → returns `{…, url: "http://demo.<projectDomain>/overview"}`.
2. User clicks the `demo` card → full navigation to `http://demo.<projectDomain>/overview` (wildcard DNS → node IP → Traefik → project-demo Ingress).
3. Ingress `/overview` → demo's overview MFE (SSR); the MFE's `/api/*` calls resolve (server-side via `RASK_GATEWAY_URL`, client-side via the same-origin Ingress `/api`) to `demo-gateway` → demo's services → demo's data.

## Config

- **`projectDomain`** + scheme: operator config only (e.g. `rask.local` + `http` locally via wildcard `dnsmasq` → node IP; `projects.rask.<...>` + `https` in prod). Nothing external.
- The controlplane reports `url` from the **live Ingress**, so there is no domain value to keep in sync between the two repos — the operator owns it, the controlplane reflects it.

## Testing

- **rask frontend:** `make check` + each app `svelte-check` after the base change; confirm MFE images build; manual/SSR check that zones compose under one host at `/<domain>`.
- **operator:** `helm template` renders 6 frontend Deployments + Services + one Ingress with host `<slug>.<projectDomain>`; a Go unit/structural test asserting the Ingress host + rule wiring; `make test`/`make lint`.
- **controlplane:** unit test url-from-Ingress mapping (Ready+Ingress → url; provisioning/no-Ingress → empty) via the fake reader.
- **live e2e (k3s):** configure a wildcard `dnsmasq` (`*.rask.local` → node IP); apply a `Project`; confirm per-project frontends + Ingress come up; browse `http://demo.rask.local/overview` and see demo's data; then from the picker, click `demo` and land in its overview. SSR 200 is not acceptance — observe the real UI with the project's data.

## Sequencing & cross-repo notes

- Component 1 (frontend base change) is a **prerequisite** for Component 2 (the operator deploys those images), and the rebuilt MFE images must be rebuilt + re-imported before the operator e2e.
- This spec builds on `feat/project-picker` (controlplane + home picker). That branch should be landed (or this branch rebased onto it) before/at merge.
- **Spec II (follow-up):** strip the platform install to front-door only (home picker + controlplane + platform gateway), decommissioning the default batch backend + `/default` frontends + ingress. Not in this spec.
