---
title: ra-hcp → rask Migration Plan
description: Absorbing ra-hcp (HCP/S3 control plane + SDK) into rask along its Polylith seams — phased, green at every step.
icon: lucide/git-merge
status: new
---

# ra-hcp → rask Migration Plan

Plan produced by a 5-agent planning workflow (2026-06-23) — read-only investigation of both repos, synthesized into phases that keep rask **green at every step**.

!!! abstract "One sentence"

    ra-hcp splits into four streams that land along rask's existing Polylith seams: its **SDK libs** become `packages/*`, its **stateless HCP/S3 FastAPI backend** becomes a viewer-free `components/services/hcp_api` (+ `packages/hcp-mapi`, no DB), its **CLI** becomes `components/apps/hcp`, and only the **bucket/object-browser** is harvested into the existing `storage-frontend` MFE — **~90% of ra-hcp's frontend (tenants, namespaces, users, MAPI admin, login) is deliberately dropped.**

## The key framing — a harvest, not a wholesale port

rask **does not** take on ra-hcp's tenant/JWT/MAPI identity model. **A rask `project` is an app-workspace axis, not an HCP tenant (infra isolation).** If per-project storage isolation is ever needed, it's per-project S3 *prefixes* governed by rask RBAC — never an HCP tenant/namespace per project. The backend is **stateless** (state lives in HCP + S3), so the whole subsystem composes like `volumes_api`/`search_api`/`ray_api` and **touches none of `core`/`alembic`/the batches table** (except the optional Phase 8 RBAC table).

## Target topology (where each piece lands)

| New brick | From ra-hcp | Layer |
|---|---|---|
| `packages/tracker` | rahcp-tracker (SQLModel transfer ledger) | lib |
| `packages/validate` | rahcp-validate (Pillow image checks) | lib |
| `packages/hcp-client` | rahcp-client (async HCP SDK + bulk engine) | lib |
| `packages/hcp-mapi` | backend's lib half (mapi/query/kv-cache/auth-utils) | lib |
| `packages/etl` | rahcp-etl (NATS pipeline) — lands **dormant** | lib |
| `packages/storage` *(grown)* | rahcp-iiif async half + the **async S3 surface** (`async_s3.py`, aioboto3 behind `StorageProtocol`) | lib |
| `components/services/hcp_api` | the stateless FastAPI backend, via `make_service_app` (mirrors `volumes_api`) | service |
| `components/apps/hcp` | rahcp-cli (Typer, `rahcp`→`hcp`) | app/CLI |
| `projects/{hcp-api,hcp}` | deployables (mirror `volumes-api`/`runner`) | deployable |
| `storage-frontend` *(grown)* | ra-hcp's **buckets domain only** (read-only first) | MFE |

Naming: drop the `rahcp-`/`ra-` prefix; `HCP_*`/`S3_*`/`MAPI_*`/`REDIS_URL` env → `RASK_*` aliases on the single `service_kit.config.Settings`. OTel/JWT/Redis machinery lives in `hcp-mapi`/`hcp_api`, **never** in dependency-light `service-kit`.

## Phases (green at every step)

| # | Phase | Depends on | ∥ | Gate (proof rask stays green) |
|---|---|---|---|---|
| 1 | Leaf SDK libs (`tracker`, `validate`) | — | ✅ | their pytest green; no existing brick changes |
| 2 | IIIF async merge into `packages/storage` | — | ✅ | storage pytest green (sync + new async); `download_iiif.py` still runs |
| 3 | `hcp-client` SDK + `hcp` CLI | 1, 2 | ❌ | `uv run --package hcp hcp --help`; their pytest green |
| 4 | `hcp-mapi` lib + **async S3 surface** in storage | 1 | ❌ | hcp-mapi + storage pytest green (moto); single `derive_hcp_creds` |
| 5 | `hcp_api` service + deployable + gateway `/hcp` | 4 | ❌ | service boots (NullStore, MAPI off); gateway routes `/api/v1/hcp/*`→:8805; core/volumes regress green |
| 6 | `storage-frontend` buckets harvest (**read-only**) | volumes_api (exists) | ✅ | storage-frontend `check` green; `/default/storage/*` serves; base-aware links |
| 7 | write/upload features (backend-gated) | 5, 6 + decisions | ❌ | moto write tests; upload/folder/delete behind the proxy |
| 8 | **Identity + project RBAC** (decision-gated) | human decision | ❌ | `dagger call migrate-up` clean incl. `project_members`; auth off by default |
| 9 | ETL / NATS (dormant now) | 1 | ✅ | `etl` pytest green; imported by nothing running |

