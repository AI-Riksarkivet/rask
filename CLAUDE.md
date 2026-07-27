# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Engineering principles (read first)

**This is state-of-the-art work. Do things properly — no shortcuts, no band-aids.**

- **Fix root causes, not symptoms.** Never paper over an app/library bug at an outer
  layer (proxy, ingress, env hack, wrapper) when the defect belongs in the app or its
  build. Workarounds that "make it pass" are not acceptable as final fixes — at most a
  clearly-labelled temporary step that is then replaced by the real fix.
- **A fix must travel with the code.** Prefer fixes that keep a component correct on its
  own (behind any proxy, in any environment), over fixes that depend on surrounding infra.
- **Verify like it ships.** SSR returning 200 is not "working" — exercise the real user
  path (a browser for UI, the actual client for APIs). Assume nothing is fixed until it's
  been observed working end-to-end.
- **Use the proper workflow.** Non-trivial changes go through the superpowers flow
  (brainstorm → spec → plan → subagent-driven-dev with reviews + TDD), not ad-hoc edits.
- **No silent scope-cuts.** If you bound coverage, sample, or defer something, say so
  explicitly. Don't let "partially done" read as "done".

## Toolchain rules

- **JS/TS uses Bun exclusively.** Use `bun` / `bunx`. `npm`, `npx`, `pnpm`, `pnpx` are not on PATH and MCP install commands assume `bunx`.
- **The JS/TS plane lives in `frontend/`** — its own bun + Turborepo workspace root (its own `package.json`, `bun.lock`, `turbo.json`). Every bun/turbo call is **scoped to it**: `bun --cwd=frontend run <task>`, `bunx turbo --cwd=frontend run <task>`. Use the `--cwd=` form — `bun --cwd <path>` with a space silently no-ops.
- **JS/TS lint + format is oxlint + oxfmt**, not ESLint/Prettier (both deleted). Svelte support comes from `@rsvelte/oxlint-plugin` (lint) and `@rsvelte/fmt` (format); configs live at `frontend/.oxlintrc.json` and `frontend/.oxfmtrc.json`. `lint` / `fmt` / `fmt:check` are **per-package turbo tasks**, run from `frontend/`.
- **Python uses uv** (3.13) with Ruff + `ty` for type-checking. Run Python via `uv run <cmd>`; type-check via `uvx ty check`.
- Identifiers and env vars carry **no `ra-`/`ra_` prefix** (legacy from the ra-batch migration). Env vars are `RASK_*`.

## Common commands

| Goal                         | Command                                                                  |
| ---------------------------- | ------------------------------------------------------------------------ |
| First-time setup             | `make install` (= `bun --cwd=frontend install` + `uv sync`)              |
| Build everything             | `make build`                                                             |
| Run tests (excludes slow)    | `make test` (= `uv run pytest -m "not slow"`)                            |
| Run all tests incl. slow     | `make test-slow` (needs real models / a GPU)                             |
| Single Python test           | `uv run pytest services/core/tests/test_pipelines.py::test_name`        |
| Filter by name               | `uv run pytest -k <pattern>`                                             |
| Skip slow tests              | `uv run pytest -m "not slow"`                                            |
| Format + lint + typecheck    | `make check` (= `make fmt` + `make lint` + `make typecheck` + `make knip`) |
| Frontend type-check only     | `bun --cwd=frontend run check` (one zone: `bunx turbo --cwd=frontend run check --filter=home`) |
| Storybook for `@rask/ui`     | `make storybook` (→ `:6006`)                                             |
| Bootstrap Claude Code config | `make claude-bootstrap`                                                  |

### Run the app locally

```bash
make ray-up            # local Ray head on :6379, dashboard :8265
make serve-up          # deploy /transcribe + /htrflow on Ray Serve
make dev-micro         # the fleet: gateway :8888 + core-api :8801 + search :8802 +
                       #   volumes :8803 + ray :8804 + controlplane :8820 (via scripts/dev-micro.sh)
make home   # SvelteKit dev server, proxies /api → :8888 (the gateway)
```

