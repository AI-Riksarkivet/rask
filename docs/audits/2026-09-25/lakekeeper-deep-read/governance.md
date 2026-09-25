# Lakekeeper deep-read — GOVERNANCE + LIFECYCLE + STATE → how rask should solve it on its own stack

Label: `lakekeeper/governance` · 2026-09-25 · READ-ONLY on the rask repo, the cluster was used for `get`/`logs` only.

Reference checkout: `/home/gabriel/Desktop/lakekeeper-ref` at `a58e40171f95` (2026-06-22).
rask: the working tree at `/home/gabriel/Desktop/rask`, including the staged pylance-12 change.
Measurements: pylance **12.0.0** / lance-namespace **0.11.1**, run from `/home/gabriel/Desktop/rask/.venv/bin/python`
(the interpreter directly, so no `uv sync` touched the repo). Probe tables live under
`…/reaudit/lakekeeper/probe/`. Paths below are relative to the two repo roots:
`LK:` = lakekeeper-ref, `R:` = rask.

---

## 0. Summary of what matters (ranked)

| # | Topic | Verdict | Rows |
|---|---|---|---|
| 1 | **Expiry and purge are one step in rask. Lakekeeper splits them into two queues.** So one orphan `.txn` blocks every drop in the estate from finishing. Measured live today. | Split them. Expiry (revoke tuples, free the name) always runs, per record. Byte purge stays gated. | LH-099, LH-073, LH-178, new |
| 2 | **Name vs identity.** Lakekeeper keys every governance fact on a UUID. rask keys every fact on the name path. That one choice causes the rename grant loss, the forced-rename protection leak, the name lock and LH-150. | Measured: the dir backend gives each create a **fresh random location prefix**, and a rask rename keeps that location. So the location already works as a per-incarnation id. Two options: A, migrate facts on rename. B, key tables on the incarnation. | LH-063, LH-150, LH-144, new ×2 |
| 3 | Rename drops every direct grant. Lakekeeper keeps them. | Move the direct tuples to the destination in the rename, or take option B. | new |
| 4 | Protection record is left behind on a forced rename. | Keep one list of name-keyed stores. Rename migrates them and destroy clears them. | new, XC-017 |
| 5 | **The task surface is rask-only (`/management/v1/.../tasks`). The spec already has one.** `DropTableRequest` says: *"return a transaction ID that client can use to track deletion progress"*. | Return `transaction_id` on a recoverable drop, answer `DescribeTransaction` from the trash record, and make `AlterTransaction`→`Canceled` the undrop. That also gives `can_cancel` a real door. Needs an owner call. | LH-077, XC-020, FE-005 |
| 6 | Warehouse delete clears trash records **without revoking the trashed objects' tuples**. Lakekeeper refuses the delete until the expirations have run. | Run the expiry step (revoke) for each trashed object before clearing, or refuse with 409. | new, LH-144, LH-016 |
| 7 | The delete profile is one estate-wide number. Lakekeeper sets it per warehouse. | Put `delete_profile` on the warehouse record: the same sub-resource shape LH-074 ruled for quota. | new (LH-074 shape) |
| 8 | Idempotency records are never reclaimed. They store response bodies and do not advertise a lifetime. | Add a reclaim pass older than lifetime+grace (Lakekeeper uses 30m+5m), fold `_idempotency` into the control-prefix list, and document re-execution after a crash. | new |
| 9 | Failed-seed unwind (LH-194) | Lakekeeper avoids the problem: its machine owns what it creates, and it compensates in-request with a guard. rask's ruling forbids the first. The nearest spec-shaped answer is `DeclareTable` plus reaping by age. | LH-194 |
| 10 | State that Lakekeeper keeps and Lance makes unnecessary | Task table, entity versions, moka caches, `deleted_at`, the `metadata_location` CAS. None of these should be ported. | LH-050, XC-011 |

---

## 1. Soft-delete / undrop / tabular expiration — expiry vs purge

### What Lakekeeper does

- **The delete profile belongs to the warehouse.** `TabularDeleteProfile::{Hard{}, Soft{expiration_seconds}}`
  (`LK:crates/lakekeeper/src/api/management/v1/warehouse/mod.rs:137-186`) is stored on the warehouse row.
  The DB default is `soft` and the column cannot be null (`LK:crates/lakekeeper-storage-postgres/migrations/20240909120857_configurable_drops.sql:1-12`).
  The create-warehouse API default is `Hard {}` (`warehouse/mod.rs:111-114,182-186`). Changing it is its own
  action, `CatalogWarehouseAction::ModifySoftDeletion`, which the managed-by lock also covers
  (`warehouse/mod.rs:902-960`).
- **Drop** (`LK:crates/lakekeeper/src/server/tables.rs:710-907`):
  - `force` ⇒ Hard, bypassing soft-delete (`:788-792`).
  - Hard: `drop_tabular` removes the row. When `purge` is set it queues a **purge task in the same transaction** (`:795-821`).
  - Soft: in one transaction, schedule a `tabular_expiration` task at `now + expiration` (`:822-844`) and mark `deleted_at` (`:846-852`).
  - FGA tuples are deleted only for Hard, **post-commit and best-effort** (`:882-891`).
  - During the soft window the grants stay live.
- **The name is freed at soft-delete. The location is not.**
  - The uniqueness constraint is `unique NULLS NOT DISTINCT (warehouse_id, namespace_id, name, deleted_at)`
    (`migrations/20250904142650_reusable_table_id.sql:332-334`). A live table and any number of soft-deleted
    tables can share a name.
  - `ensure_location_available` checks **every** tabular, soft-deleted ones included (no `deleted_at` filter;
    `LK:crates/lakekeeper-storage-postgres/src/tabular/mod.rs:552-583`).
