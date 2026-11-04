---
name: rask-architecture
description: "Where new code belongs in the rask workspace: the language-pure planes (Python `packages/` + `services/`, the `frontend/` bun root holding the console and its packages) and the entrypoint contract (the two app factories, `make_service_app` and `build_lance_service_app`, with an injectable lifespan). Use when adding, moving or deleting a workspace member, service, frontend package or deployable; editing a `pyproject.toml`, the root `uv.lock`, or a uv/bun workspace glob; wiring a service entrypoint; or when newly added code won't resolve — ModuleNotFoundError, a `uv sync` workspace error, or a package `bun install` silently skipped."
---

# rask architecture (workspace planes + composition)

The single most rask-specific thing to get right: **which plane a package lives in, and how entrypoints compose
packages**. Get this wrong and uv/bun resolution breaks (loudly for uv, *silently* for bun) or you smear a domain
across layers. Defers Python idioms to `writing-python`, FastAPI routing to `fastapi`, container builds to
`dockerfile`.

This repository holds **the lakehouse and its console, nothing else** (owner, 2026-11-04). Every other client —
flows, compute, the annotator, the viewer and search, table-maintenance, the sealed runners, their zones and kits —
was removed at XC-192 and is recovered from git history at `64ef118a9` when it moves to a repository of its own.

> **There is no `projects/` layer.** Deployables are ordinary workspace members built by `.docker/<name>.dockerfile`
> running `uv sync --frozen --package <name>` against the **root** `uv.lock`. Never recreate per-deployable pyprojects
> or per-deployable locks.

## When to use

- Adding, moving or deleting a workspace member (`packages/*`, `services/*`, or anything under `frontend/`).
- Wiring a new HTTP service entrypoint or editing one.
- Editing any `pyproject.toml` `members`/`dependencies`, or `frontend/package.json` `workspaces`.
- "ModuleNotFoundError" / uv won't resolve a first-party import after you added code.
- Deciding where new code belongs: reusable lib, runnable component, or deployable.

## The planes — one language per plane

The tree is split by **language first**, then by layer. Each globbed directory is single-language; that is what makes
the globs safe (see the invariants).