**Critical path:** `1→2→3`, `1→4→5→7`, with **6 parallel to 3–5** (read-only buckets needs only the existing `GET /api/volumes/objects`), 8 last + flagged, 9 deposited whenever convenient.

## Open decisions — these need **your** call (build gates on them)

1. **Async-S3 placement** *(load-bearing)* — grow `packages/storage` with `aioboto3` behind `StorageProtocol` (keeps "no boto3 directly" honest; adds aioboto3/aiobotocore to `uv.lock`) **vs** put adapters in `hcp-mapi` (splits S3 logic). *Default: storage.*
2. **Presigned plane vs direct-S3** — migrate ra-hcp's full presigned `HCPClient.s3` (requires a running `hcp_api`) vs bulk-engine-only over `storage.s3_client`. Determines whether the CLI/frontend-writes hard-depend on `hcp_api`.
3. **Redis** — accept as an **optional dep of `hcp_api` only** (kv degrades to NullStore) — contradicts CLAUDE.md's platform-wide "No Redis" — vs drop the cache layer.
4. **MAPI scope** — keep all ~150 HCP-vendor admin routes gated on `RASK_MAPI_ENABLED`, or ship only S3 + IIIF + presign + auth (the `MAPI_ENABLED=false` path already exists).
5. **Identity source** *(BLOCKING for any RBAC)* — real OIDC/Riksarkivet SSO, ra-hcp-style passthrough-credential JWT, or app-local accounts. rask has no auth today and **cannot punt to HCP** (it's the system of record for projects).
6. **Project ≠ tenant** — confirmed different axis. Single-org vs multi-org: does the planned `projects` table need an `org_id` FK *before* it ships? (rask is implicitly single-org; ra-hcp is multi-tenant.)
7. **`@rask/ui` primitive gap** — `table`/`checkbox`/`alert-dialog`/`progress` aren't in `@rask/ui` yet; promote them (consistent, ripples to all MFEs) vs vendor temporarily in storage-frontend (flagged drift).
8. **Overlap retirement** — delete `components/scripts/download_iiif.py` once `hcp iiif download` exists?

## Cross-cutting risks

- **Two-place workspace membership** is the silent-failure trap: each new Python brick needs root `pyproject.toml` `[tool.uv.workspace] members` + `testpaths` + isort `known-first-party` (+ `projects/<name>` if deployable). Forget one → uv resolution fails *quietly*.
- **First-time `aioboto3`/`aiobotocore`/`redis`/`nats-py`/`pyjwt`/`psycopg`** in `uv.lock` can clash with rask's `boto3`/`botocore`/`asyncpg` pins — `uv sync` + run the suite after each lib lands.
- **`service-kit` must stay dependency-light** — one accidental OTel/JWT/Redis import there ripples to volumes/search/ray.
- **Gateway longest-prefix collision** — ra-hcp mounts S3 at `/api/v1/buckets` (no sub-prefix), which shadows the `(prefix, core)` catch-all unless re-prefixed under `/hcp` (insert the route tuple *before* the catch-all).
- **`core`/`alembic`/batches** is the most sensitive surface — only Phase 8 touches it, via Alembic, preserving the orchestrator single-writer invariant. Keep the HCP/S3 subsystem stateless so nothing else touches `core`.
- **`paths.base=/default/storage` link rot** + **Svelte 5 SSR strict** (browser globals in `onMount`/`$effect` only) + **zod→valibot semantic gaps** — validate every ported `.svelte` with the svelte MCP autofixer.
- **Dedup is non-negotiable** — `derive_hcp_creds` vs `derive_s3_keys`, sync vs async IIIF manifest helpers must collapse to one source (partial dedup is the "sloppiness" the user calls out).

> Full structured plan (per-phase steps + targets) is in the planning workflow output; this doc is the durable summary. Memory: `project-ra-hcp-migration-plan`.
