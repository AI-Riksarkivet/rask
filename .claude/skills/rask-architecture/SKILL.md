---
name: rask-architecture
description: The rask workspace layers (packages/components) and the entrypoint-over-package composition contract — service-kit's make_service_app + injectable lifespan. Use when adding/moving a workspace member or service, editing a pyproject.toml or workspace member list, wiring a new entrypoint, debugging "module not found"/uv-resolution after adding code, or deciding where new code belongs (lib vs runnable vs deployable).
---

# rask architecture (workspace layers + composition)

The single most rask-specific thing to get right: **which layer a package lives in, and how entrypoints compose packages**. Get this wrong and uv resolution breaks silently or you smear a domain across layers. Defers Python idioms to `writing-python`, FastAPI routing to `fastapi`, container builds to `dockerfile`.

> **There is no `projects/` layer.** The Polylith-style `projects/<name>` composition stubs were removed (2026-07). Deployables are ordinary workspace members built by `.docker/<name>.dockerfile` running `uv sync --frozen --package <name>` against the **root** `uv.lock`. Never recreate per-deployable pyprojects or per-deployable locks.

## When to use

- Adding, moving, or deleting a workspace member (`packages/*` or `components/*`).
- Wiring a new HTTP service entrypoint or editing one.
- Editing any `pyproject.toml` `members`/`dependencies`, or root `package.json` `workspaces`.
- "ModuleNotFoundError" / uv won't resolve a first-party import after you added code.
- Deciding where new code belongs: reusable lib, runnable component, or deployable.

## The two layers — don't blur them

| Layer          | Path                                                 | Rule                                                        | Has entrypoints?                   |
| -------------- | ---------------------------------------------------- | ----------------------------------------------------------- | ---------------------------------- |
| **packages**   | `packages/<name>`                                    | reusable libraries                                          | **No** — never an `app`/`main`/CLI |
| **components** | `components/{frontends,cli,services,scripts}/<name>` | runnable code (frontends, CLIs, services, one-shot scripts) | Yes                                |

Current packages: `api` (@rask/api, Bun), `htr`, `ray-kit`, `service-kit`, `storage`, `tracker`, `ui` (@rask/ui, Bun), `validate`. The Python libs are uv members; `api`/`ui` are the JS/Bun members (the frontend design system + valibot client).
Current deployables (each = a workspace member + a `.docker/<name>.dockerfile`): `core-api`, `gateway`, `orchestrator`, `ray-api`, `runner`, `search-api`, `volumes-api`.

## The composition seam: `make_service_app` + injectable lifespan

`service_kit.make_service_app(*, title, routers, proxy_router=None, lifespan=None)` builds the FastAPI app with shared config/handlers/middleware. The **lifespan is injectable**: stateless services get the minimal `default_lifespan` (settings only); stateful ones pass a `LifespanFactory`, e.g. `core.lifespan.make_lifespan` (DB/Lance/Ray/S3). Routers mount under `settings.api_prefix` (`/api/v1`); `proxy_router` mounts at root.

A thin entrypoint is **~15 lines** — import routers + a lifespan from the domain package, call the factory. `core_api/__init__.py`:

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

`orchestrator/__init__.py` is the same shape over the **same `core` package** (`routers=[health.router, orchestrator.router]`, same `make_lifespan`).

## Domain package vs entrypoint: `core` is NOT deployable

`components/services/core` is the **core domain package** (package `core`): owns `alembic/`, `db.py`, `lifespan.py`, models, repositories, domain services, endpoints, and `main.py` (the monolith for tests + `make viewer`). It is **composed by two thin entrypoints** — `core_api` (:8801, orchestrator loop OFF) and `orchestrator` (:8810, loop ON via `RASK_ORCHESTRATOR_AUTOSTART`). They are **two processes over one package sharing the `batches` table transactionally** — not independent services. The loop must run in exactly one process; that's why it's split by config, not by code.

## Hard invariants (the gotchas)

- **Workspace membership is explicit, never globbed.** Adding a member = a **two-place edit**: root `pyproject.toml` `[tool.uv.workspace] members` AND root `package.json` `workspaces` — the latter only if it carries JS/TS. Forget the place that applies and resolution breaks **silently** (uv can't find the member; Bun won't link it). See `references/adding-a-package.md`.
- **One lock.** The root `uv.lock` is the only Python lockfile — dev, tests, and every docker image resolve from it (`uv sync --frozen --package <name>`). The runner is invoked the same way: `uv run --package runner runner` (the orchestrator's `runner_cmd` default; the in-cluster ray image overrides via `RASK_RUNNER_CMD` since it ships the console script on PATH).
- **`service-kit` stays dependency-light.** Its only deps are `storage`, `fastapi`, `pydantic`, `pydantic-settings`, `python-dotenv`. **Never** add `lancedb`, `ray`, or `sqlmodel` — those belong to the packages that need them (`core`, `ray-kit`, …). service-kit is shared by every service including the DB-free ones (`volumes_api`, `search_api`, `ray_api`).
- **Do not resurrect `viewer` or `control`.** The monolithic `viewer` service was dissolved (2026-06) into the gateway + per-domain services + the `core` package. There is no `control` package. New domain code lands in an existing package or a new one — never a revived monolith.
- **The `viewer`/`viewer_*` names live on only as the `core` package's `main.py` (dev convenience) and `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` settings.** Don't infer a deployable from them.

## When to load each reference

| Need                                                                        | Read                             |
| --------------------------------------------------------------------------- | -------------------------------- |
| Step-by-step: add a new member or deployable without breaking resolution    | `references/adding-a-package.md` |
| The full service fleet, ports, and which package each entrypoint composes   | `references/service-fleet.md`    |