| Plane | Path | Rule | Has entrypoints? |
| --- | --- | --- | --- |
| **Python packages** | `packages/<name>` | reusable Python libraries | **No** |
| **Python services** | `services/<name>` | runnable Python: the lakehouse's HTTP services and the notifications inbox | Yes |
| **JS/TS frontend** | `frontend/` | its own **bun workspace root** (own `package.json`, `bun.lock`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`); no Turborepo, plain bun scripts | — |
| ↳ the app | `frontend/console` | the console, one SvelteKit single-page app (`rask-frontend`) | Yes |
| ↳ JS libraries | `frontend/packages/<name>` | reusable TS/Svelte libraries | **No** |
| **scripts** | `scripts/` | ALL dev/ops scripts, shell **and** python, flat | one-shot, **not** a workspace member |

**Python packages** (3):

- `service-kit` — the platform package a client pins by lakehouse tag (XC-186): the app factory, config, exceptions,
  middleware, health and probes, telemetry, the auth plane (`governed`), the naming a client needs, the input and
  credential guards, the contract the lakehouse's events publish, and its pytest plugin (XC-190).
- `lakehouse-kit` — the lakehouse's kernel, which only its own services import: the lance-plane app factory
  (`lance_app`), the records, registries and governance guards, the namespace clients, the outbox and control lane, and
  the S3 client, STS and URI seam (`lakehouse_kit.storage`).
- `lineage-kit` — the OpenLineage emission kernel and the Ed25519 signing and verifying seam for lineage events, used
  by the three lakehouse services and by the inbox to verify what it reads.

**Python services** (4): `catalog` (`:2333`), `lineage` (`:8000`), `maintenance` (`:8000`) — the lakehouse, one image
(`.docker/rest-catalog.dockerfile`) run with three commands — and `notifications` (`:8850`), the inbox behind the
console's bell: a client of the lakehouse's events, its own image (`.docker/notifications.dockerfile`), deployed by the
console's release (`deploy/fixtures/console`), and forbidden `lakehouse_kit` by its own import contract.

**JS packages** (4): `@rask/ui` (the design system, `rask-styling`), `@rask/api` (the typed `/api/*` client with its
bearer provider, the generated OpenAPI types, the lineage client, the live-cursor reader and the dock stores),
`@rask/dockview` (the dockview binding) and `@rask/flow` (the Svelte Flow binding). See `rask-frontend`.

**Deployables** (each = a member + a `.docker/<name>.dockerfile`): `rest-catalog` (image `lance-rest-catalog`, the
three lakehouse services, deployed by the product chart), `notifications` and `frontend` (image `web-console`, the
console's static-file server), both deployed by the console's release; plus `cnpg-age-ext` and `marquez` for the
development stand-ins. The lakehouse runs no ingest service (ADR 0154) and no stage, training or engine (ADR 0155): data
enters through the catalog's namespace doors, and moving data between tiers is the user's pipeline on their own compute.

**The product chart deploys the lakehouse and nothing it does not own** (ADR 0153). The prerequisites (the External
Secrets Operator, the Dapr control plane, NATS JetStream, the CloudNativePG operator, the telemetry store) and the
development stand-ins (Dex, RustFS, OpenBao, Marquez, and the inbox's SMTP relay Mailpit) are each their own release, installed in development from
`deploy/fixtures/<name>` by `make k3s-fixtures`; the console's release is installed after the lakehouse by
`make k3s-clients`. A new lakehouse deployable is a chart Deployment; a new piece of infrastructure is a fixture, never a
subchart (`rask-helm` §2); a client is a release of its own whose admissions live in its own `lakehouse-values.yaml`.

## The composition seam: two app factories

| Plane | Factory | Services | Routers mount |
| --- | --- | --- | --- |
| client | `service_kit.make_service_app` | `notifications` | under `settings.api_prefix` (`RASK_API_PREFIX`); `proxy_router` at the root |
| lance | `lakehouse_kit.lance_app.build_lance_service_app` | `catalog`, `lineage`, `maintenance` | at the root; each service's own `core/config.py::get_settings()` |

`make_service_app(*, title, routers, proxy_router=None, lifespan=None)` builds the FastAPI app with shared
config/handlers/middleware. The **lifespan is injectable**: a stateless service gets the minimal `default_lifespan`
(settings only); a stateful one passes a `LifespanFactory`. The lance services keep the `fastapi` skill's
`api/v1/endpoints/` + `core/` + `services/` layout; the factory owns the boot, not the layout. A `main.py` that opens
its own `app = FastAPI(...)` is the exception to justify, not the norm: copied boots drift, and one once lost its
request-id layer (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12).

## Hard invariants (the gotchas)

- **Workspace membership is globbed — and that is only safe because every globbed dir is single-language.** Root
  `pyproject.toml` has `[tool.uv.workspace] members = ["packages/*", "services/*"]`; `frontend/package.json` has
  `workspaces = ["console", "packages/*"]` (paths relative to `frontend/`). Drop a directory in the right plane and it
  is a member — **no manifest edit**. The two toolchains fail **asymmetrically** when language purity breaks: a dir
  under a uv glob without a `pyproject.toml` is a **hard error** (`Workspace member … is missing a pyproject.toml`); a
  dir under a bun glob without a `package.json` is **SILENTLY skipped** — bun prints "Done!" and the package is never
  installed, built, linted or tested. So: **never put a JS package under root `packages/`/`services/`, and never put a
  Python package under `frontend/`.** See `references/adding-a-package.md`.
- **One lock.** The root `uv.lock` is the only Python lockfile — dev, tests, and every docker image resolve from it
  (`uv sync --frozen --package <name>`).
- **`service-kit` is the platform package, and it keeps a light base.** Base deps are `fastapi`, `pydantic`,
  `pydantic-settings`, `python-dotenv`, `dapr`, and the OpenTelemetry SDK, OTLP/HTTP exporter and instrumentors (whatever
  `setup_otel` names is ALL the instrumentation a service gets, since it runs bare `uvicorn`). The auth plane's deps live
  behind `[governed]` and the Arrow guards' behind `[arrow]` — keep them there. **It imports nothing from
  `lakehouse_kit`** (`.importlinter`, `the-platform-knows-no-kernel`), and the lakehouse image refuses `notifications`
  and `lancedb` at its import gate (`.docker/rest-catalog.dockerfile`, `--absent`). Never add `lancedb`, `ray` or
  `sqlmodel` to either kit.
- **The lakehouse knows no client.** `.importlinter`'s `the-lakehouse-knows-no-client` forbids every lakehouse module
  from importing `notifications` or `ray` by any chain, and `scripts/lakehouse_names_no_client.py` (in `make lint`)
  refuses a client's name in any file the lakehouse owns; it reads the clients off the tree (every member outside the
  lakehouse, every frontend app, every non-platform fixture), so it needs no edit when one is added.
- **`known-first-party` is COMPLETE — keep it that way.** It lists every first-party import name (`catalog`,
  `lakehouse_kit`, `lineage`, `lineage_kit`, `maintenance`, `notifications`, `service_kit`); a name missing from it sorts
  into the THIRD-PARTY block, and a backlog of ten missing names once cost one repo-wide re-sort of 391 files. Step 4 of
  `references/adding-a-package.md` keeps it complete: add or remove the name in the change that adds or deletes the
  member.
- **Membership is globbed, and so is test enrolment — except at the top level.** `[tool.pytest.ini_options] testpaths`
  globs `packages/*/tests` and `services/*/tests`, so a member's `tests/` runs in the root suite the moment it exists.
  An explicit list is how suites go missing while the run stays green (`services/catalog` and `services/lineage` ran
  nowhere until 2026-08-09). A new **top-level** `tests/<x>/` matches no glob and must be added to `testpaths` by hand.

## When to load each reference

| Need | Read |
| --- | --- |
| Adding, moving or deleting a member or deployable — **run its checklist to the end**; step 4 (`known-first-party`) is the one that gets skipped | `references/adding-a-package.md` |
| The services, ports, and which package each entrypoint composes | `references/service-fleet.md` |

## Sibling skills

`rask-services-fleet` (the Gateway routes + the services behind them) · `rask-frontend` (the console: routes, data,
sign-in, gates) · `rask-styling` (`@rask/ui`) · `rask-lance-catalog` (the catalog, governance, maintenance).
