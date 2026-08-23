---
name: rask-architecture
description: "Where new code belongs in the rask workspace: the language-pure planes (Python `packages/` + `services/`, the `frontend/` bun+turbo root, sealed `runners/`) and the entrypoint contract (`make_service_app` + injectable lifespan). Use when adding, moving or deleting a workspace member, service, zone or deployable; editing a `pyproject.toml`, the root `uv.lock`, or a uv/bun workspace glob; wiring a service entrypoint; or when newly added code won't resolve — ModuleNotFoundError, a `uv sync` workspace error, or a package `bun install` silently skipped."
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

## The planes — one language per plane

The tree is split by **language first**, then by layer. Each globbed directory is single-language; that is what makes the globs safe (see the invariants).

| Plane                | Path                                | Rule                                                       | Has entrypoints?                   |
| -------------------- | ----------------------------------- | ---------------------------------------------------------- | ---------------------------------- |
| **Python packages**  | `packages/<name>`                   | reusable Python libraries                                  | **No HTTP app.** One-shot CLIs tolerated — `ratch` ships `[project.scripts] ratch` |
| **Python services**  | `services/<name>`                   | runnable Python: HTTP services                             | Yes                                |
| **sealed runners**   | `runners/<name>`                    | model environments with their **own** `pyproject.toml` — and their own `uv.lock` **only where they build an image** (`assist`, `dummy`, `htr`); the offline Ray Data runners ship a pyproject alone and let Ray install the env on workers via `runtime_env`. Matched by **no** workspace glob | Yes (Ray entrypoints) |
| **JS/TS frontend**   | `frontend/`                         | its own **bun + turbo workspace root** (own `package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`) | — |
| ↳ zones              | `frontend/microfrontends/<zone>`    | the 7 SvelteKit MFE apps                                   | Yes                                |
| ↳ JS libraries       | `frontend/packages/<name>`          | reusable TS/Svelte libraries                               | **No**                             |
| **scripts**          | `scripts/`                          | ALL dev/ops scripts, shell **and** python, flat            | one-shot, **not** a workspace member |