The frontend's Vite proxy targets `:8888` — the **gateway**. `scripts/dev-micro.sh` is
the source of truth for the fleet's process list and ports.

`make serve-down` / `make ray-down` to tear down. Catalog pipelines: `make catalog-index`, `make harvest-ead`.
(The app database, Alembic migrations and the `pg-*`/`viewer` targets died at P7a — the only relational
stores left are the chart-managed lineage (AGE) and OpenFGA databases.)

## Repository layout

Two **language-pure planes** — **don't blur them**. Python lives at the repo root (`packages/` + `services/`); the entire JS/TS estate lives under `frontend/`, its own bun + Turborepo workspace root. (There is deliberately **no Polylith-style `projects/` layer** — it was removed 2026-07; deployables build straight from the root uv workspace via `uv sync --package <name>`, one dockerfile per deployable in `.docker/`.)

- `packages/` — reusable **Python** libraries, **no entrypoints**. uv workspace members.
  - `packages/storage` — `FSSource/Sink`, `S3Source/Sink`, `IIIFCachedSource`, `iter_keys`, `s3_client`
  - `packages/service-kit` — shared **platform library**: `make_service_app` app factory, `Settings`/config, exceptions, middleware, `get_settings`/`SettingsDep`, the injectable lifespan. Dependency-light (no lancedb/ray/sqlmodel).
  - `packages/ray-kit` — Ray Job SDK + dashboard wrapper (schemas, `build_client`, `RAY_TRANSIENT_ERRORS`, the dashboard service). Used by `ray-api`.
  - `packages/tracker` — pluggable transfer-state tracking (SQLite / Postgres backends)
  - `packages/validate` — pre-upload image validation (TIFF/JPEG/PNG corruption detection + pluggable rules)
