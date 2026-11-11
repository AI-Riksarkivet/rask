---
name: rask-lance-catalog
description: "The Lance catalog and the governance layer above it — the Lance Namespace spec (54 ops, the 24-code problem+json error model, the `POST /v1/<object>/{id}/<action>` route grammar) and rask's own `project > warehouse > namespace > table` hierarchy: the guards, the registries, protection/trash, and `services/maintenance`. Use when adding or changing a catalog endpoint; raising an error from the catalog; touching the hierarchy guards, the project/warehouse registries or `seed_ownership`; designing create/drop/undrop/cascade or protection semantics; working on compaction, maintenance's work queue and a table's pass, the hourly audit or the daily scan, a table's orphan check or the listing-floor raise, or the reconciler; or answering \"is this spec-conformant\"."
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
  the native `dir` backend stubs, and `alter_transaction`, which the `dir` backend answers SUCCEEDED having applied
  nothing (measured on pylance 13.0.0). **A transaction is `<table id>$<transaction>` and is answered on its table**
  ([[LH-221]]): describe asks `can_get_metadata` and alter `can_write_data` on the table the id names, an id of one
  segment is refused 400 first (`identifiers.transaction_table`), and describe reads through the door's resolution
  (`TransactionTableDep`, `DoorTable.read_natively`). The model has no `transaction` type. `refresh_materialized_view` refuses every caller 403: it needs
  `can_refresh` on a view, and no view exists to hold it. **pylance's `RestNamespace` implements 51 of the 54**:
  `update_field_metadata` (served here), `batch_create_table_versions` and `batch_commit_tables` fall to the
  `LanceNamespace` base class, which answers code 0 itself and sends nothing, so a client reaches them through
  the spec's generated `lance_namespace_urllib3_client`. `CreateTableVersion` is served under
  `managed_versioning=true` (ADR 0157, LH-471): see "Stock clients commit through `version/create`" below.
  Spelling matters when grepping: the spec op and
  the served route are SINGULAR — `POST /v1/table/{id}/backfill_column` (`spec.yaml:1570`,
  `endpoints/columns.py:146`) — while `alter_table_backfill_columns` is the native method it wraps.
  **`rename_table` is NOT one of them.** It is backed in-process and answers 200; `docs/COVERAGE.md:11-15`
  carries that correction, dated 2026-08-05. It MOVES A POINTER, never bytes (2026-09-04):
  `dataplane.rename_table` retires the source pointer and re-registers the destination at the source's
  own location, so version history, row count and the directory all survive untouched — every V2 reader
  finds a table through its row's `location` (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:55`).
  THE LOCATION CLAIM IS THE DOOR'S RACE ARBITRATION ([[LH-204]]), not the call order: the `dir` backend
  arbitrates only object-id ADDs, so a register at an occupied location commits and two deregisters of one
  id both succeed (measured on pylance 12.0.0: eight barrier-threaded renames left eight live ids on one
  dataset, where `drop_table` on any destroys the others' bytes). The rename hands the location's claim
  (`lakehouse.location_claims`, `_locations/` on the control root, put-if-not-exists then
  ETag) from source to destination before either backend call, stamped with a per-request token so two
  renames into ONE destination are not each other's retry; the losers answer 409 code 14, and a failed
  retire or destination register puts the source back and hands the claim back under that token. **The claim is
  handed over FENCED for the rename** ([[LH-586]], `location_claims.hand_over`): until the destination is registered
  it names a table the backend does not describe, which any other take reads as a gone holder and a registration of
  the destination's own name as an ended table's claim to replace under a new uuid (measured 2026-11-11 on a
  registered table: that RegisterTable answered 200 over the renamed table's bytes, under a uuid none of its grants
  reach). The fence refuses every other request's take (`LocationFencedError`), drop, deregister and rename until the
  rename lifts it, once its `child` edge is written, so a RegisterTable of the destination landed inside the rename
  answers 409 code 14 and detaches the row it attached; where that row still stands as the rename registers its
  destination, the rename answers the name's conflict (409 code 5) with the source put back. A dead rename's fence
  lapses with its claim's `LEASE`. **A rename into another namespace takes the table's grants along, so it asks
  grant authority** ([[LH-568]]): `can_move` on the table (`manage_grants and can_drop`) in `can_drop`'s place
  (`fga_deps._rename_relation`, asked by the router) and `can_accept_moved_table` on the namespace it enters in
  `can_create_table`'s (`fga_deps.require_rename_destination`), so under managed access a writer brings no table in
  and an owner inside takes none out; a rename that keeps its namespace asks `can_drop` on the table and
  `can_create_table` on that namespace. Where the table inherits from moves while the rename holds the claim
  (`dataplane.Reparent`, built by `fga_deps.parent_edge_mover`), so the name and the edge have one arbiter. Its
  `parent` edge moves in one OpenFGA Write BEFORE the name; its `child` edge on the source is taken away with it and
  the one on the destination is written only once the name is registered there, because a namespace's removal ends
  every object its `child` edges name (`fga_deps.end_removed_subtree`) and a table between its two names is in
  neither namespace's listing. An undone rename returns the edge before the claim is handed back, and so does a
  move whose answer OpenFGA lost. The arrival (`fga_deps._arrive_at_destination`) writes the `child` edge while the
  rename still holds the claim fenced ([[LH-586]]), so no rival rename, drop or cascade acts on the table between its
  registration and its edges being whole. A `parent` edge already at the destination is an earlier attempt's and is
  finished by renaming there again, and so is the move of a table found inheriting from the namespace it is named in
  and the one it enters both (`_settle_an_unmoved_edge` takes the first away); one anywhere else refuses the rename
  409 code 14 with nothing moved. **A refused registration writes no tuple, and no door writes a second parent**
  ([[LH-586]], owner 2026-11-11): a RegisterTable answered 409 changes no grant, whatever its table lacks, and a
  create that keeps a table, or its owner's Overwrite, links it to its name's namespace only where it inherits from
  nowhere and grants its caller nothing (`fga_deps.link_an_unlinked_table`), so none puts the readers of the namespace
  a rename left, or died leaving, back on the table. A registered table whose seed never ran holds no tuple: the
  drift report lists it under `ungoverned_tables`, and the estate admin writes its `parent` and `child` edges at
  `POST /management/v1/access/tuples`, after which its namespace's grants reach it (measured 2026-11-11). Pinned on a
  real OpenFGA by
  `services/catalog/tests/test_a_rename_into_another_namespace_needs_grant_authority_and_moves_the_parent_edge_whole.py`.
  A destructive drop or cascade refuses 409 while the claim names another
  live holder. **A door acts on the table its name resolved to** ([[LH-521]], ADR 0173 §7): the drop, deregister
  and rename doors take `ResolvedTableDep` (`catalog.api.dependencies.get_resolved_table`, solved once per request:
  the table's location and that location's claim with its ETag, `table_claims.resolve`) and never describe the name
  again. A drop or deregister FENCES that claim under the ETag it read before its backend call
  (`location_claims.fence`, lifted by `lift_fence`; a fence its request never lifted lapses after `LEASE`), and a
  rename hands it over under that ETag (`location_claims.hand_over`): a rename landing after the door's read
  answers the door 409 code 14, one landing after the fence answers 409 itself, and every take refuses a claim
  another request has fenced. Each refuses 409 while the claim names another live table, deregister included.
  **A cascade removes each table as that door does** ([[LH-586]]): the namespace door's destroying and recoverable
  cascades and the warehouse delete resolve each listed table's name again and fence its claim
  (`table_claims.fence_listed`) before its row goes, so a rename landed inside a table's removal answers 409 itself.
  A listed table whose name names nothing by then left since the listing (a drop of its own, or a rename that carried
  it out alive): the cascade skips it and ends, announces and tracks only what it removed itself; one whose name
  another table took answers the cascade 409. The destroying cascade and the warehouse delete resolve each name as
  its turn comes; the recoverable cascade resolves every name before its first write (concurrently), so a table that
  changed since answers it 409 at that table's turn. Measured 2026-11-11 without the fence: the rename answered 200,
  the cascade 200 (the recoverable one 404, the warehouse's 503), and the moved table stood with every tuple revoked
  or a trash record against it.
  Create, declare, register and both undrops take a claim, a recoverable drop
  keeps it for the trashed table's uuid, and destructive drops, deregister and the purge release it. A release replaces
  the claim it read with the same claim stamped `released_at`, under that claim's ETag, and never deletes the
  record ([[LH-458]], ADR 0172): a hand-over landing between its read and its write survives, and `holder` and
  `take` read a released claim as a free location. A holder that no
  longer resolves to its location is taken over only after `LEASE` (15 min), timed on the object store's clock: every
  stamp and every lease is judged by the `Date` of the store's answer (`records.StoreRead.at`, ADR 0179), never a pod's
  (`tests/integration/test_a_lease_is_timed_on_the_stores_clock.py`). Pinned by
  `tests/integration/test_concurrent_renames_leave_one_id_per_location.py` (with a drop, a recoverable drop and a
  deregister racing a rename, the rival landed after the door read its claim and as it acts) and
  `packages/lakehouse/tests/test_a_release_frees_only_the_claim_it_read.py`. The spec's *minimum* is 8 metadata ops; we carry the whole list
  including versioning, tags, branches, indices and transactions.
- **Every other table door acts on that resolution too** ([[LH-530]]): each door that reads or writes a table's data or
  metadata takes `DoorTableDep` (`table_claims.DoorTable`), opens and commits at the location the resolution read, never
  at the name, and each open refuses 409 code 14 (`TableChangedError`) unless the location's claim is the one the
  resolution read (its ETag) (`DoorTable.require_held`): a drop, a re-create, a rename, a deregister and a re-register
  each write the claim. A recoverable drop leaves it as it was and files a trash record, so where a trash record names
  the claim's table the open also asks `require_named`, since a crashed undrop leaves its record beside the live table
  it restored. Every write Lance serves on a handle commits through one opened there, main's merge, restore and schema
  metadata included. A native call takes the name and no location, so around it the door asks whether the name still
  resolves to the location under that claim (`require_named`): after a native read, its answer discarded and its failure
  told the change first (`DoorTable.read_natively`), and right before a write Lance serves only through the namespace
  (`DoorTable.write_natively`): both version finalizes (`version/create`'s and `commit_stamped`'s, the backend's
  put-if-not-exists), drop_index, whose system-index refusal is the namespace's and not Lance's, and backfill. Describe
  answers for the name off its own describe (`require_described_at`). A readback after a committed write (the lineage
  trailer, `dataplane.committed_version`, `branch_identifier`) opens the location unguarded: a 409 after a commit would
  make a retry commit twice. An `Overwrite` create writes at the table it asked `can_drop` on, and one whose door found
  no table creates in Create mode, so a table created under the name meanwhile answers TableAlreadyExists rather than
  being overwritten without `can_drop`. The catalog has no by-name open; a test arranging a table by its name opens
  through `services/catalog/tests/tables_by_name.py`. The merge reads each `*_filt` as its clause's own condition and
  `when_not_matched_insert_all` as false when absent (`spec.yaml`), on main as on a branch; the `dir` backend's native
  merge reads an absent insert flag as an insert. Residual, stated: a write through a handle can land between its guard
  and Lance's commit, and reach another table only where a registered location is deregistered and registered again in
  that window; a native write can reach another table only where a drop and a create land between its guard and the
  backend's own resolution of the name, one describe later; a native read is unguarded only against the name leaving the
  location and returning to it within the one call: a recoverable drop (of the table, or of its namespace's cascade),
  another table created under the name and gone again, then the undrop, which leave the claim's bytes, and so its ETag,
  as they were. Pinned by `services/catalog/tests/test_a_door_acts_only_on_the_table_its_name_resolved_to.py`, which
  lands a drop and a re-create after the router authorized each door, and, on a handle write and a native read, a
  recoverable drop and a create, and a deregister and a register at the same location.
- **Every table and namespace has a uuid the catalog mints** ([[LH-519]], ADR 0173). A table's lives in its location
  claim: create, declare and register mint it (`table_claims.claim_new_table`; a declaration takes its claim before any
  backend call, and a failed one removes its marker and releases the claim, [[LH-520]]), a create into a table an
  earlier request declared keeps that declaration's, a rename hands it over, a recoverable drop and both undrops keep it,
  and a destroy or deregister releases it, so a re-created name or a re-registered location gets a new one. A new
  table's take replaces a claim its own name still holds: its door's backend call found the name free, so that claim is
  an ended table's whose release was lost, unless another request holds it fenced, as a rename holds the claim it
  handed to its destination ([[LH-586]]). A namespace's is its `rask.uuid` property in its `__manifest` row: both create doors and the provenance boot
  mint it (`identifiers.with_minted_uuid`), an undrop restores it from the trash record, and an Overwrite mints a new
  one. DescribeTable and DescribeNamespace answer each as `rask.uuid`, and every create, declare, register and namespace
  door refuses a caller's `rask.*` property (only an admin's `rask.tier` on a namespace). Pinned by
  `tests/integration/test_the_door_that_makes_a_table_or_namespace_mints_its_uuid.py`.
- **Every grant is held by the object's uuid** ([[LH-524]], ADR 0173 §8): OpenFGA objects are `table:<uuid>` and
  `namespace:<uuid>` (`fga.table_object`, `fga.namespace_object`), the parent and child edges join uuids, and a
  top-level namespace's parent is its warehouse. Every door stays addressed by name and resolves it ONCE per request
  (`catalog.api.dependencies.Resolver`, `ResolverDep`, which `authorize`, the tier judge and `ResolvedTableDep` share),
  so a door acts on the table it authorized. An absent name is refused 403, audited and refused by the name, and only a
  READ door converts a refusal to 404, answering a name the caller cannot see exactly as an absent one, so the answer never
  depends on whether the name holds an object ([[LH-236]], `fga_deps._refused_read_as_absent`): seeing a namespace is not
  seeing its children, since the model grants `can_get_metadata` upward from any one child. It converts for a caller
  holding `can_get_metadata` on ANY ancestor namespace that stands, a hidden one between them never ending the walk, or on
  an object a recoverable drop trashed at the name or an ancestor's, whatever stands there now ([[LH-536]]: a cascade
  leaves children whose parent went too). A top-level name is never answered from its warehouse, since upward
  visibility makes every holder of one namespace a reader of it. Pinned by
  `services/catalog/tests/test_an_absent_object_is_not_found_rather_than_forbidden.py` on a real OpenFGA. A rename writes no tuple, except
  that a rename into another namespace moves the table's parent and child edges, under the rename's claim
  (`fga_deps.parent_edge_mover`, [[LH-568]]); a table created again
  under a dropped name is another uuid and inherits nothing; a name holding a space never reaches OpenFGA. Every
  listing batch-checks its own candidates on their uuids and fills its page (`fga_deps.authorized_page`, [[LH-223]]);
  only `/management/v1/me` calls `list_objects`, and the estate admin's raw ListObjects explorer. Grant, review and
  access answers carry `object` (the uuid object) and `name`; a control event's `object_id` is the uuid object with
  `object_name` beside it.
  A check-only service asks the decision door's `/management/v1/decisions/check` about objects by uuid, or about
  `tables` by name ([[LH-531]]): the door resolves each name through the request's `Resolver`, decides on the uuid and
  answers keyed by the name, with the table's and its namespace's uuid only when allowed (a name naming no table, one
  in a quarantined warehouse and one no uuid identifies answer refused). What a caller is answered is its scope's ([[LH-563]]): every object for one holding `can_decide_access` on the estate root (lineage), otherwise each object it holds that relation on, which `decider` on a project or a warehouse confers on everything beneath, granted at the project's or warehouse's access door; an object outside the scope is answered refused unasked, as an absent one is (`tests/integration/test_a_check_only_service_decides_through_the_catalog.py`). Lineage's HTTP door asks it, as the caller's
  subject on `can_get_metadata`, for every governed dataset its DescribeTable left without a uuid, which is every name a
  service account's lineage-audience token names (`tests/integration/test_a_service_account_records_lineage_on_the_tables_it_names.py`).
  Pinned by `services/catalog/tests/test_a_grant_is_held_by_the_objects_uuid.py` and
  `services/catalog/tests/test_a_listing_judges_its_own_candidates.py`.
- **An object that records no uuid holds no grant, and only the estate admin's deregister reaches it** ([[LH-534]]). A
  table whose location carries no claim recording a uuid, or a namespace with no `rask.uuid`, raises
  `object_uuids.UnidentifiedObjectError` (code 18) at every door that resolves it. A listing leaves it out and logs
  `listing_left_out_unidentified_object` at ERROR with its name and location, so it never fails the listing for every
  caller. `POST /v1/table/{id}/deregister` on such a table asks `can_observe_events` on the estate root in place of
  `can_deregister` (`fga_deps._rung_check`) and refuses anyone else with the refusal an absent name gets; the door
  fences the location, detaches the row, releases the claim and announces `table_deregistered` naming it
  `table:<name>` with no `object_uuid`. It emits no lineage marker, since lineage refuses an output no uuid names
  ([[LH-166]]). A namespace that records no uuid has no such door. Measured live 2026-10-24: a rename on helm rev 4
  (before LH-528's second commit) re-registered its table at a location no claim named. Pinned by
  `services/catalog/tests/test_the_estate_admin_deregisters_a_table_no_uuid_identifies.py` and the listing test above.
- **A table the catalog makes lives at `<namespace root>/<table uuid>`** ([[LH-520]], ADR 0173 §5), the namespace root
  being the root of the warehouse its top-level namespace is bound to (`NamespaceRootDep`; CONTEXT.md's
  `<warehouse root>/<table uuid>`), else the catalog's root. Create and declare
  run the `dir` catalog's own DeclareTable steps with the catalog choosing the location (`dataplane.declare_table`): mint
  the uuid and take the location's claim before any backend call, write `.lance-reserved` there, then record the row
  through RegisterTable's relative location, which answers `is_only_declared` until data lands (measured on pylance
  13.0.0). The backend's own DeclareTable refuses every location but its `<hash>_<object_id>`, so the catalog never calls
  it; a declare naming a location is refused 400, a root-level table id 400 (`dir` resolves one by `<name>.lance`, never
  by its row), and create and declare `properties` are kept nowhere (RegisterTable keeps none, `spec.yaml:2719-2724`;
  create still stamps them on the schema metadata, [[LH-503]]). So no name reaches a vended prefix. A REGISTERED table
  keeps its registrar's location, which `describe_table` answers percent-encoded; every vend
  (`vending.split_s3_location`) and every catalog read and write of a table object (`dataplane.dataset_fs`,
  `object_store_path`, so `version/create` too) uses the key it decodes to once, and so does a destructive drop: the
  table door and the namespace cascade judge the bytes, remove the row with DeregisterTable and delete under the decoded
  key and the spelling themselves (`dataplane.destroy_table`, [[LH-532]]), since the backend's own drop deletes where
  `Path::join` percent-encodes the location. A location holding a `_versions/` tree gets a residue trash record (`trash.make_record(residue=True)`, grace 0) before the row goes, cleared once the bytes are; a delete that fails leaves it, so the claim stays held (`table_claims.kept_in_trash`), undrop refuses it and the trash purge reclaims the bytes. `_versions/` is deleted last. The control prefixes a drop, the purge and discovery refuse are one set, `lakehouse.control_prefixes`. The register door refuses a location in
  a uuid-spelled directory directly under the namespace root (`identifiers.is_spelled_as_a_uuid`): that is the catalog's
  layout. Pinned by `tests/integration/test_warehouse_routing.py`,
  `services/catalog/tests/test_a_stock_client_opens_on_the_vended_credential.py` and
  `tests/integration/test_a_planted_base_is_refused_and_freezes_nothing.py`.
- **Route grammar:** `POST /v1/<object>/{id}/<action>` — everything a reverse proxy needs (authN/Z,
  routing) is in the PATH, never only in the body. Path/body id conflict → 400. List ops are GET with
  query-param pagination; data ops (create/insert/query) are **Arrow IPC**, not JSON; count/explain
  return plain text.
- **Identifier:** segments joined by the delimiter `$` — `["a","b","t"]` ↔ `a$b$t`. The root namespace is the
  delimiter itself. `$` is the spec's (`spec.yaml:2314-2318`) and no setting changes it ([[LH-526]]): the router's
  shape rung (`fga_deps.require_request_names`, between `authenticate` and `authorize`, so 400 before 403) refuses a
  `?delimiter` other than `$`, and refuses every segment a naming door is handed (a create, declare, register,
  namespace or view create path id, a rename's destination, a batch's ids) that is not a name: Unicode NFC, 1 to 255
  characters, no `$`, `+`, `/` or control character, no edge whitespace (`identifiers.require_names`, ADR 0173 §4).
  `*` and `?` are names, since no name reaches a vended policy. The catalog reads its path from `raw_path`
  (`catalog.api.path_reading`): a bare `+` is a space, as pylance's Rust `RestNamespace` sends one, a `%2B` stays a
  `+` (so refused), and a `%2F` is refused 400 before routing. The Gateway's `/api/catalog` route forwards the path
  unrewritten and the catalog strips the prefix itself (`LANCE_PUBLIC_PATH_PREFIX`), since Traefik's URLRewrite rebuilds
  the path from its decoded form.
- **Identity headers:** `api_key` → `x-api-key`, `auth_token` → `Authorization: Bearer`; arbitrary
  context maps BOTH WAYS through a `header.<name>` context KEY — `{"header.x-trace-id": "abc"}` is
  sent as `x-trace-id: abc` (prefix stripped) and every response header returns as `header.<name>`
  (`spec.yaml:2482-2489`; `ns_catalog/catalog/rest/index.md` § Context Header Mapping). Identity given both in the body
  and in a header is taken from the header (`catalog/rest/index.md:64`).

## The error contract — NEVER invent a status

The spec defines **24 numeric error codes (0–23)** — 22/23 are `TableBranchNotFound` /
`TableBranchAlreadyExists`, added with the branch ops — identical across Python/Java/Rust/REST;
clients dispatch on the **code**, not the HTTP status. On the wire they are **RFC 9457 problem
bodies** (`application/problem+json`).

**The one rule:** an endpoint raises `lance_namespace` typed errors
(`InvalidInputError`, `NamespaceNotFoundError`, …) and lets
`lakehouse/ns_errors.py::install_problem_handlers` translate — it maps all 24 codes
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
| who | the FGA model's `can_*` relations (`lakehouse/governed/auth/model.fga`) — the app never invents policy |
| what is possible NOW | the registries, checked BEFORE the native write: **project records** (`catalog/services/projects.py`, `_projects/<id>.json`), warehouse records + `top_ns → warehouse_id → root_uri` bindings (`catalog/services/warehouses.py`) |

**A tenant EXISTS when its registry record does** — not when a warehouse implies it and not when FGA
holds tuples for it. `POST /management/v1/projects` writes the record AND the `project#admin` tuple (the creator's; a
machine identity names the person in `admin` and is never admin itself, LH-433, `fga_deps.project_admin_for`; a
service account's subject is never a person, `is_person_subject`) in
one operation (estate-admin gated on `can_observe_events` at the root), which is what stops existence
and permission drifting apart. **A project always keeps a direct admin** ([[LH-434]]): the members door
(`DELETE /management/v1/projects/{id}/members`) refuses the last one 409 code 14, and the store arbitrates the guard.
An admin revoke deletes the admin tuple and moves `project#admin_revision` (`revision:<n>` to `<n+1>`, a bookkeeping
relation no permission reads) in ONE Write without ignoring duplicates (`fga.commit_unless_changed`), so of two
overlapping revokes one commits and the other re-reads and judges again; a refused Write is a 400 on the memory
datastore and a 409 rollback on a SQL one. Both doors that revoke a project `admin` go through `catalog.services.project_admins.revoke_admin`: the members door
and the estate admin's offboarding door ([[LH-233]]), `DELETE /management/v1/access/principals/{principal}`, which revokes
every tuple naming a principal (read type by type), refuses 409 code 14 before revoking anything while the principal is
any project's only direct admin, refuses 400 code 13 the caller and every principal whose grants git declares (one holding
a rung on the estate root other than a machine role, or a warehouse `maintenance` tuple, which only the bootstrap hook and
the catalog's own seed write, and the accounts the catalog's settings name; judged on the tuples, so the catalog names no
client, XC-183), and revokes a machine role on the estate root (a relation only a user holds directly,
`fga.machine_relations`) like any other grant: the hook grants one only to the release's own accounts, and any other
holder was granted it at a door ([[LH-563]]). It records `_principals/<blake2s>.json` (`GET` on the same path), answers `revoked` as the tuples it removed and
emits `principal_deleted` (signed by the catalog), on which a notifier erases what it keeps for the person. The reconciler's tuple scan skips bookkeeping relations. There is **no bootstrap exception** in warehouse-create: one
door, and a warehouse-owner cannot ride a create into project admin. **A project is read by the callers its list shows
it to** ([[LH-587]]): the list answers an estate observer every tenant and anyone else the projects they are a `member`
of (`projects._visible_projects`), and `GET /management/v1/projects/{id}` decides over the same two doors
(`fga_deps.require_any_relation`: `can_observe_events` on the estate root, or `member` on the project), so the trail
holds one ALLOW naming the door that opened the read, or a DENY on each for a caller holding neither, who is answered
the missing project's 404. Pinned by `services/catalog/tests/test_a_project_is_read_by_those_its_list_shows_it_to.py`.

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
read-only maintenance gate (`catalog/api/maintenance_mode.py`) refuses writes on both mounts. It classifies a request by
the ROUTE it matches, never by its path text, since a name can spell a read action (`DELETE /management/v1/projects/list`):
GET, HEAD and OPTIONS read, DELETE, PUT and PATCH write, and a POST reads only when its route's last literal segment is a
listed read action (the vend door at the read tier alone); a POST matching no route writes. Through the window the spec
vend doors vend at most the read tier (`fga_deps.spec_vend_tier`), and the catalog's cron ticks at the pod root are
answered 200 `{"skipped": true}` without running (`catalog.api.cron.ticks_of`: each mount records its tick, so a tick
mounted is a tick held). The chart's one `readOnlyWindow` renders the flag into the catalog, maintenance's worker and lineage's relay
alike ([[LH-572]]). The same classification
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
  its run and inputs, and every named input, on any tier, must resolve to a table the committer may read (`can_read_data`),
  or the commit is refused 400 code 13 with one sentence for an absent name and an unreadable table
  (`fga_deps.require_declared_inputs`, ADR 0183 §2); before the commit is finalized the catalog records each input's uuid
  for the writing table's uuid (`catalog.services.table_inputs`, `_table_inputs/` on the control root, never ended). The
  run is `catalog.core.run_context`: on `create`, `insert` and `merge_insert` it comes from the spec Context headers
  `x-rask-run-id` (a UUID), `x-rask-job-namespace`, `x-rask-job` (all three or none: the run is the commit's OpenLineage
  `parent`, ADR 0191), `x-rask-inputs` (a JSON array of `{table: [segments], version, branch}`) and the door stamps it as
  the transaction properties `rask.run_id` / `rask.job_namespace` / `rask.job` / `rask.inputs`; at `version/create` the
  catalog reads the same properties from the staged manifest, which may carry no other `rask.*` key but `rask.author`,
  and refuses a `rask.author` other than the caller (a property any committer can write proves authorship only because
  of that refusal). A compaction and a restore add no data, so they name no run. A rename commits nothing a run can
  reach, so moving a table into silver or gold from another tier is refused, and so is `register` into one, which
  attaches bytes no run produced through the catalog ([[LH-463]]). `update` and `add_columns` read no run from the
  Context, so silver and gold refuse them 406. The judge answers allow or refuse; a version breaching the project's
  quality gate is held when it is published, at the publish door (ADR 0161, below). Nothing anywhere reads a tier from
  a name.
- **Every version names its author, and every door commits stamped** ([[LH-497]]). Each catalog door stamps
  `rask.author` (the verified caller) and `rask.operation` (the lineage operation it announces) on its commit, on main
  and on every branch, through `dataplane.commit_stamped`: the door's transaction is planned on the ref's latest version,
  committed detached with the stamp and finalized as exactly the next version by the backend's put-if-not-exists, and a
  writer that lost that version plans again. An attempt that cannot land (its finalize lost, or a judge refused it) first
  deletes its own transaction file, the files it wrote and their committed-files record, for an operation that writes its
  own (a `Restore` keeps the files it names), so nothing waits for Lance's 7-day rule ([[LH-564]]). **A commit that lost its race takes its ref's commit turn** for its
  remaining attempts ([[LH-259]], ADR 0186, `lakehouse.commit_turns`, `_commit_turns/` per table uuid), and a
  stock writer that lost its race, its staged version found taken by `version/create` (409 code 14) or its finalize lost,
  is handed the ref's next turn under its verified caller and its transaction's uuid, which pylance keeps when it stages
  again, unless it staged a Merge, which Lance never retries (`transaction.md` § Merge Compatibility); a handed turn that
  lapses unused simply lapses. Every other writer of the ref waits while a door's commit holds the turn (`HOLD`, 30 s,
  Lance's own commit-retry budget, which pylance's backoff needs on the estate: 6.2-11.7 s between attempts, measured),
  and a turn handed to a stock writer holds the catalog's own writers, not that writer's peers. `/insert` and
  `version/create` wait in the event loop (`DoorTable.wait_for_turn_in_loop`), holding no request thread; a wait only a
  thread can make takes one of 20 seats per process (`commit_turns.THREAD_WAITERS`), past which its commit is refused 409
  code 14 (`TurnWaitRefusedError`). A table's waiters in one process share one read of its turn record, each answer
  for 0.1 s after the store gave it, keyed by the store and the signing access key id (`commit_turns._SharedReads`; read
  per waiter, 53 waiters pegged the catalog's CPU on the estate); only waiting takes that view, and who holds a turn is
  read from the store (`TableTurns.keeps` for a handed turn's release, a take's own read). Without the turn a writer
  committing in a tight loop won every version: measured, a `cast(NULL)` add lost all ten plans and a stock writer landed 0 appends beside 639. The
  capturing namespace answers no version after the one planned on, so Lance stages the next one rather than spending its
  own retries on a version committed since. Most pylance operations take no transaction properties (13.0.0: `update`,
  `delete`, `add_columns`, `alter_columns`, `drop_columns`, both metadata updates, `drop_index`,
  `MergeInsertBuilder.execute`), so for those the plan is Lance's own call made on a handle opened through an in-process
  namespace under managed versioning (`dataplane.captured`), which keeps the staged manifest instead of finalizing it;
  the transaction read off it leaves the rows, schema, indices and version Lance's own call leaves (measured). Building
  a schema op's transaction by hand is not possible: `LanceSchema.from_pyarrow` renumbers field ids, and a `Project`
  built from it read one column's data as another's. A keyed insert and a merge plan `execute_uncommitted` instead,
  since `LanceDataset.commit` of one rebases past a racing writer without its key check (a duplicate key landed). A
  restore is planned as Lance's `Restore`, and `drop_index` keeps the `dir` backend's own refusals (a system index 406,
  a missing one 404). **Every commit of an operation the doors announce records the caller it was authorized for**
  ([[LH-550]], ADR 0187), at `_commit_authors/<table uuid>/<transaction uuid>/<version>.json`
  (`lakehouse.commit_records`) naming the ref's key (`ref_key`: the branch and its incarnation, `""` for
  main), so a version put past every door under another commit's transaction uuid (pylance 13.0.0 commits any uuid it is
  given, measured) matches no record; a record written before its finalize also names the SHA-256 of the manifest the
  catalog judged, which the backend lands unchanged (measured), and the reconciler signs only when that manifest is the
  one committed there, so a writer past every door that lands an in-flight commit's version first under its uuid matches
  nothing. The emitter's rule decides which (`DoorTable.announces`, `provenance_namespace.announcer`): a provenance row
  write keeps none. Written once (one held for another caller or
  ref refuses `version/create` 400 code 13, as does a staged commit naming no transaction or a non-canonical uuid) and
  ended with the table by `end_object_records`: `commit_stamped` and `version/create` (every stock commit, one naming
  itself as `rask.author` included) before the finalize, an attempt whose finalize did not land deleting its record (at
  `version/create` only the request that created it, and only when the version it was staged for does not hold the
  manifest it judged, which a finalize whose answer was lost landed); the
  create door's write and an unkeyed `/insert`, which Lance commits itself and whose transaction uuid it mints inside the
  commit, just after it lands, under the transaction `committed_files.record_landed` read (`commit_authors.record_landed`,
  owner 2026-11-01), a failure logged `commit_author_unrecorded` at ERROR and counted on
  `catalog.commit_authors.unrecorded`.
