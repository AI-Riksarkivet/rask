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
make dev-micro         # the fleet: gateway :8888 + compute :8804 + controlplane :8820 +
                       #   the media viewer :8101 (via scripts/dev-micro.sh)
make dev-frontends     # ALL 7 zones + the :3024 composition proxy (builds @rask/ui first)
make home              # the catch-all zone alone, :5273
make frontend-media    # one zone alone (frontend-<zone>: lakehouse|media|annotator|compute|studio|train)
```

**Open `:3024`, not a zone's own port** — that is the composition proxy that routes
`/<zone>` to the right dev server. `scripts/dev-micro.sh` is the source of truth for the
fleet's process list and ports.

`make dev-frontends` builds `@rask/ui` + `@rask/api` before starting the zones on purpose:
an unfiltered `turbo run dev` also starts the ui library's `svelte-package -w` watcher,
which rewrites `dist/` while the zones read it — one zone crashes and turbo tears the whole
run down.

### The in-cluster dev loop: tilt + k9s + k3s

For anything that only manifests **in-cluster** — Dapr sidecar injection, the
bronze→silver→gold cascade, lineage emission, FGA checks — `make dev-micro` cannot
reproduce it, and a rebuild cycle (`k3s-build` → `k3s-import` → `k3s-up`) costs minutes.
That loop is what tilt exists for here.

```bash
make k3s-up          # the cluster + release (one-time per session)
make tilt-registry   # ONCE per host: registry on :5000 + point k3s at it (sudo; restarts k3s)
make tilt-up         # the dev loop; UI on :10350
make tilt-verify     # PROVE live_update reaches a pod (SERVICE=catalog by default)
make k9s             # inspect the cluster (installed into .localbin by `make bootstrap`)
```

**`make tilt-verify` is not optional ceremony.** This repo shipped a Tiltfile for months
that could never have worked — it synced into a path that did not exist, against services
whose uvicorn had no `--reload`, into containers with a read-only rootfs — and nothing
reported a problem. "tilt up started" and "the pod is Running" are not evidence. The
verifier writes a marker into a real source file and polls the container for it.

Scope and limits:

- **Python fleet services only.** Frontend zones are excluded (`frontend.enabled=false` in
  the Tiltfile), so `/` on the ingress 404s under tilt — use `make dev-frontends` (Vite HMR)
  for UI work; it is already sub-second and tilt would be a downgrade.
- **Dependency changes still need a rebuild.** Only the synced package paths hot-reload.
- **`dev.reload` is dev-only.** It relaxes `readOnlyRootFilesystem` (live_update cannot
  write into a read-only container) and appends `--reload`. Rendering the chart without it
  yields zero `--reload` flags and `readOnlyRootFilesystem: true` everywhere. Never set it
  in production. Every `--reload-dir` must exist in every image — uvicorn refuses to start
  on a missing one rather than skipping it, which is why `dev.reloadKits` defaults to
  `service_kit` alone.
- **Tilt and `make k3s-up` both own the `rask` release.** Running `helm upgrade` by hand
  while tilt is up replaces tilt's injected image with the chart default, and tilt silently
  stops managing that deployment — live_update then cannot fire. Pick one owner.
- **A killed `helm upgrade` leaves the release in `pending-upgrade`,** and every later
  upgrade is refused until someone runs `helm rollback rask <last-good-rev>`. If tilt
  appears to build and push images that never reach the cluster, check `helm history rask`
  before anything else.

`make serve-down` / `make ray-down` to tear down. EAD download: `make harvest-ead` (the `catalog-index` Lance indexer died in the R6/R20 wave — the EAD table re-lands catalog-governed behind `/api/media/search`).
(The app database, Alembic migrations and the `pg-*`/`viewer` targets died at P7a — the only relational
stores left are the chart-managed lineage (AGE) and OpenFGA databases.)

## Repository layout

Two **language-pure planes** — **don't blur them**. Python lives at the repo root (`packages/` + `services/`); the entire JS/TS estate lives under `frontend/`, its own bun + Turborepo workspace root. (There is deliberately **no Polylith-style `projects/` layer** — it was removed 2026-07; deployables build straight from the root uv workspace via `uv sync --package <name>`, one dockerfile per deployable in `.docker/`.)

- `packages/` — reusable **Python** libraries, **no entrypoints**. uv workspace members.
  - `packages/storage` — `FSSource/Sink`, `S3Source/Sink`, `IIIFCachedSource`, `iter_keys`, `s3_client`
  - `packages/service-kit` — shared **platform library**: `make_service_app` app factory, `Settings`/config, exceptions, middleware, `get_settings`/`SettingsDep`, the injectable lifespan. Dependency-light (no lancedb/ray/sqlmodel).
  - `packages/ray-kit` — Ray Job SDK + dashboard wrapper (schemas, `build_client`, `RAY_TRANSIENT_ERRORS`, the dashboard service). Used by the `compute` service.
  - `packages/tracker` — pluggable transfer-state tracking (SQLite / Postgres backends)
  - `packages/validate` — pre-upload image validation (TIFF/JPEG/PNG corruption detection + pluggable rules)
  - `packages/lineage-kit` — the OpenLineage emission kernel used by the medallion producer/movers
  - `packages/ratch` — media pipeline library: Lance ingestion, Ray-distributed feature stages, retrieval over the chunks table. Ships a Typer console script (`ratch`) — the one sanctioned CLI exception to the "packages have no entrypoints" rule.
- `services/` — runnable **Python** code. **The old monolithic `viewer` service is gone**, and so is the whole batches/orchestrator plane (P7a compute-plane cutover — see `docs/architecture/lance-ns-merge.md` P7):
  - `services/gateway` — reverse proxy on `:8888` (the frontend's proxy target). Path-routes `/api/*` longest-prefix-first: the `compute` rows (`/api/ray`, `/api/serve` — the URL namespace names the Ray cluster, not the service), `/api/projects` → controlplane, plus the lance-plane rows (`/api/catalog`, `/api/lineage`, `/api/produce`, `/api/train`, `/api/media/*`); owns no state; **no `/api` catch-all** — unmatched `/api/*` 404s. Upstreams are env-overridable (`RASK_COMPUTE_URL` :8804, `RASK_CONTROLPLANE_URL` :8820, `RASK_MEDIA_VIEWER_URL` :8101, …).
  - `services/compute` — the **`compute` service** (`:8804`): Ray dashboard introspection (`/api/ray/*`) + the `/api/serve/*` proxy (thin shell over `ray-kit`); no DB. `compute` on every surface — uv member, import, k8s/dapr/image/gateway (R22, supersedes R20's `ray` + its ray-api PyPI-shadow exception); the public paths stay `/api/ray` + `/api/serve`. (`core`/`core_api`/`search_api`/`volumes_api` died in the R6/R20 media wave — the S3 object browser now lives in the lance `viewer` at `/api/media/object*`; lines/EAD FTS re-land as catalog-governed Lance tables behind `/api/media/search`.)
  - `services/medallion` — the lakehouse cascade (producer + movers). Its producer owns the **P7a IIIF→bronze page head** (R23: raw is the external world, never a governed tier — the medallion is exactly bronze→silver→gold): `POST /ingest-iiif` harvests a volume from the IIIF Image API straight into the BRONZE blob-v2 page-image Lance dataset (stage-stamped at ingest) and emits the ONE bronze-write OpenLineage event through `packages/lineage-kit` with the external `iiif://…` source as input; `/bronze-arrival` fires the `medallion.bronze` cascade (the P7b HTR movers).
- `frontend/` — the **JS/TS plane** and its own workspace root: `package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/` (the shared favicon source). The only JS outside it is `tests/e2e`, a standalone Playwright project with its own lockfile (`make e2e`).
  - **The 7 zones** (`frontend/microfrontends/<zone>`), SvelteKit 2 + Svelte 5, **SSR** via `svelte-adapter-bun` (a real Bun server: `bun ./build/index.js`), composed by Turborepo's built-in microfrontends proxy on `:3024` in dev and the k3s Ingress in prod: `home` (catch-all, base `''`, owns `/` + the OIDC BFF), `lakehouse` (`/lakehouse` — data/lineage/models/admin/storage areas), `media` (`/media`, labelled **Search**), `annotator` (`/annotator`, labelled **Annotate**), `compute` (`/compute`), `train` (`/train`, placeholder data), `studio` (`/studio`). Bases are a bare `/<zone>` — **there is no `/default/` segment**. Every zone renders the shared `@rask/ui/shell` AppShell. See `.claude/skills/rask-frontend`.
  - `frontend/packages/ui` — Svelte 5 + Bits UI + Tailwind 4 design system (`@rask/ui`) w/ Storybook 10 (`@storybook/svelte-vite`). The only frontend package with a build step (`svelte-package` → `dist/`). **Styled components live here, not in the zones** (zones supply `app.css` with an `@source '../../../packages/ui/dist'` — three `../`). 39 subpath exports incl. **`@rask/ui/shell`**. See `.claude/skills/rask-styling`.
  - `frontend/packages/api` — `@rask/api`, the shared data layer: typed gateway client **plus** the OIDC/BFF plane (`bff.ts`, `oidc.ts`) and the lineage client. valibot. JIT TS, no build step.
  - `frontend/packages/media-api` — `@rask/media-api`, the Arrow-backed media/viewer client.
  - `frontend/packages/engine` — `@rask/engine`, a framework-agnostic PixiJS/WebGPU annotation canvas (ra-anno lineage).
  - `frontend/packages/labeling` — `@rask/labeling`, the `LabelOp` model + the annotator's Arrow-IPC transport.
  - `frontend/packages/config` — `@rask/config`, one shared `tsconfig.base.json` (extended by 6 of 14 packages).
  - `frontend/packages/zone-contract` — `@rask/zone-contract`, **test-only**: 12 files / 699 tests gating the estate's shape (cross-zone `data-sveltekit-reload`, the zone manifest, deploy paths, bundle budgets, and a toolchain guard that fails the build if ESLint/Prettier reappear).
- `scripts/` — **all** dev/ops scripts, shell + Python: one-shot setup / debug tools (`harvest_ead`, `index_catalog`, `download_*`, `smoke_s3`, the self-contained Ray jobs `ray_stage_job`/`ray_train_job`/`ray_iiif_ingest_job`, …) plus `dev-micro.sh` and `k3s-install.sh`. **No production-state-changing CLIs** — ingestion and the cascade run through the HTTP services (the medallion producer + movers).

- `runners/` — **sealed model environments, NOT workspace members.** `runners/htr` holds the Ray Data HTR pipeline (`src/runner`) *and* the model actors (`src/htr`) in one project with its **own `pyproject.toml` and own `uv.lock`**. Matched by no glob, so torch/htrflow/ultralytics/transformers never enter the fleet's resolution (root lock 200 → 145 packages; fleet tests ~32 min → ~6 s). `storage` is a **path** dep. Its tests are invisible to the root pytest — `make test` runs them separately; its images build from **its** lock; it carries its own ruff config (ruff resolves the nearest pyproject). Ray entrypoint: `uv run --project runners/htr runner`, overridden in-cluster by `RASK_RUNNER_CMD=runner`.

**Workspace membership is globbed, per plane** — the directories are language-pure, so every child carries the right manifest:

- `pyproject.toml` → `[tool.uv.workspace] members = ["packages/*", "services/*"]`
- `frontend/package.json` → `workspaces = ["microfrontends/*", "packages/*"]` (relative to `frontend/`)

A new Python library/service or a new zone is picked up by the glob — but it **must** ship a `pyproject.toml` (Python) or a `package.json` (JS), or its plane silently drops it.

Deployables are just workspace members with a dockerfile: `.docker/<name>.dockerfile` runs `uv sync --frozen --package <name>` against the **root** `uv.lock` (the deployable set is `gateway`, `compute`, `runner` — the `.docker/compute.dockerfile` image is the compute service; the Ray cluster image is `.docker/ray-cluster.dockerfile`).

## Architecture (image → ALTO XML)

`rask` is a distributed HTR pipeline for the Swedish National Archives. See `docs/architecture/system-overview.md` for the full diagrams. Key facts that aren't obvious from any single file:

- **Runner is the engine.** `runners/htr` submits one Ray Data pipeline per CLI invocation and blocks on `.materialize()`. It does not run a long-lived service.
- **Ray Serve persists across job submissions.** TrOCR weights stay warm in `/transcribe` (**2 replicas × 0.49 GPU** — `RASK_SERVE_REPLICAS`/`RASK_SERVE_GPU_FRAC`, so `/transcribe` and `/htrflow` co-reside on a 2-GPU pool at 1.96 ≤ 2.0). The pipeline's `TranscribeViaServe` actor is CPU-only and calls Serve synchronously over a handle. `make serve-up` deploys this independently of any job.
- **Two pipeline shapes:**
  - **Actor-per-stage** — `PageLoader → Layout → Lines → TranscribeViaServe → AltoExport → AltoWriter`. Uses GPU for YOLO regions/lines (0.001 GPU each) and TrOCR via Serve.
  - **`/htrflow`** — collapses Layout+Line+Transcribe+Alto into a single 1-replica CPU Serve deployment. Used when actor fan-out isn't worth it for a batch shape.
- **GPU sizing is split.** Serve fractions are env-overridable (`RASK_SERVE_GPU_FRAC`/`RASK_SERVE_REPLICAS`, defaulting to a 2-GPU packing in *both* `transcribe_service.py` and `htrflow_service.py`); the actor-pool sizes are hardcoded literals in `runners/htr/src/runner/pipeline.py`. Retargeting hardware means editing all three.
- **No auth, no app middleware.** The services assume localhost / trusted network. The frontend hits `/api/*` on the **gateway** (`:8888`), which path-routes to the per-domain services; `/api/ray/*` and the `/api/serve/*` proxy are served by the standalone **ray** service (over `ray-kit`). SSR `load`/remote functions reach the gateway server-side via an absolute base URL (`RASK_GATEWAY_URL`); client code uses the relative `/api/*` proxy. The gateway sits **behind** the SvelteKit Bun server (it does not serve the SPA shell).
- **State surface:** the lakehouse (Lance datasets on RustFS S3) governed by the catalog, plus the chart-managed **lineage (AGE)** and **OpenFGA** Postgres databases (CloudNativePG). **The app's own relational DB is gone** (P7a): no batches table, no Alembic, no `DATABASE_URL` in the fleet. **No Redis**; events ride Dapr pub/sub on NATS JetStream. (`.docker/` still carries 8 `docker-compose*.yml` files — auth/dex, governance, lineage, rustfs, demo, local. They are side stacks for local bring-up, not the deploy path; the Helm chart is.) The Helm chart in `chart/` is the single deploy artifact for both local k3s and production — in-cluster CloudNativePG (`Cluster`), RustFS operator (`Tenant` → `rask-rustfs-io:9000`), and KubeRay are gated by `cnpg.enabled`/`rustfs.enabled`/`ray.enabled` values toggles; each toggle gates both the operator subchart and its custom resource. Local deploy: `make k3s-install` (one-time) → `make k3s-build` → `make k3s-import` → `make k3s-up`; tear down with `make k3s-down` / `make k3s-purge`. See `docs/architecture/deployment.md` and `chart/README.md`.
- **Observability (optional, `observability.enabled`):** Vector → GreptimeDB (on RustFS S3 bucket `rask-observability`) → Perses; fleet (incl. the gateway) + Ray export OTLP/HTTP **traces and RED metrics** to `rask-greptimedb-standalone:4000/v1/otlp` via `service_kit.setup_otel` (FastAPI/HTTPX instrumentation emits `http.server.*` metrics automatically). OTLP headers split by signal: traces carry `x-greptime-pipeline-name=greptime_trace_v1` (GreptimeDB requires it for trace ingestion → `opentelemetry_traces` table), metrics use db-name only (→ PromQL series). The chart provisions a Perses "Fleet — RED" dashboard (`chart/templates/perses-dashboards.yaml`). Standard OTLP throughout (not OTel-Arrow).
- **The orchestrator is gone (P7a).** Ingestion is the medallion producer's `POST /ingest-iiif`: harvest a IIIF volume (external raw, R23) → write the BRONZE blob-v2 page-image Lance dataset directly → emit ONE bronze-write OpenLineage event through `packages/lineage-kit` (input = the external `iiif://…` source; never publishing `medallion.bronze` directly — the `/bronze-arrival` subscription fires the cascade). The governed tiers are exactly bronze→silver→gold. HTR runs as event-triggered movers on the unified Ray cluster (P7b re-cuts the sealed runner's stages; the gold schema contract is pinned in `medallion/schemas/htr.py`).
- **Source images:** IIIF (Riksarkivet) with S3 read-through cache. `PageLoaderActor` hits S3 first, IIIF on miss.
- **Remote KubeRay:** the runner accepts `--address ray://...:10001`. No K8s manifests live in this repo — the remote cluster is managed elsewhere.

## Conventions

- **Gateway port is 8888 — but the zones' dev proxies disagree on who to call.** `compute`/`studio`/`train` proxy `/api` → `VIEWER_BACKEND` (`:8888`, the gateway); `home`/`lakehouse` proxy → `LANCE_BACKEND` (**`:8001`**, the lineage service — which `dev-micro.sh` does not start); `media`/`annotator` have no `/api` proxy and reach `:8101`/`:8102`/`:8103` via their own BFF. The same split exists server-side: `compute` reads `RASK_GATEWAY_URL`, `home`/`lakehouse` read `LANCE_GATEWAY_URL`. A `/api/*` call that works in one zone can fail in another — see `.claude/skills/rask-frontend`.
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
- **Shared skills come from the [`ra-skills`](https://github.com/AI-Riksarkivet/ra-skills) marketplace** (language/toolchain: writing-python, fastapi, dagger, dockerfile, otel, testing-python, turborepo, zensical-_, …) — not vendored; `make claude-bootstrap` installs them, and you change one by editing it in ra-skills. **rask's own project skills are vendored in `.claude/skills/`** — they describe rask internals and evolve with the code, so edit them in place (the same way ra-hcp keeps its `hcp-*` skills local). Route by plane:

| Working on | Skill |
| --- | --- |
| Where code belongs; workspace globs; `pyproject.toml`; a new member or deployable | `rask-architecture` |
| The gateway, an endpoint's route, a 404/502/403 through `/api/*`, ports | `rask-services-fleet` |
| A zone, a route, data fetching, cross-zone links, the frontend gates | `rask-frontend` |
| `@rask/ui`, tokens, `class=`, an unstyled page, a new component | `rask-styling` |
| `runners/htr`, GPU packing, Ray Data/Serve, an OOM | `rask-htr-pipeline` |
| An authorization model, tuples, `.fga` files | `openfga` |

These skills are maintained against the code and **will drift** — when you find a claim that contradicts a file, fix the skill in the same commit as the code.
- See `.claude/README.md` for the full plugin/marketplace/MCP surface and bootstrap steps.
