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
| **Python packages**  | `packages/<name>`                   | reusable Python libraries (`ray-cluster-env` NAMES the Ray images' environment and ships only their tracing hooks) | **No entrypoints** — the one CLI exception (`ratch`) died with its package at the 2026-08-28 dissolution |
| **Python services**  | `services/<name>`                   | runnable Python: HTTP services, and the bring-your-own table-maintenance job     | Yes                                |
| **sealed runners**   | `runners/<name>`                    | model environments with their **own** `pyproject.toml` — and their own `uv.lock` **only where they build an image** (`assist`, `dummy`, `htr`); the offline Ray Data runners ship a pyproject alone and let Ray install the env on workers via `runtime_env`. Matched by **no** workspace glob | Yes (Ray entrypoints) |
| **JS/TS frontend**   | `frontend/`                         | its own **bun + turbo workspace root** (own `package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`) | — |
| ↳ zones              | `frontend/microfrontends/<zone>`    | the 7 SvelteKit MFE apps                                   | Yes                                |
| ↳ JS libraries       | `frontend/packages/<name>`          | reusable TS/Svelte libraries                               | **No**                             |
| **scripts**          | `scripts/`                          | ALL dev/ops scripts, shell **and** python, flat            | one-shot, **not** a workspace member |

Current Python packages: `client-kit` (code only the lakehouse's clients run: the media factory, the dataset registry, the rate limiter), `lineage-kit`, `ray-cluster-env` (the Ray images' environment and tracing hooks), `ray-kit`, `service-kit` (the lakehouse's kit, carrying no client code), `storage`, `validate`. (`htr` is **not** a package — it is `runners/htr`, sealed and outside every glob.)
Current Python services (10): `compute`, `flows`, `notifications` — plus the lance plane (`catalog`, `lineage`, `maintenance`, `viewer`, `search`), `annotator`, a client of the lakehouse with its own release (ADR 0171), and `table-maintenance`, a bring-your-own CronJob with its own release that compacts each table the catalog lists as due that it may maintain, importing no rask package (ADR 0174). (`notifications` is the per-subject inbox behind the bell, `:8850`, app-id `notifications` — see `rask-services-fleet`.) There is no edge service: the platform's Gateway routes each public `/api/*` prefix to its service through the chart's HTTPRoutes (ADR 0165, `rask-services-fleet`). Decided changes to this list, not yet built: `lineage` shrinks to provenance — an OpenLineage intake, a Lance-stored commit feed and edge list, and a forwarder — and the Postgres+AGE graph becomes bring-your-own (ADR 0158, LH-468 #592). `maintenance` decides and rewrites nothing (ADR 0162, LH-467 #587): compaction and index builds are a bring-your-own job's, committed through the catalog's `version/create`.
Current sealed runners (9): `asr`, `assist`, `diarize`, `dummy`, `htr`, `insid3`, `kg`, `topics`, `voiceprint`. (`dummy` is the GPU-free lane prover — real CDF read → merge_insert → fragment commit, no model download; `insid3` is pinned to python 3.10.)
Current JS packages: `api`, `config`, `dockview`, `engine`, `explorer-api`, `flow`, `labeling`, `ui`, `zone-contract` — see `rask-frontend`.
Current zones (7, verified against `frontend/microfrontends/` 2026-08-09): `home` (catch-all, base `''`), `annotator`, `compute`, `lakehouse`, `explorer`, `studio`, `models` — each based at a bare `/<zone>`. **`models` REPLACED `train`** (on train's port 5178); a leftover `microfrontends/train/` on a dev host is untracked build residue, not a zone. `overview`/`discover`/`storage` are **retired**; `/storage` and `/data` are routes *inside* `lakehouse`.
Current deployables (each = a workspace member + a `.docker/<name>.dockerfile`): `compute` (R22 — `compute` on every surface; deployed by its own fixture chart, `deploy/fixtures/compute`, not the product chart), `annotator` (deployed by its own client chart, `deploy/fixtures/annotator`, with the assist runner, not the product chart; ADR 0171), `table-maintenance` (a CronJob in its own release, `deploy/fixtures/table-maintenance`; ADR 0174), `flows` (a client no chart in this repo deploys), `notifications`, `runner`, `assist-runner` — plus the one parametrized `frontend.dockerfile` built per zone (images tagged `web-<zone>:<tag>`), `ray-cluster.dockerfile` (the Ray head/Serve image, which the engine runs: in development the `ray-cluster` fixture's head; the rask chart renders no Ray), `rest-catalog.dockerfile`, which is ONE image for the **three** lakehouse services (`catalog`, `lineage`, `maintenance`) run with different commands and installs no client, and `media.dockerfile`, one image for the clients `viewer` and `search`, which no chart in this repo deploys. The lakehouse runs no ingest service (ADR 0154) and no stage, training or engine (ADR 0155): data enters through the catalog's namespace doors, ingest of an external source is a bring-your-own client job, and moving data between tiers is the user's pipeline on their own compute. The medallion (bronze→silver→gold) is tier governance in the catalog's write path.

**The product chart deploys these and nothing it does not own** (ADR 0153). The prerequisites (the External Secrets
Operator, the Dapr control plane, NATS JetStream, the CloudNativePG operator, the Ray engine, the telemetry store) and
the development stand-ins (Dex, RustFS, OpenBao) and the clients with their own release (`compute`, `annotator`) are each their own release, installed in development from
`deploy/fixtures/<name>` by `make k3s-fixtures`; OpenFGA and the Postgres (`rask-age`) behind OpenFGA and the Dapr state store stay optional in the
chart and are the platform's in production (`auth.fgaApiUrl`, `age.externalHost`). A new deployable is a chart
Deployment; a new piece of infrastructure is a fixture, never a subchart (`rask-helm` §2).

## The composition seam: three app factories, one per plane

`service_kit.make_service_app(*, title, routers, proxy_router=None, lifespan=None)` builds the FastAPI app with shared config/handlers/middleware. The **lifespan is injectable**: stateless services get the minimal `default_lifespan` (settings only); stateful ones pass a `LifespanFactory` (Lance/Ray/S3). Routers mount under `settings.api_prefix`; `proxy_router` mounts at root.

⚠️ **THREE FACTORIES ARE SANCTIONED — know which plane you are in.** All nine services come out
of a factory, and which one is decided by the plane, not by taste:

| Plane | Factory | Services | Routers mount |
| --- | --- | --- | --- |
| fleet | `service_kit.make_service_app` | `compute`, `flows`, `notifications` | under `settings.api_prefix` |
| media | `client_kit.media.app.build_media_app` | `viewer`, `search`, `annotator` | at the root; `MediaSettings`; CORS exposes the Range headers |
| lance | `service_kit.lance_app.build_lance_service_app` | `catalog`, `lineage`, `maintenance` | at the root; each service's own `core/config.py::get_settings()` |

The media three also share ONE lifespan (`client_kit.media.lifespan.make_media_lifespan`),
with `setup`/`teardown` hooks for what is genuinely per-service (the annotator's actor plane).

The non-fleet services keep the `fastapi` skill's `api/v1/endpoints/` + `core/` + `services/` layout
rather than the fleet's flat-module layout. The ENTRYPOINT is the factory's: a `main.py` that opens its
own `app = FastAPI(...)` is the exception to justify, not the norm (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12 counted eight of them repeating one boot, and the
copies had drifted — one service had lost its request-id layer entirely).

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

`services/core` + `services/core_api` (the post-P7a transitional husk) are deleted, with `search_api` and `volumes_api`. Their capabilities live in the explorer plane: the S3 object browser is the viewer's `objects.py` endpoints (`/api/explorer/object*`); lines/EAD FTS re-land as catalog-governed Lance tables behind `/api/explorer/search` (docs/adr/0112-the-media-plane-absorbs-rask-s-discovery-and-viewing-r5-r6.md). Do not resurrect them.

## Hard invariants (the gotchas)

- **Workspace membership is globbed — and that is only safe because every globbed dir is single-language.** Root `pyproject.toml` has `[tool.uv.workspace] members = ["packages/*", "services/*"]`; `frontend/package.json` has `workspaces = ["microfrontends/*", "packages/*"]` (paths relative to `frontend/`). Drop a directory in the right plane and it is a member — **no manifest edit**. The safety condition is the language purity, and the two toolchains fail **asymmetrically** when it breaks: a dir under a uv glob without a `pyproject.toml` is a **hard error** (`Workspace member … is missing a pyproject.toml`), fixable only by an `exclude` list (enumeration by another name); a dir under a bun glob without a `package.json` is **SILENTLY skipped** — bun prints "Done!" and the package is simply never installed, built, linted or tested, and nothing says so. So: **never put a JS package under root `packages/`/`services/`, and never put a Python package under `frontend/`.** (The root manifest also notes `runners/*` is deliberately matched by *no* glob — sealed model envs whose heavy pins must never enter the fleet's resolution.) See `references/adding-a-package.md`.
- **One lock.** The root `uv.lock` is the only Python lockfile — dev, tests, and every fleet docker image resolve from it (`uv sync --frozen --package <name>`). The sealed `runners/htr` project carries its **own** lock and is invoked via `uv run --project runners/htr runner` (in-cluster the ray image ships the console script on PATH).
- **`service-kit` keeps a light base.** Base deps are `storage`, `fastapi`, `pydantic`, `pydantic-settings`, `python-dotenv`, **`dapr>=1.18.1`, and 8 OpenTelemetry packages** — the SDK, the OTLP/HTTP exporter, and instrumentors for fastapi, httpx, logging, requests, grpc and aiohttp-client. The last three landed 2026-08-23: the fleet runs bare `uvicorn` with no `opentelemetry-instrument` launcher, so whatever `setup_otel` names is ALL the instrumentation it gets, and without grpc + aiohttp the app→sidecar hop carried no `traceparent` and every Dapr span rooted a new trace. The heavy Lance deps live behind the `[governed]` / `[lakehouse]` / `[lancekit]` extras — keep them there. **Never** add `lancedb`, `ray`, or `sqlmodel` to service-kit at all: it is the lakehouse's kit, and code only a client runs belongs in `client-kit` (the media factory, the lancedb dataset registry, the slowapi rate limiter), which the lakehouse image refuses at its import gate (`--absent client_kit lancedb`).
- **`known-first-party` is COMPLETE — keep it that way.** It lists every first-party import name (7 code-shipping packages + the services); a name missing from it sorts into the THIRD-PARTY block, and a backlog of ten missing names cost one repo-wide re-sort of 391 files (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md X3). Step 4 of `references/adding-a-package.md` is the step that keeps it complete: add the import name in the SAME change that adds the member, and remove it in the change that deletes one.
- **Membership is globbed, and so is test enrolment — except at the top level.** A directory dropped
  into `packages/`/`services/` is a workspace member with no manifest edit, and
  `[tool.pytest.ini_options] testpaths` globs `packages/*/tests` and `services/*/tests`, so its `tests/`
  runs in the root suite the moment it exists. An explicit list is how suites go missing while the run
  stays green (`services/catalog`, `services/lineage` — one pinning a privilege escalation, one a commit
  duplication — ran nowhere until 2026-08-09). A new **top-level** `tests/<x>/` matches no glob and must
  be added to `testpaths` by hand, which is exactly how `tests/e2e-py` was lost once.
  Measured 2026-08-22: `services/search` and `services/viewer` shipped **no tests at all** (`packages/ratch` was the third — dissolved 2026-08-28); both have since gained suites (`test_the_gate_authorizes_the_table_it_reads`, `test_the_search_door_is_wired`, the viewer's gating suites), and the residue is tracked in the lakehouse register, row Q3-37 (drained 2026-09-10; in git history)/Q3-38 (the blanket ruff exemption, and `ray_kit.submit` untested), not here.
- **A sealed runner's tests are invisible to the root pytest, and to CI.** `runners/*` is matched by no
  workspace or testpaths glob by design, so `make test` and `make test-slow` loop over every
  `runners/*/tests` themselves, running each suite from inside its runner directory with
  `uv run --frozen pytest` (a runner with a suite therefore commits its own `uv.lock`).
  `dagger call test` runs the root testpaths only and says so in its own doc comment, so the 75 test
  functions that exist in the runners execute in **no CI job**. Seven of the nine ship no tests at all.
  A lockfile's absence in those seven is NOT a defect — see the plane table above: a runner carries a
  `uv.lock` only where it builds an image.
- **Do not resurrect `viewer` or `control`.** The monolithic `viewer` service was dissolved (2026-06) into per-domain services. There is no `control` package. New domain code lands in an existing package or a new one — never a revived monolith.
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
