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
   `catalog`, `lineage` and `maintenance` from `lakehouse`. The contract held before it was added, so it costs nothing and
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

## The events group (2026-11-20)

The second group by job is `lakehouse.events`: what the lakehouse's services publish and take on the bus. Measured with
`ast` on `ea704efb7` (module-level, function-local, `TYPE_CHECKING` and relative imports), the candidates import the
rest of the library (`context`, `lifecycle`, `supervision`, `projected_token`, `storage`, `storage_options`, `objectfs`,
`naming`) and are imported back by one module, `health`, under `TYPE_CHECKING` for the bus's readiness dependency;
nothing they import reaches them again, so the group and the rest import in one direction. The name collides with no
module or installed package (no top-level `events` exists), and it does not read like a service the way
`lakehouse.lineage` read like the `lineage` service, so that subpackage folds in and its name is free.

| Module | In the group as |
| --- | --- |
| `lakehouse.bus` | `lakehouse.events.bus` |
| `lakehouse.bus_delivery` | `lakehouse.events.bus_delivery` |
| `lakehouse.bus_metrics` | `lakehouse.events.bus_metrics` |
| `lakehouse.cloud_events` | `lakehouse.events.cloud_events` |
| `lakehouse.control_events` | `lakehouse.events.control_events` |
| `lakehouse.control_emit` | `lakehouse.events.control_emit` |
| `lakehouse.lineage_events` | `lakehouse.events.lineage_events` |
| `lakehouse.openlineage` | `lakehouse.events.openlineage` |
| `lakehouse.outbox` | `lakehouse.events.outbox` |
| `lakehouse.outbox_metrics` | `lakehouse.events.outbox_metrics` |
| `lakehouse.signature_doors` | `lakehouse.events.signature_doors` |
| `lakehouse.lineage.schemas` | `lakehouse.events.openlineage_schemas` |
| `lakehouse.lineage.signing` | `lakehouse.events.signing` |
| `lakehouse.lineage.keys` | `lakehouse.events.published_keys` |
| `lakehouse.lineage.door` | `lakehouse.events.door_verdict` |

The module names stay where they already say what they hold; the four from `lakehouse.lineage` take the name of their
content, since `schemas`, `keys` and `door` say nothing beside the others. `lakehouse.lineage`'s `__init__`, an eager
facade of 52 names from its four modules, is not kept: every importer names the defining module, each module declares
`__all__`, and `lakehouse.events`'s `__init__` exports nothing. `docs/COMMIT-EVENTS.md` names the new paths in the same change:
its section on the names that carry the contract points into the library for the lakehouse's own producers, and a
client still reads the events with no import from it.

Left out:

- `control_prefixes`: it names the outbox's prefix among the store's control segments, but its job is the store's
  layout, and thirteen record modules import it.
- `governed.subjects`: the address rule a notifier reads, which is the auth plane's judgement of a subject;
  `docs/COMMIT-EVENTS.md` names it where it is.
- `governed.signing_key`: the service's own key, read from the secret store under its login; it names the signing
  seam in prose and takes its loader by injection.
- `health`: the readiness probe, whose bus dependency reads the connection's state; it is the one importer in the
  library the contract allows.
- `context`, `projected_token`, `supervision`, `lifecycle`: primitives the bus builds on and the auth plane and the
  store session import too.

`.importlinter`'s `the-events-are-imported-by-the-services-and-the-probe-only` (a `protected` contract) holds it: only
`catalog`, `lineage`, `maintenance` and `lakehouse.health` import `lakehouse.events`, and its own modules import each
other. The contract held before it was added; a planted `import lakehouse.events.outbox_metrics` in `lakehouse.timers`
broke it.

A module's logger and tracer are named by its path, so five names on a record or span follow the group:
`lakehouse.bus` (logger and tracer), `lakehouse.bus_delivery`, `lakehouse.outbox`, `lakehouse.control_emit` and
`lakehouse.openlineage` are `lakehouse.events.<module>`. No alert rule, dashboard or collector route names them; the
meters (`lance.bus`, `lance.outbox`, `lance.<service>`) are named by hand and keep their names.

## The auth plane group (2026-11-20)

The third group by job is the auth plane, `lakehouse.governed`: who a caller is, what it may do, and the trail of both.
It already existed under that name, which some 1,970 references use, so the name stays and this commit pays the group's
debts and takes in its stragglers. Measured with `ast` on `a5124d9ac` (module-level, function-local, `TYPE_CHECKING`
and relative imports; 111 modules, 230 edges):

- **No eager facade.** `governed/__init__.py` held a docstring and no import, and the one module that hands on names it does not define is `fga`, which
  re-exports the SDK's `ClientTuple` and `RelationshipCondition` on purpose and lists both in its `__all__`. `governed/auth/` had no `__init__.py`: an implicit namespace package, which the
  import graph `.importlinter` judges did not contain, so no contract saw `write_model`'s imports. It is a regular
  package now, with a docstring only.
- **No `__all__`** in fifteen of the group's sixteen modules (`decider` declared one), nor in two of the three
  stragglers below (`admission` declared one). Each now declares the public names it defines.
- **Edges.** The group imports `context`, `log_handler`, `supervision`, `naming`, `draining`, `exceptions`,
  `lifecycle`, `probes`, `schemas.health` and `health`. The rest imports the group from `service_app.factory`
  (`audit`), `store_session` (`machine_identity`, `secrets`, `store_credential`), `storage_options` and `objectfs`
  (`store_credential`), and, with this commit, `events.bus` (`projected_token`).

| Module | In the group as |
| --- | --- |
| `lakehouse.admission` | `lakehouse.governed.admission` |
| `lakehouse.projected_token` | `lakehouse.governed.projected_token` |
| `lakehouse.otlp_identity` | `lakehouse.governed.otlp_identity` |