- **Expiry and purge are two queues with two jobs.**
  - The `tabular_expiration` worker (`LK:crates/lakekeeper/src/service/tasks/tabular_expiration_queue.rs:155-316`),
    in **one DB transaction**:
    - hard-drops the row, treating `TabularNotFound` as an already-expired no-op (`:186-217`);
    - removes the object's tuples from the authorizer, best-effort (`:208-216`);
    - enqueues a `tabular_purge` task **only if** the drop asked for `Purge` (`:284-304`);
    - records its own success inside the same transaction (`:306-313`).
  - The purge worker only removes files (`LK:.../tasks/tabular_purge_queue.rs:149-229`). It reads the warehouse
    with `active_and_inactive` (`:176-181`), so it purges even inside a deactivated warehouse.
  - Heartbeat windows: expiration 120 s (`tabular_expiration_queue.rs:65-73`), purge 3600 s (`tabular_purge_queue.rs:54-62`).
  - Neither queue is user-schedulable. A pin test enforces this: *"tabular_purge is destructive and must never be
    user-schedulable"* (`LK:crates/lakekeeper/src/service/tasks/mod.rs:56-105`; `task_registry.rs:19-40,370-457`).
- **Undrop** (`warehouse/mod.rs:1371-1447`): clears `deleted_at` and cancels the expiration task **in one
  transaction** (`:1410-1433`).
  - If a live table of the same name exists, the unique constraint turns the undrop into `TabularAlreadyExists`
    (`tabular/mod.rs:1646-1655`).
  - Authz is `can_undrop: modify` (`LK:authz/openfga/v4.7/components/lakekeeper_table.fga:20`), resolved with
    `include_deleted: true` (`api/management/v1/warehouse/undrop.rs:44-55,97-113`).
- **Invariant: one soft-deleted tabular, one expiration task.** Lakekeeper learned this the hard way. A race
  cancelled expiration tasks without resetting `deleted_at`, and a repair migration had to restore or re-schedule
  those tabulars (`migrations/20250623114333_fix_soft_deleted_tabulars.sql:1-154`). The list endpoint still logs an
  error and hides any soft-deleted tabular that has no expiration task (`warehouse/mod.rs:1602-1624`).
- **Namespaces have no undrop.**
  - A recursive drop in a soft-delete warehouse is refused (400) unless `force`. With `force` it becomes a
    **hard** cascade: open tasks cancelled, a purge per child when `purge` is set, tuples deleted post-commit
    (`LK:crates/lakekeeper/src/server/namespace.rs:713-853`, refusal at `:843`, cancel at `:733`, purge loop `:742`).
  - A non-recursive drop refuses while soft-deleted children exist (`postgres/src/namespace.rs:716-728`), and while
    an expiration is running (`:744-746`).

### What rask does today

- Recoverable drops are opt-in through an estate-wide number. `LANCE_TRASH_GRACE_DAYS` defaults to 0
  (`R:services/catalog/src/catalog/core/config.py:513`). The chart sets `trashGraceDays: 7` (`R:chart/values.yaml:1069`),
  and the live `rask-catalog` has `LANCE_TRASH_GRACE_DAYS=7` (read live).
- A drop writes the trash record first and then deregisters (`R:services/catalog/src/catalog/api/v1/endpoints/tables.py:529-561`).
- Grants are revoked only `if not trashed` (`tables.py:599`). The record stamps `expires_at` at drop time
  (`R:packages/service-kit/src/service_kit/lakehouse/trash.py:47-90`).
- **The name stays locked until the record is gone.**
  - `require_no_live_trash` refuses create, register and rename-into whenever **any** record exists, expired or not.
  - It **fails open** on a store read error (`R:services/catalog/src/catalog/api/fga_deps.py:974-1011`, fail-open at `:995-998`).
  - Its call sites are `tables.py:269,804,1109` and `services/table_create.py:155`.
- **Expiry and purge are fused into one pass, and the pass is gated estate-wide.**
  - `maintenance/services/purge.py` revokes, deletes bytes and clears the record, all in one pass (`R:services/maintenance/src/maintenance/services/purge.py:1-60`).
  - It runs only when the same tick's drift report is clean (`report_is_clean`, `purge.py:225-283`) **and**
    `MAINTENANCE_TRASH_PURGE_ENABLED` is on. The chart default is `trashPurge: false` (`R:chart/values.yaml:1865`).
- **Measured live, 2026-09-25 13:00 UTC:**
  - Both `rask-maintenance` and `rask-maintenance-worker` run with `MAINTENANCE_TRASH_PURGE_ENABLED=true`. The live estate does not use the chart default.
  - The reconcile tick logged `trash_purge_blocked reason="the drift report is NOT clean: 1 finding(s) across ['orphan_files']"`.
  - The sweep logged `maintenance_trashed_excluded count=19`.
  - So **no expired drop anywhere in the estate finishes**. Tuples stay live and names stay locked, because of
    one unreferenced `.txn` file with no connection to any of them. This is the finding in
    `findings_lance_lakekeeper.md:76`, re-measured on the live estate rather than the default chart.
- rask has a namespace-level recoverable cascade and undrop, which Lakekeeper does not:
  `_trash_subtree` (`R:.../endpoints/namespaces.py:388-470`) and `undrop_namespace` (`namespaces.py:725-845`).

### What rask should do on its stack

