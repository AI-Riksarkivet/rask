---
name: rask-lance-catalog
description: "The Lance catalog and the governance layer above it — the Lance Namespace spec (54 ops, the 24-code problem+json error model, the `POST /v1/<object>/{id}/<action>` route grammar) and rask's own `project > warehouse > namespace > table` hierarchy: the guards, the registries, protection/trash, and `services/maintenance`. Use when adding or changing a catalog endpoint; raising an error from the catalog; touching the hierarchy guards, the project/warehouse registries or `seed_ownership`; designing create/drop/undrop/cascade or protection semantics; working on compaction, the sweep, the orphan scan or the reconciler; or answering \"is this spec-conformant\"."
---

# rask lance catalog — the spec and the estate's layer above it

Two contracts stack here, and confusing them is how bugs happen:

1. **The Lance Namespace spec** (lance.org/format/namespace/) — operations, error codes, REST
   grammar. Defines ONLY namespaces (recursive) + tables, and explicitly prescribes **no hierarchy
   enforcement** and nothing above a namespace.
2. **rask's hierarchy** — `project > warehouse > namespace > table`. Everything above a namespace is
   OURS: our objects, our guards, our lifecycle. The spec neither requires nor forbids it.

## The spec surface (verified 2026-08-04 against lance.org)

