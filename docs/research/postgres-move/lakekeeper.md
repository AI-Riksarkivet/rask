# Lakekeeper: how it is designed (read from source, 2026-11-26)

## Sources and how to read this file

| Repository | Branch | Commit read | Commit date |
|---|---|---|---|
| `lakekeeper/lakekeeper` (catalog, Rust) | `main` | `4054ae96775158fc47ab61845b9e9f0852d71e4e` | 2026-11-25 |
| `lakekeeper/lakekeeper-charts` (Helm chart 0.15.0, appVersion 0.15.1) | `main` | `732badae09566f2d1815a49ab8d7342d0dd1515b` | 2026-11-18 |
| `lakekeeper/lakekeeper-operator` (Go, "Work in Progress") | `main` | `27fa1e18d0d625ad5ccf991349992e75c25792e7` | 2026-06-10 |
| `lakekeeper/lakekeeper-operator-community` | `main` | tarball of 2026-11-26 | README: "THIS REPOSITORY IS CURRENTLY NOT MAINTAINED" |
| `lakekeeper/terraform-provider-lakekeeper` | `main` | file listing only | pushed 2026-11-26 |

All paths below are relative to the repository named in the section, `lakekeeper/lakekeeper` unless stated. The
documentation was read from its source in the repository (`docs/docs/*.md`), which is what docs.lakekeeper.io is built
from; the website itself was not fetched. A claim marked **(docs)** was read only in that documentation; a claim marked
**(inferred)** is my reading of code I did not see exercised. Everything else is quoted from, or points at, code.

Two editions matter for the comparison. The open-source repository is what was read. The documentation marks features of
the closed-source **Lakekeeper Plus** with a `.lkp` class; its code is not public (`AGENTS.md`: "Lakekeeper Plus (private
repository) builds on this workspace, pinned to a git revision"). Where a capability exists only in Plus, this file says so
and it is by definition **(docs)**.

---

## 1. State and transactions

### What it is

Lakekeeper is an Iceberg REST catalog. In Iceberg the catalog owns the pointer to a table's current metadata file, so a
catalog needs a store that can compare-and-swap that pointer. Lakekeeper's store is one Postgres database, and it puts
nearly all of its other state in the same database so that most multi-object changes are one SQL transaction.

### Where every piece of state lives

**In Postgres** (`crates/lakekeeper-storage-postgres/migrations/`, 79 migration files). Tables created by the migrations
(collected with a grep over `CREATE TABLE`): `server`, `project`, `warehouse`, `namespace`, `tabular`, `table`, `view`,
`generic_table` and their detail tables (`table_schema`, `schema_field`, `table_snapshot`, `table_snapshot_log`,
`table_metadata_log`, `table_refs`, `table_partition_spec`, `table_sort_order`, `table_properties`, `table_statistics`,
`partition_statistics`, `view_version`, `view_representation`, …), `users`, `role` (plus role-membership and
role-assignment tables added by `20260301083426_user_role_mappings.sql` and `20260601000000_role_membership.sql`),
`grant_assignment` (`20260811000000_grants.sql`), `tag_definition`, `tag`, `tag_allowed_value`, `task`, `task_log`,
`task_config`, `task_schedule`, `tabular_expirations`, `tabular_purges`, `idempotency_record`, `endpoint_statistics`,
`warehouse_statistics`, `warehouse_statistics_history`, `cache_change_log`, `secret`.

- Iceberg table metadata is stored **normalised in the database** (snapshots, schemas, refs as rows), in addition to the
  `metadata.json` file on the object store. A commit applies "changes in the DB to all sub-tables (schemas, snapshots,
  partitions etc.)" (`crates/lakekeeper-storage-postgres/src/tabular/table/commit.rs`).
- Soft-delete state is the column `tabular.deleted_at`; protection is the column `tabular.protected` (and a `protected`
  flag on namespace and warehouse). Both are read in `mark_tabular_as_deleted`
  (`crates/lakekeeper-storage-postgres/src/tabular/mod.rs:1951`).
- Tasks, their configuration per project/warehouse, their schedules and their log are tables (dimension 2).
- Statistics: per-endpoint request counts are aggregated in memory and flushed to `endpoint_statistics` (default every
  30 s, **(docs)** `docs/docs/monitoring.md` § Endpoint Statistics); table and view counts per warehouse are kept in
  `warehouse_statistics` by database triggers (trigger `update_counts_trunc` in
  `migrations/20250108131004_statistics.sql`; I read the trigger names, not their bodies).

**Outside Postgres:**

1. **The object store**: every table's `metadata.json` files and all data files.
2. **The authorizer's store**. With OpenFGA, the hierarchy edges (table → namespace → warehouse → project → server),
   ownership, grants and role assignments are OpenFGA tuples, in OpenFGA's own database. The trait says so:
   `fn role_assignments(&self) -> Option<&dyn ManagesRoleAssignments>` — "`None` means assignments live in the catalog";
   `fn grants(&self)` — "`None` means grants live in the catalog's `grant_assignment` table"
   (`crates/lakekeeper/src/service/authz/mod.rs:2956-2965`). The OpenFGA authorizer returns `Some(self)` for both
   (`crates/authz-openfga/src/authorizer.rs:1112-1118`). So with OpenFGA, authorization state is in a second database
   that cannot join the catalog's transaction; with any other authorizer (AllowAll, Cedar in Plus, a custom one), grants
   and role assignments are Postgres rows in the catalog's own transaction.
3. **The secret store**: a warehouse's storage credential. Default backend is the same Postgres (encrypted with
   `LAKEKEEPER__PG_ENCRYPTION_KEY`); the alternative is a HashiCorp Vault KV2 store (`crates/lakekeeper-secrets-kv2`;
   `docs/docs/configuration.md` line 27: `LAKEKEEPER__SECRET_BACKEND` one of `postgres`, `kv2`).
4. **Per-process memory**: caches of warehouses, namespaces, secrets, roles, role assignments, users, short-term
   credentials; the endpoint-statistics buffer; the CloudEvents channel (dimension 7).

### What one request commits atomically

House rules, stated in `AGENTS.md` § Rules: "Never acquire a nested database connection. If a transaction is active, all
subsequent queries must use that transaction"; "Stay on the default isolation level — no `REPEATABLE READ`/`SERIALIZABLE`.
Read co-dependent data in ONE statement; guard a read-then-write with a version compare-and-set that reports a mismatch as
retryable."

Authorization is always checked **before** the write transaction opens, from the handler (dimension 4).

#### Table create — `crates/lakekeeper/src/server/tables/create_table.rs`

Order of effects:

1. Idempotency-key lookup (replay returns a re-derived load).
2. Authorization check (`load_and_authorize_namespace_action`), then contract verifiers (`check_create_table`).
3. `C::Transaction::begin_write`, `C::create_table(...)` — row inserted, **not committed**. The insert also takes advisory
   locks on the location and its prefixes and checks no other tabular or pending purge overlaps it (dimension 6).
4. Storage: read the secret, check the location is empty, **write `metadata.json`** (skipped for a staged create). Code
   comment: "We don't commit the transaction yet, first we need to write the metadata file."
5. Vend credentials for the response.
6. **Authorizer**: `authorizer.create_table(...)` — for OpenFGA, `require_no_relations` then one write of the hierarchy
   and ownership tuples (`crates/authz-openfga/src/authorizer.rs:989`).