1. **Split expiry from purge — the Lakekeeper split, translated.**
   - **Expiry** is per record, bounded and always on. At `expires_at` it:
     - re-checks liveness (`purge.check` already does this);
     - revokes the object's tuples and emits `grant_revoked` from `revoke_object_tuples`' return value (`R:packages/service-kit/src/service_kit/governed/fga.py:1855-1885`);
     - marks the record `state: expired` (a conditional write through `records.mutate_json`, as `note_refusal` already does at `trash.py:105-154`).

     From then on `require_no_live_trash` stops blocking the name.
   - **Expiry must not be gated on the estate-wide `report_is_clean`.** It touches no bytes, and the orphan-file
     class has no relation to a given tuple set.
   - **Purge** (bytes) keeps its current ladder: estate check, control prefixes, base refs, liveness. It picks up
     records in the `expired` state.
   - Whether purge stays gated on the whole-estate report, or on a per-location check, is a separate decision.
     It is LH-099's open question and D13 in `findings_reconciliation.md:186`.
   - **Mechanism.** Stay on the maintenance reconcile cron (scan-and-converge). `R:.claude/skills/rask-dapr/SKILL.md:105-111`
     already refuses Dapr Jobs for *"auto-purge at trash deadline"*, and Lakekeeper's expiration task is itself only
     a `scheduled_for` timestamp polled every 10 s (`LK:crates/lakekeeper/src/config.rs:1057-1061`). A scan over
     `expires_at` is the same mechanism. **Do not add a task table.**
   - Owner ruling needed. This reverses the diff2 F10 item 5 ruling ("an expired drop is still undroppable"). Under the split, undrop past expiry becomes *"the bytes are still here, the grants are not"*. Two ways to handle it: undrop is refused after expiry, or it restores bytes but not grants and requires a fresh grant.
2. **Keep rask's `force` / `purge` split. Do not copy Lakekeeper's `force ⇒ hard`.**
   - In rask `force` turns only the protection lock (`fga_deps.py:1088-1125`), and `purge=true` alone bypasses the trash (`tables.py:529`).
   - That is clearer than Lakekeeper overloading `force` with both meanings.
   - It also matches the spec's own split: `DropTable` is *"Drop table `id` and delete its data"* (`R:lance_docs/ns_catalog/spec.yaml:496-499`), and `DeregisterTable` *"The table content remains available in the storage"* (`spec.yaml:4076-4080`).
3. **Keep the one-record design.** The Lakekeeper repair migration above shows what goes wrong when there are two
   stores: task and row diverge. rask's record *is* the task. It has `expires_at`, `attempts` and `last_refusal`
   (`trash.py:105-154`), and there is no second store to diverge from. That is an advantage of object-store
   records and it should be kept.
4. **Recursive namespace drop.** rask goes further than Lakekeeper here, with a recoverable cascade plus undrop.
   Keep it. The one Lakekeeper behaviour worth adopting: a non-recursive drop / unbind refusal should **count trashed
   children** in its message, as `NamespaceNotEmpty` does at `postgres/src/namespace.rs:716-728`. That directly helps
   LH-016's 409 on `silver-media`.

**Rows.**
- LH-099: removes the purge-blocked-by-one-orphan trap for everything except bytes.
- LH-073 / LH-178: erasure needs drops to complete.
- LH-144: grants on dropped objects.
- New row: "expiry is fused with purge and gated estate-wide".

---

## 2. Name vs identity — what Lance gives rask (measured)

### What Lakekeeper does

- Every governance fact sits on a UUID-keyed row: `protected`, `deleted_at`, location, properties
  (`LK:crates/lakekeeper/src/service/catalog_store/tabular.rs:79-105`), the OpenFGA object `lakekeeper_table:<wh>/<uuid>`
  (`LK:crates/authz-openfga/src/tuples.rs:135-150`) and the tasks.
- A rename is one `UPDATE` of `name`, `namespace_id` and `tabular_namespace_name`
  (`LK:crates/lakekeeper-storage-postgres/src/tabular/mod.rs:1238-1520`), so every fact moves with it.
- A re-create gets a new UUID, so nothing is inherited. OpenFGA create even refuses when stale tuples exist on the
  new object (`require_no_relations`, `LK:crates/authz-openfga/src/authorizer.rs:857-880`, the conflict body at `:1400-1412`).

### What rask does today

- The FGA object is `table:<canonical name>` (`fga.canonical_object_id`).
- The records are keyed by `sha256("<kind>:<canonical_id>")[:24]` (`R:packages/service-kit/src/service_kit/lakehouse/record_store.py:99-107`).
- Consequences:
  - a rename must move every fact by hand (§3, §4);
  - a reused name would inherit a trashed table's live grants, hence the name lock (§1);
  - changing the delimiter re-keys everything (LH-150);
  - an IdP change re-keys subjects (LH-063). That is the subject side, which D5 fixes.

### What Lance offers (measured on pylance 12.0.0, `probe/dirns3`)

- `__manifest` schema: `object_id` (PK), `object_type`, `location`, `metadata`, `base_objects`. **There is no UUID column.**
- Creating, dropping and re-creating `a$t` produced three locations: `e9c23886_a$t` → `da262a7f_a$t` → `514e1495_a$t`.
  A create after a deregister also gets a new prefix.
  - **The prefix is per create, not a hash of the name.** `sha256("a$t")[:8]` is `07b2dd76`, which matches none of them.
- A rask rename registers the destination **at the source's location** (`R:.../endpoints/tables.py:1066-1069` docstring; `dataplane.rename_table`).
- So the table's **location is stable across rename and unique per incarnation**. Those are exactly the properties of Lakekeeper's UUID.

### What rask should do (owner choice)

