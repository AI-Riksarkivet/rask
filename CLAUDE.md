# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Toolchain rules

- **JS/TS uses Bun exclusively.** Use `bun` / `bunx`. `npm`, `npx`, `pnpm`, `pnpx` are not on PATH and MCP install commands assume `bunx`.
- **Python uses uv** (3.13) with Ruff + `ty` for type-checking. Run Python via `uv run <cmd>`; type-check via `uvx ty check`.
- Identifiers and env vars carry **no `ra-`/`ra_` prefix** (legacy from the ra-batch migration). Env vars are `RASK_*`.

## Common commands

| Goal                         | Command                                                        |
| ---------------------------- | -------------------------------------------------------------- |
| First-time setup             | `make install` (= `bun install` + `uv sync`)                   |
| Build everything             | `make build`                                                   |
| Run all tests                | `make test`                                                    |
| Single Python test           | `uv run pytest packages/htr/tests/test_geometry.py::test_name` |
| Filter by name               | `uv run pytest -k <pattern>`                                   |
| Skip slow tests              | `uv run pytest -m "not slow"`                                  |
| Format + lint + typecheck    | `make check` (= `make fmt` + `make lint` + `make typecheck`)   |
| Frontend type-check only     | `bun --cwd components/apps/frontend run check`                 |
| Storybook for `@rask/ui`     | `make storybook` (→ `:6006`)                                   |
| Bootstrap Claude Code config | `make claude-bootstrap`                                        |

### Run the app locally

```bash
make ray-up            # local Ray head on :6379, dashboard :8265
make serve-up          # deploy /transcribe + /htrflow on Ray Serve
make dev-micro         # the fleet: gateway :8888 + core-api :8801 + search :8802 +
                       #   volumes :8803 + ray :8804 + orchestrator :8810 (via dev-micro.sh)
make viewer            # the `core.main:app` monolith on :8888 (single-process dev convenience)
make viewer-frontend   # SvelteKit dev server, proxies /api → :8888 (the gateway)
```

The frontend's Vite proxy targets `:8888` either way — in the fleet that's the
**gateway**; with `make viewer` it's the monolith. `dev-micro.sh` is the source
of truth for the fleet's process list and ports.

`make serve-down` / `make ray-down` to tear down. Indexing pipelines: `make search-index`, `make catalog-index`, `make harvest-ead`.

### Local postgres + migrations

```bash
make pg-up                  # docker postgres:16 at localhost:5432, rask/rask/rask
make pg-migrate             # alembic upgrade head against PG_URL
make pg-revision MSG="..."  # autogenerate next migration from SQLModel changes
make pg-status / pg-down
```

Reproducible CI equivalent via Dagger (`.dagger/`):

```bash
dagger call migrate-up      # ephemeral pg + alembic upgrade — CI proof-of-clean-migration
dagger call test-pg         # same as above + the core pytest suite
```

## Repository layout (Polylith-inspired)

Three brick layers — **don't blur them**:

- `packages/` — reusable libraries, **no entrypoints**. uv + Bun workspace members.
  - `packages/htr` — Ray actors (PageLoader, Layout, Lines, Transcribe, AltoExport) + schemas
  - `packages/storage` — `FSSource/Sink`, `S3Source/Sink`, `IIIFCachedSource`, `iter_keys`, `s3_client`
  - `packages/service-kit` — shared **platform library**: `make_service_app` app factory, `Settings`/config, exceptions, middleware, `get_settings`/`SettingsDep`, the injectable lifespan. Dependency-light (no lancedb/ray/sqlmodel).
  - `packages/ray-kit` — Ray Job SDK + dashboard wrapper (schemas, `build_client`, `RAY_TRANSIENT_ERRORS`, the dashboard service). Shared by `ray-api` and the core orchestrator.
  - `packages/ui` — Svelte 5 + Bits UI + Tailwind 4 component library (`@rask/ui`; folder renamed from `@rask/ui`) w/ Storybook 10 (`@storybook/svelte-vite`). The shared design system every microfrontend imports via `workspace:*` — **styled components live here, not in the apps** (apps only supply theme tokens in their `app.css` + an `@source` pointing at `packages/ui/dist`). Subpath exports: `@rask/ui/{button,badge,card,dialog,sort-header,sidebar,utils}` + **`@rask/ui/shell`** (the shared `AppShell` + grouped `AppSidebar` + `nav-config` — so every app renders the _same_ sidebar, zero drift). See `docs/architecture/frontend-microfrontends.md`.
