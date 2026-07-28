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
Current Python services: `gateway`, `ray_api` (the `ray` service), `controlplane` — plus the lance plane (`catalog`, `lineage`, `medallion`, `compaction`, `viewer`, `search`, `annotator`). (`core`/`core_api`/`search_api`/`volumes_api` died in the R6/R20 media wave.)
Current JS packages: `api` (@rask/api, the valibot client), `ui` (@rask/ui, the design system), `zone-contract` (@rask/zone-contract, the cross-zone link guard test).
Current zones: `home` (catch-all), `overview`, `compute`, `discover`, `storage`, `train`, `studio`.
Current deployables (each = a workspace member + a `.docker/<name>.dockerfile`): `gateway`, `ray` (uv member `ray-api` — a Python package named `ray` would shadow PyPI ray), `controlplane`, `runner` — plus the one parametrized `frontend.dockerfile` built per zone, `ray-cluster.dockerfile` (the Ray head/Serve image) and `rest-catalog.dockerfile` (the lakehouse+media image).

## The composition seam: `make_service_app` + injectable lifespan

`service_kit.make_service_app(*, title, routers, proxy_router=None, lifespan=None)` builds the FastAPI app with shared config/handlers/middleware. The **lifespan is injectable**: stateless services get the minimal `default_lifespan` (settings only); stateful ones pass a `LifespanFactory`, e.g. `core.lifespan.make_lifespan` (DB/Lance/Ray/S3). Routers mount under `settings.api_prefix` (`/api/v1`); `proxy_router` mounts at root.

A thin entrypoint is **~20 lines** — import routers + a lifespan from the domain package, call the factory. `ray_api/__init__.py`:

```python
from ray_api import health, proxy, routes
from ray_api.lifespan import make_lifespan
from service_kit import make_service_app

app = make_service_app(
    title="ray",
    routers=[health.router, routes.router],
    proxy_router=proxy.router,
    lifespan=make_lifespan,
)
```

## The core husk is GONE (R6/R20, 2026-07-28)

`services/core` + `services/core_api` (the post-P7a transitional husk) are deleted, with `search_api` and `volumes_api`. Their capabilities live in the media plane: the S3 object browser is the viewer's `objects.py` endpoints (`/api/media/object*`); lines/EAD FTS re-land as catalog-governed Lance tables behind `/api/media/search` (docs/architecture/lance-ns-merge.md R6). Do not resurrect them.

## Hard invariants (the gotchas)

- **Workspace membership is globbed — and that is only safe because every globbed dir is single-language.** Root `pyproject.toml` has `[tool.uv.workspace] members = ["packages/*", "services/*"]`; `frontend/package.json` has `workspaces = ["microfrontends/*", "packages/*"]` (paths relative to `frontend/`). Drop a directory in the right plane and it is a member — **no manifest edit**. The safety condition is the language purity, and the two toolchains fail **asymmetrically** when it breaks: a dir under a uv glob without a `pyproject.toml` is a **hard error** (`Workspace member … is missing a pyproject.toml`), fixable only by an `exclude` list (enumeration by another name); a dir under a bun glob without a `package.json` is **SILENTLY skipped** — bun prints "Done!" and the package is simply never installed, built, linted or tested, and nothing says so. So: **never put a JS package under root `packages/`/`services/`, and never put a Python package under `frontend/`.** (The root manifest also notes `runners/*` is deliberately matched by *no* glob — sealed model envs whose heavy pins must never enter the fleet's resolution.) See `references/adding-a-package.md`.
- **One lock.** The root `uv.lock` is the only Python lockfile — dev, tests, and every fleet docker image resolve from it (`uv sync --frozen --package <name>`). The sealed `runners/htr` project carries its **own** lock and is invoked via `uv run --project runners/htr runner` (in-cluster the ray image ships the console script on PATH).
- **`service-kit` stays dependency-light.** Its only deps are `storage`, `fastapi`, `pydantic`, `pydantic-settings`, `python-dotenv`. **Never** add `lancedb`, `ray`, or `sqlmodel` — those belong to the packages that need them (`core`, `ray-kit`, …). service-kit is shared by every service including the DB-free ones (`gateway` via `setup_otel`, `ray_api`).
- **Do not resurrect `viewer` or `control`.** The monolithic `viewer` service was dissolved (2026-06) into the gateway + per-domain services + the `core` package. There is no `control` package. New domain code lands in an existing package or a new one — never a revived monolith.
- **`viewer` now means the lance media viewer** (`services/viewer`, `:8101`) — the old rask viewer monolith and the `RASK_VIEWER_*` settings are gone.

## When to load each reference

| Need                                                                        | Read                             |
| --------------------------------------------------------------------------- | -------------------------------- |
| Step-by-step: add a new member or deployable without breaking resolution    | `references/adding-a-package.md` |
| The full service fleet, ports, and which package each entrypoint composes   | `references/service-fleet.md`    |
