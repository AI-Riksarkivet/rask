# Design: live project picker via a control-plane read API

**Date:** 2026-06-29
**Status:** Approved (brainstorm) — pending implementation plan
**Scope:** rask monorepo (new platform service + home frontend). No operator changes.

## Problem

rask is single-tenant today. The home landing page (`components/frontends/home/src/routes/+page.svelte`) hardcodes its project picker:

```ts
const projects = [{ name: 'Default', slug: 'default', subtitle: 'The default rask workspace' }];
```

Separately, the **rask-operator** now provisions real projects: each is a cluster-scoped `Project` custom resource (`platform.rask.io/v1alpha1`) plus an isolated `project-<name>` namespace. The home picker has no knowledge of them.

We want the home page to **list the live operator projects** instead of the hardcoded `'Default'`.

## Goal and non-goals

**Goal.** The home landing page shows a live, read-only list of the operator's `Project` resources (name, team, workload, status/phase), refreshed from the cluster. Replaces the hardcoded array.

**Non-goals (explicit, deferred to later slices):**

- **Opening a project** (per-project frontend MFEs + ingress + data routing). For this slice the picker is a read-only status board; cards are not click-through.
- **Creating / deleting projects from the UI.** Projects are created by an operator/admin with Kubernetes access (`kubectl apply` of a `Project`). Site users get **read-only** visibility.
- **Per-project auth / RBAC for site users.** Out of scope.
- **Decommissioning the running single-tenant `default` deployment.** Removing `'default'` here means removing it *from the picker* and not surfacing the `/default/*` domain UI. The `default` deployment currently also *hosts the home/landing frontend and the new control-plane service*, so tearing it down needs its own plan. Tracked as a follow-up, not this slice.

## Decisions (from brainstorming)

1. **Scope:** dynamic picker only.
2. **List source:** a new **control-plane read API** (platform service) that reads `Project` CRs via the Kubernetes API. The operator stays UI-free; the frontend stays out of the k8s API.
3. **Legacy `default`:** removed from the picker. The picker shows **only** live operator projects.
4. **Read-only:** the control-plane API is GET-only; its cluster grant is read-only.

## Architecture

```
  Browser ── GET / ──► home frontend (SvelteKit, platform install)
                          │  server-side remote query()
                          ▼
                 control-plane API (FastAPI, platform install)
                          │  list_cluster_custom_object(...)
                          ▼
                 Kubernetes API ── Project CRs (platform.rask.io/v1alpha1)
                          ▲
                          │ kubectl apply (admin)
                 rask-operator provisions project-<name> namespaces
```

Three units, each independently understandable and testable.

### Unit 1 — Control-plane service (new rask brick + entrypoint)

Follows the Polylith conventions in `rask-architecture`.

- **Component brick** `components/services/controlplane` (package `controlplane`): a FastAPI router plus a small Kubernetes reader module. It owns all the domain logic; it is the runnable code.
- **Deployable entrypoint** `projects/controlplane`: a ~15-line composition via `service_kit.make_service_app(title="controlplane-api", routers=[health.router, projects.router], lifespan=default_lifespan)`. **Stateless** — no DB/Lance/Ray/S3 lifespan.
- **Workspace membership** is a two-place edit (root `pyproject.toml` `[tool.uv.workspace] members` + root `package.json` `workspaces`) plus `projects/controlplane/pyproject.toml`. (See `rask-architecture` → `references/adding-a-brick.md`.)
- **Dependency:** the official `kubernetes` Python client, declared on the `controlplane` brick — **never** on `service-kit` (which stays dependency-light).
- **Port:** 8820 (fits the 88xx fleet range; core 8801 / search 8802 / volumes 8803 / ray 8804 / orchestrator 8810 / gateway 8888).

**Kubernetes reader.**
- Config: `load_incluster_config()` in-cluster, falling back to `load_kube_config()` for local dev.
- Read: `CustomObjectsApi.list_cluster_custom_object(group="platform.rask.io", version="v1alpha1", plural="projects")`.
- The reader is a thin, injectable seam (an interface/protocol) so the router can be unit-tested against a fake that returns canned CR dicts — no live apiserver in unit tests.

