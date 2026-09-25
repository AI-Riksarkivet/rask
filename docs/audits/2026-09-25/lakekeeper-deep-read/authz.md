# Lakekeeper AUTHORIZATION deep-read → how rask's open rows should be solved

Date: 2026-09-25. Read-only. Reference: `/home/gabriel/Desktop/lakekeeper-ref` (paths below are relative to it
unless prefixed `rask:`). rask paths are relative to `/home/gabriel/Desktop/rask`. Every rask claim was read in
the working tree (which carries the staged pylance-12 fix; nothing in this domain touches it).

Stack translation rule applied throughout: Lakekeeper = Postgres catalog + OpenFGA; rask = Lance/lance-namespace
(`__manifest`, name-path identity) + OpenFGA + Dapr + NATS + Dex/K8s SA tokens. Nothing Iceberg-specific is
proposed; where Lakekeeper's answer depends on a Postgres transaction or a UUID column, the translation says so.

---------------------------------------------------------------------------------------------------------
## 0. Headline (ranked by what it unblocks)

1. **Model versioning is solved in Lakekeeper by a compiled-in model VERSION + bookkeeping tuples IN the
   store; rask pins "newest" or a hand-copied ULID and has two model writers.** rask's chart hook
   (`write_model.py`) picks `stores[0]` rather than the named store and decides "changed" on relation NAMES
   only, so a body-only edit never reaches the store through the hook. → new row / XC-011 / XC-007(authn).
2. **List filtering: Lakekeeper never uses ListObjects below the project level.** It checks one
   `can_list_everything` on the container, else batch-checks `can_include_in_list` on the DB page and refills
   the page. rask intersects every table/namespace/warehouse listing with an ESTATE-WIDE `list_objects`,
   capped at 1000, then paginates. → LH-050, the sibling-oracle finding.
3. **Existence first, then `[can_see, action]` in one batch; invisible ≡ absent.** Adopting this ordering
   answers LH-037(1) (Skip becomes reachable with no oracle) and the "403 for hidden siblings, 404 for ghosts"
   finding in one change.
4. **Typed, exhaustive per-action relations; no default.** rask's `_action_relation` falls unmapped suffixes
   through to `can_write_data`, the defect surface its own tests name four times. → XC-020/LH-077/LH-076.
5. **Tuple lifecycle.** Lakekeeper refuses a create whose id already has tuples (`require_no_relations`),
   deletes by object AND by user (usersets included), and reconciles STRUCTURAL edges from the catalog. rask:
   a FRESH create never revokes stale tuples on a reused name; revoke reads by object only; the rebuild pass
   restores only project→warehouse. Writing only the structural parent edge for `ungoverned_tables` dissolves
   LH-144's blocker (1) using rask's own 2026-09-10 ruling. → LH-144, LH-099, rename finding, LH-063.
6. **rask's project admin split is decorative**: `security_admin`/`data_admin`/`role_creator` are defined
   (rask: model.fga:65-73) and reach nothing below the project and are checked by no code. Lakekeeper wires
   them into the warehouse rungs. → new (XC-020 class), CTL-019.
7. **The raw-tuple admin door (`POST/DELETE /v1/access/tuples`) is exactly the escalation Lakekeeper refuses
   even its instance admins.** → LH-076 / new.

---------------------------------------------------------------------------------------------------------
## 1. Authorization-model versioning, writing and pinning across upgrades

### What Lakekeeper does
- The model is versioned `<major>.<minor>` and every version is a directory (`authz/openfga/v2.1`, `v3.4`,
  `v4.0`, `v4.7`) with a CHANGELOG that states, per version, `MODIFIES_TUPLES` / `ADDS_TUPLES`
  (`authz/openfga/README.md:1-66`). v4.7 is "all backwards-compatible: existing tuples authorize the same
  actions" (README.md:14); v4.0 changed the object representation and ADDED tuples (README.md:54-66).
- The binary compiles in the version it serves: `ACTIVE_MODEL_VERSION = V4_CURRENT_MODEL_VERSION = 4.7`
  (`crates/authz-openfga/src/migration.rs:11-18`) and embeds each version's `schema.json` via
  `include_str!` (migration.rs:63-98).
