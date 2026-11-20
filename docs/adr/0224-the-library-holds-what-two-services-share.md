# 0224. The library holds what two services share, and imports no service (2026-11-20)

**Status:** accepted; amends [ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md)
decision 3. Source: LH-578 (#805), decided by the owner on 2026-11-20 after three audits on the row: "Step by step, one
concern per commit."

## Context

ADR 0199 decision 3 split `packages/lakehouse` into ten subpackages named after the reference catalog's `service/`
directory (`errors`, `secrets`, `authn`, `authz`, `storage`, `lance`, `catalog_store`, `events`, `serve`, `api`), in
one linear order held by a `layers` contract. Its names and its counts were read on 2026-11-09, on a tree that has
since lost every client and Dapr.

Measured with `ast` on 2026-11-20 (tree `081bd2047`), and rebuilt from scratch by a second reviewer with identical
edges:

- **127 modules, 24,986 lines, 263 import edges and no cycle.** 82 modules sit at the top level.
- **The ten need 28 imports cut first**, 18 of them because `serve` would hold primitives everything below it imports
  (`supervision`, `context`). The reference's own `service/` imposes no order: its concerns import each other and the
  layers above them, so the order was not the reference's either.
- **Twelve modules are imported by one service alone**: the catalog's `route_path`, `governed.deps`, `clone_endings`,
  `version_retention`, `provenance_stamp`, `schema`, `blobs`, `lancekit.blobs` and `idempotency`; lineage's
  `lancekit.reader`, `lancekit.writer` and `lancekit.catalog_client`. Three more that one service imports stay, since
  each is shared by design: `lancekit.commit_verdict` (the commit vocabulary of every plane), `lancekit.absence`
  (written for every plane) and `governed.subjects` (named by module path in `docs/COMMIT-EVENTS.md`).
- **No split changes what a service imports.** Every service already reaches every heavy dependency through the
  library, so the split is a question of where code is read and what direction it may import in.

## Decision

1. **The ten subpackages in a linear order are not built.** The split is derived from rask's own import graph, not
   copied from another catalog's directory names.
2. **A module one service alone imports lives in that service.** The twelve move: to the catalog as
   `catalog.api.route_path`, `catalog.api.security.ANONYMOUS_SUBJECT`, `catalog.services.clone_ending_records`,
   `catalog.core.version_retention`, `catalog.core.provenance_stamp`, `catalog.core.schema_facet` (beside
   `catalog.schemas`, so not `schema`), `catalog.core.blobs` (one module for the two blob files) and
   `catalog.services.idempotency_records` (beside the door seam `catalog.api.idempotency`); to lineage as
   `lineage.services.catalog_client`, `catalog_reader` and `catalog_writer`.
3. **Maintenance's own classifier of a failed pass stays maintenance's.** `maintenance.core.lineage_emit.classify_retryable`
   reads any failure a pass records, as text, and answers whether a re-run may succeed; `lakehouse.lancekit.commit_verdict`
   reads a commit's exception and answers what it means to the caller of a door. Measured 2026-11-20, they answer
   three texts differently (`commit conflict: not retryable`, `retry limit exceeded on commit conflict`,
   `concurrent writer won`), so they are two rules and not a copy of one. Making them one is a change to what a
   failed pass's event or a door's answer says, and is decided on its own.
4. **The library imports no service**, by any chain: `.importlinter`'s `the-library-imports-no-service` forbids
   `catalog`, `lineage` and `maintenance` from `lakehouse`. It was kept before it was written, so it costs nothing and
   stops the first edge that would make one service run another's code.
5. **Grouping the rest by job follows one concern per commit**, each commit paying its own group's debts (a declared
   `__all__`, no eager facade, a name that collides with nothing), and each judged on the graph as it then stands.

## Why

- **Code shape only.** Points 1, 2, 3 and 5 move no object, value, identity, credential or door: they meet the four
  facts by changing none of them. GitOps, cloud native and zero trust are untouched, and the catalog's doors still read
  and write through lance-namespace and Lance as before.
- **Cloud native**, point 4: each service is a workload of its own composed from one shared library, and a library that
  reached into a service would tie two workloads' code together.

## Consequences

- Each moved module's tests move with it, to `services/catalog/tests` or `services/lineage/tests`; the root suites that
  drive several members keep theirs, importing the new names.
- `pyproject.toml`'s per-file ignore for the `/query` reader follows it to `services/lineage/src/lineage/services/catalog_reader.py`.

## The service app group (2026-11-20)

The first group by job (point 5) is `lakehouse.service_app`: the factory every service boots from and the layers only
it installs. Measured with `ast` on `a9673cdcb` (module-level, function-local and `TYPE_CHECKING` imports), a module
joined when its job is the app's boot or request plane and nothing else in the library imports it (the graph test
is necessary, the job decides), since `factory` imports `governed.audit` and an importer in the
rest of the library would make the group and the rest import each other.

| Module | In the group as |
| --- | --- |
| `lakehouse.lance_app` | `lakehouse.service_app.factory` |
| `lakehouse.body_limit` | `lakehouse.service_app.body_limit` |
| `lakehouse.deadline` | `lakehouse.service_app.deadline` |
| `lakehouse.panic_catcher` | `lakehouse.service_app.panic_catcher` |
| `lakehouse.no_store` | `lakehouse.service_app.no_store` |
| `lakehouse.thread_pool` | `lakehouse.service_app.thread_pool` |

Each declares `__all__`, and the package's `__init__` exports nothing. The log handler `lakehouse.app` is named for
what it is, `lakehouse.log_handler`, so no module shares a name with the app plane.

Left out, each for a later group:

- `log_handler` (`setup_logging`): the chart's model writer `governed.auth.write_model` logs through it.
- `exceptions`, `probes`, `draining`: the issuer mirror's own app (`governed.issuer_mirror`) and `governed.oidc`,
  `governed.machine_identity` and `governed.signing_key` import them.
- `ns_errors`, `problem`, `spec_routes`, `outage`, `read_refusal`: the spec's error answers, which the catalog's doors
  raise and build directly; `ns_errors` imports `problem`, so the two stay together.
- `admission`: the caller's judge, which each service's `api.security` builds on; its job is authentication's.
- `obs` (`configure_app_logging`): imported by the services only, but it is the process's logging, with `log_handler`.
- `context`, `supervision`, `lifecycle`, `health`: primitives the bus, the auth plane and the store session import.

`.importlinter`'s `the-service-app-is-imported-by-the-services-only` (a `protected` contract) holds it: only `catalog`,
`lineage` and `maintenance` import `lakehouse.service_app`, and its own modules import each other. It was kept before
it was written; a planted `import lakehouse.service_app.deadline` in `lakehouse.supervision` broke it.