- **Option A (incremental, now).** Keep name keys. Make rename and destroy iterate one registry of name-keyed stores (§4), and migrate the direct tuples on rename (§3).
- **Option B (structural; allowed, since no-backward-compat is the standing rule).** Key the FGA `table:` object, the protection record and the trash record on the incarnation, e.g. `sha256(full location)`.
  - Rename then keeps grants for free.
  - A re-create at the same name gets a fresh object, so the name can be **freed at drop**, as Lakekeeper does, and `require_no_live_trash` can go.
  - LH-150 disappears for tables.
- **Costs of B, stated honestly:**
  1. Every per-table authz gate needs a name→location lookup. Today `authorize` derives the object id from path segments with **no I/O**. Lakekeeper pays the same cost and caches it (`catalog_store/warehouse_cache.rs`, `namespace_cache.rs`).
  2. List filtering (OpenFGA ListObjects) returns incarnation ids, which must be mapped back to names.
  3. Uniqueness depends on location exclusivity at `register_table`. Today register accepts a second id at a location another id holds (`findings_lance_lakekeeper.md:53`). That fix is a precondition.
  4. Namespaces have `location: None` in `__manifest`, so they stay name-keyed. The design ends up mixed.
- Recommendation: do A now, and put B to the owner.

**Not checked:** whether the random prefix behaves the same on RustFS/MinIO S3 (it is the same backend code path, but only local FS was measured), or the exact source of the randomness.

**Rows.** LH-150, LH-063 (object side), LH-144, and the new rows in §3 and §4.

---

## 3. Rename semantics for grants

### What Lakekeeper does

- `rename_table` authorizes `CreateTable` on the **destination** namespace and `Rename` on the source
  (`LK:crates/lakekeeper/src/server/tables/rename_table.rs:137-204`, checks at `:175-198`).
  `can_rename: modify` (`lakekeeper_table.fga:25`).
- It updates the row and commits (`:91-130`). It **never touches the authorizer**: the `Authorizer` trait has no
  rename hook, and table parent edges are written only by `create_table` (`authz-openfga/src/authorizer.rs:857-880`)
  and by reconcile (`authz-openfga/src/reconcile.rs:599,719`).
- So direct grants survive, keyed on the UUID. After a **cross-namespace** rename the old `namespace→parent` edge
  stays until an operator runs the reconcile CLI:
  - `lakekeeper-bin/src/main.rs:333-395`, behind a Postgres advisory lock (`advisory_lock.rs`);
  - `reconcile.rs:44-60` itself names "a rename racing with the walk";
  - reconcile never touches ownership or grants (`reconcile.rs:30-33`).
- Found from the code, not tested: rename's `conflict_check` counts soft-deleted rows (`tabular/mod.rs` cross-namespace
  branch, `WHERE t.name = $1` with no `deleted_at` filter), yet create lets the name be reused. Lakekeeper is itself
  inconsistent here.
- **Correction to an earlier audit.** `findings_lance_lakekeeper.md:59` cites
  `crates/lakekeeper-integration-tests/tests/openfga_rename_tabular.rs` for *"tuples_deleted == 0"*. **That file does
  not exist at `a58e4017`** (`ls crates/lakekeeper-integration-tests/tests/`). The survival of direct grants still
  follows from the UUID keying above. The live-OpenFGA proof it cited could not be re-verified in this checkout.

### What rask does today

- Rename is gated on `can_drop` (owner) (`R:services/catalog/src/catalog/api/fga_deps.py:167`) plus
  `require_create_on_parent`.
- It seeds `owner` for the **caller** plus the new parent edge (`tables.py:1154`), then `revoke_ownership` deletes
  **every** tuple on the source (`tables.py:1155-1162`; `fga_deps.py:1241-1272`): reader, writer, validator,
  maintainer, classifier, publisher.
- Only a count is logged. **No `grant_revoked`** fires (`fga_deps.py:1270-1272`), although the project delete does
  emit one (`R:.../endpoints/projects.py:393-403`).
- The docstring says *"FGA tuples migrate from the old id to the new"* (`tables.py:1075-1076`). The code does not do that.

### What rask should do on its stack

Match Lakekeeper's outcome: direct grants survive, and inherited access follows the new parent.

- **Option A:**
  1. Read the source's direct tuples, excluding the structural `parent`/`child` edges.
  2. Rewrite them onto the destination together with the new parent edge, chunked the way Lakekeeper chunks at `MAX_TUPLES_PER_WRITE` (`authorizer.rs` role-assignment writes).
  3. Only then delete the source tuples.
  4. Emit `grant_revoked` for any grant that was deliberately **not** carried.
- **Option B** (§2) makes this automatic.
- Either way, add "parent edge disagrees with the manifest" to the reconciler's categories. That is the one drift Lakekeeper itself leaves to an operator.
- **Tier.** rask keeps rename at owner and Lakekeeper uses `modify`. Keep rask's: a rename retires an id that carries protection and policies.

**Rows.** New row: "rename silently revokes every direct grant". LH-063 (D5 fixes the subject key; this is the object key).

---

## 4. Protection

### What Lakekeeper does

- `protected` is a column on the warehouse, namespace and tabular rows:
  - `ResolvedWarehouse.protected`, `LK:crates/lakekeeper/src/service/catalog_store/warehouse.rs:246-247`;
  - `Namespace.protected`, `catalog_store/namespace.rs:35-44`;
  - `TabularInfo.protected`, `catalog_store/tabular.rs:102`.
- A protected tabular refuses delete without `force` (`ProtectedTabularDeletionWithoutForce`, `tabular.rs:1171-1184`;
  SQL `postgres/src/tabular/mod.rs` `mark_tabular_as_deleted` and `drop_tabular`, `(NOT protected) OR $force`).
