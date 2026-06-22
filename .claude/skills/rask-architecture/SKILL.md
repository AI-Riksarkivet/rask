---
name: rask-architecture
description: The rask Polylith brick layers (packages/components/projects) and the entrypoint-over-brick composition contract — service-kit's make_service_app + injectable lifespan. Use when adding/moving a brick or service, editing a pyproject.toml or workspace member list, wiring a new entrypoint, debugging "module not found"/uv-resolution after adding code, or deciding where new code belongs (lib vs runnable vs deployable).
---

# rask architecture (Polylith bricks + composition)

The single most rask-specific thing to get right: **which layer a brick lives in, and how entrypoints compose bricks**. Get this wrong and uv resolution breaks silently or you smear a domain across layers. Defers Python idioms to `writing-python`, FastAPI routing to `fastapi`, container builds to `dockerfile`.

## When to use

- Adding, moving, or deleting a brick (a `packages/*`, `components/*`, or `projects/*` member).
- Wiring a new HTTP service entrypoint or editing one.
- Editing any `pyproject.toml` `members`/`dependencies`, or root `package.json` `workspaces`.
- "ModuleNotFoundError" / uv won't resolve a first-party import after you added code.
- Deciding where new code belongs: reusable lib, runnable component, or deployable composition.

## The three brick layers — don't blur them

| Layer | Path | Rule | Has entrypoints? |
|---|---|---|---|
| **packages** | `packages/<name>` | reusable libraries | **No** — never an `app`/`main`/CLI |
| **components** | `components/{apps,services,scripts}/<name>` | runnable code (services, CLIs, one-shot scripts) | Yes |
| **projects** | `projects/<name>` | deployable composition: pins a member set, **no code** | composes, owns none |

Current packages: `htr`, `storage`, `service-kit`, `ray-kit`, `component-lib`.
Current deployables (`projects/*` with a `pyproject.toml`): `core-api`, `gateway`, `orchestrator`, `ray-api`, `runner`, `search-api`, `volumes-api`. (`projects/viewer/` is an orphan `.venv` with **no pyproject** — not a deployable; don't treat it as one.)

## The composition seam: `make_service_app` + injectable lifespan

`service_kit.make_service_app(*, title, routers, proxy_router=None, lifespan=None)` builds the FastAPI app with shared config/handlers/middleware. The **lifespan is injectable**: stateless services get the minimal `default_lifespan` (settings only); stateful ones pass a `LifespanFactory`, e.g. `core.lifespan.make_lifespan` (DB/Lance/Ray/S3). Routers mount under `settings.api_prefix` (`/api/v1`); `proxy_router` mounts at root.

A thin entrypoint is **~15 lines** — import routers + a lifespan from a brick, call the factory. `core_api/__init__.py`:

```python
from core.api.v1.endpoints import batches, catalog, chunks, health
from core.lifespan import make_lifespan
from service_kit import make_service_app

app = make_service_app(
    title="core-api",
    routers=[health.router, batches.router, chunks.router, catalog.router],
    lifespan=make_lifespan,
)
```

`orchestrator/__init__.py` is the same shape over the **same `core` brick** (`routers=[health.router, orchestrator.router]`, same `make_lifespan`).

## Brick-vs-entrypoint: `core` is NOT deployable

`components/services/core` is the **core domain brick** (package `core`): owns `alembic/`, `db.py`, `lifespan.py`, models, repositories, domain services, endpoints, and `main.py` (the monolith for tests + `make viewer`). It is **composed by two thin entrypoints** — `core_api` (:8801, orchestrator loop OFF) and `orchestrator` (:8810, loop ON via `RASK_ORCHESTRATOR_AUTOSTART`). They are **two processes over one brick sharing the `batches` table transactionally** — not independent services. The loop must run in exactly one process; that's why it's split by config, not by code.

## Hard invariants (the gotchas)

- **Workspace membership is explicit, never globbed.** Adding a brick = a **two-place edit**: root `pyproject.toml` `[tool.uv.workspace] members` AND root `package.json` `workspaces` — plus `projects/<name>/pyproject.toml` if deployable. Forget either and resolution breaks **silently** (uv can't find the member; Bun won't link it). See `references/adding-a-brick.md`.
- **`service-kit` stays dependency-light.** Its only deps are `storage`, `fastapi`, `pydantic`, `pydantic-settings`, `python-dotenv`. **Never** add `lancedb`, `ray`, or `sqlmodel` — those belong to the bricks that need them (`core`, `ray-kit`, …). service-kit is shared by every service including the DB-free ones (`volumes_api`, `search_api`, `ray_api`).
- **Do not resurrect `viewer` or `control`.** The monolithic `viewer` service was dissolved (2026-06) into the gateway + per-domain services + the `core` brick. There is no `control` brick. New domain code lands in an existing brick or a new one — never a revived monolith.
- **The `viewer`/`viewer_*` names live on only as the `core` brick's `main.py` (dev convenience) and `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` settings.** Don't infer a deployable from them.

## When to load each reference

| Need | Read |
|---|---|
| Step-by-step: add a new brick or deployable without breaking resolution | `references/adding-a-brick.md` |
| The full service fleet, ports, and which brick each entrypoint composes | `references/service-fleet.md` |
