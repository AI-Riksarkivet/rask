# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Toolchain rules

- **JS/TS uses Bun exclusively.** Use `bun` / `bunx`. `npm`, `npx`, `pnpm`, `pnpx` are not on PATH and MCP install commands assume `bunx`.
- **Python uses uv** (3.13) with Ruff + `ty` for type-checking. Run Python via `uv run <cmd>`; type-check via `uvx ty check`.
- Identifiers and env vars carry **no `ra-`/`ra_` prefix** (legacy from the ra-batch migration). Env vars are `RASK_*`.

## Common commands

| Goal | Command |
|---|---|
| First-time setup | `make install` (= `bun install` + `uv sync`) |
| Build everything | `make build` |
| Run all tests | `make test` |
| Single Python test | `uv run pytest packages/htr/tests/test_geometry.py::test_name` |
| Filter by name | `uv run pytest -k <pattern>` |
| Skip slow tests | `uv run pytest -m "not slow"` |
| Format + lint + typecheck | `make check` (= `make fmt` + `make lint` + `make typecheck`) |
| Frontend type-check only | `bun --cwd components/apps/frontend run check` |
| Storybook for `component-lib` | `make storybook` (→ `:6006`) |
| Bootstrap Claude Code config | `make claude-bootstrap` |

### Run the app locally

```bash
make ray-up            # local Ray head on :6379, dashboard :8265
make serve-up          # deploy /transcribe + /htrflow on Ray Serve
make viewer            # FastAPI on :8888 (frontend Vite proxy assumes this port)
make viewer-frontend   # SvelteKit dev server, proxies /api → :8888
```

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
dagger call test-pg         # same as above + viewer pytest
```

## Repository layout (Polylith-inspired)

Three brick layers — **don't blur them**:

- `packages/` — reusable libraries, **no entrypoints**. uv + Bun workspace members.
  - `packages/htr` — Ray actors (PageLoader, Layout, Lines, Transcribe, AltoExport) + schemas
  - `packages/storage` — `FSSource/Sink`, `S3Source/Sink`, `IIIFCachedSource`, `iter_keys`, `s3_client`
  - `packages/control` — async ops library: `reconcile_from_s3` (S3 → batches.db sync), `submit_chunk` (RayJob submit). Consumed by viewer + the thin scripts in `components/scripts/`
  - `packages/component-lib` — Svelte 5 + Bits UI + Tailwind 4 component library w/ Storybook
- `components/` — runnable code.
  - `components/apps/runner` — Typer CLI that submits Ray Data jobs
  - `components/apps/frontend` — SvelteKit SPA (adapter-static)
  - `components/services/viewer` — FastAPI service on `:8888` (only HTTP backend). Owns `alembic/` (migrations) and `models/batch.py` (SQLModel schema) until Phase 2C moves them into `packages/control`.
  - `components/scripts/` — one-shot Python tools. `sync_from_s3.py` and `submit_chunks.py` are now thin wrappers around `control.*`; the rest are ad-hoc ops (`bench_framework`, `download_*`, `harvest_ead`, `index_alto`, `index_catalog`, …)
- `projects/<name>/pyproject.toml` — **deployable composition only, no code**. Lists workspace members for that deployable (`hcp`, `runner`, `viewer`).

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
- **Viewer has no auth, no middleware.** Assumes localhost / trusted network. SPA hits `/api/*`; `/api/ray/*` is a dashboard proxy.
- **State surface:** relational DB behind a backend-agnostic ORM (SQLModel + SQLAlchemy async). **SQLite for dev** (`.cache/batches.db`, not committed); **Postgres for prod** via `DATABASE_URL=postgresql+asyncpg://…`. Schema changes go through **Alembic** (`components/services/viewer/alembic/`) — never `SQLModel.metadata.create_all` in app startup. The `Batch` SQLModel uses `SAEnum(values_callable=...)` so `htr_status`/`manifest_status` round-trip as lowercase strings against postgres-native ENUM types or sqlite VARCHAR. Plus S3 two-bucket setup (`images-batch` input, `images-batch-alto` output). **No Redis, no queue, no event bus, no docker-compose, no Helm.** The `Makefile` is the only runbook.
- **Source images:** IIIF (Riksarkivet) with S3 read-through cache. `PageLoaderActor` hits S3 first, IIIF on miss.
- **Remote KubeRay:** the runner accepts `--address ray://...:10001`. No K8s manifests live in this repo — the remote cluster is managed elsewhere.

## Conventions

- **Frontend port is 8888.** Vite proxy in `components/apps/frontend` defaults `VIEWER_BACKEND` to `http://localhost:8888`. Don't change the viewer port without updating the proxy.
- **Pytest import mode is `importlib`** (`--import-mode=importlib` in `pyproject.toml`). Test paths are explicit (`testpaths = [...]`), not discovered.
- **Ruff line length is 160**, not 100. Selected rule families include `ANN` (annotations); tests are exempted via `per-file-ignores`.
- **Prettier uses tabs**, single quotes, `printWidth: 100` — defined in root `package.json`, applied across both frontend and `component-lib` workspaces.
- **`ty` is configured with `error-on-warning = true`** — typecheck warnings fail CI.

## Claude Code project config

- All project-local config lives under `.claude/`. **No `.mcp.json` at repo root** by design — the svelte MCP server is registered at `local` scope via `make claude-bootstrap` (idempotent). The install command in the `Makefile` is the source of truth for which MCP servers this project needs.
- `.claude/settings.json` is committed (team-shared: `enabledPlugins`, permissions, hooks). `.claude/settings.local.json` is gitignored (personal overrides + local-scope MCP).
- Project-local skills live in `.claude/skills/` (fastapi, otel, python-infrastructure, writing-python, writing-typescript, dagger).
- See `.claude/README.md` for plugin/marketplace install steps.