- Namespace drop checks `NamespaceProtected`, `ChildNamespaceProtected` and `ChildTabularProtected` unless `force`
  (`catalog_store/namespace.rs:564-610`; `postgres/src/namespace.rs:732-743`).
- Warehouse delete refuses with `WarehouseProtected` unless `force` (`catalog_store/warehouse.rs:538-566`;
  `postgres/src/warehouse.rs:553-580`).
- `can_set_protection: modify` (`lakekeeper_table.fga:28`).
- **A rename carries protection, because it is a column** (the single UPDATE in §3). Lakekeeper also does not refuse a rename of a protected table: there is no protected check in `rename_tabular`.
- **`managed_by` lock** (`catalog_store/warehouse.rs:59-110,676-730,770-790`): when `instance-admin`, every
  warehouse spec mutation — delete, rename, protection, delete profile, storage, status — is limited to instance
  admins. It is checked `FOR UPDATE` inside the transaction.

### What rask does today

- Tables and namespaces use `_protection/<kind>-<hash>.json` records
  (`R:packages/service-kit/src/service_kit/lakehouse/protection.py:34-55`). Warehouses and projects carry the flag on their registry records.
- The guard is `fga_deps.require_not_protected` (`fga_deps.py:1088-1125`). It gates drop, deregister and rename
  (`tables.py:522,643,1095`).
- **Rename migrates the maintenance policy (`tables.py:1164-1174`) but not the protection record.** A forced rename
  lands the table unprotected, and a later create at the old id is born protected (`findings_lance_lakekeeper.md:60`,
  re-checked here).

### What rask should do on its stack

- Under option A (§2), translate "one row carries every fact" as **one enumerable list of name-keyed stores** in
  `service_kit.lakehouse`: `_protection`, `_policies`, `_trash` and the FGA object.
  - `rename` iterates it to migrate.
  - A destructive drop iterates it to clear.
  - `create` clears anything stale.
  - A test fails when a new `record_store` prefix is missing from the list.
- Build `purge.CONTROL_PREFIXES` from the same list. Today it misses `_idempotency`, `_tasks`, `_transforms` and
  `_gates` (`R:services/maintenance/src/maintenance/services/purge.py:95`; the prefixes are defined in `lakehouse/idempotency.py:35`, `task_registry.py:41`, and elsewhere).
- Under option B, protection keys on the incarnation and this problem goes away.
- The managed-by lock bears on phase 3: an operator or IaC owning a warehouse spec (XC-045, CTL-018). Record it there. Do not build it now.

**Rows.** New row: "forced rename leaves protection at the old id". XC-017 (zero-trust list item). LH-056: per-branch protection landed as table-level. That is consistent with Lakekeeper, which has no per-ref protection.

---

## 5. The task queue, and a spec-native task surface

### What Lakekeeper does (`LK:crates/lakekeeper/src/service/tasks/**`, `catalog_store/tasks.rs`, `postgres/src/tasks.rs`)

- **One active task per `(project, warehouse, entity_type, entity_id, queue)`.** The insert is `ON CONFLICT … DO NOTHING` (`postgres/src/tasks.rs:263`). Terminal rows move to `task_log`, so the `task` table holds only active rows (`api/management/v1/task_queue.rs:443-448`).
- **Picking** (`tasks.rs:309-457`):
  - `FOR UPDATE OF t SKIP LOCKED` (`:344`);
  - a task whose heartbeat is older than the queue's `max_time_since_last_heartbeat` (overridable per warehouse/project in `task_config`) is reclaimed (`:340`);
  - the stale attempt is logged as `'Attempt timed out.'` (`:377`);
  - the attempt counter increments (`:398`).
- **Retry.** Up to `DEFAULT_MAX_RETRIES = 5` (`service/tasks/mod.rs:34`). A failed attempt goes straight back to
  `scheduled` **with no backoff**: `scheduled_for` is untouched (`postgres/src/tasks.rs:575,703`).
- **Scheduling happens inside the mutation's own transaction:**
  - drop → expiration or purge (`server/tables.rs:797-856`);
  - project create → the `task_log_cleanup` task (`api/management/v1/project.rs:150-166`);
  - expiration → purge, plus its own success (`tabular_expiration_queue.rs:284-313`).
- **Control:** `Stop`, `Cancel`, `RunNow`, `RunAt` (`api/management/v1/tasks.rs:616-634`).
  - `Stop` is cooperative: the worker sees `should-stop` on its next heartbeat (`postgres/src/tasks.rs:874-897,989-1027`).
  - Authz: `can_get_tasks: describe`, `can_control_tasks: modify` (`lakekeeper_table.fga:30-31`).
- **Log retention** is a self-rescheduling project task: default 90 days, run daily (`task_log_cleanup_queue.rs:37-38,167-238`).
- **Workers** are restarted when they exit (`task_queues_runner.rs:75-155`).
- **Per-queue config** is validated against the queue's own type (`task_queue.rs:71-110`).
- **Manual schedule** is only allowed for queues that opted in. OSS has none (`service/tasks/mod.rs:61-87`).

### What rask does today

- rask has **no task table**, and should not grow one. The primitives are already there:
  - **Maintenance units:** Dapr pub/sub on NATS JetStream, one message per dataset, at-least-once, `RETRY` → redelivery → DLQ after `maxDeliver` (`R:services/maintenance/src/maintenance/services/work_queue.py:1-61`). JetStream's `ackWait` / `maxDeliver` / DLQ play the roles of Lakekeeper's heartbeat / `max_retries` / failed `task_log`.
  - **The trash record** is the one active expiry task per object (keyed by id), with `attempts` and `last_refusal` (`trash.py:105-154`).
  - **Single-flight ticks** use a process `asyncio.Lock` (`R:services/maintenance/src/maintenance/api/routes.py:52-72,165-168`). This is Lakekeeper's `MaintenanceLockGuard` / advisory lock (`LK:crates/lakekeeper/src/service/maintenance.rs:13-27`) mapped onto a single replica, pinned by an invariant test.