- `components/` — runnable code. **The old monolithic `viewer` service is gone** — it was dissolved (2026-06) into a gateway + per-domain services + a shared `core` brick:
  - `components/apps/runner` — Typer CLI that submits Ray Data jobs
  - `components/apps/frontend` — SvelteKit 2 + Svelte 5, **SSR** via `svelte-adapter-bun` (a real Bun server: `bun ./build/index.js`), one unified shadcn-svelte grouped sidebar (Compute / Documents / Batches / Storage). Vite dev proxy sends `/api` → the gateway on `:8888`. **Being decomposed into per-domain microfrontends** (compute/documents/batches/storage) under Turborepo — see `docs/architecture/frontend-microfrontends.md`.
  - `components/services/gateway` — reverse proxy on `:8888` (the frontend's proxy target). Path-routes `/api/*` to the services below (longest-prefix-first); owns no state. Upstreams are env-overridable (`RASK_CORE_API_URL` :8801, `RASK_SEARCH_API_URL` :8802, `RASK_VOLUMES_API_URL` :8803, `RASK_RAY_API_URL` :8804, `RASK_ORCH_API_URL` :8810).
  - `components/services/core` — the **core domain brick** (the dissolved `viewer`; package `core`). Owns `alembic/`, `core/db.py`, `core/lifespan.py`, `models/{batch,enums,pipelines}`, `repositories/`, the domain services (`services/{batches,submission,sync}`, `services/orchestrator/{derive,loop}`, `services/discover/catalog`), the batches/chunks/catalog/orchestrator endpoints, and `main.py` (monolith factory, still used by tests + `make viewer`). **Not a deployable** — composed by the two entrypoints below, which share the `batches` table transactionally (so they're two processes over one brick, not independent services).
  - `components/services/core_api` — thin entrypoint (`:8801`): health + batches + chunks + catalog over `core`; orchestrator loop **off**.
  - `components/services/orchestrator` — thin entrypoint (`:8810`): health + orchestrator endpoints over `core`; the lifespan-managed orchestrator loop **on** (`RASK_ORCHESTRATOR_AUTOSTART`).
  - `components/services/{volumes_api,search_api,ray_api}` — independent, **viewer-free** services (`:8803`/`:8802`/`:8804`): S3/IIIF image+ALTO proxy (stateless); Lance `lines` FTS + S3 thumbnails (owns a lines-only lifespan); Ray dashboard introspection (`/api/ray/*`) + the `/api/serve/*` proxy (thin shell over `ray-kit`). Each depends only on `service-kit` + its own libs — no `core`, no DB.
  - `components/scripts/` — one-shot setup / debug tools (`build_batches_db`, `chunk_batches`, `harvest_ead`, `index_alto`, `index_catalog`, `download_*`, `bench_framework`, `smoke_s3`, …). **No production-state-changing CLIs** — sync / submit / orchestrate all run through the HTTP services (core-api endpoints + the orchestrator service's lifespan loop).
- `projects/<name>/pyproject.toml` — **deployable composition only, no code**. One per deployable: `gateway`, `core-api`, `orchestrator`, `volumes-api`, `search-api`, `ray-api`, `runner` (+ `hcp`). (There is no `projects/viewer` — it was deleted when viewer dissolved.)

**Workspace membership is explicit, never globbed.** Adding a new brick requires editing **both**:

- `pyproject.toml` → `[tool.uv.workspace] members`
- root `package.json` → `workspaces`

Plus the relevant `projects/<name>/pyproject.toml` if it's deployable.

## Architecture (image → ALTO XML)

`rask` is a distributed HTR pipeline for the Swedish National Archives. See `docs/architecture/system-overview.md` for the full diagrams. Key facts that aren't obvious from any single file:

- **Runner is the engine.** `components/apps/runner` submits one Ray Data pipeline per CLI invocation and blocks on `.materialize()`. It does not run a long-lived service.
- **Ray Serve persists across job submissions.** TrOCR weights stay warm in `/transcribe` (3 replicas × 0.99 GPU). The pipeline's `TranscribeViaServe` actor is CPU-only and calls Serve synchronously over a handle. `make serve-up` deploys this independently of any job.
- **Two pipeline shapes:**
  - **Actor-per-stage** — `PageLoader → Layout → Lines → TranscribeViaServe → AltoExport → AltoWriter`. Uses GPU for YOLO regions/lines (0.001 GPU each) and TrOCR via Serve.
  - **`/htrflow`** — collapses Layout+Line+Transcribe+Alto into a single 1-replica CPU Serve deployment. Used when actor fan-out isn't worth it for a batch shape.
- **GPU sizing is hardcoded** in `components/apps/runner/src/runner/pipeline.py` for a 3-GPU node. Changing target hardware means editing that file.
- **No auth, no app middleware.** The services assume localhost / trusted network. The frontend hits `/api/*` on the **gateway** (`:8888`), which path-routes to the per-domain services; `/api/ray/*` and the `/api/serve/*` proxy are served by the standalone **ray-api** service (over `ray-kit`). SSR `load`/remote functions reach the gateway server-side via an absolute base URL (`RASK_GATEWAY_URL`); client code uses the relative `/api/*` proxy. The gateway sits **behind** the SvelteKit Bun server (it does not serve the SPA shell).
- **State surface:** relational DB behind a backend-agnostic ORM (SQLModel + SQLAlchemy async), owned by the **`core` brick**. **SQLite for dev** (`.cache/batches.db`, not committed); **Postgres for prod** via `DATABASE_URL=postgresql+asyncpg://…`. Schema changes go through **Alembic** (`components/services/core/alembic/`, run via `make pg-migrate` = `uv run --package core alembic upgrade head`) — never `SQLModel.metadata.create_all` in app startup. The `Batch` SQLModel uses `SAEnum(values_callable=...)` so `htr_status`/`manifest_status` round-trip as lowercase strings against postgres-native ENUM types or sqlite VARCHAR. Plus S3 two-bucket setup (`images-batch` input, `images-batch-alto` output). **No Redis, no queue, no event bus, no compose stack.** The Helm chart in `chart/` is the single deploy artifact for both local k3s and production — in-cluster Postgres, MinIO, and KubeRay are gated by `postgres.enabled`/`minio.enabled`/`ray.enabled` values toggles. Local deploy: `make k3s-install` (one-time) → `make k3s-build` → `make k3s-import` → `make k3s-up`; tear down with `make k3s-down` / `make k3s-purge`. See `docs/architecture/deployment.md` and `chart/README.md`.
- **Orchestrator runs in the `orchestrator` service** (a thin entrypoint over the `core` brick). A lifespan-managed `asyncio.Task` ticks every `RASK_ORCHESTRATOR_INTERVAL_SECONDS`: reconcile S3 → submit next prefetch / htr chunk. `RASK_ORCHESTRATOR_AUTOSTART` controls whether the loop starts on boot (the fleet runs `core-api` with it OFF and `orchestrator` with it ON, so the loop runs in exactly one process); operators flip it at runtime via `POST /api/v1/orchestrator/start` and `/stop`. Per-chunk control via `POST /api/v1/chunks/{id}/stop`. See `core/services/orchestrator/loop.py`. **Transitional — to be replaced by a NATS JetStream consumer once that lands.**
- **Source images:** IIIF (Riksarkivet) with S3 read-through cache. `PageLoaderActor` hits S3 first, IIIF on miss.
- **Remote KubeRay:** the runner accepts `--address ray://...:10001`. No K8s manifests live in this repo — the remote cluster is managed elsewhere.

## Conventions

- **Gateway port is 8888.** Vite proxy in `components/apps/frontend` defaults `VIEWER_BACKEND` to `http://localhost:8888` (the gateway, or the `make viewer` monolith). Don't change that port without updating the proxy.
- **Pytest import mode is `importlib`** (`--import-mode=importlib` in `pyproject.toml`). Test paths are explicit (`testpaths = [...]`), not discovered.
- **Ruff line length is 160**, not 100. Selected rule families include `ANN` (annotations); tests are exempted via `per-file-ignores`.
- **Prettier uses tabs**, single quotes, `printWidth: 100` — defined in root `package.json`, applied across both frontend and `@rask/ui` workspaces.
- **JS monorepo runs on Turborepo** (`turbo.json`): `bun run build`/`check`/`dev` delegate to `turbo run` (package tasks + `^build` ordering + cached `build`/`.svelte-kit`/`dist` outputs). Add a new JS package's scripts in its own `package.json` — never centralize task logic in root. `lint`/`format` stay root-level (Prettier + a single flat ESLint config) until the shared `@rask/eslint-config` package is extracted for the microfrontend split.
- **Frontend is SSR + Svelte 5 strict.** Every `.svelte` change is validated with the Svelte 5 skills + the `svelte` MCP autofixer. Browser-only globals must stay inside `onMount`/`$effect`/handlers (never component top level or `load`) or SSR render crashes.
- **`ty` is configured with `error-on-warning = true`** — typecheck warnings fail CI.

## Claude Code project config

- All project-local config lives under `.claude/`. **No `.mcp.json` at repo root** by design — the svelte MCP server is registered at `local` scope via `make claude-bootstrap` (idempotent). The install command in the `Makefile` is the source of truth for which MCP servers this project needs.
- `.claude/settings.json` is committed (team-shared: `enabledPlugins`, `extraKnownMarketplaces`, permissions, hooks). `.claude/settings.local.json` is gitignored (personal overrides + local-scope MCP).
- **Shared skills come from the [`ra-skills`](https://github.com/AI-Riksarkivet/ra-skills) marketplace** (language/toolchain: writing-python, fastapi, dagger, dockerfile, otel, testing-python, turborepo, zensical-_, …) — not vendored; `make claude-bootstrap` installs them, and you change one by editing it in ra-skills. **rask's own project skills (`rask-architecture`, `rask-services-fleet`, `rask-htr-pipeline`, `rask-orchestrator`, `rask-frontend`) are vendored in `.claude/skills/`** — they describe rask internals and evolve with the code, so edit them in place (the same way ra-hcp keeps its `hcp-_` skills local).
- See `.claude/README.md` for the full plugin/marketplace/MCP surface and bootstrap steps.