Current Python packages: `lineage-kit`, `ratch`, `ray-kit`, `service-kit`, `storage`, `tracker`, `validate`. (`htr` is **not** a package — it is `runners/htr`, sealed and outside every glob.)
Current Python services (13): `gateway`, `compute`, `controlplane`, `ingest`, `flows`, `notifications` — plus the lance plane (`catalog`, `lineage`, `medallion`, `maintenance`, `viewer`, `search`, `annotator`). (`notifications` is the per-subject inbox behind the bell, `:8850`, app-id `notifications` — see `rask-services-fleet`.) (`compaction` was renamed `maintenance` in 06cc7579 — it compacts, optimizes indices, cleans up old versions *and* reconciles cross-store drift, so it is named for all four, not one. `core`/`core_api`/`search_api`/`volumes_api` died in the R6/R20 media wave.)
Current sealed runners (9): `asr`, `assist`, `diarize`, `dummy`, `htr`, `insid3`, `kg`, `topics`, `voiceprint`. (`dummy` is the GPU-free lane prover — real CDF read → merge_insert → fragment commit, no model download; `insid3` is pinned to python 3.10.)
Current JS packages: `api`, `config`, `dockview`, `engine`, `explorer-api`, `flow`, `labeling`, `ui`, `zone-contract` — see `rask-frontend`.
Current zones (7, verified against `frontend/microfrontends/` 2026-08-09): `home` (catch-all, base `''`), `annotator`, `compute`, `lakehouse`, `explorer`, `studio`, `models` — each based at a bare `/<zone>`. **`models` REPLACED `train`** (on train's port 5178); a leftover `microfrontends/train/` on a dev host is untracked build residue, not a zone. `overview`/`discover`/`storage` are **retired**; `/storage` and `/data` are routes *inside* `lakehouse`.
Current deployables (each = a workspace member + a `.docker/<name>.dockerfile`): `gateway`, `compute` (R22 — `compute` on every surface), `controlplane`, `ingest`, `notifications`, `runner`, `assist-runner` — plus the one parametrized `frontend.dockerfile` built per zone (images tagged `web-<zone>:<tag>`), `ray-cluster.dockerfile` (the Ray head/Serve image) and `rest-catalog.dockerfile`, which is ONE image for **seven** lance services (`catalog`, `lineage`, `medallion` ×2 apps, `maintenance`, `viewer`, `search`, `annotator`) run with different commands. NB `make k3s-build`'s `COMPOSE_IMAGES` is still only `gateway compute controlplane` — `ingest` has a dockerfile and a chart Deployment (`:8830`) but is not in that build loop yet.

## The composition seam: `make_service_app` + injectable lifespan

`service_kit.make_service_app(*, title, routers, proxy_router=None, lifespan=None)` builds the FastAPI app with shared config/handlers/middleware. The **lifespan is injectable**: stateless services get the minimal `default_lifespan` (settings only); stateful ones pass a `LifespanFactory` (Lance/Ray/S3). Routers mount under `settings.api_prefix`; `proxy_router` mounts at root.

⚠️ **Two layouts are sanctioned — know which one you are in.** `make_service_app` is used by **5 of 13** services: `compute`, `controlplane`, `flows`, `ingest` and `notifications` (verified by call site, not by import — `gateway` mentions it in three comments and calls it nowhere). `gateway` is in neither camp — it builds `FastAPI(...)` itself, because it is a proxy, not a router host. The other seven (`annotator`, `catalog`, `lineage`, `maintenance`, `medallion` ×2 apps, `search`, `viewer`) construct `FastAPI(...)` directly with bespoke lifespans and their own `core/config.py::get_settings()`, following the `fastapi` skill's `api/v1/endpoints/` + `core/` + `services/` layout rather than the fleet's flat-module layout. Match the service you are editing; unifying one into the other is a decision, not a cleanup.

A thin fleet-layout entrypoint is **~20 lines** — import routers + a lifespan from the domain package, call the factory. `compute/__init__.py`:

```python
from compute import health, proxy, routes
from compute.lifespan import make_lifespan
from service_kit import make_service_app

app = make_service_app(
    title="compute",
    routers=[health.router, routes.router],
    proxy_router=proxy.router,
    lifespan=make_lifespan,
)
```

## The core husk is GONE (R6/R20, 2026-07-28)

`services/core` + `services/core_api` (the post-P7a transitional husk) are deleted, with `search_api` and `volumes_api`. Their capabilities live in the explorer plane: the S3 object browser is the viewer's `objects.py` endpoints (`/api/explorer/object*`); lines/EAD FTS re-land as catalog-governed Lance tables behind `/api/explorer/search` (docs/architecture/lance-ns-merge.md R6). Do not resurrect them.

## Hard invariants (the gotchas)

- **Workspace membership is globbed — and that is only safe because every globbed dir is single-language.** Root `pyproject.toml` has `[tool.uv.workspace] members = ["packages/*", "services/*"]`; `frontend/package.json` has `workspaces = ["microfrontends/*", "packages/*"]` (paths relative to `frontend/`). Drop a directory in the right plane and it is a member — **no manifest edit**. The safety condition is the language purity, and the two toolchains fail **asymmetrically** when it breaks: a dir under a uv glob without a `pyproject.toml` is a **hard error** (`Workspace member … is missing a pyproject.toml`), fixable only by an `exclude` list (enumeration by another name); a dir under a bun glob without a `package.json` is **SILENTLY skipped** — bun prints "Done!" and the package is simply never installed, built, linted or tested, and nothing says so. So: **never put a JS package under root `packages/`/`services/`, and never put a Python package under `frontend/`.** (The root manifest also notes `runners/*` is deliberately matched by *no* glob — sealed model envs whose heavy pins must never enter the fleet's resolution.) See `references/adding-a-package.md`.
- **One lock.** The root `uv.lock` is the only Python lockfile — dev, tests, and every fleet docker image resolve from it (`uv sync --frozen --package <name>`). The sealed `runners/htr` project carries its **own** lock and is invoked via `uv run --project runners/htr runner` (in-cluster the ray image ships the console script on PATH).
- **`service-kit` keeps a light base.** Base deps are `storage`, `fastapi`, `pydantic`, `pydantic-settings`, `python-dotenv`, **`dapr>=1.18.1`, and 8 OpenTelemetry packages** — the SDK, the OTLP/HTTP exporter, and instrumentors for fastapi, httpx, logging, requests, grpc and aiohttp-client. The last three landed 2026-08-23: the fleet runs bare `uvicorn` with no `opentelemetry-instrument` launcher, so whatever `setup_otel` names is ALL the instrumentation it gets, and without grpc + aiohttp the app→sidecar hop carried no `traceparent` and every Dapr span rooted a new trace. The heavy Lance/Ray deps live behind the `[governed]` / `[lakehouse]` / `[lancekit]` extras — keep them there. **Never** add `lancedb`, `ray`, or `sqlmodel` to the base: service-kit is shared by every service including the storeless ones (`gateway` via `setup_otel`, `compute`).
- **`known-first-party` is stale and is silently drifting.** Root `pyproject.toml:143` lists 7 names; **18** first-party modules exist (7 packages + 11 services). Missing: `annotator, catalog, controlplane, ingest, lineage, lineage_kit, maintenance, medallion, ratch, search, viewer`. Step 4 of `references/adding-a-package.md` is being skipped on every lance-service landing — add the name when you add the member, and ruff's import sorting stays correct.
- **Membership is globbed; TEST ENROLMENT IS NOT — and the asymmetry is where suites go missing.** A
  directory dropped into `packages/`/`services/` is a workspace member with no manifest edit, but
  `[tool.pytest.ini_options] testpaths` is an EXPLICIT list. So a new member's `tests/` runs nowhere until
  someone adds the path, and the run stays green while it does. Three suites landed green-by-absence this
  way (`services/catalog`, `services/lineage` — one pinning a privilege escalation, one a commit
  duplication — enrolled 2026-08-09). `tests/unit/test_invariants.py::test_every_workspace_test_directory_is_in_the_root_testpaths`
  now gates it in both directions, but ONLY over `packages/*/tests` and `services/*/tests`: a new
  **top-level** `tests/<x>/` is still ungated, which is exactly how `tests/e2e-py` was lost once.
  Measured 2026-08-22, still open: `services/search` (2,614 LOC), `services/viewer` (4,288) and
  `packages/ratch` (7,602) ship **no tests at all** — 14,504 lines the estate cannot regress-test. That is
  a scope decision, not an oversight; it is tracked in `open_python-audit.md` (E9), not here.
- **A sealed runner's tests are invisible to the root pytest, and to CI.** `runners/*` is matched by no
  glob by design, so `make test` names `runners/htr` and `make test-slow` names `htr` + `dummy` — by hand.
  `dagger call test` runs the root testpaths only and says so in its own doc comment, so the 75 test
  functions that exist in the runners execute in **no CI job**. Seven of the nine ship no tests at all.
  A lockfile's absence in those seven is NOT a defect — see the plane table above: a runner carries a
  `uv.lock` only where it builds an image.
- **Do not resurrect `viewer` or `control`.** The monolithic `viewer` service was dissolved (2026-06) into the gateway + per-domain services. There is no `control` package. New domain code lands in an existing package or a new one — never a revived monolith.
- **`viewer` now means the lance media viewer** (`services/viewer`, `:8101`) — the old rask viewer monolith and the `RASK_VIEWER_*` settings are gone.

## When to load each reference

| Need                                                                        | Read                             |
| --------------------------------------------------------------------------- | -------------------------------- |
| Adding, moving or deleting a member or deployable — **run its checklist to the end**; step 4 (`known-first-party`) is the one that gets skipped | `references/adding-a-package.md` |
| The full service fleet, ports, and which package each entrypoint composes   | `references/service-fleet.md`    |

## Sibling skills

`rask-services-fleet` (the gateway + per-service routing) · `rask-frontend` (zones, data, gates) · `rask-styling` (`@rask/ui`) · `rask-lance-catalog` (the catalog, governance, maintenance).

A runner's internals are deliberately undocumented here: each `runners/<workload>` is sealed and owns its
own pipeline, models and GPU packing. There is no per-workload skill — one would make that modality look
privileged, which is the opposite of how this platform is built.