- **The commit is the outbox** ([[LH-497]]). A versioned announcement's OpenLineage run id is a UUIDv8 of HMAC-SHA256
  under `commit-id-key` (a secret only the catalog reads; a stack with no store derives under the empty key) over the
  table's uuid, the ref, its incarnation and the version (`catalog.core.commit_ids`), and its `eventTime` is the
  commit's own time, so every announcement of one commit is one run on lineage's feed. Keyed because lineage binds a
  run id to its first author for ever (LIN-5) and any writer may post any run id: a predictable id would let one
  table's writer suppress another's announcements. The `catalog-commit-reconcile-cron` tick
  (`catalog.api.commit_reconcile`) lists every held location claim whose name still describes to it, reads each ref's
  versions after its mark (`_commit_marks/`) older than `LANCE_COMMIT_RECONCILE_GRACE_SECONDS`, looks each up on
  `provenance$events` by the table's uuid, ref and version (so a renamed table is not announced again), and announces
  every one the feed lacks from the version log alone, signed for the author the catalog recorded authorizing the commit,
  never for the `rask.author` a version carries, which any writer that can put a final manifest can stamp, so lineage
  records it on the catalog's signature as it records the original ([[LH-551]], [[LH-550]]); each find counts
  `catalog.commit_reconcile.reannounced`, and a version of an announced operation the catalog recorded no author for at
  that version, or whose transaction cannot be read (`commit_reconcile_unidentified`), counts
  `catalog.commit_reconcile.unauthored` (both page, `deploy/alerting/rules.yml`). A mark moves only past versions
  confirmed on the feed, unannounced by design (a `Clone`, a config-only disarm, a provenance row write) or unauthored,
  and only once each confirmed version's author record is deleted: a delete the store refuses holds the mark below it.
  A registered table's first mark is its register's ([[LH-550]]): the register door mints the table's uuid, marks each
  ref at its head (`commit_reconcile.mark_registered_heads`, 503 code 17 when a head cannot be read; a dataset pylance
  refuses over its feature flags is left to the judge, which refuses it, 400 for flag 256 and 503 for any other) and
  only then takes the location's claim, which is what makes a table one the tick reads. The versions a register attaches
  were committed past every door, so none is announced or counted, whatever `rask.author` it carries; its
  `register_table` marker is their announcement. A refused or undone register ends the records under that uuid.
  The catalog refuses an announced write while it holds no commit id key (`api/signing_gate.py`). A publication commits
  no version, so the same tick reads it off the table's `published` tag ([[LH-552]], ADR 0188): the publish and approve
  doors record the mover right before an advancing move (`commit_records.record_tag_move`, under the table's
  `_commit_authors/<uuid>/tags/published/`), the run id is derived from the table's uuid, the version and the tag
  file's `updatedAt` (`commit_ids.tag_move_run_id`, since an erasure can drop the tag and a version be published again),
  and a move the feed holds no run for is announced for that mover; one with no recorded mover is counted unauthored once
  (the `tag:published` mark, keyed by the move). A table's lifecycle markers (declare, register, rename (one `RENAME`
  dataset change naming the old id as `previousIdentifier`, ADR 0191),
  drop, deregister) stay best-effort, as in Lakekeeper and Polaris. Pinned by
  `tests/integration/test_a_commit_whose_announcement_was_lost_is_announced_once.py`.

