---
name: rask-architecture
description: "Where new code belongs in the rask workspace: the language-pure planes (Python `packages/` + `services/`, the `frontend/` bun root holding the console and its packages) and the entrypoint contract (the one app factory, `build_lance_service_app`, with an injectable lifespan). Use when adding, moving or deleting a workspace member, service, frontend package or deployable; editing a `pyproject.toml`, the root `uv.lock`, or a uv/bun workspace glob; wiring a service entrypoint; or when newly added code won't resolve — ModuleNotFoundError, a `uv sync` workspace error, or a package `bun install` silently skipped."
---

# rask architecture (workspace planes + composition)

The single most rask-specific thing to get right: **which plane a package lives in, and how entrypoints compose
packages**. Get this wrong and uv/bun resolution breaks (loudly for uv, *silently* for bun) or you smear a domain
across layers. Defers Python idioms to `writing-python`, FastAPI routing to `fastapi`, container builds to
`dockerfile`.

This repository holds **the lakehouse and its console, nothing else** (owner, 2026-11-04; XC-192). Every other client —
flows, compute, the annotator, the viewer and search, table-maintenance, the sealed runners, their zones and kits —
lives outside it, recovered from git history at `64ef118a9` when it moves to a repository of its own.

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
| **Python services** | `services/<name>` | runnable Python: the lakehouse's HTTP services | Yes |
| **JS/TS frontend** | `frontend/` | its own **bun workspace root** (own `package.json`, `bun.lock`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`); no Turborepo, plain bun scripts | — |
| ↳ the app | `frontend/console` | the console, the lakehouse's own UI: one SvelteKit single-page app (`rask-frontend`) | Yes |
| ↳ JS libraries | `frontend/packages/<name>` | reusable TS/Svelte libraries | **No** |
| **scripts** | `scripts/` | ALL dev/ops scripts, shell **and** python, flat | one-shot, **not** a workspace member |

**Python packages** (1): `lakehouse` (`packages/lakehouse`, ADR 0195), the lakehouse's one library, which only its
own services import and no client: the lance-plane app factory (`lance_app`) and what it installs (exceptions, the
request-id middleware, logging, the probes and the drain, the body cap), the auth plane (`governed`), the naming and
the input and credential guards, the records, registries and governance guards, the namespace clients, the events (the
control lane, the outbox, the OpenLineage builders), the OpenLineage emission and Ed25519 signing seam
(`lakehouse.lineage`), and the S3 client, STS and URI seam (`lakehouse.storage`). One package, organised by module: a
new package exists only for a real reason of its own (a backend swapped behind an interface, a leaf with no lakehouse
imports, a language necessity), never for an audience, because a client reaches the lakehouse through its doors and
events and imports none of it.

**Python services** (3): `catalog` (`:2333`), `lineage` (`:8000`), `maintenance` (`:8000`) — the lakehouse, one image
(`.docker/rest-catalog.dockerfile`) run with three commands. This repository hosts no client: the notifications inbox
is retired (ADR 0193), and every other client left with XC-192.

**JS packages** (1): `@rask/ui` (the design system, `rask-styling`), read from source. Code with one consumer lives in
that consumer (ADR 0201): the console holds its typed `/api/*` client with the bearer provider, the generated OpenAPI
types, the lineage client and the live-cursor reader (`console/src/lib/api`), its Svelte Flow binding and project
hierarchy (`console/src/lib/flow`) and the workbench's dock (`console/src/lib/dock`, on horizon-layout, ADR 0200). See
`rask-frontend`.

**Deployables** (each = a member + a `.docker/<name>.dockerfile`): `rest-catalog` (image `lance-rest-catalog`, the
three lakehouse services) and `frontend` (image `web-console`, the console's static-file server), both deployed by the
product chart; plus `cnpg-age-ext` and `marquez` for the
development stand-ins. The lakehouse runs no ingest service (ADR 0154) and no stage, training or engine (ADR 0155): data
enters through the catalog's namespace doors, and moving data between tiers is the user's pipeline on their own compute.

**The product chart deploys the lakehouse, its console, and nothing it does not own** (ADR 0153, ADR 0193). The prerequisites (the External
Secrets Operator, the Dapr control plane, NATS JetStream, the CloudNativePG operator, the telemetry store) and the
development stand-ins (Dex, RustFS, OpenBao, Marquez) are each their own release, installed in development from
`deploy/fixtures/<name>` by `make k3s-fixtures` (its `FIXTURES` list). A new lakehouse deployable is a chart
Deployment; a new piece of infrastructure is a fixture, never a subchart (`rask-helm` §2); a client is a release of its
own, in its own repository, whose admissions it supplies to a lakehouse install with `-f`.

## The composition seam: one app factory

`lakehouse.lance_app.build_lance_service_app(*, title, docs_enabled, audit_enabled, lifespan, log, ready_check,
routers=(), inner_middleware=())` builds `catalog`, `lineage` and `maintenance`: routers mount at the root, config
is each service's own `core/config.py::get_settings()`, and the factory owns the logging, the docs gate, the audit
trail, the problem handlers, the request-id layer and `/livez` + `/readyz`. The **lifespan is injectable** and
required: each service passes its own, which builds its resources and sets the lifecycle flags `/readyz` reads. So is
`ready_check`: each service's `/readyz` also answers for its own dependency or fact (the catalog's namespace and
signing key, lineage's feed, maintenance's signing key).
A client is an outside tool on the public API and builds its own app (owner, 2026-11-07). The lance services keep the `fastapi` skill's `api/v1/endpoints/` + `core/` + `services/` layout; the factory owns
the boot, not the layout. A `main.py` that opens
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
- **`lakehouse` declares every dependency the three services run on, with no extras.** Every importer installs all of
  it, so an extra would guard nothing. The OTel SDK, the OTLP/HTTP exporter and the instrumentors are among them: the
  chart launches every lakehouse service under `opentelemetry-instrument` when telemetry is on, and it instruments what
  is installed, so that list is the instrumentation a service gets. The lakehouse image import-gates the library and
  the three services and refuses `lancedb` (`.docker/rest-catalog.dockerfile`, `--absent`). Never add `lancedb`, `ray`
  or `sqlmodel` to it.
- **The lakehouse knows no client.** `.importlinter`'s `the-lakehouse-knows-no-client` forbids every lakehouse module
  from importing a client's package (`ray`, `lance_ray`, `lancedb`, …) by any chain, and
  `scripts/lakehouse_names_no_client.py` (in `make lint`) refuses a client's name in any file the lakehouse owns, the
  console's tree (`frontend/`, `.docker/frontend.dockerfile`) included, since the console is the lakehouse's own. It
  reads hosted clients off the tree (every member outside the lakehouse, every non-platform fixture; none today), so
  it needs no edit when one is added, and a client that leaves the tree joins `DEPARTED` in
  the commit that deletes it, as `notifications` and `mailpit` did (ADR 0193).
- **`known-first-party` is COMPLETE — keep it that way.** It lists every first-party import name (`catalog`,
  `lakehouse`, `lineage`, `maintenance`); a name missing from it sorts
  into the THIRD-PARTY block, and a backlog of ten missing names once cost one repo-wide re-sort of 391 files. Step 4 of
  `references/adding-a-package.md` keeps it complete: add or remove the name in the change that adds or deletes the
  member.
- **Membership is globbed, and so is test enrolment — except at the top level.** `[tool.pytest.ini_options] testpaths`
  globs `packages/*/tests` and `services/*/tests`, so a member's `tests/` runs in the root suite the moment it exists.
  An explicit list is how suites go missing while the run stays green. A new **top-level** `tests/<x>/` matches no glob and must be added to `testpaths` by hand.

## When to load each reference

| Need | Read |
| --- | --- |
| Adding, moving or deleting a member or deployable — **run its checklist to the end**; step 4 (`known-first-party`) is the one that gets skipped | `references/adding-a-package.md` |
| The services, ports, and which package each entrypoint composes | `references/service-fleet.md` |

## Sibling skills

`rask-services-fleet` (the Gateway routes + the services behind them) · `rask-frontend` (the console: routes, data,
sign-in, gates) · `rask-styling` (`@rask/ui`) · `rask-lance-catalog` (the catalog, governance, maintenance).