**Endpoint contract.**

`GET /api/v1/projects` →

```json
{
  "projects": [
    {
      "slug": "demo",
      "name": "demo",
      "team": "team-archives",
      "workload": "htr",
      "phase": "Ready",
      "namespace": "project-demo",
      "createdAt": "2026-06-29T07:39:00Z"
    }
  ]
}
```

Field mapping from each `Project` CR: `slug`/`name` ← `metadata.name`; `team` ← `spec.team`; `workload` ← `spec.workload.type`; `phase` ← `status.phase` (empty string if absent → treated as `Pending` by the UI); `namespace` ← `status.namespace`; `createdAt` ← `metadata.creationTimestamp`. Projects are returned sorted by `createdAt` (stable order). `GET /api/v1/health` for liveness/readiness (service-kit default).

**Error handling.** A k8s API failure returns HTTP 503 with a small error body; the home frontend renders an empty state with a "couldn't reach the platform" message rather than a hardcoded fallback list. A `Project` with missing `status` is still listed (phase shown as `Pending`).

### Unit 2 — Deployment + RBAC (platform `rask` chart)

The service is **platform-level** and must outlive ephemeral projects, so it lives in the platform install (`chart/`, namespace `default` today), **not** in the operator's per-project chart.

- New `chart/templates/controlplane.yaml`: Deployment + Service (port 8820), modeled on the existing `fleet.yaml` domain-service pattern.
- A dedicated **ServiceAccount** for the control-plane pod.
- A read-only **ClusterRole** (`apiGroups: ["platform.rask.io"]`, `resources: ["projects"]`, `verbs: ["get","list","watch"]`) + **ClusterRoleBinding** to that ServiceAccount. This is the only new cluster-level grant; it is read-only and scoped to the `Project` resource.
- Chart values for image/tag/resources, consistent with the other services.

### Unit 3 — Home frontend picker

Follows `rask-frontend` conventions (server-side data via a remote `query()` + `refresh`).

- Add a remote query that fetches the control-plane API **server-side** and returns the project list. Reachability: **direct server-side fetch** to `RASK_CONTROLPLANE_URL` (e.g. `http://controlplane-api:8820`), configured like the other `RASK_*_URL` service envs. Not routed through the per-project gateway (platform concern; avoids coupling to a stack we may later decommission).
- Replace the hardcoded `projects` array in `+page.svelte` with the query result. Each card shows the project name + team/workload subtitle + a **phase status chip** (Ready / Provisioning / Pending). Empty state when there are no projects ("No projects yet — create one with `kubectl apply`").
- Cards are **not click-through** in this slice (opening is deferred). The existing GSAP reveal and styling are preserved.
- The hardcoded `'Default'` entry and the "open the default workspace" path are removed from the landing page.

## Data flow

1. User loads `/`. The home SvelteKit server runs the remote `query()`.
2. The query fetches `GET ${RASK_CONTROLPLANE_URL}/api/v1/projects`.
3. The control-plane service lists `Project` CRs from the k8s API, maps them to DTOs, returns the list.
4. The page renders one card per project with its live phase. `refresh` keeps it current.

## Testing

- **Control-plane brick (unit):** the CR→DTO mapping and sorting, tested against a fake k8s reader returning canned CR dicts (Ready, Provisioning, missing-status, empty list). No live apiserver. Plus the 503-on-k8s-error path.
- **Frontend:** rask `make check` gates (types/lint/build) for the home app; the picker renders the fetched list and the empty/error states.
- **e2e (live k3s):** with the operator deployed and `Project demo` at `Ready`, load the home page and confirm `demo` appears with phase `Ready`; delete the Project and confirm it drops from the list on refresh.

## Follow-ups (tracked, not this slice)

- Click-through: per-project frontend MFEs + ingress + routing into an isolated project stack.
- Create/delete projects from the UI (write path + auth).
- Decommission the single-tenant `default` deployment; re-home the landing frontend + control-plane service as first-class platform components.
- Watch/stream projects (SSE/websocket) instead of poll-refresh, if liveness needs improve.