- `services/` — runnable **Python** code. **The old monolithic `viewer` service is gone**, and so is the whole batches/orchestrator plane (P7a compute-plane cutover — see `docs/architecture/lance-ns-merge.md` P7):
  - `services/gateway` — reverse proxy on `:8888` (the frontend's proxy target). Path-routes `/api/*` to the services below (longest-prefix-first) plus the lance-plane rows (`/api/catalog`, `/api/lineage`, `/api/produce`, `/api/train`, `/api/media/*`); owns no state. Upstreams are env-overridable (`RASK_CORE_API_URL` :8801, `RASK_SEARCH_API_URL` :8802, `RASK_VOLUMES_API_URL` :8803, `RASK_RAY_API_URL` :8804, `RASK_CONTROLPLANE_URL` :8820).
  - `services/core` — the **core domain package** (package `core`), since P7a a **transitional husk**: health + the EAD catalog search (`core/lifespan.py`, `services/discover/catalog`, the catalog endpoint, `main.py` app factory for tests). The batches table, Alembic lineage, orchestrator loop, submission, and S3-sync are **deleted**. Composed by `services/core_api` (`:8801`); retires with the R6/R20 media wave (lance `search` over a catalog-governed EAD table).
  - `services/{volumes_api,search_api,ray_api}` — independent, **viewer-free** services (`:8803`/`:8802`/`:8804`): S3/IIIF image+ALTO proxy (stateless); Lance `lines` FTS + S3 thumbnails (owns a lines-only lifespan); Ray dashboard introspection (`/api/ray/*`) + the `/api/serve/*` proxy (thin shell over `ray-kit`). Each depends only on `service-kit` + its own libs — no `core`, no DB.
  - `services/medallion` — the lakehouse cascade (producer + movers). Its producer owns the **P7a IIIF→raw page head**: `POST /ingest-iiif` harvests a volume from the IIIF Image API into the raw blob-v2 page-image Lance dataset and emits the ONE raw-write OpenLineage event through `packages/lineage-kit`; `/raw-arrival` fires the `medallion.raw` cascade (raw→bronze media promotion, then the P7b HTR movers).
- `frontend/` — the **JS/TS plane** and its own workspace root: `package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/` (the shared favicon source). The only JS outside it is `tests/e2e`, a standalone Playwright project with its own lockfile (`make e2e`).
  - `frontend/microfrontends/home` — the **catch-all** microfrontend (package `home`) owning `/` (the platform home), SvelteKit 2 + Svelte 5, **SSR** via `svelte-adapter-bun` (a real Bun server: `bun ./build/index.js`). Vite dev proxy sends `/api` → the gateway on `:8888`. The frontend is **already decomposed into 7 SvelteKit microfrontend zones** under Turborepo — this catch-all plus 6 domain apps (`{overview,compute,discover,storage,train,studio}`), each pinned to base `/default/<domain>` and composed by the `:3024` microfrontends proxy in dev (k3s Ingress in prod). Every domain app renders the **shared `@rask/ui/shell` AppShell sidebar** (grouped, per-domain) — see `docs/architecture/frontend-microfrontends.md`.
  - `frontend/packages/ui` — Svelte 5 + Bits UI + Tailwind 4 component library (`@rask/ui`; the former `component-lib`) w/ Storybook 10 (`@storybook/svelte-vite`). The shared design system every microfrontend imports via `workspace:*` — **styled components live here, not in the apps** (apps only supply theme tokens in their `app.css` + an `@source` pointing at `frontend/packages/ui/dist`). Subpath exports: `@rask/ui/{button,badge,card,dialog,dropdown-menu,avatar,collapsible,table,checkbox,alert-dialog,progress,sort-header,sidebar,utils}` + **`@rask/ui/shell`** (the shared `AppShell` + grouped `AppSidebar` + `nav-config` — so every app renders the _same_ sidebar, zero drift). See `docs/architecture/frontend-microfrontends.md`.
  - `frontend/packages/api` — `@rask/api`, the shared frontend data layer (typed gateway client + types, split by domain). JIT TS: apps import the source directly, no build step.
  - `frontend/packages/zone-contract` — `@rask/zone-contract`, the cross-zone link guard (a cross-zone `<a>` must carry `data-sveltekit-reload`). It is a **vitest test**, not a lint rule — the retired ESLint rule was ported here when the frontend moved to oxlint.
- `scripts/` — **all** dev/ops scripts, shell + Python: one-shot setup / debug tools (`harvest_ead`, `index_catalog`, `download_*`, `smoke_s3`, the self-contained Ray jobs `ray_stage_job`/`ray_train_job`/`ray_iiif_ingest_job`, …) plus `dev-micro.sh` and `k3s-install.sh`. **No production-state-changing CLIs** — ingestion and the cascade run through the HTTP services (the medallion producer + movers).

- `runners/` — **sealed model environments, NOT workspace members.** `runners/htr` holds the Ray Data HTR pipeline (`src/runner`) *and* the model actors (`src/htr`) in one project with its **own `pyproject.toml` and own `uv.lock`**. Matched by no glob, so torch/htrflow/ultralytics/transformers never enter the fleet's resolution (root lock 200 → 145 packages; fleet tests ~32 min → ~6 s). `storage` is a **path** dep. Its tests are invisible to the root pytest — `make test` runs them separately; its images build from **its** lock; it carries its own ruff config (ruff resolves the nearest pyproject). Ray entrypoint: `uv run --project runners/htr runner`, overridden in-cluster by `RASK_RUNNER_CMD=runner`.

**Workspace membership is globbed, per plane** — the directories are language-pure, so every child carries the right manifest:

- `pyproject.toml` → `[tool.uv.workspace] members = ["packages/*", "services/*"]`
- `frontend/package.json` → `workspaces = ["microfrontends/*", "packages/*"]` (relative to `frontend/`)

A new Python library/service or a new zone is picked up by the glob — but it **must** ship a `pyproject.toml` (Python) or a `package.json` (JS), or its plane silently drops it.

Deployables are just workspace members with a dockerfile: `.docker/<name>.dockerfile` runs `uv sync --frozen --package <name>` against the **root** `uv.lock` (the deployable set is `gateway`, `core-api`, `volumes-api`, `search-api`, `ray-api`, `runner`).

## Architecture (image → ALTO XML)

`rask` is a distributed HTR pipeline for the Swedish National Archives. See `docs/architecture/system-overview.md` for the full diagrams. Key facts that aren't obvious from any single file:

- **Runner is the engine.** `runners/htr` submits one Ray Data pipeline per CLI invocation and blocks on `.materialize()`. It does not run a long-lived service.
- **Ray Serve persists across job submissions.** TrOCR weights stay warm in `/transcribe` (3 replicas × 0.99 GPU). The pipeline's `TranscribeViaServe` actor is CPU-only and calls Serve synchronously over a handle. `make serve-up` deploys this independently of any job.
- **Two pipeline shapes:**
  - **Actor-per-stage** — `PageLoader → Layout → Lines → TranscribeViaServe → AltoExport → AltoWriter`. Uses GPU for YOLO regions/lines (0.001 GPU each) and TrOCR via Serve.
  - **`/htrflow`** — collapses Layout+Line+Transcribe+Alto into a single 1-replica CPU Serve deployment. Used when actor fan-out isn't worth it for a batch shape.
- **GPU sizing is hardcoded** in `runners/htr/src/runner/pipeline.py` for a 3-GPU node. Changing target hardware means editing that file.
- **No auth, no app middleware.** The services assume localhost / trusted network. The frontend hits `/api/*` on the **gateway** (`:8888`), which path-routes to the per-domain services; `/api/ray/*` and the `/api/serve/*` proxy are served by the standalone **ray-api** service (over `ray-kit`). SSR `load`/remote functions reach the gateway server-side via an absolute base URL (`RASK_GATEWAY_URL`); client code uses the relative `/api/*` proxy. The gateway sits **behind** the SvelteKit Bun server (it does not serve the SPA shell).
- **State surface:** the lakehouse (Lance datasets on RustFS S3) governed by the catalog, plus the chart-managed **lineage (AGE)** and **OpenFGA** Postgres databases (CloudNativePG). **The app's own relational DB is gone** (P7a): no batches table, no Alembic, no `DATABASE_URL` in the fleet. **No Redis, no compose stack**; events ride Dapr pub/sub on NATS JetStream. The Helm chart in `chart/` is the single deploy artifact for both local k3s and production — in-cluster CloudNativePG (`Cluster`), RustFS operator (`Tenant` → `rask-rustfs-io:9000`), and KubeRay are gated by `cnpg.enabled`/`rustfs.enabled`/`ray.enabled` values toggles; each toggle gates both the operator subchart and its custom resource. Local deploy: `make k3s-install` (one-time) → `make k3s-build` → `make k3s-import` → `make k3s-up`; tear down with `make k3s-down` / `make k3s-purge`. See `docs/architecture/deployment.md` and `chart/README.md`.
- **Observability (optional, `observability.enabled`):** Vector → GreptimeDB (on RustFS S3 bucket `rask-observability`) → Perses; fleet (incl. the gateway) + Ray export OTLP/HTTP **traces and RED metrics** to `rask-greptimedb-standalone:4000/v1/otlp` via `service_kit.setup_otel` (FastAPI/HTTPX instrumentation emits `http.server.*` metrics automatically). OTLP headers split by signal: traces carry `x-greptime-pipeline-name=greptime_trace_v1` (GreptimeDB requires it for trace ingestion → `opentelemetry_traces` table), metrics use db-name only (→ PromQL series). The chart provisions a Perses "Fleet — RED" dashboard (`chart/templates/perses-dashboards.yaml`). Standard OTLP throughout (not OTel-Arrow).
- **The orchestrator is gone (P7a).** Ingestion is the medallion producer's `POST /ingest-iiif`: harvest a IIIF volume → write the raw blob-v2 page-image Lance dataset → emit ONE raw-write OpenLineage event through `packages/lineage-kit` (never publishing `medallion.raw` directly — the `/raw-arrival` subscription fires the cascade). HTR runs as event-triggered movers on the unified Ray cluster (P7b re-cuts the sealed runner's stages; the gold schema contract is pinned in `medallion/schemas/htr.py`).
- **Source images:** IIIF (Riksarkivet) with S3 read-through cache. `PageLoaderActor` hits S3 first, IIIF on miss.
- **Remote KubeRay:** the runner accepts `--address ray://...:10001`. No K8s manifests live in this repo — the remote cluster is managed elsewhere.