- **Operations: 54/54 ROUTED, each driven by a stock client on the deployed estate** (criterion 2's `phase1`
  module, `tests/e2e-py/test_the_catalog_is_correct.py`, XC-092). `tests/integration/test_spec_conformance.py`
  asserts that every op in the vendored `lance_docs/ns_catalog/spec.yaml` has a served route; the module takes
  its rows from the same file. Measured live 2026-10-17 (helm rev 34), past each door's gate **six answer the
  spec's 406 (code 0)**: the two index builds (the lakehouse builds nothing, ADR 0162), the two multi-table
  commits, `batch_create_table_versions` and `batch_commit_tables`, because the catalog judges one table version
  at a time (`versions.py::_batches_are_not_judged`), and `backfill_column` and `create_materialized_view`, which
  the native `dir` backend stubs. `alter_transaction` joins them when LH-221 (#143) lands; until then both
  transaction doors refuse every caller. `refresh_materialized_view` refuses every caller 403: it needs
  `can_refresh` on a view, and no view exists to hold it. **pylance's `RestNamespace` implements 51 of the 54**:
  `update_field_metadata` (served here), `batch_create_table_versions` and `batch_commit_tables` fall to the
  `LanceNamespace` base class, which answers code 0 itself and sends nothing, so a client reaches them through
  the spec's generated `lance_namespace_urllib3_client`. `CreateTableVersion` is served under
  `managed_versioning=true` (ADR 0157, LH-471): see "Stock clients commit through `version/create`" below.
  Spelling matters when grepping: the spec op and
  the served route are SINGULAR — `POST /v1/table/{id}/backfill_column` (`spec.yaml:1570`,
  `endpoints/columns.py:146`) — while `alter_table_backfill_columns` is the native method it wraps.
  **`rename_table` is NOT one of them.** It is backed in-process and answers 200; `docs/COVERAGE.md:15-17`
  carries that correction, dated 2026-08-05. It MOVES A POINTER, never bytes (2026-09-04):
  `dataplane.rename_table` retires the source pointer and re-registers the destination at the source's
  own location, so version history, row count and the directory all survive untouched — every V2 reader
  finds a table through its row's `location` (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:55`).
  THE LOCATION CLAIM IS THE DOOR'S RACE ARBITRATION ([[LH-204]]), not the call order: the `dir` backend
  arbitrates only object-id ADDs, so a register at an occupied location commits and two deregisters of one
  id both succeed (measured on pylance 12.0.0: eight barrier-threaded renames left eight live ids on one
  dataset, where `drop_table` on any destroys the others' bytes). The rename hands the location's claim
  (`service_kit.lakehouse.location_claims`, `_locations/` on the control root, put-if-not-exists then
  ETag) from source to destination before either backend call, stamped with a per-request token so two
  renames into ONE destination are not each other's retry; the losers answer 409 code 14, and a failed
  retire or destination register hands it back (unless the destination already resolves to the location:
  then the winner's state stands). A destructive drop or cascade refuses 409 while the claim names another
  live holder. **A door acts on the table its name resolved to** ([[LH-521]], ADR 0173 §7): the drop, deregister
  and rename doors take `ResolvedTableDep` (`catalog.api.dependencies.get_resolved_table`, solved once per request:
  the table's location and that location's claim with its ETag, `table_claims.resolve`) and never describe the name
  again. A drop or deregister FENCES that claim under the ETag it read before its backend call
  (`location_claims.fence`, lifted by `lift_fence`; a fence its request never lifted lapses after `LEASE`), and a
  rename hands it over under that ETag (`location_claims.hand_over`): a rename landing after the door's read
  answers the door 409 code 14, one landing after the fence answers 409 itself, and every take refuses a claim
  another request has fenced. Each refuses 409 while the claim names another live table, deregister included.
  Create, declare, register and both undrops take a claim, a recoverable drop
  keeps it for the trashed id, and destructive drops, deregister and the purge release it. A release replaces
  the claim it read with the same claim stamped `released_at`, under that claim's ETag, and never deletes the
  record ([[LH-458]], ADR 0172): a hand-over landing between its read and its write survives, and `holder` and
  `take` read a released claim as a free location. A holder that no
  longer resolves to its location is taken over only after `LEASE` (15 min). Pinned by
  `tests/integration/test_concurrent_renames_leave_one_id_per_location.py` (with a drop, a recoverable drop and a
  deregister racing a rename, the rival landed after the door read its claim and as it acts) and
  `packages/service-kit/tests/test_a_release_frees_only_the_claim_it_read.py`. The spec's *minimum* is 8 metadata ops; we carry the whole list
  including versioning, tags, branches, indices and transactions.
- **Every table and namespace has a uuid the catalog mints** ([[LH-519]], ADR 0173). A table's lives in its location
  claim: create, declare and register mint it (`table_claims.claim_new_table`; a declaration takes its claim before any
  backend call, and a failed one removes its marker and releases the claim, [[LH-520]]), a create into a table an
  earlier request declared keeps that declaration's, a rename hands it over, a recoverable drop and both undrops keep it,
  and a destroy or deregister releases it, so a re-created name or a re-registered location gets a new one. A new
  table's take replaces a claim its own name still holds: its door's backend call found the name free, so that claim is
  an ended table's whose release was lost. A namespace's is its `rask.uuid` property in its `__manifest` row: both create doors and the provenance boot
  mint it (`identifiers.with_minted_uuid`), an undrop restores it from the trash record, and an Overwrite mints a new
  one. DescribeTable and DescribeNamespace answer each as `rask.uuid`, and every create, declare, register and namespace
  door refuses a caller's `rask.*` property (only an admin's `rask.tier` on a namespace). Pinned by
  `tests/integration/test_the_door_that_makes_a_table_or_namespace_mints_its_uuid.py`.
- **Every grant is held by the object's uuid** ([[LH-524]], ADR 0173 §8): OpenFGA objects are `table:<uuid>` and
  `namespace:<uuid>` (`fga.table_object`, `fga.namespace_object`), the parent and child edges join uuids, and a
  top-level namespace's parent is its warehouse. Every door stays addressed by name and resolves it ONCE per request
  (`catalog.api.dependencies.Resolver`, `ResolverDep`, which `authorize`, the tier judge and `ResolvedTableDep` share),
  so a door acts on the table it authorized. An absent name is refused 403, audited and refused by the name, and only a
  READ door converts it to 404 for a caller holding the parent's `can_get_metadata`. A rename writes no tuple, except
  that a rename into another namespace moves the parent edge (`fga_deps.move_parent_edge`); a table created again
  under a dropped name is another uuid and inherits nothing; a name holding a space never reaches OpenFGA. Every
  listing batch-checks its own candidates on their uuids and fills its page (`fga_deps.authorized_page`, [[LH-223]]);
  only `/management/v1/me` calls `list_objects`, and the estate admin's raw ListObjects explorer. Grant, review and
  access answers carry `object` (the uuid object) and `name`; a control event's `object_id` is the uuid object with
  `object_name` beside it. A registry model (`table:models$<name>`) is no lance-namespace table and keeps its name.
  Pinned by `services/catalog/tests/test_a_grant_is_held_by_the_objects_uuid.py` and
  `services/catalog/tests/test_a_listing_judges_its_own_candidates.py`.
- **A table the catalog makes lives at `<namespace root>/<table uuid>`** ([[LH-520]], ADR 0173 §5). Create and declare
  run the `dir` catalog's own DeclareTable steps with the catalog choosing the location (`dataplane.declare_table`): mint
  the uuid and take the location's claim before any backend call, write `.lance-reserved` there, then record the row
  through RegisterTable's relative location, which answers `is_only_declared` until data lands (measured on pylance
  13.0.0). The backend's own DeclareTable refuses every location but its `<hash>_<object_id>`, so the catalog never calls
  it; a declare naming a location is refused 400, a root-level table id 400 (`dir` resolves one by `<name>.lance`, never
  by its row), and create and declare `properties` are kept nowhere (RegisterTable keeps none, `spec.yaml:2719-2724`;
  create still stamps them on the schema metadata, [[LH-503]]). So no name reaches a vended prefix. A REGISTERED table
  keeps its registrar's location, which `describe_table` answers percent-encoded; every vend
  (`vending.split_s3_location`) and every catalog read and write of a table object (`dataplane.dataset_fs`,
  `object_store_path`, so `version/create` too) uses the key it decodes to once. The register door refuses a location in
  a uuid-spelled directory directly under the namespace root (`identifiers.is_spelled_as_a_uuid`): that is the catalog's
  layout. Pinned by `tests/integration/test_warehouse_routing.py`,
  `services/catalog/tests/test_a_stock_client_opens_on_the_vended_credential.py` and
  `tests/integration/test_a_planted_base_is_refused_and_freezes_nothing.py`.
- **Route grammar:** `POST /v1/<object>/{id}/<action>` — everything a reverse proxy needs (authN/Z,
  routing) is in the PATH, never only in the body. Path/body id conflict → 400. List ops are GET with
  query-param pagination; data ops (create/insert/query) are **Arrow IPC**, not JSON; count/explain
  return plain text.
- **Identifier:** segments joined by the configured delimiter (`$` default) — `["a","b","t"]` ↔
  `a$b$t`. The root namespace is the delimiter itself.
- **Identity headers:** `api_key` → `x-api-key`, `auth_token` → `Authorization: Bearer`; arbitrary
  context maps BOTH WAYS through a `header.<name>` context KEY — `{"header.x-trace-id": "abc"}` is
  sent as `x-trace-id: abc` (prefix stripped) and every response header returns as `header.<name>`
  (`spec.yaml:2471`). The old `x-lance-ctx-<key>` form is superseded and survives only in the
  generated per-model docs. Headers beat body fields.

## The error contract — NEVER invent a status

The spec defines **24 numeric error codes (0–23)** — 22/23 are `TableBranchNotFound` /
`TableBranchAlreadyExists`, added with the branch ops — identical across Python/Java/Rust/REST;
clients dispatch on the **code**, not the HTTP status. On the wire they are **RFC 9457 problem
bodies** (`application/problem+json`).

**The one rule:** an endpoint raises `lance_namespace` typed errors
(`InvalidInputError`, `NamespaceNotFoundError`, …) and lets
`service_kit/lakehouse/ns_errors.py::install_problem_handlers` translate — it maps all 24 codes
(not-founds → 404, already-exists/not-empty/concurrent → 409, `InvalidInput` → 400,
`PermissionDenied` → 403, `Unauthenticated` → 401, `Unsupported` → **406** (the spec's own status; Q3, 2026-09-02), `Throttling` → 429).
The branch codes were MISSING until 2026-08-04 — a missing branch answered 500 on endpoints rask
ships. `tests/unit/test_ns_errors_contract.py` now pins the map against the ENUM, so the next
spec-added code fails a test instead of a client.
**Never `HTTPException` with a hand-picked status** for domain errors — a 422 was shipped once and
no generated client understood it; fixed to `InvalidInputError` (95ae4cb). `NamespaceNotEmpty → 409`
is the spec's own error for "container refuses while full" — use it, don't mint one.

## rask's hierarchy layer

    project (tenant)  >  warehouse (ONE bucket; a project holds MANY)  >  namespace (self-nesting)  >  table

Three layers, each owned in ONE place:

| Layer | Owner |
| --- | --- |
| shape (what can exist) | the guards in `catalog/api/fga_deps.py` — `require_parent` (a table must have a namespace; rename destinations too), `require_warehouse_scoped` (a top-level namespace only via `POST /management/v1/warehouses/{id}/namespaces`; **no-op when `warehouses_enabled` is off** — single-bucket deployments have no warehouse to demand), `require_project_exists` (a warehouse's project must have a registry record — 404 naming `POST /management/v1/projects`), `require_not_protected` (deletion protection — the `force` rule is under Lifecycle) |
| who | the FGA model's `can_*` relations (`service_kit/governed/auth/model.fga`) — the app never invents policy |
| what is possible NOW | the registries, checked BEFORE the native write: **project records** (`catalog/services/projects.py`, `_projects/<id>.json`), warehouse records + `top_ns → warehouse_id → root_uri` bindings (`catalog/services/warehouses.py`) |

**A tenant EXISTS when its registry record does** — not when a warehouse implies it and not when FGA
holds tuples for it. `POST /management/v1/projects` writes the record AND the `project#admin` tuple (the creator's; a
machine identity names the person in `admin` and is never admin itself, LH-433, `fga_deps.project_admin_for`; a
service account's subject is never a person, `is_person_subject`) in
one operation (estate-admin gated on `can_observe_events` at the root), which is what stops existence
and permission drifting apart. There is **no bootstrap exception** in warehouse-create any more: one
door, and a warehouse-owner cannot ride a create into project admin.

Check order at every create door: **identity (401) → shape (`InvalidInput` 400) → parent exists
(404) → authz (403) → conflict (409) → native write → tuples (`seed_ownership`) → events.** Guards
run BEFORE `native.call` — rejecting after leaves a real Lance object with no authz parent.

`project` and `warehouse` are **rask's objects, not spec objects** — their endpoints
(`/management/v1/projects`, `/management/v1/warehouses`, `/management/v1/project/{id}/gate/*`, …) are ours, and the catalog's registry and
FGA are the only project store (there is no controlplane service, ADR 0157). Keep their errors in the
same problem-body format so one client error path serves the whole API. **`/v1` holds only spec
operations** (ADR 0156, LH-476): every rask-only route is served under `/management/v1`, projects on
`/management/v1/projects`, and a rask route on `/v1` answers 404. The two GET twins of `count_rows` and
`tags/list` stay on `/v1` because they are spec operations the stock pylance REST client sends as GET.
`tests/integration/test_spec_conformance.py` walks the served route table for both directions, and the
read-only maintenance gate (`catalog/api/maintenance_mode.py`) refuses writes on both mounts. The same classification
refuses every write the catalog would announce while it holds no signing key (`catalog/api/signing_gate.py`, [[XC-095]]):
503 code 17 before the commit, since an announcement signed after the commit could not be made; only a data write under
the provenance namespace, which the catalog never announces, is served then.

**A table's tier is the `rask.tier` property of the nearest enclosing namespace that declares one** (ADR 0156 C8,
ADR 0160, LH-470). `catalog/core/tier_policy.py` holds it:
- **Declared once.** Only the namespace's project admin (`can_administer` on `project:<p>`, resolved through the
  warehouse binding) declares it, or the estate admin (`can_observe_events` on the root) where no project owns the
  namespace. It is declared at create, on `POST /v1/namespace/{id}/create` or the warehouse door's `properties`.
  The catalog's own Overwrite is the only way to change or drop it, and it asks the same admin; no Lance operation
  alters a namespace's properties.
- **What a declaration may say.** A value outside bronze/silver/gold, a deeper namespace declaring a different tier
  from an ancestor's, and any other `rask.*` / `lineage.*` namespace key are all refused 400.
- **Every write is judged.** `fga_deps.authorize` hands each non-read `table` / `materialized_view` door and each
  create of one to `tier_policy.judge`, with FGA on or off. The judge asks the route's declared rung first, then
  reads the tier, and audits `tier_judge` with it.
- **Pinned by tests.** `services/catalog/tests/test_an_undeclared_door_is_refused.py` walks every mounted route and
  fails on a write that skips the judge; `tests/integration/test_a_tier_is_declared_by_its_admin_and_judged_on_every_write.py`
  covers the admin gate and the judged tier over HTTP.
- **Silver and gold name their run** ([[LH-225]], ADR 0156 C2). A write adding data to a silver or gold table names
  its run and inputs, and every named input, on any tier, needs `can_read_data` (`tier_policy.judge_run`). The
  run is `catalog.core.run_context`: on `create`, `insert` and `merge_insert` it comes from the spec Context headers
  `x-rask-run-id`, `x-rask-job`, `x-rask-inputs` (a JSON array of `{table: [segments], version, branch}`) and the
  door stamps it as the transaction properties `rask.run_id` / `rask.job` / `rask.inputs`, with `rask.author` the
  verified caller; at `version/create` the catalog reads the same properties from the staged manifest, which may carry
  no other `rask.*` key, and refuses a `rask.author` other than the caller or a run without one (a property any
  committer can write proves authorship only because of that refusal). A compaction and a restore add no data, so
  they name no run. A rename commits nothing a run can reach, so moving a table into silver or gold from another
  tier is refused, and so is `register` into one, which attaches bytes no run produced through the catalog ([[LH-463]]). A keyed insert
  and a merge carrying a run commit through `dataplane.commit_stamped`: detached, then finalized as exactly the
  next version by the backend's put-if-not-exists, because `LanceDataset.commit` of an `execute_uncommitted` merge
  rebases past a racing writer without its key check (measured on pylance 13.0.0: a duplicate key landed). A run on
  a branch write is refused 406. `update` and `add_columns` cannot carry transaction properties on pylance 13, so
  silver and gold refuse them 406. The judge answers allow or refuse; a version breaching the project's quality gate
  is held when it is published, at the publish door (ADR 0161, below). Nothing anywhere reads a tier from
  a name.

## Storage — what state lives where (and why there is NO app database)

| State | Store |
| --- | --- |
| table data + versions | Lance datasets on object storage (rustfs) |
| project registry (`_projects/<id>.json`), warehouse registry + namespace bindings | JSON records on the control root. **Id-MINTING creates are conditional** (`If-None-Match: *` via `service_kit.lakehouse.records.create_json`): project mint, warehouse mint and the write-once bindings are store-arbitrated, a lost race surfaces as 409, never last-writer-wins. **Mutable-field read-modify-writes are CONDITIONAL too** as of 2026-08-15 — `warehouses.upsert_warehouse` / `set_warehouse_status` / `projects.upsert_project` go through `records.mutate_json` (read + ETag-guarded replace, bounded retry), so an idempotent re-POST can no longer silently revert a concurrent quarantine or protection change. `put_warehouse` / `put_project` remain as SEEDING primitives with no production caller, pinned by `tests/unit/test_registry_writes_are_conditional.py`. Protection and trash records are still plain overwrites and correctly so: they carry no read-modify-write, so there is no carried-forward field to lose. |
| authz | OpenFGA on its chart-managed Postgres |
| lineage | Lance tables in the catalog's provenance namespace (`services.lineage.provenanceNamespace`, default `provenance`): the commit feed `events`, the table-level edge list `edges`, outbox `refusals` and forwarder `cursors`. The catalog creates the namespace at boot (`catalog/services/provenance_namespace.py`), protects it, grants each `LANCE_PROVENANCE_WRITERS` subject `writer`, and never announces a write under it. Lineage writes through `merge_insert` and reads through `query` as its own principal; the graph is bring-your-own, fed by lineage's forwarder (ADR 0158) |

A relational app-DB was removed at P7a and must not creep back for the catalog: registry writes are
admin-frequency, the conditional creates arbitrate the id-mint races, and deletes are bottom-up
single-object operations by design — there is no multi-object transaction to need one. The moment
that changes (atomic cross-object invariants, high-frequency filtered listings), it is a design
decision, not a default. The conditional-put primitive is proven live by TWO `cas`-marker e2e
suites: `test_object_store_cas_e2e.py` (Lance's own manifest commits) and
`test_registry_cas_e2e.py` (the registry seam, contended 8-way).

## Lifecycle

Reclamation, scheduling and the GC design live in `services/maintenance` itself — read
`services/maintenance/services/{sweep,expiry,arrival,due,plan,purge,reconcile,index_health}.py` — plus
`packages/service-kit/src/service_kit/lakehouse/base_refs.py`, the shallow-clone-source pre-pass, which
lives in service-kit rather than the service because the catalog (`maintenance/run` and the `version/create`
judge, `catalog/services/maintenance.py`) must apply the same refusal and cannot import the sweep. Read
them before changing anything the sweep, the reconciler or the orphan scan touches. The
per-table/namespace/project POLICY that governs a sweep is
`catalog.schemas.PolicyRequest` (retention, retain_versions, compact_enabled, interval, target rows),
resolved **winner-takes-all** — the table's own record shadows the nearest namespace's which shadows the
project record, and the winner supplies EVERY field. Maintenance matches a table and its ancestor namespaces by the
uuids `__manifest` and the location claims record (`maintenance.services.naming.policy_owners`); a table whose uuid, or
an ancestor's, cannot be read is refused (gate `unidentified`), never maintained under the defaults, since the record it
cannot find may be a legal hold. Any surface showing an effective policy must say
which record won; an inherited value rendered identically to a set one is how nobody can tell what is
governing their data. The project-scoped surface is home's `/projects/<p>` § Maintenance.

- Creates are top-down: parent must EXIST (registry), gated on the parent's `can_*`.
- Deletes are bottom-up: a container refuses **409, naming its contents**; `cascade` is explicit;
  warehouse delete gates on the PROJECT's `can_administer`; bucket purge is a **separate opt-in**;
  project delete has **no cascade at all**.
- **A cascade revokes a grant only once its object is gone** ([[LH-495]]). The warehouse door's `?cascade=true` first
  enumerates every bound namespace's subtree and holds it to its protection (`force` turns those locks as it turns the
  warehouse's), and selects its own trash (`warehouses.trash_owned_by`: records under a namespace bound to it or a
  trashed namespace whose binding names it, and tables in its bucket whose namespace is no other warehouse's, never a
  sibling's on a shared bucket), so a refusal changes nothing. It ends that trash FIRST, while the bindings that select
  it stand, so a re-issue selects the same records: each trashed object's grants, policy, claim and base record, its
  trash record last. Then, one namespace at a time, it detaches every table with `deregister_table` (the bytes stay
  unless `purge_bucket`, and only a purge forgets the base record), drops the namespaces deepest first (the `dir` drop
  is Restrict whatever `behavior` says), and only then ends what each object held, children first, through
  `fga_deps.end_removed_subtree`: its protection and policy records, then its grants. The grants go last because the
  `child` edge each object left on its parent is what finds it again: a re-issue lists only what still stands, so the
  walk follows the edges as well as the catalog's listing, and ends what an earlier failed attempt left. It skips an
  object with a live trash record, whose records and grants stay for its undrop. The namespace door's destroyed cascade
  (`_destroy_subtree`, a generator) announces each table to lineage as it goes and ends the subtree through the same
  walk, as do its Restrict, Skip and Overwrite paths. A part-way failure answers 503 `PartiallyApplied` naming
  `namespaces_dropped` (nested ones included), `tables_removed`, `trash_ended` and `tuples_revoked`, which counts each
  revoke as it lands and a partly revoked object's removed tuples through `fga.RevokeInterrupted`; re-issuing the call
  finishes it. A destroyed table's base record and location claim are keyed by its location, which nothing names once
  the table is gone, so a failure between its drop and their release leaves them (the claim lapses after its lease).
  Pinned by `tests/integration/test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py`,
  `tests/integration/test_a_namespace_cascade_revokes_a_grant_only_once_its_object_is_gone.py` and
  `services/catalog/tests/test_a_warehouse_delete_ends_only_its_own_trash.py`.
- **`force=true` overrides the `protected` flag and NOTHING else** — the FGA gate runs first and
  identically with or without it. Test both delete doors for force-without-authz.
- **Recoverable drops are OPT-IN (#75).** With `LANCE_TRASH_GRACE_DAYS` > 0 (default 0/OFF, because a
  grace period changes what `drop_table` means for every caller) a drop DEREGISTERS and files a
  `_trash/` record; `POST /management/v1/table/{id}/undrop` re-registers from it;
  `GET /management/v1/table/{id}/tasks` shows the pending deadline (§2.4 per-object task visibility). `purge=true` is the explicit
  opt-out — a caller who means "destroy the bytes now" says so. A recoverable drop **does not revoke
  the table's FGA tuples**: the owner is the one person who needs to undrop it, and revoking made
  undrop unreachable for exactly that caller (found by driving the deployed catalog — the unit tests
  run FGA off); the grants die with the bytes, at purge or expiry. `undrop` re-registers with a
  **RELATIVE** location: `register_table` refuses the absolute URI `describe_table` had just reported and keeps a
  relative one as the literal key it is given, so the undrop passes the key the recorded URL decodes to under the
  connection's root (`catalog.services.trash_doors.relative_location`, [[LH-517]]), where a registered `räksmörgås`'s
  bytes are. **A trash record is keyed by its object's uuid** ([[LH-525]], ADR 0173 §9): the name is free the moment
  its object is dropped, so a table or namespace created there is another uuid and nothing blocks the create. The
  undrop and tasks doors stay addressed by name: `trash.named` finds every record dropped under it, `?uuid=` names
  one, and the router asks the door's rung on each candidate (`fga_deps._require_trashed`), so a door acts only on,
  and lists only, what its caller holds a grant on. Several candidates answer the undrop 409 (code 19) listing each
  uuid and deadline; a live object of another uuid holding the name answers 409 too. A record keeps `uuid`,
  `namespace_uuid` (its namespace's at the drop) and, when a cascade filed it, `cascade` (the dropped namespace's
  uuid). A new table's claim never takes a location the trash keeps for its name's dropped table
  (`table_claims.kept_in_trash`). The purge judges a table record live by its location and a namespace record by its
  uuid, never by its name, so it reclaims a trashed table while a table created since under the name stands. Pinned
  by `services/catalog/tests/test_an_undrop_restores_the_trashed_table_its_caller_names.py` and
  `tests/integration/test_maintenance_finds_a_table_under_the_spelling_the_catalog_records.py`. The sweep REPORTS
  expired trash and deletes nothing. COVERAGE.md's old "soft-delete is N/A, time-travel replaces it"
  entry was WRONG and is corrected: time-travel does not survive `drop_table`.
  **The CASCADE is recoverable too (#96)**: with a grace period, `drop_namespace(cascade)` never
  issues the destructive native call — a trash record pointing at bytes that call deleted would be
  a lie — and instead DETACHES the subtree (every table deregistered, namespaces emptied
  deepest-first then dropped, one `kind`-tagged record each, shared drop-time `expires_at`; tuples
  KEPT, the same #75 rule). `POST /management/v1/namespace/{id}/undrop` is the PLURAL undrop: it restores the
  records one cascade filed (`cascade` equal to the chosen namespace's uuid), rebuilding every namespace
  shallowest-first and re-registering every table (relative-location form), resumable (`exist_ok` creates;
  already-registered = recovered);
  `GET /management/v1/namespace/{id}/tasks` shows the subtree's deadline; `purge=true` is the same explicit
  opt-out. A RESTRICT drop stays unrecorded on purpose — it only ever removes an empty manifest
  row. Declared-only tables (no recorded location) are skipped by undrop with a warning: no bytes
  were lost.
- **Protection covers EVERY rung since #73** (2026-08-04): warehouses/projects carry `protected` on
  their registry records; tables/namespaces carry it as a **control-root `_protection/` record**
  (`service_kit.lakehouse.protection`) gating drop/deregister/rename and both Overwrite modes,
  `create?mode=Overwrite` over an existing table and `insert?mode=overwrite` on any branch ([[LH-242]]),
  (table) and drop (namespace) —
  deliberately NOT schema metadata, so unprotect is never reachable through the properties door,
  toggling never creates a table version, and the guard answers even for a corrupted dataset. Set
  via `POST /management/v1/table/{id}/protection` / `/management/v1/namespace/{id}/protection`,
  owner-gated (`protection` maps to `can_set_protection`, the owner rung under its own audit name, in
  `_OWNER_SUFFIX_RELATION`). A route suffix no rung map declares is REFUSED for every caller rather
  than defaulted to a rung — `InternalError` (code 18, 500), audited and logged at ERROR with the path,
  because only a route shipped without its rung reaches it — so a new door ships with its entry in
  `_OWNER_SUFFIX_RELATION` or `_WRITER_SUFFIX_RELATION` (or a trailing word in
  `_META_READ_ACTIONS`/`_DATA_READ_ACTIONS`/`_MAINTENANCE_ACTIONS`) —
  `services/catalog/tests/test_an_undeclared_door_is_refused.py` walks every mounted route and fails
  on one that has none. **Every control-root record kept for a table or namespace is keyed by its uuid** ([[LH-523]],
  ADR 0173 §8): protection and maintenance policy (`service_kit.lakehouse.object_records.OBJECT_RECORD_PREFIXES`), a
  promotion hold, maintenance's due record, an erasure obligation and its release (each naming `uuid`/`table_uuid`),
  and the cadence and debounce stamps. A door resolves the uuid from the name it is addressed by
  (`ResolvedTable.require_uuid`, `fga_deps.identify`) and refuses `InternalError` for an object that records none,
  never reading it as unprotected. A rename moves no record and no grant, only its data directories' claims (and, into
  another namespace, its parent edge); a table created later under a dropped name holds a new uuid and inherits nothing,
  even where an ending was lost. Every door that ends an object (drop, deregister, a cascade, the trash purge) ends that
  list through `object_records.end_object_records`, under the uuids it read before the object went. The hold, due,
  erasure and project-policy listings name each table as it is named now (`table_claims.names_now`: its location's
  claim's holder while the claim carries the record's uuid) and filter it under that name; a record whose table is gone
  names no table and is listed to no one, since the name it kept may belong to a new table now. A door resolves its
  table once and opens the location it resolved (`namespace.open_resolved_dataset`), so a uuid and a location never come
  from two resolutions. Pinned by
  `tests/integration/test_a_rename_carries_what_the_table_holds.py`.
  A destructive CASCADE destroys its children INSIDE one native call, so they never re-enter this
  door — `drop_namespace` therefore enumerates `_collect_descendants` whenever `behavior=cascade`
  (no longer gated on `fga_enabled`) and protection-checks EVERY enumerated id before anything
  drops, refusing 409 and NAMING the protected descendant; `force` turns the subtree lock exactly
  as at the named rung, and on the force path the descendants' protection records are cleared after
  the drop (the same reuse rule as the named rung). Until that landed the docstring promised
  cascade coverage the code did not have. Trash records for the children landed with #96 — see the
  recoverable-drops bullet.
- **NO EXISTENCE ORACLE on destructive doors (audit #4).** `delete_warehouse`, `delete_project` and
  `_set_warehouse_status` all collapse `PermissionDenied → TableNotFound`, so "not yours" and "does
  not exist" are byte-identical and the door cannot enumerate ids. CREATE doors deliberately do the
  opposite (`require_project_exists` 404s naming `POST /management/v1/projects`) because that 404 is a fix the
  caller needs. Class rule, not a per-endpoint judgement call. **A location claim's refusal names no holder**
  ([[LH-436]]): the holder is a table the caller may hold no grant on, so a register at a held location answers
  the overlap judge's own sentence (`table_bases.occupied_location_refusal`), and the undrop, rename, destructive
  drop and data-directory refusals say "another table". Each logs the holder at WARNING for the operator.
- **A purge must prove sole ownership first.** One project may back two warehouses with one bucket
  (`projects_claiming_bucket` subtracts the caller's own project on purpose — the work+gold pair),
  so `?purge_bucket=true` checks same-project siblings AND the reserved platform buckets before the
  cascade. Without it, deleting the work warehouse wiped gold's data.
- Anything making a DESTRUCTIVE decision reads `read_bindings` (which returns unparseable paths) and
  refuses on a non-empty skip list; `list_bindings` is the tolerant half, for enumeration only. A
  binding you cannot read is a namespace you cannot see.
- `deactivate` = offboarding step one (quarantine; resolver 403s bound namespaces).
- Maintenance lives in **`services/maintenance`** and DECIDES; it rewrites no data (ADR 0162, [[LH-467]]). It runs
  version expiry (`cleanup_old_versions`, tags EXEMPT), trash purge, the orphan scan and cross-store reconcile, and
  writes the DUE LIST: one record per table that needs compacting or an index optimize, under `_maintenance_due/` on
  the control root, with the estate's clone list (`_maintenance_due/state/clones.json`) beside it
  (`service_kit.lakehouse.maintenance_due`). The catalog lists the records on `GET /management/v1/maintenance/due` to
  a caller holding `can_maintain`; the list is advisory. **Expiry is commit-driven**: the write-event lane
  (`/maintenance-arrival`) expires the table just written once it has `minVersionsToExpire` (20) reclaimable
  versions, and the sweep, every 30 minutes, is the backstop that also writes the clone list and the due records.
  Both ask one eligibility rule, `service_kit.lakehouse.version_retention`, which the catalog's GC preview shares.
  Compaction, index builds and index optimize are a bring-your-own job's, committed through `version/create` (below);
  `CreateTableIndex` answers 406 naming that path. An erasure's rewrite is a bring-your-own job's too (ADR 0163,
  [[LH-493]], below). The maintain vend tier goes to the maintenance identity only (LH-473, #599).
  `catalog/api/maintenance_mode.py` is read-only maintenance MODE (503 + Retry-After), not this. Version reclamation
  has ONE owner, maintenance's expiry: Lance's commit-path auto-cleanup (`lance.auto_cleanup.*`) deletes inside
  whichever writer commits, past a hold and a protected base. No door arms it (the property guard refuses the keys),
  `version/create` refuses a commit that does, every committing catalog door disarms the ref first
  (`core/namespace.disarm_commit_path_cleanup`; the erasure door every ref, so the job's commits are admitted), the
  restore door refuses an armed version, and the
  expiry pass deletes every such key before its refusal gates, as the backstop for a table nobody commits on (LH-245).
- **Maintenance does not read a tier.** The due list plans a compaction at the #50 policy's
  `target_rows_per_fragment`, else Lance's own sizing. There are no per-tier defaults: a tier is the
  namespace's `rask.tier` property, which a dataset path does not carry, and a size guessed from a path is
  applied silently forever (ADR 0160).
- The reconciler reports cross-store drift and deletes nothing until its report runs clean. It runs on
  its OWN Dapr cron binding (`maintenance-reconcile-cron`), separate from the sweep's — a read-only
  drift report answers a different question than the expiry backstop, on its own cadence.
- **OpenFGA rejects a bare-type Read.** `read_tuples(obj="project:")` with no user is HTTP 400
  (*"the object id and user cannot be empty"*), and the wrapper reports it as
  `ServiceUnavailableError` — so it presents as a permanent outage on a healthy server. To enumerate
  by type, read the WHOLE store unfiltered and bucket client-side; governance tuples are
  admin-frequency, and a real estate fits in one page. Measured live 2026-08-04, after four detectors
  shipped green against a double that accepted the filter.

## Gotchas

- **`update_table_schema_metadata` MERGES — the spec text ("Replace schema metadata") is wrong about
  every backend.** Probed against a real `dir` backend: posting `{owner}` over `{owner, tier}` leaves
  `tier` standing. So omitting a key cannot remove it, and the spec's request model types `metadata` as
  a strict `{str: str}` that cannot carry a null — which left table properties with no delete at all,
  and `str(None)` writing the literal string `"None"` onto the table. Since #78 a `null` value DELETES
  the key: no-null bodies stay on the native spec op, a body with any null routes to
  `dataplane.update_schema_metadata` (pylance's `update_schema_metadata`, the same dialect
  `update_field_metadata` already speaks). **Never `replace=True`** — the map a caller holds came from
  `read_schema_metadata`, which excludes `lineage.*` and `rask.*`, so a replace silently destroys the #21
  self-describing coordinates. **Those two namespaces are the platform's** ([[LH-208]],
  `catalog/core/provenance_guard.py`): a set or a null of any `lineage.*` / `rask.*` key is refused 400 at
  this door before the route split, in a create payload's schema metadata, in create `properties` and in an
  `insert?mode=overwrite` payload (whose body then carries the table's own schema and field metadata) and a
  `create?mode=Overwrite` over an existing table, which must keep each provenance column and key field at
  its type and is written with the table's own fields and reserved metadata
  (`provenance_guard.keep_provenance`, [[LH-242]]: the Overwrite is a new version of the same table, its
  grants and history intact, recorded as `overwrite_table` with lifecycle state `OVERWRITE`); the
  platform writes them with pylance directly, and a client creating from an upstream's schema strips its
  schema metadata first. `description` is the one user-facing reserved key (the
  lakehouse renders it under the table name); everything else in that map is opaque user data.
- **`drop_columns` / `alter_columns` refuse to drop, rename or re-type provenance** ([[LH-208]]): the
  column `source_rowid` by name (`stage` and `lineage` are the writer's, ADR 0156 C9), and every field carrying
  `lance-schema:unenforced-primary-key` plus its ancestors, by metadata, in any table. Measured on pylance
  12.0.0, Lance itself accepts all of those (a re-type strips the key); it refuses only `nullable=True` on
  the key (mapped to 400 in `_column_op`) and changes to the key's field metadata.
- **`register` judges what it attaches: the location's spelling, its overlaps, reader flag 256 and the
  bases the dataset declares** ([[LH-279]], `catalog/services/table_bases.py`). The `dir` backend
  registers a location without opening it (measured on pylance 12.0.0: a mixed table, an absent location
  and a nested path are all accepted) and keeps no location exclusive. BEFORE anything is attached,
  `require_registrable_location` refuses an absolute location; one whose spelling is not the place the
  store opens, by the base judge's own rule (`base_refs.names_a_location`, each segment percent-decoded
  once: one that decodes to empty, `.` or `..`, holds a control character or a `?`, or gains a `/` or `\`
  from the decode — pylance drops the tab from `x/.\t./<victim>` and opens the victim, and the store keeps a
  `?` under a re-encoded name, [[LH-435]]; every judge asks the same rule, [[LH-510]]); a segment holding
  `*` once decoded, an IAM wildcard in every policy a credential for the table is scoped by ([[LH-437]]);
  a segment beginning with `_` once decoded; one equal to or containing the control root; and one
  overlapping a configured base.
  Any other `%` is a name: `describe_table` answers a registered location's non-ASCII characters percent-encoded
  (`räksmörgås` -> `r%C3%A4ksm%C3%B6rg%C3%A5s`), so a rule refusing every `%` would strand those tables. A first segment
  spelled as a uuid is refused: the catalog lays out the tables it makes there ([[LH-520]]).
  AFTER the register and before the seed, `judge_registered_table` opens the dataset at
  the location the backend RESOLVED: another registered table overlapping it, flag 256 (mixed data file
  versions, sticky; the message names the recreate remedy) or a declared base nothing sanctions is a 400
  `InvalidInput`; an open that fails is a 503; both deregister what the call attached. An absent location
  still registers.
  Never gate this on `unsupported_features()`: a table created with one external blob prefix in `initial_bases` sets flag 16 and would be
  refused. The catalog's own re-registers (table and namespace undrop) log `restored_mixed_file_versions`
  instead of refusing, so a governed table is never stranded in the trash. A re-register that lands on
  the 409 converge path never seeds governance onto a flag-256 dataset or one declaring a base nothing
  sanctions, and a refusal whose detach fails is a 503 `PartiallyApplied` naming `attached: true`. Only
  `main` is judged. Pinned by `tests/unit/test_a_register_refuses_a_table_that_mixes_file_versions.py`
  (flag 256) and `tests/integration/test_a_planted_base_is_refused_and_freezes_nothing.py` (spelling,
  overlaps and bases).
- **An external blob base is per table, on request, and never vended** ([[LH-209]]). A create registers
  one only when the request names it in the door's `external_blob_base` query parameter (never a
  schema or property key, which LH-208 refuses), inside a `LANCE_EXTERNAL_BLOB_BASES` entry and outside every governed root and bucket (catalog,
  control, model registry and artifact roots, reserved and warehouse-claimed buckets:
  `table_bases.requested_external_blob_base`); a plain create registers none, so Lance refuses its
  `Blob.from_uri` rows. `version/create` accepts a kind-3 descriptor only through a base the table's RECORD holds
  as `external_blob`, never one merely configured or in the manifest. A vend drops every pointer base
  (judged CONFIGURED, or recorded `external_blob`) from the grant and still vends direct; external bytes
  are served by the blob door. A plain base inside a configured entry is CONFIGURED at the read/vend/blob doors
  and the register door only when it covers no governed storage (`BaseJudge.judge`, `GovernedStorage`), so a
  planted `models/<victim>/` base is sanctioned by the record or by nothing; maintenance and lineage keep the
  unnarrowed standing for protection. A pipeline deriving a tier from one names its upstream's base as
  `external_blob_base` on the create. `vend_sanctioned_bases` is gone: the vendor's allowlist is
  `LANCE_MULTIBASE_DATA_BASES` alone. Pinned by
  `tests/integration/test_an_external_blob_base_is_authorized_per_table.py` and the planted-base case of
  `test_a_forged_fragment_is_refused_and_the_table_is_unchanged.py`.
- **A vend's tier is one FGA rung's allow-list over the Lance layout** ([[LH-202]], [[LH-229]],
  `vending.build_session_policy`). Every tier lists and gets `<prefix>/*`. `write` (`can_write_data`) is a
  managed-versioning writer (ADR 0157): put on what a commit needs under the ref it writes
  (`service_kit.lakehouse.table_layout.WRITER_PUT_GLOBS`: `data/`, `_transactions/`, `_deletions/`, `_indices/` and a
  staged `_versions/<n>.manifest-<uuid>`) and an explicit Deny on every final manifest under it at any depth
  (`*.manifest`); nothing under `_refs/`, no version hint, no delete. The table's own write credential also puts
  under `tree/*` ([[LH-490]], ADR 0164): a stock client opens a branch with the table's `DescribeTable` and stages
  the branch's commits with it, and `can_write_data` writes every branch. Measured on MinIO
  `RELEASE.2025-04-22T22-12-26Z`, that grant leaves a write credential fitting a table prefix of 89 characters at a
  63-character bucket, inside a 2,048-byte session policy; RustFS enforces the same 2,048-byte limit, counted after it
  re-serialises the policy, and its boundary is unmeasured (`docs/audits/2026-10-18/rustfs-zero-trust.md` R5). The
  Deny is load-bearing, measured on MinIO (LH-469, #594) and enforced by RustFS's Deny-first evaluation (R3): the
  staged Allow also matches
  `<n>.manifest-<uuid>.manifest`, which pylance reads as a final manifest, and `tree/*` reaches every branch's
  `_versions/`. The catalog finalizes what the writer staged (`version/create`, below). `maintain` (`can_maintain`) gets put/delete on the whole prefix for compaction and
  cleanup, and is also the shape for the warehouse scope probe. The management vend door checks the one rung its
  tier names, never either. A put over an existing key is the residue no session policy can refuse.
  **Each vend is a session of its own** ([[XC-083]], ADR 0170): the catalog exchanges its OWN projected ServiceAccount
  token through STS `AssumeRoleWithWebIdentity` with that session policy, named for the verified caller
  (`vending.role_session_name`), and a refused or unanswered exchange answers 503 code 17 (`vending.vend_or_refuse`).
  The catalog holds no store key: its own IO signs with an hour's session of the same token, renewed at half life
  (`service_kit.governed.store_session`), so a running catalog never needs a restart for its credential.
  **The spec doors vend the highest tier FGA grants the caller** (ADR 0156 C3, `fga_deps.spec_vend_tier`):
  `DescribeTable` and `DeclareTable` vend unless the request sets `vend_credentials` false (unset, the spec lets the
  server decide), write on `can_write_data` (judged by the tier judge), read on `can_read_data`, otherwise none.
  Every mint, at all three doors, writes the `vend_credentials` audit record with its `tier` and `door`. A stock
  client opening through the namespace therefore signs with the vended credential, not its own
  (`services/catalog/tests/test_a_stock_client_opens_on_the_vended_credential.py`). The endpoint a vend names
  is `LANCE_S3_VENDED_ENDPOINT` (chart `vending.endpoint`), else the catalog's own `LANCE_S3_ENDPOINT` ([[LH-489]]):
  pylance applies vended options over the client's own, so an off-cluster writer reaches only what is vended. The
  dev estate vends the RustFS fixture's node port (`DEV_VENDED_ENDPOINT`); the warehouse scope probe keeps the
  catalog's own address. The maintain tier opens to
  `can_vend_maintain` alone ([[LH-473]], ADR 0156 C4): the model's `maintenance` relation, which the catalog writes
  for `LANCE_FGA_MAINTAINERS` on every warehouse (`cascade_tuples`, backfilled at startup, and the chart's
  `bootstrap-admin` hook) and no grant door hands out. The list holds principals, maintenance's service-account principal
  (`kubernetes~system:serviceaccount:<ns>:<release>-sa-maintenance`, `lance.saSubject`), each written as the user it is
  (`fga.user_object`); a bare name in it is refused when the catalog's settings are read ([[LH-063]]). An owner is a
  maintainer and keeps the maintainer doors, and is
  not vended the tier.
- **Stock clients commit through `version/create`** ([[LH-471]], ADR 0157). `DescribeTable` and `DeclareTable`
  answer `managed_versioning=true`, so pylance stages `_versions/<n>.manifest-<uuid>` (its `manifest_path` is the
  object-store path: the key, no scheme, no bucket) and asks `POST /v1/table/{id}/version/create`.
  `catalog.services.managed_versions` copies the staged bytes to a DETACHED slot `_versions/d<id>.manifest` with the
  manifest's own `version` field set to `id` (Lance refuses a manifest whose version is not the one asked for, and a
  create has no committed version to `checkout_version` from; the copy is a one-field wire edit of `Manifest.version =
  3`), opens it with pylance, judges it against the version it follows, deletes the copy, and finalizes through the
  `dir` backend's put-if-not-exists. A refusal leaves the table at its version; pylance reports it as an unknown
  outcome and keeps its staged manifest. The judge: the operation's rung (`fga_deps.require_staged_operation`:
  restore `can_restore`, rewrite `can_maintain`, an Overwrite after the first version `can_drop` plus protection,
  `Clone` and unknown operations refused); a create at 2.2 with stable row ids and no later change to either;
  reserved schema metadata, the provenance and key columns and field-level `rask.*` metadata unchanged; no
  `lance.auto_cleanup.*` and no change to `rask.*` config; feature flags the commit adds; the base list unchanged and
  sanctioned, and no added file through a base; a new fragment naming a held file, no file, or (on an Append) an
  overlay, a deletion file or row ids below the table's `next_row_id`; each added file well formed, present at its
  declared size and agreeing with its footer, at the table's file version ([[LH-211]], `client_fragments`); blob
  sidecars against the table's recorded external bases; the primary key across new rows and carried rows; a
  rewrite's `require_compactable`. The finalize is announced with `emit_measured_write`. A lost answer is Lance's own
  to recover ([[LH-491]], `verify_commit_outcome` in Lance's `rust/lance/src/io/commit.rs` at v13.0.0): it reads the
  version back and keeps the commit only when the inline transaction there is the one it staged, so the finalize must
  land the writer's transaction byte for byte
  (`test_a_commit_whose_answer_is_lost_lands_once_and_the_client_gets_its_version_back`). **A branch commit is served
  the same way** ([[LH-490]], ADR 0164): pylance stages it under `tree/<branch>/` and names `branch`; the judge opens
  it at the branch's own location (`managed_versions.CommitRef`) against the branch version it follows, judges its
  bases against the table's root, and finalizes with `branch`. A branch is created through `CreateTableBranch`
  (`branches/create`), which writes its ref: Lance's own `create_branch` commits a `Clone` and then writes
  `_refs/branches/<name>.json` itself, which no writer is vended, so `version/create` answers it 404 (code 22) naming
  the door. The compaction gate (`service_kit.lakehouse.compaction_gate`, shared with maintenance's due list) weighs
  only bases outside the table, because a branch reads its parent through a dataset-root base on the table's own
  root. Pinned by
  `services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py` and
  `tests/integration/test_a_forged_fragment_is_refused_and_the_table_is_unchanged.py`. `register` still judges file
  versions on its own door.
  **A compaction or an index commits here too** (ADR 0162). Lance's compaction protocol makes two commits,
  `ReserveFragments` (which may only advance `max_fragment_id`) then `Rewrite` (every row kept, by stable row id or
  by count; `require_compactable`), and both need `can_maintain` beyond the writer rung: the `maintainer` rung, which an
  owner holds and the grant doors hand a job's own identity like `writer` (`manage_grants`, or a maintainer holding
  `pass_grants`); a service account is never an owner (LH-433). A `CreateIndex` (a build or
  an `optimize_indices`) needs the writer rung, changes no data, and each new segment lives under the table's own
  `_indices/<uuid>/` with its declared files present and the index opening. A compaction is refused 503 while the
  sweep's clone list is missing or older than `cloneListMaxAgeSeconds` (an hour), and 406 when the table is a
  clone's source. Measured on pylance 13.0.0, Lance itself commits a rewrite that loses or duplicates rows and an
  index naming a base or an empty directory, so the judge is what refuses them.
  **Provenance travels in the spec Context** ([[LH-225]]): the staged manifest's `rask.*` transaction properties name
  the run, `version/create` refuses a silver or gold commit adding data without a run and inputs, and the finalize's
  lineage event names the inputs at their versions. See the tier bullet under "rask's hierarchy layer".
- **A write's lineage event names the commit that write made, never a reopen** ([[LH-214]]).
  `lineage_deps.emit_measured_write` takes `pin_version` and `branch` with no default: a new write door
  passes its handle's `version` after the commit, its response's `version`, or
  `dataplane.committed_version(transaction_id, branch=...)` (the spec's `DescribeTransaction` on main; a
  bounded walk of the branch's own handle on a branch, because the `dir` `describe_transaction` reads
  main only, measured on pylance 12.0.0). A branch write also carries `lance.branchIdentifier`, the last
  uuid of pylance's `branch_identifier` chain, because a recreated branch restarts its numbering. Pinned
  by `tests/integration/test_a_write_event_names_the_commit_it_made.py`, which drives every branch door.
- **`version/delete` reclaims through `cleanup_old_versions(versions=[...])`, never the `dir` backend's raw
  manifest delete** ([[LH-206]], `maintenance.delete_versions`). Measured on pylance 12.0.0, the raw delete
  removes every manifest on `{0, -1}` (describe 200, the table unopenable), and a range ending at latest
  rolls the table back so the next append mints that number again. The door refuses any range
  that reaches the current version (400; ranges are end-exclusive), refuses a version a tag or a child
  branch pins (409, naming it), judges every range before deleting any, and asks `maintenance/run`'s
  #114 base-reference gate, because the reclaim also removes data files only those versions referenced.
  **The pin refusal covers refs that exist when the reclaim starts, not refs created during it.** Lance
  reads tags and branches once per `cleanup_old_versions`, and no door serializes `tags/create`,
  `tags/update` or `branches/create` with a reclaim: measured with concurrent races, a tag created
  mid-reclaim named a deleted version (31 of 60), and `maintenance/run` loses the same race (59 of 60).
  Parked as LH-308, which covers every reclaim path, the sweep included.
- **An erasure is an obligation a bring-your-own job fulfils and the catalog verifies** ([[LH-493]], ADR 0163).
  `POST /management/v1/table/{id}/erasure` (owner rung) plans the predicate (400 with nothing recorded when Lance
  refuses it), disarms every ref and records the obligation (`catalog.services.erasure_obligations`,
  `_erasure_obligations/<id>.json`: the predicate's SHA-256, never the predicate, `retain_days`, each ref with its head
  version then, the requester and a deadline, chart `catalog.erasure.deadlineDays`), answering 202; it commits no data.
  A job holding `writer` and `maintainer` deletes the subject by row id, compacts with deletions materialised and drops
  every user index (a stock `drop_index` is a `CreateIndex` removing it), each commit judged at `version/create`, and
  rebuilds its indexes after the release. `POST .../erasure/{obligation_id}/verify` (`can_maintain`) refuses a
  predicate whose digest differs (400) and another table's or a released obligation (409), then `erasure.verify`
  judges every head on bytes (`main`/`branch:<b>`, `compact:<ref>`, `index:<ref>` are what the job owes). It reads
  `CreateIndex` provenance only up to each ref's recorded head: a segment committed since is the job's own claim (a
  copied or crafted one could hold the subject's key) and is never proof (owner, 2026-10-15). It proves each clean
  head's segments BEFORE any reclaim (a reclaim deletes the transactions that prove them), drops the tags pinning the
  subject once every head is clean, reclaims each clean ref deepest first and verifies every retained version. Every predicate probe
  reads the data with `use_scalar_index=False`: measured on pylance 13.0.0, a BTREE rebuilt after the delete answers 0
  rows behind the deletion vector where the data answers 1. The obligation is released (digest wiped) only when every
  head is clean and every residual is held by `pinned_by` alone; else it stays open naming each open surface. A release
  an earlier verification scheduled under the obligation's id is reused only while it names every current pin.
  `GET /management/v1/erasures` lists them to `can_maintain` holders with `overdue`; nothing acts on a missed deadline.
  The job rewrites a branch head through branch commits (ADR 0164) exactly as it rewrites main. Pinned by `services/catalog/tests/test_an_erasure_is_an_obligation_the_catalog_verifies.py` and the
  stock job in `test_a_stock_client_commits_every_operation_through_version_create.py`.
- **An erasure's pins are released on a schedule, not on request** ([[LH-178]], ADR 0138). When a verification
  settles an obligation and `ErasureReport.pinned_by` is non-empty, the verification door writes a release record
  under the obligation's id (`catalog.services.erasure_release`, `_erasure_releases/<id>.json` on the control root)
  BEFORE it tells anyone, then emits one targeted `erasure_release_scheduled` per person (the obligation's requester,
  table owners). The `catalog-erasure-release-cron` tick claims a due record by an ETag-guarded lease, RECORDS each
  branch's recreate reference before deleting any, deletes the tags and the branch subtree leaf-first (re-read live,
  so a branch cut during the grace period goes too) and recreates it top-down from main's head. No door cancels or
  extends one, and protection does not block it (ADR 0150). With every head clean, what a settled release names is
  tags: a branch head standing on a residual's files would hold the subject itself. Pinned by
  `services/catalog/tests/test_erasure_releases_a_pinning_branch_after_its_notice.py`.
- `deregister` keeps bytes ON PURPOSE (external data); `drop` removes them. Neither leaves Lance
  orphans — but partially-failed writes and unpurged buckets do, and nothing reclaims those yet.
  `services/maintenance`'s orphan pass REPORTS them (`MAINTENANCE_ORPHAN_SCAN_ENABLED` — it opens
  every dataset, unlike the rest of the drift report which compares three stores). **It is ON in the
  deployed estate**: `maintenance/core/config.py` defaults it `False`, but `chart/values.yaml` ships
  `orphanScan: true`, and the chart is the single deploy artefact. Read the chart for what an estate
  does; read `config.py` only for what an unconfigured process does.
- **Three Lance file classes look like orphans and are not.** A scan that names any of them would
  drive a reclaimer into live data, and all three were found by running against a real estate, not by
  reading the layout doc: `_refs/tags/*.json` are TAGS, which PIN versions (`cleanup_old_versions`
  exempts tagged versions for that reason); `.lance-reserved` is a structural marker; and a large
  binary column's bytes live in `data/<data-file-stem>/*.blob`, a SIDECAR that `data_files()` does not
  name — the first live run called 29 MB of real page images reclaimable. Conversely `_transactions/
  *.txn` and `_deletions/` files ABOVE the listing floor (the earliest retained manifest) accumulate
  forever: `cleanup_old_versions` lists those two directories only up to the floor. `data/` and
  `_indices/` are listed past it (lance #8944 for `data/`, pylance 13.0.0), so an unreferenced data or
  index file is Lance's own to reclaim once it is 7 days old; `orphans._LISTED_PAST_THE_FLOOR_KINDS`
  classifies by that, and the floor raise (`floor.py`, [[LH-094]]) acts only for the floor-bound kinds.
  The cost of #8944: a vended-credential writer that stages fragments and commits more than 7 days later
  loses them to the sweep's cleanup.
- The FGA-only live seed (`fga_seed_demo.py`) writes projects no registry knows — the origin of
  "ghost projects". The replacement SHIPPED: `scripts/seed_estate.py` drives the real doors in
  hierarchy order — `POST /management/v1/projects` → `POST /management/v1/warehouses` → `POST /management/v1/warehouses/{id}/namespaces`
  → `/declare` → `POST /management/v1/access/tuples` LAST — so every guard runs and a state that cannot be
  reached through the UI cannot be seeded either. A grant whose create failed is SKIPPED rather than
  written; writing it is exactly how a ghost is made.
- **The control-event vocabulary is a wire contract in three files.** `ControlAction` /
  `ControlObjectType` (`service_kit/control_events.py`) reach the frontend through
  `docs/catalog-openapi.json` → `frontend/packages/api/src/generated/catalog.ts`. Adding an action
  without `make openapi` + `bun --cwd=frontend run gen:types:catalog` leaves the TS client unable to
  name an event the backend publishes, and `test_openapi_contract` fails. Same for `TupleOrigin`
  (`service_kit/governed/fga.py`) — an origin string not in the Literal is a `ty` error, not a runtime one.
- The sweep covers EVERY warehouse bucket, not a static list (#81): `run_sweep` unions `s3_bucket`
  + `MAINTENANCE_S3_EXTRA_BUCKETS` with `warehouse_records.maintainable_buckets(registry)` and calls
  `discover_datasets` once per bucket — a bucket is created by an API CALL at runtime, so a
  config-time list goes stale by construction. The orphan scan reads the same registry
  (`_scannable_buckets`), reporting an `IncompleteScan` rather than silently narrowing when it is
  unreadable. Residual: no multi-warehouse run against REAL object storage yet (#80).
- **A MULTI-BASE dataset leaks, and nothing in Lance reclaims it — upstream-blocked, not a backlog
  item.** `cleanup_old_versions` is ROOT-SCOPED: it reclaims dead files under the dataset's own root
  and leaves every non-root base alone. MEASURED on pylance 9.0.0 — a dataset with
  `target_bases=['cold']` landed a data file in an external base; after an overwrite orphaned it,
  aggressive cleanup (`older_than=None, delete_unverified=True`) reported `data_files_removed: 2` for
  the root-owned files and `EXTRA_BASE_DELETED = []` for the external one, which survived. There is no
  pylance API that reclaims it, so this cannot be fixed in `services/maintenance`; it needs an upstream
  answer or a bespoke reclaimer that understands `base_paths`, which is exactly the "list the prefix,
  subtract what is referenced" logic the orphan scan REFUSES to run on these datasets for safety.
  The same root-scoping is what makes cleanup SAFE on a shallow clone (see `SUPPORTED_FOR_GC`) — the
  property that protects the base is the property that strands its garbage.
- **A JSON index has NO stats, and Lance says so with a PANIC — upstream, already contained.** On
  pylance 10.0.0 every maintenance sweep prints `thread '<unnamed>' panicked at
  lance-index/src/scalar/json.rs:95:9: not yet implemented`, twice, once per Json index. The
  reproducing call is `ds.stats.index_stats("lineage_run_id_idx")` against a table carrying a JSON
  index built as `IndexConfig(index_type="json", parameters={"target_index_type": "btree", "path": ...})`
  — such as a provenance index on `lineage -> run_id`, which lets a governed row be filtered back to
  the run that wrote it; no lakehouse service builds one, a pipeline may. `not yet implemented` is Lance's own `todo!()` — stats for the JSON scalar
  index simply do not exist yet — so there is nothing to fix on our side and nothing to file beyond
  upstream. It is CONTAINED, and deliberately: `index_health._stats` catches **`BaseException`**, not
  `Exception`, because a Rust panic surfaces as pyo3's `PanicException`, which derives from
  BaseException and would otherwise sail straight through the sweep's error handling and kill the tick.
  It logs `index_stats_unreadable` and the finding reports *"its statistics could not be read, so its
  health is UNKNOWN — not the same as healthy"*, which is the honest answer: those two indices are
  UNMONITORED, not proven fine. Do not "fix" the noise by narrowing that except clause. Note also that a
  JSON index can only be built on a Binary/LargeBinary column holding real **JSONB** — raw JSON text
  bytes fail `InvalidJsonb` at `json.rs:456`, and a string column is refused outright at `json.rs:726`.
- **`lance_ray.compact_files` DOES NOT COMPACT — upstream, and only the distributed path.** Relevant to
  the bring-your-own rewrite job of LH-467: measured on lance-ray 0.5.0 + pylance 10.0.0, a distributed
  compaction of 4 fragments reports `compaction did not reduce fragments: 4->4`, while a distributed
  WRITE, INDEX and EVOLVE on the same cluster pass. MEASURED 2026-08-16 against the identical shape — 64 rows in 4 fragments of
  16, `data_storage_version="2.2"`, `enable_stable_row_ids=True`, evolved with `add_columns`, then
  `target_rows_per_fragment=32` — NATIVE `dataset.optimize.compact_files` reduces **4 → 2**. Same
  dataset shape, same option, opposite result, so Lance is not the defect. The job's call matches
  lance-ray 0.5.0's signature (`compact_files(uri, *, compaction_options, num_workers,
  storage_options, ...)`), so it is not misuse either. The upgrade study of 2026-10-12 reports the same result on lance-ray 0.5.0 + pylance
  13.0.0. Nothing tracks the upstream fix, and the lakehouse runs no Ray job of its own (ADR 0155), so
  no rask suite exercises it.
- **CHECK THE TRASH RECORD BEFORE TOUCHING BYTES — including when you are "only looking".** The sweep
  reports `versions_removed: 0` on datasets holding versions far older than the retention, and the
  reason is almost always the F6(d) exclusion: a recoverably-dropped dataset (`_trash/` record) is
  frozen until undrop or purge, because the sweep may not rewrite bytes someone can still restore.
  Diagnosing that count by hand-running `cleanup_old_versions(older_than=7d)` on one of them (done
  2026-08-16, on `bind86-bronze$converge-proof`) IS the destructive call the exclusion exists to
  prevent: it removed 5 versions / 7,567 bytes and destroyed time-travel to v1–v4 on an object that
  was still restorable. The latest version, row count, `published` tag and undrop all survived, so
  cleanup was working correctly — the sweep was right to decline and the operator was wrong to
  override it. Tags are not the check; the trash record is.
  **Every maintenance writer reads the record itself, and one rule answers it** ([[LH-449]]):
  `sweep.why_in_trash` holds a record to cover its table and every dataset under it, so a branch
  discovery lists at `<table>/tree/<branch>` is frozen with its table. The sweep's exclusion asks it,
  and so does the listing-floor pass, before its per-tick cap and before any open, vend or commit; an
  unreadable trash index stops the floor pass for the tick. The vend door's describe is not this gate:
  a Lance commit is a put-if-not-exists on the store, so a writer holding any credential could commit.
  The arrival lane asks it too. The rule compares every location as the path it decodes to
  (`base_refs.decoded_path`, [[LH-318]]): a record holds the location `describe_table` answered, which
  percent-encodes a registered location (`…/r%C3%A4ksm%C3%B6rg%C3%A5s`), while discovery lists the decoded
  directory the store keeps (`…/räksmörgås`) and an event names the encoded one.
  **Every other maintenance reader of a location the catalog records reads it the same way** ([[LH-319]]). A
  location maintenance holds is a store key, compared as listed: discovery's, the `__manifest`'s, and the write-event
  lane's, which converts its event's URL to the key it decodes to at its boundary
  (`arrival.resolve_written_table`), so its table id, policy, cadence stamp, trash gate and vend check are the
  sweep's own. A location the catalog recorded is a URL, read once as the key it decodes to. That covers:
  - the purge (`purge.store_paths`), which reclaims a trashed table under the key its record decodes to and the
    `.lance-reserved` marker pylance 13 writes under the spelling (measured over moto: `…$r%C3%A4…/.lance-reserved`
    beside data at `…$räksmörgås/`);
  - the reconciler's trash set, a table policy's recorded path (`maintenance_policies.resolve_policy`) and the
    location the vend door answers (`credentials`);
  - the tombstone probe (`purge.objects_held`), and the compaction gate's root probe.

  A table's own branch protects nothing from the purge (`BaseRefs.protects_from_deletion`): the delete takes it along,
  while expiry keeps a branch protecting its parent (`is_protected`). On a local `file://` root pyarrow decodes the
  URL itself, so only an S3 store shows a reader that compares spellings.
- **A governed tier's maintenance run is emitted only when expiry does MATERIAL work.**
  `sweep.py::_did_material_work` gates the `expire_versions` emit on `old_versions_removed`, so a
  correctly-idle estate records nothing — deliberately, since every write and every backstop tick would
  otherwise flood the graph with no-op runs. Consequence for verification: a stamped tier dataset does NOT
  produce a `(:Run)-[:WROTE]->(:Dataset)` node just by being written and swept; a pass whose result is
  `versions_removed: 0` emits nothing, which is the gate behaving correctly, not a defect.

  **MAINTENANCE NAMES A DATASET BY `__manifest` ALONE** ([[LH-520]], `maintenance.services.naming.TableIds.table_of`):
  the row whose location it is, or whose table it is a branch of. It parses no name out of a path and reads no
  `lineage.dataset_id` stamp. A dataset no row names is refused `unnamed` by the sweep (reported by its location, never
  opened, expired or vended; the model registry, opened by explicit URI, is such a dataset), refused by the floor pass,
  and reported by the reconciler as an `UnregisteredDataset` by location (declared platform roots excepted).

  **A DERIVED TIER INHERITS ITS UPSTREAM'S `lineage.dataset_id` UNLESS THE STAMP DROPS IT.** No maintenance reader
  names a dataset by it, and `lineage.dataset_id` is schema METADATA, which survives `set_column` / `append_column` /
  `drop_columns` — so a silver table derived from bronze declares bronze's id unless something
  re-declares it (measured 2026-09-09: silver's compactions and FAIL events were filed against bronze's
  node). `service_kit.lakehouse.stage_stamp.stamp_stage(..., dataset_id=...)` re-declares the destination
  and DROPS an inherited id when none is given, the same rule it applies to the `lineage` document. No
  lakehouse service calls it; it is the helper a pipeline writing a derived tier uses. When
  you check a declared-id chain, ask not "is a name stamped" but "whose name, on the tier that derived it".

  **EVERY TIER IS A CATALOG TABLE, and the writer is the user's pipeline** (ADR 0154, ADR 0155). Bronze,
  silver and gold are written only through the namespace doors, so each tier is a `table:` object with a
  record and ownership tuples from its first row, and the catalog announces every committed write on
  lineage. Moving rows between tiers runs on the user's own compute and orchestrator; the lakehouse runs
  no stage. **The promotion gate is the `published` tag** (ADR 0156 C1, ADR 0161, [[LH-466]]): readers of a
  governed tier read `@published`, and `POST /management/v1/table/{id}/publish` runs the project's quality gate
  (`/management/v1/project/{id}/gate/*`) against the committed version; a pass advances the tag. With
  `review_enabled` on that gate, a version whose non-structural assertions fail, or whose row count moves past
  `review_band` from the published one (or that would be the table's first publication), is HELD
  (`catalog.services.promotions`): `published` stays, one record per table under `_promotions/` on the control
  root lists it on `GET /management/v1/promotions`, and the tag `rask.held` pins it against cleanup. A `can_promote`
  holder answers on `POST /management/v1/table/{id}/promotion/approve` (`published` moves to it) or
  `.../promotion/reject` (main is restored to the published version as a new commit); a hold unanswered after the
  gate's `review_hours` expires on `catalog-promotion-expiry-cron`. Every move of `published` emits a `publish_table`
  lineage run, which the notifications plane delivers to the project's watchers, and the spec's tag doors refuse
  `published`, `publishing` and every `rask.` tag (400), so nothing else moves it. A governed row needs no column (C9, [[LH-477]]): `source_rowid`
  is an opt-in the publish door holds to uint64 stable row ids, and `stage` and `lineage` are the writer's.

  **`register_table` is the door for bytes written outside the catalog's own doors.** It turns written
  bytes into a `table:` object, seeds ownership tuples, and every governed path (protection, trash,
  credential vending, the FGA doors) keys off that object. It needs no warehouse, so it works in the
  RESERVED bucket — while the WAREHOUSE route over that bucket is refused: `POST /management/v1/warehouses
  {bucket: lance-catalog}` answers **400 "bucket 'lance-catalog' is reserved platform storage …"**
  (`catalog/api/v1/endpoints/warehouses.py`, `settings.reserved_bucket_set`). The reason is the disclosure
  the bind would create: `provision_bucket` is idempotent on an existing bucket, so the claim would
  silently succeed, make that project the bucket's owner, and let **a later project-policy set govern
  every tenant's data in the shared catalog bucket**. The reservation is DEFENCE IN DEPTH in three
  places: `reserved_bucket_set` (`catalog/core/config.py` — the catalog root, the control/registry root,
  the model registry, every approved multi-base data bucket, and `LANCE_RESERVED_BUCKETS`), the
  warehouse-create and bucket-claim refusals (`warehouses.py`), and `policies.py`, which subtracts
  reserved buckets from the project-policy MATCH set rather than trusting the registry. A dataset in
  platform storage that needs retention or protection gets a platform-level mechanism or moves into a
  tenant warehouse, never a warehouse over the reserved bucket.
- **Five things live in a Lance dataset that a manifest scan does not reach.** Branches (`tree/`),
  multi-base files (`base_paths`), MemWAL shards (`_mem_wal/` — WAL + SSTable datasets, and the spec
  warns that GC'ing WAL files WEAKENS writer fencing, since fencing detects a stalled writer by a
  put-if-not-exists COLLISION), data overlays (`data/overlay-*.lance`, referenced from
  `DataFragment.overlays` not `data_files()`), and blob sidecars (`data/<stem>/*.blob`). The first
  four are REFUSED by `maintenance/services/orphans.py`; refusing overlays is what feature flag 64
  requires, not a shortcut. Overlays ARE writable on pylance 9.0.0 via `LanceOperation.DataOverlay`
  (the older "experimental and unwritable" note was stale) — pylance just refuses to reopen the
  result, which is why the refusal is driven by the feature flags rather than by that seam.
- **A dataset's files do not necessarily all live under its prefix.** A named BRANCH is a whole
  parallel dataset under `tree/{branch}/` (its own `_versions`/`_transactions`/`_deletions`/
  `_indices`; branch names may contain `/`), and `lance.dataset(uri)` opens only the MAIN branch. A
  SHALLOW CLONE's data resolves through the manifest's `base_paths[]` to another dataset root
  entirely (feature flag 16). Any "list the prefix, subtract what is referenced" logic reports both
  as garbage. Branches are caught by the `tree/` directory probe; base_paths by TWO checks, and both
  are needed — the consequence (a referenced path not present locally) plus the MANIFEST FLAG.
- **A branch's directory holds only that branch's files, and that is rask's rule, not Lance's**
  ([[LH-203]], `service_kit.lakehouse.branch_layout`). The format joins the name verbatim, so `a/b`
  lives inside `tree/a/` and `a/_versions` inside `a`'s own version directory, and Lance reads
  whatever lies under a branch's directory as that branch's (measured on pylance 12.0.0: a reclaim of
  `a` deleted `a/_versions`'s manifests; deleting `a/_versions` deleted `a`'s history). So the create
  door refuses a layout segment (400) and a name that nests with an existing branch (409, code 19); a
  branch-maintain vend grants only `tree/<b>/{_versions,_transactions,_deletions,_indices,data}/*`
  and a branch-write vend only what a commit stages under `tree/<b>/` (`table_layout.WRITER_PUT_GLOBS`);
  delete goes through nested branches deepest first, refused whole (409) when a branch outside them is
  forked from one; every reclaim (the sweep's `nested_branch` gate,
  `maintenance/run`, `version/delete`) refuses a branch with another inside it; and the sweep lists
  branches from `_refs/branches`, never by walking `tree/`.
- **The manifest's feature flags ARE reachable, and they are the refusal gate (#64).**
  `packages/service-kit/src/service_kit/lakehouse/features.py` reads
  `reader_feature_flags`/`writer_feature_flags` as varints at
  protobuf fields 9/10 of `LanceDataset._ds.serialized_manifest()` — pylance exposes neither field
  but its own pickle path uses that blob. The masks live in service-kit because `services/maintenance`
  and `catalog/services/maintenance.py` both consume them, and a mask widened for one must not silently
  widen for the other. Measured on pylance 9.0.0: plain `(0,0)`,
  `delete()` `(1,1)`, `enable_stable_row_ids` `(2,2)`, `add_bases`/`shallow_clone` set 16, a committed
  `LanceOperation.DataOverlay` sets 64.
  **TWO GATES, NOT ONE, each asked per verb.** `describe_gc_unsupported_flags` against
  **`SUPPORTED_FOR_GC` (= `SUPPORTED` | 16)** gates version reclamation: maintenance's expiry pass and the catalog's
  `maintenance/run` (`require_reclaimable`, plus the `base_refs` guard). `describe_compaction_unsupported_flags` gates
  compaction alone: the due list will not list a table it refuses, and `version/create` refuses a client's `Rewrite`
  through `require_compactable` (the evidence gate). So flag 16 is NOT refused outright: such a dataset is still
  expired, because root-scoped operations are safe on a shallow clone while compacting one silently materialises the
  base into it. The ORPHAN scan keeps the narrow flags-only `describe_unsupported_flags`/`SUPPORTED` mask, where the
  refusal is genuinely required. The cost of a blanket flag-16 refusal was measured: 17 of the estate's datasets were
  refused, exactly the ones with fragments and version history, so a sweep did no work on them at all.
  **The COMPACTION gate asks about the BASES, not about the flag — it is the one gate here that is not
  flags-only.** Refusing every flag-16 dataset was still over-broad by exactly the shape of a table with
  one external blob prefix: `initial_bases` registers ONE external
  blob prefix via `initial_bases`, which sets 16 while every data file stays under the dataset's own
  root — measured on pylance 10.0.0, compacting that is a merge and nothing more (4 fragments -> 1,
  9,445 -> 14,366 bytes locally, base byte-identical, 20/20 payloads still resolving). Refused anyway,
  the estate's most-fragmented tiers accumulated fragments forever while the sweep reported clean
  passes (`fragments_removed_total=0` over 785 ticks). `features.gather_compaction_bases` therefore
  takes THREE readings and `describe_compaction_unsupported_flags` weighs them; it permits only when
  all three say no, and **every unknown refuses**:
  1. `BasePath.is_dataset_root` — the manifest's self-report. A true positive and a useless negative:
     measured, `shallow_clone` is the ONLY writer that sets it, so `add_bases` pointed at a live Lance
     root reports False. A gate reading this bit alone (the earlier, never-wired attempt) waves the
     clone shape through.
  2. **The object store's own answer** — `objectfs.is_lance_dataset_root`, i.e. does `<base>/_versions/`
     exist. Ground truth, and the load-bearing signal.
  3. `DataFile.base_id` — is OUR data living over there. The only signal that catches
     `write_dataset(..., target_bases=[...])`, where the base is a dataset root by neither reading yet
     compaction pulls the files home (measured: local root 3,540 -> 5,991 bytes, the base's three
     files orphaned).
  A registered-but-unused bare prefix may be compacted (`add_bases` at an empty prefix); the "the next
  write may land under that base" worry is answered by reading (3) again at the next commit, not by
  refusing today.
  The sweep reports refusals as their own
  `summarize()` line + `compaction.datasets.refused` counter — never inside `errors` or `skipped`.
  **The ORPHAN scan stays flags-only, and that is not an oversight.** Its own consequence check cannot
  stand in for the flag: `add_bases` registers a base no `DataFile` resolves through yet, every
  `base_id` stays `None`, and the scan passed such a dataset as `checked=True` with orphans named. A
  scan that names live data as garbage is a different and worse failure than a rewrite it declined.
  Flag 64 was only ACCIDENTALLY safe: pylance refuses that open itself, and the untyped `open:` error it produced is exactly what
  the sweep's lineage layer drops as noise. Widening `SUPPORTED` is a deliberate edit — it is a
  whitelist, so a pylance upgrade adding a legitimate flag silently stops maintaining every dataset
  that sets it, which is why the refusal counter has to be loud. **Residual: the source-side mirror
  hazard is NOT closed** — compacting a clone's SOURCE deletes files only the clone references
  (reproduced), and the source's own manifest carries no flag, so no per-dataset check can see it.
- **A namespace is a `__manifest` ROW, not a directory** (the `dir` impl the chart runs —
  `LANCE_REST_IMPL=dir`). Only a TABLE materialises a directory. Any scan that enumerates namespaces
  by listing directories silently returns `[]` on every real estate — which reads as "checked and
  clean". The reconciler's `unbound_namespaces` detector shipped with exactly that bug.
- **The `warehouse_binding_cache` is invalidated ACROSS replicas by the control-event broadcast
  (#46, 2026-08-05).** `_resolve_warehouse_root` caches bindings positively and forever on the
  premise that a binding is immutable; the warehouse delete breaks that premise, and the local
  `pop` only fixed the deleting replica. Now `on_control_event` (`catalog/api/dapr.py`) — the same
  broadcast subscription that feeds the ring buffer, no queueGroup so EVERY replica hears every
  event — calls `warehouses.evict_stale_bindings`: `warehouse_deleted` evicts the event's
  `namespaces_dropped` PLUS a warehouse-id scan (a Decision-3 partial delete can unbind more than
  the event recorded); `warehouse_bound` evicts the re-bound namespace; `namespace_dropped` evicts
  the id's top segment. Deactivation is deliberately absent — warehouse STATUS is read live per
  request. The chart couples the two: `services.catalog.replicas > 1` with `catalog.controlEmit`
  off FAILS the render (`chart/templates/services.yaml`), so an overlay cannot scale into
  staleness. (`GET /management/v1/events`' ring buffer stays per-replica by design — the poll endpoint serves
  each replica's own buffer.)
- The `\Z`-anchored `CONTROL_ID_RE` (`catalog/core/identifiers.py`) is the ONE id-shape rule. It was
  three copies that had already drifted: Python's `$` also matches before a trailing newline, so
  `"acme\n"` was refused by one door and accepted by another.
- Credential-level tenant isolation IS attacked since #74: `tests/unit/test_vending.py` evaluates
  the cross-tenant read/write against the session-policy document the vendor really builds (IAM
  semantics, per tier, with two negative twins — B still reaches B's own table, and a read-tier
  credential cannot write its own), and `tests/e2e-py/test_credential_isolation_e2e.py` drives the
  real attack with vended credentials at table scope (LIST and PUT against a sibling table in the same
  bucket, and a read-tier PUT into its own table; env-gated — the CI half is #84). The offline
  half deliberately mocks no store: moto does not enforce inline session policies. Byte-placement
  isolation stays proven by `test_warehouse_routing.py` / `test_warehouses_e2e.py`.