## Storage — what state lives where (and why there is NO app database)

| State | Store |
| --- | --- |
| table data + versions | Lance datasets on object storage (rustfs) |
| project registry (`_projects/<id>.json`), warehouse registry + namespace bindings | JSON records on the control root. **Id-MINTING creates are conditional** (`If-None-Match: *` via `lakehouse.records.create_json`): project mint, warehouse mint and the write-once bindings are store-arbitrated, a lost race surfaces as 409, never last-writer-wins. **Mutable-field read-modify-writes are CONDITIONAL too** as of 2026-08-15 — `warehouses.upsert_warehouse` / `set_warehouse_status` / `projects.upsert_project` go through `records.mutate_json` (read + ETag-guarded replace, bounded retry), so an idempotent re-POST can no longer silently revert a concurrent quarantine or protection change. `put_warehouse` / `put_project` remain as SEEDING primitives with no production caller, pinned by `tests/unit/test_registry_writes_are_conditional.py`. Protection and trash records are still plain overwrites and correctly so: they carry no read-modify-write, so there is no carried-forward field to lose. |
| authz | OpenFGA on its chart-managed Postgres |
| lineage | Lance tables in the catalog's provenance namespace (`services.lineage.provenanceNamespace`, default `provenance`): the commit feed `events`, the table-level edge list `edges`, outbox `refusals` and forwarder `cursors`. The catalog creates the namespace at boot (`catalog/services/provenance_namespace.py`), protects it, grants each `LANCE_PROVENANCE_WRITERS` subject `writer`, revokes a `writer` the settings no longer name from one of the release's own accounts (`LANCE_OWN_ACCOUNTS`, the chart's `lance.ownAccounts`, the list the bootstrap hook converges), and leaves every other grant there to the doors: a bring-your-own compaction job's account is granted `reader` and `maintainer` at the namespace's access door, and no boot revokes it, nor a `writer` granted there to an account outside the release ([[LH-563]]). It writes its keep-last-N maintenance policy (`LANCE_PROVENANCE_RETAIN_VERSIONS`) and removes the one it wrote, while it stands as written, once none is declared, and announces no write under it but a compaction's, after which maintenance's write-event lane holds the table's pass, which expires it (ADR 0174, [[LH-529]]). Lineage writes through `merge_insert` and reads through `query` as its own principal; the graph is bring-your-own, fed by lineage's forwarder (ADR 0158) |

A relational app-DB was removed at P7a and must not creep back for the catalog: registry writes are
admin-frequency, the conditional creates arbitrate the id-mint races, and deletes are bottom-up
single-object operations by design — there is no multi-object transaction to need one. The moment
that changes (atomic cross-object invariants, high-frequency filtered listings), it is a design
decision, not a default. The conditional-put primitive is proven live by TWO `cas`-marker e2e
suites: `test_object_store_cas_e2e.py` (Lance's own manifest commits) and
`test_registry_cas_e2e.py` (the registry seam, contended 8-way).

## Lifecycle

Reclamation, scheduling and the GC design live in `services/maintenance` itself — read
`services/maintenance/src/maintenance/services/{queue,tasks,table_pass,orphan_check,orphans,floor,expiry,arrival,store_events,committed_files,due,plan,trash_task,audit,purge,reconcile,index_health}.py`
and ADR 0198 with its amendments — plus `packages/lakehouse/src/lakehouse/clone_sources.py`, the tag a
registered shallow clone holds on the source version it reads (ADR 0198 §1), which lives in the `lakehouse` library
beside `base_registry`, whose `forget_base_record` releases that tag wherever a clone's base record is forgotten
(`trash.end` included), and `base_refs.py`, the one spelling rule every reader comparing two locations asks. Read them
before changing anything a table's pass, a table's orphan check, the audit, the scan or the reconciler touches. The
per-table/namespace/project POLICY that governs a table's pass is
`catalog.schemas.PolicyRequest` (retention, retain_versions, compact_enabled, interval, target rows),
resolved **winner-takes-all** — the table's own record shadows the nearest namespace's which shadows the
project record, and the winner supplies EVERY field. Maintenance matches a table and its ancestor namespaces by the
uuids `__manifest` and the location claims record (`maintenance.services.naming.policy_owners`); a table whose uuid, or
an ancestor's, cannot be read is refused (gate `unidentified`), never maintained under the defaults, since the record it
cannot find may be a legal hold. Every reader of a setting parses the record's settings strictly where it reads it, through
`lakehouse.maintenance_policies.PolicySettings` (the door's types and bounds; `"false"` reads false, `"30d"` refuses), since
a record can reach the store past the typed door: a record that is there and cannot be read as a policy
(`MalformedPolicyError`) PARKS the table (gate `policy_unreadable`, counted on `compaction.tables.parked`, which
`MaintenanceTableParked` pages on) until it is written again, and a policy read the store did not answer decides nothing
and is retried after `retrySeconds`. Any surface showing an effective policy must say
which record won; an inherited value rendered identically to a set one is how nobody can tell what is
governing their data. The project-scoped surface is the console's project page, `frontend/console/src/routes/projects/[project]/+page.svelte`, § Maintenance.

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
  object with a live trash record, whose records and grants stay for its undrop. Of the tables it listed it ends only
  those it removed itself, each under its claim's fence ([[LH-586]], "A cascade removes each table as that door does"
  above): one a rename carried out before its turn keeps its grants. The namespace door's destroyed cascade
  (`_destroy_subtree`, a generator) announces each table to lineage as it goes and ends the subtree through the same
  walk, as do its Restrict, Skip and Overwrite paths. A part-way failure answers 503 `PartiallyApplied` naming
  `namespaces_dropped` (nested ones included), `tables_removed`, `trash_ended` and `tuples_revoked`, which counts each
  revoke as it lands and a partly revoked object's removed tuples through `fga.RevokeInterrupted`; re-issuing the call
  finishes it. A guard's refusal (a spec error answered 4xx, `endpoints.warehouses._a_refusal`) raised before any step
  counted answers as itself, since a guard refuses before its step writes; a store's or OpenFGA's failure is partial even
  with nothing counted, since the step it stopped may have landed ([[LH-572]]). A destroyed table's base record and location claim are keyed by its location, which nothing names once
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
  run FGA off). **The deadline ends the recovery** ([[LH-228]], ADR 0146): past `expires_at` both undrops refuse
  409, and maintenance asks the catalog's release door for the record (`POST /management/v1/trash/{kind}/{uuid}/release`,
  `maintenance.services.trash_release`, ungated by the drift report and by `trashPurge`), which revokes the object's
  tuples, announces a `grant_revoked` per principal (origin `trash_expired`) and stamps the record `released_at`, 15
  minutes after the deadline (`trash.RELEASE_SETTLE`) so an undrop admitted at it can finish. The trashed object's own
  task asks it, held by a schedule until then (`maintenance.services.trash_task`, queued by the drop's announcement), and
  every audit and scan asks it for any record its task has not released; a record whose object stands again
  (`purge.still_registered`) or whose location lies under no root that step read keeps its grants. The bytes wait for
  the purge. A recoverable drop answers `DropTableResponse.transaction_id` as `<table id>$<its uuid>`, and a
  recoverable cascade `DropNamespaceResponse.transaction_id` with one such id per table it detached; DescribeTransaction
  answers one from the trash record (`Queued` inside the window, `Running` past it), asked on the trashed table's own
  grants (`Resolver.trashed_as_transaction`). **The grace period is per warehouse**: `POST /management/v1/warehouses`'
  `delete_profile` (`{"mode": "hard"}`, `{"mode": "soft", "grace_days": n}`, or `{"mode": "estate"}` to clear it; kept
  as `delete_mode`/`trash_grace_days` on the record, carried forward by a re-POST naming none) decides a drop under any namespace bound to it, else
  `LANCE_TRASH_GRACE_DAYS` (`warehouses.grace_days_under`, read at the drop and stamped on the record). Pinned by
  `tests/integration/test_a_drop_is_kept_as_its_warehouse_says_until_its_deadline_releases_it.py`. `undrop` re-registers with a
  **RELATIVE** location: `register_table` refuses the absolute URI `describe_table` had just reported and keeps a
  relative one as the literal key it is given, so the undrop passes the key the recorded URL decodes to under the
  connection's root (`catalog.services.trash_doors.relative_location`, [[LH-517]]), where a registered `räksmörgås`'s
  bytes are. **A trash record is keyed by its object's uuid** ([[LH-525]], ADR 0173 §9): the name is free the moment
  its object is dropped, so a table or namespace created there is another uuid and nothing blocks the create. The
  undrop and tasks doors stay addressed by name: `trash.named` finds every record dropped under it, `?uuid=` names
  one, and the router asks the door's rung on each candidate (`fga_deps._require_trashed`), so a door acts only on,
  and lists only, what its caller holds a grant on. Several candidates answer the undrop 409 (code 19) listing each
  uuid and deadline; a live object of another uuid holding the name answers 409 too. A caller who lost the name finds
  the drop on `GET /management/v1/warehouses/{id}/trash` ([[LH-556]]): the route asks `can_get_metadata` on the
  warehouse, its candidates are `warehouses.trash_owned_by`'s, each is judged on its own uuid
  (`fga_deps.judged_page`), `?namespace=` keeps what was dropped beneath a name, and it pages on name then uuid
  (`trash_doors.listing_key`); every trash door answers `trash_doors.entry`, `expired` read by `trash.past_deadline`.
  Pinned by `services/catalog/tests/test_a_warehouses_trash_lists_what_its_caller_may_see.py`. A record keeps `uuid`,
  `namespace_uuid` (its namespace's at the drop) and, when a cascade filed it, `cascade` (the dropped namespace's
  uuid). A new table's claim never takes a location the trash keeps for its name's dropped table
  (`table_claims.kept_in_trash`). The purge judges a table record live by its location and a namespace record by its
  uuid, never by its name, so it reclaims a trashed table while a table created since under the name stands. Pinned
  by `services/catalog/tests/test_an_undrop_restores_the_trashed_table_its_caller_names.py` and
  `tests/integration/test_maintenance_finds_a_table_under_the_spelling_the_catalog_records.py`. A table's pass never
  touches a trashed table (`table_pass.run_table_pass` ends on `trash.keeps`, and the expiry reads it again once it holds the table's history fence, `expiry._dropped_since`; the drop door takes no fence, so a drop after that read is followed by one cleanup under the table's own policy); within maintenance only the daily scan's
  purge deletes its bytes, opt-in (`trashPurge`, off in the chart) and gated on that scan's own drift report (`purge.report_is_clean`),
  and `trashPurgeDryRun` reports what it would purge (`would_purge`).
  COVERAGE.md's old "soft-delete is N/A, time-travel replaces it"
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
  their registry records. A warehouse's is set and cleared through `POST /management/v1/warehouses/{warehouse_id}/protection`
  on its project's `can_administer`, a denial answered the missing warehouse's 404, and recorded beside `protected_by`;
  the create door applies a `protected` it names and carries it forward when it names none ([[LH-548]],
  `services/catalog/tests/test_a_warehouses_protection_is_set_and_cleared_by_its_project_admin.py`). A project's is set
  and cleared through `POST /management/v1/projects/{project_id}/protection` on the project's own `can_administer`, the
  gate its delete shares (`administered_project`), a denial answered the missing project's 404; its reads answer
  `protected` and `protected_by`, and a re-POST of the create carries it forward ([[LH-549]],
  `services/catalog/tests/test_a_projects_protection_is_set_and_cleared_by_its_admins.py`). Both registries apply one
  rule, `control_records.with_protection`: arming an armed record keeps its armer, and clearing removes both fields.
  Tables/namespaces carry it as a **control-root `_protection/` record**
  (`lakehouse.protection`) gating drop/deregister/rename and both Overwrite modes,
  `create?mode=Overwrite` over an existing table and `insert?mode=overwrite` on any branch ([[LH-242]]),
  (table) and drop (namespace). It guards no history reclaim (ADR 0150, [[LH-300]]): `version/delete`, `tags/delete`,
  `branches/delete` and `maintenance/run` are governed by authorization alone, since deleting a tag or a branch is what
  makes its versions reclaimable, and a version that must survive is tagged
  (`tests/integration/test_protection_guards_deletion_not_history_reclaim.py`). The record is
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
  ADR 0173 §8): protection and maintenance policy (`lakehouse.object_records.OBJECT_RECORD_PREFIXES`), a
  promotion hold, maintenance's due record, an erasure obligation and its release (each naming `uuid`/`table_uuid`),
  maintenance's policy cadence stamps and its copy of the table's commit records (`_maintenance_files/`, which
  `file_identities.end` deletes with the records). A door resolves the uuid from the name it is addressed by
  (`ResolvedTable.require_uuid`, `fga_deps.identify`) and refuses `InternalError` for an object that records none,
  never reading it as unprotected. A rename moves no record and no grant, only its data directories' claims (and, into
  another namespace, its parent edge); a table created later under a dropped name holds a new uuid and inherits nothing,
  even where an ending was lost. Every door that ends an object (drop, deregister, a cascade, the trash purge) ends that
  list through `object_records.end_object_records`, under the uuids it read before the object went. The hold, due,
  erasure and project-policy listings name each table as it is named now (`table_claims.names_now`: its location's
  claim's holder while the claim carries the record's uuid) and filter it under that name; a record whose table is gone
  names no table and is listed to no one, since the name it kept may belong to a new table now. A door resolves its
  table once and opens the location it resolved (`table_claims.DoorTable`), so a uuid and a location never come
  from two resolutions. Pinned by
  `tests/integration/test_a_rename_carries_what_the_table_holds.py`.
  A destructive CASCADE destroys its children INSIDE one native call, so they never re-enter this
  door — `drop_namespace` therefore enumerates `_collect_descendants` whenever `behavior=cascade`
  (no longer gated on `fga_enabled`) and protection-checks EVERY enumerated id before anything
  drops, refusing 409 and NAMING the protected descendant; `force` turns the subtree lock exactly
  as at the named rung, and on the force path the descendants' protection records are cleared after
  the drop, as the named rung clears its own (each is keyed by its object's uuid, which no later object holds,
  [[LH-523]]). Until that landed the docstring promised
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
  binding you cannot read is a namespace you cannot see. The warehouse registry and the trash have the same pair
  ([[LH-572]]): `warehouses.every_warehouse` refuses while a record cannot be read (a project delete, the purge's
  sibling guard, a project policy set) where `list_warehouses` skips it, and `trash.every_record` raises where
  `trash.list_all` skips (`trash.named`, an undrop restoring its cascade, `warehouses.trash_owned_by`, a cascade's
  `fga_deps._trashed_objects`). A record you cannot read may be the one the decision is about. Every such refusal tells
  an outage from a record (`lakehouse.outage.store_refusal`): a store that did not answer is 503 code 17, which a retry
  may clear; a record that is there and cannot be read as one is 500 code 18, which only writing it again clears. The
  registry reader raises the outage itself, so even a tolerant listing never reads one as a corrupt record.