7. `write_bootstrap_grants(..., t.transaction())` — grant rows in the same transaction (no-op under OpenFGA, which
   records ownership in step 6).
8. `try_insert_idempotency_key(..., t.transaction())` — "Insert idempotency key in the same transaction."
9. `t.commit()`.
10. After commit: delete the tuples of an overwritten staged table (`.ok()`, best effort); spawn the `table_created`
    event.

So one database transaction holds: the tabular rows, the normalised metadata, the grant rows (non-OpenFGA) and the
idempotency record. Two side effects happen **inside the open transaction, before commit**: the metadata file write and
the OpenFGA tuple write.

What happens on failure — the `TableCreationGuard` in the same file:

```rust
/// Remove what was prepared before `error`. Keeps it if the commit may have been applied.
async fn cleanup(&mut self, error: &IcebergErrorResponse) {
    if error.error.r#type == COMMIT_STATE_UNKNOWN_ERROR_TYPE {
        tracing::warn!(... "Creation of table {} in warehouse {} may have been committed, skipping cleanup", ...);
        return;
    }
    if self.authorizer_created && let Err(e) = self.authorizer.delete_table(self.warehouse_id, self.table_id).await {
        tracing::warn!("Failed to cleanup authorizer table {} ... after failed transaction: {e}", ...);
    }
    if let Some((io, metadata_location)) = self.metadata_location.take()
        && let Err(e) = io.delete(metadata_location.as_str()).await {
        tracing::warn!("Failed to cleanup metadata file at {metadata_location} after failed transaction: {e}");
    }
}
```

- A side effect fails **before** commit: the transaction is dropped (rolled back), and the guard compensates the ones
  already done, best effort, with a warning if compensation fails.
- The commit's outcome is **unknown** (connection lost after the server may have committed): a distinct error type,
  `COMMIT_STATE_UNKNOWN_ERROR_TYPE = "CommitStateUnknownException"` (`service/catalog_store.rs:432`), and nothing is
  cleaned up, because the table may exist. This case has its own test file
  (`crates/lakekeeper-integration-tests/tests/integration/commit_state_unknown.rs`, 407 lines).
- A process crash between step 4/6 and step 9 leaves an orphan metadata file and orphan tuples for a table id that never
  existed. Nothing in the open-source code sweeps them; the orphan file later makes the location "not empty" only for
  that exact UUID path, which is never reused. **(inferred)**

#### Table commit — `crates/lakekeeper/src/server/tables.rs` (`commit_tables_with_authz`, `try_commit_tables`, `write_commits`)

1. Validate; authorization and idempotency lookup run in parallel (`tokio::join!`).
2. A short transaction loads the stored metadata of every table in the request and **commits immediately** (it is a read).
3. In memory: apply the client's requirements and updates to the stored metadata (`apply_commit`), check format-version
   policy, compute the new metadata location; run contract verifiers (`check_table_updates`).
4. If nothing changed: record the idempotency key "in a transaction of its own" and return — no file, no event.
5. Otherwise `write_commits`:

```rust
delete_on_failure(&file_io, &new_metadata_locations, async {
    // All writes finish before an error returns, so none lands after the cleanup.
    futures::future::join_all(commits.iter().map(|commit| write_file(...))).await...?;

    let mut transaction = C::Transaction::begin_write(state.v1_state.catalog.clone()).await?;
    C::commit_table_transaction(warehouse_id, commits.iter().map(CommitContext::commit), transaction.transaction()).await?;

    // Claim the idempotency key in the same transaction.
    claim_idempotency_key::<C>(transaction, warehouse_id, idempotency).await?.commit().await
})
```

   The metadata files are written **first**, then one transaction makes them current for **all** tables of the request
   and claims the idempotency key. The compare-and-swap is in SQL
   (`crates/lakekeeper-storage-postgres/src/tabular/table/commit.rs`, `lock_tables_for_commit`):

```sql
SELECT tabular_id, metadata_location FROM tabular
WHERE warehouse_id = $1 AND tabular_id = ANY($2::uuid[]) AND typ = 'table' AND deleted_at IS NULL
ORDER BY tabular_id
FOR NO KEY UPDATE
```

   followed by `if current_location.as_deref() != expected_location { return Err(ConcurrentUpdateError...) }`. Its doc
   comment: "commits to one table queue here and a stale commit fails before its writes can collide with the winner's
   … Id order keeps commits to several tables from crossing."
6. `delete_on_failure` (`server/io.rs`): if the transaction fails, the just-written files are deleted in a spawned task;
   if the commit state is unknown they are kept ("Commit may have been applied, keeping its new metadata files").
7. On a concurrent-update error the whole attempt is retried in the server up to `MAX_RETRIES_ON_CONCURRENT_UPDATE`
   with backoff (`commit_tables_inner`).
8. After commit: `transaction_committed` event dispatched; expired metadata-log files deleted in the background with a
   deadline of five-sixths of the request limit (`cleanup_deadline`).

Consequences of the design: a **multi-table commit is atomic** (one SQL transaction over all pointers) — something an
object-store pointer cannot give. No authorizer write is involved in a commit. The only non-atomic piece is the metadata
file, and it is written before the pointer moves, so a failure leaves an unreferenced file, never a pointer to a missing
file.

#### Drop with soft delete — `server/tables.rs:810` (`drop_table`)

Authorization (plus a second check, `authorize_soft_deletion_skip`, when `force` is used in a soft-delete warehouse),
contract verifier `check_drop`, then one transaction containing either:

- **Hard profile**: `C::drop_tabular(...)` and, if purge was requested, `TabularPurgeTask::schedule_task(..., t.transaction())`.
- **Soft profile**: `TabularExpirationTask::schedule_task(... scheduled_for: now + expiration_seconds ..., t.transaction())`
  and `C::mark_tabular_as_deleted(...)`.

plus the idempotency claim; then `t.commit()`. The task row is inserted **in the same transaction** as the state change,
so a dropped table always has its expiration task and a purge is never lost (a transactional outbox, in effect). After
commit, for a hard delete only:

```rust
// Post-commit: best-effort authz cleanup for hard deletes
authorizer.delete_table(warehouse_id, table_id).await
    .inspect_err(|e| { tracing::error!(?e, "Failed to delete table from authorizer: {}", e.error); }).ok();
```

A soft drop writes **nothing** to the authorizer: the table keeps its tuples while it waits, which is why an undrop needs
no authorizer write either. Object-store deletion never happens in the request; it is the purge task's.

#### Rename — `server/tables/rename_table.rs` and `server/tabular.rs:327` (`commit_rename_with_reparent`)

One transaction: `C::rename_tabular` + idempotency claim. A rename inside one namespace touches no authorizer state. A
rename across namespaces must move the table's parent edge in OpenFGA, and the order is fixed and documented:

```rust
// Pre-commit: retire the old edge. Hard error — nothing is committed yet, so failing
// here leaves both systems as they were.
if reparented { authorizer.detach_tabular_parent(metadata, warehouse_id, tabular_id, source_namespace_id).await?; }

if let Err(err) = transaction.commit().await {
    if reparented && err.error.r#type == COMMIT_STATE_UNKNOWN_ERROR_TYPE { tracing::error!(... "Rename may have been committed; the parent edge in the authorizer may be missing until it is reconciled"); }
    else if reparented { authorizer.attach_tabular_parent(.., source_namespace_id).await.inspect_err(..).ok(); }
    return Err(err);
}
// Post-commit: publish the new edge ... Its failure cannot be reported — the rename happened — so it is logged and left to reconciliation
if reparented { authorizer.attach_tabular_parent(.., destination_namespace_id).await.inspect_err(..).ok(); }
```