Each joins because its job is authentication's and its move closes no loop: `admission` judges a request's caller
before its body is read and imports only `governed.settings`; `otlp_identity` is the session every OTLP export presents
the pod's token with and imports only `governed.machine_identity`; `projected_token` reads the token every caller that
presents this pod's identity sends, imports nothing in the library, and is imported by `machine_identity`, `oidc`,
`secrets` and the bus's login, so the bus becomes one more importer of the group and the group imports nothing of the
events. The OTel credential entry point `rask_service_account` (`packages/lakehouse/pyproject.toml`) names
`lakehouse.governed.otlp_identity:session`; the chart names only the entry point, so no chart value changes. None of
the three has a logger or a tracer, so no record or span name changes.

Left out:

- `store_session`: the service's store login, exchanging its ServiceAccount token for a session. Its job is the
  store's credential, and it imports `storage_options`, which imports `governed.store_credential`, so in the group it
  would make the group and the rest import each other through the store.
- `storage_options`, `objectfs`, `endpoint_scheme`, `storage.sts`: the store's credential guard and plumbing; they carry
  a credential and judge no caller.
- `context`: the trace id a request runs under is a correlation id, never evidence (its own docstring), and the bus,
  the problem answers and the log handler import it.

Observed and not changed here: the group and the rest already import each other through the readiness probe, since
`governed.signing_key` takes `health.Dependency`, and `health` imports `objectfs`, which imports
`governed.store_credential`. Cutting it means `store_credential` living with the store or the probe's `Dependency` type
living below both, and is a question for the group that holds the store.

`.importlinter`'s `the-auth-plane-is-imported-by-the-services-and-its-named-users-only` (a `protected` contract) holds
it: only `catalog`, `lineage`, `maintenance`, `lakehouse.service_app.factory`, `lakehouse.events.bus`,
`lakehouse.store_session`, `lakehouse.storage_options` and `lakehouse.objectfs` import `lakehouse.governed`, and its
own modules import each other. The contract held before it was added; a planted `import lakehouse.governed.fga` in
`lakehouse.timers` broke it.

**Proposed, for a commit of its own: `fga.py` by job.** Its 2,317 lines (before `__all__`) do six jobs, each with
importers of its own (production files, measured by the names each uses):

| Job | Lines | Public names | Importers |
| --- | --- | --- | --- |
| The model as data: load, type restrictions, `admits`, machine relations, hierarchy edges | 97-98, 165-253, 315-321 | `load_model`, `load_model_dsl`, `admits`, `machine_relations`, `hierarchy_edge_tuples` | 7 catalog files, `maintenance.services.rebuild` |
| User and object ids | 254-314, 322-375 | `user_object`, `subject_of`, `grantee_object`, `is_object_id`, `canonical_object_id`, `table_object`, ... | 39 catalog, 4 lineage and 2 maintenance files, `governed.decider`, `scripts/lane_estate_owner.py` |
| The call's resilience: timeout, retry policy, circuit breaker, the fail-closed guard | 100-164, 376-515 | `RetryPolicy`, `DEFAULT_RETRY`, `ONE_ATTEMPT`, `reset_circuit` | the reads and writes, `governed.decider`, 2 catalog files |
| The store and its model: client configuration, provision, resolve, model history | 516-998 | `make_client`, `provision`, `resolve`, `ModelHistory`, `canonical_model`, `newest_store` | `governed.auth_lifespan`, `governed.auth.write_model`, 2 scripts |
| Reads: check, batch check, list objects and users, expand, read tuples and changes | 999-1796 | `check`, `batch_check`, `list_users`, `read_object_tuples`, `dispose`, ... | `governed.decider`, `governed.auth_lifespan`, 16 catalog and 2 lineage files |
| Writes: grants, revokes, the hierarchy's edges, the census, the audit of each | 1797-2317 | `write_tuples`, `delete_tuples`, `grant_on_create`, `census`, `move_parent`, ... | 15 catalog and 2 maintenance files |

The split moves every `banned-api` entry in `pyproject.toml` that names `lakehouse.governed.fga.*` with the name it
bans, and every `monkeypatch` target on `fga`, in the same commit.

## The store group (2026-11-20)

The fourth group by job is the store, `lakehouse.storage`: how a lakehouse process reaches its object store and holds
its session there. The subpackage already held the clients, listing, URI parsing and the error family, so the name
stays and the stragglers move in. Measured with `ast` on `af6e9b907` (module-level, function-local, `TYPE_CHECKING` and
relative imports; 112 modules, 230 edges), a module joined when its job is reaching the store and the group's edges to the rest
run one way: the group imports four modules of the rest (`base_refs`, `supervision`, `governed.machine_identity`,
`governed.secrets`), none of which reaches the group again, and 21 modules of the rest import the group.

| Module | In the group as |
| --- | --- |
| `lakehouse.storage_options` | `lakehouse.storage.options` |
| `lakehouse.endpoint_scheme` | `lakehouse.storage.endpoint_scheme` |
| `lakehouse.objectfs` | `lakehouse.storage.objectfs` |
| `lakehouse.store_session` | `lakehouse.storage.store_session` |
| `lakehouse.stored_objects` | `lakehouse.storage.stored_objects` |
| `lakehouse.records` | `lakehouse.storage.conditional_records` |
| `lakehouse.governed.store_credential` | `lakehouse.storage.credential` |

`storage_options` would read `storage.storage_options`, so it takes `options`. `records` takes the name of what it
holds, the JSON records the store arbitrates with `If-None-Match` and `If-Match`, so it is not read as the catalog's
records, which are a later group. `store_session` keeps its name, since a bare `session` is a common local name in its
importers. `store_credential` holds the key pair a store credential is carried as and the 503 raised while none is held;
its only importers in the library were `options`, `objectfs` and `store_session`, so its job is the store's.

**The facade.** `storage/__init__.py` re-exported 13 names from its five modules. 46 import statements (42
`from lakehouse.storage import`, 4 `import lakehouse.storage as storage`, which tests patched through) and 11 dotted
references in prose, patch targets and the `boto3` ban message named it; each now names the defining module, and the
`__init__` is a docstring. That keeps `endpoint_scheme` light as its own docstring requires: a settings module importing
it loads neither boto3 nor pyarrow. `options.object_client` imports `lakehouse.storage.client` at call time, so the tests
that replace the S3 client patch `lakehouse.storage.client.s3_client`, where it is looked up. Every module of the group
declares `__all__`; `log` is left out of each.

