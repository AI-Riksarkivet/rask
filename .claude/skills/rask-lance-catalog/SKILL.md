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

- **Operations: 54/54 ROUTED, 47 backend-backed.** `tests/integration/test_spec_conformance.py`
  asserts both halves — every spec op has a served route, and the vendored
  `lance_docs/ns_catalog/spec.yaml` still carries 54 ops (a shrunken spec would silently weaken the
  check). The other **7 answer a spec-correct 501** because the native `dir` backend stubs them:
  `rename_table`, `backfill_columns`, `alter_transaction`, `batch_create_table_versions`,
  `batch_commit_tables`, and BOTH materialized-view ops (`docs/COVERAGE.md`). The spec's *minimum*
  is 8 metadata ops; we carry the whole list including versioning, tags, branches, indices and
  transactions.
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
`PermissionDenied` → 403, `Unauthenticated` → 401, `Unsupported` → 501, `Throttling` → 429).
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
| shape (what can exist) | the guards in `catalog/api/fga_deps.py` — `require_parent` (a table must have a namespace; rename destinations too), `require_warehouse_scoped` (a top-level namespace only via `POST /v1/warehouses/{id}/namespaces`; **no-op when `warehouses_enabled` is off** — single-bucket deployments have no warehouse to demand), `require_project_exists` (a warehouse's project must have a registry record — 404 naming `POST /v1/projects`), `require_not_protected` (deletion protection — the `force` rule is under Lifecycle) |
| who | the FGA model's `can_*` relations (`service_kit/governed/auth/model.fga`) — the app never invents policy |
| what is possible NOW | the registries, checked BEFORE the native write: **project records** (`catalog/services/projects.py`, `_projects/<id>.json`), warehouse records + `top_ns → warehouse_id → root_uri` bindings (`catalog/services/warehouses.py`) |

**A tenant EXISTS when its registry record does** — not when a warehouse implies it and not when FGA
holds tuples for it. `POST /v1/projects` writes the record AND the creator's `project#admin` tuple in
one operation (estate-admin gated on `can_observe_events` at the root), which is what stops existence
and permission drifting apart. There is **no bootstrap exception** in warehouse-create any more: one
door, and a warehouse-owner cannot ride a create into project admin.

Check order at every create door: **identity (401) → shape (`InvalidInput` 400) → parent exists
(404) → authz (403) → conflict (409) → native write → tuples (`seed_ownership`) → events.** Guards
run BEFORE `native.call` — rejecting after leaves a real Lance object with no authz parent.

`project` and `warehouse` are **control-plane objects, not spec objects** — their endpoints
(`/v1/projects`, `/v1/warehouses`) are ours. Keep their errors in the same problem-body format so
one client error path serves the whole API.

## Storage — what state lives where (and why there is NO app database)

| State | Store |
| --- | --- |
| table data + versions | Lance datasets on object storage (rustfs) |
| project registry (`_projects/<id>.json`), warehouse registry + namespace bindings | JSON records on the control root. **Id-MINTING creates are conditional** (`If-None-Match: *` via `service_kit.lakehouse.records.create_json`): project mint, warehouse mint and the write-once bindings are store-arbitrated, a lost race surfaces as 409, never last-writer-wins. **Mutable-field read-modify-writes are CONDITIONAL too** as of 2026-08-15 — `warehouses.upsert_warehouse` / `set_warehouse_status` / `projects.upsert_project` go through `records.mutate_json` (read + ETag-guarded replace, bounded retry), so an idempotent re-POST can no longer silently revert a concurrent quarantine or protection change. `put_warehouse` / `put_project` remain as SEEDING primitives with no production caller, pinned by `tests/unit/test_registry_writes_are_conditional.py`. Protection and trash records are still plain overwrites and correctly so: they carry no read-modify-write, so there is no carried-forward field to lose. |
| authz | OpenFGA on its chart-managed Postgres |
| lineage | AGE (Postgres), chart-managed |

A relational app-DB was removed at P7a and must not creep back for the catalog: registry writes are
admin-frequency, the conditional creates arbitrate the id-mint races, and deletes are bottom-up
single-object operations by design — there is no multi-object transaction to need one. The moment
that changes (atomic cross-object invariants, high-frequency filtered listings), it is a design
decision, not a default. The conditional-put primitive is proven live by TWO `cas`-marker e2e
suites: `test_object_store_cas_e2e.py` (Lance's own manifest commits) and
`test_registry_cas_e2e.py` (the registry seam, contended 8-way).

## Lifecycle

Reclamation, scheduling and the GC design live in `services/maintenance` itself — read
`services/maintenance/services/{sweep,optimize,purge,reconcile,base_refs,index_health,tiers}.py`
before changing anything the sweep, the reconciler or the orphan scan touches. This used to point at
a root `open_*.md`, which is ephemeral by design: the plan was deleted when its work landed and the
pointer dangled. The per-table/namespace/project POLICY that governs a sweep is
`catalog.schemas.PolicyRequest` (retention, retain_versions, compact_enabled, interval, target rows),
resolved **winner-takes-all** — an exact table match shadows the namespace record which shadows the
project record, and the winner supplies EVERY field. Any surface showing an effective policy must say
which record won; an inherited value rendered identically to a set one is how nobody can tell what is
governing their data. The project-scoped surface is home's `/projects/<p>` § Maintenance.

- Creates are top-down: parent must EXIST (registry), gated on the parent's `can_*`.
- Deletes are bottom-up: a container refuses **409, naming its contents**; `cascade` is explicit;
  warehouse delete gates on the PROJECT's `can_administer`; bucket purge is a **separate opt-in**;
  project delete has **no cascade at all**.
- **`force=true` overrides the `protected` flag and NOTHING else** — the FGA gate runs first and
  identically with or without it. Test both delete doors for force-without-authz.
- **Recoverable drops are OPT-IN (#75).** With `LANCE_TRASH_GRACE_DAYS` > 0 (default 0/OFF, because a
  grace period changes what `drop_table` means for every caller) a drop DEREGISTERS and files a
  `_trash/` record; `POST /v1/table/{id}/undrop` re-registers from it; `GET /v1/table/{id}/tasks`
  shows the pending deadline (§2.4 per-object task visibility). `purge=true` is the explicit
  opt-out — a caller who means "destroy the bytes now" says so. A recoverable drop **does not revoke
  the table's FGA tuples**: the owner is the one person who needs to undrop it, and revoking made
  undrop unreachable for exactly that caller (found by driving the deployed catalog — the unit tests
  run FGA off); the grants die with the bytes, at purge or expiry. `undrop` re-registers with a
  **RELATIVE** location (the final path segment): `register_table` refuses the absolute URI
  `describe_table` had just reported, and the `dir` backend lays tables out flat. The sweep REPORTS
  expired trash and deletes nothing. COVERAGE.md's old "soft-delete is N/A, time-travel replaces it"
  entry was WRONG and is corrected: time-travel does not survive `drop_table`.
  **The CASCADE is recoverable too (#96)**: with a grace period, `drop_namespace(cascade)` never
  issues the destructive native call — a trash record pointing at bytes that call deleted would be
  a lie — and instead DETACHES the subtree (every table deregistered, namespaces emptied
  deepest-first then dropped, one `kind`-tagged record each, shared drop-time `expires_at`; tuples
  KEPT, the same #75 rule). `POST /v1/namespace/{id}/undrop` is the PLURAL undrop: rebuilds every
  trashed namespace under the id shallowest-first, re-registers every trashed table
  (relative-location form), resumable (`exist_ok` creates; already-registered = recovered);
  `GET /v1/namespace/{id}/tasks` shows the subtree's deadline; `purge=true` is the same explicit
  opt-out. A RESTRICT drop stays unrecorded on purpose — it only ever removes an empty manifest
  row. Declared-only tables (no recorded location) are skipped by undrop with a warning: no bytes
  were lost.
- **Protection covers EVERY rung since #73** (2026-08-04): warehouses/projects carry `protected` on
  their registry records; tables/namespaces carry it as a **control-root `_protection/` record**
  (`service_kit.lakehouse.protection`) gating drop/deregister/rename (table) and drop (namespace) —
  deliberately NOT schema metadata, so unprotect is never reachable through the properties door,
  toggling never creates a table version, and the guard answers even for a corrupted dataset. Set
  via `POST /v1/table/{id}/protection` / `/v1/namespace/{id}/protection`, owner-gated (`protection`
  maps to `can_drop`/`can_delete` in `_OWNER_SUFFIX_RELATION` — an unmapped suffix falls to writer
  tier). The record dies with the object: drop/deregister clear it so a reused id can't inherit it.
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
  opposite (`require_project_exists` 404s naming `POST /v1/projects`) because that 404 is a fix the
  caller needs. Class rule, not a per-endpoint judgement call.
- **A purge must prove sole ownership first.** One project may back two warehouses with one bucket
  (`projects_claiming_bucket` subtracts the caller's own project on purpose — the work+gold pair),
  so `?purge_bucket=true` checks same-project siblings AND the reserved platform buckets before the
  cascade. Without it, deleting the work warehouse wiped gold's data.
- Anything making a DESTRUCTIVE decision reads `read_bindings` (which returns unparseable paths) and
  refuses on a non-empty skip list; `list_bindings` is the tolerant half, for enumeration only. A
  binding you cannot read is a namespace you cannot see.
- `deactivate` = offboarding step one (quarantine; resolver 403s bound namespaces).
- Maintenance (compaction + `optimize_indices` + `cleanup_old_versions` with tags EXEMPT) lives in
  **`services/maintenance`** (renamed from `compaction` — it does four things, not one) —
  `catalog/api/maintenance_mode.py` is read-only maintenance MODE (503 + Retry-After), not this. The
  operations are ONE ordered pass per dataset — **compact → optimize_indices → cleanup**
  (`maintenance/services/optimize.py::compact_one`): compaction obsoletes files, the index optimize
  folds the new fragments back in, and version reclamation runs last. This is exactly what Lance's own
  guide prescribes — *"it's recommended to rewrite files before re-building indices"* and
  *"`compact_files()` followed by `cleanup_old_versions()`"* (`lance_docs/guide.md`). `compact_one`'s
  docstring now states the order correctly and may be trusted; the warning that used to sit here
  ("read the body, not the docstring") was itself stale by 2026-08-16. A policy may skip a STEP
  (`cleanup_enabled`/`optimize_indices_enabled`), never reorder them — which is why they are modules
  in one service rather than four services each rescanning every bucket.
- **A dataset URI encodes its TIER in three different places, and two of them encode it at opposite
  ends.** `maintenance/services/tiers.py` sizes fragments per tier (bronze 512 / silver 262 144 / gold
  524 288 rows — bronze rows are ~1.8 MB page images, silver/gold ~2 KB records, so one row count
  cannot serve all three). Reading the tier from the wrong segment does not error, it returns `None`
  and silently falls back to Lance's own sizing:
  `<bucket>/<project>-<tier>/<table>` (nested — tier TRAILS, reduce from the right);
  `<bucket>/medallion/<tier>` (the cascade — the child IS the namespace, and lanes are `<tier>-<lane>`
  like `bronze-media` / `gold-htr`, so the tier LEADS, reduce from the left);
  `<bucket>/<uuid8>_<namespace>$<table>` (the `dir` backend's FLAT layout — namespace and table share
  ONE directory name, so the tier is in `parts[-1]`, not a parent directory).
  Until 2026-08-16 only the first was handled, so measured live, EVERY governed tier read as untiered
  and the per-tier defaults had never once applied. Only `medallion` may promote its child; widening
  that would let a table NAMED `gold` size itself as gold.
- The reconciler reports cross-store drift and deletes nothing until its report runs clean. It runs on
  its OWN Dapr cron binding (`maintenance-reconcile-cron`), separate from the sweep's — a read-only
  drift report must not inherit the data-rewriting sweep's cadence.
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
  `read_schema_metadata`, which excludes `lineage.*`, so a replace silently destroys the #21
  self-describing coordinates. `description` is the one RESERVED key (the lakehouse renders it under the
  table name); everything else in that map is opaque user data.
- `deregister` keeps bytes ON PURPOSE (external data); `drop` removes them. Neither leaves Lance
  orphans — but partially-failed writes and unpurged buckets do, and nothing reclaims those yet.
  `services/maintenance`'s orphan pass REPORTS them (`MAINTENANCE_ORPHAN_SCAN_ENABLED`, off by
  default — it opens every dataset, unlike the rest of the drift report which compares three stores).
- **Three Lance file classes look like orphans and are not.** A scan that names any of them would
  drive a reclaimer into live data, and all three were found by running against a real estate, not by
  reading the layout doc: `_refs/tags/*.json` are TAGS, which PIN versions (`cleanup_old_versions`
  exempts tagged versions for that reason); `.lance-reserved` is a structural marker; and a large
  binary column's bytes live in `data/<data-file-stem>/*.blob`, a SIDECAR that `data_files()` does not
  name — the first live run called 29 MB of real page images reclaimable. Conversely `_transactions/
  *.txn` genuinely accumulate forever (the spec keeps one per commit attempt) and nothing prunes them.
- The FGA-only live seed (`fga_seed_demo.py`) writes projects no registry knows — the origin of
  "ghost projects". The replacement SHIPPED: `scripts/seed_estate.py` drives the real doors in
  hierarchy order — `POST /v1/projects` → `POST /v1/warehouses` → `POST /v1/warehouses/{id}/namespaces`
  → `/declare` → `POST /v1/access/tuples` LAST — so every guard runs and a state that cannot be
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
  `discover_dataset_uris` once per bucket — a bucket is created by an API CALL at runtime, so a
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
  reproducing call is `ds.stats.index_stats("lineage_run_id_idx")` against either cascade tier
  (`s3://lance-catalog/medallion/{silver,gold}`), whose index `medallion/services/compute.py::
  _ensure_lineage_index` creates as `IndexConfig(index_type="json", parameters={"target_index_type":
  "btree", "path": ...})`. `not yet implemented` is Lance's own `todo!()` — stats for the JSON scalar
  index simply do not exist yet — so there is nothing to fix on our side and nothing to file beyond
  upstream. It is CONTAINED, and deliberately: `index_health._stats` catches **`BaseException`**, not
  `Exception`, because a Rust panic surfaces as pyo3's `PanicException`, which derives from
  BaseException and would otherwise sail straight through the sweep's error handling and kill the tick.
  It logs `index_stats_unreadable` and the finding reports *"its statistics could not be read, so its
  health is UNKNOWN — not the same as healthy"*, which is the honest answer: those two indices are
  UNMONITORED, not proven fine. Do not "fix" the noise by narrowing that except clause. Note also that a
  JSON index can only be built on a Binary/LargeBinary column holding real **JSONB** — raw JSON text
  bytes fail `InvalidJsonb` at `json.rs:456`, and a string column is refused outright at `json.rs:726`.
- **`lance_ray.compact_files` DOES NOT COMPACT — upstream, and only the distributed path.** lance-ray
  0.5.0 + pylance 10.0.0: `scripts/ray_lance_job.py` stage 4 reports `compaction did not reduce
  fragments: 4->4` and exits 1, which is the sole red in `make test`. Stages 1–3 pass on the real
  cluster (distributed WRITE → 4 fragments, distributed INDEX via lance_ray, EVOLVE v3→v4), so the Ray
  integration itself works. MEASURED 2026-08-16 against the identical shape — 64 rows in 4 fragments of
  16, `data_storage_version="2.2"`, `enable_stable_row_ids=True`, evolved with `add_columns`, then
  `target_rows_per_fragment=32` — NATIVE `dataset.optimize.compact_files` reduces **4 → 2**. Same
  dataset shape, same option, opposite result, so Lance is not the defect. The job's call matches
  lance-ray 0.5.0's signature (`compact_files(uri, *, compaction_options, num_workers,
  storage_options, ...)`), so it is not misuse either. `tests/e2e-py/test_ray_batch_e2e.py` carries a
  **strict** xfail with this reason: switching the job to native compaction would delete the capability
  the test exists to prove, and strict means the suite goes red — correctly — the day upstream fixes it.
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
- **A medallion tier's maintenance run is emitted only when the sweep does MATERIAL work.**
  `sweep.py::_did_material_work` gates the emit on `fragments_removed or old_versions_removed`, so a
  correctly-idle estate records nothing — deliberately, since a 120s cron would otherwise flood the
  graph with no-op compaction runs. Consequence for verification: a stamped medallion dataset does NOT
  produce a `(:Run)-[:WROTE]->(:Dataset)` node just by being written and swept. As of 2026-08-16 both
  halves of the declared-id chain are live (producer stamps `lineage.dataset_id`, sweep prefers it),
  and the read half is witnessed in AGE for `silver$emitproof` — but no medallion tier has yet been
  observed emitting, because every sweep since has measured `fragments_removed: 0, versions_removed:
  0`. That is the gate behaving correctly, not a defect. Nor can the emit be forced, and the reason is
  worth more than the verification was: **the medallion tiers are DATA WITHOUT GOVERNANCE.** A policy
  door does reach them — `set_namespace_policy` builds its path from `settings.root` and
  `resolve_policy` matches a namespace record by directory prefix (`rel.startswith(path + "/")`), so a
  record on `medallion` governs `medallion/bronze`; and `retain_versions` alone sets
  `effective_older_than = None`, which is keep-last-N with no age bound and would sidestep the 7-day
  `MAINTENANCE_OLDER_THAN_DAYS` wall entirely. What blocks it is AUTHORIZATION, measured live
  2026-08-16 with a real dex bearer: `POST /v1/namespace/medallion/policy/set` → 403 *"can_delete
  required on namespace:medallion"*, and `POST /v1/table/bronze$events/policy/set` → 403 *"can_drop
  required on table:bronze$events"*. Both 403 rather than 404 because the gate runs before existence
  resolution — and neither object exists: `medallion` is not a catalog namespace (it is absent even
  from the reconciler's `unbound_namespaces`, which lists `bronze`, `transcripts_v2` and the three
  `acme-*`), and `bronze$events` is not a registered table. With no namespace record, no table record
  and no parent tuple, NO principal can hold `can_delete`/`can_drop` there, so no policy, protection or
  grant can ever be applied to the datasets the cascade writes. **Read that precisely: they are not
  UNMAINTAINED, they are un-OVERRIDABLE.** The sweep covers them like everything else under the
  platform's own settings — `MAINTENANCE_OLDER_THAN_DAYS` (7), `tiers.py`'s per-tier fragment sizing,
  `optimize_indices` — which is why every live summary counts them among its 27 datasets and reports
  `index_findings` for `medallion/{silver,gold}`. What is unavailable is the TENANT-facing layer: a
  per-dataset policy override, a `_protection/` record, an FGA grant.
  **But "no table record" is a STATE, not a law — and the door that fixes it already exists.**
  `register_table` is precisely for data written outside the catalog's own doors: it turns written
  bytes into a `table:` object, seeds ownership tuples, and every governed path (protection, trash,
  credential vending, the FGA doors) keys off that object. It needs no warehouse, which is why it works
  in the RESERVED bucket — proven by shipped code, not theory: `#88 step 5` registered `gold$htr` from
  `medallion/services/htr_register.py`, and `#88 step 7` ran that lane live end-to-end. So the reserved
  bucket blocks the WAREHOUSE route (a tenant claiming platform storage) while leaving the REGISTRATION
  route open (naming an individual dataset) — two different mechanisms, and conflating them is how you
  conclude the cascade can never be governed. It can; those tiers simply were never registered.
  As of 2026-08-17 that is being generalised: `htr_register.py` → `catalog_register.py`, modality-neutral
  by construction, because the logic only ever took an id and a URI — governance belongs to the CASCADE,
  not to whichever workload happened to be built first, or every new modality starts ungoverned.
  **And that is DELIBERATE — the platform refuses to let it be fixed that way.** Driven live 2026-08-16:
  `POST /v1/projects` for a `platform` tenant succeeded 200, and the very next call was refused —
  `POST /v1/warehouses {bucket: lance-catalog}` → **400 "bucket 'lance-catalog' is reserved platform
  storage (catalog root/registry or a medallion zone bucket) and cannot back a warehouse"**
  (`catalog/api/v1/endpoints/warehouses.py:166`, `settings.reserved_bucket_set`). The guard is audited
  (2026-07-23, "the Mallory scenario's first door") and the reason is exactly the disclosure such a bind
  would create: `provision_bucket` is idempotent on an existing bucket, so the claim would silently
  succeed, make that project the bucket's owner, and let **a later project-policy set govern every
  tenant's data in the shared catalog bucket**. The reservation is DEFENCE IN DEPTH across three
  independent places, so do not assume one check is the whole story: `reserved_bucket_set`
  (`catalog/core/config.py:112` — the catalog root, the control/registry root, the model registry, every
  approved multi-base data bucket, and `LANCE_RESERVED_BUCKETS`), the warehouse-create and bucket-claim
  refusals (`warehouses.py:164`/`:641`), and — for the case where a bad record already exists —
  `policies.py:372`, which subtracts reserved buckets from the project-policy MATCH set rather than
  trusting the registry. So "give the medallion path a warehouse" is not an
  unfinished chore — it is a rejected design, and the cascade tiers are ungoverned BY CONSTRUCTION
  because they live in platform storage. The probe project was deleted again (`DELETE /v1/projects/
  platform` → 200, `tuples_revoked: 1`); do not re-create it. If those tiers ever need retention or
  protection, the answer is a platform-level mechanism (a maintenance policy keyed on the reserved
  bucket, or moving the cascade OUT of `lance-catalog` into a tenant warehouse), never a warehouse over
  the reserved bucket.
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
- **The manifest's feature flags ARE reachable, and they are the refusal gate (#64).**
  `maintenance/core/features.py` reads `reader_feature_flags`/`writer_feature_flags` as varints at
  protobuf fields 9/10 of `LanceDataset._ds.serialized_manifest()` — pylance exposes neither field
  but its own pickle path uses that blob. Measured on pylance 9.0.0: plain `(0,0)`, `delete()`
  `(1,1)`, `enable_stable_row_ids` `(2,2)`, `add_bases`/`shallow_clone` set 16, a committed
  `LanceOperation.DataOverlay` sets 64. Both `compact_one` (BEFORE any rewrite) and the orphan scan
  refuse anything outside `SUPPORTED` (= 1|2|4|8), and the sweep reports refusals as their own
  `summarize()` line + `compaction.datasets.refused` counter — never inside `errors` or `skipped`.
  Two reasons the flags rather than the consequence: **flag 16 compaction was genuinely destroying
  the feature** (`compact_one` on a shallow clone returned `fragments_removed=8, error=None` and
  left the clone holding a full local copy of data it had only referenced), and **`add_bases`
  registers a base no `DataFile` resolves through yet** — every `base_id` stays `None`, so the
  consequence check passed it as `checked=True` with orphans named. Flag 64 was only ACCIDENTALLY
  safe: pylance refuses that open itself, and the untyped `open:` error it produced is exactly what
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
  staleness. (`GET /v1/events`' ring buffer stays per-replica by design — the poll endpoint serves
  each replica's own buffer.)
- The `\Z`-anchored `CONTROL_ID_RE` (`catalog/core/identifiers.py`) is the ONE id-shape rule. It was
  three copies that had already drifted: Python's `$` also matches before a trailing newline, so
  `"acme\n"` was refused by one door and accepted by another.
- Credential-level tenant isolation IS attacked since #74: `tests/unit/test_vending.py` evaluates
  the cross-tenant read/write against the session-policy document the vendor really builds (IAM
  semantics, per tier, with two negative twins — B still reaches B's own table, and a read-tier
  credential cannot write its own), and `tests/e2e-py/test_credential_isolation_e2e.py` drives the
  real attack with vended credentials (LIST/GET/PUT, env-gated — the CI half is #84). The offline
  half deliberately mocks no store: moto does not enforce inline session policies. Byte-placement
  isolation stays proven by `test_warehouse_routing.py` / `test_warehouses_e2e.py`.