- **`_tasks/` is a task REGISTRY, not a queue.** It records *what may be run* (`R:packages/service-kit/src/service_kit/lakehouse/task_registry.py:1-17`). The word collides with Lakekeeper's meaning; keep the two apart in prose.
- **The owner-facing surface is rask-only.**
  - `GET /management/v1/table/{id}/tasks` returns only the trash entry (`tables.py:898-918`), and so does `…/namespace/{id}/tasks` (`namespaces.py:710-723`).
  - `TrashEntry` has no `attempts` or `last_refusal` (`R:services/catalog/src/catalog/schemas.py:1002-1019`), so an owner cannot see that their drop is stuck, or why.

### The spec already defines this surface (read from `R:lance_docs/ns_catalog/spec.yaml`)

- `DropTableRequest`: *"If the table and its data can be immediately deleted, return information of the deleted table. **Otherwise, return a transaction ID that client can use to track deletion progress.**"* (`spec.yaml:4038-4043`).
- `DropTableResponse.transaction_id` (`:4052-4059`), `DeregisterTableResponse.transaction_id` (`:4092-4097`), and `DropNamespaceResponse.transaction_id`, described as *"indicating the operation is long running and should be tracked using DescribeTransaction"* (`:2627-2642`).
- `TransactionStatus`: Queued / Running / Succeeded / Failed / Canceled (`:3909-3918`). `AlterTransactionSetStatus` (`:3946-3950`).
- **Measured on pylance 12 `DirectoryNamespace`** (`probe/dirns`, `probe/dirns2`):
  - `drop_table` returns `transaction_id=None`: it is synchronous.
  - `describe_transaction(id=[<table…>, "<version>"])` resolves to that version's committed Lance transaction: `SUCCEEDED` with `{version, operation, uuid, read_version}`.
  - `["t1"]` alone → `InvalidInputError: … must include table id and transaction identifier`.
  - A `.txn` filename as the id → `TransactionNotFoundError`.
  - `alter_transaction` with `Canceled` on a committed version returned `SUCCEEDED` and changed nothing.
  - So the backend's transaction namespace is **table + version**, and it knows nothing about deferred deletes.

### What rask should do on its stack

- **Short term:** surface `attempts`, `last_refusal` and `expired` on `/tasks`. They are already on the record. Surface them on the notification path too (`rask-notifications`), so the dropper learns the drop is stuck.
- **Lance-idiomatic target (owner call):**
  1. A recoverable drop or cascade returns `transaction_id = [<id segments…>, "drop-<dropped_at>"]`.
  2. The catalog's `/v1/transaction/{id}/describe` answers such ids **from the trash record**, and forwards version-shaped ids to native as today (`R:.../endpoints/transactions.py:22-32`):
     - Queued = within grace;
     - Running = purge in progress;
     - Succeeded = purged and record cleared;
     - Failed = refusal count ≥ N;
     - Canceled = undropped.
  3. `/v1/transaction/{id}/alter` with `SetStatus=Canceled` performs the undrop.

  This gives `transaction.can_cancel` a real door, and so decides LH-077 and XC-020 in the "make them real" direction. It needs a ruling, because reading *undrop as cancelling the deletion* is an interpretation; the spec only says to track deletion progress. The rask-only `undrop` route would then be removed, as the no-backward-compat rule requires.
- **Do not port:**
  - retry without backoff: JetStream redelivery already delays;
  - user-schedulable destructive queues: keep refusing them, as Lakekeeper's pin test does;
  - a task log table: the audit lane is the log.

**Rows.** FE-005 (the UI would then read `DescribeTransaction`). LH-077 / XC-020. LH-195: per-tier cadence parity. Lakekeeper's per-warehouse `task_config` has the same shape as rask's `_policies/` with `compact_interval_hours`, so there is nothing new to copy; the values stay the owner's.

---

## 6. Idempotency

### What Lakekeeper does

- The key is an optional UUID header; a duplicate header is 400 (`LK:crates/lakekeeper/src/service/idempotency.rs:39-98`). The scope is **per warehouse** (`PK (warehouse_id, idempotency_key)`, `migrations/20260318120000_idempotency_record.sql:12-47`), and only mutation endpoints qualify (CHECK constraint `:28-43`).
- The record is inserted **inside the mutation's transaction** right before commit (`catalog_store/idempotency.rs:279-290`; e.g. `server/tables.rs:859-880`), so records exist **only for committed successes**. There is no in-flight state and no stored body: a replay re-derives the response (`service/idempotency.rs:100-124`). A concurrent winner makes the loser roll back with "request in progress" (`tables.rs:869-879`).
- Lifetime is 30 minutes plus 5 minutes grace, advertised in `getConfig` (`config.rs:738-781`, tests `:2087-2095`).
- Cleanup: 1% of checks spawn a single-flight `DELETE … LIMIT 1000` of rows older than the retention (`postgres/src/idempotency.rs:14-45,74-115`).
- A recursive namespace drop is **not** idempotency-keyed, because it manages its own transaction (`server/namespace.rs:486-495`).

### What rask does today

