# The twenty control-root record kinds: facts from the code at HEAD

Read at `main` fc3af60d3 (2026-11-26), read-only. Every claim cites `path:line`. Abbreviations:
`L/` = `packages/lakehouse/src/lakehouse/`, `C/` = `services/catalog/src/catalog/`, `E/` = `C/api/v1/endpoints/`,
`M/` = `services/maintenance/src/maintenance/`, `N/` = `services/lineage/src/lineage/`. Route paths come from each
endpoint's decorator plus its router's `prefix=` (e.g. `E/tables.py:116,123`). "(inferred)" marks what was concluded rather
than read. Sizes are `wc -l` at HEAD.

## 0. Shared primitives (every section below uses them)

- Conditional writes, `L/storage/conditional_records.py` (368 lines): `create_json` (put-if-absent) :64, `read_json_at`
  (record + ETag + the store's `Date` clock) :159, `read_json` :200, `mutate_json` (read, apply, ETag replace, retried) :206,
  `exchange_json` :236, `replace_json` (If-Match) :278. There is no conditional delete.
- Unconditional writes, `L/records/record_store.py` (257 lines): `record_key(prefix, kind, id)` = `<prefix>/<kind>-<24 hex of
  sha256("kind:id")>.json` :82-96; `put_record` (overwrite) :98; `delete_record` (no condition) :176; `read_listing` :194
  (one non-recursive `FileSelector` over the prefix, then one GET per `.json`, all in memory, no cursor :209-229);
  `list_records` (skips unreadable) :232; `list_every_record` (raises on unreadable) :246.
- `L/records/object_records.py:37-45`: `end_object_records` deletes, by the object's uuid, protection, policy, history fence,
  commit turn, commit mark, `_labels`, `_contracts`; for a table also commit authors, file identities, cadence stamps
  (`_policies/state/<uuid>/`) and branch trash (:48-68).
- Announcement filing, `C/api/announcing.py:186-221` (`Announcer.announcing`): LIST of `_announcement_intents/<target hash>/`
  (:206), take over earlier filings (:209), `create_json` of a new intent (:212), the door's block, then `settle` (delete) (:221).
  Every send goes through `_send` (:478-495): a control event (`ControlEmitter.emit`) and/or a lineage event, each staged in
  its outbox before publish and deleted after (section 20).
- Every request on a registered (non-estate) warehouse reads that warehouse's record once to route it
  (`C/api/dependencies.py:108,120,130-155`).

---

## 1. Location claim (`_locations/`)

**Fields.** `LocationClaim` `L/records/location_claims.py:153-172`: `location: str`, `table: str` (canonical id),
`segments: list[str]`, `uuid: str|None`, `root: str|None` (set on a data-directory claim), `claimed_at: datetime`,
`token: str|None`, `released_at: datetime|None`, `fence: ClaimFence|None` (`token: str`, `at: datetime`, :145-150).
Key `_locations/<sha256 of decoded location>.json` (:216-222). Data-directory claims share the prefix (`take_directory` :566).
Never deleted: release is a replace stamping `released_at` (:494-534).

**Written by** (all conditional: create_json / replace_json / mutate_json):
- `take` :326-384 (create or ETag replace, 8 rounds :135) via `C/services/table_claims.py:779-792` and `claim_new_table`
  :795-817. Callers: create/declare (`C/services/tables/creation.py:257`), re-take of an existing table (`creation.py:499`),
  register admission (`C/services/tables/registering.py:266`), undrop (`registering.take_the_location_back` :341-357, from
  `E/tables.py:1385`), rename hand-back (`C/services/tables/rename.py:265`).
- `hand_over` :403-437 (rename) via `table_claims.hand_over` :700 from `rename.py:230`.
- `fence` :440-468 via `table_claims.fence` :619 / `fence_listed` :649: drop (`C/services/tables/retirement.py:154`),
  deregister (`retirement.py:327`), cascade table step (`C/services/namespace_subtrees.py:264`), warehouse detach
  (`C/services/warehouse_removal.py:107`), clone-ending finish (`C/services/clone_endings.py:226`).
- `lift_fence` :471-487 (mutate_json): `retirement.py:105,244,249,303,341`, `rename.py:180`, `namespace_subtrees.py:280`,
  `warehouse_removal.py:132,138`, `clone_endings.py:238`.
- `release` :494 (ETag replace): `creation.py:352,738` (undo), `retirement.py:357` (deregister), `retirement.py:310`
  (destroy, inferred from offset 255+56), `warehouse_removal.py:149`, `clone_endings.py:234`, `trash.end`
  (`L/records/trash.py:500`).
- `take_directory` :566 / `release_directory` :610 via `L/records/base_registry.py:451,463,390` (section 2).

**Read by.**
- catalog: `table_claims.resolve` reads the claim on every table-addressed request (`table_claims.py:96-118`);
  `DoorTable.require_held` re-reads it before every open (:173-185) and `require_described_at` before native calls (:196-204);
  `holder_is_gone`/`holder_stands` (:728-765); `held_claims` LIST in the hourly commit walk
  (`C/api/commit_reconcile.py:275`); `every_claim` LIST in the grant census/ghost doors (`C/services/grant_reconcile.py:39`)
  and erasure surfaces (`C/services/erasure_derived.py:256`); reclaim doors (`C/services/trash_reclaim.py:47`). Also imported by
  36 catalog modules (import list; not each call site checked).
- maintenance, **direct store reads, no door**: `M/services/audit.py:433` (`held_claims`, a LIST), `M/services/table_pass.py:335,
  841`, `M/services/orphan_check.py:104`, `M/services/trash_task.py:70`, `M/services/arrival.py:202`, `M/services/store_events.py:175`,
  `M/services/naming.py:97` (uuid by location), `M/services/reconcile.py:904,923`.
- lineage: none found.

**Changes together with:** see paths in list A (create, register, drop, deregister, rename, undrop, cascades, warehouse delete,
clone-ending finish, trash end). A claim is the first write of create (`creation.py:257`) and register.

**Leases and clocks.** `LEASE = 15 min` (:127), `DOOR_MARGIN = 1 min` (:131); fence lapses after `LEASE` (:179,186); judged on
the store's `Date` header (`store_now` :279-282; `take` reads first "so even a create is stamped on the store's clock" :365).
Announcement intents reuse this lease (`C/services/announcement_intents.py:50`).

**What exists only to protect it.** `L/records/location_claims.py` (620) and `C/services/table_claims.py` (835) themselves;
`kept_in_trash` (`table_claims.py:767-776`); `grant_reconcile.claim_refusal` (`C/services/grant_reconcile.py`, 90);
maintenance audit `_queue_what_holds_nothing` (`M/services/audit.py`, 433) queues work off held claims; the drift report
(`M/services/reconcile.py`, 1,342).

**How often.** Written on every create, declare, register, drop, deregister, rename, undrop, cascade step, warehouse detach and
clone-ending finish. Not written by a data commit. Read at least twice per data commit (`resolve` + `require_held`), more with
each `require_named`.

## 2. Base record and data-directory claims (`_bases/`)

**Fields.** `BaseRecord` `L/records/base_registry.py:165-172`: `location: str`, `entries: list[RecordedBase]`;
`RecordedBase` :101-125: `path`, `store`, `role: BaseRole`, `is_dataset_root`, `name`, `origin: BaseOrigin`, `source_table`,
`tag`, `version`, `branch`. Key `_bases/table-<digest of decoded location>.json` (:229-231). Data-directory claims are
`LocationClaim`s under `_locations/` with `root` set (section 1).

**Written by.**
- `claim_bases` :273-308 (create_json, else mutate_json; 3 rounds :80): Arrow create with data (`creation.py:677`), register
  re-converge (`registering.py:223`), register admission (`C/services/table_bases.py:718`).
- `claim_data_directories` :429-456 (conditional, via `take_directory`): `creation.py:676`, `table_bases.py:830`.
- `release_claim` :335-357 (unconditional `delete_record` of a record it created, else mutate_json): undo in
  `creation.py:735`, `table_bases.py:810`.
- `forget_base_record` :360-392 (releases directory claims, deletes clone tags, then unconditional delete): destroy
  (`retirement.py:309`), cascade (`namespace_subtrees.py:453`), warehouse detach (`warehouse_removal.py:143`), clone-ending
  finish (`clone_endings.py:232`), `trash.end` (`L/records/trash.py:498`).
- `release_clone_sources` :395 (Lance tag deletes on sources, record kept): deregister (`retirement.py:349`), warehouse detach
  (`warehouse_removal.py:145`).

**Read by.**
- catalog: `version/create` judge via `C/core/base_judge.py:106`; `C/services/clone_endings.py:119,259`;
  `data_directories.directories_to_end` in destroy (`C/services/tables/destroy.py:176`) and reclaim vend
  (`C/services/trash_reclaim.py:160,202`); imported also by `core/vending.py`, `client_fragments.py`, `managed_versions.py`.
- maintenance, **direct, no door**: `M/services/table_pass.py:164,334,515` (`read_base_record`, data directories, base judge);
  `M/services/purge.py:560,715,1018` (`directories_to_end`, `BaseRegistry`); `M/services/audit.py:226` (tombstone probe).
- lineage: none.

**Changes together with:** create (with data), register, destroy, deregister, warehouse detach, clone finish, trash end (list A).

**Leases and clocks.** Directory claims carry the location `LEASE` (`location_claims.py:600`). The record has none.

**What exists only to protect it.** `release_claim` (fail-closed undo) :335; `_release_what_the_write_took` in
`creation.py` (773; release at :735-738); `release_admitted` in `C/services/table_bases.py` (839); `L/records/data_directories.py`
(132).

**How often.** Create (Arrow with data) and register; ended on destroy, deregister of a clone, trash end. Read on every
`version/create` judge (inferred: every stock commit through `base_judge`) and every maintenance table pass.

## 3. Bucket claim (`_warehouses/bucket-claims/`)

**Fields.** Untyped dict `{"bucket", "project", "warehouse_id"}` (`C/services/warehouses.py:384-385`), validated on read as
`BucketClaimRecord` (`C/services/control_records.py:40`). Key `_warehouses/bucket-claims/<bucket>.json`
(`warehouses.py:384`).

**Written by.** `claim_bucket` `warehouses.py:365-397` (create_json; an existing claim of the same project passes) from
warehouse create (`E/warehouses.py:316`, in `_claim_the_bucket`, `E/warehouses.py:219`). `release_bucket_claim` :400-415
(lists every warehouse record, then **unconditional** delete) from warehouse delete with `purge_bucket`
(`E/warehouses.py:1100-1101`).

**Read by.**
- catalog: `warehouses.bucket_claim` :352 via the base judge / governed storage (`C/api/dependencies.py:291,296`;
  `C/core/base_judge.py:81-82`).
- maintenance, **direct**: `L/records/warehouse_records.py:202-210` `bucket_project`, called from
  `maintenance_policies.governing_policy` (`L/records/maintenance_policies.py:322`) in `M/services/table_pass.py:782`.
- lineage: none.

**Changes together with:** warehouse create, warehouse delete (list A).

**Leases.** None. **Protect-only code.** The LIST-then-delete guard in `release_bucket_claim` (`warehouses.py:412-415`).

**How often.** Warehouse create and warehouse delete with purge only.

## 4. Project record (`_projects/`)

**Fields.** `ProjectRecord` `L/records/project_records.py:28-47`: `id`, `deleting_by`, `created_at`, `created_by`,
`protected`, `protected_by`, `managed_by` (all `str|None` but `id`). Key `_projects/<id>.json` (`C/services/projects.py:150-152`).

**Written by** (`C/services/projects.py`): `create_project_record` :140 (create_json), `upsert_project` :68 (mutate_json),
`set_project_protection` :105, `set_project_manager` :127, `mark_deleting` :175 (all mutate_json), `delete_project_record`
:192 (**unconditional** delete). Callers: create door `E/projects.py:221,203` (via `establish_project` :254); protection door
`E/projects.py:520`; delete door `E/projects.py:558,724`; declaration converge tick `C/api/declaration_converge.py:116,122,125,181`.

**Read by.**
- catalog: `get_project` in `C/api/authz/hierarchy.py:290`, `E/roles.py:71`, `E/labels.py:262`, `E/policies.py:331`,
  `E/projects.py:190,269,406,438,584`, `C/services/grant_reconcile.py:73`, `C/api/declaration_converge.py:113,308`; listings
  `projects.read_projects` :199 (LIST) from `E/projects.py` (`_load_tenants`, offset 180+11), converge :179,212.
- maintenance, **direct**: drift report LIST `M/services/reconcile.py:784-791` (`read_listing` :364).
- lineage: none.

**Changes together with:** create project, delete project (list A).

**Leases.** None; `deleting_by` is an unbounded intent mark (`projects.py:175-190`).

**Protect-only code.** `mark_deleting`; declaration converge (`C/api/declaration_converge.py`, 353); drift repair of
`ghost_projects` (`M/services/repair.py`, 213); tuple rebuild (`M/services/rebuild.py`, 172).

**How often.** Administrative acts only (create, protect, delete, converge on drift). The converge tick reads/lists every
window (`declaration_converge.py:179`).

## 5. Warehouse record (`_warehouses/`)

**Fields.** `WarehouseRecord` `L/records/warehouse_records.py:82-111`: `id`, `bucket`, `project`, `root_uri` (required),
`endpoint`, `delete_mode`, `trash_grace_days`, `status`, `protected`, `protected_by`, `managed_by`. Key `_warehouses/<id>.json`
(:47-50).

**Written by** (`C/services/warehouses.py`): `create_warehouse_record` :266 (create_json), `upsert_warehouse` :191
(exchange_json), `set_warehouse_protection` :147, `set_warehouse_manager` :178, `set_warehouse_status` :436 (mutate_json),
`delete_warehouse_record` :465 (**unconditional**). Callers: `E/warehouses.py:426,449` (create, in `_write_record`),
`:565` (activate/deactivate), `:711` (protection), `:1103` (delete), converge `C/api/declaration_converge.py:151,161,165,169,186`.

**Read by.**
- catalog: routing every request on a registered warehouse (`C/api/dependencies.py:150`); trash grace
  (`C/services/trash_doors.py:56`); `C/api/warehouse_grant_backfill.py:82` (boot); `C/services/manifest_reclaim.py:51` (timer);
  `E/policies.py:294,396`, `E/labels.py:89`, `E/branches.py:197`, `E/grant_reconcile.py:99`, `C/api/authz/object_grants.py:661`,
  `C/api/authz/grant_authority.py:281`, `E/namespaces.py:297,386`, `E/tables.py:161`, `E/projects.py:113,188,548`,
  `E/warehouses.py` (13 sites).
- maintenance, **direct**: `M/services/reconcile.py:805-813` (LIST), `M/services/trash_release.py:114` (LIST),
  `M/services/bucket_events.py:72` (LIST), `M/services/purge.py:307` (LIST), `M/services/table_pass.py:750` (by key).
- lineage: none.

**Changes together with:** create warehouse, delete warehouse (list A).

**Leases.** None. **Protect-only code.** Record-last ordering of the delete (`E/warehouses.py:1103`); boot backfill
(`C/api/warehouse_grant_backfill.py`, 96); converge (353); rebuild (`M/services/rebuild.py`, 172).

**How often.** Administrative acts; read on every request addressed to a registered warehouse.

## 6. Roles (`_roles/`)

**Fields.** `RoleRecord` `C/schemas.py:1002-1011`: `id: UUID`, `project`, `name`, `description`, `created_at`, `created_by`;
stored as `StoredRole` with `ended_at: datetime|None` (`C/services/roles.py:42-45`). Key `_roles/<project>/<blake2s(name)>.json`
(`roles.py:58-59`).

**Written by.** `create_role_record` `roles.py:62-80` (create_json, or replace_json over an ended one) from `E/roles.py:125`.
`end_role_record` :142-165 (replace_json stamping `ended_at`; soft end) from `C/services/role_grants.py:54,63`, reached from
delete role (`E/roles.py:160`) and project delete (`E/projects.py:707-713`).

**Read by.** catalog only: `find_role` lists the project's roles and scans (`roles.py:133-135`) from `E/roles.py:85,207`,
`C/api/announcing.py:358`; `list_roles` `E/roles.py:148`; `every_role` `E/projects.py:707`. maintenance, lineage: none.

**Changes together with.** Create role: (1) FGA write of the role→project edge `E/roles.py` (offset 97+~20), (2) role record,
(3) on failure FGA delete, (4) control event (outbox). Delete role: intent filed, FGA revokes, then `end_role_record`, sends.

**Leases.** None. **Protect-only.** The FGA edge compensation in `E/roles.py` create. **How often.** Administrative.

## 7. Principals (`_principals/`)

**Fields.** `PrincipalRecord` `C/schemas.py:179-187`: `principal`, `kind: person|service`, `deleted_at`, `deleted_by`.
Key `_principals/<blake2s(principal)>.json` (`C/services/principals.py:24-26`).

**Written by.** `record_deletion` `principals.py:35-50` (create_json; an existing record is returned) from
`DELETE /management/v1/access/principals/{principal}` (`E/access_admin.py:774`).

**Read by.** catalog `GET /management/v1/access/principals/{principal}` only (`E/access_admin.py:783`). No authorization path
reads it (grep of `principals.` across `C/`, `M/`, `N/`). maintenance, lineage: none.

**Changes together with.** `E/access_admin.py:740-780`: (1) read tuples naming the principal, (2) intent filed,
(3) revoke every naming tuple in FGA, (4) principal record, (5) send, (6) settle intent.

**Leases/protect-only.** None. **How often.** Offboarding only.

## 8. Declared grants (`_declared_grants/`)

**Fields.** `GrantTuple` `C/services/declaration.py:90-97`: `user`, `relation`, `object`. Key
`_declared_grants/<sha256 of [user, relation, object]>.json` (:189-191).

**Written by.** `record_grant` :206-211 (create_json, before the tuple) and `forget_grant` :214-216 (**unconditional** delete),
only from the converge tick: `C/api/declaration_converge.py:280` (record), `:229,268,300` (forget).

**Read by.** `recorded_grants` (LIST, strict) `declaration.py:194-203` from `declaration_converge.py:204`. Nothing else.

**Changes together with.** Converge `_grant` :271-288: (1) project/warehouse record read, (2) FGA read, (3) grant record,
(4) intent filed, (5) FGA write, (6) send. `_revoke` :291-300: intent, FGA delete, send, then forget the record.

**Leases.** Runs once a timer window (`catalog-declaration-converge`, `C/api/timers.py:56`). **Protect-only.** The record itself
exists so the converge revokes only what it wrote (`declaration_converge.py:279`). **How often.** Converge ticks, on drift.

## 9. Protection (`_protection/`)

**Fields.** `ProtectionRecord` `L/records/protection.py:60-78`: `kind`, `id`, `uuid`, `protected: bool`, `set_by`. Key
`record_key("_protection", kind, uuid)` (:115). (Project and warehouse protection live in their own records, sections 4-5.)

**Written by** (all **unconditional**): `set_protection` :113 (put_record) and `clear_protection` :138 (delete). Callers:
table protection door `E/tables.py:1463,1465`; namespace protection door `E/namespaces.py:994,996`; recoverable drop clears it
(`retirement.py:252`); namespace drop clears per object (`E/namespaces.py:726,729`); boot creation of the provenance namespace
(`C/services/provenance_namespace.py:159`); `end_object_records` (`object_records.py:38`).

**Read by.** catalog: `C/services/protections.py:18` from `C/api/authz/hierarchy.py:263,275,324` (every drop, deregister,
rename, cascade check) and the GET doors `E/tables.py:1306`, `E/namespaces.py:969`. maintenance, lineage: none.

**Changes together with.** Protection doors: (1) record, (2) control event (`E/tables.py:1467`, `E/namespaces.py:997`).

**Leases/protect-only.** None. **How often.** Administrative; read on each destructive door.

## 10. Trash (`_trash/`)

**Fields.** `TrashRecord` `L/records/trash.py:110-159`: `kind`, `id`, `uuid`, `location`, `dropped_by`, `dropped_at`, `drop`,
`detached`, `expires_at`, `properties`, `namespace_uuid`, `cascade`, `residue`, `released_at`, `attempts`, `last_refusal`.
Key `record_key("_trash", kind, uuid)` (:92-94).

**Written by.**
- `put` :350-358 (**unconditional**): recoverable drop `retirement.py:229`; destroy residue `destroy.py:127`; cascade table
  `namespace_subtrees.py:277`, cascade namespace `:229`.
- `stamp_detached` :361-388 (create_json/replace_json): `retirement.py:63,69,96`, finish `C/api/announcing.py:394`.
- `withdraw` :391-416 (read, compare, **unconditional** delete under the fence): `retirement.py:98`, `announcing.py:457`.
- `clear` :477 (**unconditional**): undrop `E/tables.py:1413`, namespace undrop `E/namespaces.py:927,960`, destroy
  `destroy.py:192`, `retirement.py:382`.
- `mark_released` :328 (mutate_json) via `C/services/trash_reclaim.py:166` from release door `E/trash_reclaim.py:53`.
- `note_refusal` :504 (mutate_json) via `trash_reclaim.py:171` from `E/trash_reclaim.py:184,145-147`.
- `end` :481-501 (object records, base record, claim release, then `clear`) from `E/trash_reclaim.py:175` and
  `C/services/warehouse_removal.py:230`.

**Read by.**
- catalog: `trash.get` on every `DoorTable.require_held` through `kept_in_trash` (`table_claims.py:183,775`) i.e. every open of
  every commit; `trash.named` (LIST of the whole prefix, filtered) in `Resolver.trashed` (`C/api/dependencies.py:412`) used by
  undrop, tasks and transaction doors (`E/tables.py:1327,1421`, `E/namespaces.py:762,865`, `E/transactions.py:32`);
  `every_record` LIST in namespace undrop (`E/namespaces.py:808`), `trashed_objects` (`C/api/authz/objects.py:226`), warehouse
  trash (`warehouses.py:493`); `keeps` in `E/labels.py:296`, `grant_reconcile.py:77`; `retirement.py:57,380`;
  `announcing.py:379`; `trash_reclaim.py:66`.
- maintenance, **direct**: `M/services/audit.py:375` (LIST), `M/services/trash_release.py:184` (LIST), `M/services/purge.py:216`
  (LIST), `M/services/reconcile.py:827-839` (LIST), `M/services/trash_task.py:41`, `M/services/table_pass.py:851`,
  `M/services/orphan_check.py:107`, `M/services/expiry.py:477`. Writes go through catalog doors (release, refusals, end,
  credentials: `M/services/credentials.py:750`).
- lineage: none.

**Changes together with:** drop, destroy, cascade, undrop, release, end, warehouse delete (list A).

**Leases and clocks.** `expires_at` stamped at drop from the warehouse grace (`make_record` :282-322; `trash_doors.drop_grace_days`
reads the warehouse record). `RELEASE_SETTLE = 15 min` after the deadline (:325, `release_at` :237-240). Judged on the catalog
pod's clock (inferred: `now or datetime.now(UTC)`, :333; doors pass `clock()`). The drop's fence uses the claim lease (section 1).

**What exists only to protect it.** `stamp_detached`/`withdraw`; `retirement._settle_a_failed_drop` (`retirement.py`, 386);
the finish path in `C/api/announcing.py` (638); release task and backstop `M/services/trash_task.py` (73),
`M/services/trash_release.py` (230); purge `M/services/purge.py` (1,031); tombstone sweep `M/services/tombstones.py` (148);
reclaim doors `E/trash_reclaim.py` (271), `C/services/trash_reclaim.py` (203).

**How often.** Every drop/cascade/undrop; release/refusal/end per record per maintenance task. Read on every commit open.

## 11. Branch trash (`_branch_trash/`)

**Fields.** `HiddenBranch` `L/records/branch_trash.py:48-70`: `name`, `location`, `reference` (the ref file's text),
`parent_branch`, `parent_version`, `identifier`, `tag`, `dropped_by`, `dropped_at`, `expires_at`, `purging: bool`. Key
`_branch_trash/<table uuid>/<sha256(name)>.json` (:82-83).

**Written by** (all **unconditional**): `file` :86 (put_record) from `hidden_branches.hide` (`C/services/tables/hidden_branches.py:71`,
branch delete `E/branches.py:138` via `C/services/tables/refs.py:429,492`) and `purge` marking `purging` (:114); `end` :153
(delete) from `restore` (:94, `E/branches.py:240` via `refs.py:514,522`) and `purge` (:120); `end_table` :162 (LIST + deletes)
from `end_object_records`. `purge_all` runs on deregister (`E/tables.py:948`) and erasure (`E/erasure.py:180`).

**Read by.** catalog: `DoorTable.hidden_branch` / `is_hidden` on an open naming a branch (`table_claims.py:373,388,396`);
`hidden_branches` LIST of the table's prefix (`table_claims.py:406`, used at `refs.py:352,458,516,531`); `of_every_table`
LIST of the whole prefix in the `catalog-branch-trash` tick (`C/api/branch_trash.py:52`). maintenance, lineage: none.

**Changes together with.** Branch delete, inside the history fence (`refs.py:429`): (1) branch-trash record, (2) Lance tag
`rask.trash.<hash>` (`hidden_branches.py:72-74`), (3) delete `_refs/branches/<name>.json` (:75). Purge (tick): history fence
held (:112), record re-filed `purging`, ref file written back, Lance branch delete, tag delete, record end (:111-120).

**Leases and clocks.** `expires_at` (deadline, `past_deadline`); history fence `HOLD` 15 min (design doc; `L/records/history_fences.py`).

**Protect-only.** `purging` flag; tick `C/api/branch_trash.py` (68); `hidden_branches.py` (148). **How often.** Branch delete,
restore, deregister, erasure; tick per window.

## 12. Maintenance policy (`_policies/`)

**Fields.** `PolicyRecord` `L/records/maintenance_policies.py:120-140` (`extra="allow"`): `kind`, `id`, `uuid`, `buckets`,
`location`, plus settings parsed as `PolicySettings` :94-117 (`retention_days`, `retain_versions`, `cleanup_enabled`,
`compact_enabled`, `optimize_indices_enabled`, `compact_interval_hours`, `target_rows_per_fragment`). Key
`record_key("_policies", kind, key)` (:220).

**Written by.** `set_policy` :202-238 (create_json / replace_json, 5 rounds; **unconditional** `put_record` when the stored body
is not JSON :224-226) from `E/policies.py:120` (table/namespace/project set doors) and `provenance_namespace.py:117`.
`delete_policy` :252 (**unconditional**) from `E/policies.py:150`, project delete `E/projects.py:720`,
`provenance_namespace.py:133`, `end_object_records`.

**Read by.** catalog: `E/policies.py:139` (describe), `:400` (`list_policies` LIST); vend door `E/credentials.py:87`
(`governing_policy`). maintenance, **direct**: `governing_policy` by key per table pass `M/services/table_pass.py:782`
(root `settings.resolved_policy_root`). lineage: none.

**Changes together with.** Set/delete doors: record then control event (`E/policies.py:152,183,223,313`). Project delete (list A).

**Leases/protect-only.** None. **How often.** Administrative; read on every maintenance pass and every maintain vend.

## 13. Cadence stamp (`_policies/state/`) and maintenance due (`_maintenance_due/`)

**Fields.** `CadenceStamp` `L/records/maintenance_policies.py:396-404`: `policy`, `last_maintained_at`; key
`_policies/state/<table uuid>/cadence-<digest of uri>.json` (:382-390). `DueRecord` `L/records/maintenance_due.py:72-87`:
`table`, `uuid`, `location`, `version`, `compaction: CompactionDue|None`, `indices: list[IndexDue]`, `policy`, `evaluated_at`;
key `record_key("_maintenance_due", "table", uuid)` (:90-92).

**Written by** — **maintenance, directly, no door**:
- cadence: `write_state` :433 (**unconditional** put) from `M/services/table_pass.py:610`; ended by catalog
  `end_table_state` :442 (LIST + deletes) via `end_object_records`.
- due: `write_due` :105 (**unconditional** put) from `M/services/due.py:144`; `delete_due` :110 (**unconditional**) from
  `M/services/due.py:142`, `M/services/table_pass.py:455,844,853`. Not in `OBJECT_RECORD_PREFIXES`, so no catalog door ends it.

**Read by.** cadence: maintenance `read_state` :412 from `M/services/plan.py:132`. due: catalog
`GET /management/v1/maintenance/due` (`E/maintenance.py:161,165`: LIST of keys, then reads per page). lineage: none.

**Changes together with.** Each write is one record inside a maintenance table pass; no other record kind in the same step.

**Leases.** None (the task claim is a NATS KV entry, not a control-root record). **How often.** Every maintenance table pass.

## 14. Data contract (`_contracts/`)

**Fields.** `ContractRecord` `C/services/contracts.py:257-265`: `table_uuid`, `document: dict` (the ODCS document; parsed as
`Contract` :97-113), `set_by`, `set_at`. Key `record_key("_contracts", "table", uuid)` (:271-272).

**Written by.** `keep` :332-359 (create_json or mutate_json, 3 rounds :329) from set door `E/contracts.py:107`; `restore`
:388-393 (**unconditional** put or delete, the take-back) from `E/contracts.py:116`; `forget` :396-398 (**unconditional**) from
delete door `E/contracts.py:136`; `end_object_records` (:44).

**Read by.** catalog: every committing door through `DoorTable.contract` (`table_claims.py:447-458`), in-process commits and
`version/create`; describe door `E/contracts.py:50,56`. maintenance, lineage: none.

**Changes together with.** Set: (1) parse, (2) check refs, (3) read standing, (4) `keep`, (5) re-check, (6) maybe `restore`,
(7) control event (`E/contracts.py:101-118`). Single record kind.

**Leases.** None. **Protect-only.** `restore` (`contracts.py`, 398). **How often.** Administrative writes; read on every commit.

## 15. Label definitions and attachments (`_label_definitions/`, `_labels/`)

**Fields.** `LabelDefinition` `C/schemas.py:840-856`: `uuid`, `project`, `name`, `description`, `scope`, `kind`,
`allowed_values`, `defined_by`, `defined_at`; key `_label_definitions/<project>/<blake2s(name)>.json`
(`C/services/object_labels.py:83-84`). `Attachment` `object_labels.py:59-75`: `kind`, `uuid`, `id`, `project`, `location`,
`labels: dict[str,str|None]`, `definitions`, `set_by`; key `record_key("_labels", kind, uuid)` (:87-88).

**Written by.** `define` :159 (create_json / exchange_json) from `E/labels.py:329`; `delete_definition` :285 (**unconditional**)
from `E/labels.py:358`; `end_definitions` :290-303 (LIST + unconditional deletes) from project delete `E/projects.py:723`;
`attach` :348-379 (create_json / mutate_json) from `E/labels.py:149`; `forget` :386 (**unconditional**) `E/labels.py:153`;
`detach` :391-411 (mutate_json) from `E/labels.py:173`; `end_object_records` (:43).

**Read by.** catalog label doors only: `definition` :195, `definitions` :206 (LIST), `attachments` :217 (LIST of all of
`_labels/` filtered by project), `carriers` :239 (strict LIST), `attachment_of` :318. No commit path reads `_labels/` (grep of
`object_labels.`; the commit path's "labels" are column classifications, `C/services/tables/commits.py:42,143`).
maintenance, lineage: none.

**Changes together with.** Each door: one record, then control event (`E/labels.py:155,179,334,359`).

**Leases/protect-only.** None. **How often.** Administrative.

## 16. Erasure obligations and releases (`_erasure_obligations/`, `_erasure_releases/`)

**Fields.** `ErasureObligation` `C/services/erasure_obligations.py:99-129`: `obligation_id`, `table`, `table_uuid`, `object_id`,
`location`, `predicate_digest`, `retain_days`, `refs: dict[str,int]`, `requested_by`, `requested_at`, `deadline`, `follows`,
`state`, `verified_at`, `open_surfaces`, `released_at`, `release`, `late_derived`; key :173. `ReleaseRecord`
`C/services/erasure_release.py:74-96`: `release_id`, `table`, `table_uuid`, `location`, `requested_by`, `notified`, `refs`,
`due_at`, `phase`, `lease_until`, `tags`, `plan`, `last_error`; key :126-127.

**Written by.** Obligation: `record` :177-179 (create_json) from `POST …/erasure` (`E/erasure.py:145`); `settle` :287 (mutate_json)
from verify (`E/erasure.py:267`); late-derived append (mutate_json, :266) from `erasure_derived.watch_released` in the commit
reconcile tick (`C/api/commit_reconcile.py:410`). Release: `schedule` :155-161 (create_json) from verify (`E/erasure.py:374`);
claim/phase `_mutate` :329 and `_hand_back` :336 (mutate_json) and `_finish` :348 (**unconditional** delete) in the
`catalog-erasure-release` tick (`C/api/erasure_release.py:42-66`).

**Read by.** catalog: `list_obligations` (LIST) in `E/erasure.py:294`, `erasure_derived.py:96,122`; `open_on` :208 and
`stands_on` :170 (LISTs) from branch create (`E/branches.py:185`); `release_due` LIST :198 in the tick. maintenance, lineage: none.

**Changes together with.** Erase: (1) Lance config disarm on every ref (`E/erasure.py:129`), (2) obligation record (:145),
(3) hidden-branch purge (branch trash, :167). Verify: (1) obligation read, (2) hidden-branch purge (:235), (3) Lance verify and
tag drops, (4) obligation settle (:267), (5) release schedule (:374), control events. Release tick: history fence held, record
claim (mutate), Lance tag/branch deletes and recreates, record delete (`erasure_release.py:226-247`).

**Leases and clocks.** Release `LEASE = 15 min` (:57), `lease_until` judged against the tick's `now` (:222-223; pod clock,
inferred from `ctx.clock()`). Obligation `deadline` (requester-supplied window).

**Protect-only.** `_hand_back`; `watch_released` (`C/services/erasure_derived.py`, 284). **How often.** Erasure requests and verifies; the tick lists releases each window; the reconcile tick lists obligations each window.

## 17. Clone endings (`_clone_endings/`)

**Fields.** `CloneEnding(EndingIntent)` `C/services/clone_ending_records.py:55-90`: `location`, `table`, `uuid`, `sources`,
`forget_record`, `door`, `filing`, `filed_at`, `finishing: EndingMark|None` (`token`, `at`). Key
`_clone_endings/<location digest>/<filing>.json` (:104-111).

**Written by.** `file` :113-123 (create_json) via `clone_endings.file_ending` (`C/services/clone_endings.py:108-128`) from drop
destroy (`retirement.py:271`), deregister (`retirement.py:335`), cascade (`namespace_subtrees.py:418`), warehouse detach
(`warehouse_removal.py:116`), and register admission (`table_bases.py:754`). `mark` :170 (replace_json) from finish
(`clone_endings.py:186`). `remove` :161-167 (**unconditional** delete) from `clone_endings.py:135,224,235,262,265`,
`table_bases.py:774,85`.

**Read by.** catalog: `stands_at` LIST of one digest's prefix on every register (`clone_endings.py:151` from
`registering.py:283`); `standing` LIST + reads in `GET /management/v1/clone-endings` (`clone_endings.py:156-162`); finish
`read` (:177). maintenance asks the doors (`M/services/clone_endings.py`, `M/services/credentials.py:762`). lineage: none.

**Changes together with.** Finish (`clone_endings.py:204-238`): (1) claim read, (2) fence claim, (3) Lance tag releases on sources,
(4) forget base record, (5) release claim, (6) remove ending, (7) lift fence.

**Leases.** Fence on the claim uses the claim lease. **Protect-only.** The kind is itself a repair intent: `clone_endings.py`
(281), `clone_ending_records.py` (182), `E/clone_endings.py` (76), `M/services/clone_endings.py` (54).
**How often.** Only on ends/registrations of clones (tables whose base record has `derived_from` entries).

## 18. Idempotency keys (`_idempotency/`)

**Fields.** Discriminated union `C/services/idempotency_records.py:123-164`: `_Claimed` (`state`, `binding: KeyBinding`,
`digest`, `attempt`, `claimed_at`, `captured`, `intent`), `_Answered` (`state=done`, `binding`, `digest`, `status`, `body`,
`captured`), `_Reclaiming` (`state`, `at`). Key `_idempotency/<scope>/<key>.json` (:217-222).

**Written by** (all conditional except the reclaim delete): `claim` :232 (create_json/replace_json) via `C/api/idempotency.py:343`;
`intend` :308 (replace_json) via `idempotency.py:264`; `release` :349 via :284; `record_outcome` :373 via :304; `reclaim`
:403-461 (replace_json to `reclaiming`, then **unconditional** `delete_object` :461) from
`POST /management/v1/idempotency-keys/reclaim` (`E/idempotency_keys.py:52`), which maintenance's daily scan calls up to 50 times
(`M/services/audit.py:127,135`). Doors that take a key (only when the request carries `Idempotency-Key`): create table
(`E/data.py:186`), insert/merge/update/delete (`E/data.py:259,408,469,512`), declare, drop, deregister, register, undrop, rename,
restore (`E/tables.py:335,820,922,1039,1362,1527,1607`), namespace create/drop/undrop (`E/namespaces.py:114,464,800`), project
delete (`E/projects.py:621`), warehouse delete (`E/warehouses.py:830`).

**Read by.** catalog doors only. maintenance asks the reclaim door. lineage: none.

**Changes together with.** First write of every keyed path (list A); `intend` carries a drop's evidence (`E/tables.py:856`).

**Leases and clocks.** `LEASE = 300 s` (:75); lifetime `LANCE_IDEMPOTENCY_LIFETIME_SECONDS` default 86,400 s
(`C/core/config.py:658`); reclaim judged on the listing's/read's store clock (`idempotency_records.py:420,454`).

**Protect-only.** `reclaim` and its door (`E/idempotency_keys.py`, 62); `C/api/idempotency.py` (492). **How often.** Every keyed
request (optional header), including keyed data commits.

## 19. Timer claims (`_timers/`)

**Fields.** `_Claim` `L/timers.py:104-112`: `timer`, `window`, `claimed_at`, `by`. Key `_timers/<name>.json` (:127-129).

**Written by.** `Timer._ask` (create_json / replace_json on the store clock) `L/timers.py:235-265`. Catalog: seven timers on
`settings.registry_root` (`C/api/timers.py:48-56,73-80`). **Lineage**: `lineage-relay` on `LINEAGE_TIMER_ROOT`
(`N/api/relay.py:126,147-155`), which the chart sets to the same bucket (`chart/templates/services.yaml:275`). Maintenance runs
no `Timer` (its schedules are NATS message schedules; grep of `Timer(`).

**Read by.** The same timer loops only. **Changes together with.** Nothing; one record per window. **Clock.** Store `Date`
(`L/timers.py:11-13`). **How often.** Every window of every timer with a period.

## 20. Announcement intents and the two outboxes

**Fields.** Intent `C/services/announcement_intents.py:231` (`door`, `target`, `announcements: list[Announcement]`; each
`Announcement` = `evidence` (one of `TupleGone`, `TupleHeld`, `NameResolves`, `Unregistered`, `NamespaceResolves`, `RecordGone`,
`RecordHeld`, `RoleEnded`, `TrashedUnder`, :55-162) + `message` (`ControlMessage` :171 or `LineageMarker` :180)); key
`_announcement_intents/<sha256(target)[:32]>/<uuid7 hex>.json` (:261-272). Outbox objects: the signed CloudEvent envelope at
`<outbox uri>/<envelope id>.json` (`L/events/outbox.py:84-120`); URIs `s3://<bucket>/_lineage_outbox` and `_control_outbox`
(`chart/templates/services.yaml:108,116`, `chart/templates/maintenance.yaml:30`).

**Written by.**
- Intents: `file` (create_json) and `settle` (unconditional `remove_object`) from `Announcer.announcing`
  (`C/api/announcing.py:212,221`); finish removes (`announcing.py:289,324`) from `POST /management/v1/announcement-intents/finish`
  (`E/announcement_intents.py:35-56`, limit 200), called by maintenance's audit up to 20 times (`M/services/audit.py:255,264`).
  Doors that file: drop, deregister, register, undrop, rename (`E/tables.py:857,942,1070,1384,1569`), namespace drop
  (`E/namespaces.py:477`), project create/delete (`E/projects.py:290,643`), role delete and project role ends
  (`E/roles.py` delete; `E/projects.py:709`), access grant/revoke and managed-access (`E/access.py:542,850`), members
  (`E/members.py:142,189`), principal delete (`E/access_admin.py:740-780`), estate tuple write/delete (`E/access_admin.py:437`), trash release (`E/trash_reclaim.py:87`), warehouse
  delete (`E/warehouses.py:842`), declaration converge grants (`C/api/declaration_converge.py:280-299`). Not filed by create
  table, namespace create, warehouse create, contract/label/policy/protection doors (they emit directly).
- Lineage outbox: staged (plain put) and dropped (plain delete) by the catalog's lineage emitter (`C/main.py:387`) and by
  **maintenance**'s lineage emitter (`M/runtime.py:128`); drained (read + delete) by **lineage**'s relay (`N/api/relay.py:232,
  300,320,349`); paged by lineage's DLQ door (`N/api/v1/endpoints/dlq.py:146`).
- Control outbox: staged/dropped by the catalog's control emitter (`C/main.py:418`); drained by the catalog's
  `catalog-control-relay` (`C/api/control_relay.py`). Maintenance stages nothing on it (`M/runtime.py:143-146`).

**Read by.** Intents: catalog only (`announcement_intents.standing`, a LIST of the target's prefix on every filing
`announcing.py:206`; a LIST of the whole prefix on every finish `announcing.py:260`, `announcement_intents.py:304-341`).
Lineage outbox: lineage (relay, DLQ). Control outbox: catalog relay.

**Changes together with.** By construction every filing door (above) does: intent create → its own writes → sends (outbox put,
publish, outbox delete per event) → intent delete.

**Leases and clocks.** Intent lease = claim `LEASE` 15 min (`announcement_intents.py:50`), age judged on the listing's store
clock (`announcement_intents.py:333-336`).

**Protect-only.** `C/api/announcing.py` (638), `C/services/announcement_intents.py` (356), `E/announcement_intents.py` (66);
`L/events/outbox.py` (458), `L/events/outbox_metrics.py` (256), `C/api/control_relay.py` (360), `N/api/relay.py` (361),
`N/api/v1/endpoints/dlq.py` (319).

**How often.** Intents: every filing door. Lineage outbox: one put + one delete per lineage event, so on every data commit when
`services.lineage.outbox.enabled` (default true, `chart/values.yaml:167-169`). Control outbox: every control event when
`catalog.controlEmit` (true, `chart/values.yaml:635`) and `controlOutbox.enabled` (true, :652-654).

---

## A. Request paths that write two or more of the twenty kinds (in order)

Outbox staging (kind 20) accompanies every event send; it is shown once per path as "sends". FGA = OpenFGA tuple writes;
`__manifest` = namespace backend calls.

1. **Create table** `POST /v1/table/{id}/create` (`E/data.py:111`; `C/api/table_create.py:153`; `creation.py`):
   1. idempotency claim (optional) `E/data.py:186`
   2. location claim `creation.py:257`
   3. FGA grants before the row `creation.py:263` → `table_create.py:241`
   4. `.lance-reserved` marker `creation.py:264`
   5. `__manifest` `register_table` `creation.py:265`
   6. parent re-check `creation.py:268`
   7. data-directory claims, base record `creation.py:676-677`
   8. Lance `write_dataset` `creation.py:687`
   9. FGA take-back unless kept `table_create.py:284`; lineage send `:396`; control event `:424`; idempotency remember.
2. **Insert/merge/update/delete (keyed)** `E/data.py:217,357,444,493`: idempotency claim → claim + trash reads → contract read →
   Lance commit → idempotency remember → lineage send (outbox). Unkeyed: only the lineage outbox.
3. **Register** `POST /v1/table/{id}/register` (`E/tables.py:986-1083`): idempotency `:1039` → FGA seed (`:1119`) →
   `__manifest` `register_table` (`:1126`) → parent check → location claim (`registering.py:266`) → clone-endings LIST
   (`registering.py:283`) → clone ending filed, Lance tags on sources, directory claims, base record (`table_bases.py:710-721`)
   → intent filed (`E/tables.py:1070`) → FGA ownership (`:1252`) → clone ending removed (`table_bases.py:774`) → sends →
   idempotency remember.
4. **Drop into trash** `POST /v1/table/{id}/drop` (`E/tables.py:796-884`): idempotency `:820` → protection read → idempotency
   intend `:856` → intent filed `:857` → claim fence `retirement.py:154` → trash put `:229` → `__manifest` `deregister_table` `:233`
   → trash stamp `:235` → lift fence `:244/249` → protection clear `:252` → sends → intent delete → idempotency remember. No FGA.
5. **Destroying drop** (same door, `purge`/grace 0; `retirement.py:255-316`, `destroy.py`): idempotency → intent → fence →
   clone ending filed (`retirement.py:271`) → residue trash put (`destroy.py:127`) → `__manifest` deregister (`destroy.py:155`)
   → bytes and directories deleted → trash clear (`destroy.py:192`) → base record forgotten (directory claims, clone tags)
   `retirement.py:309` → claim release `:310` → clone ending clear `:312` → `end_object_records` (protection, policy, labels,
   contract, cadence, branch trash) `:315` → send → FGA revoke (`E/tables.py:880`) → send.
6. **Deregister** `POST /v1/table/{id}/deregister` (`E/tables.py:892-962`): idempotency `:922` → intent `:942` → fence
   (`retirement.py:327`) → clone ending filed (`:335`) → `__manifest` deregister (`:337`) → lift/settle trash (`:340-343`) → clone
   tags released (`:349`) → claim release (`:357`) → clone ending clear (`:359`) → hidden branches purged (`E/tables.py:948`) →
   `end_object_records` (`:951`) → send → FGA revoke (`:957`) → send.
7. **Undrop table** (`E/tables.py:1331-1418`): idempotency `:1362` → trash LIST (`dependencies.py:412`) → intent `:1384` →
   claim take back `:1385` → `__manifest` register `:1387` → send → trash clear `:1413` → send.
8. **Rename** (`E/tables.py:1479-1590`, `rename.py`): idempotency `:1527` → protection read → intent `:1569` → claim hand-over
   (`rename.py:154,230`) → FGA parent move (`rename.py:244`) → `__manifest` deregister (`:168`) → `__manifest` register (`:273`)
   → FGA arrive (`:175`) → lift fence (`:180`) → sends.
9. **Namespace drop, recoverable cascade** (`E/namespaces.py:409-546`, `namespace_subtrees.py:145-280`): idempotency `:464` →
   protection reads over the subtree → intent `:477` → per table: fence, trash put, `__manifest` deregister, lift
   (`namespace_subtrees.py:264-280`) → per namespace: `__manifest` drop, trash put (`:216-229`) → protection clears
   (`E/namespaces.py:726,729`) → send. **Destroying cascade**: per table path 5, then `end_removed_subtree` (records, then FGA
   revoke, `object_grants.py:690`).
10. **Namespace undrop** (`E/namespaces.py:766-962`): idempotency `:800` → trash LIST `:808` → `__manifest` create namespaces →
    claim take-back and register per table → trash clear per record `:927,960`.
11. **Warehouse create** (`E/warehouses.py:139`, `establish_warehouse` :192): warehouse LIST `:268` → bucket claim `:316` →
    bucket provision `:221` → store probe `:222` → warehouse record `:226` → `__manifest` row `:231` → FGA seed `:233/235` →
    control event `:237`.
12. **Warehouse delete** (`E/warehouses.py:771`, `_cascade_the_delete` :1074): idempotency `:830` → intent `:842` → per trash
    record: FGA revoke, directories, `trash.end` (object records, base record, claim release, trash delete;
    `warehouse_removal.py:229-230`) → per table: fence, clone ending, directories, `__manifest` deregister, lift, base record,
    clone tags, claim release, clone ending clear (`warehouse_removal.py:106-151`) → `__manifest` drop namespaces → records then FGA
    per object → `__manifest` warehouse row `:1086` → FGA on warehouse `:1094` → bucket purge `:1098` → bucket claim release
    `:1100` → warehouse record delete `:1103`.
13. **Project create** (`E/projects.py:231`, `establish_project` :254): intent `:290` → project record `:297` → FGA admin
    `:298` → sends.
14. **Project delete** (`E/projects.py:563`): idempotency `:621` → warehouse LIST, `mark_deleting` (`:548,558`) → intent `:643`
    → FGA revoke all on project `:649` → per role: intent, FGA revokes, role record ended (`:707-713`) → FGA revoke again `:654`
    → policy delete `:720` → label definitions LIST + delete `:723` → project record delete `:724` → sends → idempotency
    remember.
15. **Role delete** (`E/roles.py:160`): intent → FGA revokes → role record ended (`role_grants.py:63`) → sends.
16. **Principal delete** (`E/access_admin.py:740`): intent → FGA revokes → principal record → send.
17. **Trash release** (`E/trash_reclaim.py:53`): trash read → intents LIST (`_unannounced_refusal`) → claim read → intent
    filed `:87` → FGA revoke `:88` → sends → trash `mark_released` `:90`; a refusal writes `note_refusal`.
18. **Trash end** (`E/trash_reclaim.py:159`): trash + claim reads → FGA revoke `:174` → `trash.end` `:175`: object records
    (protection, policy, labels, contract, cadence, branch trash), base record + directory claims + clone tags, claim release,
    trash delete.
19. **Clone-ending finish** (`E/clone_endings.py:49`): see section 17 (claim fence/release, base record, clone ending).
20. **Erasure** `POST …/erasure` (`E/erasure.py:91`): obligation record → branch-trash purge (record, Lance branch, tag).
    **Verify** (`E/erasure.py:186`): branch-trash purge → obligation settle → release schedule → control events.
21. **Declaration converge** tick (`C/api/declaration_converge.py:91-98`): project record writes (via `establish_project`,
    path 13) and warehouse record writes (via `establish_warehouse`, path 11), declared-grant record → intent → FGA → send.
22. **Branch delete** (`E/branches.py:138`): history fence (not one of the twenty) + branch-trash record + Lance tag + ref
    delete. Only one of the twenty (branch trash) plus outbox.

## B. Listings of a record kind (LIST of a prefix, then reads)

No listing pages through the store with a cursor held across calls: every one lists the whole prefix in one call
(`read_listing` :209, `stored_objects.list_under` / `list_under_at`), then reads. Pages offered to callers are cut after the
full listing (marked "page after full LIST").

| Kind | Caller | Paging |
| --- | --- | --- |
| Location claims (held) | `M/services/audit.py:433`; `C/api/commit_reconcile.py:275` (hourly walk) | none |
| Location claims (all, strict) | `C/services/grant_reconcile.py:39`; `C/services/erasure_derived.py:256` | none |
| Projects | `E/projects.py` `_load_tenants` (via `list_projects` `C/services/projects.py:226`); converge `C/api/declaration_converge.py:179,212`; `M/services/reconcile.py:784` | none |
| Warehouses | `C/services/warehouses.py:286-345` (`list`/`every`/`read`) from create `E/warehouses.py:268`, `release_bucket_claim` :413, project delete `E/projects.py:548`, backfill, manifest reclaim, converge :184, policies; maintenance `reconcile.py:805`, `trash_release.py:114`, `bucket_events.py:72`, `purge.py:307` | none |
| Roles of a project | `roles.find_role` / `list_roles` / `every_role` (`C/services/roles.py:83-135`) | none |
| Declared grants | `declaration.recorded_grants` :194 (each converge tick) | none |
| Trash | `trash.named` (`C/api/dependencies.py:412`; whole prefix for one name); `every_record` `E/namespaces.py:808`, `C/api/authz/objects.py:226`, `C/services/warehouses.py:493` (page after full LIST, `E/warehouses.py:483-521`); maintenance `audit.py:375`, `trash_release.py:184`, `purge.py:216`, `reconcile.py:827` | none / page after full LIST |
| Branch trash | per table `branch_trash.of_table` (`table_claims.py:406`); whole prefix `of_every_table` (`C/api/branch_trash.py:52`); `end_table` | none |
| Policies | `list_policies` `E/policies.py:400` | none |
| Cadence stamps of a table | `end_table_state` (`L/records/maintenance_policies.py:442`) | none |
| Due records | `E/maintenance.py:161-165`: LIST of keys, records read per page | page (reads only the page) |
| Label definitions | `definitions` `object_labels.py:206` (`E/labels.py:97,310`); `end_definitions` :290 | none |
| Label attachments | `attachments` :217 (whole `_labels/`, filtered by project; `E/labels.py:385`, page after full LIST); `carriers` :239 | page after full LIST |
| Erasure obligations | `list_obligations` (`E/erasure.py:294`, page after full LIST; `erasure_derived.py:96,122`); `open_on` (`E/branches.py:185`) | page after full LIST |
| Erasure releases | `release_due` :198 (tick); `stands_on` :170 (branch create) | none |
| Clone endings | `standing` (`C/services/clone_endings.py:156-162`); `stands_at` per register (`registering.py:283`) | none |
| Idempotency | `reclaim` `C/services/idempotency_records.py:419-420` | deletes ≤ limit after full LIST |
| Announcement intents | per target on every filing `announcing.py:206`; whole prefix on finish `announcing.py:260` | ≤ 200 read after full LIST, cursor `after` |
| Lineage outbox | `N/api/relay.py:232` (`list_events`, limit); `N/api/v1/endpoints/dlq.py:146` (`page_events`, cursor) | limit / cursor |
| Control outbox | `C/api/control_relay.py` (inferred: same `outbox.list_events`) | limit (inferred) |

## C. Kinds written by a process other than the catalog

| Kind | Writer | Where |
| --- | --- | --- |
| 13 Cadence stamp | maintenance (unconditional put) | `M/services/table_pass.py:610` |
| 13 Maintenance due | maintenance (put and delete) | `M/services/due.py:142,144`; `M/services/table_pass.py:455,844,853` |
| 19 Timer claim | lineage (`lineage-relay`) | `N/api/relay.py:147-155` |
| 20 Lineage outbox | maintenance (stages lineage events); lineage (deletes after draining) | `M/runtime.py:128`; `N/api/relay.py:300,320,349` |

Every other write by maintenance to the twenty goes through a catalog door (`M/services/credentials.py:597,629,714,728,750,762,780`).

## D. `rename_table`

- The catalog never calls the backend's `rename_table`: the only spellings are the door name and the request model
  (`E/tables.py:35,1479`); the move is `ns.deregister_table` (`C/services/tables/rename.py:168`) then `ns.register_table`
  (`rename.py:273`), with a re-register of the source as compensation (`rename.py:281`).
- Measured on pylance 14.0.3 (`uv run --no-sync python -`, script
  in the measuring session's scratch directory, not kept),
  local temp directories, `DirectoryNamespace(root=…, inline_optimization_enabled="false", dir_listing_enabled="false")` (the
  two properties rask sets, `C/core/config.py:236-250`; built as in `C/core/namespace.py:84`), one table created, eight threads
  behind a barrier each renaming it to `dst<i>`, four rounds: **every call raised**
  `NotImplementedError: Not supported: rename_table not implemented, …/rust/lance-namespace/src/namespace.rs:292:13`, in all
  four rounds; afterwards exactly one name (`src`) resolved to the dataset in each round. The same error with default
  properties (one extra call). So the race could not be measured: the `dir` backend on 14.0.3 has no rename.

## E. Python database libraries in `uv.lock`

psycopg, psycopg-binary, psycopg-pool, psycopg2, asyncpg, sqlalchemy, sqlmodel, alembic: **all absent**
(`grep -ciE "psycopg|asyncpg|sqlalchemy|sqlmodel|alembic" uv.lock` = 0; 152 packages locked).

## Could not confirm

- **Clocks of trash and erasure deadlines**: that `expires_at`, `RELEASE_SETTLE` and the erasure release `lease_until` are judged
  on the pod's clock is inferred from `datetime.now(UTC)` defaults and `clock()`/`ctx.clock()` arguments; the clock providers
  were not read.
- **Whether pyarrow's `get_file_info` pages the S3 listing internally** (it must, for prefixes over 1,000 keys): not read; the
  caller sees one in-memory list (`L/records/record_store.py:209`).
- **Maintenance's store permissions**: that maintenance writes `_policies/state/`, `_maintenance_due/` and `_lineage_outbox/`
  with its own credential is read from the code; the RustFS/S3 policy granting it was not read.
- **Every catalog reader of location claims**: listed by import (36 modules) and by the main call sites; each call site was not
  opened.
- **Control relay listing** (`C/api/control_relay.py`): not opened; assumed to use `outbox.list_events` like lineage.
- **A few line numbers were computed from grep offsets inside a function** rather than printed on their own (e.g.
  `E/tables.py:1252`, `E/roles.py` create steps, `E/projects.py` `_load_tenants`); the rest were printed by grep.
- **Chart periods per timer**: not mapped timer-by-timer; only `controlOutbox.periodSeconds: 60` (`chart/values.yaml:659`) was
  read in context.
- **Rates**: no write rate was measured; "how often" lists the doors that write.
- **`rebuild.py`'s source of project/warehouse records**: assumed to be the drift report's listing (it imports only the models,
  `M/services/rebuild.py:51-52`).