- `deactivate` = offboarding step one (quarantine; resolver 403s bound namespaces).
- Maintenance lives in **`services/maintenance`** and DECIDES; it rewrites no data (ADR 0162, [[LH-467]]). It runs
  version expiry (`cleanup_old_versions`, tags EXEMPT), trash purge, each table's orphan check and cross-store reconcile, and
  writes the DUE LIST: one record per table that needs compacting or an index optimize, under `_maintenance_due/` on
  the control root (`lakehouse.maintenance_due`). The catalog lists the records on `GET /management/v1/maintenance/due` to
  a caller holding `can_maintain`; the list is advisory. **Maintenance is driven by commits** (ADR 0198 and its
  2026-11-09 amendment): a table is passed when it changes or its time comes, never on a whole-estate clock. Its work is tasks on a NATS JetStream
  work queue (`maintenance.services.queue`): one per subject on `MAINTENANCE_TASKS` (`maintenance.task.<kind>.<uuid>`; a
  guarded publish, `Nats-Expected-Last-Subject-Sequence: 0`, refuses a second while one is queued or in flight), and its
  times are NATS message schedules on `MAINTENANCE_SCHEDULES`. The worker is a process of
  its own (`python -m maintenance.worker`) in a Deployment of its own (`-maintenance-worker`, two replicas, RollingUpdate,
  `chart/templates/maintenance.yaml`), under the same identity as the web pods, which serve the write-event lane and run
  no task; its sidecar health-checks its probe port, so it outlives the drain and leaves with the worker. Every worker
  replica runs a pull worker on the chart's durable consumers, as its own bus user
  (`maintenance-worker`) through nats-py rather than Dapr, whose JetStream subscription sends no in-progress ack and whose
  publish cannot set a schedule header (`maintenance.services.tasks`). It takes one task at a time and heartbeats it
  every `maintenance.queue.heartbeatSeconds`, so a killed replica's task goes to another after `ackWaitSeconds`; a replica
  whose heartbeats the bus stops answering ends itself before that ack wait runs out (`Worker._tell`), as does one whose heartbeat ends by an error nothing expected (`Worker._keep_alive`), one whose drain
  ends with a pass still running ends there (`Worker.drain`), each pull has a subscription of its own, and a pass
  holds a claim on its table in the MAINTENANCE_CLAIMS bucket that another pass waits out (`Worker._claim`), so two
  passes of one table never overlap; a replica whose credential the bus takes no longer (revoked when its grants changed,
  or expired) ends itself and starts again on the store's current one (`TaskQueue.connect`); a pass
  past twice `maxPassSeconds` ends its replica, and a task that outlived its worker on all `maxDeliver` deliveries is
  ended unrun on the one more the consumer allows. The worker counts each task retried, ended, completed (and its time
  from claim to ack), each store event by how it settled, and each fired schedule the queue refused, and samples every
  minute each stream's depth and oldest message's age and each durable's ack-pending and redelivered counts
  (`maintenance.services.queue.sample_queue`, `maintenance.queue.*`), which the `maintenance` alert group reads. With
  store events expected, the hourly audit also reads each governed bucket's event configuration and reports each
  sending no `s3:ObjectCreated:*` event, counting each bucket once per audit on `maintenance.store_event_buckets.checked` by
  outcome (`configured`, `without`, `unknown`; `maintenance.services.bucket_events`, alerts `MaintenanceBucketWithoutStoreEvents`
  and `MaintenanceBucketStoreEventsUnknown`). A commit's announcement on `lineage.events.v1` (`/maintenance-arrival`,
  `maintenance.services.arrival.lane_task`) holds one follow-up pass `commitPassDelaySeconds` (300) out unless one is
  held already, so a burst of commits makes one pass; a table's drop (`drop_table`) queues its trash task instead. A table's pass
  (`maintenance.services.table_pass.run_table_pass`) expires the table and each branch once it has
  `minVersionsToExpire` (20) reclaimable versions, refreshes its due record, reads its committed files (below), and
  schedules its next pass (`next_due`: the earliest a version or a staged manifest comes due, held to the policy's
  cadence, never later than `recheckHours` (24) and never sooner than 60 s; a pass that could not decide, or found the
  table's history fenced, is tried again after `retrySeconds`). The hourly audit opens no table and queues each table
  and trash record that holds neither a schedule nor a queued task, which bounds a lost announcement or schedule to one
  firing (`maintenance.services.audit.run_audit`); a trash record already released, or carrying no deadline, gets no task,
  which would end holding nothing. The chart's stream Job reads MAINTENANCE_SCHEDULES back and fails unless it allows
  message schedules, which nats-server older than 2.12 drops without a word. A table's pass also deletes each `_versions/<n>.manifest-<uuid>` a stock writer staged for a version the table already holds, once past `MAINTENANCE_STAGED_MANIFEST_GRACE_SECONDS` (600): a commit that lost its version leaves one, Lance's listing drops the name so `cleanup_old_versions` never does, and the write tier grants no delete ([[LH-529]]).
  The pass's pre-vend probe and its next-pass time count what a pass would reclaim with Lance's own `explain_cleanup_old_versions`, under the arguments the pass's `cleanup_old_versions` takes, the time asking with the age a version will then have (`table_pass.versions_due_at`, [[LH-562]]); the catalog's GC preview names the versions by `lakehouse.version_retention`, since Lance's explanation counts them only.
  Compaction, index builds and index optimize are a bring-your-own job's, committed through `version/create` (below);
  `CreateTableIndex` answers 406 naming that path. An erasure's rewrite is a bring-your-own job's too (ADR 0163,
  [[LH-493]], below). The maintain vend tier goes to the maintenance identity only (LH-473, #599).
  `catalog/api/maintenance_mode.py` is read-only maintenance MODE (503 + Retry-After), not this. A table's version
  reclamation has ONE owner, maintenance's expiry (each root's `__manifest` is the catalog's, below): Lance's commit-path auto-cleanup (`lance.auto_cleanup.*`) deletes inside
  whichever writer commits, past a hold and a protected base. No door arms it (the property guard refuses the keys),
  `version/create` refuses a commit that does, every committing catalog door disarms the ref first
  (`core/namespace.disarm_commit_path_cleanup`; the erasure door every ref, so the job's commits are admitted), the
  restore door refuses an armed version, and the
  expiry pass deletes every such key before its refusal gates, as the backstop for a table nobody commits on (LH-245).
  A table's compaction plan has ONE source too, its maintenance policy: Lance plans from `lance.compaction.*` manifest
  config whenever a plan names no option (`lance_docs/lance/format/table/table_config.md`), so the property guard
  (`core/formats.compaction_config_keys`), `version/create`, the register judge (`table_bases.judge_registered_table`)
  and the restore door (`dataplane.refuse_a_restore_carrying_refused_config`) refuse every such key ([[LH-538]]).
- **Maintenance does not read a tier.** The due list plans a compaction at the #50 policy's
  `target_rows_per_fragment`, else Lance's own sizing. There are no per-tier defaults: a tier is the
  namespace's `rask.tier` property, which a dataset path does not carry, and a size guessed from a path is
  applied silently forever (ADR 0160).
- The reconciler reports cross-store drift and deletes nothing until its report runs clean. It runs as two tasks of
  its own (`maintenance.services.audit`), each a NATS message schedule the worker asserts at its start and every 15
  minutes, so a purged schedules stream heals: the hourly AUDIT (`maintenance.queue.auditSchedule`, `@hourly`) walks
  no storage, so it opens no table and lists no bucket, and the daily SCAN (`scanSchedule`, `0 30 0 * * *`, a six-field
  cron: nats-server 2.15.2 refuses a five-field one) adds the storage walk (discovery: `unregistered_datasets`,
  `absent_datasets` and the walk's own `incomplete` notes), then the trash purge that report certifies. The purge's gate
  (`purge.report_is_clean`) is the drift report alone (owner, 2026-11-10: "Drift report only"): no gating finding, no
  category unavailable, no source incomplete, and storage walked (the hourly audit's report is refused). What is inside a live table, its unreferenced files, is no part of it:
  it changes nothing about which bytes the purge deletes. The audit also holds an orphan check, a jittered time within
  a day out, for every live table holding neither a check's schedule nor a queued check. Each runs once per firing, on one replica
  whatever the replica count: its schedule fires on a subject of its own, and the queue holds one task per subject. The
  two share one claim (`queue._claim_key`, `reconcile`), since each repairs the estate from a report of its own: they
  never run at once, and the scan fires off the hour so it rarely waits. Maintenance has no Dapr cron binding; the
  chart's six are the catalog's and lineage's relay (`chart/templates/dapr-component.yaml`).
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
  grants and history intact, recorded as `overwrite_table` with lifecycle state `OVERWRITE` and announced on no
  control action, since no table came into being; `insert?mode=overwrite` keeps the schema and so stays at the writer
  rung, ADR 0099, [[LH-415]]); the
  platform writes them with pylance directly, and a client creating from an upstream's schema strips its
  schema metadata first. `description` is the one user-facing reserved key (the
  console renders it under the table name); everything else in that map is opaque user data.
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
  `?` under a re-encoded name, [[LH-435]]; every judge asks the same rule, [[LH-510]]); a segment holding IAM
  policy syntax once decoded (`vending.IAM_POLICY_SYNTAX`: `*` and `?`, which IAM reads as wildcards, and `${`, a
  policy variable, in every policy a credential for the table is scoped by, [[LH-437]], [[LH-526]]); a segment
  beginning with `_` once decoded; one equal to or containing the control root; and one overlapping a configured
  base ([[LH-204]]).
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
  the 409 path writes no tuple ([[LH-586]]); it converges the table's base record, never for a flag-256 dataset or one
  declaring a base nothing sanctions, and a refusal
  whose detach fails is a 503 `PartiallyApplied` naming `attached: true`. A registration whose claim another request
  holds fenced answers 409 code 14, never the occupied-location 400: the same call is judged afresh once that
  request has ended. Only
  `main` is judged. Pinned by `tests/unit/test_a_register_refuses_a_table_that_mixes_file_versions.py`
  (flag 256) and `tests/integration/test_a_planted_base_is_refused_and_freezes_nothing.py` (spelling,
  overlaps and bases).
- **An external blob base is per table, on request, and never vended** ([[LH-209]]). A create registers
  one only when the request names it in the door's `external_blob_base` query parameter (never a
  schema or property key, which LH-208 refuses), inside a `LANCE_EXTERNAL_BLOB_BASES` entry and outside every governed root and bucket (the catalog
  and control roots, reserved and warehouse-claimed buckets:
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
  (`lakehouse.table_layout.WRITER_PUT_GLOBS`: `data/`, `_transactions/`, `_deletions/`, `_indices/` and a
  staged `_versions/<n>.manifest-<uuid>`) and an explicit Deny on every final manifest under it at any depth
  (`*.manifest`); nothing under `_refs/`, no version hint, no delete. The table's own write credential also puts
  under `tree/*` ([[LH-490]], ADR 0164): a stock client opens a branch with the table's `DescribeTable` and stages
  the branch's commits with it, and `can_write_data` writes every branch. Measured on MinIO
  `RELEASE.2025-04-22T22-12-26Z`, that grant leaves a write credential fitting a table prefix of 89 characters at a
  63-character bucket, inside a 2,048-byte session policy; RustFS enforces the same 2,048-byte limit, counted after it
  re-serialises the policy, and its boundary is unmeasured (measured 2026-10-18; `rask-rustfs` §2). The catalog sends the policy as compact JSON and refuses one past 2,048 characters before any exchange, 500 code 18 titled `SessionPolicyTooLong` with its length and the limit as extension members; with `vending.encryption` naming an SSE-KMS key (its key ARN, refused at boot otherwise) every tier's session may `kms:Decrypt` with that key, and write and maintain also `kms:GenerateDataKey`, only through S3 (`kms:ViaService`). The warehouse create door's probe then writes and reads back an object encrypted with it (`encrypted_write`), so a store whose role holds no grant on the key is refused 400 code 13 ([[LH-262]]). The
  Deny is load-bearing, measured on MinIO (LH-469, #594) and enforced by RustFS's Deny-first evaluation (R3): the
  staged Allow also matches
  `<n>.manifest-<uuid>.manifest`, which pylance reads as a final manifest, and `tree/*` reaches every branch's
  `_versions/`. The catalog finalizes what the writer staged (`version/create`, below). `maintain` (`can_maintain`) gets put/delete on the whole prefix for compaction and
  cleanup except an explicit Deny of put/delete on `<prefix>/_refs/*` (`lakehouse.table_layout.REFS_GLOB`), plus, on
  the control root, the table's history fence (get/put) and its committed-file records `_file_identities/<uuid>/*`
  (get/delete) (`vending.TableRecords`, [[LH-219]]), and get/delete on the table's own data directories, those of its declared bases its base record holds as
  `data` entries (`vending.VendableBases`, [[LH-286]]); every other base is read-only at every tier. It is also the
  shape for the warehouse scope probe. The management vend door checks the one rung its
  tier names, never either. A put over an existing key is the residue no session policy can refuse, so it is DETECTED
  ([[LH-419]], ADR 0184): every catalog commit records each file it adds under the table's root (data files, blob sidecars,
  deletion files, index files and its transaction file), with the ETag and size the store answers, before its version is
  announced (`lakehouse.file_identities`, `_file_identities/<table
  uuid>/<transaction uuid>.json`, written by `version/create` before it judges a stock commit's files, by
  `commit_stamped` before its finalize, and by the create door and an unkeyed `/insert`, which Lance commits itself,
  just after it lands, logging `committed_files_unrecorded` at ERROR and counting `catalog.committed_files.unrecorded`,
  which warns, when that write fails), and each pass of the table reads every recorded file again against one listing
  of the table's root (`maintenance.services.committed_files`, `lakehouse.stored_objects.listed_identities`: a request per
  thousand objects rather than a HEAD per file), reading only the records written since its last through maintenance's
  own copy (`file_identities.recorded_and_copied`, `_maintenance_files/<table uuid>.json`). The store's event of a write
  under the table (on `store.events`, which the store's operator configures per bucket, `objectStore.events`) holds
  that pass `commitPassDelaySeconds` out (`maintenance.services.store_events`, ADR 0198 decision 3); a delete is not
  acted on, and the table's daily recheck backs up an event that never arrives. A recorded file
  held with another ETag or size, or gone while a retained version names it, is named in `overwritten_files`, logged
  `committed_file_overwritten` and counted `maintenance.files.overwritten`, which pages (`CommittedFileOverwritten`). A
  record is forgotten only once no retained version names its files and the store holds none of them. The platform store keeps object versions to restore it (`rask-rustfs` § 8). Prevention
  waits on upstream: stock pylance sends `If-None-Match` only on manifests, and object_store has no conditional
  multipart. A file named through a base is not recorded, since no vend puts there, and a registered table's files are
  recorded only from its first commit through the catalog.
  **Each vend is a session of its own** ([[XC-083]], ADR 0170): the catalog exchanges its OWN projected ServiceAccount
  token through STS `AssumeRoleWithWebIdentity` with that session policy, named for the verified caller
  (`vending.role_session_name`), and a refused or unanswered exchange answers 503 code 17 (`vending.vend_or_refuse`).
  The catalog holds no store key: its own IO signs with an hour's session of the same token, renewed at half life
  (`lakehouse.store_session`), so a running catalog never needs a restart for its credential.
  **The spec doors vend the highest tier FGA grants the caller** (ADR 0156 C3, `fga_deps.spec_vend_tier`):
  `DescribeTable` and `DeclareTable` vend unless the request sets `vend_credentials` false (unset, the spec lets the
  server decide), write on `can_write_data` (judged by the tier judge) or on `can_read_data` with `can_maintain` (a
  compaction job's rungs, whose commits `version/create` admits for a compaction alone, ADR 0174, and whose puts can
  overwrite a committed data file as a writer's can), read on `can_read_data`, otherwise none.
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
  not vended the tier. The management door refuses the tier 409 code 19 for a table whose maintenance policy keeps its
  whole history (`cleanup_enabled` false, `lakehouse.maintenance_policies.keeps_whole_history`, gate
  `require_reclaimable_history` in `api/v1/endpoints/credentials.py`) or is there and cannot be read as a policy (it may
  be the hold, and no retry reads it otherwise), and 503 code 17 when the store did not answer the policy read; and
  maintenance's probe asks no vend for such a table.
  **The trash is the catalog's to end, and maintenance deletes only a due record's bytes** ([[XC-084]], ADR 0197).
  Maintenance's own canned policy writes only its due list, its copy of the commit records (`_maintenance_files/`),
  policy state and lineage outbox (`lance.objectStorePolicy.maintenance`, `chart/templates/_helpers.tpl`), so it asks four doors
  under `/management/v1/trash/{kind}/{uuid}` (`api/v1/endpoints/trash_reclaim.py`, judgement
  `services/trash_reclaim.py`, client `maintenance.services.credentials`), each asking `can_reclaim_trash` on the
  estate root, which only the `reclaimer` estate role opens (seeded for maintenance's ServiceAccount by the chart's
  bootstrap-admin hook, `lance.fgaEstateRoles`): `POST .../release` revokes the object's grants once the record is past
  its deadline by `trash.RELEASE_SETTLE`, announces a catalog-signed `grant_revoked` per person and stamps the record;
  `POST .../credentials` vends a released table record's bytes session (`vending.ReclaimScope`: list and delete over its
  location in both spellings and its data directories, nothing on the control root, a whole-bucket place refused, and
  none while a registered clone's tag stands on the table, `trash_reclaim.clone_refusal`, ADR 0198 §1);
  `DELETE ...` ends a released record once its place holds nothing (`lakehouse.trash.end`: the object's records, its
  base record and with it a clone's tags, its claim, then the record); `POST .../refusals` notes maintenance's own refusals. The release comes
  first, so the settle an undrop admitted at the deadline needs and the telling of each person who loses access
  precede any delete; maintenance's purge selects released records only (`purge.due_from`), and the daily scan releases
  before it purges (`audit.run_scan`), so a record the scan releases is purged and ended in the same run (measured on
  helm rev 25: without the order the purge deleted a drop's bytes and revoked its
  grants unannounced three minutes after its deadline). The catalog judges on reads no session writes: the path's kind
  and uuid, the record it filed, and the location's claim, which must record that uuid; a refusal is 409 code 19 and
  noted on the record (`trash.note_refusal`).
  **Maintenance holds no OpenFGA credential** (ADR 0197): its drift reconciler reads the grant census and asks for its
  repairs at `/management/v1/grants` (`GET /census`, `POST /ghosts/revoke`, `POST /tenancy/rebuild`;
  `api/v1/endpoints/grant_reconcile.py`, judgement `services/grant_reconcile.py`), each asking `can_reconcile_grants`,
  which only the `reconciler` estate role opens. The census leaves out the deployment's own objects, and the catalog
  judges each candidate again on its own reads.
- **A classified column is never vended raw** ([[LH-058]], [[LH-207]]). A label is field metadata `rask.classification`,
  set and cleared only through `update_field_metadata` under `can_classify` (plus `can_apply` on each value). Both vend
  doors (`/management/v1/table/{id}/credentials`, `DescribeTable(vend_credentials=true)`) answer server-mediated when
  `vending.classified_in_any_ref` finds a label on any field, nested ones included, on main or on any branch: a
  credential for any ref reads every ref's files (a stock client opens a branch with the table's credential; a branch
  reads its parent's fragments through a base, `lance_docs/lance/format/table/layout.md` § Shallow Clone). A label never leaves a
  field another way: `alter_columns` and `drop_columns` commit with `keeps_labels` (`dataplane.commit_stamped`), which
  refuses 400 a commit losing or changing a field's `rask.*` keys by Lance field id (a same-type re-type re-mints the id
  with no metadata, measured on pylance 13.0.0), as `version/create` already refuses for a stock commit; an
  `add_columns` or `update` expression that reads a classified column asks `can_classify`, Lance itself telling which
  columns it reads (`dataplane.expressions_reading_a_label` plans it with and without the labelled columns); and a
  create, through the door (on the parent namespace's `can_classify`) or a first `version/create` (on the table's),
  that carries a label asks what labelling asks. A vend whose table cannot be opened is refused, never read as
  unlabelled (`vending._open_vended_ref`, [[LH-572]]): an outage answers 503 (code 17) and a committed version that
  does not open 500 (code 18); only a table holding no committed version (`dataplane.holds_a_version`) has no labels.
  Pinned by `services/catalog/tests/test_a_classified_column_is_never_vended_raw.py`. A copy made outside the catalog by
  a writer who reads the column is LH-288's.
- **Stock clients commit through `version/create`** ([[LH-471]], ADR 0157). `DescribeTable` and `DeclareTable`
  answer `managed_versioning=true`, so pylance stages `_versions/<n>.manifest-<uuid>` (its `manifest_path` is the
  object-store path: the key, no scheme, no bucket) and asks `POST /v1/table/{id}/version/create`.
  `catalog.services.managed_versions` copies the staged bytes to a DETACHED slot `_versions/d<id>.manifest` with the
  manifest's own `version` field set to `id` (Lance refuses a manifest whose version is not the one asked for, and a
  create has no committed version to `checkout_version` from; the copy is a one-field wire edit of `Manifest.version =
  3`), opens it with pylance, judges it against the version it follows, deletes the copy, and finalizes through the
  `dir` backend's put-if-not-exists. A refusal leaves the table at its version; pylance reports it as an unknown
  outcome and keeps its staged manifest. A version another writer took since the writer staged it answers 409 code 14
  before it is judged (`managed_versions.VersionTakenError`, the spec's 409 for a version that exists) and hands the
  writer the ref's next turn ([[LH-259]]); a version beyond the next answers 400. A read the judgment needs that the store
  cannot serve answers 503 code 17, in Lance's dialect and pyarrow's alike (`lakehouse.outage`), never the 400
  code 13 a stock client drops as its own malformed commit; only a ref `dataplane.holds_a_version` finds holding no
  committed version follows version 0, and one whose latest committed version does not open answers 500 code 18
  ([[LH-545]], `lance_docs/ns_catalog/namespace/operations/errors.md`). The door admits a writer or a maintainer (`can_create_version`), and the judge
  asks the operation's own rung (`fga_deps.require_staged_operation`: the writer operations and a table's first version `can_write_data`, restore
  `can_restore`, rewrite `can_maintain`, an Overwrite after the first version `can_drop` plus protection,
  `Clone` and unknown operations refused); a create at 2.2 with stable row ids and no later change to either;
  reserved schema metadata, the provenance and key columns and field-level `rask.*` metadata unchanged; no
  `lance.auto_cleanup.*` and no change to `rask.*` config; feature flags the commit adds; the base list unchanged and
  sanctioned, and no added file through a base; a new fragment naming a held file, no file, or (on an Append) an
  overlay, a deletion file or row ids below the table's `next_row_id`; each added file well formed, present at its
  declared size and agreeing with its footer, at the table's file version ([[LH-211]], `client_fragments`); blob
  sidecars against the table's recorded external bases, every descriptor wherever its blob field lies beneath a Struct, List or LargeList ([[LH-539]]); the primary key across new rows and carried rows (`primary_keys.refuse_repeats_in_version`, which every catalog door's commit meets too in `dataplane.commit_stamped`, [[LH-540]]); a
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
  the door. The compaction gate (`lakehouse.compaction_gate`, shared with maintenance's due list) weighs
  only bases outside the table, because a branch reads its parent through a dataset-root base on the table's own
  root. Pinned by
  `services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py` and
  `tests/integration/test_a_forged_fragment_is_refused_and_the_table_is_unchanged.py`. `register` still judges file
  versions on its own door.
  **A compaction or an index commits here too** (ADR 0162). Lance's compaction protocol makes two commits,
  `ReserveFragments` (which may only advance `max_fragment_id`) then `Rewrite` (every row kept, by stable row id or
  by count; `require_compactable`), and both need `can_maintain` and no writer rung (ADR 0174): the `maintainer` rung,
  which an owner holds and the grant doors hand a job's own identity (`manage_grants`, or a maintainer holding
  `pass_grants`), with `reader` to read what it rewrites; a service account is never an owner (LH-433). The
  bring-your-own compaction job compacts each table the due list names it this way. A `CreateIndex` (a build or
  an `optimize_indices`) needs the writer rung, changes no data, and each new segment lives under the table's own
  `_indices/<uuid>/` with its declared files present and the index opening. A compaction of a table a registered clone
  reads is judged as any other: the clone's tag keeps the version it reads, whatever the compaction and the cleanup
  after it remove (ADR 0198 §1, `lakehouse.clone_sources`). The register door takes that tag,
  `rask.clone.<sha256 of the clone's location>`, on the source version the clone's first transaction (a shallow
  `Clone`) names, under the source's history fence (`table_bases.admit_clone`, `clone_sources.take`). It takes one on EVERY table the clone reads through: a clone of a clone reads the first source's files where they are (measured on pylance 13.0.0), so it tags that source too, at the version its own source's record pins, and the end of the clone between them releases only that clone's tag. A clone of a source branch names `<source>/tree/<branch>` and `Clone.ref_name`, and is tagged `(branch, version)` on the source table. Each `derived_from` entry of the clone's base record keeps the version and branch it pins, because the clone's own version expiry removes the transaction that says so: a later registration of it tags from the record, and a recorded root with no pin is refused (400), never admitted untagged. Its caller must
  hold `can_read_data` and `can_create_tag` on each source, asked in that order and in the order the clone reads through
  them (`table_bases.CLONE_RELATIONS`: the clone's owner reads the source's rows, and the model's `publisher`, who may
  tag, is no reader; owner 2026-11-09 on the tag rung: a reader would pin the owner's table), BEFORE the native register
  (`tables._require_clone_sources`), so a refused caller leaves no row, mark or claim: 403 code 15, and for a caller
  holding nothing on a source one sentence (`can_read_data required on <source root>`) whether a governed table is at
  the source's root or not, naming only that root ([[LH-436]]); 400 when the source no longer keeps that version, 409 code
  14 while a reclaim holds its history. Every door that ends the clone releases the tag BEFORE the location's claim
  (`base_registry.release_clone_sources`, `forget_base_record`), and keeps a tag the location's claim names another
  table for (a clone registered there since holds one of the same name); a deregister that cannot delete it answers
  503 PartiallyApplied with the location still held. While a tag stands, a destructive drop of the source refuses 409
  code 19 (`dataplane.require_no_clone_reading`), a destroying cascade removes each source after the clones it removes
  with it and refuses 409 code 19 while a clone outside it reads one of its tables (`clone_sources.destroy_order`), and
  the trash credentials door vends no session to delete a trashed source's bytes. **A failed end is finished by the
  record its door filed** (owner 2026-11-10, "Intent record"; ADR 0198's amendment of that day): each of the four doors
  ending a clone (deregister, a destructive drop, a namespace's destroying cascade, a warehouse cascade) files the
  clone's ending, one object per filing at `_clone_endings/<the claim's digest>/<filing token>.json`
  (`lakehouse.clone_endings`, `catalog.services.clone_endings`), naming its location, uuid, sources and whether its base
  record goes, BEFORE its row goes (a destructive drop and a destroying cascade through `destroy_table`'s
  `before_removal`, once every judgement has passed, so a refusal files none), and deletes it last; a door whose native
  call failed leaves it, since no describe tells the clone's row from a same-name registration's; a store that will not
  delete a tag leaves it standing, and maintenance's audit asks `GET
  /management/v1/clone-endings` and `POST /management/v1/clone-endings/{digest}/{filing}/finish` (rung
  `can_reclaim_trash`, as the trash doors; audited `clone_ending_finish`). The finish acts once a claim's `LEASE` has
  passed since the filing and no other finish's mark is younger, on the claim it read: held for another uuid, or for the
  clone standing there (its door never removed it, or is still at work), it releases nothing and leaves the ending (409
  code 19), removing it with nothing released only once the clone has stood a day past the filing (`STANDING_BOUND`);
  held for the clone gone, it FENCES the claim under that read's ETag (moved: 409 code 14), releases each source's tag,
  the base record where the bytes went and the fenced claim, and deletes the ending last. A registration at a location
  where an ending stands is refused 409 code 14 once it holds the claim (`clone_endings.refuse_registration_while_ending`),
  so no finish can release a new clone's tags. No request releases a claim another table holds: the register undo and
  the finish name the uuid they own (`location_claims.release(..., uuid=...)`), and so do both cascades, under their
  own fence, since a table's bytes can take longer to go than a fence stands ([[LH-586]]); the drop door and
  deregister release by name under their own fence, which refuses every other take while it stands; the create and
  declare undos act on a location named after a uuid they minted; data-directory claims record no uuid and are released
  by name by the table the location's claim names; `trash.end` releases a claim the trash record keeps from every other
  take. The finish forgets a base record only where the door meant the bytes to go and no dataset stands there. No reader judges a clone gone (owner 2026-11-09). Measured on pylance 13.0.0, Lance itself commits a rewrite that loses or duplicates rows and an
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
  branch pins (409, naming it), and judges every range before deleting any. The reclaim also removes data files only
  those versions referenced, so a version a registered clone reads is kept by the clone's tag (ADR 0198 §1): this door
  refuses it as it refuses any tagged version, `maintenance/run` exempts it as every cleanup exempts a tagged version,
  and the erasure's verification reports the tag as a `held` surface it never drops, which the clone's own end
  releases (`erasure._HELD_BY_A_CLONE`).
  **A ref written during a reclaim is held off by the table's history fence** ([[LH-308]], ADR 0176,
  `lakehouse.history_fences`). Lance reads tags and branches once per `cleanup_old_versions`
  (`cleanup.rs:472-511` at v13.0.0); measured on pylance 13.0.0, a tag created mid-cleanup named a deleted version in
  20 of 60 races and a branch could not be read in 9 of 60. So every reclaim (`maintenance/run`, `version/delete`, the
  erasure verification's step 3, maintenance's expiry pass while it deletes) and every ref write (`tags/create`,
  `tags/update`, `branches/create`, publication's tag move, the erasure release's branch re-creation) holds the fence,
  one record per table uuid under `_history_fences/`; holders of one kind share it, the other kind is refused 409 code
  14 (`DoorTable.history_held`), and a fenced expiry is `skipped="history_fenced"`, its table's pass tried again after
  `retrySeconds` (`table_pass._dataset_due_at`). A branch create (the create door,
  the erasure release's re-creation) holds the kind `branch`, which also excludes another branch create, so two creates
  of nesting names on one table take turns and each judges a listing no other create changes (ADR 0179, [[LH-413]]). A
  hold lapses after 15 minutes on the store's clock.
  Pinned by `services/catalog/tests/test_tag_and_branch_failures_carry_their_spec_code.py` and
  `tests/integration/test_no_reclaim_deletes_a_version_while_a_ref_is_written.py`.
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
- **An erasure is held until every table derived from its table holds an obligation following it** ([[LH-332]],
  ADR 0178, ADR 0183). A table is derived when a run writing it declared the source, or a table derived from it. The
  verification's step 5 (`catalog.services.erasure_derived`) walks the catalog's input record (`table_inputs`, written
  before each declaring commit, so a write is named while lineage is down) together with `provenance$edges`, located
  through DescribeTable on the provenance namespace and read in-process, to the last table with lineage's own walk
  (`lakehouse.derived_tables`, which `GET /tables/{name}/derived` shares). A release stays final: the commit
  reconciler's tick lists a late derived table (found after the release, its write declared before it) in the
  obligation's `late_derived` and counts `catalog.erasure.late_derived`, which pages (`ErasureDerivedAfterRelease`). Each derived table is a
  `derived:<uuid>` surface: `followed` while an obligation recorded on it names this one in `follows`, directly or through
  a chain (`erasure_obligations.followed_tables`); `failed` while a claim is held for it (live or in the trash) and none
  does, which keeps the obligation open, as an unreadable edge list or claim does; `held` once deregistered with a dataset
  still at its released claim's location (`objectfs.is_lance_dataset_root`, at `base_refs.key_uri`), open until those
  bytes are deleted; `gone` once dropped with its bytes, or named by no claim at all (`location_claims.every_claim`, read
  whole). The source's predicate never judges a derived table. A reader is shown a derived table (its uuid, and its edge
  name in the detail) only while holding `can_get_metadata` on it; the rest are counted in one `derived` surface, in the
  report and the obligation views alike (`erasure.shown`). Each tag the verification drops is announced as an untargeted
  `table_tag_deleted` control event naming the tag, the ref it pinned (`dataplane.tag_event_ref`, the tag door's own) and
  the obligation, and listed in `report.untagged`. A `follows` naming no recorded obligation is refused 400. A catalog
  keeping no provenance namespace walks the input record alone. Pinned by
  `tests/integration/test_an_erasure_finds_every_table_derived_from_a_source.py` (live catalog and lineage) and
  `services/catalog/tests/test_an_erasure_is_an_obligation_the_catalog_verifies.py` (the refusal, and the governed
  reader matrix).
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
  orphans — but partially-failed writes and unpurged buckets do. **Each table's orphan check** finds them (owner,
  2026-11-09/10; ADR 0198's 2026-11-10 amendment): a task of its own per table (`maintenance.task.orphans.<uuid>`,
  `services/orphan_check.py`) that takes its table's claim (`table.<uuid>`, so never beside its pass), applies the
  pass's guards (claim uuid, `trash.keeps`, `governs`, a `__manifest` row), scans the main dataset
  (`orphans.scan_dataset`, with an age cut: a file not surely before the scan began reading is no finding), raises the
  listing floor on its own findings (`floor.raise_listing_floor`, then a follow-up pass of the table), logs one
  `maintenance_orphan_check` line and counts `maintenance.orphans.checks` by outcome and `maintenance.orphans.stranded`.
  Its next check rides the schedule's body (`Task.interval_hours`): stranded or undecided -> a day; after a raise ->
  the retention plus a day; clean or only Lance-reclaimable -> twice its interval up to
  `maintenance.orphanCheck.ceilingDays` (30); excluded (a branch, MemWAL, an unsupported flag, files through a base,
  past 500 versions, `orphans._MAX_REFERENCED_FILES` referenced or `_MAX_LISTED_FILES` listed files) or an ungoverned
  bucket -> the ceiling; trashed or gone -> none;
  a failed read raises, so the worker retries. Its bound is its own (`orphanCheck.maxSeconds`, `Worker`'s
  `max_seconds_by_kind`). A table's pass holds its first check a day out (`TaskQueue.schedule_first`, refused while one
  is held; the audit holds a missing one the same way). A raise moves the floor only once the table's pass cleans
  beneath it, so while a raise stands above the floor (`floor.raise_stands`) the pass cleans from the first reclaimable
  version rather than from `minVersionsToExpire`.
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
  classifies by that, and the floor raise (`floor.py`, [[LH-094]]) acts only for the floor-bound kinds. A table carrying
  the raise's stamp (`floor.FLOOR_KEY`) is raised again only once its floor has moved past the stamp (measured
  2026-11-10 on pylance 13.0.0: a second raise over a moved floor, then one cleanup, removed the file stranded since).
  The floor and the stamp are read off one handle, and a reading that fails refuses the raise, so a store that fails a
  second read never reads as "never raised".
  The cost of #8944: a vended-credential writer that stages fragments and commits more than 7 days later
  loses them to the `cleanup_old_versions` a table's pass runs.
- The FGA-only live seed (`fga_seed_demo.py`) writes projects no registry knows — the origin of
  "ghost projects". The replacement SHIPPED: `scripts/seed_estate.py` drives the real doors in
  hierarchy order — `POST /management/v1/projects` → `POST /management/v1/warehouses` → `POST /management/v1/warehouses/{id}/namespaces`
  → `/declare` → `POST /management/v1/access/tuples` LAST — so every guard runs and a state that cannot be
  reached through the UI cannot be seeded either. A grant whose create failed is SKIPPED rather than
  written; writing it is exactly how a ghost is made.
- **The control-event vocabulary is a wire contract in three files.** `ControlAction` /
  `ControlObjectType` (`lakehouse/control_events.py`) reach the frontend through
  `docs/catalog-openapi.json` → `frontend/console/src/lib/api/generated/catalog.ts`. Adding an action
  without `make openapi` + `bun --cwd=frontend run gen:types:catalog` leaves the TS client unable to
  name an event the backend publishes, and `test_openapi_contract` fails. Same for `TupleOrigin`
  (`lakehouse/governed/fga.py`) — an origin string not in the Literal is a `ty` error, not a runtime one.
- Maintenance covers EVERY warehouse bucket, not a static list (#81): a table's pass runs only on a table in a bucket
  it governs, read per task (`table_pass.governs`: a namespace bound to a warehouse is judged by that warehouse's
  status first, so a deactivated warehouse's table is not governed even in a configured bucket; only a namespace bound to
  none falls to `s3_bucket` + `MAINTENANCE_S3_EXTRA_BUCKETS`; an unreadable binding or warehouse record decides nothing
  and the pass is tried again after `retrySeconds`), and a table's orphan check asks the same `governs`. The purge's
  maintained estate (`purge.maintained_roots`) and the release's roots (`trash_release.roots_read`) read the registry
  strictly (`warehouse_records.read_warehouse_records`): one record either cannot read stops the step, since a root left
  out reads its live tables as gone — a bucket is created by an API CALL at runtime, so a config-time list goes stale by construction. The daily
  scan's storage walk reads the same registry (`_scannable_buckets`, a deactivated warehouse's bucket included),
  reporting an `IncompleteScan` rather than silently narrowing when it is unreadable. Residual: no multi-warehouse run against REAL object storage yet (#80).
- **Lance's cleanup leaves every non-root base alone, so maintenance reclaims a table's data directories
  itself** ([[LH-286]], `maintenance.services.base_reclaim`). `cleanup_old_versions` is ROOT-SCOPED: measured
  on pylance 13.0.0, after an overwrite of a table whose fragments sit on a data base, `cleanup_old_versions(older_than=0,
  delete_unverified=True)` removed every superseded version and none of the base's files (against
  `lance_docs/lance/guide/read_and_write.md:534`). The same root-scoping is what makes cleanup SAFE on a shallow clone (see
  `SUPPORTED_FOR_GC`). A data base is its tables' storage on the estate's store, reached under the catalog's own
  identity like a root (owner, 2026-10-24: Lance's default, "every base inherits the unscoped options",
  `lance_docs/lance/guide/object_store.md` § Per-Base Configuration); no base holds a credential of its own, so the native doors read it and the
  vend is direct. The expiry pass, after `cleanup_old_versions`, lists each directory the table's base record names
  as a `data` entry (a plain base, never a dataset root), subtracts every file any retained version of main or any
  branch references (a branch inherits the base under the same id and appends into the same directory, measured), and
  deletes what is left once it is older than Lance's unverified-file age, 7 days (`lance_docs/lance/guide/read_and_write.md:564-571`). It signs with the
  maintain-tier vend, which gets delete on the table's own data directories (`vending.VendableBases`), and the probe
  asks for that vend when a stranded file is past the age, so a table overwritten once still reclaims. A deleted row's
  bytes stay in its base fragment behind a deletion vector; the pass reports such fragments
  (`compaction.base_fragments.holding_deleted_rows`) and the compaction gate keeps refusing the table, since
  `CompactionOptions` has no `target_bases` and a rewrite would move the rows into the root (owner, 2026-10-24). The
  trash purge deletes a dropped table's data directories after its root and before the catalog forgets its base record, and
  so does every catalog door that ends a table with its bytes (`lakehouse.data_directories`, one rule for
  which directories: a `data` entry on a plain base whose claim names the table, held or released by it): a destructive
  drop and namespace cascade (`dataplane.destroy_table`, after the root, inside its residue record), a purging
  warehouse cascade (`_detach_table` before the deregister, so a failed delete leaves the table for the re-issue;
  `_forget_trashed` before the record) and a create's undo. A cascade keeping its bucket keeps the directories. Pinned by
  `tests/integration/test_a_data_bases_bytes_are_reclaimed.py`.
- **A JSON index has NO stats, and Lance says so with a PANIC — upstream, already contained.** On
  pylance 10.0.0 a maintenance pass over such a table prints `thread '<unnamed>' panicked at
  lance-index/src/scalar/json.rs:95:9: not yet implemented` once per Json index it reads (twice on the measured estate). The
  reproducing call is `ds.stats.index_stats("lineage_run_id_idx")` against a table carrying a JSON
  index built as `IndexConfig(index_type="json", parameters={"target_index_type": "btree", "path": ...})`
  — such as a provenance index on `lineage -> run_id`, which lets a governed row be filtered back to
  the run that wrote it; no lakehouse service builds one, a pipeline may. `not yet implemented` is Lance's own `todo!()` — stats for the JSON scalar
  index simply do not exist yet — so there is nothing to fix on our side and nothing to file beyond
  upstream. It is CONTAINED, and deliberately: `index_health._stats` catches **`BaseException`**, not
  `Exception`, because a Rust panic surfaces as pyo3's `PanicException`, which derives from
  BaseException and would otherwise sail straight through the pass's and the worker's error handling, which catch
  `Exception`.
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
- **CHECK THE TRASH RECORD BEFORE TOUCHING BYTES — including when you are "only looking".** A table
  holding versions far older than the retention that no pass reclaims is almost always the F6(d)
  exclusion: a recoverably-dropped table (`_trash/` record) is frozen until undrop or purge, because
  maintenance may not rewrite bytes someone can still restore, so its pass ends unrun
  (`maintenance_task_table_trashed`). Diagnosing that by hand-running `cleanup_old_versions(older_than=7d)` on one of them (done
  2026-08-16, on `bind86-bronze$converge-proof`) IS the destructive call the exclusion exists to
  prevent: it removed 5 versions / 7,567 bytes and destroyed time-travel to v1–v4 on an object that
  was still restorable. The latest version, row count, `published` tag and undrop all survived, so
  cleanup was working correctly — maintenance was right to decline and the operator was wrong to
  override it. Tags are not the check; the trash record is.
  **Every maintenance writer reads the record itself** ([[LH-449]]). A table's pass asks `trash.keeps` by the task's
  uuid before it opens anything, and a table the trash keeps gets no pass and no next time, its branches with it
  (`table_pass.run_table_pass`, ended `trashed`), its own trash task deciding what follows. A table's orphan check asks
  the same `trash.keeps` before it opens anything, and a trashed table is checked no more, so the listing-floor raise it
  makes never writes to a trashed table. The vend door's describe is not this gate:
  a Lance commit is a put-if-not-exists on the store, so a writer holding any credential could commit.
  The storage walk (`reconcile._trashed_keys`) and the trash purge compare a record's location as the path it decodes to
  (`base_refs.decoded_path`, [[LH-318]]): a record holds the location `describe_table` answered, which
  percent-encodes a registered location (`…/r%C3%A4ksm%C3%B6rg%C3%A5s`), while discovery lists the decoded
  directory the store keeps (`…/räksmörgås`) and an event names the encoded one.
  **Every other maintenance reader of a location the catalog records reads it the same way** ([[LH-319]]). A
  location maintenance holds is a store key, compared as listed: discovery's, the `__manifest`'s, and a table task's,
  whose pass finds the table's claim at the task's location (the claim's own, or a write event's `dataSource` URL) and
  then reads that location as the key it decodes to (`table_pass.held_location`), so its table id, policy, cadence
  stamp and vend check are every reader's own. A location the catalog recorded is a URL, read once as the key it decodes to. That covers:
  - the purge, which reclaims a trashed table under the key its record decodes to and the `.lance-reserved` marker
    pylance 13 writes under the spelling, both from `lakehouse.table_paths.spellings`, deleting by key
    (`lakehouse.stored_objects.list_under`/`delete_objects`, no directory marker written) with `_versions/` last (measured over moto: `…$r%C3%A4…/.lance-reserved`
    beside data at `…$räksmörgås/`);
  - the reconciler's trash set and the location the vend door answers (`credentials`); a table's policy is found by
    its uuid and its namespaces' uuids (`maintenance_policies.resolve_policy`, [[LH-523]]), never by a path;
  - the tombstone probe (`purge.objects_held`), and the compaction gate's root probe.

  The purge deletes everything under the table's root by key, its own branches' `tree/` with it; a trashed table a
  registered clone reads keeps its bytes, since the catalog vends no session to delete them while the clone's tag
  stands on it (`trash_reclaim.clone_refusal`, ADR 0198 §1). On a local `file://` root pyarrow decodes the
  URL itself, so only an S3 store shows a reader that compares spellings.
- **A governed tier's maintenance run is emitted only when expiry does MATERIAL work.**
  `table_pass.py::_did_material_work` gates the `expire_versions` emit, and its `lance.audit` record, on
  `old_versions_removed` or `base_files_removed`, so a correctly-idle estate records nothing — deliberately, since a
  pass after every commit would otherwise flood the graph with no-op runs. Consequence for verification: a stamped tier
  dataset does NOT produce a `(:Run)-[:WROTE]->(:Dataset)` node just by being written and passed; a pass that removed
  no version and no base file emits nothing, which is the gate behaving correctly, not a defect.

  **MAINTENANCE NAMES A DATASET BY `__manifest` ALONE** ([[LH-520]], `maintenance.services.naming.TableIds.table_of`):
  the row whose location it is, or whose table it is a branch of. It parses no name out of a path and reads no
  `lineage.dataset_id` stamp. A dataset no row names is refused `unnamed` by its table's pass (`table_pass._unnamed`:
  reported by its location, never expired or vended), checked by no orphan check (`unnamed`), and reported by the reconciler as an
  `UnregisteredDataset` by location.

  **A DERIVED TIER INHERITS ITS UPSTREAM'S `lineage.dataset_id` UNLESS THE STAMP DROPS IT.** No maintenance reader
  names a dataset by it, and `lineage.dataset_id` is schema METADATA, which survives `set_column` / `append_column` /
  `drop_columns` — so a silver table derived from bronze declares bronze's id unless something
  re-declares it (measured 2026-09-09: silver's compactions and FAIL events were filed against bronze's
  node). `lakehouse.stage_stamp.stamp_stage(..., dataset_id=...)` re-declares the destination
  and DROPS an inherited id when none is given, the same rule it applies to the `lineage` document. No
  lakehouse service calls it; it is the helper a pipeline writing a derived tier uses. When
  you check a declared-id chain, ask not "is an id stamped" but "whose uuid, on the tier that derived it": the catalog
  stamps the uuid its location claim holds, never the name (`dataplane._named_by_its_uuid`, ADR 0173 §10).

  **EVERY TIER IS A CATALOG TABLE, and the writer is the user's pipeline** (ADR 0154, ADR 0155). Bronze,
  silver and gold are written only through the namespace doors, so each tier is a `table:` object with a
  record and ownership tuples from its first row, and the catalog announces every committed write on
  lineage. Moving rows between tiers runs on the user's own compute and orchestrator; the lakehouse runs
  no stage. **The promotion gate is the `published` tag** (ADR 0156 C1, ADR 0161, [[LH-466]]): readers of a
  governed tier read `@published`, and `POST /management/v1/table/{id}/publish` runs the project's quality gate
  (`/management/v1/project/{id}/gate/*`) against the committed version; a pass advances the tag. A declaration that
  exists and cannot be read, or the table's binding to its project, refuses the publish (503 when the store did not
  answer, 500 code 18 for a record that cannot be read as one) rather than judging it against the request's own weaker
  values (`publication.declared_gate`, [[LH-572]]). With
  `review_enabled` on that gate, a version whose non-structural assertions fail, or whose row count moves past
  `review_band` from the published one (or that would be the table's first publication), is HELD
  (`catalog.services.promotions`): `published` stays, one record per table under `_promotions/` on the control
  root lists it on `GET /management/v1/promotions`, and the tag `rask.held` pins it against cleanup. A `can_promote`
  holder answers on `POST /management/v1/table/{id}/promotion/approve` (`published` moves to it) or
  `.../promotion/reject` (main is restored to the published version as a new commit); a hold unanswered after the
  gate's `review_hours` expires on `catalog-promotion-expiry-cron`. Every move of `published` emits a `publish_table`
  lineage run, which a notifier delivers to the project's watchers (`docs/COMMIT-EVENTS.md`), and the spec's tag doors refuse
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
  every approved multi-base data bucket, and `LANCE_RESERVED_BUCKETS`), the
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
  ([[LH-203]], `lakehouse.branch_layout`). The format joins the name verbatim, so `a/b`
  lives inside `tree/a/` and `a/_versions` inside `a`'s own version directory, and Lance reads
  whatever lies under a branch's directory as that branch's (measured on pylance 12.0.0: a reclaim of
  `a` deleted `a/_versions`'s manifests; deleting `a/_versions` deleted `a`'s history). So the create
  door refuses a layout segment (400) and a name that nests with an existing branch (409, code 19); a
  branch-maintain vend grants only `tree/<b>/{_versions,_transactions,_deletions,_indices,data}/*`
  and a branch-write vend only what a commit stages under `tree/<b>/` (`table_layout.WRITER_PUT_GLOBS`);
  delete goes through nested branches deepest first, announcing each it removes as `table_branch_deleted` with its
  incarnation ([[LH-413]]), refused whole (409) when a branch outside them is
  forked from one; every reclaim (expiry's `nested_branch` gate,
  `maintenance/run`, `version/delete`) refuses a branch with another inside it; and a table's pass lists its
  branches from its refs (`table_pass._datasets_of`, `branches.list()`), never by walking `tree/`.
- **A tag names a version within ONE branch's history, so no reader takes its version alone** ([[LH-284]]). A tag
  file records `branch` beside `version` (`lance_docs/lance/format/table/branch_tag.md` § Tag File Format), branch
  histories are numbered independently, and pylance's `tags.get_version` answers the version alone (measured on pylance
  13.0.0: a tag on `("work", 3)` answers 3). Every catalog reader resolves a tag through `dataplane.tag_reference`,
  which reads both. DescribeTable refuses a tag on a branch with the code `branch` gets (406, code 0), since
  DescribeTableResponse has no field to name the branch. The tags the catalog keeps on main (`published`,
  `publishing`, `rask.held`) are read through `dataplane.main_version_of_tag`, which refuses one found on a branch 409
  code 19. Pinned by `tests/integration/test_moto_s3.py::test_describe_at_tag_resolves_via_the_catalog` and
  `tests/unit/test_publication.py`.
- **The manifest's feature flags ARE reachable, and they are the refusal gate (#64).**
  `packages/lakehouse/src/lakehouse/features.py` reads
  `reader_feature_flags`/`writer_feature_flags` as varints at
  protobuf fields 9/10 of `LanceDataset._ds.serialized_manifest()` — pylance exposes neither field
  but its own pickle path uses that blob. The masks live in `lakehouse/features.py` because `services/maintenance`
  and `catalog/services/maintenance.py` both consume them, and a mask widened for one must not silently
  widen for the other. Measured on pylance 9.0.0: plain `(0,0)`,
  `delete()` `(1,1)`, `enable_stable_row_ids` `(2,2)`, `add_bases`/`shallow_clone` set 16, a committed
  `LanceOperation.DataOverlay` sets 64.
  **TWO GATES, NOT ONE, each asked per verb.** `describe_gc_unsupported_flags` against
  **`SUPPORTED_FOR_GC` (= `SUPPORTED` | 16)** gates version reclamation: maintenance's expiry pass and the catalog's
  `maintenance/run` (`require_reclaimable`; a version a registered clone reads is kept by the clone's tag, ADR 0198
  §1). `describe_compaction_unsupported_flags` gates
  compaction alone: the due list will not list a table it refuses, and `version/create` refuses a client's `Rewrite`
  through `require_compactable` (the evidence gate). So flag 16 is NOT refused outright: such a dataset is still
  expired, because root-scoped operations are safe on a shallow clone while compacting one silently materialises the
  base into it. The ORPHAN scan keeps the narrow flags-only `describe_unsupported_flags`/`SUPPORTED` mask, where the
  refusal is genuinely required. The cost of a blanket flag-16 refusal was measured: 17 of the estate's datasets were
  refused, exactly the ones with fragments and version history, so maintenance did no work on them at all.
  **The COMPACTION gate asks about the BASES, not about the flag — it is the one gate here that is not
  flags-only.** Refusing every flag-16 dataset was still over-broad by exactly the shape of a table with
  one external blob prefix: `initial_bases` registers ONE external
  blob prefix via `initial_bases`, which sets 16 while every data file stays under the dataset's own
  root — measured on pylance 10.0.0, compacting that is a merge and nothing more (4 fragments -> 1,
  9,445 -> 14,366 bytes locally, base byte-identical, 20/20 payloads still resolving). Refused anyway,
  the estate's most-fragmented tiers accumulated fragments forever while maintenance reported clean
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
  A table the compaction gate refuses is left off the due list, logged `maintenance_compaction_not_due_refused_layout`
  at DEBUG (`due.compaction_due`). A pass reports each refusal of its own on the dataset's `maintenance_dataset_outcome`
  line and counts it on `compaction.datasets.refused` by `refused_by` (`table_pass._record_dataset_metrics`) — never
  inside `error` or `skipped`.
  **The ORPHAN scan stays flags-only, and that is not an oversight.** Its own consequence check cannot
  stand in for the flag: `add_bases` registers a base no `DataFile` resolves through yet, every
  `base_id` stays `None`, and the scan passed such a dataset as `checked=True` with orphans named. A
  scan that names live data as garbage is a different and worse failure than a rewrite it declined.
  Flag 64 was only ACCIDENTALLY safe: pylance refuses that open itself, and the untyped `open:` error it produced is exactly what
  a pass's lineage layer drops as noise (`table_pass._lane_of`). Widening `SUPPORTED` is a deliberate edit — it is a
  whitelist, so a pylance upgrade adding a legitimate flag silently stops maintaining every dataset
  that sets it, which is why the refusal counter has to be loud. **A clone's SOURCE is held by a tag, not a flag**
  (ADR 0198 §1): compacting the source and cleaning up after it deletes files only the clone references (reproduced),
  and the source's own manifest carries no flag, so no per-dataset check can see it. The tag a registered clone holds
  on the version it reads keeps those files, since cleanup exempts a tagged version (measured on pylance 13.0.0,
  `lakehouse.clone_sources`); a clone never registered keeps its own tag through the tag door, as Lance's contract
  asks.
- **A namespace is a `__manifest` ROW, not a directory** (the `dir` impl the chart runs —
  `LANCE_REST_IMPL=dir`). Only a TABLE materialises a directory. Any scan that enumerates namespaces
  by listing directories silently returns `[]` on every real estate — which reads as "checked and
  clean". The reconciler's `unbound_namespaces` detector shipped with exactly that bug.
- **`__manifest` is the catalog's to reclaim, and its connections are why** ([[LH-251]], ADR 0185). Every namespace
  operation rewrites a root's index whole as a new version, and the backend's rewrite skips `lance.auto_cleanup.*`, so
  nothing but `catalog-manifest-reclaim-cron` (`catalog.api.manifest_reclaim`) bounds it: Lance's own
  `cleanup_old_versions(older_than=catalog.manifestReclaim.retainSeconds)` on the catalog's root and every registered
  warehouse's. A root it keeps failing on pages (`CatalogManifestReclaimFailing`, counted on
  `catalog.manifest_reclaim.failed` by root). Maintenance never touches it: a pass runs on a table a claim names, and `discovery` skips `__` directories. A `dir` connection reloads by
  probing only the version after the one it holds (lance v13.0.0 `DatasetConsistencyWrapper::reload`), so a reclaim
  that removed that successor would leave the connection answering `InternalError` for good (measured); that is why
  `NamespaceConnections` builds a connection again once it is a quarter of the retention old. Never run a cleanup on an
  index with a shorter `older_than` than the chart's retention, and never by hand on a live root. The catalog sets
  `inline_optimization_enabled=false`: the spec's three indexes cost store requests on every create and describe at
  estate scale (measured over moto).
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