- An optional header (`R:services/catalog/src/catalog/api/idempotency.py:42-45`), scoped **per subject** (`blake2s(sub)`, `:54-57`).
- Two phases: `claim` writes `in_flight` with a 300 s lease through a conditional create; `record_outcome` merges state, status and the **full response body** (`R:packages/service-kit/src/service_kit/lakehouse/idempotency.py:49,85-145`).
- Nothing ever reclaims `_idempotency/` (`findings_lance_lakekeeper.md:77`), and `purge.CONTROL_PREFIXES` lacks the prefix (`purge.py:95`).

### What rask should do on its stack

- An object store has no multi-object transaction. The two-phase claim is **inherent**, and re-execution after a crash between the Lance write and `record_outcome` cannot be avoided; say so in the docstring.
- Copy the rest:
  - a maintenance reclaim of records older than lifetime + grace, capped per tick like Lakekeeper's `LIMIT 1000` (30m+5m is far above the 300 s lease);
  - advertise the lifetime somewhere a client reads (a rask config or `/management/v1` metadata; the lance-ns spec has no field for it);
  - store bodies only where they cannot be re-derived: drop and rename already return empty bodies;
  - exclude cascade drops, as Lakekeeper does.
- Note for D5: when the principal key becomes `<idp>~<claim>`, the scope hash changes. Existing keys are test data.
- Lance data commits have their own replay marker (`findings_lance_lakekeeper.md:40`). The catalog records are only for metadata doors.

**Rows.** New row: "idempotency records never reclaimed".

---

## 7. Project and warehouse lifecycle

### What Lakekeeper does

- **Project create** writes the row and the authorizer tuples and schedules the log-cleanup task, **all before commit** (`api/management/v1/project.rs:143-168`).
- **Project delete:**
  - it is FK-refused as `ProjectNotEmpty` while warehouses exist (`postgres/src/warehouse.rs:295-323`);
  - then `delete_project` → `delete_all_relations`, which deletes both the object's own relations and the relations where it is the user (`authz-openfga/src/authorizer.rs:791-797,1425-1436`);
  - both happen before commit (`project.rs:252-286`).
- **Warehouse delete** (`api/management/v1/warehouse/mod.rs:675-730`; `postgres/src/warehouse.rs:530-583`) refuses while:
  - **any task** exists for the warehouse, expirations and purges included (`WarehouseHasUnfinishedTasks`, `:535-551`);
  - any tabular row exists, soft-deleted included, through an FK (`WarehouseNotEmpty`, `:568-571`);
  - it is protected without `force` (`:578-580`).

  So in Lakekeeper **a warehouse cannot be deleted before its soft-deletes have expired**. The expiry worker, which removes tuples, always runs first. The integration test `test_cannot_drop_warehouse_before_purge_tasks_completed` pins it (`LK:crates/lakekeeper-integration-tests/tests/drop_warehouse.rs:24-135`).
- **Status** `active`/`inactive` (`catalog_store/warehouse.rs:52-57`; deactivate/activate `warehouse/mod.rs:1034-1148`). Rename works only when `active` (`postgres/src/warehouse.rs:585-624`). The overlap check at create includes inactive warehouses (`warehouse/mod.rs:1798-1827`).
- **Create compensation in the same request:** `TableCreationGuard` removes authorizer tuples and the metadata file if the transaction fails after `authorizer.create_table` (`server/tables/create_table.rs:45-100,164,366`).

### What rask does today

- Project delete refuses while warehouses exist, and there is no cascade parameter (`R:.../endpoints/projects.py:284-330`). It emits `grant_revoked` (`:393-403`).
- Warehouse delete (`R:.../endpoints/warehouses.py:840-1026`):
  - gated on `project#can_administer`, with 404 collapse;
  - checks protection and emptiness by bindings;
  - `?cascade` drops bound namespaces and revokes the descendants of *registered* tables (`_revoke_descendants_of`, `:748-783`);
  - `?purge_bucket` is a separate opt-in.
- `delete_warehouse_record` calls `_clear_trash_under` (`R:services/catalog/src/catalog/services/warehouses.py:549-606`). This **clears trash records whose location is in the bucket, without revoking those objects' tuples**.
  - Trashed tables are deregistered, so the cascade's descendant revoke cannot list them.
  - A cascade-trashed top-level namespace has already been unbound, so it is not in `bound`.
  - Namespace-kind records carry `location: ""`, so the `startswith(prefix)` match (`:596-599`) **never clears them**. Found from the code, not driven.
  - Live reconcile at 13:00:19 shows `ghost_tables: 43`. **I did not attribute these to this path**; LH-061's drain residue is the other known source.

### What rask should do on its stack

- In `delete_warehouse`, either:
  - (a) **run the expiry step** (§1: revoke tuples, emit `grant_revoked`) for every trash record under the warehouse before clearing it, matching namespace-kind records by `binding.warehouse_id` or id prefix, not by location; or
  - (b) **refuse with 409** like Lakekeeper: *"warehouse holds N recoverable drops until <max expires_at>; undrop, purge, or pass ?purge_bucket"*.

  (a) fits rask's existing "cascade" and "purge_bucket" vocabulary better. (b) is the Lakekeeper semantics. Either closes the leak.
- **Keep rask's quarantine stance on deactivated warehouses.** The purge refuses them (`purge.py:44-46` header), where Lakekeeper purges inactive warehouses (`tabular_purge_queue.rs:176-181`). rask's choice is deliberate and stricter; record it as a conscious divergence.

**Rows.** New row: "warehouse delete leaks trashed objects' tuples". LH-144. LH-016: count trashed children in the refusal.

---

## 8. Failed-seed unwind (LH-194)