## Conventions

- **Gateway port is 8888.** Vite proxy in `frontend/microfrontends/home` defaults `VIEWER_BACKEND` to `http://localhost:8888` (the gateway, or the `make viewer` monolith). Don't change that port without updating the proxy.
- **Pytest import mode is `importlib`** (`--import-mode=importlib` in `pyproject.toml`). Test paths are explicit (`testpaths = [...]`), not discovered.
- **Ruff line length is 160**, not 100. Selected rule families include `ANN` (annotations); tests are exempted via `per-file-ignores`.
- **oxfmt uses tabs**, single quotes, `printWidth: 100` — defined in `frontend/.oxfmtrc.json`, applied across every JS/TS workspace (zones, `@rask/ui`, `@rask/api`, `@rask/zone-contract`). Prettier is gone.
- **JS monorepo runs on Turborepo** (`frontend/turbo.json`): `bun --cwd=frontend run build`/`check`/`dev` delegate to `turbo run` (package tasks + `^build` ordering + cached `build`/`.svelte-kit`/`dist` outputs). Add a new JS package's scripts in its own `package.json` — never centralize task logic in root. `lint`/`fmt`/`fmt:check` are **per-package turbo tasks** too (each package runs `oxlint` / `oxfmt`); only `knip` stays root-level, because it analyses the whole JS graph at once.
- **The cross-zone link gate is a test, not a lint rule.** A cross-zone `<a>` must carry `data-sveltekit-reload` or SvelteKit soft-navigates into a route the zone doesn't own (→ 404). Enforced by `@rask/zone-contract`'s vitest suite (`frontend/packages/zone-contract/src/cross-zone-reload.test.ts`) — oxlint's `.svelte` support reads the `<script>` block, not the markup, so an anchor-attribute rule cannot live there.
- **Frontend is SSR + Svelte 5 strict.** Every `.svelte` change is validated with the Svelte 5 skills + the `svelte` MCP autofixer. Browser-only globals must stay inside `onMount`/`$effect`/handlers (never component top level or `load`) or SSR render crashes.
- **`ty` is configured with `error-on-warning = true`** — typecheck warnings fail CI.

## Claude Code project config

- All project-local config lives under `.claude/`. **No `.mcp.json` at repo root** by design — the svelte MCP server is registered at `local` scope via `make claude-bootstrap` (idempotent). The install command in the `Makefile` is the source of truth for which MCP servers this project needs.
- `.claude/settings.json` is committed (team-shared: `enabledPlugins`, `extraKnownMarketplaces`, permissions, hooks). `.claude/settings.local.json` is gitignored (personal overrides + local-scope MCP).
- **Shared skills come from the [`ra-skills`](https://github.com/AI-Riksarkivet/ra-skills) marketplace** (language/toolchain: writing-python, fastapi, dagger, dockerfile, otel, testing-python, turborepo, zensical-_, …) — not vendored; `make claude-bootstrap` installs them, and you change one by editing it in ra-skills. **rask's own project skills (`rask-architecture`, `rask-services-fleet`, `rask-htr-pipeline`, `rask-frontend`) are vendored in `.claude/skills/`** — they describe rask internals and evolve with the code, so edit them in place (the same way ra-hcp keeps its `hcp-_` skills local).
- See `.claude/README.md` for the full plugin/marketplace/MCP surface and bootstrap steps.