Left out:

- `record_store`: the hashed-key layout under every control-root registry (its key bytes are the registries'
  persistence contract); it imports the group and the group does not import it, so it waits for the records group.
- `base_refs`: the rule for comparing a manifest's and the catalog's spellings of a location, a table concern;
  `objectfs` imports it.
- `lance_session`, `table_paths`, `data_directories`, `file_identities`: Lance tables and their layout.
- `health`: the readiness probe, whose store dependency builds on the group.

**The loop.** ADR 0224's auth plane section recorded `governed.signing_key` -> `health` -> `objectfs` ->
`governed.store_credential`. With the key pair in the store the chain ends in the group, and `governed` is imported from
the store by `store_session` alone. The auth plane and the rest still import each other through the probe on one chain:
`governed.signing_key` -> `health` -> `events.bus` (under `TYPE_CHECKING`, the bus dependency's type) ->
`governed.projected_token`. It is cut by the probe's `Dependency` type living in a module that imports neither the bus
nor the store, so that `signing_key` imports no probe that reaches them; that is the probe's group, not this one.

`.importlinter`'s `the-store-imports-only-its-named-dependencies-in-the-library` (a `forbidden` contract from
`lakehouse.storage` to `lakehouse.*`) holds it: the group imports nothing else in the library, directly or through
another module, beyond the four named imports `ignore_imports` lists. The contract held before it was added; a planted
`import lakehouse.record_store` in `lakehouse.storage.uri` broke it. The auth plane's contract names
`lakehouse.storage.store_session` as its one importer in the store, since `options` and `objectfs` import nothing of
it.

`objectfs` imports `base_refs` at module level: the function-local import's stated reason, that `base_refs` imports
`objectfs`, does not hold (`base_refs` imports only the standard library).

A module's logger is named by its path, so three record names follow the group: `lakehouse.store_session`,
`lakehouse.records` and `lakehouse.stored_objects` are `lakehouse.storage.store_session`,
`lakehouse.storage.conditional_records` and `lakehouse.storage.stored_objects`. No alert rule, dashboard, collector
route or test names them. The file audit logger `lakehouse.file_audit` is named by hand and keeps its name. No module
of the group has a tracer.

## The Lance table plane group (2026-11-20)

The fifth group by job is the Lance table plane, `lakehouse.lancekit`: how every service reads and judges a Lance table.
Measured with `ast` on `fb10c462b` (module-level, function-local, `TYPE_CHECKING` and relative imports; 112 modules, 242
edges), the brief's two candidate halves are two jobs, not one: the table plane (what a manifest, a version, a
transaction, a branch and a location mean to Lance) and the catalog's records (the registries and governance records
kept under the control root). Their edges run one way: 6 record modules import 5 table modules (`base_refs` from five,
`features` from two, `branch_layout`, `lance_session` and `transactions` from `clone_sources`), and no table module
imports a record. Of the table candidates, the ten below import nothing in the library but each other (`features` ->
`manifest_wire`, `table_layout` -> `branch_layout`), and the rest imports them from the records, the store
(`storage.objectfs` -> `base_refs`) and `table_paths`, so the group is the floor of the library and this commit does
it.

`lancekit` already held four modules of the job (`versions`, `filters`, `absence`, `commit_verdict`), so the name stays:
16 references name it, which a plainer name would move for no gain in meaning, and it says what it holds (Lance primitives). Its
`__init__` was a docstring with no import, so no facade is removed: 0 references reached a name through it. Every module
of the group declares `__all__` (twelve did not: the ten joining and `versions` and `filters`); `log` is left out of
each.

| Module | In the group as |
| --- | --- |
| `lakehouse.features` | `lakehouse.lancekit.features` |
| `lakehouse.manifest_wire` | `lakehouse.lancekit.manifest_wire` |
| `lakehouse.manifest_names` | `lakehouse.lancekit.manifest_names` |
| `lakehouse.transactions` | `lakehouse.lancekit.transactions` |
| `lakehouse.branch_layout` | `lakehouse.lancekit.branch_layout` |
| `lakehouse.table_layout` | `lakehouse.lancekit.table_layout` |
| `lakehouse.base_refs` | `lakehouse.lancekit.base_refs` |
| `lakehouse.lance_session` | `lakehouse.lancekit.lance_session` |
| `lakehouse.auto_cleanup` | `lakehouse.lancekit.auto_cleanup` |
| `lakehouse.listing_floor` | `lakehouse.lancekit.listing_floor` |

Each keeps its name: none collides inside the package, and `lance_session` keeps its name beside the function of the same name it
defines, which its importers call. `auto_cleanup` and `listing_floor` join as manifest config keys a committer writes and a
reader judges a table by (Lance's `lance.auto_cleanup.*` and the floor's stamp), not as records: neither reads or
writes the control root. `base_refs` joins as Lance's spellings of a location (a manifest's `base_paths` against
`describe_table`'s answer); the store's contract names `lakehouse.storage.objectfs -> lakehouse.lancekit.base_refs` as
its one import of it.

Left out:

- `table_paths`: lists a table's objects through the store (`storage.stored_objects`), and the store imports the group,
  so in it the group and the store would import each other.
- `arrow_ipc`: the Arrow IPC body of the namespace's write doors, a wire of the spec, not of a table.
- `outage`: whether the store answered, read with the spec's error answers (`ns_errors` imports it).
- `lance_metrics`: the process's telemetry, bridging Lance's IO meters into OpenTelemetry.
- `derived_tables`: lineage's edge list walked for erasure, a provenance read.
- `data_directories`, `file_identities`, `clone_sources`, `compaction_gate`: each imports a record
  (`base_registry`, `location_claims`, `record_store`), so each is the records' or above them.

`.importlinter`'s `the-lance-table-plane-imports-nothing-else-in-the-library` (a `forbidden` contract from
`lakehouse.lancekit` to `lakehouse.*`, with no ignored import) holds it. The contract held before it was added; a planted
`import lakehouse.naming` in `lakehouse.lancekit.versions` broke it.

