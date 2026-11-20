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
other. It was kept before it was written; a planted `import lakehouse.events.outbox_metrics` in `lakehouse.timers`
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
own modules import each other. It was kept before it was written; a planted `import lakehouse.governed.fga` in
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
another module, beyond the four named imports `ignore_imports` lists. It was kept before it was written; a planted
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