The reasoning is in the function's doc comment: "Detach-then-commit-then-attach, so that every failure mode leaves the
authorizer *missing* an edge rather than holding an extra one". It names three residual windows itself: an ambiguous
commit, a failed post-commit attach ("leaves the tabular parentless, and no retry repairs it … Reconciliation is the
repair path, not retry"), and two renames of the same table in flight ("their OpenFGA writes are not ordered against
each other … Only `--mode add-and-delete-drift` retracts the survivor").

#### Warehouse create and delete — `api/management/v1/warehouse/mod.rs`

**Create** (`:555`): authorize; then, outside any transaction and in parallel, validate storage access with the supplied
credential and check no other warehouse of the project overlaps the location; `WriteTransaction::begin`; store the
credential in the secret store ("The secret is stored outside the transaction, so a rollback does not remove it");
insert the warehouse row; `authorizer.create_warehouse`; bootstrap grant rows; commit. On any failure
`clean_up_failed_warehouse_creation` deletes the tuples (only if `authz_relations_written`: "a failing `create_warehouse`
may have failed *because* the id already carries relations, and deleting then would strip the rightful holder's grants")
and the secret; on an unknown commit state it deletes nothing and logs "if the warehouse does not exist, its id cannot be
used again and the secret is orphaned".

**Delete** (`:1266`): one transaction deletes the warehouse's task schedules, cancels its tasks
(`cancel_tasks_on_warehouse_delete`), checks the spec is mutable, and deletes the row, returning the secret id "from the
row the delete removed, inside this transaction, so it cannot name a credential a concurrent rotation replaced". After
commit, best effort and logged on failure: `authorizer.delete_warehouse(...)`, then `secrets.delete_secret(...)`
("deleting inside the transaction would destroy the credential of a warehouse that still exists if the commit then
failed"). A warehouse delete does not delete data files.

#### Namespace create and drop — `server/namespace.rs`

Create: transaction opened, row inserted, idempotency key inserted, `authorizer.create_namespace` (line 370), bootstrap
grants, `t.commit()` (line 385). I found **no compensation** in this path for tuples written when the commit then fails
(no guard, no `delete_namespace` call before line 803), unlike table and warehouse create. Drop (including recursive):
rows deleted and purge tasks scheduled in one transaction, then post-commit `delete_namespace`, `delete_table`,
`delete_view` per child, each best effort (`:893-954`).

### The pattern, stated once

| Effect | Relative to the DB commit | On its failure |
|---|---|---|
| Catalog rows, normalised metadata, task rows, grant rows (non-OpenFGA), idempotency record, cache-change records | inside | rolled back together |
| `metadata.json` write | before | compensating delete, best effort; kept if commit state unknown |
| OpenFGA tuples that **add** access (create hooks, attach parent) | create: before commit, compensated on failure; attach: after commit | logged; "left to reconciliation" |
| OpenFGA tuples that **remove** access on a move (detach parent) | before commit, failure fails the request | old edge re-attached best effort if the commit fails |
| OpenFGA tuple deletion on drop | after commit | logged only; tuples for a dead id remain |
| Secret store write / delete | create: before commit; delete: after commit | compensating delete / logged orphan |
| Object-store data deletion | never in the request — a task | retried by the task queue |
| Events | after commit, spawned | logged; event lost |

The direction is consistent: every ordering is chosen so that a failure leaves the system **fail-closed** (a missing
permission edge, an unreferenced file, an orphan tuple for an id nobody can address) rather than fail-open.

### Conceptually strong

- One transactional store makes multi-row and multi-table changes atomic without any protocol: a multi-table commit, a
  drop together with its expiration task, a delete of a warehouse together with cancellation of its tasks, a write
  together with its idempotency record and its cache-invalidation record.
- The "commit state unknown" outcome is a first-class, tested error type, and every compensation path checks it before
  deleting anything.
- Side-effect ordering is written down as a contract on the trait (`detach_*`/`attach_*` doc comments), with the residual
  windows enumerated by the authors.

### Weak or absent

- Atomicity ends at the database. With OpenFGA, catalog and tuples can diverge; the repair is a **manually run CLI**
  (dimension 4), not a background reconciler.
- Tuple deletion after a drop is best effort with no retry; nothing reclaims tuples for dropped objects (the reconcile
  command leaves tuples "whose endpoints are *both* unknown" alone, and never touches ownership or grants).
- Namespace create has no compensation for tuples if its commit fails (as read).
- A crash between a pre-commit side effect and the commit leaves an orphan file or orphan tuples with no sweeper in the
  open-source edition (remove-orphan-files is a Plus task).
- Postgres is a hard dependency and single point: "We currently support only Postgres" (`docs/docs/concepts.md`).

---

## 2. Background work

### What it is

A task queue for work that must outlive a request: finalising soft-deleted tables when their grace period ends, deleting
a dropped table's files, cleaning old task logs — and, in Plus, snapshot expiry, orphan-file removal and compaction.

### How it is built

- **Storage**: Postgres tables `task` (active tasks), `task_log` (finished attempts), `task_config` (per queue, per
  project and optionally per warehouse), `task_schedule` (recurring jobs). A task is enqueued through
  `schedule_task(..., transaction)`, i.e. in the caller's transaction.
- **Queues in the open-source build** (`crates/lakekeeper/src/service/tasks/`): `soft_deletion` (legacy name
  `tabular_expiration`), `tabular_purge`, `task_log_cleanup`. Plus adds `expire_snapshots`, `remove_orphan_files`,
  `optimize` **(docs** `docs/docs/configuration.md:141`**)**. Custom queues can be registered by a downstream binary
  (`task_registry.rs`; `docs/docs/customize.md`).
- **Uniqueness**: one active task per `(project_id, warehouse_id, entity_type, entity_id, queue_name)` — inserts use
  `ON CONFLICT (...) DO NOTHING` (`crates/lakekeeper-storage-postgres/src/tasks.rs:519`).
- **Claim** (`pick_task_with_retry_policy`, `tasks.rs:614`): one statement selects the oldest due `scheduled` row with
  `FOR UPDATE OF t SKIP LOCKED LIMIT 1` and updates it to `status = 'running', picked_up_at = now(),
  last_heartbeat_at = now(), attempt = task.attempt + 1`, guarded by `task.attempt = p.attempt`. Any number of workers
  on any number of replicas can poll; `SKIP LOCKED` keeps them from blocking each other.
- **Timeout**: before each pick, a worker fails at most one attempt of its queue whose heartbeat is older than the
  queue's `max_time_since_last_heartbeat` (120 s default for soft deletion; configurable per warehouse in `task_config`)
  (`fail_timed_out_attempt`, `tasks.rs:911`). A long purge renews its heartbeat on an interval (`with_heartbeat`,
  `tabular_purge_queue.rs:268`).
- **Retry** **(docs** `configuration.md:137`**)**: "A task that fails is tried up to ten times in total; a try whose
  worker stops responding counts as failed. Each new try waits twice as long as the one before, starting at about 30
  seconds and at most about an hour". Code: `RetryPolicy`, `default_retry_backoff` (`tasks/mod.rs:147-238`), with jitter.
- **Guarantee**: at-least-once. A worker that dies after doing the work but before `record_success` is retried after the
  heartbeat timeout. The soft-deletion worker avoids double effects by doing its catalog change and
  `record_success_in_transaction` in one transaction (`tabular_expiration_queue.rs`, `handle_table`). The purge is
  idempotent by nature (delete everything under a location).
- **Schedules** (`tasks/task_schedule.rs`): "Ticks fall on `anchor + k * interval` … Each due tick inserts a task whose
  entity id is the schedule id, so at most one instance per schedule is active." Missed ticks are folded into one run and
  counted in `missed_runs`. Only API-serving instances materialise schedules **(docs)**.
- **Scaling**: every process runs workers by default (2 per built-in queue). `LAKEKEEPER__SERVE_HTTP_API=false` makes a
  headless worker; `LAKEKEEPER__TASK_*_WORKERS=0` removes a queue from a process. The API pods expose
  `/task-queue/<queue>/demand` on the metrics port so a KEDA `ScaledJob` can start worker Jobs only while tasks wait;
  workers exit after `LAKEKEEPER__TASK_IDLE_EXIT_AFTER` (`docs/docs/task-workers.md`). Graceful drain:
  `LAKEKEEPER__TASK_DRAIN_TIMEOUT`.
- **Management API**: list, inspect, cancel, run-now and reschedule tasks, and get/set queue config per warehouse or
  project (`api/management/v1/tasks.rs`, `task_queue.rs`; e.g.
  `/management/v1/warehouse/{warehouse_id}/task-queue/{queue}/config`).

The soft-deletion worker's order is worth noting (`tabular_expiration_queue.rs`): inside its open transaction it drops
the row, then calls `authorizer.delete_table(...)` best effort (`.ok()`), then schedules the purge task and records
success, then commits. The tuples are therefore deleted **before** the commit; if the commit fails the table still exists
soft-deleted but without tuples until the retry drops it.

### Conceptually strong

- The queue is in the same database as the state, so enqueueing is transactional with the change that needs it. No
  message is lost between "state changed" and "work scheduled".
- Claim, heartbeat, timeout, backoff, uniqueness and cancellation are generic; a new kind of work is a new queue name
  and a payload type.
- Workers scale to zero and by demand, and can be separated from API pods by configuration only.

### Weak or absent

- Polling (default 10 s), not push.
- The open-source edition has three queues; all table maintenance is Plus.
- At-least-once: a task body must be idempotent; nothing enforces it.

---

## 3. Soft delete, protection, undrop

### What it is

Three safety mechanisms on drop: a grace period during which a dropped table can be restored, a per-object flag that
refuses deletion, and an override.

### How it is built

- **Profile per warehouse**: `TabularDeleteProfile::Hard {}` or `Soft { expiration_seconds }`
  (`warehouse.tabular_delete_profile`). "The expiration delay is fixed at the time of dropping" **(docs** `concepts.md`**)**
  — it is the `scheduled_for` of the task row.
- **State machine of a tabular** (table, view, generic table):
  - `active` (`deleted_at IS NULL`) → *drop in soft warehouse* → `soft-deleted` (`deleted_at` set, expiration task
    `scheduled`) → *task runs* → row deleted, tuples deleted best effort, purge task enqueued if the drop asked for purge
    → files removed.
  - `soft-deleted` → *undrop* → `active`: `clear_tabular_deleted_at` sets `deleted_at = NULL` and
    `TabularExpirationTask::cancel_scheduled_tasks` cancels the task, in one transaction
    (`api/management/v1/warehouse/mod.rs:2041-2064`). The SQL locks both the tabular rows and their task rows
    (`FOR UPDATE OF t`, `FOR UPDATE OF ta`, `tabular/mod.rs:1788-1806`), so undrop and expiry cannot both win.
  - `active` → *drop in hard warehouse, or drop with `force`* → row deleted at once.
  - A staged table (created with `stage-create`, no metadata location) is a further state, replaced on a real create.
- A soft-deleted table keeps its UUID, its location reservation and its authorization tuples; "its metadata file cannot
  be registered again; restore the table or view instead" **(docs)**. Namespaces cannot be soft-deleted.
- **Protection**: `protected` boolean on warehouse, namespace, table, view, generic table, set through the management
  API. Enforced in SQL: `AND ((NOT lt.protected) OR $4)` where `$4` is `force` (`mark_tabular_as_deleted`).
- **Force**: bypasses protection and soft deletion. In a soft-delete warehouse it needs an extra permission, the one that
  changes the warehouse's soft-deletion settings (`authorize_soft_deletion_skip`; **(docs)** § Skipping Soft Deletion),
  and so do recursive namespace deletion, a register-with-overwrite that replaces a different table, and running a
  soft-deletion task early through the task API.
- Soft-deleted tabulars are listable through a management endpoint with their `deletion_scheduled_for`.

### Conceptually strong

- The whole lifecycle is two columns and one task row in one database, so drop, undrop and expiry are each one
  transaction and race-free by row locks.
- "Skip the safety net" is a separate permission from "drop".

### Weak or absent

- Soft delete protects the catalog entry only. `docs/docs/concepts.md` § What Lakekeeper Enforces: "A writer can therefore
  overwrite or delete any file of the table, including the files of older versions, and Lakekeeper cannot see or prevent
  it. Snapshots, time travel, soft deletion and protection guard against changes made through the catalog, not against
  changes to the files themselves."
- No soft delete for namespaces or warehouses.

---

## 4. Authorization

### What it is

Every request is decided by one configured **Authorizer**. Open source ships OpenFGA (relationship tuples in an external
service) and AllowAll; Plus ships Cedar (policy files evaluated in process) **(docs** `authorization.md`**)**. An OPA
bridge (`authz/opa-bridge/`) lets Trino ask Lakekeeper for decisions.

### The OpenFGA model — `authz/openfga/v4.16/components/*.fga`

Types: `server`, `project`, `warehouse`, `namespace`, `lakekeeper_table`, `lakekeeper_view`, `lakekeeper_generic_table`,
`lakekeeper_catalog_tag`, `role`, `user`, `model_version`.

- **Hierarchy edges are tuples**: `lakekeeper_table#parent: [namespace]`; `namespace#parent: [namespace, warehouse]` and
  the inverse `namespace#child`; `warehouse` ↔ `project`; `project#server`.
- **Derivation down the tree**, e.g. on `lakekeeper_table`:
  `define modify_effective: modify or ownership or modify_effective from parent`;
  `define select_effective: select or ownership or write_effective or select_effective from parent`;
  actions read the effective relation: `define can_commit: write_effective`, `define can_drop: modify_effective`,
  `define can_read_data: select_effective`.
- **Each privilege is two relations**: "the bare name is what was granted here, and grants are written to and listed from
  it; the `_effective` twin adds the weaker privileges it subsumes" (`project.fga`). Tiers: `describe` < `select` <
  `write` < `modify`, plus `create`.
- **Visibility bubbles up**: `define visible_below: describe or select or create or write or modify or ownership or
  visible_below from child` — a grant on one deep table makes the containers above it listable.
- **Grant administration is in the model**: `pass_grants`, `manage_grants`, `can_grant_<privilege>`,
  `can_revoke_<privilege>`; `managed_access` on a namespace removes the owner's implicit right to grant
  (`manage_grants: [...] or (ownership but not managed_access_inheritance from parent) or manage_grants from parent`).
- **Built-in roles**: server `admin` and `operator`; project `project_admin`, `security_admin`, `data_admin`,
  `role_creator`, `tag_creator`. Roles are a type with `assignee: [user, role#assignee]`, so roles nest and a principal
  can assume a role (`x-assume-role`, `check_assume_role_impl`).
- The model is **versioned and migrated** (`authz/openfga/v2.1 … v4.16`, a changelog stating per version whether tuples
  are modified; `crates/authz-openfga/src/migration.rs`), applied by `lakekeeper migrate`.

### Where the check happens

In the handler, before the write transaction: e.g. `load_and_authorize_table_operation`
(`service/authz/table.rs:722`) fetches warehouse, namespace and table in parallel, re-validates ids and versions "with
TOCTOU protection" (`refresh_warehouse_and_namespace_if_needed`), then calls the authorizer. The result is passed through
`event_ctx.emit_authz(...)`, which writes the audit record for the decision (dimension 7). Checks are batched
(`are_allowed_*_actions_impl`, `max_batch_check_size` in `crates/authz-openfga/src/config.rs`).

**Listing**: the handler first asks one coarse question on the namespace, `CatalogNamespaceAction::ListEverything`
(`server/tables.rs:267-284`, relation `can_list_everything: describe_effective`); if it is denied, items are checked
individually against `can_include_in_list` (I read the shortcut and the relation; I did not read the per-item batching
loop in full).

**Caching**: I found no cache of OpenFGA decisions (a grep for "cache" in `crates/authz-openfga/src` finds only catalog
role lookups). What is cached, per process with TTL (`docs/docs/configuration.md` § Caches): warehouses (60 s),
namespaces (60 s), secrets (600 s), roles, role assignments, users, short-term credentials. Cross-replica invalidation:
"Every write that changes a cached entry also writes a change record to the database, in the same transaction. Each
instance reads the new change records from the primary database every `LAKEKEEPER__CACHE__INVALIDATION__POLL_INTERVAL`
(default 1 second)" — table `cache_change_log`. `AGENTS.md`: "Never rely on per-process caches for cross-replica
correctness: cross-replica eviction is best effort, and the TTL stays the bound"; management routes bypass caches.

### Bootstrap and break-glass

- `POST /management/v1/bootstrap`, once: the calling principal gets server `admin` (or `operator` with
  `"is-operator": true`). OpenFGA implementation writes one tuple idempotently (`authorizer.rs:151`). The flag
  `open_for_bootstrap` lives in Postgres; `lakekeeper reopen-bootstrap --yes` flips it back
  (`crates/lakekeeper-bin/src/main.rs`).
- **Instance admins** (`LAKEKEEPER__INSTANCE_ADMINS`, static configuration): "principals listed directly in Lakekeeper's
  static configuration. They bypass the configured Authorizer for administrative actions, so they can still manage the
  catalog when the Authorizer itself is broken or misconfigured" **(docs** `instance-admins.md`**)**. They do **not**
  bypass data-plane actions (`ReadData`, `WriteData`, view `Select`) — tests
  `test_instance_admin_does_not_bypass_data_plane_table_actions` (`service/authz/mod.rs:5781`).
- A warehouse can be marked `managed-by: instance-admin`, which locks its spec against its own grant holders
  (`ManagedBy`, `service/catalog_store/warehouse.rs:90`).

### Grants and the management API

A uniform Grants API (`api/management/v1/grant.rs`, 4322 lines) over an authorizer-owned vocabulary
(`grantable_privileges`), including subtree read and revoke. Role CRUD, role membership, assume-role, a `check` endpoint
to ask "may X do Y" (`api/management/v1/check.rs`). Under OpenFGA, grants and assignments are read from and written to
OpenFGA; under others they are rows in `grant_assignment`.

### Pluggability

`pub trait Authorizer` (`service/authz/mod.rs:2689`): associated action types per resource level, `are_allowed_*_impl`
batch checks, lifecycle hooks (`create_*`, `delete_*`, `detach_*_parent`, `attach_*_parent`), optional facets
(`role_assignments()`, `grants()`), `bootstrap`, its own router and OpenAPI. The binary chooses between two
(`crates/lakekeeper-bin/src/authorizer.rs`: `AuthorizerEnum::{AllowAll, OpenFGA}`). A custom authorizer means building
your own binary (`docs/docs/customize.md`).

### Tuples through an object's life

| Event | Tuple write | Relative to DB commit | If it fails |
|---|---|---|---|
| Create table / view / generic table | `require_no_relations` (read at higher consistency), then one write: parent edge + ownership | before commit | request fails; guard deletes the tuples best effort; kept if commit state unknown |
| Create namespace | same | before commit | request fails; no compensation found if the *commit* then fails |
| Create warehouse / project / role / tag | same | before commit, inside the open transaction | rollback; warehouse path deletes tuples it wrote |
| Rename in place | none | — | — |
| Rename across namespaces / move namespace | delete old parent edge | **before** commit, hard error | old edge re-attached best effort if commit fails |
| | write new parent edge | **after** commit | logged; object is parentless until reconcile |
| Soft drop | none | — | — |
| Undrop | none | — | — |
| Hard drop / expiry of a soft drop | delete all relations of the object (as object and as user) | after commit (request) / before commit (expiry task) | logged only |
| Delete warehouse / namespace / role / user | delete all relations | after commit | logged only |

The parent edge is two tuples (forward and inverse), so detach and attach use idempotent writes with per-tuple
`on_missing`/`on_duplicate: Ignore`, and the comment explains why a strict write would be wrong: "a strict write is
atomic: if only one of them is already gone the whole delete fails, so treating that error as 'already applied' would
leave the surviving tuple in place — a stale parent edge" (`authorizer.rs`, `detach_namespace_parent`).

### What repairs a mismatch

`lakekeeper openfga reconcile [--mode add-missing | add-and-delete-drift] [--dry-run]`
(`crates/lakekeeper-bin/src/main.rs:150`, `crates/authz-openfga/src/reconcile.rs`, 1569 lines). From its module doc:

- "Catalog is the source of truth: missing edges are added, and (in `add-and-delete-drift` mode) drift is removed."
- Only **structural** tuples: "Ownership tuples (`actor -[ownership]-> *`), grants, role assignments, and bootstrap admin
  tuples are **never** touched." "Tuples whose endpoints are *both* unknown are left alone".
- Delete mode takes a Postgres advisory lock (`RECONCILE_LOCK_KEY`) to serialise reconciles; it "does **not** block API
  writes — operators should run during low-traffic windows", and races "self-heal on the **next** reconcile run".
- "A future revision will add a shared advisory lock on the authorizer's write paths".

It is an operator-run command. I found no scheduled or automatic invocation of it.

### Conceptually strong

- Inheritance, ownership, grant delegation, managed access and bottom-up visibility are all expressed in the model, and
  the Rust code is forbidden from re-deriving them ("Don't re-encode the OpenFGA model in Rust", `AGENTS.md`).
- A break-glass identity that does not depend on the authorizer, scoped to the control plane only.
- Fail-closed ordering for moves, and a catalog-is-truth repair tool with a dry run.
- The authorizer is a real seam: grants can live in the catalog's own transaction (non-OpenFGA) or in the authorizer.

### Weak or absent

- With OpenFGA the tuple store is eventually consistent with the catalog, repaired by hand.
- Ownership and grant tuples of dropped objects are never garbage-collected by reconcile.
- No row filters or column masks: "Neither engine expresses row filters or column masks" **(docs)**.
- No decision cache; every request costs OpenFGA round trips (batched).
- Attribute conditions (time, tags, what a commit changes) exist only under Cedar, i.e. Plus.

---

## 5. Authentication

### What it is

Verifies a bearer token and maps it to a principal id.

### How it is built — `service/authn.rs` (2957 lines), `docs/docs/authentication.md`, `docs/docs/configuration.md:265-346`

- **Several issuers at once**: a primary OIDC provider (`LAKEKEEPER__OPENID_PROVIDER_URI`, `idp_id = "oidc"`), any
  number of named providers (`LAKEKEEPER__OPENID_PROVIDERS__<IDP_ID>__…`), and Kubernetes. "Tokens are tried against
  authenticators in this order: the primary provider …, then providers … in alphabetical order of `idp_id`, then
  Kubernetes" **(docs)**.
- **Kubernetes service accounts**: validated with the `TokenReview` API (needs `system:auth-delegator`; the chart can
  create the ClusterRoleBinding, `auth.k8s.createClusterRoleBinding`). Audience check via
  `LAKEKEEPER__KUBERNETES_AUTHENTICATION_AUDIENCE`; the docs warn "If … not set, all tokens proceed to validation!".
  Subject is the service account UID by default (`kubernetes~<uid>`) or `system:serviceaccount:<ns>:<name>` with
  `…_SUBJECT_SOURCE=username`, "stable across clusters and suitable for pre-provisioning".
- **Principal id**: `<idp_id>~<subject>`; subject claim configurable (`LAKEKEEPER__OPENID_SUBJECT_CLAIM`, default `oid`
  then `sub`). Audience (`LAKEKEEPER__OPENID_AUDIENCE`) optional — "If unset, audience validation is **skipped**". Also:
  required scope, required-claim rules (`…REQUIRED_CLAIMS__<RULE>__ANY_OF/ALL_OF/NONE_OF/EXISTS`), a roles claim (used by
  Cedar and custom authorizers; "The default OpenFGA implementation does not use token roles").
- **User records**: rows in `users`. Created explicitly (`POST /management/v1/user`, which the UI calls at login) or
  "Implicit on-the-fly creation when calling GET `/catalog/v1/config`" **(docs** `concepts.md:136-139`**)**. Name and
  email are parsed from the token at first sight.
- Plus adds **role providers** (LDAP, Entra, Okta, token) that sync group membership into roles per user at login, and
  **admission gates** (an external allow/deny before any handler) **(docs)**. The `AdmissionGate` trait itself is open
  source (`service/admission.rs`).
- Authentication can be off entirely (no provider configured); authorization requires it.

### Strong / weak

Strong: multiple issuers with stable namespaced ids; first-class workload identity without an IdP; required-claim rules.
Weak: audience validation is opt-in for OIDC and for Kubernetes; `TokenReview` needs a cluster-wide RBAC grant and a call
per (uncached) token; changing an `idp_id` or the subject source orphans all assignments **(docs)**.

---

## 6. Storage access

### What it is

A warehouse binds a name to an object-store location and a credential. Clients never get that credential: they get
short-lived, down-scoped credentials or per-request signatures for one table's location.

### How it is built

- **Storage profile per warehouse** (`service/storage/`): S3 and S3-compatible (flavours), ADLS, OneLake, GCS, STACKIT.
  The credential is stored in the secret store and referenced by `storage_secret_id`.
- **Vended credentials**: S3 via STS `AssumeRole` with a session policy scoped to the table location and to the
  permission level (`StoragePermissions::ReadWriteDelete` / read), validity `sts-token-validity-seconds` (default 3600),
  optional session tags; Azure SAS; GCS downscoped tokens. Cached in process for half their lifetime (STC cache).
- **Remote signing** (S3): "clients get no storage credentials. Before each S3 request, the client asks Lakekeeper to
  sign it. Lakekeeper checks that the user may access the table and that the request stays inside the table's location"
  (**(docs)** `storage-s3.md`; code `server/s3_signer/sign.rs`, 3368 lines). Revocation is immediate for signing, bounded
  by token lifetime for vending.
- **Layout** (`service/storage/storage_layout.rs`, `docs/docs/storage-layout.md`): default is flat,
  `<base>/<uuid-of-tabular>/`; alternatives `full-hierarchy` and `tabular-only` with `{name}`/`{uuid}` templates. A
  client-supplied location is admitted only as a strict sublocation of the warehouse base. Metadata files are
  `<n>-<uuid>.metadata.json`. Under name-based layouts, namespace rename or move is refused
  (`StorageLayoutForbidsNamespaceMove`).
- **Overlap prevention between tables** (`crates/lakekeeper-storage-postgres/src/tabular/mod.rs:531-625`):
  `ensure_location_available` "Errors with `LocationAlreadyTaken` if any other tabular in `warehouse_id` occupies
  `location`, sits under it, or contains it, or if a queued or running purge task will delete such a location." Because
  "no index on `fs_location` is unique", concurrent creates serialise on **Postgres advisory locks** keyed by a hash of
  the location and each of its path prefixes: "locking a location together with its prefixes makes any two colliding
  creates share a key and serialize". Additionally the location must be empty on the store (`require_empty_location`).
- **Overlap between warehouses**: `ensure_no_storage_overlap` lists the project's warehouses (including inactive) and
  compares in the handler (`warehouse/mod.rs:2473`). It is per project and is a read before the transaction, not a
  locked check. **(inferred:** two concurrent creates, or warehouses in different projects, are not excluded by it.**)**
- **Validation at warehouse create** (`service/storage/validation.rs`; `docs/docs/storage-validation.md`): the profile is
  normalised; Lakekeeper writes, reads back and deletes a probe file; issues down-scoped credentials and checks they work
  inside and are **refused outside** the table location (`vended-credentials-scope-enforced`); CORS and bucket-policy
  warnings. The same checks are available as dry-run endpoints (`POST /management/v1/warehouse-creation-validation` and
  three `validate-*` endpoints on an existing warehouse) returning a per-check report.

### Strong / weak

Strong: scope enforcement is *proved* at create time by a negative probe; two access modes with different revocation
properties; location exclusivity includes pending purges; a dry-run validator sharing the create request type.
Weak (stated by the docs): a writer's credential can overwrite or delete any file under the table location, and "can
commit metadata that points to files outside the table's location"; the warehouse-overlap check is advisory in scope.

---

## 7. Events and audit

### What it is

Two separate outputs: **change events** for other systems (CloudEvents to NATS/Kafka), and an **audit log** of who tried
what and what was decided.

### How it is built

- **Dispatch** (`service/events/dispatch.rs`): an `EventDispatcher` holds `EventListener`s; ~60 hook methods
  (`table_created`, `transaction_committed`, `warehouse_deleted`, `authorization_failed`, `authorization_succeeded`,
  `idempotent_replay_served`, `grants_changed`, `operation_recorded`, …). Hooks fire **after** the operation; most are
  spawned (`tokio::spawn`). Contract: "If the listener fails, it will be logged, but the request will continue to
  process." Custom listeners require building your own binary.
- **CloudEvents publisher** (`service/events/publisher.rs`): implements only tabular events —
  `transaction_committed`, `table_created/registered/dropped/renamed`, `view_created/registered/committed/dropped/renamed`,
  `generic_table_created/dropped/renamed`, `tabular_undropped`. It pushes onto an in-process
  `tokio::sync::mpsc` channel with `send_timeout`; a background task builds a CloudEvents 1.0 event (source
  `uri:iceberg-catalog-service:<hostname>`, extensions `tabular-id`, `warehouse-id`, `name`, `namespace`,
  `sequence-number`, `trace-id`, `actor`) and sends it to every sink; a sink failure is a `tracing::warn!`.
- **Sinks**: NATS (`crates/lakekeeper-events-nats`: `.publish(self.topic.clone(), …)` to one configured subject), Kafka
  (`crates/lakekeeper-events-kafka`, rust-rdkafka), and logging (`LAKEKEEPER__LOG_CLOUDEVENTS=true`).
- **Delivery guarantee**: best effort, at most once. The event is created after the database commit, held only in memory,
  and dropped if the channel is full past the timeout, if the sink errors, or if the process dies. There is no outbox
  table and no replay. A commit that changes nothing publishes no event.
- **Not emitted as CloudEvents**: namespace, warehouse, project, role, grant, user and task events (the dispatcher has
  the hooks; the publisher does not implement them); reads; anything about data files.
- **Audit** (`service/events/backends/audit/`, `crates/audit-macros`, `audit-format/`, `docs/docs/audit/schema.json`):
  structured **log records** with `"event_source": "audit"`, selected by a tracing target (`AUDIT_TARGET`, filter with
  `RUST_LOG`) **(docs** `logging.md`**)**. Three shapes: `authorization` (every access check and its decision, with a
  per-decision breakdown), `replay` (a request answered from an idempotency record), `operation` (something the system
  did for a user: role resolution, a grant write). The record format is versioned (`AUDIT_FORMAT = "1.2"`), has a JSON
  schema, fixtures regenerated by tooling, and a CI check; handler rules in `AGENTS.md` require that "From the first
  authorization step on, audit every failure". Destination is the process's log stream; shipping and retention are the
  operator's.

### Strong / weak

Strong: the audit record is a designed, versioned, schema-checked product surface covering allow *and* deny; the
listener trait gives one seam for all side outputs.
Weak: change events are lossy by construction and cover tabulars only; the audit trail is log lines with whatever
durability the log pipeline has; core NATS publish to one subject, no per-warehouse subject **(as read at
`lakekeeper-events-nats/src/lib.rs:83`)**.

---

## 8. Idempotency and request safety

### What it is

An implementation of the Iceberg REST `Idempotency-Key` header so a client can safely retry a mutation.

### How it is built — `service/idempotency.rs`, `service/catalog_store/idempotency.rs`, table `idempotency_record`

- Opt-in per request; the key must be a UUID (any version). Covered endpoints (collected from
  `EndpointFlat::` uses in handlers): create/drop namespace, update namespace properties, create/register/update/drop/
  rename table, commit transaction, register/replace/drop/rename view, create generic table.
- **The record is inserted in the same transaction as the mutation**, so "records only exist for committed successes.
  There is no 'in-progress' state and no stored error bodies". A replay does not return a stored body: "the handler
  should re-derive its success response rather than execute again".
- A concurrent duplicate loses the insert, rolls back and gets `request_in_progress`. Reuse on another endpoint returns
  `400 IdempotencyKeyReused`; reuse on the same endpoint for a different object "is not detected" **(docs)**.
- Lifetime 30 min + 5 min grace, advertised in `getConfig`; background cleanup. A served replay is audited
  (`emit_idempotent_replay`).
- **Other request safety** (`api/router.rs`): body limit `LAKEKEEPER__MAX_REQUEST_BODY_SIZE` (default 32 MB), request
  timeout `LAKEKEEPER__MAX_REQUEST_TIME` (default 30 s, `TimeoutLayer`), panic catching, compression; a bound on
  `referenced-by` view chains; a commit-size bound (`validate_commit_count`); **maintenance mode** `read-only` returning
  503 with `Retry-After` for mutations (`service/maintenance_mode.rs`), switchable per pod.
- **Rate limiting / load shedding**: none found (grep for `rate_limit`, `governor`, `ConcurrencyLimit`, `LoadShed` in
  `crates/lakekeeper` and `Cargo.toml` finds nothing).

### Strong / weak

Strong: exactly-once effect for retried mutations at the cost of one row in the mutation's own transaction — only
possible because state and record share a database. Weak: no rate limiting or per-tenant quota in the open-source code;
management-API mutations (warehouse, grants, roles) are not idempotency-keyed.

---

## 9. Tenancy and configuration as code

### What it is

Hierarchy: server → project → warehouse → namespace → table/view/generic table. A project is the tenancy unit (roles,
tags, task configuration and statistics are project-scoped); a warehouse is a storage binding and the `prefix` in catalog
URLs.

### How it is built

- All of it is **runtime state created through the management API** and stored in Postgres. Nothing is declared in the
  chart: the chart's values have no project, warehouse, role or grant section (`lakekeeper-charts`
  `charts/lakekeeper/values.yaml` top-level keys: `catalog`, `optimizeWorkers`, `serviceAccount`, `secretBackend`,
  `postgresql`, `externalDatabase`, `auth`, `authz`, `internalOpenFGA`, `openfga`, `OPABridge`).
- **The chart installs**: the catalog Deployment (+ Service, Ingress, HPA, PDB), a DB-migration Job
  (`templates/db-migration.yaml`: Helm `post-install,post-upgrade` hook, or Argo CD `hook: Sync`, `sync-wave: "0"`, named
  per release revision), an init container on every pod that waits for the database to be migrated (`wait-for-db -dm`),
  an encryption-key Secret, optional OPA bridge, Plus optimize workers (Deployment or KEDA ScaledJob). Dependencies: a
  bundled Postgres subchart, **enabled by default** with the warning "embedded Postgres is NOT recommended for
  production", and an optional OpenFGA subchart (`internalOpenFGA: false`). Default `authz.backend: "allowall"`;
  authentication off unless configured.
- **Bootstrap** is not a chart job: the only chart artefact that bootstraps is the `helm test` pod
  (`templates/tests/bootstrap.yaml`) posting to `/management/v1/bootstrap` with its service-account token.
- **Migrations** are explicit (`lakekeeper migrate`), `serve` "refuses to start while the database is not migrated for
  its version" **(docs)**; the OpenFGA model migration runs in the same command.
- **Operator** (`lakekeeper-operator`, README: "Work in Progress — … not yet production-ready"): one CRD, `Lakekeeper`
  (`config/crd/bases/lakekeeper.k8s.lakekeeper.io_lakekeepers.yaml`). It reconciles the Deployment, the migration Job,
  services, and bootstrap (detects via `GET /management/v1/info`; optionally calls bootstrap when
  `spec.bootstrap.enabled`). `docs/architecture.md` lists `LakekeeperProject`, `LakekeeperWarehouse`,
  `LakekeeperNamespace`, `LakekeeperUser`, `LakekeeperRole` as "📋 Planned". Projects, warehouses, roles and grants are
  therefore left to the API today.
- **Terraform/OpenTofu provider** (`lakekeeper/terraform-provider-lakekeeper`, file listing only): resources for project,
  warehouse, namespace, role, user, protection flags, and user/role assignments at server, project, warehouse, namespace
  and role level. This is the existing declarative path for catalog objects.
- **Hooks for a control plane in the catalog itself**: the server `operator` role "designed to be used by machines that
  provision resources inside Lakekeeper, i.e. a `KubernetesOperator`" (`server.fga`); instance admins from static
  config; `managed-by: instance-admin` on a warehouse; caller-supplied `warehouse-id` so "provisioning tools that need
  the ID up front" can be idempotent **(docs)**.

### Strong / weak

Strong: the catalog exposes the *primitives* a reconciler needs (a machine role, a break-glass identity, a per-object
"managed by" lock, client-chosen ids, dry-run validation), and a Terraform provider exists.
Weak: no declarative tenancy in the chart; the operator does not yet reconcile projects, warehouses or grants; desired
state lives in the database, so "what git declares" and "what the estate is" are connected only by an external tool.

---

## 10. Operations

- **Scale-out**: every replica is equal and stateless apart from caches; they share Postgres (read and write URLs can
  differ to use read replicas — `LAKEKEEPER__PG_DATABASE_URL_READ/WRITE`), the authorizer and the secret store. Cache
  coherence is the `cache_change_log` poll (≈1 s) with TTL as the bound. Workers can be split from API pods (dimension 2).
  Chart provides HPA and PDB.
- **Health**: one unauthenticated `GET /health` (`api/router.rs:346`) aggregating background-refreshed health of the
  registered services — the catalog's `read_pool` and `write_pool` (`crates/lakekeeper-storage-postgres/src/lib.rs:163`),
  the authorizer, the secret store — returning 200 or 503. The chart points **both** liveness and readiness at it
  (`catalog-deployment.yaml:113-131`). It also reports `maintenance_mode` **(docs)**. Headless workers serve the same
  route.
- **Metrics**: Prometheus on a separate port (9000): HTTP request metrics by method/status/endpoint, Tokio runtime
  metrics, task-queue metrics and demand, cache size/hit/miss per cache, cache-invalidation metrics, event-listener
  duration histogram, admission-gate metrics (`docs/docs/monitoring.md`).
- **Tracing**: no OpenTelemetry. A grep for `opentelemetry`/`otlp` over `crates/`, `Cargo.toml` and `docs/docs` returns
  nothing; `publisher.rs` carries the comment "Implement distributed tracing:
  https://github.com/lakekeeper/lakekeeper/issues/63". There is an `x-request-id` header and structured `tracing` logs.
- **Endpoint statistics**: per project and warehouse, in the database, queryable through the management API and shown in
  the UI — "the only source of per-project and per-warehouse breakdowns" **(docs)**.
- **Maintenance mode** and graceful task drain (dimensions 2 and 8); `lakekeeper healthcheck`, `wait-for-db`,
  `migrate`, `reopen-bootstrap`, `openfga reconcile` subcommands.

Weak: liveness depends on downstream health (a database outage fails liveness as well as readiness, as the chart is
written); no distributed tracing; no alert rules shipped (the docs give example rules and SQL).

---

## 11. What it does not do

| Capability | In open source? | Evidence |
|---|---|---|
| **Lineage** | No. | grep for "lineage" in `crates/` finds nothing; the docs mention it only as a use for custom event listeners: "feeding data-discovery and lineage tools" (`docs/docs/customize.md:53`). No OpenLineage. |
| **Data contracts / schema enforcement at commit** | A hook only. | `ContractVerification` trait (`service/contract_verification.rs`): `check_create_table`, `check_table_updates(updates, current_metadata)`, `check_drop`, `check_rename`, and view equivalents, returning `Clear` or `Violation{error_model}`; called before every create, commit, drop and rename. The default set is empty (`#[builder(default)] pub contract_verification: ContractVerifiers`, `serve.rs:150`; the binary passes none). `concepts.md`: "Data Contract System (optional): Lakekeeper can interface with external data contract systems". It sees metadata updates, not data. |
| **Judging a commit beyond the Iceberg requirements** | A little. | Format-version policy per warehouse (`ensure_format_version_upgrades_allowed`), immutable properties (`check_immutable_properties_not_modified`), location must stay inside the warehouse (`require_allowed_location`), refs kept (`ensure_refs_kept`), schema content stable (`commit_tables.rs`). Authorization can distinguish kinds of commit only under Cedar (Plus): "a policy can also check what a change to a table does, for example allow only commits that change data" **(docs)**. No run/provenance requirement on a write. |
| **Table maintenance** | Metadata-file cleanup only. | On commit, expired `metadata.json` files are deleted per `write.metadata.delete-after-commit.enabled` (`write_commits`). Expire snapshots, remove orphan files and optimize (compaction) are Plus task queues **(docs** `table-maintenance.md`, `optimize.md`**)** — in Plus the catalog itself runs maintenance and compaction workers. |
| **Table metrics reports** | Accepted and discarded. | `server/metrics.rs`: `async fn report_metrics(...) -> Result<()> { Ok(()) }`. |
| **Server-side scan planning** | No. | `SCAN_PLANNING_MODE_CLIENT`: "it serves no `planTableScan` endpoint". |
| **Row filters / column masks** | No. | `authorization.md`. |
| **Non-Iceberg tables (Lance, Parquet, CSV)** | Yes, as **generic tables**, without commit coordination. | `docs/docs/generic-tables.md`: name, `format` string, base location, free-form schema/statistics/properties; vended credentials, remote signing, soft delete, protection, rename, per-action permissions, events. "Commit coordination ❌ — The catalog does not arbitrate writes — engines write directly"; "Schema enforcement ❌ — Schema is stored, not validated against data files". A Lance integration test exists (`tests/integration-tests/lance/test_lance.py`). The API is Lakekeeper's own (`/lakekeeper/v1/...`), not the Lance Namespace specification. |
| **Durable event delivery / outbox** | No. | Dimension 7. |
| **Automatic catalog↔authorizer reconciliation** | No. | Dimension 4: a CLI. |
| **Rate limiting** | No. | Dimension 8. |

---

## Could not confirm

- **docs.lakekeeper.io itself** was not fetched; the documentation was read from `docs/docs/` at the commit above. A
  published page could lag or lead that source.
- **Lakekeeper Plus** (Cedar authorizer, role providers, admission-gate endpoint, expire snapshots, remove orphan files,
  optimize, table-metadata cache): closed source; everything said about it is from the docs.
- **Listing filter**: I read the `ListEverything` shortcut and the `can_include_in_list` relations, not the per-item
  batching code path end to end.
- **No OpenFGA decision cache**: concluded from the absence of cache configuration and cache code in
  `crates/authz-openfga/src`; I did not read `check.rs` (1245 lines) line by line.
- **Namespace-create compensation**: concluded from `server/namespace.rs` having no guard or `delete_namespace` call on
  the create path; I did not rule out a compensation inside the storage or authorizer layer.
- **Project delete** path was not read (the warehouse delete comment says "see `delete_project`" for the same pattern).
- **Warehouse statistics triggers**: saw trigger names in migrations only.
- **Audit destination**: taken from `docs/docs/logging.md` (log records on a tracing target); I did not trace
  `backends/audit/emitter.rs` to the writer.
- **NATS sink**: read one publish call; did not verify whether the client is core NATS or a JetStream context, nor the
  Kafka producer's acknowledgement settings.
- **Orphans after a crash** between a pre-commit side effect and the commit: inferred from the code order; no test or
  doc read that states it.
- **Automatic reconcile**: searched `main.rs` and `reconcile.rs`; found only the CLI entry. Did not search the chart for
  a CronJob beyond listing its templates (none present).
- **Terraform provider**: file names only; behaviour not read.
- **Concurrent warehouse creates with overlapping locations**: inferred from the check being a pre-transaction read
  scoped to one project; not tested.
- **Retry count for concurrent commits** (`MAX_RETRIES_ON_CONCURRENT_UPDATE`): constant's value not read.