- **Lakekeeper** does not face this problem. A creating machine becomes owner (`ownership_tuples_for_table(actor, …)`, `authz-openfga/src/authorizer.rs:869-875`). An in-request failure is compensated by `TableCreationGuard` (§7), and every authorizer create runs before commit.
- **rask's ruling** (*"a machine does not own what it registers"*, `R:.../api/fga_deps.py:1212-1231`) removes the first option. The seed failure happens in a **later** request, so the in-request guard cannot help either.
- **Translation.** The spec's `DeclareTable` records *"the table existence and sets up aspects like access control"* without touching storage (`R:lance_docs/ns_catalog/spec.yaml` `operationId: DeclareTable` description). It is Lance's version of Lakekeeper's staged table (`TabularListFlags.include_staged`, `catalog_store/tabular.rs:28-71`).
  - A producer that **declares** the head rather than registering it leaves only a byte-less declaration if the seed fails.
  - A maintenance pass may reap declarations older than N hours: shape (b), the `absent_datasets` category. This is safe precisely because there are no bytes.
- **Not checked:** whether the medallion head can use declare instead of register given its `register` converge arm. I did not read the medallion producer.

**Rows.** LH-194.

---

## 9. State Lakekeeper keeps, and what Lance and the object store make unnecessary

`LK:crates/lakekeeper/src/service/catalog_store.rs:306-1115` is the whole store contract.

| Lakekeeper state | Why it exists there | rask / Lance equivalent | Port? |
|---|---|---|---|
| `tabular` rows with `metadata_location`, `ConcurrentUpdateError` on drop-with-required-location (`postgres/src/tabular/mod.rs` `drop_tabular`) | Iceberg puts the commit pointer in the catalog | Lance commits are CAS in the object store; `__manifest` holds `object_id → location` | **No.** CLAUDE.md's Lance-only ruling states exactly this |
| `deleted_at` column + `active_tabulars` view | Soft-delete on the row | deregister + `_trash/` record | No; keep the record (§1) |
| `task` / `task_log` / `task_config` tables | A Postgres queue | JetStream units + trash records + `_policies/`; audit lane as the log | No (§5) |
| `idempotency_record` | In-transaction dedupe | `_idempotency/` conditional records | Only retention and reclaim (§6) |
| Entity versions (`WarehouseVersion`, `NamespaceVersion`, `RoleVersion`; `catalog_store/warehouse.rs:129,262-263`, `namespace.rs:32`) plus moka caches with version-gated insert, `Op::Remove` invalidation, no negative caching, and a documented resurrection residual (`catalog_store.rs:112-199`; `warehouse_cache.rs:108-248`; `namespace_cache.rs:111-186,384-427`) | Cache coherence across replicas | rask reads `__manifest` and records directly; records use ETag RMW (`records.mutate_json`). **LH-050** keeps "no query store" | Not unless LH-050 measures a need. If rask ever caches, copy the discipline: version-gated insert, invalidate through the loader's lock, never negative-cache. rask's `warehouse_binding_cache` caches positively and forever (`warehouses.py:883-887` docstring; pops at `:600,945`) and depends on explicit `pop` on unbind |
| `ensure_location_available` over all rows | UUID-named locations can still collide | The dir backend gives each create a fresh prefix (measured, §2). Collision can only come from a caller-supplied location at `register_table` or `undrop` | Only at register, undrop and rename re-register (`findings_lance_lakekeeper.md:53`) |
| `users`, `role`, `role_assignment`, sync logs, role-membership graph with cycle and depth checks (`catalog_store/role.rs`, `role_assignment.rs:481-880`) | Lakekeeper's own role store for non-OpenFGA authorizers and UI search | OpenFGA + Dex. rask has no user table | No. D5 sets the subject key; group sync stays in the IdP |
| `ServerInfo {server_id, terms_accepted, open_for_bootstrap}` + operator `reopen_for_bootstrap` (`catalog_store/server.rs`; `catalog_store.rs:323-343`) | Bootstrap and admin recovery | **XC-011**: rask writes no bootstrap record | Yes, the shape: a `_control/bootstrap.json` written through `create_json`, plus an operator-only reopen path |
| `WarehouseFormatVersionPolicy` (allowed/default format version) (`catalog_store/warehouse.rs:131-228`) | Iceberg v1/v2/v3 per warehouse | The staged pylance-12 mixed-file-version guard (`R:tests/unit/test_a_register_refuses_a_table_that_mixes_file_versions.py`, staged) is the Lance equivalent | Candidate. A per-warehouse `data_storage_version` floor on the warehouse record, the same sub-resource shape. Not a row today |
| `endpoint_statistics` | Usage | GreptimeDB metrics | No |

---

## 10. What I did not check

- Lakekeeper's event publishers (NATS/Kafka CloudEvents) for lifecycle events: `emit_table_dropped_async`, `emit_tabular_undropped` and the rest. Events are another agent's domain.
- `api/management/v1/tasks.rs`: authz and list details beyond the `ControlTaskAction` enum and the permission constants (`:44-59,605-634`).
- Lakekeeper views and generic tables beyond the shared tabular paths.
- rask `catalog/api/v1/endpoints/maintenance.py`, including whether any door lets a user trigger a purge.
- The rask medallion producer (§8).
- rask `namespaces.py` beyond `:380-480` and `:700-845`.
- `dataplane.rename_table` internals. I relied on its docstring for "registered at the source's location".
- Attribution of the live `ghost_tables: 43` to the warehouse-delete leak.
- Two claims are from the code only, with no live OpenFGA run: Lakekeeper's stale parent edge after a cross-namespace rename, and its force-recursive drop not revoking soft-deleted children's tuples (`postgres/src/namespace.rs:696,698` versus the `child_tables` loop in `server/namespace.rs`).
- The dir-backend location randomness on S3/RustFS (local FS only).
- No rask test was run and no rask file was written.