A module's logger is named by its path, so two logger names follow the group: `lakehouse.features` and
`lakehouse.lance_session` are `lakehouse.lancekit.features` and `lakehouse.lancekit.lance_session`. No alert rule,
dashboard or collector route names them; the one test that reads one (`caplog` in
`tests/unit/test_a_permanent_base_denial_is_reported_once.py`) names it as `features.__name__`. No module of the group
has a tracer.

**Proposed, for a commit of its own: the catalog's records.** Measured on the same tree after this commit, 19 modules
(`record_store`, `control_prefixes`, `object_records`, `project_records`, `warehouse_records`, `location_claims`,
`trash`, `protection`, `maintenance_policies`, `maintenance_due`, `history_fences`, `commit_records`, `commit_turns`,
`base_registry`, `clone_sources`, `file_identities`, `data_directories`, `compaction_gate`, `table_paths`) import only
`lakehouse.lancekit`, `lakehouse.storage` and `lakehouse.naming` (`warehouse_records`) in the rest of the library, and
one module of the rest imports them: `timers` (`control_prefixes`, for its window claims' prefix). A subpackage holding
them, with `timers` either joining as a claim on the control root or named as the one importer, is held by a
`forbidden` contract from it to `lakehouse.*` ignoring the three, and moves 622 references in the same change. The
name is the group's to choose: `lakehouse.records` is free, since the store's writes it arbitrates are
`storage.conditional_records`.

## The catalog's records group (2026-11-20)

The sixth group by job is the catalog's records, `lakehouse.records`: the registries and governance records the
lakehouse keeps on the control root, and the hashed-key layout and control prefixes they are kept by. Measured with
`ast` on `5fa83ac19` (module-level, function-local, `TYPE_CHECKING` and relative imports; 112 modules, 226 edges), the
proposal above held on the graph: its 19 candidates import nothing in the library but each other, `lakehouse.lancekit`,
`lakehouse.storage` and `lakehouse.naming` (`warehouse_records` alone), and the rest of the library imports them from
`timers` (`control_prefixes`) and `compaction_gate` (`base_registry`). The graph is necessary and the job decides, so
17 join and two stay out by their job. The 17 move with every reference to them, in one change.

| Module | In the group as |
| --- | --- |
| `lakehouse.record_store` | `lakehouse.records.record_store` |
| `lakehouse.control_prefixes` | `lakehouse.records.control_prefixes` |
| `lakehouse.object_records` | `lakehouse.records.object_records` |
| `lakehouse.project_records` | `lakehouse.records.project_records` |
| `lakehouse.warehouse_records` | `lakehouse.records.warehouse_records` |
| `lakehouse.location_claims` | `lakehouse.records.location_claims` |
| `lakehouse.trash` | `lakehouse.records.trash` |
| `lakehouse.protection` | `lakehouse.records.protection` |
| `lakehouse.maintenance_policies` | `lakehouse.records.maintenance_policies` |
| `lakehouse.maintenance_due` | `lakehouse.records.maintenance_due` |
| `lakehouse.history_fences` | `lakehouse.records.history_fences` |
| `lakehouse.commit_records` | `lakehouse.records.commit_records` |
| `lakehouse.commit_turns` | `lakehouse.records.commit_turns` |
| `lakehouse.base_registry` | `lakehouse.records.base_registry` |
| `lakehouse.clone_sources` | `lakehouse.records.clone_sources` |
| `lakehouse.file_identities` | `lakehouse.records.file_identities` |
| `lakehouse.data_directories` | `lakehouse.records.data_directories` |

**The name.** `lakehouse.records` names what the modules hold, and no module or installed package uses it. The store's
primitive is named for how a write lands (`storage.conditional_records`, `If-None-Match` and `If-Match`), and these for
what is kept, so the two do not read alike. `control_records` was weighed and refused: `catalog.services.control_records`
holds the catalog's own writes of the same records. `registries` was weighed and refused: `control_prefixes`,
`clone_sources` and `data_directories` hold no registry. Every module keeps its name, `records.project_records` and its
siblings included, since none collides inside the package and each importer's local name stays what it was.

Borderline calls:

- `clone_sources` joins: `base_registry` imports it, and it imports `location_claims` and `history_fences`, so outside
  the group the two would import each other. Its job is a record too: the Lance tag a clone holds its source by, and
  the claim on the source it takes before tagging.
- `data_directories` joins: its every input is a base record and a location claim, and it is the one rule for which of
  the directories they name an ending deletes, so the rule and the records it reads are kept together.
- `project_records` joins with no import at all: it is the project registry's record, which maintenance reads beside
  the catalog that writes it.
- `compaction_gate` stays out: it is the judge of a compaction (`version/create` and maintenance's due list), keeps no
  record, and asks the base record one question (`is_own`).
- `table_paths` stays out: it lists a table's own objects at its location in both spellings, not a record on the
  control root.
- `timers` stays out and is the group's named importer: its job is the process's supervised loop, and claiming a window
  under `control_prefixes.TIMERS` is how it runs once a window, not a record the catalog keeps about an object.
- No label, contract, erasure, retention or idempotency record is in the library: each is written and read by the
  catalog alone (`catalog.services.*`), so by point 2 it lives there.

`records/__init__.py` is a docstring with no import, and every module of the group declares `__all__` (none did): the
public names it defines, with `log` left out of each.

`.importlinter`'s `the-records-import-only-the-table-plane-the-store-and-naming` (a `forbidden` contract from
`lakehouse.records` to `lakehouse.*`) holds it: the group imports nothing else in the library, directly or through
another module, beyond the 45 imports of `lancekit`, `storage` and `naming` that `ignore_imports` names one line per
edge. The contract held before it was added; a planted `import lakehouse.timers` in `lakehouse.records.protection` broke
it.

A module's logger is named by its path, so twelve logger names follow the group: `record_store`, `warehouse_records`,
`location_claims`, `trash`, `maintenance_policies`, `maintenance_due`, `history_fences`, `commit_turns`,
`base_registry`, `clone_sources`, `file_identities` and `data_directories` log as `lakehouse.records.<module>`. No alert
rule, dashboard, collector route or runbook names them; two tests read one with `caplog` by its name
(`tests/unit/test_maintenance_policies.py`, `packages/lakehouse/tests/test_a_release_frees_only_the_claim_it_read.py`)
and name the new one. No module of the group has a tracer or a meter.

`ns_errors` declared `__all__` without `PartiallyApplied` and `as_unsupported_if_stub`, which it defines and the
catalog imports; both are listed.

**What remains at the top of the library**, each with its plan:

- The runtime primitives `health`, `supervision`, `lifecycle`, `context`, `timers`, `log_handler`, `obs`, with
  `draining`, `probes` and `exceptions`: one group, once the probe's `Dependency` type lives in a module that imports
  neither the bus nor the store, which cuts the auth plane's chain through `health`.
- The spec's error answers `ns_errors`, `problem`, `spec_routes`, `outage`, `read_refusal`: one group, the answers the
  catalog's doors raise.
- The rest, `naming`, `arrow_ipc`, `compaction_gate`, `table_paths`, `derived_tables`, `lance_metrics` and `schemas`:
  each judged by its own job when its group is drawn, or left at the top where it is a leaf of one job alone.

## The process group (2026-11-20)

The seventh group by job is the process, `lakehouse.process`: what a lakehouse process is and says about itself while it
runs (ADR 0203). Measured with `ast` on `e91910dfd` (module-level, function-local, `TYPE_CHECKING` and relative imports;
113 modules, 230 edges), the ten candidates import nothing in the library but `schemas.health` (`health`, `probes`),
`events.bus` (`health`, under `TYPE_CHECKING`), `storage.objectfs` and `storage.options` (`health`), and
`records.control_prefixes`, `storage.conditional_records` and `storage.options` (`timers`); the rest imports them from
the bus, the envelope, the auth plane, the store session, the problem envelope and the service app. So the candidates
and the rest imported each other in two places, `health` and `timers`, and this commit cuts one and leaves the other
out.

| Module | In the group as |
| --- | --- |
| `lakehouse.supervision` | `lakehouse.process.supervision` |
| `lakehouse.lifecycle` | `lakehouse.process.lifecycle` |
| `lakehouse.draining` | `lakehouse.process.draining` |
| `lakehouse.health` | `lakehouse.process.health` |
| `lakehouse.probes` | `lakehouse.process.probes` |
| `lakehouse.context` | `lakehouse.process.correlation` |
| `lakehouse.log_handler` | `lakehouse.process.log_handler` |
| `lakehouse.obs` | `lakehouse.process.log_levels` |
| `lakehouse.health.store_dependency` | `lakehouse.storage.objectfs.store_dependency` |
| `lakehouse.health.bus_dependency` | `lakehouse.events.bus.bus_dependency` |

**The name.** `process` is what the modules are about: one running lakehouse process, its loops, its end, its flags, its
probes, its trace and its logs; ADR 0203 names the job the same way. No module or installed package uses the name.
`runtime` was weighed and refused: `maintenance.runtime` is the worker's own assembly, and `from lakehouse.runtime.health
import` inside `maintenance/runtime.py` would read as one module. `context` takes the name of what it holds, the
correlation every record is joined by (ADR 0209), since `context` beside `process` reads as the process's context and
the module already imports `opentelemetry.context` as a second `context` (15 references). `obs` takes `log_levels`
beside `log_handler`: one is the stdout handler every record goes through (6 references), the other the levels the
lakehouse's own packages log at (5 references), and `obs` named neither. `log_handler` keeps its name, since beside
`log_levels` it says which half it is.

**The `Dependency` split.** `health` held the type and the watch, and the constructors of three dependencies: the store's
(`store_dependency`, a listing of a root through `storage.objectfs`), the bus's (`bus_dependency`, typed by
`events.bus.Bus`) and OpenFGA's. `governed.signing_key` imports `health.Dependency` for the secret store's dependency, so
the auth plane reached the bus and the store through the probe:

- before, `governed.signing_key -> health -> events.bus`, `-> storage.objectfs` and `-> storage.options` (3 chains from
  `lakehouse.governed` to `lakehouse.events` or `lakehouse.storage`, every one through `health`);
- after, 0 chains from `lakehouse.governed` to either, through any module (114 modules, 229 edges).

The constructors that name the bus or the store are built beside them, as the secret store's already is beside its
user (`governed.signing_key.secret_store_dependency`): `store_dependency` in `storage.objectfs`, beside `fs_and_base`,
which is its read, and `bus_dependency` in `events.bus`, beside `Bus.answering`, which is its read. Splitting `health`
in two modules of the group was weighed and refused: the group's constructor module would still import the bus and the
store, which import the group's primitives, so the group and the two would import each other. `openfga_dependency`
stays in `health`: it reads `app.state.fga` and imports nothing. Same functions, same names, same behaviour; each of
the three services imports the two constructors from their new modules.

Borderline calls:

- `probes` joins: the router that serves `/livez` and `/readyz` off the lifecycle flags is the process telling the
  truth about itself, and the issuer mirror's own app (`governed.issuer_mirror`) and `governed.signing_key` import it,
  so the service app, which the services alone import, cannot hold it.
- `draining` joins: it flips the lifecycle flag at SIGTERM and hands the signal on; it imports `lifecycle` alone.
- `exceptions` stays out: its job is an answer, the domain error hierarchy and its RFC 9457 handler, a sibling of the
  spec's error answers below, and nothing in the group imports it. It imports nothing, so either side holds on the
  graph and the job decides.
- `timers` stays out, at the top of the library: it claims each window with a conditional write under
  `records.control_prefixes.TIMERS` through `storage.conditional_records`, and the store imports the group
  (`storage.store_session -> process.supervision`), so in the group it would make the group and the store import each
  other. Its loop is kept by a `Supervisor`, which it takes no import of.
- `schemas.health` stays out: the probe's body (`Readiness`, `Liveness`) is the process's answer by job, but `schemas` is
  left for the last group; the two edges into it are named in the group's contract.

`process/__init__.py` is a docstring with no import. Every module of the group declares `__all__`: four did not
(`correlation`, `draining`, `probes`, `log_levels`), and `health`'s lists the type, the watch, the verdict and
`openfga_dependency`.
Names a test reads from a module that are not in its `__all__` are a test's patch where the name is used
(`draining.signal`) and the bus's private seams (`_Session`, `_open`, `_PULL_SECONDS`, `NatsError`), left out.

Contracts:

- `the-process-imports-nothing-else-in-the-library` (`forbidden`, from `lakehouse.process` to `lakehouse.*`, two ignored
  imports into `lakehouse.schemas.health`). It held when it was added; a planted `import lakehouse.naming` in
  `process.lifecycle` broke it.
- `the-auth-plane-reaches-neither-the-events-nor-the-store` (`forbidden`, from `lakehouse.governed` to `lakehouse.events`
  and `lakehouse.storage`, indirect imports included), the chain this commit cuts. It held when it was added; a planted
  `import lakehouse.storage.uri` in `governed.projected_token` broke it, and a planted `import lakehouse.events.bus` in
  `process.health` broke it through `governed.signing_key -> process.health`, with the process and events contracts.
- The events contract is `the-events-are-imported-by-the-services-only`: `lakehouse.health` is not an allowed importer,
  since nothing in the library imports the events. A planted `import lakehouse.events.outbox_metrics` in
  `lakehouse.timers` broke it.
- The store's contract names five imports: the four it named, with `store_session -> process.supervision` at its path
  in the group, and `objectfs -> process.health` for the store's dependency. A planted `import lakehouse.timers` in
  `storage.uri` broke it.

A module's logger is named by its path, so three logger names follow the group: `lakehouse.health`,
`lakehouse.supervision` and `lakehouse.draining` log as `lakehouse.process.health` (`dependency_unavailable`,
`dependency_answering`), `lakehouse.process.supervision` (`background_work_ended`) and `lakehouse.process.draining`
(`drain_armed_by_sigterm`). The on-call runbook and the e2e lane name the events, not the logger, and no alert rule,
dashboard, collector route or `caplog` names any of the three; the runbook's and `docs/RESILIENCE.md`'s module paths
name the new ones. `log_levels` raises the `lakehouse` package logger, which the three inherit. No module of the group has
a tracer or a meter; `lance.timers` is named by hand and `timers` stays where it is.

**Proposed, for a commit of its own: the spec's error answers.** Measured on the same tree after this commit, `ns_errors`
(360 lines), `problem` (86), `spec_routes` (170), `outage` (118) and `exceptions` (158) import nothing in the library
but each other and `process.correlation` (`problem`), and the rest imports them from the auth plane (`exceptions`, in
`issuer_mirror`, `machine_identity` and `oidc`) and the service app (`factory`, `body_limit`, `deadline`,
`panic_catcher`), so the edges run one way. A subpackage holding the five, held by a `forbidden` contract to
`lakehouse.*` ignoring `problem -> process.correlation`, with `catalog-api-speaks-the-spec-taxonomy` naming the domain
errors' new path, moves some 107 references. `problem` and `exceptions` each define `PROBLEM_JSON`, which one module
should define. `read_refusal` is not of the group: its importers are `service_app.body_limit` and the catalog's
`api.load_shed`, two request layers, so it is the service app's. `governed.oidc` imports `exceptions`, so the commit
follows the change in flight there.

**What then remains at the top of the library:** `timers`, `naming`, `arrow_ipc`, `compaction_gate`, `table_paths`,
`derived_tables`, `lance_metrics` and `schemas`, each judged by its own job: `schemas.health` with the process, `timers`
as the process's claim on the store or a module of its own above the records, and the rest as leaves of one job each.

## The answers group (2026-11-20)

The eighth group by job is how a lakehouse service answers an error, `lakehouse.problems`: the RFC 9457 body and its media
type, the spec's error mapping onto it and the handlers every service app installs, the committed set of spec routes, the
store-outage reading, and the domain errors a rask-only door raises. Measured with `ast` on `9a5cebcde` (module-level,
function-local, `TYPE_CHECKING` and relative imports; 114 modules, 229 edges), the proposal above held: `ns_errors`,
`problem`, `spec_routes`, `outage` and `exceptions` import nothing in the library but each other (`ns_errors` ->
`outage`, `problem`, `spec_routes`) and `process.correlation` (`problem`), and the rest imports them from the auth plane
(`exceptions`, in `issuer_mirror`, `machine_identity` and `oidc`) and the service app (`factory` -> `exceptions`,
`ns_errors`; `body_limit`, `deadline`, `panic_catcher` -> `problem`). The edges run one way. 111 lines outside
`docs/adr` named the six modules' old paths; each names the new one.

| Module | In the group as |
| --- | --- |
| `lakehouse.ns_errors` | `lakehouse.problems.ns_errors` |
| `lakehouse.problem` | `lakehouse.problems.envelope` |
| `lakehouse.spec_routes` | `lakehouse.problems.spec_routes` |
| `lakehouse.outage` | `lakehouse.problems.outage` |
| `lakehouse.exceptions` | `lakehouse.problems.domain_errors` |
| `lakehouse.read_refusal` | `lakehouse.service_app.read_refusal` |

**The name.** `problems` is what a reader looking for an error answer looks for: every body the group builds or declares
is an RFC 9457 problem served as `application/problem+json`, and `lance_docs/ns_catalog/spec.yaml`'s `ErrorResponse` is
that body with the spec's `code`. No module or installed package uses the name. `errors` and `exceptions` were refused:
they read as the standard library's, and `lakehouse.storage.errors` already names the store's own failures. `refusals`
was weighed and refused: a redacted 500 is not a refusal, and `read_refusal`, which is not of the group, would read as one
of it. Inside it, `problem` takes `envelope`, since `problems.problem` names nothing and the module's own docstring calls
it the envelope every refusal carries (11 lines named it), and `exceptions` takes `domain_errors` beside `ns_errors`: one is
the spec's taxonomy and its handlers, the other the domain hierarchy a rask-only door raises and its handler, and the
pair says which taxonomy each is (19 lines named it). `ns_errors`, `spec_routes` and `outage` keep their names.

Borderline calls:

- `outage` joins: its job is which spec answer a failure gets (`store_refusal` returns code 17 or 18, and the installer's
  catch-all answers 503 by it), and nothing else in the library imports it.
- `spec_routes` joins: it is data, but the only library reader is the installer, which answers a spec route's validation
  failure 400 by it and declares each spec operation's error responses from it.
- `read_refusal` joins the service app, not this group: it builds no body and imports nothing; it is the mechanism a
  pure-ASGI layer answers by from inside the body read it hands the app, and its two importers are request layers
  (`service_app.body_limit` and the catalog's `api.load_shed`). The service app's contract admits the catalog as an
  importer, so the move costs no contract.
- `schemas.health` stays out: a probe's body is the process's answer, not an error's.

The group's debts:

- **A re-export facade.** `ns_errors` listed `PROBLEM_JSON` and `problem_body` in its `__all__` for names `problem`
  defines. Three import sites took them from there (`problem_body` in the catalog's `api.maintenance_mode` and
  `api.signing_gate`, `PROBLEM_JSON` in `tests/unit/test_framework_errors_carry_a_code.py`), and one test docstring named
  `ns_errors.problem_body`; each names `problems.envelope`. `ns_errors` still imports both for its own handlers and lists
  neither.
- **`PROBLEM_JSON` defined twice.** `problem` and `exceptions` each defined `application/problem+json`; `domain_errors`
  imports `envelope`'s, so the media type is defined once in the library and every handler and `send_problem` sends that
  one. `packages/lakehouse/tests/test_problem_extension_members.py` and `tests/unit/test_framework_errors_carry_a_code.py`
  assert the content type the two handler families send, and pass. The catalog restates the literal twice more, in
  `catalog/main.py` and `catalog/api/maintenance_mode.py` (which `api.signing_gate` imports it from); those are a
  service's, outside this group, and are named here for the catalog's own row.
- **Problem responses built more than one way** (the catalog API audit's L5), within this group's modules only. Builders:
  `envelope.problem_body` (six keys and the trace's `instance`); `ns_errors.problem_detail`, which builds the same keys
  by hand and then merges a `PartiallyApplied`'s extension members; and `domain_errors._problem`, a different body
  (`about:blank#<class>` type, four keys, no `code`, `error` or `instance`, then extension members). Emitters: a
  `JSONResponse` with `media_type=PROBLEM_JSON` at six handler sites (five in `ns_errors`, one in `domain_errors`), and
  `envelope.send_problem` for the pure-ASGI layers. So the spec's bodies go through one builder but for
  `problem_detail`, and the domain body is a second shape. Measured, not changed: for all 24 `lance_namespace` error
  classes, `problem_detail`'s body equals `problem_body(exc.code, status=…, title=<class>, detail=…, slug=<class lower>)`
  key for key and in order, so building it through `problem_body` is a refactor with no wire change, proposed for a commit
  of its own. Giving the domain body the spec's `code` changes what a rask-only door answers, and is decided on its own.

`problems/__init__.py` is a docstring with no import. Every module of the group declares `__all__`: three did not
(`envelope`, `spec_routes`, `domain_errors`). Names a module is imported for that are not in its `__all__`: the
spec-status map `ns_errors._STATUS`, which `tests/unit/test_ns_errors_contract.py` reads to pin that every `ErrorCode` is
mapped, left private.

Contract: `the-problems-import-nothing-else-in-the-library` (`forbidden`, from `lakehouse.problems` to `lakehouse.*`,
indirect imports included, ignoring `problems.envelope -> process.correlation`). It held when it was added; a planted
`import lakehouse.naming` in `problems.outage` broke it. `catalog-api-speaks-the-spec-taxonomy` forbids
`lakehouse.problems.domain_errors`.

A module's logger is named by its path, so one logger name follows the group: `lakehouse.exceptions` logs as
`lakehouse.problems.domain_errors` (`domain error`, a 5xx domain error's traceback). No alert rule, dashboard, collector
route, runbook or `caplog` names it, and `process.log_levels` raises the `lakehouse` package logger, which it inherits.
`ns_errors`' handlers log on the logger the service passes in, so the catalog's, lineage's and maintenance's names are
unchanged. No module of the group has a tracer or a meter.

**What then remains at the top of the library**, each judged by its own job:

- `naming`: the governed id delimiter and a control-plane id's shape; imported by the events, the auth plane and the
  records, so it stays a leaf of its own below all three.
- `arrow_ipc`: the Arrow IPC stream encoder, the validating decoder and its media type; a leaf of one job, imported by the
  services alone.
- `table_paths`: where a table recorded at a location keeps its objects; built on `lancekit.base_refs` and the store, so
  it belongs above both, with the records or as a leaf of its own.
- `compaction_gate`: whether compacting a dataset is refused (ADR 0162), the gate maintenance and the catalog's
  `version/create` both ask; built on `lancekit.features`, `records.base_registry` and the store, a leaf above the
  records.
- `derived_tables`: what a table fed, read from the edge list; built on `lancekit.filters`, a leaf of one job.
- `lance_metrics`: Lance's native IO metrics bridged into OpenTelemetry; imports nothing in the library, a leaf.
- `timers`: a fixed timer whose windows are claimed under `records.control_prefixes` through the store; the process's
  claim on the store or a module of its own above the records.
- `schemas`: the probe's body (`schemas.health`), imported by the process and the auth plane; the process's answer by
  job, left for the last group as the process group records.

## The authorization client, split by job (2026-11-20)

The auth plane group proposed cutting `governed/fga.py` (2,401 lines with its `__all__`, measured on `932e31331`) into six
modules by job. An `ast` pass over its 113 top-level definitions, each with the names it uses from the same file, gave the
internal graph, and it agreed with the six jobs with four corrections:

- `dispose` closes the client `make_client` builds and its docstring puts it "beside the factory", so it sits with the
  provisioning, not the reads.
- `read_subject_tuples`, `census` and its `GrantCensus`, `ADMIN_REVISION` and `BOOKKEEPING_RELATIONS` stood in the writes
  section and only read, so they sit with the reads.
- `names_someone` judges a tuple's user, so it sits with the ids.
- `DEFAULT_TIMEOUT_SECONDS` is read by `make_client` alone, so it sits with the provisioning, not the resilience.

Five private names were used by more than one job, and the library imports no private name across modules, so each is
public in its one home under the same name without its underscore: `guarded`, `retrying`, `FAIL_CLOSED`,
`UnansweredBatchItems` (`fga_resilience`) and `CHILD_EDGE_PARENT_TYPES` (`fga_model`). Every other definition is
byte-identical to its `fga.py` source but for three passages of prose (the banned path `hierarchy_edge_tuples`'s
docstring names, where `RevokeInterrupted`'s message comes from, and a `delete_tuples` comment the comment gate reads as
history) and the two call sites below.

| Module | What it holds | Lines |
| --- | --- | --- |
| `fga_model` | The model as data: `load_model`, `load_model_dsl`, `model_types`, `type_restrictions`, `admits`, `machine_relations`, `hierarchy_edge_tuples` and the parent types that take a `child` edge | 138 |
| `fga_ids` | User and object ids: `user_object`, `subject_of`, `grantee_object`, `is_object_id`, `canonical_object_id`, `parent_namespace_id`, `table_object`, `namespace_object`, `object_of`, `names_someone` | 158 |
| `fga_resilience` | `RetryPolicy`, `DEFAULT_RETRY`, `ONE_ATTEMPT`, the circuit and `reset_circuit`, the transient-failure test, `retrying`, `guarded` and `FAIL_CLOSED` | 257 |
| `fga_provision` | The store and its model: `client_configuration`, `make_client`, `dispose`, `provision`, `resolve`, `ModelHistory`, `canonical_model`, `newest_store`, `credential_refused` and the two boot errors | 563 |
| `fga_reads` | Check, batch check, list objects and users, expand and `expand_tree`, the changelog, tuple pages and whole reads, the census, and the condition clock (`condition_context`, `reject_stored_clock`) | 881 |
| `fga_writes` | Grants, revokes, the hierarchy's edge moves, `commit_unless_changed`, the per-tuple audit, `TupleOrigin`, and the SDK's `ClientTuple` and `RelationshipCondition` re-exported for the services | 534 |

Edges between them, all one way: `fga_writes` imports the reads, the ids, the model and the resilience; `fga_reads` the
ids and the resilience; `fga_provision` the model and the resilience; `fga_ids` the model. `fga_model` and
`fga_resilience` import none of the six. The name `fga` is kept by none of them: 129 files imported it as `fga` or an
alias and reached 759 names through it, and a module that kept the name would hold, as plain attributes, the names it
imports from the others, so a test written as `monkeypatch.setattr(fga, "user_object", …)` would patch a copy no caller
reads. With no `fga` module, a stale `fga.<name>` fails at import or as an unresolved attribute. Keeping `fga` as the
reads would have left their 131 references unchanged and changed the other 628.

**Module state.** Each piece lives in one module and every reader reaches that object: the circuit (`_CIRCUIT`) in
`fga_resilience`, read by `guarded` and closed by `reset_circuit`, which the root `conftest.py` calls around every test;
the `functools.cache` of `model_types` and `_type_restrictions` on their functions in `fga_model`, which `fga_ids` imports
by reference; one logger per module. No module holds a client, a lock or a cached model: a client is the caller's
(`app.state.fga`), built by `make_client`.

**Patch targets.** A name a test replaces is read at call time from the module that defines it, so a patch on that
module reaches every caller. Two calls cross modules to a name tests replace, and reach it by its module rather than by a
`from` import: `fga_provision.provision` and `resolve` call `fga_model.load_model()`, and
`fga_writes.revoke_object_tuples` calls `fga_reads.read_object_tuples`. Every `monkeypatch.setattr` on the old module
names the new home of what it patches (`OpenFgaClient` and `asyncio` on `fga_provision`, where `provision` and `resolve`
read them). For each patched name, a run in which the patched value raises fails the same tests on this commit as on
`932e31331`.

**Names.** Every log record, retry and breaker parameter, and fail-closed message is unchanged. Logger names follow the
module path, `lakehouse.governed.fga` becoming `fga_resilience` (`openfga_call_retry`, `openfga_circuit_open` and every
`openfga_*_unavailable` that `guarded` logs), `fga_provision` (the model write's three records), `fga_reads` (the
truncation and depth-cap warnings) and `fga_writes` (a revoke's and an arbitrated write's). No alert rule, dashboard,
collector route or runbook names the logger; two tests' `caplog` read it from the module that logs. The module has no
tracer or meter. The `banned-api` entries name the new paths; `fga_writes` imports `hierarchy_edge_tuples` under a
line-level `# noqa: TID251` as the sanctioned site, and a line in a service calling `fga_writes.write_tuples`,
`fga_writes.revoke_object_tuples`, `fga_provision.provision` or importing `fga_model.hierarchy_edge_tuples` is refused.

Each module declares `__all__`. Names used from a module that are not in it: the tests' reads of `fga_resilience._is_transient`,
`fga_reads._MAX_EXPAND_DEPTH`, each module's `log`, and the SDK names `fga_provision` imports (`OpenFgaClient`, `asyncio`),
which tests patch where `provision` and `resolve` read them.

Contract: `the-authorization-client-imports-one-way` (`layers` in `lakehouse.governed`: `fga_writes` over
`fga_reads | fga_provision` over `fga_ids | fga_resilience` over `fga_model`, each `|` pair independent). It held when it
was added; a planted `from lakehouse.governed import fga_writes` in `fga_model` broke it, and so did a planted import of
`fga_ids` in `fga_resilience`.

Flat modules rather than a `governed/fga/` subpackage: the prefix keeps each call site and each `banned-api` path saying which job it is (`fga_writes.write_tuples`), where `writes.write_tuples` under a package import would not, and a subpackage's `__init__` is the place a facade grows back.
