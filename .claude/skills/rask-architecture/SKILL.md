---
name: rask-architecture
description: The rask workspace planes (the Python `packages/` + `services/` dirs, the JS `frontend/` root) and the entrypoint-over-package composition contract — service-kit's make_service_app + injectable lifespan. Use when adding/moving a workspace member or service, editing a pyproject.toml or the uv/bun workspace globs, wiring a new entrypoint, debugging "module not found"/uv-resolution after adding code, or deciding where new code belongs (lib vs runnable vs deployable).
---

# rask architecture (workspace planes + composition)

The single most rask-specific thing to get right: **which plane a package lives in, and how entrypoints compose packages**. Get this wrong and uv/bun resolution breaks (loudly for uv, *silently* for bun) or you smear a domain across layers. Defers Python idioms to `writing-python`, FastAPI routing to `fastapi`, container builds to `dockerfile`.

> **There is no `projects/` layer.** The Polylith-style `projects/<name>` composition stubs were removed (2026-07). Deployables are ordinary workspace members built by `.docker/<name>.dockerfile` running `uv sync --frozen --package <name>` against the **root** `uv.lock`. Never recreate per-deployable pyprojects or per-deployable locks.

## When to use

- Adding, moving, or deleting a workspace member (`packages/*`, `services/*`, or anything under `frontend/`).
- Wiring a new HTTP service entrypoint or editing one.
- Editing any `pyproject.toml` `members`/`dependencies`, or `frontend/package.json` `workspaces`.
- "ModuleNotFoundError" / uv won't resolve a first-party import after you added code.
- Deciding where new code belongs: reusable lib, runnable component, or deployable.

## The planes — don't blur them

The tree is split by **language first**, then by layer. Each globbed directory is single-language; that is what makes the globs safe (see the invariants).

| Plane                | Path                                | Rule                                                       | Has entrypoints?                   |
| -------------------- | ----------------------------------- | ---------------------------------------------------------- | ---------------------------------- |
| **Python packages**  | `packages/<name>`                   | reusable Python libraries                                  | **No** — never an `app`/`main`/CLI |
| **Python services**  | `services/<name>`                   | runnable Python: HTTP services, the `runner` CLI, `core`   | Yes                                |
| **JS/TS frontend**   | `frontend/`                         | its own **bun + turbo workspace root** (own `package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`) | — |
| ↳ zones              | `frontend/microfrontends/<zone>`    | the 7 SvelteKit MFE apps                                   | Yes                                |
| ↳ JS libraries       | `frontend/packages/<name>`          | reusable TS/Svelte libraries                               | **No**                             |
| **scripts**          | `scripts/`                          | ALL dev/ops scripts, shell **and** python, flat            | one-shot, **not** a workspace member |

Current Python packages: `htr`, `ray-kit`, `service-kit`, `storage`, `tracker`, `validate`.
Current Python services: `core`, `core_api`, `gateway`, `orchestrator`, `ray_api`, `runner`, `search_api`, `volumes_api`.
Current JS packages: `api` (@rask/api, the valibot client), `ui` (@rask/ui, the design system), `zone-contract` (@rask/zone-contract, the cross-zone link guard test).
Current zones: `home` (catch-all), `overview`, `compute`, `discover`, `storage`, `train`, `studio`.
Current deployables (each = a workspace member + a `.docker/<name>.dockerfile`): `core-api`, `gateway`, `orchestrator`, `ray-api`, `runner`, `search-api`, `volumes-api` — plus the one parametrized `frontend.dockerfile` built per zone.

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

`services/core` is the **core domain package** (package `core`): owns `alembic/`, `db.py`, `lifespan.py`, models, repositories, domain services, endpoints, and `main.py` (the monolith for tests + `make viewer`). It is **composed by two thin entrypoints** — `core_api` (:8801, orchestrator loop OFF) and `orchestrator` (:8810, loop ON via `RASK_ORCHESTRATOR_AUTOSTART`). They are **two processes over one package sharing the `batches` table transactionally** — not independent services. The loop must run in exactly one process; that's why it's split by config, not by code.

## Hard invariants (the gotchas)

- **Workspace membership is globbed — and that is only safe because every globbed dir is single-language.** Root `pyproject.toml` has `[tool.uv.workspace] members = ["packages/*", "services/*"]`; `frontend/package.json` has `workspaces = ["microfrontends/*", "packages/*"]` (paths relative to `frontend/`). Drop a directory in the right plane and it is a member — **no manifest edit**. The safety condition is the language purity, and the two toolchains fail **asymmetrically** when it breaks: a dir under a uv glob without a `pyproject.toml` is a **hard error** (`Workspace member … is missing a pyproject.toml`), fixable only by an `exclude` list (enumeration by another name); a dir under a bun glob without a `package.json` is **SILENTLY skipped** — bun prints "Done!" and the package is simply never installed, built, linted or tested, and nothing says so. So: **never put a JS package under root `packages/`/`services/`, and never put a Python package under `frontend/`.** (The root manifest also notes `runners/*` is deliberately matched by *no* glob — sealed model envs whose heavy pins must never enter the fleet's resolution.) See `references/adding-a-package.md`.
- **One lock.** The root `uv.lock` is the only Python lockfile — dev, tests, and every docker image resolve from it (`uv sync --frozen --package <name>`). The runner is invoked the same way: `uv run --package runner runner` (the orchestrator's `runner_cmd` default; the in-cluster ray image overrides via `RASK_RUNNER_CMD` since it ships the console script on PATH).
- **`service-kit` stays dependency-light.** Its only deps are `storage`, `fastapi`, `pydantic`, `pydantic-settings`, `python-dotenv`. **Never** add `lancedb`, `ray`, or `sqlmodel` — those belong to the packages that need them (`core`, `ray-kit`, …). service-kit is shared by every service including the DB-free ones (`volumes_api`, `search_api`, `ray_api`).
- **Do not resurrect `viewer` or `control`.** The monolithic `viewer` service was dissolved (2026-06) into the gateway + per-domain services + the `core` package. There is no `control` package. New domain code lands in an existing package or a new one — never a revived monolith.
- **The `viewer`/`viewer_*` names live on only as the `core` package's `main.py` (dev convenience) and `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` settings.** Don't infer a deployable from them.

## When to load each reference

| Need                                                                        | Read                             |
| --------------------------------------------------------------------------- | -------------------------------- |
| Step-by-step: add a new member or deployable without breaking resolution    | `references/adding-a-package.md` |
| The full service fleet, ports, and which package each entrypoint composes   | `references/service-fleet.md`    |