- The version→model-id mapping lives IN the OpenFGA store as bookkeeping tuples of two model types,
  `auth_model_id` and `model_version { openfga_id: [auth_model_id]; exists: [auth_model_id:*] }`
  (`authz/openfga/v4.7/components/model_version.fga:1-8`); migrate writes
  `auth_model_id:<id> openfga_id model_version:<v>` and `auth_model_id:* exists model_version:<v>`
  (migration.rs:129-137). (`openfga-client` 0.6's `TupleModelManager` source is not on this host; the
  semantics above are from migration.rs's own doc comment and the model file.)
- ONE writer: `lakekeeper migrate` (`crates/lakekeeper-bin/src/main.rs:412-434`, which calls
  `authorizer::migrate` at `crates/lakekeeper-bin/src/authorizer.rs:24-31`), run by the chart as a
  per-revision Job hook (`lakekeeper-charts/charts/lakekeeper/templates/db-migration.yaml:1-24`,
  `helm.sh/hook: post-install,post-upgrade`); `catalog.dbMigrations.enabled` says "you will need to ensure
  `lakekeeper migrate` runs before the catalog pods start" (values.yaml:233-236).
- Serving pods NEVER write a model. At startup `new_authorizer` resolves the id for the compiled-in version
  and pins every Check to it: `get_active_auth_model_id` → `BasicOpenFgaClient::new(service_client, &store.id,
  &auth_model_id)` (`crates/authz-openfga/src/client.rs:109-127`); an absent version is fatal with
  "Make sure to run migration first!" (`crates/authz-openfga/src/error.rs:150` `ActiveAuthModelNotFound`, and
  migration.rs:124-126).
- Upgrade safety comes from immutability: an old pod keeps the id it pinned at boot; the new version's model
  is written before new pods start; migrations are idempotent — three `migrate` runs leave exactly two models
  (v4.0 + v4.7) (migration.rs:204-247).
- Representation changes are ADDITIVE migrations: v4.0's `v4_push_down_warehouse_id` walks
  server→project→warehouse→namespace BFS and re-writes every tuple naming `table:<id>` as
  `lakekeeper_table:<wh>/<id>` without deleting the old ones (`src/migration/migration_fns_v4.rs:77-200`,
  BFS 266-312, 50-permit semaphore 66-73, per-warehouse batching to bound memory 139-141); the old types are
  dropped two minors later with orphans "harmless" (README.md:16-18).
- Operator pin: `authorization_model_version` config skips migration and serves a pre-existing version
  (config.rs:67-73, migration.rs:147-152). **Do not copy the implementation:** `get_active_auth_model_id`
  computes `model_version` from the configured value but looks up `*ACTIVE_MODEL_VERSION`
  (migration.rs:113-116), so the configured version is logged, not used — a latent Lakekeeper bug.
- OpenFGA itself is authenticated (client credentials or API key; a half-configured client is refused)
  (config.rs:83-154, client.rs:15-53).

### What rask does today
- Model: one `model.fga`/`model.json`, no version number, no bookkeeping type (rask: model.fga:1-777).
- Two writers:
  - the catalog at every unpinned boot: `build_fga_client(provision=True)` (rask:
    services/catalog/src/catalog/main.py:140; packages/service-kit/src/service_kit/governed/auth_lifespan.py:105-170)
    → `fga.provision` reuses the named store, refuses a NARROWING model, skips an identical one, else writes
    (rask: packages/service-kit/src/service_kit/governed/fga.py:514-604). Its own comments record 1,316
    model versions (fga.py:560-567) and a rollback that stripped `warehouse#event_stager` (fga.py:541-551).
  - the chart hook `openfga-model` (post-install/post-upgrade, weight 0) running
    `service_kit.governed.auth.write_model` (rask: chart/templates/openfga-model.yaml:27-78).
- **Defects in the hook writer (read, not driven):**
  - Store selection is `RASK_FGA_STORE_ID or stores[0]["id"]` (rask: write_model.py:83) — the first store
    the server lists, not the `lance-catalog` store that `provision`/`resolve` select by name + newest
    `created_at` (fga.py:526-531, 624-629). On a store with test-residue stores (the Lakekeeper findings
    record a dirty shared store) the hook can write the model into the wrong store while reporting success.
  - "Changed" is `shape()` = types + relation NAMES (write_model.py:27-43). A body-only edit (the kind
    Lakekeeper's v4.7 `can_grant_data_admin` tightening is, README.md:47) is never written by the hook.
  - No narrowing guard, unlike `provision` (fga.py:533-557): a `helm upgrade` to an older chart writes the
    older (narrower) model as "newest".
- Readers: every non-catalog service `fga.resolve`s the store by name and pins its NEWEST model
  (fga.py:607-644; auth_lifespan.py:142). Production pin = two hand-copied ULIDs `auth.fgaStoreId`/
  `auth.fgaModelId`, empty by default (rask: chart/values.yaml:962-967; _helpers.tpl:1315-1324), with
  `audit_pinned_model` reporting (never refusing) drift (fga.py:670-734).
- OpenFGA is unauthenticated (XC-007 finding; `ClientConfiguration(api_url=...)` only, fga.py:525,541,633).

### What rask should do (own stack)
1. Add Lakekeeper's two bookkeeping types verbatim in shape: `type auth_model_id` and
   `type model_version { define openfga_id: [auth_model_id]; define exists: [auth_model_id:*] }`, and a
   compiled-in `MODEL_VERSION = "M.m"` constant beside `model.json` in `service_kit.governed.auth`.
2. ONE writer: the `openfga-model` hook Job, with its own ServiceAccount identity (D1), is the only thing that
   writes a model. It (a) resolves the store BY NAME (fix `write_model.py:83`), (b) reads
   `model_version:<M.m>#openfga_id`; if present → exit 0 (idempotent by VERSION, not by shape); else writes
   `model.json` and the two bookkeeping tuples in one Write. Delete `provision=True` from the catalog
   (main.py:140) and the whole provision path's write half.
3. Every service at boot reads `model_version:<its MODEL_VERSION>#openfga_id`, pins that id in `make_client`
   (fga.py:737-768), and FAILS CLOSED if absent (Lakekeeper's `ActiveAuthModelNotFound`). Drop `fgaModelId`
   from the chart; the store id may stay resolve-by-name. `audit_pinned_model` becomes unnecessary.
4. A unit test that fails when `model.json` changes without a `MODEL_VERSION` bump (hash of the compiled model
   committed next to the constant). This is what makes "body-only edit" impossible to ship silently.
5. Re-key migrations (LH-063 principal key, LH-150/§2 object-id canonicalisation) run inside the same Job as a
   version-gated function, idempotent, before the new version's bookkeeping tuple is written — Lakekeeper's
   `add_model(..., migration_fn)` hook (migration.rs:63-79). **Owner rule overrides Lakekeeper here:** no
   dual representation / deprecation window — the Job re-keys and deletes old tuples in one pass (the store
   is test data; `chart/values.yaml` already records "RESEEDING THE FGA STORE IS ACCEPTABLE", owner
   2026-08-08).
6. Authenticate OpenFGA with projected SA tokens (`OPENFGA_AUTHN_METHOD=oidc`, SA issuer) — the XC-007
   authn split the Lakekeeper findings already wrote; the model-writing Job is then the only identity with
   model-write reach.

Rows: XC-011 (its "provision is content-gated" answer is superseded by version bookkeeping), XC-007 (authn
half), **new** ("the model hook writes `stores[0]` and compares names only").

---------------------------------------------------------------------------------------------------------
## 2. Object identity: what an FGA object id is keyed on

### Lakekeeper
- Every governed object is keyed on an immutable UUID: `namespace:<uuid>`, `warehouse:<uuid>`,
  `lakekeeper_table:<warehouse_uuid>/<table_uuid>` (`crates/authz-openfga/src/entities.rs:262-318`; the
  warehouse prefix because "table ids can be reused across warehouses", :272-284).
- Consequence: rename touches no tuple, a delimiter or display change cannot re-key anything, and a fresh
  object can never inherit a predecessor's grants.
- User subjects are `user:` + urlencode(`<idp_id>~<subject>`) (entities.rs:156-189) — so the `:` in
  `system:serviceaccount:<ns>:<sa>` and the `~` separator survive OpenFGA's `type:id` grammar.

### rask
- Object ids are the NAME PATH joined with the configurable request delimiter:
  `canonical_object_id(segments, delimiter=settings.delimiter)` (fga.py:173-183) → `table:bronze$pages`.
- lance_docs gives no table UUID to key on: the identity is the name path (spec.yaml:4624-4645
  `RenameTableRequest` identifies by `id` + `new_table_name`/`new_namespace_id`), and `__manifest.object_id`
  is "the namespace path joined by `$` delimiter" (lance_docs/namespace.md:972). The REST `delimiter` is a
  per-request parsing parameter ("When not specified, the `$` delimiter must be used",
  namespace.md:2714,2806; string-style identifier `cat4#t3` "when using delimiter `#`", namespace.md:1599-1605).

### What rask should do
- **Do not invent a UUID.** A rask-minted id would be a second identity beside `__manifest` that every check
  must resolve name→id through; Lance's identity is the name path and the owner's rule is Lance-idiomatic.
- **Key FGA objects on the manifest's canonical form, not on the request delimiter.** Canonicalise
  segments with the storage `$` (namespace.md:972) independent of `LANCE_NS_DELIMITER`, which then only
  parses/prints wire ids. That makes LH-150's boot check unnecessary: changing the wire delimiter no longer
  re-keys anything. Preconditions not checked here: (a) whether pylance 12's DirectoryNamespace honours a
  non-`$` delimiter in `__manifest` (not measured); (b) that segments cannot contain `$` (spec grammar not
  checked). `tests/unit/test_cross_axis_identity.py` currently holds the opposite premise ("byte-identical
  under any delimiter", per LH-150) and would be rewritten.
- Because keys are names, rask must do explicitly what Lakekeeper gets for free: rename moves every tuple
  (§3), a fresh create clears stale tuples (§3), and every name-keyed record set is enumerable (the
  Lakekeeper-findings rename rows).

Rows: LH-150 (answer: re-canonicalise, not a boot refusal), rename rows (new, from findings_lance_lakekeeper).

---------------------------------------------------------------------------------------------------------
## 3. Tuple lifecycle: create, delete, soft-delete, rename

### Lakekeeper
- Two tuple classes per object: HIERARCHY (both directions, derivable from the catalog) and OWNERSHIP (the
  creating actor, "not reconstructable from the catalog") — one helper per type used by BOTH create and
  rebuild so they cannot drift, with golden tests (`crates/authz-openfga/src/tuples.rs:1-14, 40-246, 248+`).
  Project creator gets `project_admin`, every other creator `ownership`; an assumed role owns as
  `role:<id>#assignee` (tuples.rs:57-64; entities.rs:191-210).
- Create: `require_no_relations(object)` with HigherConsistency — 409 if ANY tuple names the object as
  object, or as user (per type in `user_of()`, with `#assignee` userset suffixes)
  (`src/authorizer.rs:1353-1423`; called by every create_* at 711, 755, 779, 807, 836, 868, 899;
  `lib.rs:90-131` for `user_of`/`usersets`).
- Ordering vs the catalog: catalog write inside the DB tx → FGA write → `t.commit()`
  (`crates/lakekeeper/src/server/namespace.rs:320-355`); an FGA failure rolls the DB back. Delete: commit
  first, then best-effort `delete_*` that logs and never fails the request ("namespace is gone from catalog,
  we should not return an error", server/namespace.rs:573-582, 766-840; tables.rs:881-890).
- Delete removes tuples where the object is OBJECT (`delete_relations_to_object`) AND where it is USER, per
  object type, including usersets — so a deleted role's `role:R#assignee` grants and a deleted table's
  `child` edge on its namespace go too (authorizer.rs:1425-1504). `delete_user` uses the same path
  (authorizer.rs:739-745).
- Soft delete keeps tuples (undrop and `can_list_deleted_tabulars` need them); the always-on expiration task
  deletes them at expiry, independent of purge (`crates/lakekeeper/src/service/tasks/tabular_expiration_queue.rs:209,238,272`).
- Idempotent writes and deletes (`on_duplicate: ignore` / `on_missing: ignore`, OpenFGA ≥ v1.11,
  `docs/docs/authorization-openfga.md:7-8`; `WriteOptions::new_idempotent()` at authorizer.rs:178-188,
  947-994, reconcile.rs:749-751); ≤100 tuples per Write, and a Write is one transaction
  (`lib.rs:43`; authorizer.rs:1140-1152).

### rask
- `grant_on_create` writes owner (+ parent + inverse child) in one batch (fga.py:1706-1772); machines get no
  owner (rask: services/catalog/src/catalog/api/fga_deps.py:1181-1238, ruling 2026-09-10).
- **A FRESH create does not clear stale tuples**: `table_create.py:224-229` revokes only when an Overwrite
  replaced an existing table ("a fresh create has nothing to revoke"). But a hard drop's revoke runs AFTER
  the irreversible native drop and on an OpenFGA outage "fails closed (503) with the tuples left stale until
  reconciled" (fga_deps.py:1241-1272, docstring). A create at that name before the repair tick inherits every
  stale grant — the reused-id bleed rask's own comments name. (Inferred from code; not driven live.)
- Revoke reads BY OBJECT only and reconstructs one inverse `child` edge (fga.py:1486-1536, 1855-1915); tuples
  where the dropped object is the USER in other shapes (e.g. a role's `role:R#assignee` grants, a
  `team:T#member` subject) are not swept — the model itself records that user-position objects are
  "invisible to `revoke_object_tuples`" (model.fga:120-127).
- Deletes are one Write per tuple to dodge the all-or-nothing 400 on an absent tuple (fga.py:1792-1853),
  although the installed SDK (openfga-sdk 0.10.4) has `ConflictOptions(on_missing_deletes=...)`
  (`.venv/.../openfga_sdk/client/models/write_conflict_opts.py:21-37`) and rask already uses
  `on_duplicate_writes=IGNORE` (fga.py:1672-1691); OpenFGA is v1.18.3 (chart/values.yaml:3017,3045).
- Rename seeds the caller and revokes every tuple on the source: direct grants are LOST (Lakekeeper findings
  row "A rename silently revokes every direct grant"; tables.py:1106-1108).
- Soft delete: tuples stay until the purge, and the purge is off by default (LH-099).

### What rask should do
1. **Create guard (Lakekeeper `require_no_relations`, translated):** on every FRESH create, after the native
   create succeeded (so the name was absent in `__manifest`), read tuples by object with HIGHER_CONSISTENCY;
   any found are stale by construction → delete them and write the seed in ONE Write. (Lakekeeper refuses
   409 because a UUID collision is corruption; for a name, reuse after a drop is legitimate, so clear-and-seed
   is the correct translation.)
2. **Idempotent batched deletes:** replace per-tuple deletes with ≤100-tuple Writes using
   `on_missing_deletes=IGNORE` (atomic per chunk). Needs a live proof on v1.18.3 before merge (not exercised).
3. **Rename = one atomic move:** read the source's tuples; build writes = each direct tuple re-pointed to the
   destination + new parent/child edges; deletes = every source tuple + old edges; one Write when ≤100 (else
   writes-before-deletes in chunks). Structural edges from the source parent are dropped, from the new parent
   added; grants on the source NAMESPACE do not follow (Lakekeeper's outcome: the source-namespace grantee
   loses the table, the destination grantee gains it — cited by the findings' rename row from
   `crates/lakekeeper-integration-tests/tests/openfga_rename_tabular.rs`, not re-read here).
4. **Delete by object AND by user:** add `revoke_subject_tuples(subject)` that reads by `user=` across every
   object type whose `directly_related_user_types` admit it — derive the type list from `model.json` rather
   than hand-listing like Lakekeeper's `user_of()` (lib.rs:96-123) — with `#assignee`/`#member` userset
   suffixes. Use it in role deletion and the principal-deletion door (§10).
5. **Expiry separate from purge** (LH-099): an always-on step revokes a soft-dropped object's tuples at
   `expires_at`; the purge only removes bytes (Lakekeeper `tabular_expiration_queue.rs`).

Rows: LH-099, LH-194 (the catalog-internal compensation is the Lakekeeper shape: the door that created the
object undoes it — `seed_ownership_or_compensate` is already that, fga_deps.py:1275+), rename finding (new),
LH-063 (subject sweep), **new** ("a fresh create inherits stale tuples on a reused name").

---------------------------------------------------------------------------------------------------------
## 4. Structural reconcile against the catalog

### Lakekeeper
- `rebuild_hierarchy_tuples_from_catalog` (additive, lock-free) and
  `reconcile_hierarchy_tuples_from_catalog` (add + delete drift, caller-held lock, advisory key
  `RECONCILE_LOCK_KEY`) (`crates/authz-openfga/src/reconcile.rs:1-63, 95-105, 164-247`).
- It rebuilds EVERY hierarchy edge from a catalog index (projects, warehouses, namespaces by BFS, all
  tabulars, roles) (reconcile.rs:253-510, 570-621) using the same `hierarchy_tuples_for_*` helpers as create.
- Deletion candidates are only managed STRUCTURAL triples (reconcile.rs:526-564) the catalog contradicts,
  with at least one endpoint known (reconcile.rs:667-672); ownership, grants, role assignments, bootstrap
  tuples and model bookkeeping are never touched (reconcile.rs:18-33; docs authorization-openfga.md:98-124).
  Dry-run first-class (reconcile.rs:124-147).

### rask
- `reconcile.py` reports `ghost_*` / `ungoverned_tables` (rask: services/maintenance/src/maintenance/services/reconcile.py:98-107,136).
- `repair.py` revokes ALL tuples of ghosts (object named by no catalog record) and refuses `ungoverned_tables`
  by name ("needs a grant, not a revoke", repair.py:82-121).
- `rebuild.py` explicitly takes Lakekeeper's line ("STRUCTURE IS REBUILT; A PERMISSION IS NEVER RE-GRANTED",
  rebuild.py:14-20) but only plans the project→warehouse tenancy pointer for stranded projects
  (rebuild.py:140-230); no namespace/table edge is rebuilt from `__manifest`.
- LH-144 blocker (1): "Governing an unowned table to a named subject is a privilege-escalation path".

### What rask should do
- **Extend `rebuild.py` to write structural edges for `ungoverned_tables`**: `namespace:<parent> parent
  table:<id>` + `table:<id> child namespace:<parent>` via `hierarchy_edge_tuples` (fga.py:125-153), from the
  Lance `__manifest` listing. This names NO subject — it is exactly what a machine registration writes under
  the 2026-09-10 ruling ("THE TABLE IS NOT LEFT UNOWNED … the project's admin already owns it transitively",
  fga_deps.py:1188-1200). Blocker (1) dissolves: the repair is structure, not a grant. Blocker (2) (synthetic
  drop) is not needed for tables that still exist.
- Keep rask's divergence on ghosts: full revoke is right HERE (name reuse would inherit), where Lakekeeper can
  leave orphans harmless because UUIDs never recur.
- Serialize the delete-drift mode with a single-writer lock (Lakekeeper: Postgres advisory lock). rask's
  equivalent primitive was not checked in this pass (see `rask-dapr` skill for which lock block, if any, the
  estate uses).

Rows: LH-144 (answers blocker 1), LH-061-follow-up (rebuild scope), LH-194 (absent_datasets is the inverse
report).

---------------------------------------------------------------------------------------------------------
## 5. List filtering: ListObjects vs batch check

### Lakekeeper
- ListObjects is used ONCE in the whole authorizer: listing projects when the caller lacks
  `can_list_all_projects` (`src/authorizer.rs:1104-1138`).
- Everything below a project: fetch a DB page; one Check of `can_list_everything` on the container; if true,
  mask all-true; else batch-check `can_include_in_list` for every item of the page
  (`crates/lakekeeper/src/server/tabular.rs:64-180`), chunked by `max_batch_check_size` (default 50 =
  OpenFGA's `OPENFGA_MAX_CHECKS_PER_BATCH_CHECK`, config.rs:74-80,140-144) in parallel, with index correlation
  ids and a `MissingItemInBatchCheck` error if any index does not come back (authorizer.rs:1277-1328).
- `fetch_until_full_page` refills a filtered page from the next DB pages until it is full, carrying per-item
  page tokens (`crates/lakekeeper/src/server/mod.rs:218-280`).
- The listing relation is consistent with upward visibility: `can_include_in_list: can_get_metadata`, and a
  namespace's `can_get_metadata: describe or can_get_metadata from child`, so "only items in the direct path
  are presented" (`authz/openfga/v4.7/components/namespace.fga:29-35`; docs authorization-openfga.md:64-67).
  `can_list_everything: describe` deliberately excludes the bottom-up clause (namespace.fga:34).

### rask
- Every governed collection listing calls `fga.list_objects` for the WHOLE estate and intersects:
  namespaces (rask: services/catalog/src/catalog/api/v1/endpoints/namespaces.py:373-376), tables in a
  namespace (namespaces.py:965-976, relation `can_read_data`), all tables (tables.py:223-236), warehouses
  (warehouses.py:305-320, 373, 413), models (models.py:106).
- ListObjects has no pagination; results are capped at `LIST_OBJECTS_SERVER_CAP = 1000` (fga.py:1072) and
  surfaced as `authorization_truncated` (fga.py:934-1001). The namespace listing drops that flag
  (namespaces.py:374-376 reads only `.objects`).
- Cost scales with everything the caller can reach in the estate, per request; an estate owner listing one
  namespace enumerates every table (ghost_tables alone was 1,033 on 2026-09-20, repair.py:13).
- The backend listing is drained UNPAGINATED so the filter can run before `paginate` (namespaces.py:940-959, docstring + `_drain_tables` at :959).
- Lineage already uses the batch pattern for visibility (rask: services/lineage/src/lineage/api/fga_deps.py:104,508).

### What rask should do
1. Model: add `can_include_in_list: can_get_metadata` to warehouse/namespace/table/materialized_view and
   `can_list_everything: reader` to warehouse/namespace (`reader` cascades to every child, so it is the
   "sees everything below" bar; it excludes the upward `from child` clause, as Lakekeeper's `describe` does).
2. Listing handlers: native page with `limit`/`page_token` (lance-ns ListNamespaces/ListTables carry both,
   spec.yaml:130-160, 2334-2346) → one `check(can_list_everything, container)` → else batch-check
   `can_include_in_list` over the page → refill like `fetch_until_full_page`. Stop draining unpaginated.
3. `fga.batch_check` must take `(relation, object)` pairs with correlation ids and verify completeness —
   today it returns a dict keyed by object for one relation (fga.py:856-890), which cannot express
   `[can_see, action]` on the same object (§6). The catalog's callers are fail-closed on a missing key
   (`allowed.get(obj)`, fga_deps.py:698-716); other callers were not checked.
4. Keep `list_objects` only for `/v1/me` projects (me.py:80-92) — Lakekeeper's one use.
5. Delete `authorization_truncated` and the 1000-cap machinery from catalog listings once (2) lands.

Rows: LH-050 (the listing "query store" is not needed for authz reasons; the O(estate) ListObjects is the
cost to remove), the Lakekeeper-findings sibling-oracle row (new), **new** ("governed listings enumerate the
whole estate per request and truncate at 1000").

---------------------------------------------------------------------------------------------------------
## 6. Existence hiding: the order of "does it exist" and "may you"

### Lakekeeper
- Resolve existence in the catalog FIRST; absent → "cannot see" (NotFound) (`crates/lakekeeper/src/service/authz/table.rs:570-583`).
- Then ONE batch `[CAN_SEE, action]` (table.rs:585-650; namespace.rs:340-432). `CAN_SEE` is `GetMetadata`
  for namespace/table/view/generic table and `Use` for warehouse (table.rs:39, namespace.rs:28, view.rs:31,
  generic_table.rs:29, warehouse.rs:24).
- Cannot see → the SAME `TabularNotFound`/`NamespaceNotFound` body as absent (table.rs:320-341;
  namespace.rs:36-45,102-115 (comment at :43) "HTTP response is deliberately ambiguous, but audit log should be concrete"); the audit
  keeps `ResourceNotFound` vs `CannotSeeResource`. Visible but not allowed → 403.
- This applies to destructive doors too (Drop goes through `require_table_action`).
- Multi-action on one object: `require_table_actions` puts CAN_SEE first per object and batches the rest
  (table.rs:783-850).

### rask
- `authorize` is a router dependency that checks the ACTION before any existence lookup
  (fga_deps.py:724-820); only on deny does `_absent_to_a_reader_of_the_parent` convert to 404, and only when
  the object is absent AND the caller holds `can_get_metadata` on the parent (fga_deps.py:822-887).
- Owner rule (2026-09-11): "404 on READ doors, 403 kept on destructive ones" (quoted by LH-037 from
  `tests/integration/test_an_absent_object_is_not_found_rather_than_forbidden.py:10`). Live: describe of an
  unknown id answers 403 (LH-037).
- Consequences: `drop_namespace mode=Skip` is unreachable (LH-037); a reader of `ns$a` gets 403 for hidden
  `ns$b` and 404 for `ns$ghost` (sibling-oracle finding).

### What rask should do
- Move per-object checks from "router before existence" to "handler after existence", Lakekeeper's order:
  existence (Lance `table_exists`/`describe`) → batch `[can_get_metadata, action]` → invisible ≡ absent.
- Read doors: uniform 404 for absent and invisible — closes the sibling oracle with no parent-probe special
  case (delete `_absent_to_a_reader_of_the_parent`).
- Destructive doors: absent and invisible answer identically (still no oracle, which is the intent of the
  2026-09-11 rule), visible-not-owner → 403. With that, `Skip` on an absent-or-invisible id is a 204 no-op
  (audited as ResourceNotFound/CannotSeeResource) and LH-037(1) needs no withdrawal of `Skip`. This changes the
  recorded "403 for unknown ids on destructive doors" to "404/204", so it needs the owner's confirmation.
- Cost: one existence read before authz for callers who will be denied — Lakekeeper accepts it.

Rows: LH-037 (answers (1)), sibling-oracle finding (new), LH-144 (the `exists` door already answers without
a per-object gate, LH-144 text).

---------------------------------------------------------------------------------------------------------
## 7. Per-action relations: typed and exhaustive, no fall-through

### Lakekeeper
- Each resource has a backend-agnostic action enum with a `variants()` list: `CatalogTableAction`
  {Drop{force,purge}, WriteData, ReadData, GetMetadata, Commit{updated/removed properties}, Rename,
  IncludeInList, Undrop, GetTasks, ControlTasks, SetProtection} (`crates/lakekeeper/src/service/authz/mod.rs:995-1049`),
  and the same for server/project/warehouse/namespace/view/generic table/role/user (mod.rs:352-1228).
- Every handler names its action explicitly; the OpenFGA backend maps each variant to one relation with an
  exhaustive `match` (`crates/authz-openfga/src/relations.rs:390-402, 681-706, 1000-1030, 1304-1320,
  1576-1590, 1838-1850, 1934-1946`). No default branch exists; adding a variant is a compile error until
  mapped — stated as a design rule for `is_spec_mutation`: "Exhaustive on purpose — adding a new action forces
  a compile-time decision" (mod.rs:701-740).
- Actions carry CONTEXT (properties, force/purge/recursive, format, base_location) as an `ActionDescriptor`
  used for the audit string and by policy engines (Cedar/OPA); OpenFGA collapses context to one relation
  (mod.rs:250-332, 898-977, 1050-1077).
- Data-plane vs control-plane is a property of the action (`is_data_plane`: ReadData/WriteData,
  table.rs:268-280) — used to keep the instance-admin bypass off data (§8).
- Every `can_*` relation in the model has a producer (e.g. table: lakekeeper_table.fga:18-39 ↔
  relations.rs:1576-1590 + API enums).
- Execution is governed as actions ON the data objects: `can_get_tasks: describe` / `can_control_tasks:
  modify` on tables/views/generic tables, `can_get_all_tasks`/`can_control_all_tasks` on warehouses,
  project tasks + task-queue config on projects (lakekeeper_table.fga:29-31; warehouse.fga:43-50;
  project.fga:52-56). There is no job/run type.
- The only non-Iceberg governed type is `lakekeeper_generic_table` — table's rungs minus `can_commit`, created
  by `namespace#can_create_generic_table`, format-tagged in the action context (e.g. "lance", "delta")
  (`authz/openfga/v4.7/components/lakekeeper_generic_table.fga:1-37`; mod.rs:829-848). No opaque-asset type.

### rask
- `_action_relation(fga_type, suffix)` infers the relation from the route suffix and defaults: namespace →
  `can_update_properties` (fga_deps.py:363-365), table → `can_write_data` (fga_deps.py:381); unguarded if the
  path matches no resource prefix (fga_deps.py:736-738). The comments beside `_OWNER_SUFFIX_RELATION`
  (fga_deps.py:163-294) and `test_fga_model_contract.py:353-387` record the fall-through catching `tasks`,
  `changes`, `history`, `branches/delete`, `tags/delete`, compaction — each fixed one at a time.
- Relations with no producer: `transaction.can_set_property`/`can_cancel` (XC-020, LH-077);
  `can_read_assignments` on warehouse/namespace/table (model.fga:416, 517, 630) — `access/list` is gated at
  `can_drop`/`can_delete` instead (fga_deps.py:207, 226, 273, 294); the whole admin split (§8).
- Audit records the relation only (`audit(relation, ALLOW|DENY, subject, resource)`, fga_deps.py:411-418),
  so two actions sharing a rung are indistinguishable (rask added `can_set_protection` for exactly that).

### What rask should do
1. One `StrEnum` of actions per resource in `service_kit` (table: `read_data`, `write_data`, `get_metadata`,
   `drop`, `deregister`, `rename`, `restore`, `create_branch`, `create_tag`, `update_tag`, `maintain`,
   `classify`, `set_protection`, …) and a TOTAL `{action: relation}` dict per type, asserted against
   `model.json` both ways (every action maps to an existing relation; every `can_*` relation has an action).
2. Routes declare their action (`dependencies=[Depends(require(TableAction.READ_DATA))]`); `_action_relation`
   and its defaults are deleted; an undeclared guarded route raises at import. A test iterates `app.routes`
   under `/v1` and `/management/v1` and fails on any route without a declaration — the Python stand-in for
   Lakekeeper's exhaustive match.
3. The audit line carries the action name + context (`force`, `purge`, properties keys), Lakekeeper's
   `ActionDescriptor.log_string()` (mod.rs:311-331), plus the relation.
4. LH-077: authorize `alter_transaction` per distinct state action in one batch (Lakekeeper's
   `require_table_actions` pattern, table.rs:783-850) so `can_set_property`/`can_cancel` get producers; if the
   owner prefers one check, delete the relations — the reference keeps no producer-less relation.
5. CP-004 ruling answer from the reference: execution rights are actions on what the job touches
   (`can_get_tasks`/`can_control_tasks` on table/warehouse/project), not a zone/job/run type.
6. LOW-028 answer from the reference: it has no opaque-asset type; its one non-table type is a table-shaped
   sibling. `table:` stands unless the model registry needs a rung a table lacks.

Rows: XC-020, LH-077, CP-004, LOW-028, LH-076 (§8), **new** ("route→relation mapping has a writer default").

---------------------------------------------------------------------------------------------------------
## 8. Hierarchy and built-in roles: server/estate → project → warehouse → namespace → table

### Lakekeeper
- `server`: `admin` (manage projects/users, grant admin; NO data — "the admin can assign himself as
  project_admin … visible in the audit log") and `operator` (machines; everything) with per-purpose actions
  `can_create_project`, `can_list_all_projects`, `can_list_users`, `can_provision_users`, `can_update_users`,
  `can_delete_users`, `can_read_assignments`, `can_grant_admin`, `can_grant_operator`
  (`authz/openfga/v4.7/components/server.fga:1-34`; store test "admin … can_get_metadata: false" on a
  warehouse, `v4.7/store.fga.yaml:207-229`).
- `project`: `project_admin` (lock-out protection "Checked to never be empty"; `or operator from server`),
  `security_admin` (grants, not data), `data_admin` (data, not grants), `role_creator`
  (project.fga:9-19). They REACH the resources: `warehouse.manage_grants: … or security_admin from project`,
  `warehouse.modify: … or modify from project or data_admin from project`, `project.create: … or data_admin`,
  `project.describe: … or data_admin or security_admin` (warehouse.fga:16-20; project.fga:23-26).
  v4.7 tightened `can_grant_data_admin` to `security_admin` (README.md:47).
- Bottom-up navigation to the top: `project.can_get_metadata: describe or can_get_metadata from warehouse or
  admin from server`, `warehouse.can_get_metadata: describe or can_get_metadata from namespace`
  (project.fga:36; warehouse.fga:28) — test "Select Table 3 bubbles list up" (store.fga.yaml:753-800).
- Every edge is written in both directions (server↔project, project↔warehouse, warehouse↔namespace,
  namespace↔namespace, namespace↔table) (tuples.rs:40-225).
- Instance admins (config list of `<idp>~<sub>`, e.g. `kubernetes~system:serviceaccount:lakekeeper:operator`)
  bypass CONTROL-plane authz only: not data-plane, not role assumption, and not the permission-management
  endpoints — "keeps a leaked operator credential from being trivially used either to exfiltrate data or to
  escalate arbitrary principals to admin" (`docs/docs/authorization.md:18-114`;
  `crates/lakekeeper/src/service/authz/instance_admin.rs:1-99`; bypass predicate table.rs:966-975).

### rask
- `estate` (admin/operator/owner/writer/reader/event_stager; `can_observe_events`, `can_browse_storage`,
  `can_stage_events`, `can_create_project`) → `project` → `warehouse` → `namespace` → `table`
  (model.fga:102-195, 59-98, 197-420, 423-518, 520-630). Estate owner does not reach tenant data (warehouse
  owner is `… or admin from project`, model.fga:205) — parity with Lakekeeper's server admin.
- **The admin split is decorative.** `security_admin`, `data_admin`, `role_creator` are defined
  (model.fga:65-73) and appear in no other relation (`grep "security_admin from|data_admin from"` → none)
  and in no service code (`grep -rn security_admin|data_admin|role_creator services packages --include=*.py`
  outside tests → none). Granting `security_admin` confers no grant power on any warehouse; `data_admin`
  confers no data power. Only the model tests pin that they exist (model.fga.yaml:206-225).
- `can_observe_events` IS the estate-admin rung and gates tenant minting, stores, the raw tuple editor,
  lineage's estate projection (model.fga:143-168, LH-076).
- No project-level upward visibility: `project` has no `can_get_metadata` and no `warehouse` inverse edge, so
  a table grantee outside the project's members cannot see the project (`/v1/me` lists projects by
  `admin`/`member` relations, me.py:80-92).

### What rask should do
1. Admin split: either wire it the Lakekeeper way — `warehouse.manage_grants: … or security_admin from
   project`, a data-steward path for `data_admin` (rask's `can_drop` is `owner`, which also carries
   `manage_grants`, so `data_admin` needs a lifecycle rung that is not ownership: e.g. warehouse
   `steward: [..] or owner or data_admin from project`, with `can_drop/can_delete/can_restore` deriving from
   `steward` one rung down), `project.can_create_role: role_creator` with a role-create door (CTL-019) — or
   delete the three relations. Owner decision; the relations as shipped claim a separation of duties that does
   not exist.
2. LH-076: do not rename `can_observe_events`, SPLIT it into per-purpose actions on `estate` exactly like
   `server.fga`: `can_read_events`, `can_create_project`, `can_list_all_projects`, `can_manage_stores`,
   `can_read_assignments`, `can_grant_admin`, `can_grant_operator` — each `: owner` (or `admin`), each call
   site repointed. Computed usersets, so no tuple migrates (LH-076's own measurement).
3. Add `project.can_get_metadata: member or can_get_metadata from warehouse` plus the inverse
   `project:P warehouse warehouse:W` edge so bottom-up browsing reaches the tenant.
4. Machine identities stay RELATIONS (rask's `estate.operator` + narrow rungs `maintainer`/`publisher`/
   `event_stager`, model.fga:129-141, 206-251) — rask is already stricter than Lakekeeper's all-powerful
   `operator`. With D1 the subject becomes `user:kubernetes~system:serviceaccount:<ns>:<sa>` (url-encoded).

Rows: CTL-019, LH-076, **new** ("the project admin split reaches nothing"), **new** ("no upward visibility to
the project").

---------------------------------------------------------------------------------------------------------
## 9. Grant writes, grant reads, managed access, and the raw tuple editor

### Lakekeeper
- Every grant write goes through `checked_write`: each relation being written or deleted maps to its
  `can_grant_*` (`GrantableRelation::grant_relation`, relations.rs:522-535, 827-839, 1158-1170, 1435-1445) and
  the actor must hold ALL of them on the object; writes+deletes then go in one OpenFGA Write
  (`crates/authz-openfga/src/api.rs:2545-2618`). Anonymous refused; grants by an ASSUMED role on
  namespace/table/view objects refused "as we are missing public usersets for managed access"
  (api.rs:2565-2578); self-assignment of a role refused (api.rs:2261-2270). Every write emits an authz event
  (`event_ctx.emit_authz`, api.rs:2280, 2037-2057).
- Reading grants: `can_read_assignments` = the union of the object's `can_grant_*` — "Only if we can GRANT a
  privilege, we can LIST them" (warehouse.fga:46-47; lakekeeper_table.fga:27). Inspecting ANOTHER principal's
  permissions requires `can_read_assignments` on each object, appended as guard checks to the same batch
  (`check_actions_with_permission_guard`, authorizer.rs:1238-1276; guards built at 411-419, 445-462,
  488-505, 533-549, 584-597).
- Managed access: `managed_access: [user:*, role:*]` flag, inherited down; removes `ownership`'s grant power
  (namespace.fga:11-16; api.rs:2620-2681 writes both wildcard tuples).
- There is NO raw-tuple write surface; instance admins cannot touch permission endpoints (authorization.md:56-65).

### rask
- Per-object grant doors authorize per rung from the body (`_authorize_grant`, fga_deps.py:501+;
  `tests/unit/test_fga_model_contract.py:384-420`); managed access ported, including the warehouse-rung fix
  (model.fga:278-331) — parity or better.
- `access/list`, `access/check`, `access/graph` gate at `can_drop`/`can_delete` (fga_deps.py:207-226,
  273-294), not at the defined-but-unused `can_read_assignments: manage_grants` (model.fga:416, 517, 630).
- **Raw tuple editor:** `POST /v1/access/tuples` and `DELETE /v1/access/tuples` write/delete ANY directly
  assignable tuple on ANY object for a holder of `estate#can_observe_events`
  (rask: services/catalog/src/catalog/api/v1/endpoints/access_admin.py:1-17, 199, 290-348). It bypasses every
  `can_grant_*` rule and managed access (e.g. `user:x owner warehouse:<tenant>`). The model's own comment lists
  "reading and MUTATING the authorization graph itself" among what the estate rung grants (model.fga:152-153).

### What rask should do
1. Delete the raw WRITE/DELETE tuple routes; keep the read-only diagnostics (`GET /tuples`, `/model`,
   `/check`, `/list-objects`, `/expand`, `/simulate`) behind a separate `estate#can_read_assignments`.
   Estate-level grants (estate admin/operator) get their own checked doors (`can_grant_admin`,
   `can_grant_operator` — Lakekeeper server.fga:32-34). Break-glass = the bootstrap Job re-run, as
   Lakekeeper's `reopen-bootstrap` (authorization-openfga.md:126-149).
2. Gate `access/list|graph|check` on `can_read_assignments`, defined as the union of the object's
   `can_grant_*` (a pass-grants holder sees whom they may grant to).
3. Keep one Write per grant change (writes + deletes together) — already true for single grants; make it true
   for rename (§3).

Rows: LH-076, XC-020 (can_read_assignments has no producer), **new** ("the estate admin can write any tuple").

---------------------------------------------------------------------------------------------------------
## 10. Principal key and subject lifecycle

### Lakekeeper
- `UserId` = `<idp_id>~<subject>`, url-encoded into `user:` (entities.rs:156-189; a pre-0.9 id still parses,
  entities.rs:324-337). Service accounts via the Kubernetes authenticator: `kubernetes~system:serviceaccount:<ns>:<sa>`
  (authorization.md:79-84).
- Anonymous = `user:*` (entities.rs:191-196); an assumed role acts as `role:<id>#assignee` (entities.rs:197-201),
  gated by `role.can_assume` (authorizer.rs:119-132).
- `delete_user` → `delete_all_relations(user)` (authorizer.rs:739-745), by-user sweep over every object type
  (authorizer.rs:1437-1495).
- Role assignments are stored in OpenFGA (`ManagesRoleAssignments`, `crates/lakekeeper/src/service/authz/mod.rs:195-248`;
  authorizer.rs:926-1095) with higher-consistency listing for read-after-write (authorizer.rs:1030-1043).

### rask
- FGA subject = bare `token.sub` (LH-063; rask: packages/service-kit/src/service_kit/governed/deps.py:181,208).
- No subject-wide revoke door; revoke is by object only (§3).

### What rask should do (D5 + D1 decided)
- Subject key `user:` + urlencode(`<idp-id>~<claim>`) for humans (idp id per Dex connector/issuer alias) and
  `user:` + urlencode(`kubernetes~system:serviceaccount:<ns>:<sa>`) for machines (D1). One helper in
  `service_kit.governed.fga` builds it; `check`/`list_objects` stop prepending `user:` to a bare sub
  (fga.py:832, 962).
- Re-key migration as the version-gated function in the model Job (§1.5), one pass, no dual keys (owner rule).
- `DELETE /management/v1/principals/{id}` → `revoke_subject_tuples` (§3.4) + `grant_revoked` control events
  per removed tuple (notifications' GRANT_REVOKED already exists).

Rows: LH-063 (both findings), D5, D1.

---------------------------------------------------------------------------------------------------------
## 11. Consistency preferences and caching (resilience)

### Lakekeeper
- Default Check consistency `MinimizeLatency` (client.rs:58-72); HigherConsistency for: the create guard and
  create writes after a staged overwrite (authorizer.rs:709-727, 866-880), deletes (authorizer.rs:1437-1504),
  role-assignment listings (authorizer.rs:1030-1043), migration and reconcile deletes (migration_fns_v4.rs:95-97;
  reconcile.rs:633-635).
- Recommends OpenFGA caches for medium/large estates: `OPENFGA_CACHE_CONTROLLER_ENABLED`,
  `OPENFGA_CHECK_QUERY_CACHE_ENABLED`, `OPENFGA_CHECK_ITERATOR_CACHE_ENABLED`, connection pool sizing
  (authorization-openfga.md:83-96); the chart also sets `OPENFGA_LIST_OBJECTS_ITERATOR_CACHE_ENABLED`
  (lakekeeper-charts/charts/lakekeeper/values.yaml:612-619).
- Health = a real Check against a random project (health.rs:13-35).

### rask
- No consistency preference anywhere (`grep -i consistency fga.py` → none); no OpenFGA cache settings in
  the subchart values (chart/values.yaml:2958-3035 read; the rest of the block not read). The estate's own comment attributes 91 OpenFGA
  OOMKills to a per-table single-Check storm from the cascade-lag tick across ~90 warehouses
  (chart/values.yaml:3003-3013).

### What rask should do
- Batch that tick's checks (§5.3's pair-based batch_check). Enable the check-query cache only together with
  `HIGHER_CONSISTENCY` on the read-after-write paths (create→seed→check, revoke-on-create, grant listing,
  reconcile), mirroring Lakekeeper's list above — enabling the cache alone would make a just-seeded owner fail
  their first check.

Rows: **new** (resilience criterion 5); LH-183 is a different pod (maintenance native memory) — not this.

---------------------------------------------------------------------------------------------------------
## 12. Branches (data point for LH-056 / D3)

- Lakekeeper has no branch type; every table mutation, ref changes included, is one `Commit` action →
  `can_commit: modify` (`crates/lakekeeper/src/server/tables.rs:1317-1345`; lakekeeper_table.fga:24).
- rask: `can_create_branch: owner`, `branches/delete: can_drop`, tags `owner or publisher`
  (model.fga:586-598; fga_deps.py:169-206). D3's recommended (a) — a table writer writes every branch,
  creation stays owner — matches the reference on "no branch principal" and is stricter than it on creation.
  Both are defensible; the reference does not argue for a per-branch rung.

Rows: LH-056, D3.

---------------------------------------------------------------------------------------------------------
## 13. Where rask is ahead (keep)

- Time-boxed grants (`non_expired_grant` condition, model.fga:775-777; clock defaulted in every read,
  fga.py:771-791) — Lakekeeper has none.
- Narrow machine rungs (`maintainer`, `publisher`, `classifier`, `event_stager`) and "a machine does not own
  what it registers" (fga_deps.py:1188-1200) — Lakekeeper's `operator` is all-powerful.
- Fail-closed retry wrapper with a single retry layer (fga.py:1-25, 737-768).
- Managed access fixed at the warehouse rung and on `pass_grants` (model.fga:278-331) — Lakekeeper's warehouse
  `pass_grants` has no managed-access subtraction (warehouse.fga:15).
- Full revoke of ghosts (§4) — necessary on name keys.

---------------------------------------------------------------------------------------------------------
## Files

READ IN FULL (Lakekeeper): authz/openfga/README.md; authz/openfga/v4.7/fga.mod + all 10 components;
authz/openfga/v4.0 and v3.4 components (via full `diff -r` against v4.7/v4.0, plus both fga.mod);
authz/openfga/v2.1/schema.fga; crates/authz-openfga/Cargo.toml, src/lib.rs, src/models.rs, src/config.rs,
src/client.rs, src/health.rs, src/migration.rs, src/entities.rs; the non-test parts of src/tuples.rs (1-247),
src/reconcile.rs (1-845), src/authorizer.rs (1-1554), src/migration/migration_fns_v4.rs (1-483);
crates/lakekeeper/src/service/authz/instance_admin.rs, decision.rs; docs/docs/authorization.md,
docs/docs/authorization-openfga.md.
SKIMMED (Lakekeeper): authz/openfga/v4.7/store.fga.yaml (~70%), crates/authz-openfga/src/relations.rs (1-1014
read, rest by grep of every mapping), src/check.rs (1-560), src/api.rs (header, 962-1060, 2024-2110,
2246-2683), src/error.rs (130-260); crates/lakekeeper/src/service/authz/mod.rs (1-1850 except 1467-1519;
tests not read), table.rs (1-420, 570-700, 915-1010), namespace.rs (20-160, 280-440), project.rs (95-206),
server.rs (240-294), warehouse/view/generic_table/role/user/allow_all.rs (grep only); server/namespace.rs,
server/tables.rs, server/tabular.rs, server/mod.rs (authz call sites); lakekeeper-bin authorizer.rs/main.rs;
authz/opa-bridge (README + check.rego grep); the Lakekeeper chart (values.yaml openfga/migration blocks,
templates/db-migration.yaml 1-80).
rask: read write_model.py, openfga-model.yaml, openfga-migrate.yaml, docs/AUTHZ.md in full; the rest by
section as cited.

NOT CHECKED: `openfga-client` 0.6 `TupleModelManager` source (not on host); Lakekeeper integration tests
(`openfga_rename_tabular.rs` etc. are cited via findings_lance_lakekeeper.md); authorization-cedar.md and
opa.md; OPA policies beyond grep; v2.1/v3.4/v4.0 store.fga.yaml; rask's lineage/viewer/notifications/
ingest/medallion batch_check callers' completeness handling; whether pylance 12's DirectoryNamespace honours
a non-`$` delimiter in `__manifest`; whether lance-ns forbids `$` inside a segment; `on_missing_deletes`
against the live v1.18.3 server; which lock primitive rask's maintenance could use for delete-drift; nothing
here was driven against the cluster.
